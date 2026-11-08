import json
import random
from datetime import datetime, timedelta
from email.message import EmailMessage

import pytest

from app import utils
from app.services import customers as cust, engine
from app.services.mailing import parse_inbound
from app.services.transports import DemoTransport, SendError, SendLimitError, TransientError, AuthError, Transport
from conftest import freeze


class Rng:
    def uniform(self, a, b): return a


def add_customers(db, org, camp, n=3, grade_ok=True):
    ids = []
    for i in range(n):
        st, cid, _ = cust.add_customer(db, org, camp, company_name=f"Co{i}", website=f"co{i}.com", email=f"sales@co{i}.com",
                                       phone="1", country="USA", contact_name=f"Pat{i} Smith")
        ids.append(cid)
    db.x("UPDATE customers SET grade='Hot', score=90 WHERE org_id=?", (org,))
    return ids


def make_round(db, org, camp, **kw):
    f = dict(subject="Hello {{company_name}}", body="Hi {{first_name}}, we make gloves.", followup_days="3,7",
             followup_bodies="", ignore_after_days=3, mode="all", grades=["Hot", "Warm"], statuses=["Unverified", "Verified"])
    f.update(kw)
    return engine.create_round(db, org, camp, None, f)


def approve(db, org, cfg, rid):
    rd = db.one("SELECT * FROM rounds WHERE id=?", (rid,))
    engine.send_test(db, org, cfg, rd, "me@test.com")
    return engine.approve_round(db, org, cfg, rid, 1)


def tick_all(db, org, cfg, n=50):
    sent = 0
    for _ in range(n):
        r = engine.send_tick(db, org, cfg, rng=Rng())
        if r == "sent":
            sent += 1
        db.x("UPDATE orgs SET next_send_at=NULL WHERE id=?", (org,))
        if r in ("idle", "limit", "window", "paused", "noaddress"):
            break
    return sent


def reply_raw(from_addr, to, in_reply_to, subject="Re: hi", body="Interested, send prices", extra=None):
    m = EmailMessage(); m["From"] = from_addr; m["To"] = to; m["Subject"] = subject
    m["Message-ID"] = f"<r{random.random()}@x.com>"
    if in_reply_to: m["In-Reply-To"] = in_reply_to
    for k, v in (extra or {}).items(): m[k] = v
    m.set_content(body)
    return m.as_bytes()


def push(db, org, raw):
    db.x("INSERT INTO demo_inbox (org_id, raw, created_at) VALUES (?,?,?)", (org, raw, utils.now()))


# ------------------------------------------------------------------ audience / round creation
def test_audience_filters_and_removals(db, org, campaign):
    ids = add_customers(db, org, campaign, 6)
    db.x("UPDATE customers SET grade='Cold' WHERE id=?", (ids[0],))
    cust.exclude_customer(db, org, ids[1], None)
    cust.add_dnc(db, org, "sales@co2.com", "unsubscribed")
    db.x("UPDATE customers SET email_status='Invalid' WHERE id=?", (ids[3],))
    db.x("UPDATE customers SET email='', email_status='None' WHERE id=?", (ids[4],))
    rid = make_round(db, org, campaign)
    cands = {r["customer_id"] for r in db.q("SELECT customer_id FROM recipients WHERE round_id=?", (rid,))}
    assert cands == {ids[5]}
    _, counts = engine.audience_candidates(db, org, campaign)
    assert counts["Excluded customers"] == 1 and counts["Unsubscribed / bounced"] == 1 and counts["Invalid email"] == 1
    assert counts["No email"] == 1 and counts["Outside chosen filters"] == 1


