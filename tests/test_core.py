import json
import pytest

from app import utils
from app.services import customers as cust, emailcheck, scoring
from app.services.domains import clean_domain, normalize_email, is_free_mail, dedup_key, is_directory_domain
from app.services.email_finder import extract_emails, decode_cf, find_emails, rank, business_only


# ---------------------------------------------------------------- domains
@pytest.mark.parametrize("raw,exp", [
    ("https://www.Example.com/path?x=1", "example.com"), ("example.com", "example.com"),
    ("HTTP://WWW.A-B.co.uk:8080/x", "a-b.co.uk"), ("  www.foo.org ", "foo.org"), ("", ""), ("not a domain", ""),
    ("localhost", ""), ("info@example.com", ""), ("http://", ""), ("http://256", ""), ("-bad-.com", ""),
    ("https://münchen.de", "xn--mnchen-3ya.de"),
])
def test_clean_domain(raw, exp):
    assert clean_domain(raw) == exp


def test_email_helpers():
    assert normalize_email(" <Info@Example.COM> ") == "info@example.com"
    assert normalize_email("bad@@x.com") == "" and normalize_email("a@b") == ""
    assert is_free_mail("gmail.com") and is_free_mail("yahoo.co.uk") and is_free_mail("hotmail.fr")
    assert not is_free_mail("acme.com")
    assert dedup_key("a.com", "x@y.com") == "a.com" and dedup_key("", "x@gmail.com") == "mail:x@gmail.com" and dedup_key("", "") == ""
    assert is_directory_domain("facebook.com") and is_directory_domain("m.facebook.com") and not is_directory_domain("acme.com")


# ---------------------------------------------------------------- email check
def test_check_email():
    emailcheck.set_resolver(lambda d: d != "dead.com")
    emailcheck.MX_ENABLED = True
    try:
        assert emailcheck.check_email("sales@acme.com")[0] == "Unverified"
        assert emailcheck.check_email("x@dead.com") == ("Invalid", "domain has no mail server")
        assert emailcheck.check_email("webmaster@acme.com")[0] == "Risky"
        assert emailcheck.check_email("noreply@acme.com")[0] == "Invalid"
        assert emailcheck.check_email("bad..x@acme.com")[0] == "Invalid"
        assert emailcheck.check_email("nobody@gmail.com")[0] == "Unverified"  # free mail: no MX query
        emailcheck.set_resolver(lambda d: None)  # network trouble: never mark invalid
        assert emailcheck.check_email("y@other.com")[0] == "Unverified"
    finally:
        emailcheck.set_resolver(None)
        emailcheck.MX_ENABLED = False


# ---------------------------------------------------------------- scoring
def mk(**kw):
    base = dict(company_name="Acme Glove Distributors", description="", domain="acme.com", phone="123",
                email_status="Unverified", country="USA")
    base.update(kw)
    return base


def test_scoring_grades():
    s, g, parts = scoring.score_customer(mk(), "distributor", "United States")
    assert (s, g) == (100, "Hot") and sum(parts.values()) == 100
    s, g, _ = scoring.score_customer(mk(phone=""), "distributor", "United States")
    assert (s, g) == (85, "Hot")
    s, g, _ = scoring.score_customer(mk(phone="", company_name="Zed", email_status="Unverified"), "distributor", "USA")
    assert (s, g) == (55, "Warm")
    s, g, _ = scoring.score_customer(mk(phone="", company_name="Zed", email_status="None", domain=""), "distributor", "USA")
    assert g == "Cold" and s == 10
    assert scoring.grade_for(75) == "Hot" and scoring.grade_for(74) == "Warm" and scoring.grade_for(50) == "Warm" and scoring.grade_for(49) == "Cold"
    s, _, parts = scoring.score_customer(mk(email_status="Risky"), "distributor", "")
    assert parts["Usable email"] == 15


# ---------------------------------------------------------------- email finder
def test_extract_emails():
    html = '''<a href="mailto:Sales@Acme.com?subject=x">m</a> info [at] acme.com <img src="logo@2x.png">
      <span data-cfemail="%s"></span> user@example.com x@sentry.io''' % (
        "".join(["%02x" % 0x42] + ["%02x" % (ord(ch) ^ 0x42) for ch in "boss@acme.com"]))
    found = extract_emails(html)
    assert "sales@acme.com" in found and "boss@acme.com" in found
    assert not any("png" in e or "example.com" in e or "sentry" in e for e in found)
    assert decode_cf("zz") == ""


def test_rank_and_business_only():
    mails = ["bob@gmail.com", "info@acme.com", "sales@acme.com", "x@other.com"]
    assert business_only(mails) == ["info@acme.com", "sales@acme.com", "x@other.com"]
    assert rank(business_only(mails), "acme.com") == ["sales@acme.com", "info@acme.com", "x@other.com"]


