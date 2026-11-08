"""Background workers (one thread per job type, so a long discovery run never blocks sending)."""
from __future__ import annotations

import logging
import threading
import time

from . import utils
from .db import DB
from .services import customers as cust
from .services import discovery, engine
from .services.fetcher import Fetcher

log = logging.getLogger("leadgen.scheduler")


def _each_org(db):
    return [r["id"] for r in db.q("SELECT id FROM orgs WHERE email_mode<>'none'")]


def job_discovery(db, cfg):
    """Every 10 min: start the oldest Queued job (one at a time)."""
    job = discovery.next_queued(db)
    if job:
        fetcher = Fetcher(delay=cfg["FETCH_DELAY"], allow_private=cfg.get("ALLOW_PRIVATE_HOSTS", False))
        discovery.run_job(db, job["id"], fetcher)


def job_enrich(db, cfg):
    fetcher = Fetcher(delay=min(cfg["FETCH_DELAY"], 2), allow_private=cfg.get("ALLOW_PRIVATE_HOSTS", False))
    cust.enrich_pending(db, fetcher, limit=10)


def job_send(db, cfg):
    for oid in _each_org(db):
        engine.send_tick(db, oid, cfg)


def job_replies(db, cfg):
    for oid in _each_org(db):
        engine.process_replies(db, oid, cfg)


def job_housekeeping(db, cfg):
    for r in db.q("SELECT id FROM orgs"):
        engine.mark_ignored_and_finish(db, r["id"])
    db.x("DELETE FROM rate_limits WHERE created_at < ?", (time.time() - 3600,))


JOBS = [  # name, interval seconds, function
    ("discovery", 600, job_discovery),
    ("enrich", 60, job_enrich),
    ("send", 20, job_send),
    ("replies", 600, job_replies),
    ("housekeeping", 900, job_housekeeping),
]


class Scheduler:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.stop_evt = threading.Event()
        self.threads: list[threading.Thread] = []

    def _loop(self, name, interval, fn):
        db = DB(self.cfg["DATABASE"])
        if name == "discovery":
            discovery.recover_stuck(db)
        self.stop_evt.wait(5)
        while not self.stop_evt.is_set():
            try:
                fn(db, self.cfg)
            except Exception:
                log.exception("job %s failed", name)
            self.stop_evt.wait(interval)
        db.close()

    def start(self):
        for name, interval, fn in JOBS:
            t = threading.Thread(target=self._loop, args=(name, interval, fn), name=f"job-{name}", daemon=True)
            t.start()
            self.threads.append(t)

    def stop(self):
        self.stop_evt.set()


def run_discovery_now(cfg: dict, job_id: int) -> None:
    """'Run now' button: run one job immediately in its own thread."""
    def target():
        db = DB(cfg["DATABASE"])
        try:
            fetcher = Fetcher(delay=cfg["FETCH_DELAY"], allow_private=cfg.get("ALLOW_PRIVATE_HOSTS", False))
            discovery.run_job(db, job_id, fetcher)
        except Exception:
            log.exception("run-now job failed")
        finally:
            db.close()
    threading.Thread(target=target, daemon=True).start()
