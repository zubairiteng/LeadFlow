"""Find business emails on a company website (home, contact, about pages)."""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .domains import EMAIL_RE, email_domain, is_free_mail
from .fetcher import FetchError, Fetcher

FIND_RE = re.compile(r"[A-Za-z0-9._%+\-']{1,64}@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,24}")
BAD_TLDS = {"png", "jpg", "jpeg", "gif", "svg", "webp", "css", "js", "ico", "woff", "woff2", "pdf"}
BAD_DOMAINS = {"example.com", "domain.com", "email.com", "sentry.io", "wixpress.com", "yourdomain.com",
               "sentry-next.wixpress.com", "godaddy.com"}
PREFERRED = ["sales", "info", "contact", "hello", "enquiry", "inquiry", "export", "office"]
PATHS = ["/", "/contact", "/contact-us", "/contactus", "/about", "/about-us"]


def decode_cf(hexstr: str) -> str:
    """Decode Cloudflare email protection (data-cfemail)."""
    try:
        key = int(hexstr[:2], 16)
        return "".join(chr(int(hexstr[i:i + 2], 16) ^ key) for i in range(2, len(hexstr), 2))
    except ValueError:
        return ""


def extract_emails(html: str) -> list[str]:
    found: list[str] = []
    soup = BeautifulSoup(html or "", "lxml")
    for a in soup.select("a[href^=mailto]"):
        addr = a["href"][7:].split("?")[0].strip()
        found.extend(FIND_RE.findall(addr))
    for el in soup.select("[data-cfemail]"):
        dec = decode_cf(el.get("data-cfemail", ""))
        if dec:
            found.append(dec)
    text = soup.get_text(" ")
    text = re.sub(r"\s*[\[(]\s*at\s*[\])]\s*", "@", text, flags=re.I)
    found.extend(FIND_RE.findall(text))
    out, seen = [], set()
    for e in found:
        e = e.lower().strip(".")
        if e in seen or not EMAIL_RE.match(e):
            continue
        tld = e.rsplit(".", 1)[-1]
        if tld in BAD_TLDS or email_domain(e) in BAD_DOMAINS:
            continue
        seen.add(e)
        out.append(e)
    return out


def business_only(emails: list[str]) -> list[str]:
    return [e for e in emails if not is_free_mail(email_domain(e))]


def rank(emails: list[str], site_domain: str) -> list[str]:
    def key(e: str):
        local, dom = e.split("@", 1)
        same = 0 if (dom == site_domain or dom.endswith("." + site_domain)) else 1
        pref = PREFERRED.index(local) if local in PREFERRED else len(PREFERRED)
        return (same, pref, e)
    return sorted(emails, key=key)


def find_emails(domain: str, fetcher: Fetcher, max_pages: int = 5) -> list[str]:
    """Return ranked business emails found on the site (may be empty)."""
    base = f"https://{domain}"
    urls = [urljoin(base, p) for p in PATHS]
    collected: list[str] = []
    visited: set[str] = set()
    tried_http = False
    i = 0
    while i < len(urls) and len(visited) < max_pages:
        url = urls[i]
        i += 1
        if url in visited:
            continue
        visited.add(url)
        try:
            html = fetcher.get(url)
        except FetchError:
            if i == 1 and not tried_http:  # home failed on https: try http once
                tried_http = True
                urls.insert(i, "http://" + domain + "/")
            continue
        collected.extend(extract_emails(html))
        if url.rstrip("/") == base or url.rstrip("/") == "http://" + domain:
            for a in BeautifulSoup(html, "lxml").select("a[href]"):
                href = a["href"]
                if re.search(r"contact|about", href, re.I):
                    full = urljoin(url, href)
                    if urlparse(full).hostname and (urlparse(full).hostname or "").lstrip("www.") == domain:
                        if full not in urls:
                            urls.insert(i, full)
        if len(set(business_only(collected))) >= 3:
            break
    return rank(list(dict.fromkeys(business_only(collected))), domain)
