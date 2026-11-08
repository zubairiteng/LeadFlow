"""Rounds, audience, approval gate, sending (daily limits, gaps, follow-ups), reply reading, analytics."""
from __future__ import annotations

import hashlib
import json
import random
import re
from datetime import timedelta

from itsdangerous import BadSignature, URLSafeSerializer

from .. import utils
from . import customers as cust
from .mailing import OutMessage, customer_ctx, footer, render, unknown_vars
from .transports import (AuthError, SendError, SendLimitError, TransientError, make_transport)

DEFAULT_FOLLOWUPS = [
    "Hi {{first_name}},\n\nJust following up on my earlier note in case it got buried. "
    "Would it make sense to have a quick look?\n",
    "Hi {{first_name}},\n\nLast note from me. If this is not relevant, no problem at all. "
    "I am happy to help whenever the timing suits you.\n",
]
ACTIVE_ROUND = ("Approved", "Sending")
UNSUB_WORDS = re.compile(r"\b(unsubscribe|remove me|stop (emailing|sending|contacting)|do not contact|don't contact)\b", re.I)
HARD_CAP = 100


# ---------------------------------------------------------------- tokens
def unsub_token(cfg, org_id: int, customer_id: int) -> str:
    return URLSafeSerializer(cfg["SECRET_KEY"], salt="unsub").dumps({"o": org_id, "c": customer_id})


def read_unsub_token(cfg, token: str) -> tuple[int, int] | None:
    try:
        d = URLSafeSerializer(cfg["SECRET_KEY"], salt="unsub").loads(token)
        return int(d["o"]), int(d["c"])
    except (BadSignature, KeyError, ValueError, TypeError):
        return None


def unsub_url(cfg, org_id: int, customer_id: int) -> str:
    return f"{cfg['BASE_URL'].rstrip('/')}/u/{unsub_token(cfg, org_id, customer_id)}"


# ---------------------------------------------------------------- round helpers
def followup_days(rnd) -> list[int]:
    out = []
    for p in (rnd["followup_days"] or "").split(","):
        p = p.strip()
        if p.isdigit() and 1 <= int(p) <= 60:
            out.append(int(p))
    return sorted(set(out))[:5]


def followup_bodies(rnd) -> list[str]:
    parts = [p.strip("\n") for p in re.split(r"\n-{3,}\n", (rnd["followup_bodies"] or "").replace("\r", ""))]
    n = len(followup_days(rnd))
    out = []
    for i in range(n):
        txt = parts[i].strip() if i < len(parts) and parts[i].strip() else ""
        out.append(txt or DEFAULT_FOLLOWUPS[min(i, len(DEFAULT_FOLLOWUPS) - 1)])
    return out


def total_steps(rnd) -> int:
    return 1 + len(followup_days(rnd))


def content_hash(rnd) -> str:
    s = "\x00".join([rnd["subject"] or "", rnd["body"] or "", rnd["followup_days"] or "",
                     "\x01".join(followup_bodies(rnd))])
    return hashlib.sha256(s.encode()).hexdigest()


def validate_content(subject: str, body: str, fu_bodies: str = "") -> list[str]:
    errs = []
    if not (subject or "").strip():
        errs.append("Subject is required.")
    if len(subject or "") > 200:
        errs.append("Subject is too long (max 200).")
    if not (body or "").strip():
        errs.append("Email text is required.")
    bad = unknown_vars(subject) | unknown_vars(body) | unknown_vars(fu_bodies)
    if bad:
        errs.append("Unknown placeholders: " + ", ".join("{{%s}}" % b for b in sorted(bad)))
    return errs


