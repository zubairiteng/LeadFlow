"""Compose outgoing messages and parse incoming ones."""
from __future__ import annotations

import email
import re
from dataclasses import dataclass, field
from email import policy
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parseaddr, parsedate_to_datetime

ALLOWED_VARS = {"company_name", "contact_name", "first_name", "city", "country", "website", "domain", "sender_name"}
VAR_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def unknown_vars(text: str) -> set[str]:
    return {m for m in VAR_RE.findall(text or "") if m not in ALLOWED_VARS}


def render(text: str, ctx: dict) -> str:
    return VAR_RE.sub(lambda m: str(ctx.get(m.group(1), "") or ""), text or "")


def customer_ctx(c, org) -> dict:
    fn = (c["contact_name"] or "").split()[0] if (c["contact_name"] or "").strip() else ""
    return {"company_name": c["company_name"], "contact_name": c["contact_name"] or c["company_name"],
            "first_name": fn or "there", "city": c["city"], "country": c["country"],
            "website": c["website"], "domain": c["domain"], "sender_name": org["sender_name"] or org["name"]}


def footer(org, unsub_url: str) -> str:
    return ("\n\n--\n" + (org["sender_name"] or org["name"]) + "\n" + (org["postal_address"] or "").strip()
            + "\n\nIf you do not want to hear from us, unsubscribe here: " + unsub_url + "\n")


@dataclass
class OutMessage:
    from_addr: str
    from_name: str
    to_addr: str
    subject: str
    body: str
    unsub_url: str = ""
    in_reply_to: str = ""
    thread_id: str = ""
    message_id: str = ""

    def build(self) -> EmailMessage:
        m = EmailMessage()
        m["From"] = formataddr((self.from_name, self.from_addr)) if self.from_name else self.from_addr
        m["To"] = self.to_addr
        m["Subject"] = self.subject
        dom = self.from_addr.split("@")[-1] or "localhost"
        self.message_id = self.message_id or make_msgid(domain=dom)
        m["Message-ID"] = self.message_id
        if self.in_reply_to:
            m["In-Reply-To"] = self.in_reply_to
            m["References"] = self.in_reply_to
        if self.unsub_url:
            m["List-Unsubscribe"] = f"<{self.unsub_url}>"
            m["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
        m.set_content(self.body)
        return m


# ------------------------------------------------------------------ inbound
@dataclass
class Inbound:
    key: str
    from_addr: str = ""
    subject: str = ""
    date: str = ""
    in_reply_to: str = ""
    references: list[str] = field(default_factory=list)
    thread_id: str = ""
    body: str = ""
    auto_reply: bool = False
    is_bounce: bool = False
    hard_bounce: bool = True
    bounced_rcpts: list[str] = field(default_factory=list)
    mentioned_ids: set[str] = field(default_factory=set)


AUTO_SUBJECT = re.compile(r"(out of (the )?office|automatic reply|auto[- ]?reply|autoreply|away from (my )?(desk|office)|"
                          r"on vacation|vacation reply)", re.I)
ID_RE = re.compile(r"<[^<>\s]{1,200}@[^<>\s]{1,200}>")


def _text_of(msg) -> str:
    try:
        part = msg.get_body(preferencelist=("plain", "html"))
        if part is not None:
            txt = part.get_content()
            if part.get_content_type() == "text/html":
                txt = re.sub(r"<[^>]+>", " ", txt)
            return re.sub(r"\s+", " ", txt).strip()[:4000]
    except Exception:
        pass
    return ""


def parse_inbound(raw: bytes, thread_id: str = "") -> Inbound:
    msg = email.message_from_bytes(raw, policy=policy.default)
    mid = (msg["Message-ID"] or "").strip()
    frm = parseaddr(str(msg["From"] or ""))[1].lower()
    ib = Inbound(key=mid or f"raw:{hash(raw)}", from_addr=frm, subject=str(msg["Subject"] or ""),
                 in_reply_to=(msg["In-Reply-To"] or "").strip(), thread_id=thread_id)
    ib.references = ID_RE.findall(str(msg["References"] or ""))
    try:
        ib.date = parsedate_to_datetime(str(msg["Date"])).isoformat()
    except Exception:
        ib.date = ""
    ib.body = _text_of(msg)
    ctype = msg.get_content_type()
    local = frm.split("@")[0]
    dsn_parts = [p for p in msg.walk() if p.get_content_type() == "message/delivery-status"]
    if dsn_parts or ctype == "multipart/report" or local in ("mailer-daemon", "postmaster"):
        ib.is_bounce = True
        for p in dsn_parts:
            blocks = p.get_payload()
            for blk in (blocks if isinstance(blocks, list) else [blocks]):
                try:
                    rc = str(blk.get("Final-Recipient", "") or "")
                    if rc:
                        ib.bounced_rcpts.append(rc.split(";")[-1].strip().lower())
                    status = str(blk.get("Status", "") or "")
                    if status.startswith("4") or str(blk.get("Action", "")).lower() == "delayed":
                        ib.hard_bounce = False
                except Exception:
                    continue
        if not dsn_parts:
            ib.bounced_rcpts = [a.lower() for a in re.findall(r"[\w.+\-']+@[\w\-]+\.[\w.\-]+", ib.body)][:3]
        full = raw.decode("utf-8", errors="ignore")
        ib.mentioned_ids = set(ID_RE.findall(full))
        return ib
    auto = (str(msg["Auto-Submitted"] or "no").lower() != "no" or msg["X-Autoreply"] is not None
            or msg["X-Autorespond"] is not None
            or str(msg["Precedence"] or "").lower() in ("auto_reply", "auto-reply")
            or bool(AUTO_SUBJECT.search(ib.subject)))
    ib.auto_reply = auto
    return ib
