import pytest
from fastapi.testclient import TestClient

from app.categorize import normalize_merchant

SAMPLE = """Transaction Date,Post Date,Description,Category,Type,Amount,Memo
04/20/2026,04/22/2026,DLO RAPPI,Food & Drink,Sale,-324.11,
04/20/2026,04/22/2026,DLO RAPPI,Food & Drink,Return,16.15,
04/19/2026,04/21/2026,DLO UBEREATS CA2,Food & Drink,Sale,-51.66,
04/20/2026,04/20/2026,UBER* EATS,Food & Drink,Sale,-20.80,
04/09/2026,04/12/2026,UBER   *TRIP,Travel,Sale,-5.24,
04/09/2026,04/12/2026,UBER   *TRIP,Travel,Sale,-5.24,
04/20/2026,04/22/2026,Amazon.com  Inc. AMZN,Bills & Utilities,Sale,-33.76,
03/09/2026,03/09/2026,AMZN Digital*BE9MG9S80,Shopping,Sale,-19.99,
04/04/2026,04/05/2026,AUNA ONCO ONLINE 4,Bills & Utilities,Sale,-431.01,
04/03/2026,04/03/2026,PUBLIC STORAGE 26904,Home,Sale,-281.00,
04/15/2026,04/17/2026,SUMESA OAXACA,Groceries,Sale,-1100.00,
04/10/2026,04/12/2026,SOME NEW PLACE,Food & Drink,Sale,-12.00,
04/05/2026,04/06/2026,JUAN VALDEZ ARP INTERN,Food & Drink,Sale,-8.54,
04/09/2026,04/11/2026,H&amp;M  0450MIAMI,Shopping,Sale,-39.38,
04/01/2026,04/01/2026,Payment Thank You-Mobile,,Payment,6931.34,
"""


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr("app.db.DB_PATH", str(tmp_path / "test.db"))
    from app.main import app

    with TestClient(app) as c:
        yield c


def upload(client, text=SAMPLE, name="Chase6297_Activity.CSV"):
    return client.post("/import", files=[("files", (name, text, "text/csv"))]).json()


def test_normalize_merchant():
    assert normalize_merchant("AMAZON MKTPL*GK3V04R63") == "AMAZON MKTPL"
    assert normalize_merchant("PUBLIX #1662") == "PUBLIX"
    assert normalize_merchant("AMERICAN AIR0012318120817") == "AMERICAN AIR"
    assert normalize_merchant("UBER   *EATS") == "UBER *EATS"
    assert normalize_merchant("SUPERMERCADO LA GRANJI X3") == "SUPERMERCADO LA GRANJI"


def test_import_and_dedup(client):
    first = upload(client)
    assert first["imported"] == 14  # payment row skipped
    assert first["duplicates"] == 0
    assert first["uncategorized"] == 2  # SOME NEW PLACE, JUAN VALDEZ
    assert first["oneoffs"] == 1  # PUBLIC STORAGE, not JUAN VALDEZ "ARP IN"
    assert first["files"][0]["card_last4"] == "6297"
    flagged = {f["description"] for f in first["flagged"]}
    assert flagged == {"DLO RAPPI", "PUBLIC STORAGE 26904", "SUMESA OAXACA", "AUNA ONCO ONLINE 4"}

    again = upload(client)
    assert again["imported"] == 0
    assert again["duplicates"] == 14


def test_categories(client):
    upload(client)
    txs = {t["description"]: t for t in client.get("/transactions?month=2026-04").json()}
    assert txs["UBER* EATS"]["category"] == "Food & Dining"  # odd spacing still matches
    assert txs["Amazon.com  Inc. AMZN"]["category"] == "Shopping"
    assert txs["H&M  0450MIAMI"]["category"] == "Shopping"  # '&amp;' unescaped
    assert txs["AUNA ONCO ONLINE 4"]["is_fixed"] is True
    assert txs["PUBLIC STORAGE 26904"]["is_oneoff"] is True
    march = client.get("/transactions?month=2026-03").json()
    assert march[0]["category"] == "Bills & Utilities"  # AMZN DIGITAL beats AMZN


def test_summary(client):
    upload(client)
    s = client.get("/summary/2026-04").json()
    cats = {c["category"]: c for c in s["categories"]}
    food = cats["Food & Dining"]
    assert food["actual"] == round(324.11 - 16.15 + 51.66 + 20.80, 2)
    assert food["over_target"] is False
    assert cats["Groceries"]["actual"] == 1100.0
    assert cats["Groceries"]["over_target"] is True  # 37.5% over target
    assert cats["Transport"]["actual"] == 10.48
    assert s["fixed_on_card_total"] == 431.01
    assert s["oneoff_total"] == 281.0
    assert s["uncategorized_total"] == 20.54


def test_bad_month(client):
    assert client.get("/summary/2026-4").status_code == 400