# ---------------------------------------------------------------- audience
def audience_candidates(db, org_id: int, campaign_id: int, mode: str = "all", grades=("Hot", "Warm"),
                        statuses=("Unverified", "Verified"), prev_round_id: int | None = None):
    """Eligible customers + a count of why others were removed."""
    rows = db.q("SELECT * FROM customers WHERE org_id=? AND campaign_id=? ORDER BY score DESC, id", (org_id, campaign_id))
    replied = {r["customer_id"] for r in db.q(
        "SELECT customer_id FROM recipients WHERE org_id=? AND status='Replied'", (org_id,))}
    contacted = {r["customer_id"] for r in db.q(
        "SELECT r.customer_id FROM recipients r JOIN rounds d ON d.id=r.round_id "
        "WHERE r.org_id=? AND d.campaign_id=? AND r.steps_sent>=1", (org_id, campaign_id))}
    ignored = set()
    if mode == "ignored" and prev_round_id:
        ignored = {r["customer_id"] for r in db.q(
            "SELECT customer_id FROM recipients WHERE org_id=? AND round_id=? AND status='Ignored'",
            (org_id, prev_round_id))}
    counts = {"Excluded customers": 0, "No email": 0, "Invalid email": 0, "Unsubscribed / bounced": 0,
              "Already replied": 0, "Outside chosen filters": 0}
    ok = []
    for c in rows:
        if c["excluded"]:
            counts["Excluded customers"] += 1
        elif not c["email"]:
            counts["No email"] += 1
        elif c["email_status"] == "Invalid":
            counts["Invalid email"] += 1
        elif cust.is_dnc(db, org_id, c["email"]):
            counts["Unsubscribed / bounced"] += 1
        elif c["id"] in replied:
            counts["Already replied"] += 1
        elif mode == "manual":
            ok.append(c)
        elif c["grade"] not in grades or c["email_status"] not in statuses:
            counts["Outside chosen filters"] += 1
        elif mode == "new" and c["id"] in contacted:
            counts["Outside chosen filters"] += 1
        elif mode == "ignored" and c["id"] not in ignored:
            counts["Outside chosen filters"] += 1
        else:
            ok.append(c)
    return ok, counts


def recent_contact_ids(db, org_id: int, customer_ids: list[int], days: int = 14) -> set[int]:
    if not customer_ids:
        return set()
    since = utils.to_s(utils.now_dt() - timedelta(days=days))
    out: set[int] = set()
    for i in range(0, len(customer_ids), 500):
        chunk = customer_ids[i:i + 500]
        ph = ",".join("?" * len(chunk))
        rows = db.q(f"SELECT DISTINCT r.customer_id FROM recipients r WHERE r.org_id=? AND r.customer_id IN ({ph}) "
                    f"AND r.last_sent_at >= ?", (org_id, *chunk, since))
        out |= {r["customer_id"] for r in rows}
    return out


def create_round(db, org_id: int, campaign_id: int, user_id: int, f: dict) -> int:
    n = (db.one("SELECT COALESCE(MAX(number),0)+1 AS n FROM rounds WHERE campaign_id=?", (campaign_id,))["n"])
    audience = {"mode": f.get("mode", "all"), "grades": f.get("grades", ["Hot", "Warm"]),
                "statuses": f.get("statuses", ["Unverified", "Verified"]), "prev_round_id": f.get("prev_round_id")}
    with db.tx():
        rid = db.x(
            "INSERT INTO rounds (org_id,campaign_id,number,name,subject,body,followup_days,followup_bodies,"
            "ignore_after_days,daily_limit,audience,start_date,created_by,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (org_id, campaign_id, n, (f.get("name") or f"Round {n}")[:100], f["subject"].strip(), f["body"].strip(),
             f.get("followup_days", "3,7"), f.get("followup_bodies", ""), int(f.get("ignore_after_days", 3)),
             f.get("daily_limit"), json.dumps(audience), f.get("start_date") or None, user_id, utils.now()))
        if audience["mode"] != "manual":
            cands, _ = audience_candidates(db, org_id, campaign_id, audience["mode"], audience["grades"],
                                           audience["statuses"], audience["prev_round_id"])
            add_recipients(db, org_id, rid, [c["id"] for c in cands])
    return rid


def add_recipients(db, org_id: int, round_id: int, customer_ids: list[int]) -> int:
    n = 0
    for cid in customer_ids:
        c = db.one("SELECT * FROM customers WHERE id=? AND org_id=?", (cid, org_id))
        if not c or c["excluded"] or not c["email"] or c["email_status"] == "Invalid" or cust.is_dnc(db, org_id, c["email"]):
            continue
        if db.one("SELECT 1 FROM recipients WHERE org_id=? AND customer_id=? AND status='Replied'", (org_id, cid)):
            continue
        cur = db.n("INSERT OR IGNORE INTO recipients (org_id,round_id,customer_id,updated_at) VALUES (?,?,?,?)",
                   (org_id, round_id, cid, utils.now()))
        n += max(cur, 0)
    return n


def set_recipient_status(db, rec_id: int, status: str, note: str = "") -> None:
    db.x("UPDATE recipients SET status=?, note=?, updated_at=? WHERE id=?", (status, note, utils.now(), rec_id))


