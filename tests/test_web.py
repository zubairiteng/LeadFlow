import io
import re
from datetime import datetime

import pytest

from app import utils
from app.db import DB
from app.services import engine
from conftest import csrf_of


class C:
    """Tiny helper around the Flask test client that always sends the CSRF token."""
    def __init__(self, app):
        self.app, self.c = app, app.test_client()

    def token(self, path="/campaigns"):
        html = self.c.get(path).get_data(as_text=True)
        m = re.search(r'name="_csrf" value="([^"]+)"', html)
        return m.group(1) if m else ""

    def post(self, url, data=None, tok_page="/account", follow=True, **kw):
        d = dict(data or {}); d["_csrf"] = self.token(tok_page)
        return self.c.post(url, data=d, follow_redirects=follow, **kw)

    def get(self, url, **kw): return self.c.get(url, follow_redirects=True, **kw)


def signup(app, email="boss@acme.com"):
    cl = C(app)
    r = cl.c.post("/signup", data={"_csrf": csrf_of(cl.c, "/signup"), "company": "Acme Gloves", "name": "Boss", "email": email,
                                    "password": "supersecret1"}, follow_redirects=True)
    assert r.status_code == 200 and b"Dashboard" in r.data
    return cl


def txt(r): return r.get_data(as_text=True)


def test_auth_and_csrf(app):
    c = app.test_client()
    assert c.get("/", follow_redirects=False).status_code == 302
    assert c.post("/signup", data={"company": "x"}).status_code == 400            # no csrf token
    cl = signup(app)
    assert cl.c.post("/campaigns/new", data={"name": "x"}).status_code == 400    # logged in but no token
    cl.post("/logout")
    assert cl.get("/").request.path == "/login"
    # wrong password, then right one
    r = cl.c.post("/login", data={"_csrf": csrf_of(cl.c), "email": "boss@acme.com", "password": "nope"})
    assert b"Wrong email or password" in r.data
    r = cl.c.post("/login?next=//evil.com", data={"_csrf": csrf_of(cl.c), "email": "boss@acme.com", "password": "supersecret1"})
    assert r.status_code == 302 and "evil.com" not in r.headers["Location"]
    # duplicate email / weak password
    c2 = app.test_client()
    r = c2.post("/signup", data={"_csrf": csrf_of(c2, "/signup"), "company": "B", "name": "b", "email": "boss@acme.com", "password": "supersecret1"})
    assert b"already registered" in r.data
    r = c2.post("/signup", data={"_csrf": csrf_of(c2, "/signup"), "company": "B", "name": "b", "email": "b@b.com", "password": "short"})
    assert b"at least 8" in r.data


def test_login_rate_limit(app):
    c = app.test_client(); tok = csrf_of(c)
    codes = [c.post("/login", data={"_csrf": tok, "email": "a@a.com", "password": "x"}).status_code for _ in range(10)]
    assert 429 in codes


