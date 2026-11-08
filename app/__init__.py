"""Lead Generation SaaS (Flask). Free, self-hosted, minimal dependencies."""
from __future__ import annotations

import os
import secrets
import time
from pathlib import Path

from flask import Flask, abort, g, request, session

from . import utils
from .db import DB
from .services import crypto, emailcheck

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_secret(instance: Path) -> str:
    env = os.environ.get("SECRET_KEY")
    if env:
        return env
    f = instance / "secret.key"
    if f.exists():
        return f.read_text().strip()
    instance.mkdir(parents=True, exist_ok=True)
    key = secrets.token_hex(32)
    f.write_text(key)
    try:
        os.chmod(f, 0o600)
    except OSError:
        pass
    return key


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__)
    instance = Path(os.environ.get("LEADGEN_INSTANCE", BASE_DIR / "instance"))
    cfg = {
        "SECRET_KEY": None,
        "DATABASE": str(instance / "leadgen.db"),
        "BASE_URL": os.environ.get("BASE_URL", "http://localhost:5000"),
        "ALLOW_SIGNUP": os.environ.get("ALLOW_SIGNUP", "1") == "1",
        "GOOGLE_CLIENT_ID": os.environ.get("GOOGLE_CLIENT_ID", ""),
        "GOOGLE_CLIENT_SECRET": os.environ.get("GOOGLE_CLIENT_SECRET", ""),
        "DEFAULT_POSTAL_ADDRESS": os.environ.get("POSTAL_ADDRESS", ""),
        "FETCH_DELAY": float(os.environ.get("FETCH_DELAY", "3")),
        "RUN_SCHEDULER": os.environ.get("RUN_SCHEDULER", "1") == "1",
        "MX_CHECK": os.environ.get("MX_CHECK", "1") == "1",
        "ALLOW_PRIVATE_HOSTS": False,
        "SESSION_COOKIE_HTTPONLY": True,
        "SESSION_COOKIE_SAMESITE": "Lax",
        "MAX_CONTENT_LENGTH": 6 * 1024 * 1024,
        "TESTING": False,
    }
    if test_config:
        cfg.update(test_config)
    if not cfg["SECRET_KEY"]:
        cfg["SECRET_KEY"] = _load_secret(instance)
    if cfg["BASE_URL"].startswith("https://"):
        cfg["SESSION_COOKIE_SECURE"] = True
    app.config.update(cfg)
    Path(cfg["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)

    crypto.init(app.config["SECRET_KEY"])
    emailcheck.MX_ENABLED = bool(app.config["MX_CHECK"])
    init_db = DB(cfg["DATABASE"])
    init_db.init_schema()
    init_db.close()

    # ---- per-request db + user
    def get_db() -> DB:
        if "db" not in g:
            g.db = DB(app.config["DATABASE"])
        return g.db

    app.get_db = get_db  # type: ignore[attr-defined]

    @app.teardown_appcontext
    def _close(_e):
        d = g.pop("db", None)
        if d:
            d.close()

    @app.before_request
    def _load_user():
        g.user = g.org = None
        uid = session.get("uid")
        if uid:
            db = get_db()
            u = db.one("SELECT * FROM users WHERE id=? AND active=1", (uid,))
            if u:
                g.user = u
                g.org = db.one("SELECT * FROM orgs WHERE id=?", (u["org_id"],))
            else:
                session.clear()

    @app.before_request
    def _csrf():
        if request.method in ("POST", "PUT", "DELETE", "PATCH"):
            if request.endpoint in app.csrf_exempt:  # type: ignore[attr-defined]
                return
            sent = request.form.get("_csrf") or request.headers.get("X-CSRF-Token")
            if not sent or not secrets.compare_digest(sent, session.get("csrf", "")):
                abort(400, "Invalid or missing CSRF token. Reload the page and try again.")

    app.csrf_exempt = set()  # type: ignore[attr-defined]

    @app.after_request
    def _headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        if not (request.endpoint or "").startswith("main.subscribe"):
            resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        return resp

    @app.context_processor
    def _inject():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(24)
        tz = g.org["timezone"] if g.get("org") else "UTC"
        return {"csrf_token": session["csrf"], "me": g.get("user"), "org": g.get("org"),
                "fmt": lambda s, f="%d %b %Y, %H:%M": utils.fmt_local(s, tz, f)}

    app.jinja_env.filters["dictupdate"] = lambda a, b: {**{k: v for k, v in dict(a).items() if v != ""}, **b}

    from .views import register
    register(app)

    if app.config["RUN_SCHEDULER"] and not app.config["TESTING"]:
        from .scheduler import Scheduler
        if not getattr(app, "_scheduler_started", False):
            app.scheduler = Scheduler(app.config)  # type: ignore[attr-defined]
            app.scheduler.start()
            app._scheduler_started = True  # type: ignore[attr-defined]
    return app