def log_event(db, org_id: int, type_: str, detail: str = "", customer_id=None, recipient_id=None, round_id=None) -> None:
    db.x("INSERT INTO events (org_id,customer_id,recipient_id,round_id,type,detail,created_at) VALUES (?,?,?,?,?,?,?)",
         (org_id, customer_id, recipient_id, round_id, type_, detail[:500], utils.now()))


# ---------------------------------------------------------------- unsubscribe / bounce / reply
def unsubscribe_customer(db, org_id: int, customer_id: int, reason: str = "unsubscribed") -> bool:
    c = db.one("SELECT * FROM customers WHERE id=? AND org_id=?", (customer_id, org_id))
    if not c:
        return False
    with db.tx():
        if c["email"]:
            cust.add_dnc(db, org_id, c["email"], reason)
        for r in db.q("SELECT id, round_id FROM recipients WHERE customer_id=? AND status IN ('Pending','Sent')", (customer_id,)):
            set_recipient_status(db, r["id"], "Unsubscribed", "Unsubscribed")
        log_event(db, org_id, "unsubscribed", reason, customer_id)
    return True


def apply_bounce(db, org_id: int, rec, detail: str) -> None:
    with db.tx():
        c = db.one("SELECT * FROM customers WHERE id=?", (rec["customer_id"],))
        if c and c["email"]:
            cust.add_dnc(db, org_id, c["email"], "bounced")
            db.x("UPDATE customers SET email_status='Invalid', updated_at=? WHERE id=?", (utils.now(), c["id"]))
            cust.rescore(db, org_id, c["id"])
        for r in db.q("SELECT id FROM recipients WHERE customer_id=? AND status IN ('Pending','Sent')", (rec["customer_id"],)):
            set_recipient_status(db, r["id"], "Bounced", detail[:200])
        log_event(db, org_id, "bounced", detail, rec["customer_id"], rec["id"], rec["round_id"])


def apply_reply(db, org_id: int, rec, snippet: str) -> None:
    with db.tx():
        if rec["status"] in ("Sent", "Pending", "Ignored"):
            set_recipient_status(db, rec["id"], "Replied", "")
        log_event(db, org_id, "replied", snippet, rec["customer_id"], rec["id"], rec["round_id"])
        if not db.one("SELECT 1 FROM tasks WHERE recipient_id=? AND status='Open'", (rec["id"],)):
            db.x("INSERT INTO tasks (org_id,customer_id,round_id,recipient_id,kind,note,created_at) VALUES (?,?,?,?,?,?,?)",
                 (org_id, rec["customer_id"], rec["round_id"], rec["id"], "reply", snippet[:500], utils.now()))


# ---------------------------------------------------------------- sending
def effective_limit(org, round_cap=None) -> int:
    lim = max(1, min(int(org["daily_limit"] or 50), HARD_CAP))
    started = utils.parse(org["email_connected_at"])
    if started and utils.now_dt() < started + timedelta(days=7):
        lim = min(lim, 20)  # warm-up: first week
    return lim


def sent_today(db, org, round_id=None) -> int:
    since = utils.local_day_start_utc(org["timezone"])
    sql = "SELECT COUNT(*) c FROM sent_emails WHERE org_id=? AND status='sent' AND is_test=0 AND sent_at>=?"
    args: list = [org["id"], since]
    if round_id:
        sql += " AND round_id=?"
        args.append(round_id)
    return db.one(sql, args)["c"]


def in_send_window(org, demo: bool = False) -> bool:
    if demo:
        return True
    ln = utils.local_now(org["timezone"])
    if org["weekdays_only"] and ln.weekday() >= 5:
        return False
    return org["window_start"] <= ln.hour < org["window_end"]


def _blocked(db, org_id: int, r, c) -> tuple[str, str] | None:
    """Re-check right before each email. Returns (new_status, note) if this recipient must not be emailed."""
    if c["excluded"]:
        return "Skipped", "Customer excluded"
    if not c["email"] or c["email_status"] == "Invalid":
        return "Skipped", "No valid email"
    d = db.one("SELECT reason FROM dnc WHERE org_id=? AND email=?", (org_id, c["email"].lower()))
    if d:
        return ("Unsubscribed", "On do-not-contact list") if d["reason"] != "bounced" else ("Bounced", "On do-not-contact list")
    if db.one("SELECT 1 FROM recipients WHERE org_id=? AND customer_id=? AND status='Replied' AND id<>?",
              (org_id, c["id"], r["id"])):
        return "Skipped", "Customer already replied"
    return None


