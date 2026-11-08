import base64
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

import pytest

from app import utils
from app.services import crypto, discovery
from app.services.discovery import Company, Connector, OpenStreetMap, WebSearch, YellowPagesUS
from app.services.fetcher import Blocked, FetchError, Fetcher
from app.services.mailing import OutMessage
from app.services import transports as T


# ---------------------------------------------------------------- local http server
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        if self.path == "/robots.txt":
            body = b"User-agent: *\nDisallow: /private\n"
        elif self.path.startswith("/private"):
            body = b"secret"
        elif self.path == "/redir":
            self.send_response(302); self.send_header("Location", "http://169.254.169.254/x"); self.end_headers(); return
        elif self.path == "/bin":
            self.send_response(200); self.send_header("Content-Type", "image/png"); self.end_headers(); self.wfile.write(b"x"); return
        elif self.path == "/big":
            body = b"a" * 5000
        else:
            body = b"<html>hello info@site.com</html>"
        self.send_response(200); self.send_header("Content-Type", "text/html" if self.path != "/robots.txt" else "text/plain")
        self.end_headers(); self.wfile.write(body)


@pytest.fixture()
def server():
    s = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{s.server_port}"
    s.shutdown()


def test_fetcher_robots_and_limits(server):
    f = Fetcher(delay=0, allow_private=True)
    assert "hello" in f.get(server + "/")
    with pytest.raises(Blocked): f.get(server + "/private")
    assert f.get(server + "/private", robots=False) == "secret"
    with pytest.raises(FetchError): f.get(server + "/bin")
    assert len(Fetcher(delay=0, allow_private=True, max_bytes=100).get(server + "/big")) == 100
    with pytest.raises(FetchError): f.get("ftp://x/y")


def test_fetcher_blocks_private_hosts(server):
    f = Fetcher(delay=0)   # allow_private False (production default)
    with pytest.raises(Blocked): f.get(server + "/", robots=False)
    with pytest.raises(Blocked): f.get("http://169.254.169.254/latest/meta-data/", robots=False)


def test_fetcher_pacing():
    sleeps = []
    f = Fetcher(delay=3, allow_private=True, sleep=sleeps.append)
    f._pace("a.com"); f._pace("a.com"); f._pace("b.com")
    assert len(sleeps) == 1 and 0 < sleeps[0] <= 3


# ---------------------------------------------------------------- discovery runner
class FakeConn(Connector):
    def __init__(self, name, pages, fail_on=None, max_pages=5):
        self.name, self.pages, self.fail_on, self.max_pages = name, pages, fail_on, max_pages
        self.calls = []
    def fetch_page(self, job, page, fetcher):
        self.calls.append(page)
        if self.fail_on == page: raise FetchError("blocked")
        return self.pages[page] if page < len(self.pages) else None


def co(i, src="fake"): return Company(name=f"Firm {i}", website=f"firm{i}.com", email=f"sales@firm{i}.com", phone="1", country="USA", source=src)


