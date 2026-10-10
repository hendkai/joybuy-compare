"""Competitor shop scrapers. Each returns a list of raw offers:
{title, price, unit_price_text, url, image, ean}

Fast path: every shop keeps one *warm* tab on its own origin (cookies, consent and – for REWE – the
chosen market are in place) and searches are issued with `fetch()` from inside that tab. No rendering,
no images, ~0.3–1 s per search. If that fails the slow path (navigate a fresh tab and read the
rendered DOM) is used as fallback.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from urllib.parse import parse_qs, quote_plus, urlparse

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


# JS template: fetch a search page inside the warm tab and run an extractor on the parsed HTML.
FETCH_HTML = """async (url) => {
    const r = await fetch(url, {credentials: 'include'});
    const html = await r.text();
    const doc = new DOMParser().parseFromString(html, 'text/html');
    const T = (el) => el ? (el.textContent || '').replace(/\\s+/g, ' ').trim() : null;
    const A = (el) => el ? new URL(el.getAttribute('href'), url).href : null;
    return {status: r.status, html_len: html.length, flags: (%(flags)s)(html), items: (%(extract)s)(doc, T, A)};
}"""


class Shop:
    name = "shop"
    label = "Shop"
    short = ""
    color = "#888"
    min_interval = 1.0          # seconds between two requests to this shop
    ORIGIN: str | None = None   # warm tab start page (enables the fast path)
    page_fallback = True        # use the slow page-load path if the fast path fails
    default = True              # part of the default shop list
    EXTRACT: str | None = None  # JS (doc, T, A) => items, for HTML search pages
    FLAGS = "(html) => ({})"

    async def setup(self, browser: Browser, page) -> None:
        """One-time preparation of the warm tab (consent, market, API keys …)."""
        await _consent(page)

    async def warm(self, browser: Browser):
        return await browser.warm(self.name, self.ORIGIN, self.setup)

    async def fetch_html(self, browser: Browser, url: str) -> dict:
        page = await self.warm(browser)
        return await page.evaluate(FETCH_HTML % {"flags": self.FLAGS, "extract": self.EXTRACT}, url)

    async def fetch_json(self, browser: Browser, url: str, body: str | None = None) -> dict:
        page = await self.warm(browser)
        res = await page.evaluate("""async ([u, b]) => {
            const r = await fetch(u, b ? {method: 'POST', body: b, headers: {'content-type': 'text/plain'}} : {});
            return [r.status, await r.text()]; }""", [url, body])
        if res[0] >= 400:
            raise RuntimeError(f"{self.name}: HTTP {res[0]}")
        return json.loads(res[1])

    async def search(self, browser: Browser, query: str) -> list[dict]:
        if self.ORIGIN:
            try:
                return await self.search_fast(browser, query)
            except ShopBlocked:
                raise
            except Exception as e:
                if not self.page_fallback:
                    raise
                log.info("%s: schneller Weg fehlgeschlagen (%s) – lade Seite", self.name, str(e)[:100])
                if "closed" in str(e) or "Target" in str(e):
                    browser.drop_warm(self.name)
        return await self.search_page(browser, query)

    async def search_fast(self, browser: Browser, query: str) -> list[dict]:
        raise NotImplementedError

    async def search_page(self, browser: Browser, query: str) -> list[dict]:
        raise NotImplementedError


# ---------------------------------------------------------------------------------------------- Amazon
def _amazon_unit(items):
    for i in items:  # "( 17,83 € 17,83€/kg)" -> "17,83 €/kg"
        m = re.search(r"([\d.,]+)\s*€\s*/\s*([\d.,]*\s*[a-zA-ZäöüÄÖÜ]+)", i.get("unit_price_text") or "")
        i["unit_price_text"] = f"{m.group(1)} €/{m.group(2).strip()}" if m else None
    return _clean([i for i in items if not i.get("sponsored")])


class Amazon(Shop):
    name, label, short, color = "amazon", "Amazon", "Amazon", "#ff9900"
    min_interval = 2.0
    ORIGIN = "https://www.amazon.de/"
    FLAGS = "(html) => ({captcha: /validateCaptcha|api-services-support@amazon/.test(html)})"
    EXTRACT = r"""(doc, T, A) => Array.from(doc.querySelectorAll('div[data-component-type="s-search-result"]')).slice(0, 16).map(el => {
        const h2 = el.querySelector('h2');
        let title = h2 ? (h2.getAttribute('aria-label') || T(h2)) : null;
        if (title) title = title.replace(/^Gesponserte Anzeige\s*[–-]\s*/, '');
        const a = el.querySelector('a.a-link-normal[href*="/dp/"]') || el.querySelector('a[href*="/dp/"]');
        const pr = el.querySelector('.a-price:not(.a-text-price) .a-offscreen');
        const unit = Array.from(el.querySelectorAll('span')).map(s => T(s) || '')
            .filter(s => s.length < 40 && /\u20ac\s*\/\s*\d*\s*(kg|g|l|ml|st|stück)\b/i.test(s))
            .sort((x, y) => x.length - y.length)[0];
        const img = el.querySelector('img.s-image');
        const sponsored = !!el.querySelector('.puis-sponsored-label-text, [aria-label*="Gesponsert"]');
        return {title, price: T(pr), unit_price_text: unit || null, url: a ? A(a).split('/ref=')[0] : null,
                image: img ? img.getAttribute('src') : null, asin: el.getAttribute('data-asin'), sponsored};
    })"""
    # slow path: same extractor on the rendered document
    JS = "() => (" + EXTRACT + ")(document, (el) => el ? (el.innerText || '').trim() : null, (el) => el ? el.href : null)"

    async def search_fast(self, browser, query):
        res = await self.fetch_html(browser, f"https://www.amazon.de/s?k={quote_plus(query)}")
        if res["flags"].get("captcha") or res["status"] == 503:
            raise ShopBlocked("amazon captcha")
        return _amazon_unit(res["items"])

    async def search_page(self, browser, query):
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
            return _amazon_unit(await page.evaluate(self.JS))


# ---------------------------------------------------------------------------------------------- REWE
class NeedsMarket(Exception):
    pass


class Rewe(Shop):
    name, label, short, color = "rewe", "REWE", "REWE", "#cc071e"
    min_interval = 1.0
    ORIGIN = "https://www.rewe.de/shop/productList?search=milch"
    MARKER = "rewe_zip.txt"
    FLAGS = "(html) => ({no_market: html.includes('Konkreter Preis abhängig vom Standort')})"
    EXTRACT = r"""(doc, T, A) => Array.from(doc.querySelectorAll('div[id^="plr-"][class*="a-pt__product-tile_"]')).slice(0, 20).map(el => {
        const t = el.querySelector('h4');
        const a = el.querySelector('a[href*="/shop/p/"]');
        const pr = el.querySelector('[id$="-price"]') || el.querySelector('[class*="price__price"]');
        const g = el.querySelector('[id$="-grammage"]');
        const img = el.querySelector('img');
        const lid = (el.querySelector('input[name=listingId]') || {value: ''}).value || el.querySelector('input[name=listingId]')?.getAttribute('value') || '';
        const m = lid.match(/^\d+-(\d{8,14})-/);
        return {title: T(t), price: T(pr), unit_price_text: T(g), url: A(a), image: img ? img.getAttribute('src') : null,
                ean: m ? m[1] : null, id: el.id.replace('plr-', '')};
    })"""
    JS = "() => (" + EXTRACT + ")(document, (el) => el ? (el.innerText || '').trim() : null, (el) => el ? el.href : null)"

    def _zip(self) -> str:
        z = db.zip_code()
        if not z:
            raise ShopUnavailable("REWE: keine Postleitzahl gesetzt (beim ersten Besuch der Web-UI eingeben)")
        return z

    async def _sync_zip(self, browser) -> str:
        """Forget REWE's chosen market when the user changed the PLZ."""
        zip_code = self._zip()
        marker = config.PROFILE_DIR / self.MARKER
        current = marker.read_text().strip() if marker.exists() else None
        if current != zip_code:
            await browser.ctx.clear_cookies(domain=re.compile(r"rewe\.de$"))
            marker.unlink(missing_ok=True)
            browser.drop_warm(self.name)
            log.info("REWE: PLZ %s -> %s, Marktwahl zurückgesetzt", current, zip_code)
        return zip_code

    async def _ensure_market(self, page, zip_code: str) -> bool:
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

    async def setup(self, browser, page):
        await page.wait_for_timeout(2500)
        await self._ensure_market(page, self._zip())
        await _consent(page)

    async def search_fast(self, browser, query):
        await self._sync_zip(browser)
        res = await self.fetch_html(browser, f"https://www.rewe.de/shop/productList?search={quote_plus(query)}")
        if res["flags"].get("no_market"):
            raise NeedsMarket("REWE: kein Markt gewählt")
        return _clean(res["items"])

    async def search_page(self, browser, query):
        zip_code = await self._sync_zip(browser)
        url = f"https://www.rewe.de/shop/productList?search={quote_plus(query)}"
        async with browser.page() as page:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(2500)
            if await self._ensure_market(page, zip_code):
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(2500)
            await _consent(page)
            return _clean(await page.evaluate(self.JS))


