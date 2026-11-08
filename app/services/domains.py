"""Domain / email normalisation helpers."""
from __future__ import annotations

import re
from urllib.parse import urlparse

_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
DOMAIN_RE = re.compile(rf"^(?:{_LABEL}\.)+[a-z]{{2,24}}$")
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-']{1,64}@([A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,24})$")

FREE_MAIL = {
    "gmail.com", "googlemail.com", "outlook.com", "live.com", "msn.com", "aol.com", "icloud.com",
    "me.com", "mac.com", "proton.me", "protonmail.com", "pm.me", "mail.ru", "yandex.com",
    "yandex.ru", "qq.com", "163.com", "126.com", "rediffmail.com", "gmx.com", "gmx.net", "gmx.de",
    "web.de", "zoho.com", "mail.com", "inbox.com", "fastmail.com", "tutanota.com",
}
_FREE_PATTERNS = (re.compile(r"^yahoo\.[a-z.]+$"), re.compile(r"^hotmail\.[a-z.]+$"),
                  re.compile(r"^outlook\.[a-z.]+$"), re.compile(r"^live\.[a-z.]+$"))

# Domains that are directories/social/search sites, never a prospect's own website.
NOT_A_COMPANY = {
    "facebook.com", "linkedin.com", "instagram.com", "twitter.com", "x.com", "youtube.com",
    "pinterest.com", "tiktok.com", "reddit.com", "quora.com", "wikipedia.org", "wikimedia.org",
    "yelp.com", "yellowpages.com", "yell.com", "mapquest.com", "bbb.org", "crunchbase.com",
    "glassdoor.com", "indeed.com", "tripadvisor.com", "alibaba.com", "aliexpress.com",
    "amazon.com", "ebay.com", "indiamart.com", "made-in-china.com", "tradeindia.com",
    "google.com", "bing.com", "duckduckgo.com", "yahoo.com", "apple.com", "microsoft.com",
    "medium.com", "blogspot.com", "wordpress.com", "github.com", "bloomberg.com", "forbes.com",
    "thomasnet.com", "dnb.com", "zoominfo.com", "manta.com", "foursquare.com", "trustpilot.com",
    "europages.com", "kompass.com", "globalspec.com", "gov.uk", "usa.gov",
}


def clean_domain(value: str | None) -> str:
    """'https://www.Example.com/path?x=1' -> 'example.com'. Returns '' if not a valid host."""
    if not value:
        return ""
    v = value.strip().lower()
    if not v or " " in v or "@" in v.split("/")[0]:
        return ""
    if "://" not in v:
        v = "http://" + v
    try:
        host = urlparse(v).hostname or ""
    except ValueError:
        return ""
    host = host.strip(".")
    if host.startswith("www."):
        host = host[4:]
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return ""
    return host if DOMAIN_RE.match(host) else ""


def normalize_email(value: str | None) -> str:
    v = (value or "").strip().strip("<>").lower()
    return v if EMAIL_RE.match(v) else ""


def email_domain(email: str) -> str:
    return email.rsplit("@", 1)[-1].lower() if "@" in email else ""


def is_free_mail(domain: str) -> bool:
    d = (domain or "").lower()
    return d in FREE_MAIL or any(p.match(d) for p in _FREE_PATTERNS)


def is_directory_domain(domain: str) -> bool:
    d = (domain or "").lower()
    return any(d == x or d.endswith("." + x) for x in NOT_A_COMPANY)


def website_url(domain_or_url: str | None) -> str:
    d = clean_domain(domain_or_url)
    return f"https://{d}" if d else ""


def dedup_key(domain: str, email: str) -> str:
    """Business domain if we have one, otherwise the email itself."""
    if domain:
        return domain
    if email:
        return "mail:" + email
    return ""
