"""Competitor shop scrapers. Each returns a list of raw offers:
{title, price, unit_price_text, url, image, ean}
"""
from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import quote_plus

import httpx

from .. import config, db
from ..browser import Browser
from ..units import parse_price

log = logging.getLogger(__name__)


class ShopBlocked(Exception):
    """Shop answered with a captcha / bot wall."""


class ShopUnavailable(ShopBlocked):
    """Shop can't be used in this run (e.g. missing configuration)."""


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
    short = ""
    color = "#888"

    async def search(self, browser: Browser, query: str) -> list[dict]:
        raise NotImplementedError


class Amazon(Shop):
    name, label, short, color = "amazon", "Amazon", "Amazon", "#ff9900"
    JS = r"""() => Array.from(document.querySelectorAll('div[data-component-type="s-search-result"]')).slice(0, 16).map(el => {
        const h2 = el.querySelector('h2');
        let title = h2 ? (h2.getAttribute('aria-label') || h2.innerText) : null;
        if (title) title = title.split('\n').sort((a, b) => b.length - a.length)[0].replace(/^Gesponserte Anzeige\s*[–-]\s*/, '');
        const a = el.querySelector('a.a-link-normal[href*="/dp/"]') || el.querySelector('a[href*="/dp/"]');
        const pr = el.querySelector('.a-price:not(.a-text-price) .a-offscreen');
        const unit = Array.from(el.querySelectorAll('span'))
            .map(s => (s.innerText || '').trim())
            .filter(s => s.length < 40 && /\u20ac\s*\/\s*\d*\s*(kg|g|l|ml|st|stück)\b/i.test(s))
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
            for i in items:  # "( 17,83 € 17,83€/kg)" -> "17,83 €/kg"
                m = re.search(r"([\d.,]+)\s*€\s*/\s*([\d.,]*\s*[a-zA-ZäöüÄÖÜ]+)", i.get("unit_price_text") or "")
                i["unit_price_text"] = f"{m.group(1)} €/{m.group(2).strip()}" if m else None
            return _clean([i for i in items if not i.get("sponsored")])


class Rewe(Shop):
    name, label, short, color = "rewe", "REWE", "REWE", "#cc071e"
    JS = r"""() => Array.from(document.querySelectorAll('div[id^="plr-"][class*="a-pt__product-tile_"]')).slice(0, 20).map(el => {
        const id = el.id.replace('plr-', '');
        const t = el.querySelector('h4');
        const a = el.querySelector('a[href*="/shop/p/"]');
        const pr = el.querySelector('[id$="-price"]') || el.querySelector('[class*="price__price"]');
        const g = el.querySelector('[id$="-grammage"]');
        const img = el.querySelector('img');
        const lid = (el.querySelector('input[name=listingId]') || {}).value || '';
        const m = lid.match(/^\d+-(\d{8,14})-/);
        return {title: t ? t.innerText : null, price: pr ? pr.innerText : null, unit_price_text: g ? g.innerText : null,
                url: a ? a.href : null, image: img ? img.src : null, ean: m ? m[1] : null, id};
    })"""

    MARKER = "rewe_zip.txt"

    async def _sync_zip(self, browser) -> str | None:
        """Forget REWE's chosen market when the user changed the PLZ."""
        zip_code = db.zip_code()
        marker = config.PROFILE_DIR / self.MARKER
        current = marker.read_text().strip() if marker.exists() else None
        if zip_code and current != zip_code:
            await browser.ctx.clear_cookies(domain=re.compile(r"rewe\.de$"))
            marker.unlink(missing_ok=True)
            log.info("REWE: PLZ %s -> %s, Marktwahl zurückgesetzt", current, zip_code)
        return zip_code

    async def _ensure_market(self, page, zip_code: str):
        inp = await page.query_selector("#zipCodeInput")
        if not inp or not await inp.is_visible():
            return False
        await _consent(page)
        await page.wait_for_timeout(800)
        await inp.focus()
        await page.keyboard.type(zip_code, delay=100)
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
        (config.PROFILE_DIR / self.MARKER).write_text(zip_code)
        return True

    async def search(self, browser, query):
        zip_code = await self._sync_zip(browser)
        if not zip_code:
            raise ShopUnavailable("REWE: keine Postleitzahl gesetzt (beim ersten Besuch der Web-UI eingeben)")
        url = f"https://shop.rewe.de/productList?search={quote_plus(query)}"
        async with browser.page() as page:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(2500)
            if await self._ensure_market(page, zip_code):
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(2500)
            await _consent(page)
            items = await page.evaluate(self.JS)
            return _clean(items)


