"""Competitor shop scrapers. Each returns a list of raw offers:
{title, price, unit_price_text, url, image, ean}
"""
from __future__ import annotations

import asyncio
import logging
from urllib.parse import quote_plus

from .. import config
from ..browser import Browser
from ..units import parse_price

log = logging.getLogger(__name__)


class ShopBlocked(Exception):
    """Shop answered with a captcha / bot wall."""


async def _consent(page, labels=("Alle akzeptieren", "Akzeptieren", "Alle erlauben", "Zustimmen", "Einverstanden")):
    for sel in ("#sp-cc-accept", "#onetrust-accept-all-handler", "[data-testid=uc-accept-all-button]",
                "button[data-testid='uc-accept-all-button']"):
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                await el.click(timeout=2000)
                return
        except Exception:
            pass
    for t in labels:
        try:
            b = page.get_by_role("button", name=t, exact=False).first
            if await b.is_visible(timeout=300):
                await b.click(timeout=2000)
                return
        except Exception:
            pass


def _clean(items: list[dict]) -> list[dict]:
    out = []
    for it in items:
        price = it.get("price")
        if isinstance(price, str):
            price = parse_price(price)
        if not it.get("title") or not price:
            continue
        unit = " ".join((it.get("unit_price_text") or "").split()) or None
        out.append({**it, "price": price, "title": " ".join(it["title"].split()), "unit_price_text": unit})
    return out


class Shop:
    name = "shop"
    label = "Shop"
    color = "#888"

    async def search(self, browser: Browser, query: str) -> list[dict]:
        raise NotImplementedError


class Amazon(Shop):
    name, label, color = "amazon", "Amazon", "#ff9900"
    JS = """() => Array.from(document.querySelectorAll('div[data-component-type="s-search-result"]')).slice(0, 16).map(el => {
        const h2 = el.querySelector('h2');
        let title = h2 ? (h2.getAttribute('aria-label') || h2.innerText) : null;
        if (title) title = title.split('\\n').sort((a, b) => b.length - a.length)[0].replace(/^Gesponserte Anzeige\\s*[–-]\\s*/, '');
        const a = el.querySelector('a.a-link-normal[href*="/dp/"]') || el.querySelector('a[href*="/dp/"]');
        const pr = el.querySelector('.a-price:not(.a-text-price) .a-offscreen');
        const unit = Array.from(el.querySelectorAll('span'))
            .map(s => (s.innerText || '').trim())
            .filter(s => s.length < 40 && /\\u20ac\\s*\\/\\s*\\d*\\s*(kg|g|l|ml|st|stück)\\b/i.test(s))
            .sort((x, y) => x.length - y.length)[0];
        const img = el.querySelector('img.s-image');
        const sponsored = !!el.querySelector('.puis-sponsored-label-text, [aria-label*="Gesponsert"]');
        return {title, price: pr ? pr.textContent : null, unit_price_text: unit || null,
                url: a ? a.href.split('/ref=')[0] : null, image: img ? img.src : null, asin: el.dataset.asin, sponsored};
    })"""

    async def search(self, browser, query):
        async with browser.page() as page:
            await page.goto(f"https://www.amazon.de/s?k={quote_plus(query)}", wait_until="domcontentloaded", timeout=45000)
            html = await page.content()
            if "validateCaptcha" in html or "api-services-support@amazon.com" in html:
                raise ShopBlocked("amazon captcha")
            await _consent(page)
            try:
                await page.wait_for_selector('div[data-component-type="s-search-result"]', timeout=8000)
            except Exception:
                return []
            items = await page.evaluate(self.JS)
            return _clean([i for i in items if not i.get("sponsored")])


