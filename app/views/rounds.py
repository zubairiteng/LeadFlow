"""Rounds: create, edit draft, audience, test email, approval gate, pause/resume, recipients, demo simulation."""
from __future__ import annotations

import json
from email.message import EmailMessage
from email.utils import make_msgid

from flask import abort, current_app, flash, g, jsonify, redirect, render_template, request, url_for

from .. import utils
from ..services import ai, engine
from ..services.transports import AuthError, SendError, SendLimitError, TransientError
from .common import (bp, csv_response, db, flash_all, get_campaign, get_round, login_required)

GRADES = ["Hot", "Warm", "Cold"]
EMAIL_STATUSES = ["Unverified", "Verified", "Risky"]
REC_PER_PAGE = 50


def _form_round(f, prev=None) -> tuple[dict, list[str]]:
    days = ",".join(p.strip() for p in f.get("followup_days", "").split(",") if p.strip())
    errs = []
    if days and not all(p.strip().isdigit() and 1 <= int(p.strip()) <= 60 for p in days.split(",")):
        errs.append("Follow-up days must be numbers between 1 and 60, e.g. 3,7.")
    fu_bodies = f.get("followup_bodies", "")
    errs += engine.validate_content(f.get("subject", ""), f.get("body", ""), fu_bodies)
    try:
        ign = int(f.get("ignore_after_days", "3") or 3)
        if not 1 <= ign <= 60:
            raise ValueError
    except ValueError:
        ign = 3
        errs.append("'Mark Ignored after' must be 1 to 60 days.")
    cap = None
    if f.get("daily_limit", "").strip():
        try:
            cap = int(f["daily_limit"])
            if not 1 <= cap <= engine.HARD_CAP:
                raise ValueError
        except ValueError:
            errs.append(f"Round daily limit must be 1 to {engine.HARD_CAP}.")
    start = f.get("start_date", "").strip() or None
    if start:
        try:
            utils.datetime.strptime(start, "%Y-%m-%d")
        except ValueError:
            errs.append("Start date must look like 2026-12-31.")
    grades = [x for x in f.getlist("grades") if x in GRADES]
    statuses = [x for x in f.getlist("statuses") if x in EMAIL_STATUSES]
    prev = f.get("prev_round_id", type=int)
    mode = f.get("mode", "all")
    if mode not in ("all", "new", "ignored", "manual"):
        mode = "all"
    if mode == "ignored" and not prev:
        errs.append("Choose which earlier round's Ignored customers to include.")
    return {"name": f.get("name", "").strip(), "subject": f.get("subject", "").strip(), "body": f.get("body", "").strip(),
            "followup_days": days, "followup_bodies": fu_bodies, "ignore_after_days": ign, "daily_limit": cap,
            "start_date": start, "mode": mode, "grades": grades or ["Hot", "Warm"],
            "statuses": statuses or ["Unverified", "Verified"], "prev_round_id": prev}, errs


@bp.route("/campaigns/<int:cid>/rounds/new", methods=["GET", "POST"])
@login_required("manager")
def round_new(cid):
    d = db()
    c = get_campaign(cid)
    prev_rounds = d.q("SELECT * FROM rounds WHERE campaign_id=? ORDER BY number", (cid,))
    if request.method == "POST":
        data, errs = _form_round(request.form)
        if data["prev_round_id"] and not any(r["id"] == data["prev_round_id"] for r in prev_rounds):
            errs.append("Unknown earlier round.")
        if errs:
            flash_all(errs)
            return render_template("round_new.html", c=c, prev_rounds=prev_rounds, f=request.form, grades=GRADES,
                                   statuses=EMAIL_STATUSES, sel_grades=data["grades"], sel_statuses=data["statuses"]), 400
        rid = engine.create_round(d, g.org["id"], cid, g.user["id"], data)
        flash("Round created as Draft. Review the audience, send a test email, then approve.", "ok")
        return redirect(url_for("main.round_view", rid=rid))
    form = {"followup_days": "3,7", "followup_bodies": "\n---\n".join(engine.DEFAULT_FOLLOWUPS),
            "ignore_after_days": "3", "mode": "new" if prev_rounds else "all",
            "subject": "", "body": "Hi {{first_name}},\n\n", "grades": ["Hot", "Warm"], "statuses": ["Unverified", "Verified"]}
    return render_template("round_new.html", c=c, prev_rounds=prev_rounds, f=form, grades=GRADES,
                           statuses=EMAIL_STATUSES, sel_grades=form["grades"], sel_statuses=form["statuses"])


