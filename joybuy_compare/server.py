"""FastAPI backend + static webshop UI."""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import config, db
from .crawler import read_status, write_status
from .sources.shops import SHOPS

STATIC = Path(__file__).parent / "static"
app = FastAPI(title="Joybuy Preisvergleich")
_proc: subprocess.Popen | None = None

CATEGORY_SQL = "COALESCE(c1.name, c2.name, 'Weitere')"
SORTS = {
    "savings": "savings_pct IS NULL, savings_pct DESC",
    "savings_eur": "savings_eur IS NULL, savings_eur DESC",
    "discount": "p.discount_pct IS NULL, p.discount_pct DESC",
    "price_asc": "p.price ASC",
    "price_desc": "p.price DESC",
    "rating": "p.rating IS NULL, p.rating DESC",
    "expensive": "savings_pct IS NULL, savings_pct ASC",
    "new": "p.first_seen DESC",
}


def _base_query(shops: list[str]) -> tuple[str, list]:
    ph = ",".join("?" * len(shops))
    sql = f"""
    WITH best AS (
        SELECT sku, MIN(comparable_price) AS best_price,
               (SELECT shop FROM offers o2 WHERE o2.sku = o.sku AND o2.shop IN ({ph})
                ORDER BY comparable_price LIMIT 1) AS best_shop,
               COUNT(*) AS n_offers
        FROM offers o WHERE shop IN ({ph}) GROUP BY sku
    )
    SELECT p.*, {CATEGORY_SQL} AS category, b.best_price, b.best_shop, COALESCE(b.n_offers, 0) AS n_offers,
           CASE WHEN b.best_price > 0 THEN ROUND((b.best_price - p.price) / b.best_price * 100, 1) END AS savings_pct,
           CASE WHEN b.best_price > 0 THEN ROUND(b.best_price - p.price, 2) END AS savings_eur
    FROM products p
    LEFT JOIN best b ON b.sku = p.sku
    LEFT JOIN categories c1 ON c1.id = p.category_id1
    LEFT JOIN categories c2 ON c2.id = p.category_id2
    """
    return sql, [*shops, *shops]


def _shops(param: str | None) -> list[str]:
    s = [x for x in (param or "").split(",") if x in SHOPS]
    return s or list(SHOPS)


@app.get("/api/products")
def products(
    q: str | None = None,
    category: str | None = None,
    keyword: str | None = None,
    shops: str | None = None,
    compare: str = Query("all", pattern="^(all|cheaper|pricier|compared|uncompared)$"),
    min_savings: float | None = None,
    min_discount: float | None = None,
    price_min: float | None = None,
    price_max: float | None = None,
    in_stock: bool = False,
    sort: str = "savings",
    offset: int = 0,
    limit: int = Query(48, le=200),
):
    shop_list = _shops(shops)
    base, params = _base_query(shop_list)
    where, wp = [], []
    if q:
        for word in q.split():
            where.append("(p.title LIKE ? OR p.brand LIKE ?)")
            wp += [f"%{word}%", f"%{word}%"]
    if category:
        where.append(f"{CATEGORY_SQL} = ?")
        wp.append(category)
    if keyword:
        where.append("p.keyword = ?")
        wp.append(keyword)
    if compare == "cheaper":
        where.append("b.best_price > p.price")
    elif compare == "pricier":
        where.append("b.best_price < p.price")
    elif compare == "compared":
        where.append("b.best_price IS NOT NULL")
    elif compare == "uncompared":
        where.append("p.compared_at IS NULL")
    if min_savings is not None:
        where.append("(b.best_price - p.price) / b.best_price * 100 >= ?")
        wp.append(min_savings)
    if min_discount is not None and min_discount > 0:
        where.append("p.discount_pct >= ?")
        wp.append(min_discount)
    if price_min is not None:
        where.append("p.price >= ?")
        wp.append(price_min)
    if price_max is not None:
        where.append("p.price <= ?")
        wp.append(price_max)
    if in_stock:
        where.append("p.in_stock = 1")
    inner = base + (" WHERE " + " AND ".join(where) if where else "")
    order = SORTS.get(sort, SORTS["savings"]).replace("p.", "")
    with db.session() as con:
        total = con.execute(f"SELECT COUNT(*) FROM ({inner})", params + wp).fetchone()[0]
        rows = con.execute(f"SELECT * FROM ({inner}) ORDER BY {order} LIMIT ? OFFSET ?",
                           params + wp + [limit, offset]).fetchall()
        items = [dict(r) for r in rows]
        if items:
            skus = [i["sku"] for i in items]
            offers: dict[str, list] = {}
            for o in con.execute(
                f"SELECT sku, shop, price, comparable_price, match_kind, url FROM offers "
                f"WHERE sku IN ({','.join('?' * len(skus))}) AND shop IN ({','.join('?' * len(shop_list))}) "
                f"ORDER BY comparable_price", skus + shop_list):
                offers.setdefault(o["sku"], []).append(dict(o))
            for i in items:
                i["offers"] = offers.get(i["sku"], [])
    return {"total": total, "items": items}


