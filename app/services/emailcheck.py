"""Email checking: format + mail server. Without a paid checker an address is never 'Verified'."""
from __future__ import annotations

import socket
import threading
from typing import Callable

from .domains import EMAIL_RE, email_domain, is_free_mail

MX_ENABLED = True
_cache: dict[str, bool | None] = {}
_lock = threading.Lock()

NOREPLY = {"noreply", "no-reply", "donotreply", "do-not-reply", "mailer-daemon", "postmaster", "bounce", "bounces"}
RISKY_LOCALS = {"webmaster", "abuse", "admin", "root", "privacy", "legal", "hr", "jobs", "careers",
                "press", "media", "security", "spam", "billing", "support", "helpdesk", "newsletter"}


def _default_has_mx(domain: str) -> bool | None:
    """True = domain can receive mail, False = it cannot, None = could not tell."""
    try:
        import dns.resolver  # type: ignore

        try:
            ans = dns.resolver.resolve(domain, "MX", lifetime=4)
            return any(str(r.exchange).strip(".") not in ("", ".") for r in ans)
        except dns.resolver.NoAnswer:
            pass  # fall through: an A record also accepts mail (RFC 5321 implicit MX)
        except dns.resolver.NXDOMAIN:
            return False
        except Exception:
            return None
    except ImportError:
        pass
    try:
        socket.getaddrinfo(domain, None)
        return True
    except socket.gaierror as e:
        return False if e.errno in (socket.EAI_NONAME, getattr(socket, "EAI_NODATA", -5)) else None
    except OSError:
        return None


_resolver: Callable[[str], bool | None] = _default_has_mx


def set_resolver(fn: Callable[[str], bool | None] | None) -> None:
    global _resolver
    _resolver = fn or _default_has_mx
    with _lock:
        _cache.clear()


def has_mx(domain: str) -> bool | None:
    with _lock:
        if domain in _cache:
            return _cache[domain]
    res = _resolver(domain)
    if res is not None:  # never cache "could not tell"
        with _lock:
            _cache[domain] = res
    return res


def check_email(addr: str) -> tuple[str, str]:
    """Return (status, reason). status in Unverified | Risky | Invalid."""
    addr = (addr or "").strip().lower()
    if not EMAIL_RE.match(addr):
        return "Invalid", "bad format"
    local = addr.split("@", 1)[0]
    domain = email_domain(addr)
    if local in NOREPLY:
        return "Invalid", "no-reply address"
    if ".." in addr or local.startswith(".") or local.endswith("."):
        return "Invalid", "bad format"
    if MX_ENABLED and not is_free_mail(domain):
        mx = has_mx(domain)
        if mx is False:
            return "Invalid", "domain has no mail server"
    if local in RISKY_LOCALS:
        return "Risky", "role address"
    return "Unverified", "format and mail server look fine"
