"""Fixed-rule lead scoring (0-100). AI never decides the score."""
from __future__ import annotations

import re

HOT = 75
WARM = 50


def grade_for(score: int) -> str:
    return "Hot" if score >= HOT else "Warm" if score >= WARM else "Cold"


def _tokens(s: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", (s or "").lower()) if len(t) >= 3]


def _same_country(a: str, b: str) -> bool:
    a, b = (a or "").strip().lower(), (b or "").strip().lower()
    if not a or not b:
        return False
    alias = {"usa": "united states", "us": "united states", "u.s.": "united states",
             "u.s.a.": "united states", "uk": "united kingdom", "gb": "united kingdom",
             "uae": "united arab emirates", "pk": "pakistan"}
    return alias.get(a, a) == alias.get(b, b)


def score_customer(c, company_type: str = "", country: str = "") -> tuple[int, str, dict]:
    """c: mapping with company_name, description, domain, phone, email_status, country."""
    parts: dict[str, int] = {}
    text = " ".join([c["company_name"] or "", c["description"] or "", c["domain"] or ""]).lower()
    toks = _tokens(company_type)
    if toks:
        parts["Company type match"] = 30 if any(t in text for t in toks) else 0
    else:
        parts["Company type match"] = 15  # nothing to match against: neutral half credit
    parts["Website"] = 15 if c["domain"] else 0
    parts["Phone"] = 15 if (c["phone"] or "").strip() else 0
    st = c["email_status"]
    parts["Usable email"] = 30 if st in ("Unverified", "Verified") else 15 if st == "Risky" else 0
    if country:
        parts["Country"] = 10 if _same_country(c["country"], country) else 0
    else:
        parts["Country"] = 10 if (c["country"] or "").strip() else 0
    total = max(0, min(100, sum(parts.values())))
    return total, grade_for(total), parts