def _candidates(db, org):
    now = utils.now_dt()
    today = utils.local_today_str(org["timezone"])
    due = []
    for r in db.q("SELECT r.*, d.followup_days, d.status AS rstatus FROM recipients r JOIN rounds d ON d.id=r.round_id "
                  "WHERE r.org_id=? AND r.status='Sent' AND d.status IN ('Approved','Sending')", (org["id"],)):
        days = followup_days(r)
        if r["steps_sent"] < 1 or r["steps_sent"] > len(days):
            continue
        first = utils.parse(r["first_sent_at"])
        last = utils.parse(r["last_sent_at"])
        due_at = first + timedelta(days=days[r["steps_sent"] - 1])
        if due_at <= now and last + timedelta(days=1) <= now:
            due.append((due_at, r))
    due.sort(key=lambda t: t[0])
    for _, r in due:
        yield r
    for r in db.q("SELECT r.* FROM recipients r JOIN rounds d ON d.id=r.round_id WHERE r.org_id=? AND r.status='Pending' "
                  "AND d.status IN ('Approved','Sending') AND (d.start_date IS NULL OR d.start_date<=?) "
                  "ORDER BY d.id, r.id", (org["id"], today)):
        yield r


def send_tick(db, org_id: int, cfg: dict, transport=None, rng=random) -> str:
    """Send at most ONE email if allowed now. Called every ~20s by the scheduler."""
    org = db.one("SELECT * FROM orgs WHERE id=?", (org_id,))
    if not org or org["email_mode"] == "none":
        return "noconn"
    t = transport or make_transport(db, org, cfg)
    if t is None:
        return "noconn"
    demo = org["email_mode"] == "demo"
    now = utils.now_dt()
    if org["paused_until"] and utils.parse(org["paused_until"]) > now:
        return "paused"
    if org["next_send_at"] and utils.parse(org["next_send_at"]) > now:
        return "wait"
    if not in_send_window(org, demo):
        return "window"
    if sent_today(db, org) >= effective_limit(org):
        return "limit"
    if not (org["postal_address"] or "").strip():
        return "noaddress"
    checked = 0
    for r in _candidates(db, org):
        checked += 1
        if checked > 300:
            break
        rd = db.one("SELECT * FROM rounds WHERE id=?", (r["round_id"],))
        c = db.one("SELECT * FROM customers WHERE id=?", (r["customer_id"],))
        blocked = _blocked(db, org_id, r, c)
        if blocked:
            set_recipient_status(db, r["id"], blocked[0], blocked[1])
            log_event(db, org_id, "skipped", blocked[1], c["id"], r["id"], rd["id"])
            continue
        if rd["daily_limit"] and sent_today(db, org, rd["id"]) >= int(rd["daily_limit"]):
            continue
        return _send_one(db, org, cfg, t, rd, r, c, demo, rng)
    return "idle"


