"""Joybuy.de scraper.

Joybuy (JD.com Europe) is a Next.js app. Search results arrive as JSON from
`color-api.joybuy.de/?functionId=guide_search_getItems` (signed requests, so we
let the real page issue them and just capture the responses) and the first
cards are also embedded as React-Server-Component payload in the HTML.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from urllib.parse import quote_plus

from ..browser import Browser
from ..units import parse_quantity

log = logging.getLogger(__name__)
BASE = "https://www.joybuy.de"
IMG = "https://images1.joy-sourcing.com/product/s300x300_{}.webp"


class Blocked(Exception):
    pass


def image_url(path: str | None) -> str | None:
    if not path:
        return None
    if path.startswith("http"):
        return path
    return IMG.format(path.lstrip("/"))


def _f(x):
    try:
        return float(x) if x not in (None, "") else None
    except (TypeError, ValueError):
        return None


def parse_item(v: dict, keyword: str | None = None) -> dict | None:
    """Convert an `itemCompleteView` dict into our product dict."""
    common = v.get("itemCommonView") or {}
    sku = common.get("skuId")
    title = ((v.get("itemTitleView") or {}).get("title") or "").strip()
    if not sku or not title:
        return None
    price = ((v.get("itemPriceView") or {}).get("priceInfo")) or {}
    main, cross = _f(price.get("mainPrice")), _f(price.get("crossOffPrice"))
    disc_pct = None
    if main and cross and cross > main:
        disc_pct = round((cross - main) / cross * 100, 1)
    m = re.search(r"(\d+)\s*%", price.get("discountText") or "")
    if m and disc_pct is None:
        disc_pct = float(m.group(1))
    comp = (v.get("itemPriceComplianceView") or {}).get("priceInfoList") or []
    unit_txt = comp[0].get("priceText") if comp else None
    cs = v.get("itemCommentSaleView") or {}
    track = common.get("eventTrackMap") or {}
    q = parse_quantity(title)
    promos = [l.get("text") for l in ((v.get("itemTotalPromotionView") or {}).get("labelList") or []) if l.get("text")]
    return {
        "sku": str(sku),
        "title": title,
        "brand": (v.get("itemBrandView") or {}).get("brandName") or track.get("brand_name"),
        "url": f"{BASE}/dp/{sku}",
        "image": image_url((v.get("itemImageView") or {}).get("imageUrl")),
        "price": main,
        "list_price": cross,
        "discount_pct": disc_pct,
        "discount_text": " · ".join(filter(None, [price.get("discountText"), *promos])) or None,
        "unit_price_text": unit_txt,
        "qty_label": q.label() if q else None,
        "category_id": str(common.get("categoryId") or (common.get("extMap") or {}).get("categoryId") or "") or None,
        "category_id1": (common.get("extMap") or {}).get("categoryId1"),
        "category_id2": (common.get("extMap") or {}).get("categoryId2"),
        "rating": _f(cs.get("starRating")),
        "sales_text": cs.get("saleCountText"),
        "in_stock": 1 if common.get("inStock") else 0,
        "seller": track.get("time_label"),
        "keyword": keyword,
    }


def _rsc_cards(html: str) -> list[dict]:
    """Extract item cards from the Next.js flight payload embedded in the HTML."""
    chunks = re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', html, re.S)
    full = []
    for c in chunks:
        try:
            full.append(json.loads('"' + c + '"'))
        except Exception:
            pass
    rows = {}
    for line in "".join(full).split("\n"):
        m = re.match(r"^([0-9a-f]+):([\[{].*)$", line)
        if m:
            try:
                rows[m.group(1)] = json.loads(m.group(2))
            except Exception:
                pass

    def res(o, d=0):
        if d > 12:
            return o
        if isinstance(o, str) and re.fullmatch(r"\$[0-9a-f]+", o) and o[1:] in rows:
            return res(rows[o[1:]], d + 1)
        if isinstance(o, dict):
            return {k: res(x, d + 1) for k, x in o.items()}
        if isinstance(o, list):
            return [res(x, d + 1) for x in o]
        return o

    return [res(v) for v in rows.values() if isinstance(v, dict) and "itemCommonView" in v]


def _is_blocked(url: str, title: str) -> bool:
    return "/risk/" in url or "error403" in url or "403" in title


async def warmup(page) -> None:
    """Visit the homepage first – a cold profile that deep-links into search gets the risk page."""
    await page.goto(BASE + "/", wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(6000)
    await page.mouse.wheel(0, 1500)
    await page.wait_for_timeout(1500)


def _collect(pl: dict, keyword: str, products: dict, categories: dict) -> tuple[int, int | None]:
    """Merge one guide_search_getItems payload; returns (new items, total page count)."""
    data = (pl or {}).get("data") or {}
    fc = data.get("fieldCollection") or {}
    for lvl, key in ((1, "categoryLv1List"), (2, "categoryLv2List"), (3, "categoryLv3List")):
        for c in fc.get(key) or []:
            if c.get("catId"):
                categories[c["catId"]] = {"id": c["catId"], "name": c.get("catName"), "parent_id": c.get("parentId"), "level": lvl}
    new = 0
    for cv in data.get("completeViews") or []:
        it = cv.get("itemCompleteView")
        p = parse_item(it, keyword) if it else None
        if p and p["sku"] not in products:
            products[p["sku"]] = p
            new += 1
    page_info = ((data.get("head") or {}).get("summary") or {}).get("page") or {}
    return new, page_info.get("pageCount")


async def search(browser: Browser, keyword: str, pages: int = 2) -> tuple[list[dict], list[dict]]:
    """Return (products, categories) for a keyword, loading up to `pages` result pages (à 40 items).

    Only the plain search URL is opened; further pages are loaded the way the website itself does it –
    by scrolling (infinite scroll). URLs with `&page=` are disallowed in Joybuy's robots.txt.
    """
    products: dict[str, dict] = {}
    categories: dict[str, dict] = {}
    url = f"{BASE}/s?k={quote_plus(keyword)}"

    async with browser.page() as page:
        if not getattr(browser, "_joybuy_warm", False):
            await warmup(page)
            browser._joybuy_warm = True
        payloads: list[dict] = []

        async def on_resp(r):
            if "functionId=guide_search_getItems" in r.url:
                try:
                    payloads.append(await r.json())
                except Exception:
                    pass

        page.on("response", on_resp)
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        if _is_blocked(page.url, await page.title()):
            log.info("joybuy risk page – warming up and retrying")
            await asyncio.sleep(5)
            await warmup(page)
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            if _is_blocked(page.url, await page.title()):
                raise Blocked(f"Joybuy blocked request ({page.url[:100]}). Tipp: `jbc unblock` ausführen.")
        await page.wait_for_timeout(2500)
        for card in _rsc_cards(await page.content()):  # first cards are server-rendered
            p = parse_item(card, keyword)
            if p:
                products.setdefault(p["sku"], p)

        seen = 0
        total_pages = None
        idle = 0
        # scroll until `pages` result pages have been loaded or nothing new arrives
        while len(payloads) < pages and idle < 6:
            await page.mouse.wheel(0, 3000)
            await page.wait_for_timeout(800)
            if len(payloads) > seen:
                for pl in payloads[seen:]:
                    _, tp = _collect(pl, keyword, products, categories)
                    total_pages = tp or total_pages
                seen = len(payloads)
                idle = 0
                if total_pages and seen >= total_pages:
                    break
            else:
                idle += 1
        for pl in payloads[seen:]:
            _collect(pl, keyword, products, categories)
        log.info("joybuy %r: %d Seiten geladen, %d Artikel", keyword, len(payloads), len(products))
    return list(products.values()), list(categories.values())
