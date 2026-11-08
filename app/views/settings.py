"""Settings: AI key, email connection (Gmail OAuth / SMTP+IMAP / demo), sending rules, users."""
from __future__ import annotations

import secrets
from datetime import timedelta
from urllib.parse import urlencode

import requests
from flask import abort, current_app, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import generate_password_hash

from .. import utils
from ..services import ai, crypto
from ..services.domains import normalize_email
from ..services.transports import (GMAIL_SCOPES, GOOGLE_AUTH, GOOGLE_TOKEN, GmailApiTransport, SmtpImapTransport)
from .common import bp, db, flash_all, login_required


@bp.get("/settings")
@login_required("admin")
def settings():
    return redirect(url_for("main.settings_ai"))


# ------------------------------------------------------------------ AI
@bp.route("/settings/ai", methods=["GET", "POST"])
@login_required("admin")
def settings_ai():
    d, o = db(), g.org
    if request.method == "POST":
        action = request.form.get("action", "save")
        provider = request.form.get("provider", "")
        model = request.form.get("model", "").strip()[:80]
        key = request.form.get("key", "").strip()
        if action == "remove":
            d.x("UPDATE orgs SET ai_provider='', ai_model='', ai_key_enc='' WHERE id=?", (o["id"],))
            flash("AI key removed.", "ok")
            return redirect(url_for("main.settings_ai"))
        if provider not in ai.PROVIDERS:
            flash("Choose a provider.", "error")
            return redirect(url_for("main.settings_ai"))
        model = ai.normalize_model(provider, model)
        if key:
            d.x("UPDATE orgs SET ai_key_enc=? WHERE id=?", (crypto.encrypt(key), o["id"]))
        d.x("UPDATE orgs SET ai_provider=?, ai_model=? WHERE id=?", (provider, model, o["id"]))
        o = d.one("SELECT * FROM orgs WHERE id=?", (o["id"],))
        if action == "test":
            try:
                flash(ai.test_key(provider, model, crypto.decrypt(o["ai_key_enc"])), "ok")
            except ai.AIError as e:
                flash(str(e), "error")
        else:
            flash("AI settings saved.", "ok")
        return redirect(url_for("main.settings_ai"))
    default_model = ai.normalize_model(o["ai_provider"] or "gemini", o["ai_model"])
    return render_template("settings_ai.html", providers=ai.PROVIDERS, default_model=default_model,
                           masked=crypto.mask(crypto.decrypt(o["ai_key_enc"])))


# ------------------------------------------------------------------ Email
def _redirect_uri():
    return current_app.config["BASE_URL"].rstrip("/") + "/oauth/google/callback"


