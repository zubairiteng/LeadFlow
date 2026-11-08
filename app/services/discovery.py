"""Discovery Bot: connectors + resumable job runner.

Connectors are polite (robots.txt, delays) and fail independently: if one is blocked the bot moves
to the next. Google Search/Maps scraping is deliberately NOT included (it violates Google's terms).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

from bs4 import BeautifulSoup

from .. import utils
from . import customers as cust
from .domains import clean_domain, email_domain, is_directory_domain, is_free_mail, normalize_email
from .fetcher import FetchError, Fetcher

NOMINATIM = "https://nominatim.openstreetmap.org/search"
OVERPASS = "https://overpass-api.de/api/interpreter"


@dataclass
class Company:
    name: str
    website: str = ""
    email: str = ""
    phone: str = ""
    country: str = ""
    city: str = ""
    description: str = ""
    source: str = ""


class Connector:
    name = "base"
    max_pages = 1

    def applies(self, job) -> bool:
        return True

    def fetch_page(self, job, page: int, fetcher: Fetcher) -> list[Company] | None:
        """Return companies for this page, or None when there are no more pages."""
        raise NotImplementedError


# --------------------------------------------------------------- OpenStreetMap
class OpenStreetMap(Connector):
    name = "OpenStreetMap"
    max_pages = 1

    def fetch_page(self, job, page, fetcher):
        place = ", ".join(p for p in (job["city"], job["country"]) if p)
        if not place:
            return None
        geo = fetcher.get(NOMINATIM, robots=False, as_json=True,
                          params={"q": place, "format": "jsonv2", "limit": 1},
                          headers={"Accept": "application/json"})
        if not geo:
            return None
        g = geo[0]
        if g.get("osm_type") == "relation":
            area = f"area({3600000000 + int(g['osm_id'])})->.a;"
            scope = "(area.a)"
        elif g.get("osm_type") == "way":
            area = f"area({2400000000 + int(g['osm_id'])})->.a;"
            scope = "(area.a)"
        else:
            s, n, w, e = g["boundingbox"]
            area, scope = "", f"({s},{w},{n},{e})"
        kw = re.escape(job["keyword"]).replace('"', "")
        ql = (f'[out:json][timeout:60];{area}'
              f'(nwr{scope}[~"^(name|shop|craft|office|industrial|product|description)$"~"{kw}",i]'
              f'[~"^(website|contact:website|url|email|contact:email)$"~"."];);out tags center 300;')
        data = fetcher.get(OVERPASS, robots=False, as_json=True, method="POST", data={"data": ql})
        out: list[Company] = []
        for el in data.get("elements", []):
            t = el.get("tags", {})
            name = t.get("name")
            if not name:
                continue
            site = t.get("website") or t.get("contact:website") or t.get("url") or ""
            email = t.get("email") or t.get("contact:email") or ""
            if not site and not email:
                continue  # cannot de-duplicate or contact a company with neither
            out.append(Company(
                name=name, website=site, email=email.split(";")[0],
                phone=(t.get("phone") or t.get("contact:phone") or "").split(";")[0],
                country=job["country"], city=t.get("addr:city") or job["city"],
                description=" ".join(str(t.get(k, "")) for k in ("shop", "craft", "office", "industrial", "description")),
                source=self.name))
        return out


# ------------------------------------------------------------- Yellow Pages (US)
class YellowPagesUS(Connector):
    name = "Yellow Pages (US)"
    max_pages = 10

    def applies(self, job):
        return (job["country"] or "").strip().lower() in ("us", "usa", "united states", "u.s.", "u.s.a.")

    def fetch_page(self, job, page, fetcher):
        url = (f"https://www.yellowpages.com/search?search_terms={quote_plus(job['keyword'])}"
               f"&geo_location_terms={quote_plus(job['city'] or 'USA')}&page={page + 1}")
        soup = BeautifulSoup(fetcher.get(url), "lxml")
        cards = soup.select("div.result, div.v-card")
        if not cards:
            return None
        out = []
        for c in cards:
            n = c.select_one("a.business-name")
            if not n:
                continue
            site = c.select_one("a.track-visit-website")
            ph = c.select_one("div.phones, .phone")
            cat = c.select_one(".categories")
            out.append(Company(
                name=n.get_text(" ", strip=True), website=site["href"] if site and site.get("href") else "",
                phone=ph.get_text(strip=True) if ph else "", country=job["country"], city=job["city"],
                description=cat.get_text(" ", strip=True) if cat else "", source=self.name))
        return out


# ------------------------------------------------------ Web search + website check
class WebSearch(Connector):
    name = "Web search + website check"
    max_pages = 5

    def fetch_page(self, job, page, fetcher):
        q = " ".join(x for x in (job["keyword"], job["company_type"], job["city"], job["country"]) if x)
        url = f"https://html.duckduckgo.com/html/?q={quote_plus(q)}&s={page * 30}"
        soup = BeautifulSoup(fetcher.get(url), "lxml")
        links = soup.select("a.result__a")
        if not links:
            return None
        out, seen = [], set()
        for a in links:
            href = a.get("href", "")
            if "uddg=" in href:
                href = unquote(parse_qs(urlparse(href).query).get("uddg", [""])[0])
            d = clean_domain(href)
            if not d or d in seen or is_directory_domain(d):
                continue
            seen.add(d)
            try:  # website check: must load, gives us the real company name
                html = fetcher.get(f"https://{d}/")
            except FetchError:
                continue
            s2 = BeautifulSoup(html, "lxml")
            og = s2.find("meta", property="og:site_name")
            title = (og["content"] if og and og.get("content") else (s2.title.get_text() if s2.title else d))
            name = re.split(r"\s[|\-–—•]\s", title.strip())[0][:120] or d
            desc = s2.find("meta", attrs={"name": "description"})
            out.append(Company(name=name, website=d, country=job["country"], city=job["city"],
                               description=(desc["content"] if desc and desc.get("content") else "")[:300],
                               source=self.name))
        return out


def default_connectors() -> list[Connector]:
    return [OpenStreetMap(), YellowPagesUS(), WebSearch()]


# --------------------------------------------------------------------- runner
def _log(db, job_id: int, msg: str) -> None:
    row = db.one("SELECT log FROM search_jobs WHERE id=?", (job_id,))
    lines = ((row["log"] if row else "") or "").splitlines()[-60:]
    lines.append(f"{utils.now()} {msg}")
    db.x("UPDATE search_jobs SET log=? WHERE id=?", ("\n".join(lines), job_id))


def next_queued(db):
    return db.one("SELECT * FROM search_jobs WHERE status='Queued' ORDER BY id LIMIT 1")


def recover_stuck(db) -> None:
    """After a crash/restart a Running job goes back to Queued and resumes from its saved cursor."""
    db.x("UPDATE search_jobs SET status='Queued' WHERE status='Running'")


def run_job(db, job_id: int, fetcher: Fetcher, connectors: list[Connector] | None = None) -> str:
    connectors = connectors if connectors is not None else default_connectors()
    job = db.one("SELECT * FROM search_jobs WHERE id=?", (job_id,))
    if not job or job["status"] not in ("Queued", "Running"):
        return job["status"] if job else "Missing"
    org_id, camp = job["org_id"], job["campaign_id"]
    db.x("UPDATE search_jobs SET status='Running', started_at=COALESCE(started_at,?) WHERE id=?",
         (utils.now(), job_id))
    prog = json.loads(job["progress"] or "{}")
    start_ci, start_page = prog.get("connector", 0), prog.get("page", 0)
    active = [c for c in connectors if c.applies(job)]
    _log(db, job_id, "Connectors: " + ", ".join(c.name for c in active))

    def reached() -> bool:
        return db.one("SELECT found FROM search_jobs WHERE id=?", (job_id,))["found"] >= job["target"]

    for ci in range(start_ci, len(active)):
        conn = active[ci]
        first = start_page if ci == start_ci else 0
        for page in range(first, conn.max_pages):
            cur = db.one("SELECT status FROM search_jobs WHERE id=?", (job_id,))
            if cur["status"] == "Cancelled":
                return "Cancelled"
            try:
                companies = conn.fetch_page(job, page, fetcher)
            except Exception as e:  # blocked / site changed / network: next connector
                _log(db, job_id, f"{conn.name}: skipped ({type(e).__name__}: {e})")
                break
            if not companies:
                break
            for co in companies:
                if reached():
                    break
                st, _, _ = cust.add_customer(
                    db, org_id, camp, company_name=co.name, website=co.website, email=co.email,
                    phone=co.phone, country=co.country, city=co.city, description=co.description,
                    source=co.source, allow_free_email=False)
                if st == "added":
                    db.x("UPDATE search_jobs SET found=found+1 WHERE id=?", (job_id,))
                elif st == "duplicate":
                    db.x("UPDATE search_jobs SET duplicates=duplicates+1 WHERE id=?", (job_id,))
                elif st == "excluded":
                    db.x("UPDATE search_jobs SET excluded_skipped=excluded_skipped+1 WHERE id=?", (job_id,))
            db.x("UPDATE search_jobs SET progress=? WHERE id=?",
                 (json.dumps({"connector": ci, "page": page + 1}), job_id))
            if reached():
                break
        if reached():
            break
        db.x("UPDATE search_jobs SET progress=? WHERE id=?", (json.dumps({"connector": ci + 1, "page": 0}), job_id))

    row = db.one("SELECT found, target FROM search_jobs WHERE id=?", (job_id,))
    if row["found"] >= row["target"]:
        status, msg = "Completed", f"Found {row['found']} new customers."
    else:
        status = "Completed with Shortfall"
        msg = (f"Found {row['found']} of {row['target']}. All available sources were exhausted or blocked; "
               f"no fake leads were created. Try a broader keyword/city, add customers manually, or import a CSV.")
    db.x("UPDATE search_jobs SET status=?, message=?, finished_at=? WHERE id=?",
         (status, msg, utils.now(), job_id))
    _log(db, job_id, msg)
    return status
