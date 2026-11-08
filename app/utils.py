"""Small shared helpers: clock, ids, text."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

FMT = "%Y-%m-%d %H:%M:%S"
_NOW_OVERRIDE: datetime | None = None


def set_now(dt: datetime | None) -> None:
    """Tests only: freeze the clock (naive UTC)."""
    global _NOW_OVERRIDE
    _NOW_OVERRIDE = dt


def now_dt() -> datetime:
    if _NOW_OVERRIDE is not None:
        return _NOW_OVERRIDE
    return datetime.now(timezone.utc).replace(tzinfo=None)


def now() -> str:
    return now_dt().strftime(FMT)


def to_s(d: datetime) -> str:
    return d.strftime(FMT)


def parse(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.strptime(s, FMT)


def get_tz(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except Exception:
        return ZoneInfo("UTC")


def local_now(tzname: str | None) -> datetime:
    """Aware datetime in the org timezone."""
    return now_dt().replace(tzinfo=timezone.utc).astimezone(get_tz(tzname))


def local_day_start_utc(tzname: str | None) -> str:
    """UTC string of today's 00:00 in the org timezone."""
    ln = local_now(tzname)
    start = ln.replace(hour=0, minute=0, second=0, microsecond=0)
    return to_s(start.astimezone(timezone.utc).replace(tzinfo=None))


def next_local_day_start_utc(tzname: str | None) -> datetime:
    ln = local_now(tzname)
    start = ln.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return start.astimezone(timezone.utc).replace(tzinfo=None)


def local_today_str(tzname: str | None) -> str:
    return local_now(tzname).strftime("%Y-%m-%d")


def fmt_local(s: str | None, tzname: str | None = "UTC", fmt: str = "%d %b %Y, %H:%M") -> str:
    d = parse(s)
    if not d:
        return "—"
    return d.replace(tzinfo=timezone.utc).astimezone(get_tz(tzname)).strftime(fmt)


def first_name(full: str | None) -> str:
    if not full:
        return ""
    return re.split(r"\s+", full.strip())[0]


def clip(s: str | None, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"
