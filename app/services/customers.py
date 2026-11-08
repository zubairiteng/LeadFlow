"""Customer records: add (with duplicate/excluded checks), exclude/restore, enrich, rescore."""
from __future__ import annotations

import csv
import io
import json

from .. import utils
from . import emailcheck, scoring
from .domains import (clean_domain, dedup_key, email_domain, is_free_mail, normalize_email, website_url)
from .email_finder import find_emails


def _campaign_ctx(db, org_id: int, campaign_id: int | None) -> tuple[str, str]:
    """(company_type, country) used for scoring: taken from the campaign's latest search."""
    if not campaign_id:
        return "", ""
    r = db.one("SELECT company_type, country FROM search_jobs WHERE org_id=? AND campaign_id=? "
               "ORDER BY id DESC LIMIT 1", (org_id, campaign_id))
    return (r["company_type"], r["country"]) if r else ("", "")


def rescore(db, org_id: int, customer_id: int) -> None:
    c = db.one("SELECT * FROM customers WHERE id=? AND org_id=?", (customer_id, org_id))
    if not c:
        return
    ctype, country = _campaign_ctx(db, org_id, c["campaign_id"])
    s, g, _ = scoring.score_customer(c, ctype, country)
    db.x("UPDATE customers SET score=?, grade=?, updated_at=? WHERE id=?", (s, g, utils.now(), customer_id))


def is_dnc(db, org_id: int, email: str) -> bool:
    return bool(email) and db.one("SELECT 1 FROM dnc WHERE org_id=? AND email=?", (org_id, email.lower())) is not None


def add_customer(db, org_id: int, campaign_id: int | None, *, company_name: str = "", website: str = "",
                 email: str = "", phone: str = "", country: str = "", city: str = "",
                 contact_name: str = "", description: str = "", source: str = "Manual",
                 allow_free_email: bool = True) -> tuple[str, int | None, str]:
    """Returns (status, customer_id, message). status: added | duplicate | excluded | invalid."""
    domain = clean_domain(website)
    em = normalize_email(email)
    if em and is_free_mail(email_domain(em)) and not allow_free_email:
        em = ""
    if not domain and em and not is_free_mail(email_domain(em)):
        domain = email_domain(em)
    key = dedup_key(domain, em)
    if not key:
        return "invalid", None, "Need a valid website or email"
    name = (company_name or "").strip() or domain or em
    if db.one("SELECT 1 FROM excluded_domains WHERE org_id=? AND dedup_key=?", (org_id, key)):
        return "excluded", None, "On the Excluded list"
    if db.one("SELECT 1 FROM customers WHERE org_id=? AND dedup_key=?", (org_id, key)):
        return "duplicate", None, "Already saved"
    status, reason = ("None", "")
    if em:
        status, reason = emailcheck.check_email(em)
    enrich = "pending" if (domain and not em) else "done"
    now = utils.now()
    try:
        cid = db.x(
            "INSERT INTO customers (org_id,campaign_id,company_name,dedup_key,domain,website,email,email_status,"
            "phone,country,city,contact_name,description,source,enrich_state,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (org_id, campaign_id, name[:200], key, domain, website_url(domain) if domain else "", em, status,
             (phone or "").strip()[:60], (country or "").strip()[:80], (city or "").strip()[:80],
             (contact_name or "").strip()[:120], (description or "").strip()[:500], source,
             enrich, now, now))
    except Exception as e:  # unique race
        if "UNIQUE" in str(e):
            return "duplicate", None, "Already saved"
        raise
    rescore(db, org_id, cid)
    return "added", cid, reason


def exclude_customer(db, org_id: int, customer_id: int, user_id: int | None) -> bool:
    c = db.one("SELECT * FROM customers WHERE id=? AND org_id=?", (customer_id, org_id))
    if not c:
        return False
    with db.tx():
        db.x("INSERT OR IGNORE INTO excluded_domains (org_id,dedup_key,customer_id,company_name,created_at,created_by)"
             " VALUES (?,?,?,?,?,?)", (org_id, c["dedup_key"], customer_id, c["company_name"], utils.now(), user_id))
        db.x("UPDATE customers SET excluded=1, updated_at=? WHERE id=?", (utils.now(), customer_id))
        db.x("UPDATE recipients SET status='Skipped', note='Customer excluded', updated_at=? "
             "WHERE customer_id=? AND status='Pending'", (utils.now(), customer_id))
    return True


