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
from .sources.shops import GROCERY_SHOPS, SHOPS, ShopBlocked, search_with_retry

log = logging.getLogger(__name__)
STATUS_FILE = config.DATA_DIR / "status.json"


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


JOYBUY_PAUSE = 20.0       # seconds between two Joybuy searches (Joybuy blocks the IP after ~15 quick searches)
JOYBUY_COOLDOWN_H = 12    # after a block: no Joybuy crawling for this long (comparisons still run)


def joybuy_blocked_until() -> float:
    return float(db.get_setting("joybuy_blocked_until") or 0)


async def crawl_joybuy(browser: Browser, keywords: list[str], pages: int = 2, parallel: int = 1) -> int:
    """Crawl Joybuy search results for the given terms (`parallel` tabs at once)."""
    total = 0
    done = 0
    stop = asyncio.Event()
    sem = asyncio.Semaphore(max(1, parallel))
    write_status(phase="joybuy", step="Joybuy-Artikel laden", done=0, total=len(keywords))

    async def one(kw: str):
        nonlocal total, done
        if stop.is_set():
            return
        async with sem:
            if stop.is_set():
                return
            try:
                products, cats = await joybuy.search(browser, kw, pages=pages)
            except joybuy.Blocked as e:
                until = time.time() + JOYBUY_COOLDOWN_H * 3600
                db.set_setting("joybuy_blocked_until", str(until))
                log.error("%s – stopping Joybuy crawl, pausing Joybuy until %s", e, time.strftime("%d.%m. %H:%M", time.localtime(until)))
                write_status(error=f"Joybuy hat gedrosselt – Joybuy-Crawl pausiert bis {time.strftime('%d.%m. %H:%M', time.localtime(until))} (Vergleiche laufen weiter)")
                stop.set()
                return
            except Exception as e:
                log.warning("joybuy %r failed: %s", kw, e, exc_info=log.isEnabledFor(logging.DEBUG))
                return
            with db.session() as con:
                for c in cats:
                    db.upsert_category(con, c["id"], c["name"], c["parent_id"], c["level"])
                for p in products:
                    db.upsert_product(con, p)
            db.mark_keyword(kw, len(products))
            total += len(products)
            done += 1
            write_status(done=done, step=f"Joybuy: „{kw}“ ({len(products)} Artikel)")
            await asyncio.sleep(JOYBUY_PAUSE)

    if not getattr(browser, "_joybuy_warm", False) and keywords:
        await one(keywords[0])  # first one alone: warms up the Joybuy session
        keywords = keywords[1:]
    await asyncio.gather(*(one(k) for k in keywords))
    with db.session() as con:
        n = con.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    log.info("Joybuy: %d Suchbegriffe, %d Treffer, %d Artikel in der Datenbank", done, total, n)
    return total


async def refresh_catalog(browser: Browser, force: bool = False) -> int:
    """Load Joybuy's category tree into the keyword list (weekly, or when the list is empty)."""
    last = float(db.get_setting("catalog_loaded") or 0)
    if not force and time.time() - last < 7 * 86400:
        return 0
    terms = await joybuy.catalog_terms(browser)
    added = db.add_keywords(terms, "catalog") + db.add_keywords(config.DEFAULT_KEYWORDS, "default")
    db.set_setting("catalog_loaded", str(time.time()))
    log.info("Joybuy-Kategoriebaum: %d Kategorien, %d neue Suchbegriffe", len(terms), added)
    return added


def _sales(text: str | None) -> float:
    m = re.search(r"([\d.,]+)\s*(k|mio)?", (text or "").lower())
    if not m:
        return 0.0
    n = float(m.group(1).replace(".", "").replace(",", "."))
    return n * {"k": 1e3, "mio": 1e6}.get(m.group(2) or "", 1)


class _Throttle:
    """Per-shop politeness delay + a per-run cache, so identical searches (e.g. 'Nutella' on ALDI for several
    Joybuy articles) hit the shop only once."""

    def __init__(self):
        self.locks = {s: asyncio.Lock() for s in SHOPS}
        self.last = {s: 0.0 for s in SHOPS}
        self.cache: dict[tuple[str, str], asyncio.Future] = {}

    async def search(self, browser: Browser, shop: str, query: str) -> list[dict]:
        key = (shop, query.lower())
        fut = self.cache.get(key)
        if fut is None:
            fut = self.cache[key] = asyncio.get_running_loop().create_future()
            try:
                await self.wait(shop)
                fut.set_result(await search_with_retry(SHOPS[shop], browser, query))
            except BaseException as e:
                fut.set_exception(e)
                del self.cache[key]
        return list(await asyncio.shield(fut))

    async def wait(self, shop: str):
        async with self.locks[shop]:
            delta = time.monotonic() - self.last[shop]
            gap = SHOPS[shop].min_interval
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