# ---------------------------------------------------------------------------------------------- dm
class Dm(Shop):
    name, label, short, color = "dm", "dm", "dm", "#2d8a3e"
    # dm's search API answers only ~1 request per 6 s per client (more -> blocked, also for the website itself)
    min_interval = 6.0
    page_fallback = False
    default = False  # too rate-limited to be useful by default; enable with --shops ...,dm
    ORIGIN = "https://www.dm.de/"
    API = "https://product-search.services.dmtech.com/de/search?query={q}&searchType=search&type=search"

    @staticmethod
    def parse(data: dict) -> list[dict]:
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

    async def search_fast(self, browser, query):
        return self.parse(await self.fetch_json(browser, self.API.format(q=quote_plus(query))))

    async def search_page(self, browser, query):
        async with browser.page() as page:
            try:
                async with page.expect_response(lambda r: "product-search.services.dmtech.com/de/search?" in r.url
                                                and r.request.resource_type in ("xhr", "fetch"), timeout=20000) as resp:
                    await page.goto(f"https://www.dm.de/search?query={quote_plus(query)}", wait_until="domcontentloaded",
                                    timeout=45000)
                data = await (await resp.value).json()
            except Exception as e:
                log.debug("dm: %s", e)
                return []
        return self.parse(data) if isinstance(data, dict) else []


