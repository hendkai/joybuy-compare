"""FastAPI backend + static webshop UI."""
from __future__ import annotations

import asyncio
import logging
import re
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
log = logging.getLogger("joybuy_compare.server")
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
        "shops": [{"name": s.name, "label": s.label, "short": s.short or s.label, "color": s.color, "matches": shop_counts.get(s.name, 0)}
                  for s in SHOPS.values()],
    }


INTERVALS = (0, 6, 12, 24, 168)  # hours; 0 = automatic updates off
AUTO_LIMIT = 200                  # max. comparisons per automatic run


def _interval() -> float:
    try:
        return float(db.get_setting("interval_h") or 0)
    except ValueError:
        return 0.0


def _next_run() -> float | None:
    iv = _interval()
    if not iv:
        return None
    last = float(db.get_setting("last_auto_run") or 0)
    return max(last + iv * 3600, time.time()) if last else time.time()


@app.get("/api/settings")
def get_settings():
    return {"zip": db.zip_code(), "interval_h": _interval(), "intervals": INTERVALS, "next_run": _next_run(),
            "auto_limit": AUTO_LIMIT}


@app.post("/api/settings")
def post_settings(body: dict = Body(...)):
    z = str(body.get("zip", "")).strip()
    if not re.fullmatch(r"\d{5}", z):
        raise HTTPException(400, "Bitte eine 5-stellige Postleitzahl eingeben")
    if "interval_h" in body:
        iv = float(body["interval_h"] or 0)
        if iv not in INTERVALS:
            raise HTTPException(400, "Ungültiges Intervall")
        if iv != _interval():
            db.set_setting("interval_h", str(iv))
            db.set_setting("last_auto_run", str(time.time()))  # first automatic run one interval from now
    old = db.zip_code()
    db.set_setting("zip", z)
    reset = 0
    if old and old != z:
        # supermarket prices depend on the market -> drop them and queue those products for a fresh comparison
        with db.session() as con:
            skus = [r[0] for r in con.execute("SELECT sku FROM searches WHERE shop='rewe'")]
            con.execute("DELETE FROM offers WHERE shop='rewe'")
            con.execute("DELETE FROM searches WHERE shop='rewe'")
            con.executemany("UPDATE products SET compared_at=NULL WHERE sku=?", [(s,) for s in skus])
            reset = len(skus)
    return {**get_settings(), "changed": old != z, "reset": reset}


@app.get("/api/status")
def status():
    st = read_status()
    st["running"] = bool(_proc and _proc.poll() is None) or (
        st.get("running") and time.time() - st.get("updated_at", 0) < 600)
    st["default_keywords"] = config.DEFAULT_KEYWORDS
    st["next_run"] = _next_run()
    return st


def _start(cmd: list[str]) -> subprocess.Popen:
    global _proc
    log_path = config.DATA_DIR / "refresh.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    write_status(running=True, phase="start", step="Starte …", error=None, done=0, total=0)
    _proc = subprocess.Popen(cmd, stdout=open(log_path, "w"), stderr=subprocess.STDOUT, start_new_session=True)
    return _proc


@app.post("/api/refresh")
def refresh(body: dict = Body(default={})):
    if (_proc and _proc.poll() is None) or status()["running"]:
        raise HTTPException(409, "Aktualisierung läuft bereits")
    if not db.zip_code():
        raise HTTPException(400, "Bitte zuerst deine Postleitzahl eingeben")
    if body.get("sku"):
        cmd = [sys.executable, "-m", "joybuy_compare.cli", "compare", "--max-age", "0", "--sku", str(body["sku"])]
    else:
        cmd = [sys.executable, "-m", "joybuy_compare.cli", "run", "--pages", str(int(body.get("pages", 2)))]
    if body.get("limit") and not body.get("sku"):
        cmd += ["--limit", str(int(body["limit"]))]
    if body.get("shops"):
        cmd += ["--shops", ",".join(_shops(body["shops"]))]
    cmd += [k.strip() for k in (body.get("keywords") or []) if k.strip()]
    p = _start(cmd)
    return {"started": True, "pid": p.pid}


async def _scheduler():
    """Automatic updates while `jbc serve` is running."""
    while True:
        await asyncio.sleep(60)
        try:
            iv = _interval()
            nxt = _next_run()
            if not iv or not db.zip_code() or nxt is None or time.time() < nxt or status()["running"]:
                continue
            db.set_setting("last_auto_run", str(time.time()))
            # re-crawl Joybuy and refresh comparisons older than the interval
            _start([sys.executable, "-m", "joybuy_compare.cli", "run", "--pages", "2", "--limit", str(AUTO_LIMIT),
                    "--max-age", str(iv)])
            log.info("automatische Aktualisierung gestartet (alle %g h)", iv)
        except Exception as e:  # never let the scheduler die
            log.warning("scheduler: %s", e)


@app.on_event("startup")
async def _startup():
    asyncio.create_task(_scheduler())


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