def test_round_numbers_and_modes(db, org, campaign, cfg):
    ids = add_customers(db, org, campaign, 4)
    r1 = make_round(db, org, campaign)
    assert db.one("SELECT number FROM rounds WHERE id=?", (r1,))["number"] == 1
    # nobody contacted yet -> mode new includes all
    r2 = make_round(db, org, campaign, mode="new")
    assert db.one("SELECT COUNT(*) c FROM recipients WHERE round_id=?", (r2,))["c"] == 4 and db.one("SELECT number FROM rounds WHERE id=?", (r2,))["number"] == 2
    # mark two as contacted & one ignored in r1
    db.x("UPDATE recipients SET steps_sent=1, status='Ignored' WHERE round_id=? AND customer_id=?", (r1, ids[0]))
    db.x("UPDATE recipients SET steps_sent=1, status='Sent' WHERE round_id=? AND customer_id=?", (r1, ids[1]))
    r3 = make_round(db, org, campaign, mode="new")
    assert {r["customer_id"] for r in db.q("SELECT customer_id FROM recipients WHERE round_id=?", (r3,))} == {ids[2], ids[3]}
    r4 = make_round(db, org, campaign, mode="ignored", prev_round_id=r1)
    assert {r["customer_id"] for r in db.q("SELECT customer_id FROM recipients WHERE round_id=?", (r4,))} == {ids[0]}
    r5 = make_round(db, org, campaign, mode="manual")
    assert db.one("SELECT COUNT(*) c FROM recipients WHERE round_id=?", (r5,))["c"] == 0
    assert engine.add_recipients(db, org, r5, [ids[0], ids[1], ids[0]]) == 2
    # replied customers can never be added
    db.x("UPDATE recipients SET status='Replied' WHERE round_id=? AND customer_id=?", (r1, ids[2]))
    assert engine.add_recipients(db, org, r5, [ids[2]]) == 0


def test_recent_contact_warning(db, org, campaign, clean_clock):
    freeze()
    ids = add_customers(db, org, campaign, 2)
    r1 = make_round(db, org, campaign)
    db.x("UPDATE recipients SET steps_sent=1, status='Sent', last_sent_at=? WHERE round_id=? AND customer_id=?",
         (utils.to_s(utils.now_dt() - timedelta(days=5)), r1, ids[0]))
    db.x("UPDATE recipients SET steps_sent=1, status='Sent', last_sent_at=? WHERE round_id=? AND customer_id=?",
         (utils.to_s(utils.now_dt() - timedelta(days=20)), r1, ids[1]))
    assert engine.recent_contact_ids(db, org, ids) == {ids[0]}


# ------------------------------------------------------------------ approval gate
def test_approval_gate(db, org, campaign, cfg):
    add_customers(db, org, campaign, 2)
    rid = make_round(db, org, campaign)
    ok, errs = engine.approve_round(db, org, cfg, rid, 1)
    assert not ok and any("test email" in e for e in errs)
    # nothing is ever sent before approval
    assert tick_all(db, org, cfg) == 0 and db.one("SELECT COUNT(*) c FROM sent_emails WHERE is_test=0")[0] == 0
    ok, errs = approve(db, org, cfg, rid)
    assert ok, errs
    rd = db.one("SELECT * FROM rounds WHERE id=?", (rid,))
    assert rd["status"] == "Approved" and rd["frozen_hash"]
    ok, errs = engine.approve_round(db, org, cfg, rid, 1)       # cannot approve twice
    assert not ok
    # editing the text after the test invalidates it
    rid2 = make_round(db, org, campaign)
    engine.send_test(db, org, cfg, db.one("SELECT * FROM rounds WHERE id=?", (rid2,)), "a@b.com")
    db.x("UPDATE rounds SET body='changed' WHERE id=?", (rid2,))
    assert not engine.approve_round(db, org, cfg, rid2, 1)[0]


def test_approval_requirements(db, org, campaign, cfg):
    add_customers(db, org, campaign, 1)
    rid = make_round(db, org, campaign)
    db.x("UPDATE orgs SET postal_address='' WHERE id=?", (org,))
    ok, errs = engine.approve_round(db, org, cfg, rid, 1)
    assert not ok and any("postal address" in e for e in errs)
    db.x("UPDATE orgs SET postal_address='1 Main St, Sialkot', email_mode='none' WHERE id=?", (org,))
    ok, errs = engine.approve_round(db, org, cfg, rid, 1)
    assert any("Connect an email" in e for e in errs)
    bad = engine.validate_content("S", "Hi {{nope}}")
    assert any("nope" in e for e in bad) and engine.validate_content("", "")


