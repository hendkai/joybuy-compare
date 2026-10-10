"""History features: Joybuy price changes over time (temporary DB, never touches real data)."""
import time

import pytest
from fastapi.testclient import TestClient

from joybuy_compare import config, db


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    from joybuy_compare import server
    return TestClient(server.app)


def _product(sku, price, title="Testartikel 500 g"):
    return {"sku": sku, "title": title, "price": price, "in_stock": 1}


def test_price_history_fields_and_filters(client, monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(time, "time", lambda: t[0])
    with db.session() as con:
        db.upsert_product(con, _product("A", 5.00))   # A: 5.00 -> 4.00 -> 3.50 (new low)
        db.upsert_product(con, _product("B", 2.00))   # B: 2.00 -> 2.50 (raised)
        db.upsert_product(con, _product("C", 1.00))   # C: unchanged
    for price_a, price_b in ((4.00, 2.50), (3.50, 2.50)):
        t[0] += 3600
        with db.session() as con:
            db.upsert_product(con, _product("A", price_a))
            db.upsert_product(con, _product("B", price_b))
            db.upsert_product(con, _product("C", 1.00))

    items = {i["sku"]: i for i in client.get("/api/products", params={"limit": 50}).json()["items"]}
    a, b, c = items["A"], items["B"], items["C"]
    assert (a["jb_n"], a["jb_min"], a["jb_max"], a["jb_prev"]) == (3, 3.5, 5.0, 4.0)
    assert a["jb_change_pct"] == -12.5 and a["jb_is_low"] == 1
    assert b["jb_prev"] == 2.0 and b["jb_change_pct"] == 25.0 and b["jb_is_low"] == 0
    assert c["jb_n"] == 1 and c["jb_prev"] is None  # unchanged prices are not recorded twice

    def skus(**params):
        return [i["sku"] for i in client.get("/api/products", params=params).json()["items"]]

    assert skus(history="dropped") == ["A"]
    assert skus(history="raised") == ["B"]
    assert skus(history="lowest") == ["A"]
    assert set(skus(history="changed")) == {"A", "B"}
    assert skus(sort="drop")[0] == "A"
    st = client.get("/api/facets").json()["stats"]
    assert (st["dropped"], st["raised"], st["lowest"], st["with_history"]) == (1, 1, 1, 2)