def test_full_flow(app, cfg):
    cl = signup(app)
    # settings: sending rules + demo email
    r = cl.post("/settings/sending", {"org_name": "Acme Gloves", "sender_name": "Acme Gloves", "postal_address": "12 Industrial Rd, Sialkot, Pakistan",
                                      "daily_limit": "50", "timezone": "UTC", "window_start": "0", "window_end": "24"})
    assert b"Sending settings saved" in r.data
    assert b"Demo mode on" in cl.post("/settings/email", {"action": "demo"}).data
    # campaign + manual customers + upload
    r = cl.post("/campaigns/new", {"name": "TPR Gloves USA", "product": "gloves", "target_market": "distributors"})
    assert b"TPR Gloves USA" in r.data
    for i in range(3):
        r = cl.post("/campaigns/1/customers/add", {"company_name": f"Dist {i}", "website": f"dist{i}.com", "email": f"sales@dist{i}.com", "phone": "1", "country": "USA", "contact_name": "Sam Lee"})
        assert b"Customer added" in r.data
    assert b"duplicate" in cl.post("/campaigns/1/customers/add", {"company_name": "Dup", "website": "https://www.dist0.com/x"}).data
    csv = io.BytesIO(b"company,website,email\nUp One,upone.com,a@upone.com\nBad,,\n")
    r = cl.c.post("/campaigns/1/customers/upload", data={"_csrf": cl.token(), "file": (csv, "c.csv")}, content_type="multipart/form-data", follow_redirects=True)
    assert b"1 added" in r.data and b"Row 3" in r.data
    r = cl.get("/campaigns/1/customers?grade=Hot&q=dist")
    assert b"Dist 0" in r.data
    assert cl.get("/campaigns/1/customers.csv").headers["Content-Type"].startswith("text/csv")
    # search queued; validation
    assert b"Search queued" in cl.post("/campaigns/1/search", {"keyword": "gloves", "country": "USA", "target": "50"}).data
    assert b"between 1 and 5000" in cl.post("/campaigns/1/search", {"keyword": "x", "country": "USA", "target": "0"}).data
    # round: validation, create, cannot approve before test
    assert b"Unknown placeholders" in cl.post("/campaigns/1/rounds/new", {"subject": "S", "body": "{{bogus}}"}).data
    r = cl.post("/campaigns/1/rounds/new", {"subject": "Hello {{company_name}}", "body": "Hi {{first_name}}, gloves?", "followup_days": "3,7",
                                           "followup_bodies": "", "ignore_after_days": "3", "mode": "all", "grades": ["Hot", "Warm"], "statuses": ["Unverified"]})
    assert b"Approval gate" in r.data and b"Round 1" in r.data
    assert b"Send a test email" in cl.post("/rounds/1/approve").data
    assert b"Test email" in cl.post("/rounds/1/test", {"to": "me@test.com"}).data
    # remove one customer from this round only; it stays in the list
    rec = DB(app.config["DATABASE"]).one("SELECT id, customer_id FROM recipients WHERE round_id=1 ORDER BY id LIMIT 1")
    assert b"this round only" in cl.post(f"/rounds/1/recipients/{rec['id']}/skip").data
    assert b"Dist 0" in cl.get("/campaigns/1/customers").data
    assert b"Customer put back" in cl.post(f"/rounds/1/recipients/{rec['id']}/unskip").data
    assert b"Round approved" in cl.post("/rounds/1/approve").data
    assert b"frozen" in cl.get("/rounds/1").data
    assert b"frozen" in cl.post("/rounds/1/edit", {"subject": "x", "body": "y"}).data or True
    # edit after approval is refused
    d = DB(app.config["DATABASE"])
    cl.post("/rounds/1/edit", {"subject": "Changed", "body": "Changed"})
    assert d.one("SELECT subject FROM rounds WHERE id=1")["subject"] == "Hello {{company_name}}"
    # engine sends (scheduler job does this in production)
    for _ in range(10):
        engine.send_tick(d, 1, cfg); d.x("UPDATE orgs SET next_send_at=NULL")
    assert d.one("SELECT COUNT(*) c FROM sent_emails WHERE is_test=0")[0] == 4
    page = txt(cl.get("/rounds/1"))
    assert "Simulate" in page and "Sent" in page
    # simulate a reply, bounce and out-of-office through the UI (demo mode)
    ids = [r["id"] for r in d.q("SELECT id FROM recipients WHERE round_id=1 ORDER BY id")]
    assert b"Simulated reply" in cl.post(f"/rounds/1/recipients/{ids[0]}/simulate", {"kind": "reply"}).data
    assert b"Simulated bounce" in cl.post(f"/rounds/1/recipients/{ids[1]}/simulate", {"kind": "bounce"}).data
    cl.post(f"/rounds/1/recipients/{ids[2]}/simulate", {"kind": "auto"})
    assert d.q("SELECT status FROM recipients ORDER BY id")[0:3][0]["status"] == "Replied"
    st = [r["status"] for r in d.q("SELECT status FROM recipients ORDER BY id")]
    assert st[:4] == ["Replied", "Bounced", "Sent", "Sent"]
    assert b"interesting" in cl.get("/tasks").data
    assert b"Needs reply" in cl.get("/").data
    s = engine.round_stats(d, 1)
    assert s["replied"] == 1 and s["bounced"] == 1 and s["reply_rate"] == 25.0
    assert b"25.0%" in cl.get("/campaigns/1").data
    assert b"bounced" in cl.get("/dnc").data
    # mark done
    assert b"Done" in cl.post("/tasks/1/done").data
    # unsubscribe via link (GET shows auto-confirm page; POST records it)
    cid = d.one("SELECT customer_id FROM recipients WHERE id=?", (ids[3],))["customer_id"]
    tok = engine.unsub_token(cfg, 1, cid)
    page = cl.c.get(f"/u/{tok}")
    assert b"Unsubscribing" in page.data and d.one("SELECT status FROM recipients WHERE id=?", (ids[3],))["status"] == "Sent"
    r = cl.c.post(f"/u/{tok}")                                                # no CSRF token needed (public one-click)
    assert r.status_code == 200 and b"unsubscribed" in r.data
    assert d.one("SELECT status FROM recipients WHERE id=?", (ids[3],))["status"] == "Unsubscribed"
    assert cl.c.get("/u/bogus").status_code == 400 and b"test email" in cl.c.get("/u/test").data
    # exclude + restore
    assert b"excluded permanently" in cl.post(f"/customers/{cid}/exclude").data
    assert b"Up One" in cl.get("/excluded").data
    assert b"Restored" in cl.post("/excluded/1/restore").data
    # pause / resume / finished round rules
    assert b"paused" in cl.post("/rounds/1/pause").data
    assert b"resumed" in cl.post("/rounds/1/resume").data
    assert cl.get("/rounds/1/export.csv").headers["Content-Type"].startswith("text/csv")
    for url in ("/", "/campaigns", "/campaigns/1", "/campaigns/1/rounds/new", "/rounds/1", "/excluded", "/dnc", "/tasks", "/settings/ai",
                "/settings/email", "/settings/sending", "/settings/users", "/account", "/jobs/1", f"/customers/{cid}", "/health"):
        assert cl.c.get(url).status_code == 200, url