def test_approval_drops_newly_ineligible(db, org, campaign, cfg):
    ids = add_customers(db, org, campaign, 3)
    rid = make_round(db, org, campaign)
    rd = db.one("SELECT * FROM rounds WHERE id=?", (rid,))
    engine.send_test(db, org, cfg, rd, "a@b.com")
    cust.add_dnc(db, org, "sales@co0.com", "unsubscribed")        # unsubscribes after the draft was built
    cust.exclude_customer(db, org, ids[1], None)
    ok, errs = engine.approve_round(db, org, cfg, rid, 1)
    assert ok
    st = {r["customer_id"]: r["status"] for r in db.q("SELECT * FROM recipients WHERE round_id=?", (rid,))}
    assert st[ids[0]] == "Unsubscribed" and st[ids[1]] == "Skipped" and st[ids[2]] == "Pending"


# ------------------------------------------------------------------ sending
def test_send_flow_and_followups(db, org, campaign, cfg, clean_clock):
    freeze(h=10)
    ids = add_customers(db, org, campaign, 2)
    rid = make_round(db, org, campaign)
    assert approve(db, org, cfg, rid)[0]
    assert tick_all(db, org, cfg) == 2
    rows = db.q("SELECT * FROM sent_emails WHERE is_test=0 ORDER BY id")
    assert len(rows) == 2 and rows[0]["subject"] == "Hello Co0" and rows[0]["step"] == 0
    assert db.one("SELECT status FROM rounds WHERE id=?", (rid,))["status"] == "Sending"
    assert tick_all(db, org, cfg) == 0                                    # follow-up not due yet
    freeze(d=6, h=10)                                                      # day 3
    assert tick_all(db, org, cfg) == 2
    fu = db.q("SELECT * FROM sent_emails WHERE is_test=0 AND step=1")
    assert len(fu) == 2 and fu[0]["subject"] == "Re: Hello Co0"
    freeze(d=10, h=10)                                                     # day 7
    assert tick_all(db, org, cfg) == 2
    assert db.one("SELECT COUNT(*) c FROM sent_emails WHERE step=2 AND is_test=0")[0] == 2
    assert tick_all(db, org, cfg) == 0                                     # sequence complete
    # Ignored after 3 more days of silence, round finishes
    freeze(d=14, h=10)
    r = engine.mark_ignored_and_finish(db, org)
    assert r["ignored"] == 2 and r["finished"] == 1
    assert db.one("SELECT status FROM rounds WHERE id=?", (rid,))["status"] == "Finished"
    s = engine.round_stats(db, rid)
    assert s["sent"] == 2 and s["ignored"] == 2 and s["reply_rate"] == 0.0


def test_gap_between_emails(db, org, campaign, cfg, clean_clock):
    freeze()
    add_customers(db, org, campaign, 2)
    rid = make_round(db, org, campaign); approve(db, org, cfg, rid)
    assert engine.send_tick(db, org, cfg, rng=Rng()) == "sent"
    assert engine.send_tick(db, org, cfg, rng=Rng()) == "wait"
    utils.set_now(utils.now_dt() + timedelta(seconds=10))
    assert engine.send_tick(db, org, cfg, rng=Rng()) == "sent"
    # real (non-demo) accounts wait 60-180s
    db.x("UPDATE orgs SET email_mode='smtp', next_send_at=NULL WHERE id=?", (org,))
    class T(Transport):
        mode = "x"
        def send(self, m): return (m.build() and m.message_id, "", "")
    add_customers_extra = cust.add_customer(db, org, campaign, company_name="X", website="x.com", email="a@x.com")
    rid2 = make_round(db, org, campaign, mode="manual")
    engine.add_recipients(db, org, rid2, [add_customers_extra[1]])
    # approve through demo state to freeze; then flip to smtp for the gap check
    db.x("UPDATE orgs SET email_mode='demo' WHERE id=?", (org,)); approve(db, org, cfg, rid2)
    db.x("UPDATE orgs SET email_mode='smtp', next_send_at=NULL WHERE id=?", (org,))
    assert engine.send_tick(db, org, cfg, transport=T(), rng=Rng()) == "sent"
    nxt = utils.parse(db.one("SELECT next_send_at FROM orgs WHERE id=?", (org,))[0])
    assert nxt - utils.now_dt() == timedelta(seconds=60)


