"""Auth, dashboard, campaigns, searches, customers, excluded/DNC lists, tasks."""
from __future__ import annotations

import json
import secrets

from flask import abort, current_app, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from .. import utils
from ..scheduler import run_discovery_now
from ..services import customers as cust
from ..services import engine, scoring
from ..services.domains import clean_domain, normalize_email, website_url
from .common import (bp, client_ip, csv_response, db, flash_all, get_campaign, get_customer, login_required,
                     rate_limited, safe_next)

PER_PAGE = 50


# ------------------------------------------------------------------ auth
@bp.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("main.dashboard"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        if rate_limited(f"login:{client_ip()}", 15, 600) or rate_limited(f"login:{email}", 8, 600):
            flash("Too many attempts. Wait a few minutes.", "error")
            return render_template("login.html"), 429
        u = db().one("SELECT * FROM users WHERE email=? AND active=1", (email,))
        if u and check_password_hash(u["password_hash"], request.form.get("password", "")):
            session.clear()
            session["uid"] = u["id"]
            session["csrf"] = secrets.token_urlsafe(24)
            return redirect(safe_next(request.args.get("next")))
        flash("Wrong email or password.", "error")
    return render_template("login.html")


@bp.route("/signup", methods=["GET", "POST"])
def signup():
    if g.user:
        return redirect(url_for("main.dashboard"))
    if not current_app.config["ALLOW_SIGNUP"]:
        abort(404)
    if request.method == "POST":
        f = request.form
        company, name = f.get("company", "").strip(), f.get("name", "").strip()
        email, pw = f.get("email", "").strip().lower(), f.get("password", "")
        errs = []
        if not company:
            errs.append("Company name is required.")
        if not name:
            errs.append("Your name is required.")
        if not normalize_email(email):
            errs.append("Enter a valid email address.")
        if len(pw) < 8:
            errs.append("Password must be at least 8 characters.")
        if not errs and db().one("SELECT 1 FROM users WHERE email=?", (email,)):
            errs.append("That email is already registered.")
        if not errs and rate_limited(f"signup:{client_ip()}", 10, 3600):
            errs.append("Too many sign-ups from this address. Try later.")
        if errs:
            flash_all(errs)
            return render_template("signup.html", form=f), 400
        d = db()
        with d.tx():
            oid = d.x("INSERT INTO orgs (name,public_key,created_at,sender_name,postal_address) VALUES (?,?,?,?,?)",
                      (company[:120], secrets.token_urlsafe(10), utils.now(), company[:120],
                       current_app.config["DEFAULT_POSTAL_ADDRESS"]))
            uid = d.x("INSERT INTO users (org_id,email,name,password_hash,role,created_at) VALUES (?,?,?,?,?,?)",
                      (oid, email, name[:120], generate_password_hash(pw), "admin", utils.now()))
        session.clear()
        session["uid"] = uid
        session["csrf"] = secrets.token_urlsafe(24)
        flash("Welcome! Start by creating your first campaign.", "ok")
        return redirect(url_for("main.dashboard"))
    return render_template("signup.html", form={})


@bp.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("main.login"))


