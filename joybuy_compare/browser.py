"""Shared Playwright browser session (persistent profile, realistic UA, optional DoH)."""
from __future__ import annotations

import asyncio
import json
import logging
import socket

from playwright.async_api import BrowserContext, Page, async_playwright

from . import config

log = logging.getLogger(__name__)


DOH_TEST_HOSTS = ("www.joybuy.de", "color-api.joybuy.de", "st.joy-sourcing.com", "images1.joy-sourcing.com")


def _want_doh() -> bool:
    mode = config.USE_DOH
    if mode in ("1", "true", "yes", "on"):
        return True
    if mode in ("0", "false", "no", "off"):
        return False
    for host in DOH_TEST_HOSTS:  # auto
        try:
            socket.getaddrinfo(host, 443)
        except OSError:
            log.info("System-DNS löst %s nicht auf – nutze DNS-over-HTTPS im Browser", host)
            return True
    return False


def _pick_doh() -> str | None:
    """First DoH server that answers (ad blockers often block some of them)."""
    import httpx
    for t in config.DOH_TEMPLATES:
        try:
            r = httpx.get(t, params={"name": "www.joybuy.de", "type": "A"}, headers={"accept": "application/dns-json"},
                          timeout=4)
            if r.status_code < 500:
                log.info("DNS-over-HTTPS: %s", t)
                return t
        except Exception:
            continue
    log.warning("Kein DNS-over-HTTPS-Server erreichbar – nutze System-DNS")
    return None


def _prepare_profile() -> None:
    config.PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    local_state = config.PROFILE_DIR / "Local State"
    state: dict = {}
    if local_state.exists():
        try:
            state = json.loads(local_state.read_text())
        except Exception:
            state = {}
    template = _pick_doh() if _want_doh() else None
    if template:
        state["dns_over_https"] = {"mode": "secure", "templates": template}
    else:
        state.pop("dns_over_https", None)
    local_state.write_text(json.dumps(state))


class Browser:
    """Async context manager that owns one persistent Chromium context.

    `page()` hands out pages; use `async with browser.page() as p:`.
    A semaphore bounds parallel tabs.
    """

    def __init__(self, parallel: int = 3, headless: bool | None = None):
        self.parallel = parallel
        self.headless = config.HEADLESS if headless is None else headless
        self._sem = asyncio.Semaphore(parallel)
        self._pw = None
        self.ctx: BrowserContext | None = None
        self._joybuy_warm = False
        # one long-lived tab per competitor shop (see sources/shops.py "fast path")
        self._warm: dict[str, Page] = {}
        self._warm_locks: dict[str, asyncio.Lock] = {}
        self._page_hooks: dict[str, list] = {}

    async def __aenter__(self) -> "Browser":
        _prepare_profile()
        self._pw = await async_playwright().start()
        self.ctx = await self._pw.chromium.launch_persistent_context(
            str(config.PROFILE_DIR),
            headless=self.headless,
            channel="chromium",  # full chromium ("new headless") – honours DoH + looks like a real browser
            args=["--disable-blink-features=AutomationControlled"],
            locale="de-DE",
            timezone_id="Europe/Berlin",
            user_agent=config.USER_AGENT,
            viewport={"width": 1366, "height": 900},
        )
        return self

    async def __aexit__(self, *exc):
        for p in list(self._warm.values()):
            try:
                await p.close()
            except Exception:
                pass
        if self.ctx:
            await self.ctx.close()
        if self._pw:
            await self._pw.stop()

    def page(self):
        return _PageCM(self)

    # ---- warm tabs ----
    def has_warm(self, name: str) -> bool:
        p = self._warm.get(name)
        return bool(p and not p.is_closed())

    def on_new_page(self, name: str, hook) -> None:
        """Register a callback that is applied to the next warm tab created for `name`."""
        self._page_hooks.setdefault(name, []).append(hook)

    def drop_warm(self, name: str) -> None:
        p = self._warm.pop(name, None)
        if p and not p.is_closed():
            asyncio.ensure_future(p.close())

    async def warm(self, name: str, origin: str, setup=None) -> Page:
        """Return the warm tab for a shop, creating (and preparing) it on first use."""
        lock = self._warm_locks.setdefault(name, asyncio.Lock())
        async with lock:
            if self.has_warm(name):
                return self._warm[name]
            page = await self.ctx.new_page()
            for hook in self._page_hooks.pop(name, []):
                hook(page)
            # the warm tab only needs HTML/JS – skip images, fonts and media
            await page.route("**/*", _block_heavy)
            await page.goto(origin, wait_until="domcontentloaded", timeout=45000)
            if setup:
                await setup(self, page)
            self._warm[name] = page
            log.debug("warm tab ready: %s", name)
            return page


async def _block_heavy(route):
    if route.request.resource_type in ("image", "font", "media"):
        await route.abort()
    else:
        await route.continue_()


class _PageCM:
    def __init__(self, b: Browser):
        self.b = b
        self.p: Page | None = None

    async def __aenter__(self) -> Page:
        await self.b._sem.acquire()
        self.p = await self.b.ctx.new_page()
        return self.p

    async def __aexit__(self, *exc):
        try:
            if self.p:
                await self.p.close()
        finally:
            self.b._sem.release()