def test_daily_limit_and_warmup(db, org, campaign, cfg, clean_clock):
    freeze(h=10)
    add_customers(db, org, campaign, 5)
    db.x("UPDATE orgs SET daily_limit=3 WHERE id=?", (org,))
    rid = make_round(db, org, campaign); approve(db, org, cfg, rid)
    n = tick_all(db, org, cfg)
    assert n == 3 and engine.send_tick(db, org, cfg, rng=Rng()) == "limit"
    freeze(d=4, h=10)                                                      # next day, carry-over
    assert tick_all(db, org, cfg) == 2
    # warm-up: connected < 7 days ago caps to 20, raised limit 100 is hard-capped
    db.x("UPDATE orgs SET daily_limit=100, email_connected_at=? WHERE id=?", (utils.now(), org))
    assert engine.effective_limit(db.one("SELECT * FROM orgs WHERE id=?", (org,))) == 20
    db.x("UPDATE orgs SET daily_limit=500, email_connected_at='2020-01-01 00:00:00' WHERE id=?", (org,))
    assert engine.effective_limit(db.one("SELECT * FROM orgs WHERE id=?", (org,))) == 100


def test_window_and_weekdays(db, org, campaign, cfg, clean_clock):
    freeze(d=3, h=8)
    add_customers(db, org, campaign, 1)
    rid = make_round(db, org, campaign); approve(db, org, cfg, rid)
    db.x("UPDATE orgs SET email_mode='smtp', window_start=9, window_end=17, weekdays_only=1, timezone='Asia/Karachi' WHERE id=?", (org,))
    class T(Transport):
        def send(self, m): return (m.build() and m.message_id, "", "")
    freeze(d=3, h=2)                      # Tue 07:00 Karachi: before window
    assert engine.send_tick(db, org, cfg, transport=T(), rng=Rng()) == "window"
    freeze(d=7, h=6)                      # Sat 11:00 Karachi: weekend
    assert engine.send_tick(db, org, cfg, transport=T(), rng=Rng()) == "window"
    freeze(d=3, h=8)                      # Tue 13:00 Karachi: open
    assert engine.send_tick(db, org, cfg, transport=T(), rng=Rng()) == "sent"


def test_start_date_and_round_cap(db, org, campaign, cfg, clean_clock):
    freeze(d=3)
    add_customers(db, org, campaign, 3)
    rid = make_round(db, org, campaign, start_date="2026-11-05", daily_limit=1); approve(db, org, cfg, rid)
    assert engine.send_tick(db, org, cfg, rng=Rng()) == "idle"             # start date in the future
    freeze(d=5)
    assert engine.send_tick(db, org, cfg, rng=Rng()) == "sent"
    db.x("UPDATE orgs SET next_send_at=NULL WHERE id=?", (org,))
    assert engine.send_tick(db, org, cfg, rng=Rng()) == "idle"             # round cap of 1/day reached


def test_do_not_contact_rechecked_before_each_email(db, org, campaign, cfg, clean_clock):
    freeze()
    ids = add_customers(db, org, campaign, 3)
    rid = make_round(db, org, campaign); approve(db, org, cfg, rid)
    cust.add_dnc(db, org, "sales@co0.com", "unsubscribed")                 # added AFTER approval
    cust.add_dnc(db, org, "sales@co1.com", "bounced")
    assert tick_all(db, org, cfg) == 1
    st = {r["customer_id"]: r["status"] for r in db.q("SELECT * FROM recipients WHERE round_id=?", (rid,))}
    assert st[ids[0]] == "Unsubscribed" and st[ids[1]] == "Bounced" and st[ids[2]] == "Sent"
    assert db.one("SELECT COUNT(*) c FROM sent_emails WHERE is_test=0 AND to_email IN ('sales@co0.com','sales@co1.com')")[0] == 0


def test_paused_round_sends_nothing(db, org, campaign, cfg, clean_clock):
    freeze()
    add_customers(db, org, campaign, 2)
    rid = make_round(db, org, campaign); approve(db, org, cfg, rid)
    db.x("UPDATE rounds SET status='Paused' WHERE id=?", (rid,))
    assert tick_all(db, org, cfg) == 0
    db.x("UPDATE rounds SET status='Approved' WHERE id=?", (rid,))
    assert tick_all(db, org, cfg) == 2


def test_missing_address_blocks(db, org, campaign, cfg, clean_clock):
    freeze()
    add_customers(db, org, campaign, 1)
    rid = make_round(db, org, campaign); approve(db, org, cfg, rid)
    db.x("UPDATE orgs SET postal_address='' WHERE id=?", (org,))
    assert engine.send_tick(db, org, cfg, rng=Rng()) == "noaddress"