class Dm(Shop):
    name, label, short, color = "dm", "dm", "dm", "#2d8a3e"

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
    name, label, short, color = "idealo", "idealo (bester Händler)", "idealo", "#ff6600"
    JS = r"""() => Array.from(document.querySelectorAll('[class*="sr-resultItemTile_"]')).slice(0, 16).map(el => {
        const t = el.querySelector('[class*="productSummary__title"]');
        const a = el.querySelector('a[href]');
        const pr = el.querySelector('[class*="detailedPriceInfo__price"]');
        const bp = el.querySelector('[class*="detailedPriceInfo__basePrice"]');
        const oc = el.querySelector('[class*="detailedPriceInfo__offerCount"]');
        const img = el.querySelector('img');
        const sm = (el.innerText || '').match(/Verkauf durch:\s*([^\n|]+)/);
        return {title: t ? t.innerText : null, price: pr ? pr.innerText : null, unit_price_text: bp ? bp.innerText : null,
                url: a ? a.href : null, image: img ? img.src : null, offers: oc ? oc.innerText : null,
                seller: sm ? sm[1].trim() : null};
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
            # idealo also lists Joybuy's own offers ("Verkauf durch: joybuy.de") – never compare Joybuy with itself
            items = [i for i in items if "joybuy" not in (i.get("seller") or "").lower()]
            return _clean(items)


class AldiSued(Shop):
    name, label, short, color = "aldi_sued", "ALDI SÜD", "ALDI S", "#00005f"
    IMG_WIDTH = 300

    async def search(self, browser, query):
        async with browser.page() as page:
            try:
                want = "q=" + quote_plus(query)
                async with page.expect_response(lambda r: "api.aldi-sued.de/v3/product-search" in r.url
                                                and want.lower() in r.url.replace("%20", "+").lower(), timeout=25000) as rv:
                    await page.goto(f"https://www.aldi-sued.de/produkte?q={quote_plus(query)}",
                                    wait_until="domcontentloaded", timeout=45000)
                data = await (await rv.value).json()
            except Exception as e:
                log.debug("aldi_sued: %s", e)
                return []
        out = []
        for p in (data.get("data") or [])[:20]:
            pr = p.get("price") or {}
            assets = p.get("assets") or []
            img = assets[0]["url"].replace("{width}", str(self.IMG_WIDTH)).replace("{slug}", p.get("urlSlugText") or "p") \
                if assets and assets[0].get("url") else None
            amount = pr.get("amountRelevant") or pr.get("amount")
            name = p.get("name") or ""
            size = p.get("sellingSize") or ""
            if size and re.search(r"\d", name):  # name usually already contains the size
                size = ""
            out.append({
                "title": f"{p.get('brandName') or ''} {name} {size}".strip(),
                "price": amount / 100 if amount else None,
                "unit_price_text": pr.get("comparisonDisplay"),
                "url": f"https://www.aldi-sued.de/produkt/{p.get('urlSlugText')}-{p.get('sku')}" if p.get("sku") else None,
                "image": img,
            })
        return _clean(out)


class AldiNord(Shop):
    name, label, short, color = "aldi_nord", "ALDI Nord", "ALDI N", "#00a0e2"
    JS = r"""() => Array.from(document.querySelectorAll('div.product-tile')).slice(0, 20).map(el => {
        const q = (s) => el.querySelector(s);
        const brand = q('.product-tile__content__upper__brand-name');
        const name = q('.product-tile__content__upper__product-name');
        const price = q('.tag__label--price');
        const unit = q('.tag__marker--salesunit');
        const base = q('.tag__marker--base-price');
        const a = q('a.product-tile__action') || q('a[href*="/produkt/"]');
        const img = q('img');
        return {title: [brand && brand.innerText, name && name.innerText, unit && unit.innerText].filter(Boolean).join(' '),
                price: price ? price.innerText : null,
                unit_price_text: base ? base.innerText.replace(/^\s*(\w+)\s*=\s*([\d.,]+)/, '1 $1 = $2 €') : null,
                url: a ? a.href : null, image: img ? img.currentSrc || img.src : null};
    })"""

    async def search(self, browser, query):
        async with browser.page() as page:
            await page.goto(f"https://www.aldi-nord.de/suchergebnisse.html?query={quote_plus(query)}",
                            wait_until="domcontentloaded", timeout=45000)
            try:
                await page.wait_for_selector("div.product-tile .tag__label--price", timeout=8000)
            except Exception:
                return []
            items = await page.evaluate(self.JS)
        for i in items:  # "3.99" is a decimal point price
            if isinstance(i.get("price"), str):
                try:
                    i["price"] = float(i["price"].replace(",", ".").strip())
                except ValueError:
                    i["price"] = None
        return _clean(items)


class Lidl(Shop):
    """lidl.de only sells non-food, drinks and promotional items online – groceries are not listed."""
    name, label, short, color = "lidl", "Lidl (Onlineshop)", "Lidl", "#0050aa"
    API = "https://www.lidl.de/q/api/search"

    async def search(self, browser, query):
        params = {"q": query, "fetchsize": 24, "locale": "de_DE", "assortment": "DE", "version": "v2.0.0"}
        async with httpx.AsyncClient(timeout=20, headers={"User-Agent": config.USER_AGENT}) as c:
            r = await c.get(self.API, params=params)
            r.raise_for_status()
            data = r.json()
        out = []
        for it in data.get("items") or []:
            g = ((it.get("gridbox") or {}).get("data")) or {}
            pr = g.get("price") or {}
            if not g.get("fullTitle") or not pr.get("price"):
                continue
            base = (pr.get("basePrice") or {}).get("text")  # "1 l = 6.39"
            pack = (pr.get("packaging") or {}).get("text")
            out.append({
                "title": " ".join(filter(None, [g["fullTitle"], pack])),
                "price": float(pr["price"]),
                "unit_price_text": re.sub(r"=\s*([\d.]+)", lambda m: "= " + m.group(1).replace(".", ",") + " €", base) if base else None,
                "url": "https://www.lidl.de" + g["canonicalUrl"] if g.get("canonicalUrl") else None,
                "image": g.get("image"),
            })
        return _clean(out)


SHOPS: dict[str, Shop] = {s.name: s for s in (Amazon(), Rewe(), Dm(), AldiSued(), AldiNord(), Lidl(), Idealo())}
# supermarkets / drugstores: only searched for products with a weight or volume
GROCERY_SHOPS = {"rewe", "dm", "aldi_sued", "aldi_nord"}


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