class Rewe(Shop):
    name, label, color = "rewe", "REWE", "#cc071e"
    JS = """() => Array.from(document.querySelectorAll('div[id^="plr-"][class*="a-pt__product-tile_"]')).slice(0, 20).map(el => {
        const id = el.id.replace('plr-', '');
        const t = el.querySelector('h4');
        const a = el.querySelector('a[href*="/shop/p/"]');
        const pr = el.querySelector('[id$="-price"]') || el.querySelector('[class*="price__price"]');
        const g = el.querySelector('[id$="-grammage"]');
        const img = el.querySelector('img');
        const lid = (el.querySelector('input[name=listingId]') || {}).value || '';
        const m = lid.match(/^\\d+-(\\d{8,14})-/);
        return {title: t ? t.innerText : null, price: pr ? pr.innerText : null, unit_price_text: g ? g.innerText : null,
                url: a ? a.href : null, image: img ? img.src : null, ean: m ? m[1] : null, id};
    })"""

    async def _ensure_market(self, page):
        inp = await page.query_selector("#zipCodeInput")
        if not inp or not await inp.is_visible():
            return False
        await _consent(page)
        await page.wait_for_timeout(800)
        await inp.focus()
        await page.keyboard.type(config.ZIP_CODE, delay=100)
        await page.wait_for_timeout(500)
        await page.get_by_text("Abhol- und Lieferservice finden").click(force=True)
        await page.wait_for_timeout(3500)
        for t in ("Lieferservice", "Abholservice"):
            try:
                await page.get_by_text(t, exact=False).first.click(timeout=3000, force=True)
                break
            except Exception:
                pass
        await page.wait_for_timeout(3000)
        return True

    async def search(self, browser, query):
        url = f"https://shop.rewe.de/productList?search={quote_plus(query)}"
        async with browser.page() as page:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(2500)
            if await self._ensure_market(page):
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(2500)
            await _consent(page)
            items = await page.evaluate(self.JS)
            return _clean(items)


class Dm(Shop):
    name, label, color = "dm", "dm", "#002878"

    async def search(self, browser, query):
        async with browser.page() as page:
            try:
                async with page.expect_response(lambda r: "product-search.services.dmtech.com/de/search?" in r.url
                                                and r.request.resource_type in ("xhr", "fetch"),
                                                timeout=20000) as resp:
                    await page.goto(f"https://www.dm.de/search?query={quote_plus(query)}", wait_until="domcontentloaded",
                                    timeout=45000)
                data = await (await resp.value).json()
                if not isinstance(data, dict):
                    return []
            except Exception as e:
                log.debug("dm: %s", e)
                return []
        out = []
        for p in (data.get("products") or [])[:20]:
            td = p.get("tileData") or {}
            price = (((td.get("price") or {}).get("price") or {}).get("current") or {}).get("value")
            infos = (td.get("price") or {}).get("tileInfos") or []
            imgs = td.get("images") or []
            out.append({
                "title": f"{p.get('brandName') or ''} {p.get('title') or ''}".strip(),
                "price": price,
                "unit_price_text": infos[0] if infos else None,
                "url": "https://www.dm.de" + td["self"] if td.get("self") else None,
                "image": imgs[0].get("tileSrc") if imgs else None,
                "ean": str(p.get("gtin")) if p.get("gtin") else None,
            })
        return _clean(out)


class Idealo(Shop):
    name, label, color = "idealo", "idealo (bester Händler)", "#0a3761"
    JS = """() => Array.from(document.querySelectorAll('[class*="sr-resultItemTile_"]')).slice(0, 16).map(el => {
        const t = el.querySelector('[class*="productSummary__title"]');
        const a = el.querySelector('a[href]');
        const pr = el.querySelector('[class*="detailedPriceInfo__price"]');
        const bp = el.querySelector('[class*="detailedPriceInfo__basePrice"]');
        const oc = el.querySelector('[class*="detailedPriceInfo__offerCount"]');
        const img = el.querySelector('img');
        return {title: t ? t.innerText : null, price: pr ? pr.innerText : null, unit_price_text: bp ? bp.innerText : null,
                url: a ? a.href : null, image: img ? img.src : null, offers: oc ? oc.innerText : null};
    })"""

    async def search(self, browser, query):
        async with browser.page() as page:
            await page.goto(f"https://www.idealo.de/preisvergleich/MainSearchProductCategory.html?q={quote_plus(query)}",
                            wait_until="domcontentloaded", timeout=45000)
            try:
                await page.wait_for_selector('[class*="sr-resultItemTile_"]', timeout=8000)
            except Exception:
                return []
            items = await page.evaluate(self.JS)
            return _clean(items)


SHOPS: dict[str, Shop] = {s.name: s for s in (Amazon(), Rewe(), Dm(), Idealo())}


async def search_with_retry(shop: Shop, browser: Browser, query: str, retries: int = 1) -> list[dict]:
    for attempt in range(retries + 1):
        try:
            return await shop.search(browser, query)
        except ShopBlocked:
            raise
        except Exception as e:  # network hiccups, timeouts
            log.warning("%s search %r failed (%s)", shop.name, query, str(e)[:120])
            if attempt == retries:
                raise
            await asyncio.sleep(3)
    return []
