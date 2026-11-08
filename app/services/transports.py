"""Sending/reading mailboxes: Demo (no real mail), Gmail API (OAuth), generic SMTP+IMAP."""
from __future__ import annotations

import base64
import imaplib
import json
import smtplib
import ssl
from datetime import datetime, timedelta

import requests

from .. import utils
from . import crypto
from .mailing import Inbound, OutMessage, parse_inbound

GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.send", "https://www.googleapis.com/auth/gmail.readonly"]


class SendError(Exception):
    """This recipient cannot be emailed (permanent)."""


class SendLimitError(Exception):
    """Provider says daily limit reached: stop for today."""


class TransientError(Exception):
    """Temporary problem (network etc.): retry later."""


class AuthError(Exception):
    """Credentials rejected: needs reconnect."""


class Transport:
    mode = "base"

    def send(self, m: OutMessage) -> tuple[str, str, str]:
        """Returns (message_id, alt_message_id, thread_id)."""
        raise NotImplementedError

    def fetch_inbound(self, since: datetime) -> list[Inbound]:
        raise NotImplementedError

    def test(self) -> str:
        raise NotImplementedError


# -------------------------------------------------------------------- demo
class DemoTransport(Transport):
    """Nothing leaves the machine. Replies/bounces are injected from the UI for demonstrations."""
    mode = "demo"

    def __init__(self, db, org):
        self.db, self.org = db, org

    def send(self, m):
        m.build()
        return m.message_id, "", ""

    def fetch_inbound(self, since):
        rows = self.db.q("SELECT id, raw FROM demo_inbox WHERE org_id=? ORDER BY id", (self.org["id"],))
        out = [parse_inbound(bytes(r["raw"])) for r in rows]
        if rows:
            self.db.x("DELETE FROM demo_inbox WHERE id<=?", (rows[-1]["id"],))
        return out

    def test(self):
        return "Demo mode is active: no real email is sent."


# --------------------------------------------------------------- SMTP + IMAP
class SmtpImapTransport(Transport):
    mode = "smtp"

    def __init__(self, host, port, user, password, imap_host, imap_port=993, from_addr=""):
        self.host, self.port, self.user, self.password = host, int(port), user, password
        self.imap_host, self.imap_port = imap_host, int(imap_port or 993)
        self.from_addr = from_addr or user

    def _smtp(self):
        ctx = ssl.create_default_context()
        if self.port == 465:
            s = smtplib.SMTP_SSL(self.host, self.port, timeout=30, context=ctx)
        else:
            s = smtplib.SMTP(self.host, self.port, timeout=30)
            s.ehlo()
            if self.port != 25 and s.has_extn("starttls"):
                s.starttls(context=ctx)
                s.ehlo()
        if self.user:
            s.login(self.user, self.password)
        return s

    def send(self, m):
        msg = m.build()
        try:
            s = self._smtp()
            try:
                s.send_message(msg)
            finally:
                try:
                    s.quit()
                except Exception:
                    pass
        except smtplib.SMTPAuthenticationError as e:
            raise AuthError(f"SMTP login rejected: {e.smtp_error!r}") from e
        except smtplib.SMTPRecipientsRefused as e:
            raise SendError(str(e)) from e
        except smtplib.SMTPResponseException as e:
            text = (e.smtp_error or b"").decode("utf-8", "ignore").lower()
            if any(w in text for w in ("daily", "quota", "limit exceeded", "sending limit", "too many")):
                raise SendLimitError(text[:200]) from e
            if 500 <= e.smtp_code < 600:
                raise SendError(f"{e.smtp_code} {text[:150]}") from e
            raise TransientError(f"{e.smtp_code} {text[:150]}") from e
        except (smtplib.SMTPException, OSError) as e:
            raise TransientError(str(e)) from e
        return m.message_id, "", ""

    def fetch_inbound(self, since):
        out: list[Inbound] = []
        try:
            im = imaplib.IMAP4_SSL(self.imap_host, self.imap_port, timeout=30)
        except Exception as e:
            raise TransientError(f"IMAP connect failed: {e}") from e
        try:
            im.login(self.user, self.password)
            im.select("INBOX", readonly=True)
            typ, data = im.search(None, "SINCE", since.strftime("%d-%b-%Y"))
            ids = (data[0].split() if typ == "OK" and data and data[0] else [])[-300:]
            for i in ids:
                typ, parts = im.fetch(i, "(BODY.PEEK[])")
                if typ == "OK" and parts and isinstance(parts[0], tuple):
                    out.append(parse_inbound(parts[0][1]))
        except imaplib.IMAP4.error as e:
            raise AuthError(f"IMAP error: {e}") from e
        finally:
            try:
                im.logout()
            except Exception:
                pass
        return out

    def test(self):
        s = self._smtp()
        s.quit()
        if self.imap_host:
            im = imaplib.IMAP4_SSL(self.imap_host, self.imap_port, timeout=20)
            try:
                im.login(self.user, self.password)
            finally:
                try:
                    im.logout()
                except Exception:
                    pass
        return "SMTP login OK" + (" and IMAP login OK." if self.imap_host else " (IMAP not set: replies will not be read).")