def test_new_patterns(client):
    text = """Transaction Date,Post Date,Description,Category,Type,Amount,Memo
08/20/2026,08/23/2026,DLO UBERRIDES CA2,Travel,Sale,-35.43,
08/21/2026,08/23/2026,DLO*UBER,Travel,Sale,-4.37,
08/22/2026,08/23/2026,DLO*UBER EATS,Food & Drink,Sale,-61.32,
05/18/2026,05/19/2026,TARGET        00021881,Shopping,Sale,-107.02,
03/22/2026,03/23/2026,CARULLA FRESH COUNTRY,Groceries,Sale,-169.77,
"""
    upload(client, text)
    txs = {t["description"]: t for m in ("2026-03", "2026-05", "2026-08")
           for t in client.get(f"/transactions?month={m}").json()}
    assert txs["DLO UBERRIDES CA2"]["category"] == "Transport"
    assert txs["DLO*UBER"]["category"] == "Transport"
    assert txs["DLO*UBER EATS"]["category"] == "Food & Dining"  # longer pattern wins
    assert txs["TARGET        00021881"]["is_oneoff"] is True
    assert txs["CARULLA FRESH COUNTRY"]["category"] == "Groceries"


def test_user_rule_recategorizes(client):
    upload(client)
    r = client.post("/merchant-map", json={"pattern": "some new place", "category": "Food & Dining"})
    assert r.json()["transactions_updated"] == 1
    txs = {t["description"]: t for t in client.get("/transactions?month=2026-04").json()}
    assert txs["SOME NEW PLACE"]["category"] == "Food & Dining"
    bad = client.post("/merchant-map", json={"pattern": "X", "category": "Nope"})
    assert bad.status_code == 400


def test_large_medical_bill_is_oneoff_but_recorded(client):
    text = """Transaction Date,Post Date,Description,Category,Type,Amount,Memo
09/15/2026,09/16/2026,MSMC MYCHART BILLPAY,Health & Wellness,Sale,-1028.84,
09/16/2026,09/17/2026,MSMC MAIN GARAGE,Health & Wellness,Sale,-12.00,
"""
    upload(client, text)
    s = client.get("/summary/2026-09").json()
    health = next(c for c in s["categories"] if c["category"] == "Health & Wellness")
    assert health["actual"] == 12.0  # parking counts, the big bill doesn't
    assert s["oneoffs"] == [{"date": "2026-09-15", "description": "MSMC MYCHART BILLPAY",
                             "amount": 1028.84, "category": "Health & Wellness"}]


def test_frontend_and_months(client, monkeypatch):
    assert "Family Budget" in client.get("/").text
    upload(client)
    months = client.get("/months").json()
    assert [m["month"] for m in months] == ["2026-03", "2026-04"]
    april = months[1]
    # same number /summary reports: everything except one-offs and fixed
    assert april["variable_total"] == client.get("/summary/2026-04").json()["variable_total"]
    assert april["variable_target"] == 5365


def test_config_reads_income_from_env(client, monkeypatch):
    cfg = client.get("/config").json()
    assert cfg["monthly_net_income"] is None  # not set -> savings tile hidden
    assert "Food & Dining" in cfg["categories"] and "One-off" in cfg["categories"]
    monkeypatch.setenv("MONTHLY_NET_INCOME", "1000")
    assert client.get("/config").json()["monthly_net_income"] == 1000.0


def test_login_required_when_users_configured(client, monkeypatch):
    monkeypatch.setenv("APP_USERS", "Mariano:pw-one, Maura:pw-two")
    assert client.get("/").status_code == 401
    assert client.get("/docs").status_code == 401
    assert client.get("/", auth=("maura", "wrong")).status_code == 401
    r = client.get("/config", auth=("maura", "pw-two"))  # name is case-insensitive
    assert r.status_code == 200 and r.json()["user"] == "Maura"
    assert client.get("/config", auth=("Mariano", "pw-one")).json()["user"] == "Mariano"


def test_open_without_app_users(client):
    assert client.get("/config").json()["user"] is None


def test_fixed_costs_itemized_from_env(client, monkeypatch):
    monkeypatch.setenv("FIXED_COSTS", "Rent:2500, Domestic help:1561,Internet (Totalplay):61,bad")
    cfg = client.get("/config").json()
    assert cfg["fixed_costs"] == [
        {"name": "Rent", "amount": 2500.0},
        {"name": "Domestic help", "amount": 1561.0},
        {"name": "Internet (Totalplay)", "amount": 61.0},
    ]
    assert cfg["monthly_fixed_costs"] == 4122.0


def test_icon_public_but_data_private(client, monkeypatch):
    monkeypatch.setenv("APP_USERS", "Maura:pw")
    assert client.get("/static/icon-180.png").status_code == 200
    assert client.get("/static/manifest.webmanifest").status_code == 200
    assert client.get("/static/app.js").status_code == 401
    assert client.get("/months").status_code == 401