@bp.post("/campaigns/<int:cid>/ai-draft")
@login_required("manager")
def ai_draft(cid):
    c = get_campaign(cid)
    try:
        subject, body = ai.draft_email(g.org, c["product"], c["target_market"], (request.json or {}).get("instructions", ""))
    except ai.AIError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"subject": subject, "body": body})


def _recipient_page(rid):
    status = request.args.get("status", "")
    where, args = ["r.round_id=?"], [rid]
    if status:
        where.append("r.status=?")
        args.append(status)
    page = max(1, request.args.get("page", 1, type=int))
    d = db()
    total = d.one(f"SELECT COUNT(*) c FROM recipients r WHERE {' AND '.join(where)}", args)["c"]
    rows = d.q(f"SELECT r.*, c.company_name, c.email, c.grade, c.score, c.domain FROM recipients r "
               f"JOIN customers c ON c.id=r.customer_id WHERE {' AND '.join(where)} "
               f"ORDER BY r.id LIMIT ? OFFSET ?", args + [REC_PER_PAGE, (page - 1) * REC_PER_PAGE])
    return rows, total, page, status


@bp.get("/rounds/<int:rid>")
@login_required()
def round_view(rid):
    d = db()
    rd = get_round(rid)
    c = get_campaign(rd["campaign_id"])
    stats = engine.round_stats(d, rid)
    rows, total, page, status = _recipient_page(rid)
    aud = json.loads(rd["audience"] or "{}")
    cands, removal = engine.audience_candidates(d, g.org["id"], rd["campaign_id"], "manual")
    inround = {r["customer_id"] for r in d.q("SELECT customer_id FROM recipients WHERE round_id=?", (rid,))}
    picker = [x for x in cands if x["id"] not in inround][:200]
    pending_ids = [r["customer_id"] for r in d.q("SELECT customer_id FROM recipients WHERE round_id=? AND status='Pending'", (rid,))]
    recent = engine.recent_contact_ids(d, g.org["id"], pending_ids)
    editable = rd["status"] == "Draft"
    tested = rd["test_hash"] == engine.content_hash(rd)
    today_sent = engine.sent_today(d, g.org, rid)
    return render_template("round.html", rd=rd, rdd=dict(rd), c=c, stats=stats, rows=rows, total=total, page=page,
                           pages=max(1, -(-total // REC_PER_PAGE)), status=status, aud=aud, picker=picker,
                           removal=removal, recent_count=len(recent), editable=editable, tested=tested,
                           fu_days=engine.followup_days(rd), fu_bodies=engine.followup_bodies(rd),
                           today_sent=today_sent, limit=engine.effective_limit(g.org),
                           demo=g.org["email_mode"] == "demo", statuses=engine.STATUSES, defaults=engine.DEFAULT_FOLLOWUPS)


@bp.post("/rounds/<int:rid>/edit")
@login_required("manager")
def round_edit(rid):
    rd = get_round(rid)
    if rd["status"] != "Draft":
        flash("An approved round's email text is frozen and cannot be edited.", "error")
        return redirect(url_for("main.round_view", rid=rid))
    data, errs = _form_round(request.form)
    if errs:
        flash_all(errs)
        return redirect(url_for("main.round_view", rid=rid))
    db().x("UPDATE rounds SET name=?, subject=?, body=?, followup_days=?, followup_bodies=?, ignore_after_days=?, "
           "daily_limit=?, start_date=? WHERE id=?",
           (data["name"] or f"Round {rd['number']}", data["subject"], data["body"], data["followup_days"],
            data["followup_bodies"], data["ignore_after_days"], data["daily_limit"], data["start_date"], rid))
    flash("Saved. Send a test email again before approving (the text changed).", "ok")
    return redirect(url_for("main.round_view", rid=rid))


@bp.post("/rounds/<int:rid>/settings")
@login_required("manager")
def round_settings(rid):
    """Daily cap / start date can change after approval; email text cannot."""
    rd = get_round(rid)
    cap = request.form.get("daily_limit", "").strip()
    try:
        capv = int(cap) if cap else None
        if capv is not None and not 1 <= capv <= engine.HARD_CAP:
            raise ValueError
    except ValueError:
        flash("Daily limit must be 1 to 100.", "error")
        return redirect(url_for("main.round_view", rid=rid))
    db().x("UPDATE rounds SET daily_limit=? WHERE id=?", (capv, rid))
    flash("Round daily limit updated.", "ok")
    return redirect(url_for("main.round_view", rid=rid))


@bp.post("/rounds/<int:rid>/delete")
@login_required("manager")
def round_delete(rid):
    rd = get_round(rid)
    if rd["status"] != "Draft":
        flash("Only Draft rounds can be deleted.", "error")
        return redirect(url_for("main.round_view", rid=rid))
    db().x("DELETE FROM rounds WHERE id=?", (rid,))
    flash("Draft round deleted.", "ok")
    return redirect(url_for("main.campaign", cid=rd["campaign_id"]))


@bp.post("/rounds/<int:rid>/test")
@login_required("manager")
def round_test(rid):
    rd = get_round(rid)
    if rd["status"] != "Draft":
        flash("Test emails are only for Draft rounds.", "error")
        return redirect(url_for("main.round_view", rid=rid))
    to = request.form.get("to", "").strip() or g.user["email"]
    from ..services.domains import normalize_email
    if not normalize_email(to):
        flash("Enter a valid test address.", "error")
        return redirect(url_for("main.round_view", rid=rid))
    errs = engine.validate_content(rd["subject"], rd["body"], rd["followup_bodies"])
    if errs or not (g.org["postal_address"] or "").strip():
        flash_all(errs or ["Add your postal address in Settings > Sending first."])
        return redirect(url_for("main.round_view", rid=rid))
    try:
        flash(engine.send_test(db(), g.org["id"], dict(current_app.config), rd, normalize_email(to)), "ok")
    except (SendError, SendLimitError, AuthError, TransientError) as e:
        flash(f"Test email failed: {e}", "error")
    return redirect(url_for("main.round_view", rid=rid))


@bp.post("/rounds/<int:rid>/approve")
@login_required("manager")
def round_approve(rid):
    ok, errs = engine.approve_round(db(), g.org["id"], dict(current_app.config), rid, g.user["id"])
    if ok:
        flash("Round approved. Sending starts automatically inside your working-hour window.", "ok")
    else:
        flash_all(errs)
    return redirect(url_for("main.round_view", rid=rid))


@bp.post("/rounds/<int:rid>/pause")
@login_required("manager")
def round_pause(rid):
    rd = get_round(rid)
    if rd["status"] in ("Approved", "Sending"):
        db().x("UPDATE rounds SET status='Paused' WHERE id=?", (rid,))
        flash("Round paused. Nothing more will be sent until you resume.", "ok")
    return redirect(url_for("main.round_view", rid=rid))


@bp.post("/rounds/<int:rid>/resume")
@login_required("manager")
def round_resume(rid):
    rd = get_round(rid)
    if rd["status"] == "Paused":
        sent = db().one("SELECT COUNT(*) c FROM recipients WHERE round_id=? AND steps_sent>=1", (rid,))["c"]
        db().x("UPDATE rounds SET status=? WHERE id=?", ("Sending" if sent else "Approved", rid))
        flash("Round resumed.", "ok")
    return redirect(url_for("main.round_view", rid=rid))


@bp.post("/rounds/<int:rid>/recipients/add")
@login_required("manager")
def recipients_add(rid):
    rd = get_round(rid)
    if rd["status"] == "Finished":
        flash("This round is finished.", "error")
        return redirect(url_for("main.round_view", rid=rid))
    ids = [int(x) for x in request.form.getlist("customer_ids") if x.isdigit()]
    n = engine.add_recipients(db(), g.org["id"], rid, ids)
    flash(f"{n} customers added to this round.", "ok")
    return redirect(url_for("main.round_view", rid=rid))


@bp.post("/rounds/<int:rid>/recipients/add-matching")
@login_required("manager")
def recipients_add_matching(rid):
    rd = get_round(rid)
    if rd["status"] == "Finished":
        return redirect(url_for("main.round_view", rid=rid))
    a = json.loads(rd["audience"] or "{}")
    cands, _ = engine.audience_candidates(db(), g.org["id"], rd["campaign_id"], a.get("mode", "all"),
                                          a.get("grades", ["Hot", "Warm"]), a.get("statuses", ["Unverified", "Verified"]),
                                          a.get("prev_round_id"))
    n = engine.add_recipients(db(), g.org["id"], rid, [c["id"] for c in cands])
    flash(f"{n} new matching customers added.", "ok")
    return redirect(url_for("main.round_view", rid=rid))


def _recipient(rid, recid):
    r = db().one("SELECT * FROM recipients WHERE id=? AND round_id=? AND org_id=?", (recid, rid, g.org["id"]))
    if not r:
        abort(404)
    return r


@bp.post("/rounds/<int:rid>/recipients/<int:recid>/skip")
@login_required("researcher")
def recipient_skip(rid, recid):
    get_round(rid)
    r = _recipient(rid, recid)
    if r["status"] in ("Pending", "Sent"):
        engine.set_recipient_status(db(), recid, "Skipped", "Removed from this round")
        flash("Customer removed from this round only (still saved in your list).", "ok")
    return redirect(request.form.get("back") or url_for("main.round_view", rid=rid))


@bp.post("/rounds/<int:rid>/recipients/<int:recid>/unskip")
@login_required("researcher")
def recipient_unskip(rid, recid):
    get_round(rid)
    r = _recipient(rid, recid)
    if r["status"] == "Skipped" and r["note"] == "Removed from this round":
        c = db().one("SELECT * FROM customers WHERE id=?", (r["customer_id"],))
        if engine._blocked(db(), g.org["id"], r, c):
            flash("This customer can no longer be emailed.", "error")
        else:
            engine.set_recipient_status(db(), recid, "Sent" if r["steps_sent"] else "Pending", "")
            flash("Customer put back in this round.", "ok")
    return redirect(url_for("main.round_view", rid=rid))


@bp.get("/rounds/<int:rid>/export.csv")
@login_required()
def round_export(rid):
    rd = get_round(rid)
    rows = db().q("SELECT c.company_name, c.email, c.grade, r.status, r.steps_sent, r.first_sent_at, r.last_sent_at, r.note "
                  "FROM recipients r JOIN customers c ON c.id=r.customer_id WHERE r.round_id=? ORDER BY r.id", (rid,))
    return csv_response(f"round-{rd['number']}.csv", ["company", "email", "grade", "status", "emails_sent",
                                                      "first_sent_at", "last_sent_at", "note"],
                        ([r[k] for k in r.keys()] for r in rows))


@bp.post("/rounds/<int:rid>/recipients/<int:recid>/simulate")
@login_required("manager")
def recipient_simulate(rid, recid):
    """Demo mode only: inject a reply / bounce / out-of-office / unsubscribe so clients can see the full flow."""
    get_round(rid)
    if g.org["email_mode"] != "demo":
        abort(404)
    r = _recipient(rid, recid)
    kind = request.form.get("kind")
    c = db().one("SELECT * FROM customers WHERE id=?", (r["customer_id"],))
    if not r["first_message_id"] or kind not in ("reply", "bounce", "auto", "unsub"):
        flash("Send the first email before simulating a response.", "error")
        return redirect(url_for("main.round_view", rid=rid))
    m = EmailMessage()
    mid = make_msgid(domain="demo.local")
    m["Message-ID"] = mid
    if kind == "bounce":
        m["From"] = "Mail Delivery Subsystem <mailer-daemon@demo.local>"
        m["To"] = g.org["email_address"] or "me@demo.local"
        m["Subject"] = "Delivery Status Notification (Failure)"
        m.set_content(f"Address not found. Your message to {c['email']} was not delivered.\nOriginal: {r['first_message_id']}")
    else:
        m["From"] = c["email"]
        m["To"] = g.org["email_address"] or "me@demo.local"
        m["In-Reply-To"] = r["first_message_id"]
        m["References"] = r["first_message_id"]
        if kind == "auto":
            m["Subject"] = "Automatic reply: Out of office"
            m["Auto-Submitted"] = "auto-replied"
            m.set_content("I am out of the office until next week.")
        elif kind == "unsub":
            m["Subject"] = "Re: hello"
            m.set_content("Please unsubscribe me.")
        else:
            m["Subject"] = "Re: hello"
            m.set_content("Thanks for reaching out, this looks interesting. Can you send prices?")
    db().x("INSERT INTO demo_inbox (org_id, raw, created_at) VALUES (?,?,?)", (g.org["id"], m.as_bytes(), utils.now()))
    engine.process_replies(db(), g.org["id"], dict(current_app.config))
    flash(f"Simulated {kind}. See the status below and the Needs Reply list.", "ok")
    return redirect(url_for("main.round_view", rid=rid))