@bp.route("/settings/email", methods=["GET", "POST"])
@login_required("admin")
def settings_email():
    d, o = db(), g.org
    if request.method == "POST":
        action = request.form.get("action")
        if action == "demo":
            d.x("UPDATE orgs SET email_mode='demo', email_address=CASE WHEN email_address='' THEN ? ELSE email_address END, "
                "email_connected_at=COALESCE(email_connected_at, ?), last_error='', paused_until=NULL WHERE id=?",
                ("demo@demo.local", utils.now(), o["id"]))
            flash("Demo mode on. No real emails are sent; you can simulate replies on a round page.", "ok")
        elif action == "disconnect":
            d.x("UPDATE orgs SET email_mode='none', gmail_refresh_enc='', gmail_access_enc='', gmail_expiry=NULL, "
                "smtp_pass_enc='', email_connected_at=NULL, last_error='' WHERE id=?", (o["id"],))
            flash("Email disconnected. Sending stops immediately.", "ok")
        elif action in ("smtp_save", "smtp_test"):
            f = request.form
            addr = normalize_email(f.get("email_address", ""))
            host, user = f.get("smtp_host", "").strip(), f.get("smtp_user", "").strip()
            errs = []
            if not addr:
                errs.append("Enter the sender email address.")
            if not host:
                errs.append("SMTP host is required.")
            try:
                port = int(f.get("smtp_port", "587"))
                iport = int(f.get("imap_port", "993") or 993)
            except ValueError:
                port, iport = 0, 0
                errs.append("Ports must be numbers.")
            pw = f.get("smtp_pass", "")
            if not pw and not o["smtp_pass_enc"]:
                errs.append("Password (or Gmail App Password) is required.")
            if errs:
                flash_all(errs)
                return redirect(url_for("main.settings_email"))
            pw_enc = crypto.encrypt(pw) if pw else o["smtp_pass_enc"]
            t = SmtpImapTransport(host, port, user or addr, crypto.decrypt(pw_enc), f.get("imap_host", "").strip(), iport, addr)
            try:
                msg = t.test()
            except Exception as e:
                flash(f"Could not log in: {str(e)[:200]}", "error")
                return redirect(url_for("main.settings_email"))
            d.x("UPDATE orgs SET email_mode='smtp', email_address=?, smtp_host=?, smtp_port=?, smtp_user=?, smtp_pass_enc=?, "
                "imap_host=?, imap_port=?, email_connected_at=COALESCE(email_connected_at, ?), last_error='', paused_until=NULL "
                "WHERE id=?", (addr, host, port, user or addr, pw_enc, f.get("imap_host", "").strip(), iport, utils.now(), o["id"]))
            flash(msg, "ok")
        return redirect(url_for("main.settings_email"))
    gmail_ready = bool(current_app.config["GOOGLE_CLIENT_ID"] and current_app.config["GOOGLE_CLIENT_SECRET"])
    return render_template("settings_email.html", gmail_ready=gmail_ready, redirect_uri=_redirect_uri(),
                           has_pw=bool(o["smtp_pass_enc"]))


@bp.get("/oauth/google/start")
@login_required("admin")
def google_start():
    c = current_app.config
    if not (c["GOOGLE_CLIENT_ID"] and c["GOOGLE_CLIENT_SECRET"]):
        flash("Google login is not set up on this server yet. Use SMTP/App Password below.", "error")
        return redirect(url_for("main.settings_email"))
    state = secrets.token_urlsafe(24)
    session["oauth_state"] = state
    q = urlencode({"client_id": c["GOOGLE_CLIENT_ID"], "redirect_uri": _redirect_uri(), "response_type": "code",
                   "scope": " ".join(GMAIL_SCOPES), "access_type": "offline", "prompt": "consent", "state": state})
    return redirect(f"{GOOGLE_AUTH}?{q}")


@bp.get("/oauth/google/callback")
@login_required("admin")
def google_callback():
    c = current_app.config
    if request.args.get("error"):
        flash("Google sign-in was cancelled.", "error")
        return redirect(url_for("main.settings_email"))
    state = session.pop("oauth_state", None)
    if not state or not secrets.compare_digest(state, request.args.get("state", "")):
        abort(400, "Invalid OAuth state")
    try:
        r = requests.post(GOOGLE_TOKEN, timeout=30, data={
            "code": request.args.get("code", ""), "client_id": c["GOOGLE_CLIENT_ID"], "client_secret": c["GOOGLE_CLIENT_SECRET"],
            "redirect_uri": _redirect_uri(), "grant_type": "authorization_code"})
        j = r.json()
    except (requests.RequestException, ValueError):
        flash("Could not reach Google. Try again.", "error")
        return redirect(url_for("main.settings_email"))
    if r.status_code != 200 or not j.get("refresh_token"):
        flash("Google did not return a refresh token. Remove the app from your Google account permissions and retry.", "error")
        return redirect(url_for("main.settings_email"))
    d, o = db(), g.org
    d.x("UPDATE orgs SET email_mode='gmail', gmail_refresh_enc=?, gmail_access_enc=?, gmail_expiry=? WHERE id=?",
        (crypto.encrypt(j["refresh_token"]), crypto.encrypt(j["access_token"]),
         utils.to_s(utils.now_dt() + timedelta(seconds=int(j.get("expires_in", 3600)))), o["id"]))
    o = d.one("SELECT * FROM orgs WHERE id=?", (o["id"],))
    try:
        who = GmailApiTransport(d, o, c["GOOGLE_CLIENT_ID"], c["GOOGLE_CLIENT_SECRET"])._call("GET", "/profile")["emailAddress"]
    except Exception:
        who = ""
    d.x("UPDATE orgs SET email_address=?, email_connected_at=COALESCE(email_connected_at, ?), last_error='', paused_until=NULL "
        "WHERE id=?", (who, utils.now(), o["id"]))
    flash(f"Gmail connected{(' as ' + who) if who else ''}.", "ok")
    return redirect(url_for("main.settings_email"))