def _send_one(db, org, cfg, t, rd, r, c, demo, rng) -> str:
    step = r["steps_sent"]
    ctx = customer_ctx(c, org)
    first_subject = render(rd["subject"], ctx)
    if step == 0:
        subject, body = first_subject, render(rd["body"], ctx)
    else:
        subject = "Re: " + first_subject
        body = render(followup_bodies(rd)[step - 1], ctx)
    url = unsub_url(cfg, org["id"], c["id"])
    msg = OutMessage(from_addr=org["email_address"], from_name=org["sender_name"] or org["name"],
                     to_addr=c["email"], subject=subject, body=body + footer(org, url), unsub_url=url,
                     in_reply_to=r["first_message_id"] if step else "", thread_id=r["thread_id"] if step else "")
    now_s = utils.now()
    try:
        mid, alt, thread = t.send(msg)
    except SendError as e:
        db.x("INSERT INTO sent_emails (org_id,recipient_id,round_id,step,to_email,subject,status,error,sent_at) "
             "VALUES (?,?,?,?,?,?,?,?,?)", (org["id"], r["id"], rd["id"], step, c["email"], subject, "failed", str(e)[:300], now_s))
        apply_bounce(db, org["id"], r, f"Refused at send: {str(e)[:150]}")
        return "refused"
    except SendLimitError as e:
        db.x("UPDATE orgs SET paused_until=?, last_error=? WHERE id=?",
             (utils.to_s(utils.next_local_day_start_utc(org["timezone"])), "Provider daily limit reached: " + str(e)[:150], org["id"]))
        log_event(db, org["id"], "limit", str(e)[:200])
        return "limit"
    except AuthError as e:
        db.x("UPDATE orgs SET paused_until=?, last_error=? WHERE id=?",
             (utils.to_s(utils.now_dt() + timedelta(hours=6)), "Reconnect needed: " + str(e)[:150], org["id"]))
        return "auth"
    except TransientError as e:
        errs = org["consecutive_errors"] + 1
        pause = utils.to_s(utils.now_dt() + timedelta(hours=1)) if errs >= 5 else None
        db.x("UPDATE orgs SET consecutive_errors=?, last_error=?, next_send_at=?, paused_until=COALESCE(?,paused_until) WHERE id=?",
             (errs, str(e)[:200], utils.to_s(utils.now_dt() + timedelta(minutes=5)), pause, org["id"]))
        return "error"
    with db.tx():
        db.x("INSERT INTO sent_emails (org_id,recipient_id,round_id,step,message_id,message_id_alt,thread_id,to_email,"
             "subject,status,sent_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
             (org["id"], r["id"], rd["id"], step, mid, alt, thread, c["email"], subject, "sent", now_s))
        if step == 0:
            db.x("UPDATE recipients SET status='Sent', steps_sent=1, first_sent_at=?, last_sent_at=?, thread_id=?, "
                 "first_message_id=?, updated_at=? WHERE id=?", (now_s, now_s, thread, mid, now_s, r["id"]))
        else:
            db.x("UPDATE recipients SET steps_sent=steps_sent+1, last_sent_at=?, updated_at=? WHERE id=?",
                 (now_s, now_s, r["id"]))
        if rd["status"] == "Approved":
            db.x("UPDATE rounds SET status='Sending' WHERE id=?", (rd["id"],))
        gap = rng.uniform(2, 5) if demo else rng.uniform(60, 180)
        db.x("UPDATE orgs SET next_send_at=?, consecutive_errors=0, last_error='' WHERE id=?",
             (utils.to_s(utils.now_dt() + timedelta(seconds=gap)), org["id"]))
        log_event(db, org["id"], "sent", f"Email {step + 1} to {c['email']}", c["id"], r["id"], rd["id"])
    return "sent"


# ---------------------------------------------------------------- maintenance
def mark_ignored_and_finish(db, org_id: int) -> dict:
    now = utils.now_dt()
    ign = 0
    for r in db.q("SELECT r.*, d.followup_days, d.ignore_after_days FROM recipients r JOIN rounds d ON d.id=r.round_id "
                  "WHERE r.org_id=? AND r.status='Sent' AND d.status<>'Draft'", (org_id,)):
        if r["steps_sent"] >= 1 + len(followup_days(r)):
            last = utils.parse(r["last_sent_at"])
            if last + timedelta(days=int(r["ignore_after_days"] or 3)) <= now:
                set_recipient_status(db, r["id"], "Ignored", "")
                log_event(db, org_id, "ignored", "No reply after all emails", r["customer_id"], r["id"], r["round_id"])
                ign += 1
    fin = 0
    for d in db.q("SELECT id FROM rounds WHERE org_id=? AND status IN ('Approved','Sending')", (org_id,)):
        left = db.one("SELECT COUNT(*) c FROM recipients WHERE round_id=? AND status IN ('Pending','Sent')", (d["id"],))["c"]
        if left == 0:
            db.x("UPDATE rounds SET status='Finished' WHERE id=?", (d["id"],))
            fin += 1
    return {"ignored": ign, "finished": fin}