class FakeFetcher:
    def __init__(self, pages): self.pages, self.calls = pages, []
    def get(self, url, **kw):
        self.calls.append(url)
        from app.services.fetcher import FetchError
        if url in self.pages: return self.pages[url]
        raise FetchError("404")


def test_find_emails_follows_contact_links():
    f = FakeFetcher({"https://acme.com/": '<a href="/reach-contact">Contact</a> hi',
                     "https://acme.com/reach-contact": "write to sales@acme.com or gmail me@gmail.com"})
    assert find_emails("acme.com", f) == ["sales@acme.com"]
    f2 = FakeFetcher({})
    assert find_emails("none.com", f2) == []
    assert any(u.startswith("http://none.com") for u in f2.calls)


# ---------------------------------------------------------------- customers
def test_add_dedup_excluded(db, org, campaign):
    st, cid, _ = cust.add_customer(db, org, campaign, company_name="Acme", website="https://www.acme.com/x", email="Info@Acme.com")
    assert st == "added"
    c = db.one("SELECT * FROM customers WHERE id=?", (cid,))
    assert c["domain"] == "acme.com" and c["email"] == "info@acme.com" and c["email_status"] == "Unverified" and c["enrich_state"] == "done"
    assert cust.add_customer(db, org, campaign, website="acme.com")[0] == "duplicate"
    assert cust.add_customer(db, org, campaign, email="x@acme.com")[0] == "duplicate"   # business email -> same domain key
    assert cust.add_customer(db, org, campaign, company_name="NoKey")[0] == "invalid"
    st, g_id, _ = cust.add_customer(db, org, campaign, company_name="Joe", email="joe@gmail.com")
    assert st == "added" and db.one("SELECT dedup_key FROM customers WHERE id=?", (g_id,))[0] == "mail:joe@gmail.com"
    assert cust.add_customer(db, org, campaign, email="JOE@gmail.com")[0] == "duplicate"
    # discovered companies drop free-mail addresses
    st, nid, _ = cust.add_customer(db, org, campaign, company_name="Z", website="zed.com", email="z@gmail.com", allow_free_email=False)
    assert db.one("SELECT email, enrich_state FROM customers WHERE id=?", (nid,))["enrich_state"] == "pending"
    # exclude -> never re-added, undo restores
    assert cust.exclude_customer(db, org, cid, None)
    assert cust.add_customer(db, org, campaign, website="acme.com")[0] == "excluded"
    ex = db.one("SELECT id FROM excluded_domains WHERE org_id=?", (org,))["id"]
    assert cust.restore_excluded(db, org, ex)
    assert db.one("SELECT excluded FROM customers WHERE id=?", (cid,))["excluded"] == 0
    assert cust.add_customer(db, org, campaign, website="acme.com")[0] == "duplicate"


def test_org_isolation(db, org, campaign):
    other = db.x("INSERT INTO orgs (name,public_key,created_at) VALUES ('B','pk2',?)", (utils.now(),))
    assert cust.add_customer(db, org, campaign, website="same.com")[0] == "added"
    assert cust.add_customer(db, other, None, website="same.com")[0] == "added"   # different tenant


def test_enrich_pending(db, org, campaign):
    cust.add_customer(db, org, campaign, company_name="A", website="acme.com", phone="1")
    cust.add_customer(db, org, campaign, company_name="B", website="b.com")
    f = FakeFetcher({"https://acme.com/": "mail sales@acme.com or info@acme.com"})
    assert cust.enrich_pending(db, f) == 2
    a = db.one("SELECT * FROM customers WHERE domain='acme.com'")
    assert a["email"] == "sales@acme.com" and a["extra_emails"] == "info@acme.com" and a["enrich_state"] == "done" and a["score"] > 0
    assert db.one("SELECT email, enrich_state FROM customers WHERE domain='b.com'")["email"] == ""


def test_import_csv_xlsx(db, org, campaign, tmp_path):
    csvdata = "Company,Website,Email,Phone,Country\nAcme,acme.com,a@acme.com,1,USA\nAcme dup,https://acme.com,,,\nBeta,,,,\nGamma,gamma.com,,,UK\n".encode()
    rows = cust.read_rows("x.csv", csvdata)
    res = cust.import_rows(db, org, campaign, rows)
    assert (res["added"], res["duplicate"], res["invalid"]) == (2, 1, 1) and "Row 4" in res["errors"][0]
    import openpyxl
    wb = openpyxl.Workbook(); ws = wb.active
    ws.append(["Name", "URL", "e-mail"]); ws.append(["Delta", "delta.com", "d@delta.com"])
    p = tmp_path / "x.xlsx"; wb.save(p)
    rows = cust.read_rows("x.xlsx", p.read_bytes())
    assert cust.import_rows(db, org, campaign, rows)["added"] == 1
    with pytest.raises(ValueError): cust.read_rows("x.pdf", b"x")
    with pytest.raises(ValueError): cust.read_rows("x.csv", b"foo,bar\n1,2\n")
    with pytest.raises(ValueError): cust.read_rows("x.csv", b"")