class Boom(Transport):
    def __init__(self, exc): self.exc = exc
    def send(self, m): raise self.exc


def test_send_errors(db, org, campaign, cfg, clean_clock):
    freeze(h=10)
    ids = add_customers(db, org, campaign, 2)
    rid = make_round(db, org, campaign); approve(db, org, cfg, rid)
    # provider limit -> stop until next local day
    assert engine.send_tick(db, org, cfg, transport=Boom(SendLimitError("daily limit")), rng=Rng()) == "limit"
    assert engine.send_tick(db, org, cfg, rng=Rng()) == "paused"
    freeze(d=4, h=1)
    assert engine.send_tick(db, org, cfg, transport=Boom(SendError("550 no such user")), rng=Rng()) == "refused"
    r = db.one("SELECT * FROM recipients WHERE customer_id=?", (ids[0],))
    assert r["status"] == "Bounced" and db.one("SELECT email_status FROM customers WHERE id=?", (ids[0],))[0] == "Invalid"
    assert db.one("SELECT 1 FROM dnc WHERE email='sales@co0.com'")
    # transient errors back off, 5 in a row pause for an hour
    for i in range(5):
        db.x("UPDATE orgs SET next_send_at=NULL WHERE id=?", (org,))
        assert engine.send_tick(db, org, cfg, transport=Boom(TransientError("net")), rng=Rng()) == "error"
    assert db.one("SELECT paused_until FROM orgs WHERE id=?", (org,))[0]
    assert db.one("SELECT status FROM recipients WHERE customer_id=?", (ids[1],))["status"] == "Pending"   # still retried later
    # auth errors surface a reconnect message
    db.x("UPDATE orgs SET paused_until=NULL, next_send_at=NULL WHERE id=?", (org,))
    assert engine.send_tick(db, org, cfg, transport=Boom(AuthError("bad")), rng=Rng()) == "auth"
    assert "Reconnect" in db.one("SELECT last_error FROM orgs WHERE id=?", (org,))[0]


def test_followups_first_and_limit_shared(db, org, campaign, cfg, clean_clock):
    freeze(h=10)
    add_customers(db, org, campaign, 3)
    db.x("UPDATE orgs SET daily_limit=2 WHERE id=?", (org,))
    rid = make_round(db, org, campaign); approve(db, org, cfg, rid)
    freeze(d=3); db.x("UPDATE orgs SET daily_limit=3 WHERE id=?", (org,))
    assert tick_all(db, org, cfg) == 3
    freeze(d=7, h=10)                                                       # 2 follow-ups due AND 1 new waiting
    db.x("UPDATE orgs SET daily_limit=3 WHERE id=?", (org,))
    assert tick_all(db, org, cfg) == 3
    order = [r["step"] for r in db.q("SELECT step FROM sent_emails WHERE is_test=0 AND sent_at>=? ORDER BY id", (utils.to_s(datetime(2026, 11, 7)),))]
    assert order[:2] == [1, 1]                                              # follow-ups before new first emails


# ------------------------------------------------------------------ replies
def send_round(db, org, cfg, n=3):
    ids = add_customers(db, org, db.one("SELECT id FROM campaigns")["id"], n)
    rid = make_round(db, org, db.one("SELECT id FROM campaigns")["id"]); assert approve(db, org, cfg, rid)[0]
    tick_all(db, org, cfg)
    return ids, rid


def rec(db, cid): return db.one("SELECT * FROM recipients WHERE customer_id=?", (cid,))