def process_replies(db, org_id: int, cfg: dict, transport=None) -> dict:
    org = db.one("SELECT * FROM orgs WHERE id=?", (org_id,))
    res = {"replies": 0, "auto": 0, "bounces": 0, "unsub": 0, "seen": 0}
    if not org or org["email_mode"] == "none":
        return res
    t = transport or make_transport(db, org, cfg)
    if t is None:
        return res
    started = utils.now_dt()
    last = utils.parse(org["last_reply_check"])
    since = (last - timedelta(hours=2)) if last else started - timedelta(days=14)
    since = max(since, started - timedelta(days=14))
    try:
        inbound = t.fetch_inbound(since)
    except (AuthError, TransientError) as e:
        db.x("UPDATE orgs SET last_error=? WHERE id=?", (f"Reading replies failed: {str(e)[:150]}", org_id))
        return res
    sent = db.q("SELECT s.recipient_id, s.message_id, s.message_id_alt, s.thread_id, s.to_email, s.subject FROM sent_emails s "
                "WHERE s.org_id=? AND s.status='sent' AND s.is_test=0 AND s.recipient_id IS NOT NULL AND s.sent_at>=?",
                (org_id, utils.to_s(started - timedelta(days=120))))
    by_id: dict[str, int] = {}
    by_thread: dict[str, int] = {}
    by_addr: dict[str, list] = {}
    for s in sent:
        for k in (s["message_id"], s["message_id_alt"]):
            if k:
                by_id[k] = s["recipient_id"]
        if s["thread_id"]:
            by_thread[s["thread_id"]] = s["recipient_id"]
        by_addr.setdefault(s["to_email"].lower(), []).append(s)
    own = (org["email_address"] or "").lower()

    def norm_subj(x: str) -> str:
        return re.sub(r"^(re|aw|fwd?):\s*", "", (x or "").strip(), flags=re.I).lower()

    for ib in inbound:
        res["seen"] += 1
        if not ib.key or db.n("INSERT OR IGNORE INTO processed_inbound (org_id,msg_key,created_at) VALUES (?,?,?)",
                              (org_id, ib.key, utils.now())) == 0:
            continue
        if ib.from_addr and ib.from_addr == own:
            continue
        rec_id = None
        if ib.is_bounce:
            for mid in ib.mentioned_ids:
                if mid in by_id:
                    rec_id = by_id[mid]
                    break
            if rec_id is None:
                for a in ib.bounced_rcpts:
                    if a in by_addr:
                        rec_id = by_addr[a][-1]["recipient_id"]
                        break
            if rec_id is None:
                continue
            rec = db.one("SELECT * FROM recipients WHERE id=?", (rec_id,))
            if ib.hard_bounce:
                apply_bounce(db, org_id, rec, "Delivery failure notice received")
                res["bounces"] += 1
            else:
                log_event(db, org_id, "soft_bounce", "Temporary delivery problem", rec["customer_id"], rec_id, rec["round_id"])
            continue
        for ref in [ib.in_reply_to, *ib.references]:
            if ref and ref in by_id:
                rec_id = by_id[ref]
                break
        if rec_id is None and ib.thread_id and ib.thread_id in by_thread:
            rec_id = by_thread[ib.thread_id]
        if rec_id is None and ib.from_addr in by_addr:
            rec_id = by_addr[ib.from_addr][-1]["recipient_id"]
        if rec_id is None and ib.from_addr and "@" in ib.from_addr:
            dom = ib.from_addr.split("@")[1]
            for addr, lst in by_addr.items():
                if addr.split("@")[1] == dom and any(norm_subj(ib.subject) == norm_subj(s["subject"]) for s in lst) \
                        and ib.subject.lower().startswith(("re:", "aw:")):
                    rec_id = lst[-1]["recipient_id"]
                    break
        if rec_id is None:
            continue
        rec = db.one("SELECT * FROM recipients WHERE id=?", (rec_id,))
        if ib.auto_reply:
            log_event(db, org_id, "auto_reply", ib.subject[:150], rec["customer_id"], rec_id, rec["round_id"])
            res["auto"] += 1
            continue
        if UNSUB_WORDS.search(ib.body[:300]) and len(ib.body) < 400:
            unsubscribe_customer(db, org_id, rec["customer_id"], "unsubscribed by reply")
            res["unsub"] += 1
            continue
        apply_reply(db, org_id, rec, ib.body[:300] or ib.subject)
        res["replies"] += 1
    db.x("UPDATE orgs SET last_reply_check=? WHERE id=?", (utils.to_s(started), org_id))
    return res