# ------------------------------------------------------------------ Gmail API
class GmailApiTransport(Transport):
    mode = "gmail"

    def __init__(self, db, org, client_id: str, client_secret: str):
        self.db, self.org = db, org
        self.cid, self.secret = client_id, client_secret

    def _token(self) -> str:
        org = self.db.one("SELECT * FROM orgs WHERE id=?", (self.org["id"],))
        access = crypto.decrypt(org["gmail_access_enc"])
        exp = utils.parse(org["gmail_expiry"])
        if access and exp and exp > utils.now_dt() + timedelta(seconds=60):
            return access
        refresh = crypto.decrypt(org["gmail_refresh_enc"])
        if not refresh:
            raise AuthError("Gmail is not connected")
        r = requests.post(GOOGLE_TOKEN, data={"client_id": self.cid, "client_secret": self.secret,
                                              "refresh_token": refresh, "grant_type": "refresh_token"}, timeout=30)
        if r.status_code in (400, 401):
            raise AuthError("Google rejected the saved login. Please reconnect Gmail.")
        if r.status_code != 200:
            raise TransientError(f"Google token error {r.status_code}")
        j = r.json()
        self.db.x("UPDATE orgs SET gmail_access_enc=?, gmail_expiry=? WHERE id=?",
                  (crypto.encrypt(j["access_token"]),
                   utils.to_s(utils.now_dt() + timedelta(seconds=int(j.get("expires_in", 3600)))), org["id"]))
        return j["access_token"]

    def _call(self, method, path, **kw):
        h = {"Authorization": f"Bearer {self._token()}"}
        try:
            r = requests.request(method, GMAIL + path, headers=h, timeout=30, **kw)
        except requests.RequestException as e:
            raise TransientError(str(e)) from e
        if r.status_code == 401:
            raise AuthError("Gmail login expired. Please reconnect.")
        if r.status_code == 429 or (r.status_code == 403 and any(
                w in r.text.lower() for w in ("ratelimit", "quota", "limit exceeded", "sending limit"))):
            raise SendLimitError(r.text[:200])
        if r.status_code in (400, 404) and "recipient" in r.text.lower():
            raise SendError(r.text[:200])
        if r.status_code >= 500:
            raise TransientError(f"Gmail {r.status_code}")
        if r.status_code >= 400:
            raise TransientError(f"Gmail {r.status_code}: {r.text[:150]}")
        return r.json()

    def send(self, m):
        msg = m.build()
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        body = {"raw": raw}
        if m.thread_id:
            body["threadId"] = m.thread_id
        res = self._call("POST", "/messages/send", json=body)
        alt = ""
        try:
            meta = self._call("GET", f"/messages/{res['id']}",
                              params={"format": "metadata", "metadataHeaders": "Message-ID"})
            for h in meta.get("payload", {}).get("headers", []):
                if h["name"].lower() == "message-id" and h["value"].strip() != m.message_id:
                    alt = h["value"].strip()
        except Exception:
            pass
        return m.message_id, alt, res.get("threadId", "")

    def fetch_inbound(self, since):
        after = int(since.replace(tzinfo=None).timestamp())
        ids, token = [], None
        for _ in range(3):
            params = {"q": f"in:inbox after:{after}", "maxResults": 100}
            if token:
                params["pageToken"] = token
            res = self._call("GET", "/messages", params=params)
            ids += [x["id"] for x in res.get("messages", [])]
            token = res.get("nextPageToken")
            if not token:
                break
        out = []
        for mid in ids:
            full = self._call("GET", f"/messages/{mid}", params={"format": "raw"})
            raw = base64.urlsafe_b64decode(full["raw"] + "=" * (-len(full["raw"]) % 4))
            out.append(parse_inbound(raw, thread_id=full.get("threadId", "")))
        return out

    def test(self):
        p = self._call("GET", "/profile")
        return f"Connected as {p.get('emailAddress')}"


def make_transport(db, org, cfg) -> Transport | None:
    mode = org["email_mode"]
    if mode == "demo":
        return DemoTransport(db, org)
    if mode == "smtp":
        return SmtpImapTransport(org["smtp_host"], org["smtp_port"], org["smtp_user"],
                                 crypto.decrypt(org["smtp_pass_enc"]), org["imap_host"], org["imap_port"],
                                 org["email_address"])
    if mode == "gmail" and cfg.get("GOOGLE_CLIENT_ID"):
        return GmailApiTransport(db, org, cfg["GOOGLE_CLIENT_ID"], cfg["GOOGLE_CLIENT_SECRET"])
    return None