# ---------------------------------------------------------------------------------------------- idealo
class Idealo(Shop):
    name, label, short, color = "idealo", "idealo (bester Händler)", "idealo", "#ff6600"
    min_interval = 1.5
    ORIGIN = "https://www.idealo.de/"
    EXTRACT = r"""(doc, T, A) => Array.from(doc.querySelectorAll('[class*="sr-resultItemTile_"]')).slice(0, 16).map(el => {
        const t = el.querySelector('[class*="productSummary__title"]');
        const a = el.querySelector('a[href]');
        const pr = el.querySelector('[class*="detailedPriceInfo__price"]');
        const bp = el.querySelector('[class*="detailedPriceInfo__basePrice"]');
        const img = el.querySelector('img');
        // idealo also lists Joybuy's own offers ("Verkauf durch: joybuy.de")
        const joybuy = /Verkauf durch:\s*joybuy/i.test(el.textContent || '');
        return {title: T(t), price: T(pr), unit_price_text: T(bp), url: A(a), image: img ? img.getAttribute('src') : null, joybuy};
    })"""
    JS = "() => (" + EXTRACT + ")(document, (el) => el ? (el.innerText || '').trim() : null, (el) => el ? el.href : null)"
    URL = "https://www.idealo.de/preisvergleich/MainSearchProductCategory.html?q={q}"

    @staticmethod
    def _filter(items):
        return _clean([i for i in items if not i.get("joybuy")])  # never compare Joybuy with itself

    async def search_fast(self, browser, query):
        return self._filter((await self.fetch_html(browser, self.URL.format(q=quote_plus(query))))["items"])

    async def search_page(self, browser, query):
        async with browser.page() as page:
            await page.goto(self.URL.format(q=quote_plus(query)), wait_until="domcontentloaded", timeout=45000)
            try:
                await page.wait_for_selector('[class*="sr-resultItemTile_"]', timeout=8000)
            except Exception:
                return []
            return self._filter(await page.evaluate(self.JS))