def queries_for(shop: str, p: dict) -> list[str]:
    """Search strings to try, most specific first. Supermarket searches are strict, so they get short queries."""
    t, b = p["title"], p.get("brand")
    if shop in ("aldi_sued", "aldi_nord"):
        # ALDI search only finds something with one or two plain words
        qs = [search_query(t, b, max_tokens=2, with_size=False), search_query(t, b, max_tokens=1, with_size=False)]
    elif shop == "dm":  # heavily rate-limited: one query only
        qs = [search_query(t, b, max_tokens=4)]
    elif shop in GROCERY_SHOPS:
        qs = [search_query(t, b, max_tokens=4), search_query(t, b, max_tokens=2)]
    else:
        qs = [search_query(p["title"], p.get("brand"))]
    return list(dict.fromkeys(qs))


async def search_and_match(browser: Browser, shop: str, p: dict, throttle: "_Throttle | None" = None
                           ) -> tuple[str, list[dict], dict | None]:
    """Search one shop for a Joybuy product; falls back to a shorter query when nothing matches."""
    raw: list[dict] = []
    query = ""
    for query in queries_for(shop, p):
        found = await (throttle.search(browser, shop, query) if throttle
                       else search_with_retry(SHOPS[shop], browser, query))
        raw += found
        best = pick_best(p, raw)
        if best:
            return query, raw, best
    return query, raw, None


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

    async def one_shop(p: dict, s: str):
        if s in blocked:
            return
        try:
            query, raw, best = await search_and_match(browser, s, p, throttle)
        except ShopBlocked as e:
            if s not in blocked:
                log.error("%s blocked us (%s) – skipping it for this run", s, e)
            blocked.add(s)
            return
        except Exception:
            with db.session() as con:
                db.save_offer(con, p["sku"], s, None, search_query(p["title"], p.get("brand")), "error")
            return
        with db.session() as con:
            db.save_offer(con, p["sku"], s, best, query, "match" if best else "nomatch", raw)
        if best:
            log.info("  %-9s %6.2f€ vs JB %6.2f€ [%s %.0f] %s", s, best["comparable_price"], p["price"],
                     best["match_kind"], best["score"], best["title"][:60])

    async def one(p: dict):
        nonlocal done
        q = jb_quantity(p["title"], p.get("price"), p.get("unit_price_text"))
        todo = [s for s in shops if not (s in GROCERY_SHOPS and not (q and q.dimension in ("mass", "volume")))]
        # all shops of one product in parallel; per-shop throttling keeps it polite
        await asyncio.gather(*(one_shop(p, s) for s in todo))
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
              parallel: int = 6, skip_joybuy: bool = False, skip_compare: bool = False,
              kw_limit: int | None = None, kw_max_age_h: float = 0, jb_parallel: int = 1) -> None:
    """keywords given -> crawl exactly those; otherwise crawl the `kw_limit` least recently crawled terms
    from the keyword list (Joybuy's whole category tree + default terms)."""
    write_status(running=True, started_at=time.time(), error=None, phase="start", done=0, total=0)
    try:
        async with Browser(parallel=parallel) as browser:
            if not skip_joybuy and joybuy_blocked_until() > time.time():
                log.warning("Joybuy-Crawl pausiert bis %s (Drosselung) – nur Vergleiche",
                            time.strftime("%d.%m. %H:%M", time.localtime(joybuy_blocked_until())))
                skip_joybuy = True
            if not skip_joybuy:
                if keywords:
                    db.add_keywords(keywords, "user")
                    terms = keywords
                else:
                    write_status(phase="joybuy", step="Lade Joybuy-Kategoriebaum")
                    await refresh_catalog(browser)
                    terms = db.due_keywords(kw_limit, kw_max_age_h)
                await crawl_joybuy(browser, terms, pages, parallel=jb_parallel)
            if not skip_compare:
                await compare(browser, shops, limit=limit, max_age_h=max_age_h)
    except Exception as e:
        write_status(error=str(e))
        raise
    finally:
        write_status(running=False, phase="idle", finished_at=time.time())
