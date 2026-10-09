"""Orchestrates Joybuy crawling and competitor comparison."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time

from . import config, db
from .browser import Browser
from .matching import jb_quantity, match, search_query
from .sources import joybuy
from .sources.shops import SHOPS, ShopBlocked, search_with_retry

log = logging.getLogger(__name__)
STATUS_FILE = config.DATA_DIR / "status.json"
GROCERY_SHOPS = {"rewe", "dm"}
MIN_INTERVAL = {"amazon": 4.0, "idealo": 2.5, "rewe": 1.5, "dm": 1.0}


def write_status(**kw) -> None:
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    cur = read_status()
    cur.update(kw, updated_at=time.time())
    STATUS_FILE.write_text(json.dumps(cur))


def read_status() -> dict:
    try:
        return json.loads(STATUS_FILE.read_text())
    except Exception:
        return {}


async def crawl_joybuy(browser: Browser, keywords: list[str], pages: int = 2) -> int:
    total = 0
    for i, kw in enumerate(keywords, 1):
        write_status(phase="joybuy", step=f"Joybuy: „{kw}“", done=i - 1, total=len(keywords))
        try:
            products, cats = await joybuy.search(browser, kw, pages=pages)
        except joybuy.Blocked as e:
            log.error("%s – stopping Joybuy crawl", e)
            write_status(error=str(e))
            break
        except Exception as e:
            log.warning("joybuy %r failed: %s", kw, e)
            continue
        with db.session() as con:
            for c in cats:
                db.upsert_category(con, c["id"], c["name"], c["parent_id"], c["level"])
            for p in products:
                db.upsert_product(con, p)
        total += len(products)
        log.info("joybuy %r: %d products (%d total)", kw, len(products), total)
        await asyncio.sleep(1.5)
    return total


def _sales(text: str | None) -> float:
    m = re.search(r"([\d.,]+)\s*(k|mio)?", (text or "").lower())
    if not m:
        return 0.0
    n = float(m.group(1).replace(".", "").replace(",", "."))
    return n * {"k": 1e3, "mio": 1e6}.get(m.group(2) or "", 1)


class _Throttle:
    def __init__(self):
        self.locks = {s: asyncio.Lock() for s in SHOPS}
        self.last = {s: 0.0 for s in SHOPS}

    async def wait(self, shop: str):
        async with self.locks[shop]:
            delta = time.monotonic() - self.last[shop]
            gap = MIN_INTERVAL.get(shop, 2.0)
            if delta < gap:
                await asyncio.sleep(gap - delta)
            self.last[shop] = time.monotonic()


def pick_best(p: dict, offers: list[dict]) -> dict | None:
    q = jb_quantity(p["title"], p.get("price"), p.get("unit_price_text"))
    cands = []
    for o in offers:
        m = match(p["title"], p.get("brand"), p["price"], q, o["title"], o["price"], o.get("unit_price_text"))
        if m:
            cands.append({**o, "match_kind": m.kind, "score": round(m.score, 1), "comparable_price": m.comparable_price})
    if not cands:
        return None
    # same package size beats unit-price conversions; then take the cheapest of the best name matches
    exact = [c for c in cands if c["match_kind"] == "exact"]
    pool = exact or cands
    top = max(c["score"] for c in pool)
    near = [c for c in pool if c["score"] >= top - 4]
    return min(near, key=lambda c: c["comparable_price"])


async def compare(browser: Browser, shops: list[str], limit: int | None = None, max_age_h: float = 24,
                  skus: list[str] | None = None, only_known: bool = False) -> int:
    cutoff = time.time() - max_age_h * 3600
    with db.session() as con:
        if skus:
            rows = con.execute(f"SELECT * FROM products WHERE sku IN ({','.join('?' * len(skus))})", skus).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM products WHERE price IS NOT NULL AND (compared_at IS NULL OR compared_at < ?) "
                + ("AND compared_at IS NOT NULL " if only_known else "")
                + "ORDER BY compared_at IS NOT NULL, discount_pct DESC NULLS LAST", (cutoff,)).fetchall()
    # most-sold first: those are the comparisons people care about
    products = sorted((dict(r) for r in rows), key=lambda r: (r["compared_at"] is not None, -_sales(r.get("sales_text"))))
    products = products[: limit or None]
    throttle = _Throttle()
    blocked: set[str] = set()
    done = 0
    write_status(phase="compare", step="Vergleiche Preise", done=0, total=len(products))

    async def one(p: dict):
        nonlocal done
        q = jb_quantity(p["title"], p.get("price"), p.get("unit_price_text"))
        query_long = search_query(p["title"], p.get("brand"))
        query_short = search_query(p["title"], p.get("brand"), max_tokens=4)
        for s in shops:
            query = query_short if s in GROCERY_SHOPS else query_long
            if s in blocked:
                continue
            if s in GROCERY_SHOPS and not (q and q.dimension in ("mass", "volume")):
                continue
            await throttle.wait(s)
            try:
                raw = await search_with_retry(SHOPS[s], browser, query)
            except ShopBlocked as e:
                log.error("%s blocked us (%s) – skipping it for this run", s, e)
                blocked.add(s)
                continue
            except Exception:
                with db.session() as con:
                    db.save_offer(con, p["sku"], s, None, query, "error")
                continue
            best = pick_best(p, raw)
            with db.session() as con:
                db.save_offer(con, p["sku"], s, best, query, "match" if best else "nomatch", raw)
            if best:
                log.info("  %-7s %6.2f€ vs JB %6.2f€ [%s %.0f] %s", s, best["comparable_price"], p["price"],
                         best["match_kind"], best["score"], best["title"][:60])
        with db.session() as con:
            con.execute("UPDATE products SET compared_at=? WHERE sku=?", (time.time(), p["sku"]))
        done += 1
        write_status(done=done, step=f"Vergleich: {p['title'][:60]}")

    # bounded concurrency: the browser semaphore limits open tabs, this bounds queued products
    sem = asyncio.Semaphore(browser.parallel)

    async def guarded(p):
        async with sem:
            try:
                await one(p)
            except Exception as e:
                log.warning("compare %s failed: %s", p["sku"], e)

    await asyncio.gather(*(guarded(p) for p in products))
    return done


def rematch() -> tuple[int, int]:
    """Re-run matching on cached raw search results (no scraping)."""
    changed = n = 0
    with db.session() as con:
        prods = {r["sku"]: dict(r) for r in con.execute("SELECT * FROM products")}
        for srow in con.execute("SELECT * FROM searches WHERE raw IS NOT NULL").fetchall():
            p = prods.get(srow["sku"])
            if not p or p.get("price") is None:
                continue
            n += 1
            best = pick_best(p, json.loads(srow["raw"]))
            old = con.execute("SELECT title, comparable_price FROM offers WHERE sku=? AND shop=?",
                              (srow["sku"], srow["shop"])).fetchone()
            if (old and best and (old["title"], old["comparable_price"]) == (best["title"], best["comparable_price"])) \
                    or (not old and not best):
                continue
            changed += 1
            db.save_offer(con, srow["sku"], srow["shop"], best, srow["query"], "match" if best else "nomatch",
                          record_search=False)
    return n, changed


async def run(keywords: list[str] | None, pages: int, shops: list[str], limit: int | None, max_age_h: float,
              parallel: int = 3, skip_joybuy: bool = False, skip_compare: bool = False) -> None:
    write_status(running=True, started_at=time.time(), error=None, phase="start", done=0, total=0)
    try:
        async with Browser(parallel=parallel) as browser:
            if not skip_joybuy:
                await crawl_joybuy(browser, keywords or config.DEFAULT_KEYWORDS, pages)
            if not skip_compare:
                await compare(browser, shops, limit=limit, max_age_h=max_age_h)
    except Exception as e:
        write_status(error=str(e))
        raise
    finally:
        write_status(running=False, phase="idle", finished_at=time.time())