def test_reply_cancels_followups_and_creates_task(db, org, campaign, cfg, clean_clock):
    freeze(h=10)
    ids, rid = send_round(db, org, cfg, 2)
    r = rec(db, ids[0])
    push(db, org, reply_raw("Pat <sales@co0.com>", "demo@demo.local", r["first_message_id"], body="Yes, interested!"))
    out = engine.process_replies(db, org, cfg)
    assert out["replies"] == 1
    assert rec(db, ids[0])["status"] == "Replied" and rec(db, ids[1])["status"] == "Sent"
    t = db.one("SELECT * FROM tasks WHERE customer_id=?", (ids[0],))
    assert t["status"] == "Open" and "interested" in t["note"]
    freeze(d=7)                                                             # follow-ups: only the non-replied customer
    tick_all(db, org, cfg)
    assert db.one("SELECT COUNT(*) c FROM sent_emails WHERE recipient_id=? AND step>0", (rec(db, ids[0])["id"],))[0] == 0
    assert db.one("SELECT COUNT(*) c FROM sent_emails WHERE recipient_id=? AND step>0", (rec(db, ids[1])["id"],))[0] == 1
    # same message processed twice does nothing
    push(db, org, reply_raw("sales@co0.com", "me", r["first_message_id"]))
    engine.process_replies(db, org, cfg)
    assert db.one("SELECT COUNT(*) c FROM tasks WHERE customer_id=?", (ids[0],))[0] == 1


def test_auto_reply_does_not_stop_followups(db, org, campaign, cfg, clean_clock):
    freeze(h=10)
    ids, rid = send_round(db, org, cfg, 1)
    r = rec(db, ids[0])
    push(db, org, reply_raw("sales@co0.com", "me", r["first_message_id"], subject="Automatic reply: Out of office",
                            extra={"Auto-Submitted": "auto-replied"}, body="I am away"))
    push(db, org, reply_raw("sales@co0.com", "me", r["first_message_id"], subject="Out of Office", body="away"))
    out = engine.process_replies(db, org, cfg)
    assert out["auto"] == 2 and out["replies"] == 0 and rec(db, ids[0])["status"] == "Sent"
    freeze(d=7)
    assert tick_all(db, org, cfg) == 1


def test_bounce_detection_dsn(db, org, campaign, cfg, clean_clock):
    freeze(h=10)
    ids, rid = send_round(db, org, cfg, 2)
    r = rec(db, ids[0])
    dsn = (f"From: Mail Delivery Subsystem <mailer-daemon@googlemail.com>\r\nTo: demo@demo.local\r\nSubject: Delivery Status Notification (Failure)\r\n"
           f"Message-ID: <dsn1@googlemail.com>\r\nMIME-Version: 1.0\r\nContent-Type: multipart/report; report-type=delivery-status; boundary=B\r\n\r\n"
           f"--B\r\nContent-Type: text/plain\r\n\r\nAddress not found\r\n--B\r\nContent-Type: message/delivery-status\r\n\r\n"
           f"Reporting-MTA: dns; googlemail.com\r\n\r\nFinal-Recipient: rfc822; sales@co0.com\r\nAction: failed\r\nStatus: 5.1.1\r\n\r\n"
           f"--B\r\nContent-Type: message/rfc822\r\n\r\nMessage-ID: {r['first_message_id']}\r\nSubject: Hello\r\n\r\nbody\r\n--B--\r\n").encode()
    push(db, org, dsn)
    out = engine.process_replies(db, org, cfg)
    assert out["bounces"] == 1 and rec(db, ids[0])["status"] == "Bounced"
    assert db.one("SELECT email_status FROM customers WHERE id=?", (ids[0],))[0] == "Invalid"
    assert db.one("SELECT reason FROM dnc WHERE email='sales@co0.com'")[0] == "bounced"
    # a soft (4.x.x) bounce is only noted
    soft = dsn.replace(b"sales@co0.com", b"sales@co1.com").replace(b"5.1.1", b"4.2.2").replace(b"<dsn1@", b"<dsn2@").replace(
        r["first_message_id"].encode(), rec(db, ids[1])["first_message_id"].encode())
    push(db, org, soft)
    out = engine.process_replies(db, org, cfg)
    assert out["bounces"] == 0 and rec(db, ids[1])["status"] == "Sent"


def test_unsubscribe_by_link_and_reply(db, org, campaign, cfg, clean_clock):
    freeze(h=10)
    ids, rid = send_round(db, org, cfg, 3)
    tok = engine.unsub_token(cfg, org, ids[0])
    assert engine.read_unsub_token(cfg, tok) == (org, ids[0]) and engine.read_unsub_token(cfg, tok + "x") is None
    assert engine.read_unsub_token(cfg, "garbage") is None
    engine.unsubscribe_customer(db, org, ids[0])
    assert rec(db, ids[0])["status"] == "Unsubscribed" and db.one("SELECT 1 FROM dnc WHERE email='sales@co0.com'")
    r = rec(db, ids[1])
    push(db, org, reply_raw("sales@co1.com", "me", r["first_message_id"], body="Please unsubscribe me"))
    out = engine.process_replies(db, org, cfg)
    assert out["unsub"] == 1 and rec(db, ids[1])["status"] == "Unsubscribed"
    assert db.one("SELECT COUNT(*) c FROM tasks")[0] == 0