# ---------------------------------------------------------------- test email / approval
def send_test(db, org_id: int, cfg: dict, rnd, to_addr: str, transport=None) -> str:
    org = db.one("SELECT * FROM orgs WHERE id=?", (org_id,))
    t = transport or make_transport(db, org, cfg)
    if t is None:
        raise TransientError("Connect an email account first (Settings > Email).")
    sample = {"company_name": "Sample Company Ltd", "contact_name": "Alex Sample", "contact": "", "city": "Sample City",
              "country": "Sample Country", "website": "https://example.com", "domain": "example.com"}
    sample["contact_name"] = "Alex Sample"
    ctx = customer_ctx({**sample}, org)
    url = f"{cfg['BASE_URL'].rstrip('/')}/u/test"
    parts = [("", rnd["subject"], rnd["body"])]
    for i, b in enumerate(followup_bodies(rnd)):
        parts.append((f"[Follow-up {i + 1}] ", "Re: " + rnd["subject"], b))
    for tag, subj, body in parts:
        msg = OutMessage(from_addr=org["email_address"], from_name=org["sender_name"] or org["name"], to_addr=to_addr,
                         subject="[TEST] " + tag + render(subj, ctx), body=render(body, ctx) + footer(org, url),
                         unsub_url=url)
        mid, alt, thread = t.send(msg)
        db.x("INSERT INTO sent_emails (org_id,round_id,step,message_id,to_email,subject,status,is_test,sent_at) "
             "VALUES (?,?,?,?,?,?,?,?,?)", (org_id, rnd["id"], 0, mid, to_addr, msg.subject, "sent", 1, utils.now()))
    db.x("UPDATE rounds SET test_hash=? WHERE id=?", (content_hash(rnd), rnd["id"]))
    return f"Test email(s) sent to {to_addr}."


def approve_round(db, org_id: int, cfg: dict, round_id: int, user_id: int) -> tuple[bool, list[str]]:
    org = db.one("SELECT * FROM orgs WHERE id=?", (org_id,))
    rd = db.one("SELECT * FROM rounds WHERE id=? AND org_id=?", (round_id, org_id))
    errs: list[str] = []
    if not rd or rd["status"] != "Draft":
        return False, ["Only a Draft round can be approved."]
    errs += validate_content(rd["subject"], rd["body"], rd["followup_bodies"])
    if org["email_mode"] == "none" or (org["email_mode"] != "demo" and not org["email_address"]):
        errs.append("Connect an email account first (Settings > Email).")
    if not (org["postal_address"] or "").strip():
        errs.append("Add your postal address in Settings > Sending (required in every email by law).")
    if rd["test_hash"] != content_hash(rd):
        errs.append("Send a test email for the current text first.")
    if errs:
        return False, errs
    with db.tx():
        for r in db.q("SELECT r.*, c.excluded, c.email, c.email_status, c.id AS cid FROM recipients r "
                      "JOIN customers c ON c.id=r.customer_id WHERE r.round_id=? AND r.status='Pending'", (round_id,)):
            c = db.one("SELECT * FROM customers WHERE id=?", (r["cid"],))
            b = _blocked(db, org_id, r, c)
            if b:
                set_recipient_status(db, r["id"], b[0], b[1])
        n = db.one("SELECT COUNT(*) c FROM recipients WHERE round_id=? AND status='Pending'", (round_id,))["c"]
        if n == 0:
            return False, ["No customers left to email in this round."]
        db.x("UPDATE rounds SET status='Approved', approved_by=?, approved_at=?, frozen_hash=?, "
             "start_date=COALESCE(start_date, ?) WHERE id=?",
             (user_id, utils.now(), content_hash(rd), utils.local_today_str(org["timezone"]), round_id))
        log_event(db, org_id, "round_approved", f"Round {rd['number']} approved: {n} customers", None, None, round_id)
    return True, []


# ---------------------------------------------------------------- analytics
STATUSES = ["Pending", "Sent", "Replied", "Ignored", "Bounced", "Unsubscribed", "Skipped"]


def round_stats(db, round_id: int) -> dict:
    cnt = {s: 0 for s in STATUSES}
    for r in db.q("SELECT status, COUNT(*) c FROM recipients WHERE round_id=? GROUP BY status", (round_id,)):
        cnt[r["status"]] = r["c"]
    total = sum(cnt.values())
    contacted = db.one("SELECT COUNT(*) c FROM recipients WHERE round_id=? AND steps_sent>=1", (round_id,))["c"]
    selected = total - cnt["Skipped"] if total else 0
    return {"selected": selected, "sent": contacted, "bounced": cnt["Bounced"], "replied": cnt["Replied"],
            "ignored": cnt["Ignored"], "unsubscribed": cnt["Unsubscribed"], "pending": cnt["Pending"],
            "in_progress": cnt["Sent"], "skipped": cnt["Skipped"],
            "reply_rate": round(100.0 * cnt["Replied"] / contacted, 1) if contacted else 0.0}
