"""Public pages: one-click unsubscribe and the embeddable subscribe form."""
from __future__ import annotations

from flask import current_app, jsonify, render_template, request

from ..services import customers as cust
from ..services import engine
from ..services.domains import normalize_email
from .common import bp, client_ip, db, rate_limited


@bp.route("/u/<token>", methods=["GET", "POST"])
def unsubscribe(token):
    """GET renders a page that confirms automatically (scanners that prefetch links cannot run JS);
    POST (also used by mail apps' one-click List-Unsubscribe-Post) records the opt-out immediately."""
    if token == "test":
        return render_template("unsub.html", state="test")
    ids = engine.read_unsub_token(current_app.config, token)
    if not ids:
        return render_template("unsub.html", state="invalid"), 400
    if request.method == "POST":
        engine.unsubscribe_customer(db(), ids[0], ids[1], "unsubscribed")
        return render_template("unsub.html", state="done")
    return render_template("unsub.html", state="confirm", token=token)


def _form_org(key, cid):
    d = db()
    org = d.one("SELECT * FROM orgs WHERE public_key=?", (key,))
    camp = d.one("SELECT * FROM campaigns WHERE id=? AND org_id=?", (cid, org["id"])) if org else None
    return org, camp


@bp.get("/subscribe/<key>/<int:cid>")
def subscribe_form(key, cid):
    org, camp = _form_org(key, cid)
    if not camp:
        return render_template("error.html", code=404, msg="Form not found."), 404
    resp = current_app.make_response(render_template("subscribe.html", org=org, camp=camp, done=False, key=key, cid=cid))
    resp.headers.pop("X-Frame-Options", None)  # this form is meant to be embedded in the client's website
    return resp


@bp.post("/subscribe/<key>/<int:cid>")
def subscribe_post(key, cid):
    org, camp = _form_org(key, cid)
    if not camp:
        return jsonify({"ok": False, "error": "Form not found"}), 404
    wants_json = request.is_json or request.args.get("format") == "json"
    f = request.get_json(silent=True) or request.form
    if f.get("website_hp"):  # honeypot
        return (jsonify({"ok": True}) if wants_json else render_template("subscribe.html", org=org, camp=camp, done=True, key=key, cid=cid))
    if rate_limited(f"sub:{client_ip()}", 20, 3600):
        return jsonify({"ok": False, "error": "Too many requests"}), 429
    em = normalize_email(f.get("email", ""))
    err = None
    if not em:
        err = "Please enter a valid email address."
    elif not f.get("consent"):
        err = "Please tick the consent box."
    if err:
        if wants_json:
            return jsonify({"ok": False, "error": err}), 400
        resp = current_app.make_response((render_template("subscribe.html", org=org, camp=camp, done=False, error=err, key=key, cid=cid), 400))
        resp.headers.pop("X-Frame-Options", None)
        return resp
    cust.add_customer(db(), org["id"], cid, company_name=str(f.get("company_name", ""))[:200], email=em,
                      contact_name=str(f.get("name", ""))[:120], website=str(f.get("website", ""))[:200], source="Web Form")
    if wants_json:
        return jsonify({"ok": True})
    resp = current_app.make_response(render_template("subscribe.html", org=org, camp=camp, done=True, key=key, cid=cid))
    resp.headers.pop("X-Frame-Options", None)
    return resp
