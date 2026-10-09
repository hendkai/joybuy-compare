"""Shared Playwright browser session (persistent profile, realistic UA, optional DoH)."""
from __future__ import annotations

import asyncio
import json
import logging

from playwright.async_api import BrowserContext, Page, async_playwright

from . import config

log = logging.getLogger(__name__)


def _prepare_profile() -> None:
    config.PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    local_state = config.PROFILE_DIR / "Local State"
    state: dict = {}
    if local_state.exists():
        try:
            state = json.loads(local_state.read_text())
        except Exception:
            state = {}
    if config.USE_DOH:
        state["dns_over_https"] = {"mode": "secure", "templates": config.DOH_TEMPLATE}
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
        if self.ctx:
            await self.ctx.close()
        if self._pw:
            await self._pw.stop()

    def page(self):
        return _PageCM(self)


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
