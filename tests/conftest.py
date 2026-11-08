import os
import re
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["RUN_SCHEDULER"] = "0"

from app import create_app, utils  # noqa: E402
from app.db import DB  # noqa: E402
from app.services import emailcheck  # noqa: E402


@pytest.fixture()
def app(tmp_path):
    a = create_app({"TESTING": True, "DATABASE": str(tmp_path / "t.db"), "SECRET_KEY": "test-secret",
                    "MX_CHECK": False, "RUN_SCHEDULER": False, "ALLOW_PRIVATE_HOSTS": True,
                    "BASE_URL": "http://localhost:5000", "FETCH_DELAY": 0})
    emailcheck.MX_ENABLED = False
    yield a
    utils.set_now(None)


@pytest.fixture()
def cfg(app):
    return dict(app.config)


@pytest.fixture()
def db(app):
    d = DB(app.config["DATABASE"])
    yield d
    d.close()


@pytest.fixture()
def org(db):
    """An org in demo email mode, Karachi tz, window open all day, all weekdays."""
    oid = db.x("INSERT INTO orgs (name,public_key,created_at,sender_name,postal_address,email_mode,email_address,"
               "timezone,window_start,window_end,weekdays_only,daily_limit,email_connected_at) "
               "VALUES ('Acme','pk1',?, 'Acme Gloves','1 Main St, Sialkot, Pakistan','demo','demo@demo.local',"
               "'UTC',0,24,0,50,?)", (utils.now(), "2020-01-01 00:00:00"))
    return oid


@pytest.fixture()
def campaign(db, org):
    return db.x("INSERT INTO campaigns (org_id,name,product,target_market,created_at) VALUES (?,?,?,?,?)",
                (org, "TPR Gloves USA", "gloves", "distributors", utils.now()))


def freeze(y=2026, m=11, d=3, h=10, mi=0):
    utils.set_now(datetime(y, m, d, h, mi, 0))


@pytest.fixture()
def clean_clock():
    yield
    utils.set_now(None)


def csrf_of(client, path="/login"):
    html = client.get(path).get_data(as_text=True)
    m = re.search(r'name="_csrf" value="([^"]+)"', html)
    return m.group(1) if m else ""
