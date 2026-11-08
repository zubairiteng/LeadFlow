"""Polite HTTP client: robots.txt, per-host delay, SSRF protection, size limit."""
from __future__ import annotations

import ipaddress
import socket
import threading
import time
import urllib.robotparser
from urllib.parse import urljoin, urlparse

import requests

UA = "LeadGenBot/1.0 (business-contact research; respects robots.txt)"


class FetchError(Exception):
    pass


class Blocked(FetchError):
    """robots.txt disallows, or host is not public."""


class Fetcher:
    def __init__(self, delay: float = 3.0, timeout: float = 12, max_bytes: int = 1_500_000,
                 allow_private: bool = False, sleep=time.sleep, session=None):
        self.delay, self.timeout, self.max_bytes = delay, timeout, max_bytes
        self.allow_private = allow_private
        self.sleep = sleep
        self.s = session or requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept-Language": "en"})
        self._last: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._lock = threading.Lock()

    # -- safety -------------------------------------------------------
    def _check_public(self, host: str) -> None:
        if self.allow_private:
            return
        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror as e:
            raise FetchError(f"cannot resolve {host}") from e
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                    or ip.is_multicast or ip.is_unspecified):
                raise Blocked(f"{host} is not a public address")

    def _pace(self, host: str) -> None:
        with self._lock:
            last = self._last.get(host)
            wait = (last + self.delay - time.monotonic()) if last else 0
        if wait > 0:
            self.sleep(wait)
        with self._lock:
            self._last[host] = time.monotonic()

    def allowed(self, url: str) -> bool:
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        if base not in self._robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                self._check_public(p.hostname or "")
                r = self.s.get(base + "/robots.txt", timeout=self.timeout, allow_redirects=True)
                if r.status_code in (401, 403):
                    rp.disallow_all = True
                elif r.status_code >= 400:
                    rp.allow_all = True
                else:
                    rp.parse(r.text.splitlines())
            except Exception:
                rp = None  # robots unreachable: treat as allowed (standard practice)
            self._robots[base] = rp
        rp = self._robots[base]
        return True if rp is None else rp.can_fetch(UA, url)

    # -- fetching -----------------------------------------------------
    def get(self, url: str, *, robots: bool = True, params=None, headers=None, as_json: bool = False,
            method: str = "GET", data=None):
        """Return text (or parsed JSON). Raises FetchError / Blocked."""
        if urlparse(url).scheme not in ("http", "https"):
            raise FetchError("unsupported scheme")
        if robots and not self.allowed(url):
            raise Blocked(f"robots.txt disallows {url}")
        cur = url
        for _ in range(5):
            host = urlparse(cur).hostname or ""
            self._check_public(host)
            self._pace(host)
            try:
                r = self.s.request(method, cur, params=params if cur == url else None, data=data,
                                   headers=headers, timeout=self.timeout, allow_redirects=False, stream=True)
            except requests.RequestException as e:
                raise FetchError(str(e)) from e
            try:
                if r.is_redirect and r.headers.get("Location"):
                    cur = urljoin(cur, r.headers["Location"])
                    if urlparse(cur).scheme not in ("http", "https"):
                        raise FetchError("bad redirect")
                    method, data = "GET", None
                    continue
                if r.status_code >= 400:
                    raise FetchError(f"HTTP {r.status_code}")
                ctype = r.headers.get("Content-Type", "").lower()
                if not any(t in ctype for t in ("html", "json", "xml", "text")):
                    raise FetchError(f"unsupported content type {ctype}")
                raw = b""
                for chunk in r.iter_content(65536):
                    raw += chunk
                    if len(raw) > self.max_bytes:
                        break
                raw = raw[: self.max_bytes]
                text = raw.decode(r.encoding or "utf-8", errors="replace")
                if as_json:
                    import json
                    try:
                        return json.loads(text)
                    except ValueError as e:
                        raise FetchError("invalid JSON") from e
                return text
            finally:
                r.close()
        raise FetchError("too many redirects")