# ------------------------------------------------------------------ Sending
@bp.route("/settings/sending", methods=["GET", "POST"])
@login_required("admin")
def settings_sending():
    if request.method == "POST":
        f = request.form
        errs = []
        try:
            lim = int(f.get("daily_limit", "50"))
            ws, we = int(f.get("window_start", "9")), int(f.get("window_end", "17"))
        except ValueError:
            lim = ws = we = -1
        if not 1 <= lim <= 100:
            errs.append("Daily limit must be between 1 and 100.")
        if not (0 <= ws < we <= 24):
            errs.append("Working hours: start must be before end (0 to 24).")
        tz = f.get("timezone", "UTC").strip()
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(tz)
        except Exception:
            errs.append("Unknown timezone (example: Asia/Karachi).")
        addr = f.get("postal_address", "").strip()
        if not addr or len(addr) < 8:
            errs.append("A full postal address is required.")
        sender = f.get("sender_name", "").strip()
        if not sender:
            errs.append("Sender name is required.")
        if errs:
            flash_all(errs)
            return redirect(url_for("main.settings_sending"))
        db().x("UPDATE orgs SET daily_limit=?, window_start=?, window_end=?, weekdays_only=?, timezone=?, postal_address=?, "
               "sender_name=?, name=? WHERE id=?",
               (lim, ws, we, 1 if f.get("weekdays_only") else 0, tz, addr[:300], sender[:120], f.get("org_name", g.org["name"]).strip()[:120] or g.org["name"], g.org["id"]))
        flash("Sending settings saved.", "ok")
        return redirect(url_for("main.settings_sending"))
    from ..services import engine
    return render_template("settings_sending.html", eff=engine.effective_limit(g.org))


# ------------------------------------------------------------------ Users
@bp.route("/settings/users", methods=["GET", "POST"])
@login_required("admin")
def settings_users():
    d = db()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "add":
            email = normalize_email(request.form.get("email", ""))
            role = request.form.get("role", "researcher")
            pw = request.form.get("password", "")
            errs = []
            if not email:
                errs.append("Valid email required.")
            if role not in ("admin", "manager", "researcher"):
                errs.append("Unknown role.")
            if len(pw) < 8:
                errs.append("Temporary password must be at least 8 characters.")
            if email and d.one("SELECT 1 FROM users WHERE email=?", (email,)):
                errs.append("That email is already registered.")
            if errs:
                flash_all(errs)
            else:
                d.x("INSERT INTO users (org_id,email,name,password_hash,role,created_at) VALUES (?,?,?,?,?,?)",
                    (g.org["id"], email, request.form.get("name", "").strip()[:120], generate_password_hash(pw), role, utils.now()))
                flash("User added. Share the temporary password with them.", "ok")
        else:
            uid = request.form.get("user_id", type=int)
            u = d.one("SELECT * FROM users WHERE id=? AND org_id=?", (uid, g.org["id"]))
            if not u:
                abort(404)
            if u["id"] == g.user["id"] and action in ("deactivate", "role"):
                flash("You cannot change your own role or deactivate yourself.", "error")
            elif action == "deactivate":
                d.x("UPDATE users SET active=1-active WHERE id=?", (uid,))
                flash("User updated.", "ok")
            elif action == "role" and request.form.get("role") in ("admin", "manager", "researcher"):
                d.x("UPDATE users SET role=? WHERE id=?", (request.form["role"], uid))
                flash("Role changed.", "ok")
        return redirect(url_for("main.settings_users"))
    users = d.q("SELECT * FROM users WHERE org_id=? ORDER BY id", (g.org["id"],))
    return render_template("settings_users.html", users=users)