@bp.route("/account", methods=["GET", "POST"])
@login_required()
def account():
    if request.method == "POST":
        cur, new = request.form.get("current", ""), request.form.get("new", "")
        if not check_password_hash(g.user["password_hash"], cur):
            flash("Current password is wrong.", "error")
        elif len(new) < 8:
            flash("New password must be at least 8 characters.", "error")
        else:
            db().x("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(new), g.user["id"]))
            flash("Password changed.", "ok")
            return redirect(url_for("main.account"))
    return render_template("account.html")


@bp.get("/health")
def health():
    db().one("SELECT 1")
    return {"status": "ok"}


# ------------------------------------------------------------------ dashboard
@bp.get("/")
@login_required()
def dashboard():
    d, oid = db(), g.org["id"]
    stats = {
        "customers": d.one("SELECT COUNT(*) c FROM customers WHERE org_id=? AND excluded=0", (oid,))["c"],
        "hot": d.one("SELECT COUNT(*) c FROM customers WHERE org_id=? AND excluded=0 AND grade='Hot'", (oid,))["c"],
        "with_email": d.one("SELECT COUNT(*) c FROM customers WHERE org_id=? AND excluded=0 AND email<>'' "
                            "AND email_status<>'Invalid'", (oid,))["c"],
        "campaigns": d.one("SELECT COUNT(*) c FROM campaigns WHERE org_id=?", (oid,))["c"],
        "sent_today": engine.sent_today(d, g.org),
        "limit": engine.effective_limit(g.org),
        "open_replies": d.one("SELECT COUNT(*) c FROM tasks WHERE org_id=? AND status='Open'", (oid,))["c"],
        "dnc": d.one("SELECT COUNT(*) c FROM dnc WHERE org_id=?", (oid,))["c"],
    }
    camps = d.q("SELECT * FROM campaigns WHERE org_id=? ORDER BY id DESC LIMIT 8", (oid,))
    summary = []
    for c in camps:
        n = d.one("SELECT COUNT(*) c FROM customers WHERE campaign_id=? AND excluded=0", (c["id"],))["c"]
        rounds = d.q("SELECT id FROM rounds WHERE campaign_id=?", (c["id"],))
        tot = {"sent": 0, "replied": 0}
        for r in rounds:
            s = engine.round_stats(d, r["id"])
            tot["sent"] += s["sent"]
            tot["replied"] += s["replied"]
        summary.append({"c": c, "customers": n, "rounds": len(rounds), **tot})
    tasks = d.q("SELECT t.*, c.company_name, c.email FROM tasks t LEFT JOIN customers c ON c.id=t.customer_id "
                "WHERE t.org_id=? AND t.status='Open' ORDER BY t.id DESC LIMIT 6", (oid,))
    events = d.q("SELECT e.*, c.company_name FROM events e LEFT JOIN customers c ON c.id=e.customer_id "
                 "WHERE e.org_id=? ORDER BY e.id DESC LIMIT 10", (oid,))
    warnings = []
    o = g.org
    if o["email_mode"] == "none":
        warnings.append(("Connect an email account to start sending.", url_for("main.settings_email")))
    if not (o["postal_address"] or "").strip():
        warnings.append(("Add your postal address (required in every email).", url_for("main.settings_sending")))
    if o["last_error"]:
        warnings.append(("Email problem: " + o["last_error"], url_for("main.settings_email")))
    if o["paused_until"] and utils.parse(o["paused_until"]) > utils.now_dt():
        warnings.append(("Sending is paused until " + utils.fmt_local(o["paused_until"], o["timezone"]) + ".", None))
    return render_template("dashboard.html", stats=stats, summary=summary, tasks=tasks, events=events,
                           warnings=warnings)


# ------------------------------------------------------------------ campaigns
@bp.get("/campaigns")
@login_required()
def campaigns():
    d = db()
    rows = d.q("SELECT c.*, (SELECT COUNT(*) FROM customers x WHERE x.campaign_id=c.id AND x.excluded=0) AS n, "
               "(SELECT COUNT(*) FROM rounds r WHERE r.campaign_id=c.id) AS rounds FROM campaigns c "
               "WHERE c.org_id=? ORDER BY c.id DESC", (g.org["id"],))
    return render_template("campaigns.html", rows=rows)


@bp.post("/campaigns/new")
@login_required()
def campaign_new():
    f = request.form
    name = f.get("name", "").strip()
    if not name:
        flash("Campaign name is required.", "error")
        return redirect(url_for("main.campaigns"))
    cid = db().x("INSERT INTO campaigns (org_id,name,product,target_market,created_by,created_at) VALUES (?,?,?,?,?,?)",
                 (g.org["id"], name[:120], f.get("product", "").strip()[:300], f.get("target_market", "").strip()[:300],
                  g.user["id"], utils.now()))
    flash("Campaign created. Now start a customer search below.", "ok")
    return redirect(url_for("main.campaign", cid=cid))


@bp.get("/campaigns/<int:cid>")
@login_required()
def campaign(cid):
    d = db()
    c = get_campaign(cid)
    jobs = d.q("SELECT * FROM search_jobs WHERE campaign_id=? ORDER BY id DESC LIMIT 20", (cid,))
    rounds = []
    for r in d.q("SELECT * FROM rounds WHERE campaign_id=? ORDER BY number", (cid,)):
        rounds.append({"r": r, "s": engine.round_stats(d, r["id"])})
    grades = {g_["grade"]: g_["n"] for g_ in d.q(
        "SELECT grade, COUNT(*) n FROM customers WHERE campaign_id=? AND excluded=0 GROUP BY grade", (cid,))}
    total = sum(grades.values())
    with_email = d.one("SELECT COUNT(*) c FROM customers WHERE campaign_id=? AND excluded=0 AND email<>'' "
                       "AND email_status<>'Invalid'", (cid,))["c"]
    pending_enrich = d.one("SELECT COUNT(*) c FROM customers WHERE campaign_id=? AND enrich_state='pending'", (cid,))["c"]
    embed = f"{current_app.config['BASE_URL'].rstrip('/')}/subscribe/{g.org['public_key']}/{cid}"
    has_ai_key = bool(g.org["ai_key_enc"]) if g.org else False
    return render_template("campaign.html", c=c, jobs=jobs, rounds=rounds, grades=grades, total=total,
                           with_email=with_email, pending_enrich=pending_enrich, embed=embed,
                           has_ai_key=has_ai_key)


@bp.post("/campaigns/<int:cid>/delete")
@login_required("manager")
def campaign_delete(cid):
    c = get_campaign(cid)
    if db().one("SELECT 1 FROM rounds WHERE campaign_id=? AND status<>'Draft'", (cid,)):
        flash("A campaign with approved rounds cannot be deleted.", "error")
        return redirect(url_for("main.campaign", cid=cid))
    db().x("DELETE FROM campaigns WHERE id=?", (cid,))
    flash("Campaign deleted.", "ok")
    return redirect(url_for("main.campaigns"))


@bp.post("/campaigns/<int:cid>/search")
@login_required()
def search_create(cid):
    get_campaign(cid)
    f = request.form
    kw = f.get("keyword", "").strip()
    try:
        target = int(f.get("target", "100"))
    except ValueError:
        target = 0
    errs = []
    if not kw:
        errs.append("Keyword is required.")
    if not f.get("country", "").strip():
        errs.append("Country is required.")
    if not 1 <= target <= 5000:
        errs.append("Lead target must be between 1 and 5000.")
    has_ai = bool(g.org["ai_key_enc"]) if g.org else False
    if not has_ai and not current_app.config.get("TESTING"):
        errs.append("Gemini API key is required to discover leads with live Google Search. Please add your free key in Settings > AI key first.")
    if errs:
        flash_all(errs)
        return redirect(url_for("main.campaign", cid=cid))
    db().x("INSERT INTO search_jobs (org_id,campaign_id,keyword,country,city,company_type,target,created_at) "
           "VALUES (?,?,?,?,?,?,?,?)", (g.org["id"], cid, kw[:120], f["country"].strip()[:80], f.get("city", "").strip()[:80],
                                        f.get("company_type", "").strip()[:120], target, utils.now()))
    flash("Search queued. The Discovery Bot starts within 10 minutes, or press Run now.", "ok")
    return redirect(url_for("main.campaign", cid=cid))


def _job(jid):
    j = db().one("SELECT * FROM search_jobs WHERE id=? AND org_id=?", (jid, g.org["id"]))
    if not j:
        abort(404)
    return j


@bp.post("/jobs/<int:jid>/cancel")
@login_required()
def job_cancel(jid):
    j = _job(jid)
    if j["status"] in ("Queued", "Running"):
        db().x("UPDATE search_jobs SET status='Cancelled', finished_at=? WHERE id=?", (utils.now(), jid))
        flash("Search cancelled.", "ok")
    return redirect(url_for("main.campaign", cid=j["campaign_id"]))


@bp.post("/jobs/<int:jid>/run")
@login_required()
def job_run(jid):
    j = _job(jid)
    if j["status"] == "Queued":
        run_discovery_now(dict(current_app.config), jid)
        flash("Discovery Bot started. Refresh in a minute to see progress.", "ok")
    return redirect(url_for("main.campaign", cid=j["campaign_id"]))


@bp.get("/jobs/<int:jid>")
@login_required()
def job_view(jid):
    j = _job(jid)
    return render_template("job.html", j=j, c=get_campaign(j["campaign_id"]))


# ------------------------------------------------------------------ customers
def _customer_filters(cid):
    f = request.args
    where, args = ["campaign_id=?", "excluded=0"], [cid]
    if f.get("q"):
        where.append("(company_name LIKE ? OR domain LIKE ? OR email LIKE ?)")
        like = f"%{f['q'].strip()}%"
        args += [like, like, like]
    for col, key in (("grade", "grade"), ("email_status", "email_status"), ("country", "country"), ("source", "source")):
        if f.get(key):
            where.append(f"{col}=?")
            args.append(f[key])
    return " AND ".join(where), args


@bp.get("/campaigns/<int:cid>/customers")
@login_required()
def customers(cid):
    d = db()
    c = get_campaign(cid)
    where, args = _customer_filters(cid)
    total = d.one(f"SELECT COUNT(*) c FROM customers WHERE {where}", args)["c"]
    page = max(1, request.args.get("page", 1, type=int))
    rows = d.q(f"SELECT * FROM customers WHERE {where} ORDER BY score DESC, id DESC LIMIT ? OFFSET ?",
               args + [PER_PAGE, (page - 1) * PER_PAGE])
    countries = [r["country"] for r in d.q("SELECT DISTINCT country FROM customers WHERE campaign_id=? AND country<>'' "
                                           "ORDER BY country", (cid,))]
    sources = [r["source"] for r in d.q("SELECT DISTINCT source FROM customers WHERE campaign_id=? ORDER BY source", (cid,))]
    dnc = {r["email"] for r in d.q("SELECT email FROM dnc WHERE org_id=?", (g.org["id"],))}
    return render_template("customers.html", c=c, rows=rows, total=total, page=page, pages=max(1, -(-total // PER_PAGE)),
                           countries=countries, sources=sources, dnc=dnc, args=request.args)


@bp.get("/campaigns/<int:cid>/customers.csv")
@login_required()
def customers_export(cid):
    c = get_campaign(cid)
    where, args = _customer_filters(cid)
    rows = db().q(f"SELECT * FROM customers WHERE {where} ORDER BY score DESC", args)
    return csv_response(f"customers-{cid}.csv",
                        ["company_name", "website", "email", "email_status", "phone", "country", "city", "contact_name",
                         "grade", "score", "source"],
                        ([r[k] for k in ("company_name", "website", "email", "email_status", "phone", "country", "city",
                                         "contact_name", "grade", "score", "source")] for r in rows))


@bp.post("/campaigns/<int:cid>/customers/add")
@login_required()
def customer_add(cid):
    get_campaign(cid)
    f = request.form
    st, _, msg = cust.add_customer(db(), g.org["id"], cid, company_name=f.get("company_name", ""),
                                   website=f.get("website", ""), email=f.get("email", ""), phone=f.get("phone", ""),
                                   country=f.get("country", ""), city=f.get("city", ""),
                                   contact_name=f.get("contact_name", ""), source="Manual")
    flash({"added": "Customer added.", "duplicate": "That customer is already saved (duplicate).",
           "excluded": "That customer is on the Excluded list. Restore it there first.",
           "invalid": msg}[st], "ok" if st == "added" else "error")
    return redirect(url_for("main.customers", cid=cid))


@bp.post("/campaigns/<int:cid>/customers/upload")
@login_required()
def customer_upload(cid):
    get_campaign(cid)
    up = request.files.get("file")
    if not up or not up.filename:
        flash("Choose a CSV or Excel file.", "error")
        return redirect(url_for("main.customers", cid=cid))
    try:
        rows = cust.read_rows(up.filename, up.read())
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("main.customers", cid=cid))
    except Exception:
        flash("Could not read that file. Save it as .csv or .xlsx and try again.", "error")
        return redirect(url_for("main.customers", cid=cid))
    res = cust.import_rows(db(), g.org["id"], cid, rows, source="Import")
    flash(f"Import done: {res['added']} added, {res['duplicate']} duplicates, {res['excluded']} excluded, "
          f"{res['invalid']} invalid.", "ok")
    flash_all(res["errors"])
    return redirect(url_for("main.customers", cid=cid))


@bp.post("/campaigns/<int:cid>/enrich")
@login_required()
def enrich_queue(cid):
    get_campaign(cid)
    n = db().n("UPDATE customers SET enrich_state='pending' WHERE campaign_id=? AND excluded=0 AND domain<>'' AND email='' "
               "AND enrich_state<>'pending'", (cid,))
    flash(f"{n} customers queued for email finding (runs in the background).", "ok")
    return redirect(url_for("main.campaign", cid=cid))


@bp.post("/customers/<int:cid>/exclude")
@login_required()
def customer_exclude(cid):
    c = get_customer(cid)
    cust.exclude_customer(db(), g.org["id"], cid, g.user["id"])
    flash(f"{c['company_name']} excluded permanently. Undo it from the Excluded list.", "ok")
    if request.form.get("next"):
        return redirect(safe_next(request.form.get("next")))
    if c["campaign_id"]:
        return redirect(url_for("main.customers", cid=c["campaign_id"]))
    return redirect(url_for("main.dashboard"))


@bp.route("/customers/<int:cid>", methods=["GET", "POST"])
@login_required()
def customer_view(cid):
    d = db()
    c = get_customer(cid)
    if request.method == "POST":
        f = request.form
        em = normalize_email(f.get("email", ""))
        domain = clean_domain(f.get("website", ""))
        if f.get("email", "").strip() and not em:
            flash("That email address is not valid.", "error")
        elif not domain and not em:
            flash("A website or an email is required.", "error")
        else:
            key = domain or ("mail:" + em)
            clash = d.one("SELECT 1 FROM customers WHERE org_id=? AND dedup_key=? AND id<>?", (g.org["id"], key, cid))
            if clash:
                flash("Another customer already uses that website/email.", "error")
            else:
                from ..services import emailcheck
                st = emailcheck.check_email(em)[0] if em else "None"
                if em == c["email"]:
                    st = c["email_status"]
                d.x("UPDATE customers SET company_name=?, dedup_key=?, domain=?, website=?, email=?, email_status=?, phone=?, "
                    "country=?, city=?, contact_name=?, updated_at=? WHERE id=?",
                    ((f.get("company_name", "").strip() or domain or em)[:200], key, domain, website_url(domain) if domain else "",
                     em, st, f.get("phone", "").strip()[:60], f.get("country", "").strip()[:80], f.get("city", "").strip()[:80],
                     f.get("contact_name", "").strip()[:120], utils.now(), cid))
                cust.rescore(d, g.org["id"], cid)
                flash("Saved.", "ok")
                return redirect(url_for("main.customer_view", cid=cid))
        c = get_customer(cid)
    ctype, country = cust._campaign_ctx(d, g.org["id"], c["campaign_id"])
    _, _, parts = scoring.score_customer(c, ctype, country)
    events = d.q("SELECT * FROM events WHERE customer_id=? ORDER BY id DESC LIMIT 30", (cid,))
    recs = d.q("SELECT r.*, d.number, d.name FROM recipients r JOIN rounds d ON d.id=r.round_id WHERE r.customer_id=?", (cid,))
    return render_template("customer.html", c=c, parts=parts, events=events, recs=recs,
                           dnc=cust.is_dnc(d, g.org["id"], c["email"]))


# ------------------------------------------------------------------ excluded / dnc / tasks
@bp.get("/excluded")
@login_required()
def excluded():
    rows = db().q("SELECT * FROM excluded_domains WHERE org_id=? ORDER BY id DESC", (g.org["id"],))
    return render_template("excluded.html", rows=rows)


@bp.post("/excluded/<int:eid>/restore")
@login_required()
def excluded_restore(eid):
    if cust.restore_excluded(db(), g.org["id"], eid):
        flash("Restored. The customer is back in the list.", "ok")
    return redirect(url_for("main.excluded"))


@bp.route("/dnc", methods=["GET", "POST"])
@login_required()
def dnc_list():
    d = db()
    if request.method == "POST":
        em = normalize_email(request.form.get("email", ""))
        if not em:
            flash("Enter a valid email.", "error")
        else:
            cust.add_dnc(d, g.org["id"], em, "manual")
            flash("Added to the do-not-contact list.", "ok")
        return redirect(url_for("main.dnc_list"))
    rows = d.q("SELECT * FROM dnc WHERE org_id=? ORDER BY id DESC LIMIT 500", (g.org["id"],))
    return render_template("dnc.html", rows=rows)


@bp.post("/dnc/<int:did>/remove")
@login_required("manager")
def dnc_remove(did):
    row = db().one("SELECT * FROM dnc WHERE id=? AND org_id=?", (did, g.org["id"]))
    if row and row["reason"] == "bounced":
        flash("Bounced addresses cannot be removed: they do not exist.", "error")
    elif row:
        db().x("DELETE FROM dnc WHERE id=?", (did,))
        flash("Removed from the do-not-contact list.", "ok")
    return redirect(url_for("main.dnc_list"))


@bp.get("/tasks")
@login_required()
def tasks():
    d = db()
    rows = d.q("SELECT t.*, c.company_name, c.email, rd.number AS rnum, ca.name AS cname, ca.id AS caid FROM tasks t "
               "LEFT JOIN customers c ON c.id=t.customer_id LEFT JOIN rounds rd ON rd.id=t.round_id "
               "LEFT JOIN campaigns ca ON ca.id=rd.campaign_id WHERE t.org_id=? ORDER BY t.status DESC, t.id DESC LIMIT 200",
               (g.org["id"],))
    return render_template("tasks.html", rows=rows)


@bp.post("/tasks/<int:tid>/done")
@login_required()
def task_done(tid):
    db().x("UPDATE tasks SET status='Done', done_at=? WHERE id=? AND org_id=?", (utils.now(), tid, g.org["id"]))
    nxt = request.form.get("next")
    return redirect(safe_next(nxt) if nxt else url_for("main.tasks"))
