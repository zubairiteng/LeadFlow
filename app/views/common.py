from __future__ import annotations

import csv
import io
import time
from functools import wraps
from urllib.parse import urlparse

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, session, url_for

bp = Blueprint("main", __name__)
RANK = {"researcher": 1, "manager": 2, "admin": 3}


def db():
    return current_app.get_db()


def login_required(role: str | None = None):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            if not g.user:
                return redirect(url_for("main.login", next=request.full_path if request.method == "GET" else None))
            if role and RANK[g.user["role"]] < RANK[role]:
                abort(403)
            return fn(*a, **kw)
        return wrapper
    return deco


def safe_next(target: str | None) -> str:
    if target and target.startswith("/") and not target.startswith("//") and not urlparse(target).netloc:
        return target.rstrip("?")
    return url_for("main.dashboard")


def rate_limited(key: str, limit: int, window: int) -> bool:
    """True if this key already hit `limit` events in `window` seconds (and records this attempt)."""
    d = db()
    now = time.time()
    n = d.one("SELECT COUNT(*) c FROM rate_limits WHERE k=? AND created_at>?", (key, now - window))["c"]
    if n >= limit:
        return True
    d.x("INSERT INTO rate_limits (k, created_at) VALUES (?,?)", (key, now))
    return False


def client_ip() -> str:
    return request.remote_addr or "?"


def get_campaign(cid: int):
    c = db().one("SELECT * FROM campaigns WHERE id=? AND org_id=?", (cid, g.org["id"]))
    if not c:
        abort(404)
    return c


def get_round(rid: int):
    r = db().one("SELECT * FROM rounds WHERE id=? AND org_id=?", (rid, g.org["id"]))
    if not r:
        abort(404)
    return r


def get_customer(cid: int):
    c = db().one("SELECT * FROM customers WHERE id=? AND org_id=?", (cid, g.org["id"]))
    if not c:
        abort(404)
    return c


def flash_all(errors, cat="error"):
    for e in errors:
        flash(e, cat)


def csv_safe(v) -> str:
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def csv_response(filename: str, header: list[str], rows):
    from flask import Response
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for r in rows:
        w.writerow([csv_safe(x) for x in r])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def register_errors(app):
    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    @app.errorhandler(413)
    @app.errorhandler(429)
    @app.errorhandler(500)
    def err(e):
        code = getattr(e, "code", 500)
        msg = {403: "You do not have permission to do that.", 404: "Page not found.",
               413: "That file is too large (max 6 MB).", 429: "Too many requests. Try again later.",
               500: "Something went wrong on our side."}.get(code, getattr(e, "description", "Bad request"))
        return render_template("error.html", code=code, msg=msg), code