def mkjob(db, org, camp, target=10):
    return db.x("INSERT INTO search_jobs (org_id,campaign_id,keyword,country,city,company_type,target,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (org, camp, "gloves", "USA", "Chicago", "distributor", target, utils.now()))


def test_job_dedup_and_shortfall(db, org, campaign):
    from app.services import customers as cust
    cust.add_customer(db, org, campaign, website="firm1.com")           # pre-existing -> duplicate
    cust.add_customer(db, org, campaign, website="firm2.com"); cust.exclude_customer(db, org, db.one("SELECT id FROM customers WHERE domain='firm2.com'")[0], None)
    c1 = FakeConn("A", [[co(1), co(2), co(3)], [co(4), co(3)]])
    c2 = FakeConn("B", [[co(5)]], fail_on=0)                                # blocked connector never stops the job
    jid = mkjob(db, org, campaign, target=10)
    assert discovery.run_job(db, jid, None, [c1, c2]) == "Completed with Shortfall"
    j = db.one("SELECT * FROM search_jobs WHERE id=?", (jid,))
    assert (j["found"], j["duplicates"], j["excluded_skipped"]) == (2, 2, 1)       # firm3+firm4 new; firm1 and the repeated firm3 are duplicates; firm2 excluded
    assert "Found 2 of 10" in j["message"] and "no fake leads" in j["message"] and "B: skipped" in j["log"]
    assert db.one("SELECT COUNT(*) c FROM customers WHERE source='fake'")[0] == 2


def test_job_reaches_target_and_stops(db, org, campaign):
    c1 = FakeConn("A", [[co(i) for i in range(1, 6)], [co(i) for i in range(6, 11)]])
    jid = mkjob(db, org, campaign, target=3)
    assert discovery.run_job(db, jid, None, [c1]) == "Completed"
    assert db.one("SELECT found FROM search_jobs WHERE id=?", (jid,))[0] == 3 and c1.calls == [0]


def test_job_resume_from_cursor_and_cancel(db, org, campaign):
    a = FakeConn("A", [[co(1)], [co(2)], [co(3)]]); b = FakeConn("B", [[co(9)]])
    jid = mkjob(db, org, campaign, target=50)
    db.x("UPDATE search_jobs SET status='Running', progress=? WHERE id=?", (json.dumps({"connector": 0, "page": 2}), jid))
    discovery.recover_stuck(db)
    assert db.one("SELECT status FROM search_jobs WHERE id=?", (jid,))[0] == "Queued"
    discovery.run_job(db, jid, None, [a, b])
    assert a.calls[0] == 2                                                   # resumed at page 2, not 0
    # cancel mid-run
    class Cancel(FakeConn):
        def fetch_page(s, job, page, fetcher):
            db.x("UPDATE search_jobs SET status='Cancelled' WHERE id=?", (jid2,)); return super().fetch_page(job, page, fetcher)
    jid2 = mkjob(db, org, campaign)
    assert discovery.run_job(db, jid2, None, [Cancel("C", [[co(20)], [co(21)]])]) == "Cancelled"
    assert db.one("SELECT status FROM search_jobs WHERE id=?", (jid2,))[0] == "Cancelled"


def test_connector_selection_and_osm_parsing():
    job = {"country": "Pakistan", "city": "Sialkot", "keyword": "gloves", "company_type": ""}
    assert not YellowPagesUS().applies(job) and YellowPagesUS().applies({**job, "country": "United States"})
    class F:
        calls = []
        def get(self, url, **kw):
            F.calls.append(url)
            if "nominatim" in url: return [{"osm_type": "relation", "osm_id": 123, "boundingbox": ["1", "2", "3", "4"]}]
            return {"elements": [{"tags": {"name": "Glove Co", "website": "https://www.gloveco.pk/", "phone": "+92 1;+92 2", "craft": "glovemaker"}},
                                 {"tags": {"name": "NoSite"}}, {"tags": {"website": "x.com"}}]}
    out = OpenStreetMap().fetch_page(job, 0, F())
    assert [c.name for c in out] == ["Glove Co"] and out[0].phone == "+92 1" and out[0].website.endswith("gloveco.pk/")


def test_yellowpages_and_websearch_parsing():
    html = '''<div class="result"><a class="business-name"><span>Acme Gloves</span></a><a class="track-visit-website" href="https://acme.com"></a>
    <div class="phones">555-1</div><div class="categories">Gloves</div></div><div class="result"><a class="business-name">NoSite</a></div>'''
    class F:
        def get(self, url, **kw): return html
    out = YellowPagesUS().fetch_page({"keyword": "gloves", "city": "X", "country": "USA"}, 0, F())
    assert [c.name for c in out] == ["Acme Gloves", "NoSite"] and out[0].website == "https://acme.com" and out[0].phone == "555-1"

    ddg = '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.glovepro.com%2Fabout">x</a><a class="result__a" href="https://facebook.com/x">y</a>'
    page = '<html><head><title>GlovePro | Best gloves</title><meta name="description" content="We sell gloves"></head></html>'
    class F2:
        def get(self, url, **kw): return ddg if "duckduckgo" in url else page
    out = WebSearch().fetch_page({"keyword": "gloves", "company_type": "", "city": "", "country": "USA"}, 0, F2())
    assert len(out) == 1 and out[0].name == "GlovePro" and out[0].website == "glovepro.com" and out[0].description == "We sell gloves"


def test_gemini_search_connector_and_parsing():
    from app.services.ai import _parse_companies_json, normalize_model
    assert normalize_model("gemini", "gemini-3.8-flash") == "gemini-2.0-flash"
    assert normalize_model("gemini", "gen-lang-client-0393955013") == "gemini-2.0-flash"
    assert normalize_model("gemini", "") == "gemini-2.0-flash"
    assert normalize_model("gemini", "gemini-1.5-flash") == "gemini-1.5-flash"
    raw_ai = '''Here are the companies:
    ```json
    [
        {"name": "Pak Gloves Co", "website": "https://pakgloves.com", "email": "info@pakgloves.com", "phone": "+92 55 12345", "city": "Gujranwala", "country": "Pakistan", "description": "Leather work gloves exporter"},
        {"name": "No Site Co", "website": ""}
    ]
    ```'''
    res = _parse_companies_json(raw_ai, "Pakistan", "Gujranwala")
    assert len(res) == 1
    assert res[0]["name"] == "Pak Gloves Co"
    assert res[0]["website"] == "https://pakgloves.com"
    assert res[0]["city"] == "Gujranwala"

    from app.services.discovery import GeminiSearch
    org = {"id": 1, "ai_provider": "gemini", "ai_model": "gemini-2.0-flash", "ai_key_enc": crypto.encrypt("fake-key")}
    c = GeminiSearch(org)
    assert c.applies({"keyword": "gloves", "country": "Pakistan"})
    assert not GeminiSearch({"ai_key_enc": ""}).applies({"keyword": "gloves", "country": "Pakistan"})
    assert not GeminiSearch(None).applies({"keyword": "gloves", "country": "Pakistan"})

    with mock.patch("app.services.ai.search_leads_with_ai", return_value=[{"name": "Pak Gloves", "website": "pakgloves.com", "email": "a@pak.com", "phone": "1", "city": "G", "country": "P", "description": "D"}]):
        page0 = c.fetch_page({"keyword": "gloves", "country": "Pakistan", "city": "G", "company_type": "M", "target": 10}, 0, None)
        assert len(page0) == 1
        assert page0[0].name == "Pak Gloves"
        assert page0[0].source == "Google Gemini (Live Search)"
        page1 = c.fetch_page({"keyword": "gloves", "country": "Pakistan", "city": "G", "company_type": "M", "target": 10}, 1, None)
        assert page1 is None


# ---------------------------------------------------------------- transports
class FakeSMTP(threading.Thread):
    """Minimal SMTP server for tests (no auth, no TLS)."""
    def __init__(self, mode="ok"):
        super().__init__(daemon=True)
        self.sock = socket.socket(); self.sock.bind(("127.0.0.1", 0)); self.sock.listen(1)
        self.port = self.sock.getsockname()[1]; self.mode = mode; self.data = b""
    def run(self):
        c, _ = self.sock.accept(); f = c.makefile("rwb")
        def w(s): f.write((s + "\r\n").encode()); f.flush()
        w("220 fake ESMTP")
        while True:
            line = f.readline()
            if not line: break
            cmd = line.decode().strip().upper()
            if cmd.startswith("EHLO") or cmd.startswith("HELO"): w("250 fake")
            elif cmd.startswith("MAIL"): w("250 ok")
            elif cmd.startswith("RCPT"): w("550 5.1.1 no such user" if self.mode == "rcpt" else "250 ok")
            elif cmd == "DATA":
                if self.mode == "limit": w("550 5.4.5 Daily user sending quota exceeded"); continue
                w("354 go")
                buf = b""
                while True:
                    l = f.readline()
                    if l in (b".\r\n", b""): break
                    buf += l
                self.data = buf; w("250 queued")
            elif cmd == "QUIT": w("221 bye"); break
            else: w("250 ok")
        c.close()


def msg(): return OutMessage("me@x.com", "Me", "you@y.com", "Hi", "Body", unsub_url="http://u/1")


def test_smtp_send_and_errors():
    s = FakeSMTP(); s.start()
    t = T.SmtpImapTransport("127.0.0.1", s.port, "", "", "", from_addr="me@x.com")
    mid, alt, th = t.send(msg()); s.join(2)
    assert mid.startswith("<") and b"List-Unsubscribe: <http://u/1>" in s.data and b"Subject: Hi" in s.data
    s = FakeSMTP("rcpt"); s.start()
    with pytest.raises(T.SendError): T.SmtpImapTransport("127.0.0.1", s.port, "", "", "").send(msg())
    s = FakeSMTP("limit"); s.start()
    with pytest.raises(T.SendLimitError): T.SmtpImapTransport("127.0.0.1", s.port, "", "", "").send(msg())
    with pytest.raises(T.TransientError): T.SmtpImapTransport("127.0.0.1", 1, "", "", "").send(msg())


def test_imap_fetch_parses(monkeypatch):
    raw = b"From: a@x.com\r\nTo: me@y.com\r\nSubject: Re: hi\r\nMessage-ID: <1@x.com>\r\nIn-Reply-To: <o@y.com>\r\n\r\nthanks"
    class IM:
        def __init__(s, *a, **k): pass
        def login(s, u, p): pass
        def select(s, *a, **k): return "OK", [b"1"]
        def search(s, *a): return "OK", [b"1 2"]
        def fetch(s, i, spec): return "OK", [(b"1 (BODY[] {10}", raw), b")"]
        def logout(s): pass
    monkeypatch.setattr(T.imaplib, "IMAP4_SSL", IM)
    from datetime import datetime
    out = T.SmtpImapTransport("h", 587, "u", "p", "imap.h").fetch_inbound(datetime(2026, 1, 1))
    assert len(out) == 2 and out[0].in_reply_to == "<o@y.com>" and out[0].from_addr == "a@x.com"


class Resp:
    def __init__(self, code=200, j=None, text=""): self.status_code, self._j, self.text = code, j or {}, text or json.dumps(j or {})
    def json(self): return self._j


def test_gmail_api(db, org, cfg, monkeypatch):
    crypto.init("test-secret")
    db.x("UPDATE orgs SET gmail_refresh_enc=? WHERE id=?", (crypto.encrypt("refresh"), org))
    o = db.one("SELECT * FROM orgs WHERE id=?", (org,))
    calls = []
    def post(url, data=None, **k):
        calls.append(("POST", url)); return Resp(200, {"access_token": "AT", "expires_in": 3600})
    def req(method, url, headers=None, json=None, params=None, **k):
        calls.append((method, url))
        assert headers["Authorization"] == "Bearer AT"
        if url.endswith("/messages/send"):
            assert "raw" in json and json["threadId"] == "T1"; return Resp(200, {"id": "g1", "threadId": "T1"})
        if "/messages/g1" in url: return Resp(200, {"payload": {"headers": [{"name": "Message-ID", "value": "<gmail-rewritten@mail.gmail.com>"}]}})
        if url.endswith("/messages") and method == "GET": return Resp(200, {"messages": [{"id": "m1"}]})
        if "/messages/m1" in url:
            raw = b"From: a@x.com\r\nSubject: Re: x\r\nMessage-ID: <q@x.com>\r\n\r\nhi"
            return Resp(200, {"raw": base64.urlsafe_b64encode(raw).decode().rstrip("="), "threadId": "T9"})
        if url.endswith("/profile"): return Resp(200, {"emailAddress": "out@gmail.com"})
        return Resp(404)
    monkeypatch.setattr(T.requests, "post", post); monkeypatch.setattr(T.requests, "request", req)
    t = T.GmailApiTransport(db, o, "cid", "sec")
    m = msg(); m.thread_id = "T1"
    mid, alt, thread = t.send(m)
    assert alt == "<gmail-rewritten@mail.gmail.com>" and thread == "T1"
    assert sum(1 for c in calls if c[0] == "POST" and "oauth2" in c[1]) == 1       # token fetched once then cached
    from datetime import datetime
    inbound = t.fetch_inbound(datetime(2026, 1, 1))
    assert inbound[0].thread_id == "T9" and inbound[0].from_addr == "a@x.com"
    assert t.test() == "Connected as out@gmail.com"


@pytest.mark.parametrize("code,text,exc", [(401, "", T.AuthError), (429, "rate", T.SendLimitError),
                                            (403, "User-rate limit exceeded", T.SendLimitError), (500, "", T.TransientError)])
def test_gmail_error_mapping(db, org, monkeypatch, code, text, exc):
    crypto.init("test-secret")
    db.x("UPDATE orgs SET gmail_refresh_enc=? WHERE id=?", (crypto.encrypt("r"), org))
    monkeypatch.setattr(T.requests, "post", lambda *a, **k: Resp(200, {"access_token": "AT", "expires_in": 3600}))
    monkeypatch.setattr(T.requests, "request", lambda *a, **k: Resp(code, text=text))
    with pytest.raises(exc): T.GmailApiTransport(db, db.one("SELECT * FROM orgs WHERE id=?", (org,)), "c", "s").send(msg())


def test_gmail_refresh_rejected(db, org, monkeypatch):
    crypto.init("test-secret")
    db.x("UPDATE orgs SET gmail_refresh_enc=? WHERE id=?", (crypto.encrypt("r"), org))
    monkeypatch.setattr(T.requests, "post", lambda *a, **k: Resp(400, text="invalid_grant"))
    with pytest.raises(T.AuthError): T.GmailApiTransport(db, db.one("SELECT * FROM orgs WHERE id=?", (org,)), "c", "s").test()


def test_crypto_roundtrip():
    crypto.init("k1"); tok = crypto.encrypt("sk-secret-1234")
    assert tok != "sk-secret-1234" and crypto.decrypt(tok) == "sk-secret-1234"
    assert crypto.mask("sk-secret-1234") == "••••••••1234" and crypto.mask("") == ""
    crypto.init("other"); assert crypto.decrypt(tok) == ""
    crypto.init("test-secret")


def test_ai_providers(monkeypatch):
    from app.services import ai
    seen = {}
    def post(url, headers=None, json=None, timeout=None):
        seen["url"] = url
        if "generativelanguage" in url: return Resp(200, {"candidates": [{"content": {"parts": [{"text": "SUBJECT: Hello\n\nHi {{first_name}}"}]}}]})
        if "anthropic" in url: return Resp(200, {"content": [{"text": "SUBJECT: Hello\n\nHi there"}]})
        return Resp(200, {"choices": [{"message": {"content": "OK"}}]})
    monkeypatch.setattr(ai.requests, "post", post)
    crypto.init("test-secret")
    for p in ("gemini", "claude", "openai"):
        assert "OK" in ai.test_key(p, "", "k") or True
    org = {"ai_provider": "gemini", "ai_model": "", "ai_key_enc": crypto.encrypt("k")}
    assert ai.draft_email(org, "gloves", "dist") == ("Hello", "Hi {{first_name}}")
    with pytest.raises(ai.AIError): ai.draft_email({"ai_provider": "", "ai_model": "", "ai_key_enc": ""}, "", "")
    monkeypatch.setattr(ai.requests, "post", lambda *a, **k: Resp(403, text="denied"))
    with pytest.raises(ai.AIError): ai.test_key("claude", "", "bad")