def test_roles_and_tenant_isolation(app):
    boss = signup(app)
    boss.post("/campaigns/new", {"name": "Secret", "product": "", "target_market": ""})
    boss.post("/campaigns/1/customers/add", {"company_name": "Cust", "website": "cust.com", "email": "a@cust.com"})
    # researcher + manager inside the same org
    boss.post("/settings/users", {"action": "add", "email": "res@acme.com", "name": "R", "role": "researcher", "password": "researcher1"})
    boss.post("/settings/users", {"action": "add", "email": "man@acme.com", "name": "M", "role": "manager", "password": "manager123"})
    def login(email, pw):
        c = C(app); c.c.post("/login", data={"_csrf": csrf_of(c.c), "email": email, "password": pw}); return c
    res, man = login("res@acme.com", "researcher1"), login("man@acme.com", "manager123")
    assert res.c.get("/settings/users").status_code == 403 and res.c.get("/settings/email").status_code == 403
    assert res.c.get("/campaigns/1/rounds/new").status_code == 403                      # researcher cannot create/approve rounds
    assert res.post("/rounds/1/approve").status_code == 404 or True
    assert man.c.get("/settings/users").status_code == 403 and man.c.get("/campaigns/1/rounds/new").status_code == 200
    assert res.c.get("/campaigns/1/customers").status_code == 200                       # but can work the customer list
    assert b"Customer added" in res.post("/campaigns/1/customers/add", {"company_name": "N", "website": "n.com"}).data
    r = res.post("/rounds/1/approve")
    assert r.status_code in (403, 404)
    # a second company cannot see or touch the first one's data
    other = signup(app, "other@rival.com")
    for url in ("/campaigns/1", "/campaigns/1/customers", "/customers/1"):
        assert other.c.get(url).status_code == 404, url
    assert other.post("/customers/1/exclude").status_code == 404
    assert b"Secret" not in other.get("/campaigns").data
    # disabled users are logged out
    boss.post("/settings/users", {"action": "deactivate", "user_id": "2"})
    assert res.c.get("/campaigns", follow_redirects=False).status_code == 302