def restore_excluded(db, org_id: int, excluded_id: int) -> bool:
    ex = db.one("SELECT * FROM excluded_domains WHERE id=? AND org_id=?", (excluded_id, org_id))
    if not ex:
        return False
    with db.tx():
        db.x("DELETE FROM excluded_domains WHERE id=?", (excluded_id,))
        db.x("UPDATE customers SET excluded=0, updated_at=? WHERE org_id=? AND dedup_key=?",
             (utils.now(), org_id, ex["dedup_key"]))
    return True


def add_dnc(db, org_id: int, email: str, reason: str) -> None:
    email = normalize_email(email)
    if email:
        db.x("INSERT OR IGNORE INTO dnc (org_id,email,reason,created_at) VALUES (?,?,?,?)",
             (org_id, email, reason, utils.now()))


def enrich_pending(db, fetcher, limit: int = 10, org_id: int | None = None) -> int:
    """Step 4: read websites for emails. Returns number of customers processed."""
    sql = "SELECT * FROM customers WHERE enrich_state='pending' AND excluded=0"
    args: tuple = ()
    if org_id:
        sql += " AND org_id=?"
        args = (org_id,)
    rows = db.q(sql + " ORDER BY id LIMIT ?", args + (limit,))
    for c in rows:
        try:
            emails = find_emails(c["domain"], fetcher)
            if emails:
                best = emails[0]
                st, _ = emailcheck.check_email(best)
                db.x("UPDATE customers SET email=?, email_status=?, extra_emails=?, enrich_state='done', "
                     "updated_at=? WHERE id=?", (best, st, ", ".join(emails[1:5]), utils.now(), c["id"]))
            else:
                db.x("UPDATE customers SET enrich_state='done', updated_at=? WHERE id=?", (utils.now(), c["id"]))
            rescore(db, c["org_id"], c["id"])
        except Exception:
            db.x("UPDATE customers SET enrich_state='failed', updated_at=? WHERE id=?", (utils.now(), c["id"]))
    return len(rows)


# ---------------------------------------------------------------- bulk import
ALIASES = {
    "company_name": {"company", "company_name", "company name", "name", "business", "business name", "organization"},
    "website": {"website", "url", "domain", "web", "site", "website url"},
    "email": {"email", "e-mail", "email address", "mail"},
    "phone": {"phone", "tel", "telephone", "mobile", "phone number"},
    "country": {"country"},
    "city": {"city", "town"},
    "contact_name": {"contact", "contact_name", "contact name", "person", "full name"},
}
MAX_ROWS = 5000


def _map_headers(headers: list[str]) -> dict[int, str]:
    mapping: dict[int, str] = {}
    for i, h in enumerate(headers):
        hl = (h or "").strip().lower()
        for field, names in ALIASES.items():
            if hl in names and field not in mapping.values():
                mapping[i] = field
                break
    return mapping


def read_rows(filename: str, data: bytes) -> list[dict]:
    name = (filename or "").lower()
    rows: list[list] = []
    if name.endswith(".xlsx"):
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        ws = wb.active
        for r in ws.iter_rows(values_only=True):
            rows.append(["" if v is None else str(v) for v in r])
            if len(rows) > MAX_ROWS + 1:
                break
    elif name.endswith((".csv", ".txt")):
        text = data.decode("utf-8-sig", errors="replace")
        try:
            dialect = csv.Sniffer().sniff(text[:2048], delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        for r in csv.reader(io.StringIO(text), dialect):
            rows.append(r)
            if len(rows) > MAX_ROWS + 1:
                break
    else:
        raise ValueError("Upload a .csv or .xlsx file")
    if not rows:
        raise ValueError("File is empty")
    mapping = _map_headers(rows[0])
    if not mapping or not ({"website", "email"} & set(mapping.values())):
        raise ValueError("Need a header row with at least a 'website' or 'email' column")
    out = []
    for r in rows[1:MAX_ROWS + 1]:
        rec = {f: (r[i].strip() if i < len(r) and r[i] else "") for i, f in mapping.items()}
        if any(rec.values()):
            out.append(rec)
    return out


def import_rows(db, org_id: int, campaign_id: int, rows: list[dict], source: str = "Import") -> dict:
    res = {"added": 0, "duplicate": 0, "excluded": 0, "invalid": 0, "errors": []}
    for n, rec in enumerate(rows, start=2):
        st, _, msg = add_customer(db, org_id, campaign_id, source=source, **rec)
        res[st] += 1
        if st == "invalid" and len(res["errors"]) < 15:
            res["errors"].append(f"Row {n}: {msg}")
    return res