@app.get("/api/products/{sku}")
def product(sku: str):
    base, params = _base_query(list(SHOPS))
    with db.session() as con:
        r = con.execute(f"SELECT * FROM ({base}) WHERE sku = ?", params + [sku]).fetchone()
        if not r:
            raise HTTPException(404)
        offers = [dict(o) for o in con.execute("SELECT * FROM offers WHERE sku=? ORDER BY comparable_price", (sku,))]
        searches = [dict(s) for s in con.execute("SELECT * FROM searches WHERE sku=?", (sku,))]
        hist = [dict(h) for h in con.execute("SELECT shop, price, ts FROM price_history WHERE sku=? ORDER BY ts", (sku,))]
    return {"product": dict(r), "offers": offers, "searches": searches, "history": hist}


@app.get("/api/facets")
def facets(shops: str | None = None):
    shop_list = _shops(shops)
    base, params = _base_query(shop_list)
    with db.session() as con:
        cats = [dict(r) for r in con.execute(
            f"SELECT category AS name, COUNT(*) AS n, SUM(best_price > price) AS cheaper FROM ({base}) "
            f"GROUP BY category ORDER BY n DESC", params)]
        kws = [dict(r) for r in con.execute(
            "SELECT keyword AS name, COUNT(*) AS n FROM products WHERE keyword IS NOT NULL GROUP BY keyword ORDER BY n DESC")]
        st = dict(con.execute(
            f"SELECT COUNT(*) AS products, SUM(compared_at IS NOT NULL) AS compared, SUM(best_price IS NOT NULL) AS matched, "
            f"SUM(best_price > price) AS cheaper, SUM(best_price < price) AS pricier, "
            f"ROUND(AVG(CASE WHEN best_price IS NOT NULL THEN savings_pct END), 1) AS avg_savings, "
            f"SUM(discount_pct > 0) AS discounted, MAX(updated_at) AS last_update FROM ({base})", params).fetchone())
        vals = sorted(r[0] for r in con.execute(f"SELECT savings_pct FROM ({base}) WHERE savings_pct IS NOT NULL", params))
        st["median_savings"] = (vals[len(vals) // 2] if len(vals) % 2 else round((vals[len(vals) // 2 - 1] + vals[len(vals) // 2]) / 2, 1)) if vals else None
        shop_counts = {r["shop"]: r["n"] for r in con.execute("SELECT shop, COUNT(*) n FROM offers GROUP BY shop")}
    return {
        "categories": cats,
        "keywords": kws,
        "stats": st,
        "shops": [{"name": s.name, "label": s.label, "color": s.color, "matches": shop_counts.get(s.name, 0)}
                  for s in SHOPS.values()],
    }


@app.get("/api/status")
def status():
    st = read_status()
    st["running"] = bool(_proc and _proc.poll() is None) or (
        st.get("running") and time.time() - st.get("updated_at", 0) < 600)
    st["default_keywords"] = config.DEFAULT_KEYWORDS
    return st


@app.post("/api/refresh")
def refresh(body: dict = Body(default={})):
    global _proc
    if (_proc and _proc.poll() is None) or status()["running"]:
        raise HTTPException(409, "Aktualisierung läuft bereits")
    if body.get("sku"):
        cmd = [sys.executable, "-m", "joybuy_compare.cli", "compare", "--max-age", "0", "--sku", str(body["sku"])]
    else:
        cmd = [sys.executable, "-m", "joybuy_compare.cli", "run", "--pages", str(int(body.get("pages", 2)))]
    if body.get("limit") and not body.get("sku"):
        cmd += ["--limit", str(int(body["limit"]))]
    if body.get("shops"):
        cmd += ["--shops", ",".join(_shops(body["shops"]))]
    kws = [k.strip() for k in (body.get("keywords") or []) if k.strip()]
    cmd += kws
    log_path = config.DATA_DIR / "refresh.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    write_status(running=True, phase="start", step="Starte …", error=None, done=0, total=0)
    _proc = subprocess.Popen(cmd, stdout=open(log_path, "w"), stderr=subprocess.STDOUT, start_new_session=True)
    return {"started": True, "pid": _proc.pid}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