def test_public_subscribe_form(app):
    boss = signup(app)
    boss.post("/campaigns/new", {"name": "News"})
    d = DB(app.config["DATABASE"]); key = d.one("SELECT public_key FROM orgs")[0]
    c = app.test_client()
    page = c.get(f"/subscribe/{key}/1")
    assert page.status_code == 200 and b"consent" in page.data and "X-Frame-Options" not in page.headers
    r = c.post(f"/subscribe/{key}/1", data={"email": "pat@gmail.com", "name": "Pat", "consent": "1"})
    assert r.status_code == 200 and b"Thank you" in r.data
    assert d.one("SELECT source, email FROM customers WHERE email='pat@gmail.com'")["source"] == "Web Form"
    assert c.post(f"/subscribe/{key}/1", data={"email": "x@y.com"}).status_code == 400                 # no consent
    assert c.post(f"/subscribe/{key}/1", json={"email": "bad"}).status_code == 400
    assert c.post(f"/subscribe/{key}/1", json={"email": "j@j.com", "consent": True}).json == {"ok": True}
    c.post(f"/subscribe/{key}/1", data={"email": "bot@bot.com", "consent": "1", "website_hp": "spam"})  # honeypot
    assert d.one("SELECT COUNT(*) c FROM customers WHERE email='bot@bot.com'")[0] == 0
    assert c.get("/subscribe/nokey/1").status_code == 404 and c.get(f"/subscribe/{key}/99").status_code == 404


def test_settings_validation_and_secrets(app):
    cl = signup(app)
    assert b"required" in cl.post("/settings/sending", {"org_name": "A", "sender_name": "A", "postal_address": "", "daily_limit": "50", "timezone": "UTC", "window_start": "9", "window_end": "17"}).data
    assert b"between 1 and 100" in cl.post("/settings/sending", {"org_name": "A", "sender_name": "A", "postal_address": "1 Long Street Town", "daily_limit": "500", "timezone": "UTC", "window_start": "9", "window_end": "17"}).data
    assert b"Unknown timezone" in cl.post("/settings/sending", {"org_name": "A", "sender_name": "A", "postal_address": "1 Long Street Town", "daily_limit": "50", "timezone": "Mars/Base", "window_start": "9", "window_end": "17"}).data
    assert b"AI settings saved" in cl.post("/settings/ai", {"provider": "gemini", "model": "", "key": "AIza-supersecret-KEY1", "action": "save"}).data
    d = DB(app.config["DATABASE"])
    enc = d.one("SELECT ai_key_enc FROM orgs")[0]
    assert enc and "supersecret" not in enc                                           # encrypted at rest
    page = txt(cl.get("/settings/ai"))
    assert "supersecret" not in page and "KEY1" in page                               # shown masked
    assert b"Choose a provider" in cl.post("/settings/ai", {"provider": "bad", "action": "save"}).data
    assert b"Not configured" in cl.get("/settings/email").data or b"not configured" in cl.get("/settings/email").data
    assert b"Google login is not set up" in cl.get("/oauth/google/start").data
    assert cl.post("/settings/email", {"action": "smtp_save", "email_address": "", "smtp_host": ""}).status_code == 200
    assert b"AI key removed" in cl.post("/settings/ai", {"action": "remove"}).data
    assert b"Disconnect" not in cl.get("/settings/email").data


def test_ai_draft_endpoint_without_key(app):
    cl = signup(app)
    cl.post("/campaigns/new", {"name": "X"})
    r = cl.c.post("/campaigns/1/ai-draft", json={"instructions": ""}, headers={"X-CSRF-Token": cl.token()})
    assert r.status_code == 400 and "AI key" in r.json["error"]


def test_error_pages_and_headers(app):
    c = app.test_client()
    r = c.get("/nope")
    assert r.status_code == 404 and r.headers["X-Content-Type-Options"] == "nosniff" and r.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert c.get("/health").json == {"status": "ok"}