# ---------------------------------------------------------------------------------------------- ALDI SÜD
class AldiSued(Shop):
    name, label, short, color = "aldi_sued", "ALDI SÜD", "ALDI S", "#00005f"
    min_interval = 0.7
    ORIGIN = "https://www.aldi-sued.de/produkte?q=milch"
    IMG_WIDTH = 300
    API = "https://api.aldi-sued.de/v3/product-search?currency=EUR&serviceType=walk-in&q={q}&limit=30&offset=0&sort=relevance"
    service_point: str | None = None

    async def setup(self, browser, page):
        await _consent(page)
        # the website picks a default store ("servicePoint"); reuse it for our API calls
        for _ in range(20):
            if self.service_point:
                break
            await page.wait_for_timeout(300)

    async def warm(self, browser):
        if not browser.has_warm(self.name):
            def on_req(r):
                if "api.aldi-sued.de/v3/product-search" in r.url and not self.service_point:
                    sp = parse_qs(urlparse(r.url).query).get("servicePoint")
                    if sp:
                        self.service_point = sp[0]
            browser.on_new_page(self.name, lambda p: p.on("request", on_req))
        return await super().warm(browser)

    def parse(self, data: dict) -> list[dict]:
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

    async def search_fast(self, browser, query):
        url = self.API.format(q=quote_plus(query))
        await self.warm(browser)
        if self.service_point:
            url += f"&servicePoint={self.service_point}"
        return self.parse(await self.fetch_json(browser, url))

    async def search_page(self, browser, query):
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
        return self.parse(data)


# ---------------------------------------------------------------------------------------------- ALDI Nord
class AldiNord(Shop):
    name, label, short, color = "aldi_nord", "ALDI Nord", "ALDI N", "#00a0e2"
    min_interval = 0.7
    ORIGIN = "https://www.aldi-nord.de/suchergebnisse.html?query=milch"
    algolia: dict | None = None  # {url, index} captured from the website's own search request
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

    async def warm(self, browser):
        if not browser.has_warm(self.name):
            def on_req(r):
                if "algolia.net" in r.url and "queries" in r.url and not self.algolia and r.post_data:
                    try:
                        reqs = json.loads(r.post_data)["requests"]
                        idx = next(q["indexName"] for q in reqs if "products" in q["indexName"])
                        self.algolia = {"url": r.url, "index": idx}
                    except Exception:
                        pass
            browser.on_new_page(self.name, lambda p: p.on("request", on_req))
        return await super().warm(browser)

    async def setup(self, browser, page):
        await _consent(page)
        for _ in range(20):
            if self.algolia:
                break
            await page.wait_for_timeout(300)

    async def search_fast(self, browser, query):
        await self.warm(browser)
        if not self.algolia:
            raise RuntimeError("Algolia-Konfiguration nicht gefunden")
        body = json.dumps({"requests": [{"indexName": self.algolia["index"], "hitsPerPage": 20, "query": query}]})
        data = await self.fetch_json(browser, self.algolia["url"], body)
        out = []
        for h in data["results"][0].get("hits", []):
            cp = h.get("currentPrice") or {}
            base = (cp.get("basePrice") or [{}])[0]
            assets = h.get("assets") or []
            out.append({
                "title": " ".join(filter(None, [h.get("brandName"), h.get("name"), h.get("salesUnit")])),
                "price": cp.get("priceValue"),
                "unit_price_text": f"1 {base['basePriceScale']} = {base['basePriceValue']} €"
                if base.get("basePriceValue") and base.get("basePriceScale") else None,
                "url": f"https://www.aldi-nord.de/produkt/{h['productSlug']}.html" if h.get("productSlug") else None,
                "image": assets[0]["url"] + "?wid=300" if assets and assets[0].get("url") else None,
            })
        return _clean(out)

    async def search_page(self, browser, query):
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


# ---------------------------------------------------------------------------------------------- Lidl
class Lidl(Shop):
    """lidl.de only sells non-food, drinks and promotional items online – groceries are not listed."""
    name, label, short, color = "lidl", "Lidl (Onlineshop)", "Lidl", "#0050aa"
    min_interval = 0.5
    API = "https://www.lidl.de/q/api/search"
    _client: httpx.AsyncClient | None = None

    async def search(self, browser, query):
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=20, headers={"User-Agent": config.USER_AGENT})
        params = {"q": query, "fetchsize": 24, "locale": "de_DE", "assortment": "DE", "version": "v2.0.0"}
        r = await self._client.get(self.API, params=params)
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
DEFAULT_SHOPS = [n for n, s in SHOPS.items() if s.default]
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
            await asyncio.sleep(max(2.0, shop.min_interval * 2))  # rate limits: back off
    return []