def test_reply_matching_fallbacks(db, org, campaign, cfg, clean_clock):
    freeze(h=10)
    ids, rid = send_round(db, org, cfg, 2)
    # no In-Reply-To header (Gmail rewrote ids): matched by sender address
    push(db, org, reply_raw("sales@co0.com", "me", None, subject="Re: Hello Co0"))
    # unrelated mail is ignored
    push(db, org, reply_raw("stranger@else.com", "me", None, subject="Buy my SEO"))
    # our own address is ignored
    push(db, org, reply_raw("demo@demo.local", "me", None))
    out = engine.process_replies(db, org, cfg)
    assert out["replies"] == 1 and rec(db, ids[0])["status"] == "Replied" and rec(db, ids[1])["status"] == "Sent"


def test_parse_inbound_headers():
    ib = parse_inbound(reply_raw("A <A@x.com>", "b@y.com", "<abc@z.com>", extra={"References": "<r1@z.com> <abc@z.com>"}))
    assert ib.from_addr == "a@x.com" and ib.in_reply_to == "<abc@z.com>" and "<r1@z.com>" in ib.references and not ib.auto_reply and not ib.is_bounce
    assert parse_inbound(reply_raw("a@x.com", "b", None, extra={"Precedence": "auto_reply"})).auto_reply
    assert parse_inbound(reply_raw("a@x.com", "b", None, extra={"X-Autoreply": "yes"})).auto_reply


def test_replied_customer_not_emailed_in_other_round(db, org, campaign, cfg, clean_clock):
    freeze(h=10)
    ids, rid = send_round(db, org, cfg, 1)
    push(db, org, reply_raw("sales@co0.com", "me", rec(db, ids[0])["first_message_id"]))
    engine.process_replies(db, org, cfg)
    r2 = make_round(db, org, campaign, mode="manual")
    assert engine.add_recipients(db, org, r2, ids) == 0
    # even if inserted directly, the send-time check refuses
    db.x("INSERT INTO recipients (org_id,round_id,customer_id,updated_at) VALUES (?,?,?,?)", (org, r2, ids[0], utils.now()))
    approve(db, org, cfg, r2)
    tick_all(db, org, cfg)
    assert db.one("SELECT status FROM recipients WHERE round_id=?", (r2,))[0] in ("Skipped", "Replied")
    assert db.one("SELECT COUNT(*) c FROM sent_emails WHERE round_id=? AND is_test=0", (r2,))[0] == 0


def test_stats_example_from_spec(db, org, campaign):
    """Spec example: R1 100 emailed -> 62 ignored, 8 replied, 3 bounced, 2 unsubscribed; round 2 counted separately."""
    r1 = db.x("INSERT INTO rounds (org_id,campaign_id,number,subject,body,created_at) VALUES (?,?,1,'s','b',?)", (org, campaign, utils.now()))
    i = 0
    for status, n in (("Ignored", 62), ("Replied", 8), ("Bounced", 3), ("Unsubscribed", 2), ("Sent", 25)):
        for _ in range(n):
            i += 1
            c = db.x("INSERT INTO customers (org_id,campaign_id,company_name,dedup_key,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                     (org, campaign, f"c{i}", f"c{i}.com", utils.now(), utils.now()))
            db.x("INSERT INTO recipients (org_id,round_id,customer_id,status,steps_sent,updated_at) VALUES (?,?,?,?,1,?)", (org, r1, c, status, utils.now()))
    s = engine.round_stats(db, r1)
    assert (s["selected"], s["sent"], s["ignored"], s["replied"], s["bounced"], s["unsubscribed"]) == (100, 100, 62, 8, 3, 2)
    assert s["reply_rate"] == 8.0
    r2 = db.x("INSERT INTO rounds (org_id,campaign_id,number,subject,body,created_at) VALUES (?,?,2,'s','b',?)", (org, campaign, utils.now()))
    assert engine.round_stats(db, r2)["selected"] == 0
