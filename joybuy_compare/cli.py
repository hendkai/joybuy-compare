"""Command line interface: `jbc crawl|compare|run|serve|stats`."""
from __future__ import annotations

import argparse
import asyncio
import logging

from . import config, db
from .sources.shops import DEFAULT_SHOPS, SHOPS


def main(argv=None):
    ap = argparse.ArgumentParser(prog="jbc", description="Joybuy-Preisvergleich")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--shops", default=",".join(DEFAULT_SHOPS),
                       help=f"Kommagetrennt, Default: {','.join(DEFAULT_SHOPS)} (verfügbar: {','.join(SHOPS)})")
        p.add_argument("--limit", type=int, default=None, help="Max. Produkte zum Vergleichen")
        p.add_argument("--max-age", type=float, default=24, help="Vergleich erneuern, wenn älter als N Stunden")
        p.add_argument("--parallel", type=int, default=6, help="Artikel, die gleichzeitig verglichen werden")
        p.add_argument("--headed", action="store_true", help="Browserfenster anzeigen (Debug)")

    p_run = sub.add_parser("run", help="Joybuy crawlen und danach vergleichen")
    p_run.add_argument("keywords", nargs="*", help="Suchbegriffe (Default: eingebaute Liste)")
    p_run.add_argument("--pages", type=int, default=2, help="max. Ergebnisseiten pro Suchbegriff (à 40, per Scrollen; meist ~2 verfügbar)")
    common(p_run)

    p_crawl = sub.add_parser("crawl", help="Nur Joybuy-Produkte holen")
    p_crawl.add_argument("keywords", nargs="*")
    p_crawl.add_argument("--pages", type=int, default=2)
    p_crawl.add_argument("--parallel", type=int, default=2)
    p_crawl.add_argument("--headed", action="store_true")

    p_cmp = sub.add_parser("compare", help="Nur Preise vergleichen")
    p_cmp.add_argument("--sku", action="append", help="Nur diese SKU(s)")
    p_cmp.add_argument("--known", action="store_true", help="Nur bereits verglichene Artikel auffrischen")
    common(p_cmp)

    p_srv = sub.add_parser("serve", help="Web-UI starten")
    p_srv.add_argument("--host", default="127.0.0.1")
    p_srv.add_argument("--port", type=int, default=8787)

    sub.add_parser("stats", help="Kurze Statistik")
    sub.add_parser("rematch", help="Matching auf gespeicherten Suchergebnissen neu berechnen (ohne Scraping)")
    sub.add_parser("unblock", help="Sichtbaren Browser öffnen, um ein Joybuy-Captcha einmalig selbst zu lösen")

    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if getattr(a, "headed", False):
        config.HEADLESS = False

    from . import crawler
    from .browser import Browser

    if a.cmd == "run":
        asyncio.run(crawler.run(a.keywords or None, a.pages, a.shops.split(","), a.limit, a.max_age, a.parallel))
    elif a.cmd == "crawl":
        async def _c():
            crawler.write_status(running=True, error=None)
            try:
                async with Browser(parallel=a.parallel) as b:
                    n = await crawler.crawl_joybuy(b, a.keywords or config.DEFAULT_KEYWORDS, a.pages)
                    print(f"{n} Produkte gespeichert")
            finally:
                crawler.write_status(running=False, phase="idle")
        asyncio.run(_c())
    elif a.cmd == "compare":
        async def _cmp():
            crawler.write_status(running=True, error=None)
            try:
                async with Browser(parallel=a.parallel) as b:
                    n = await crawler.compare(b, a.shops.split(","), a.limit, a.max_age, skus=a.sku, only_known=a.known)
                    print(f"{n} Produkte verglichen")
            finally:
                crawler.write_status(running=False, phase="idle")
        asyncio.run(_cmp())
    elif a.cmd == "serve":
        import uvicorn
        uvicorn.run("joybuy_compare.server:app", host=a.host, port=a.port, log_level="info")
    elif a.cmd == "unblock":
        async def _u():
            async with Browser(parallel=1, headless=False) as b:
                page = await b.ctx.new_page()
                await page.goto("https://www.joybuy.de/s?k=nutella")
                print("Browser offen: Captcha lösen bzw. prüfen, dass Produkte sichtbar sind – dann Fenster schließen.")
                await page.wait_for_event("close", timeout=0)
        asyncio.run(_u())
    elif a.cmd == "rematch":
        n, changed = crawler.rematch()
        print(f"{n} Suchergebnisse neu bewertet, {changed} Treffer geändert")
    elif a.cmd == "stats":
        with db.session() as con:
            n = con.execute("SELECT COUNT(*) FROM products").fetchone()[0]
            c = con.execute("SELECT COUNT(*) FROM products WHERE compared_at IS NOT NULL").fetchone()[0]
            print(f"Produkte: {n}, verglichen: {c}")
            for r in con.execute("SELECT shop, COUNT(*) n FROM offers GROUP BY shop"):
                print(f"  {r['shop']}: {r['n']} Treffer")


if __name__ == "__main__":
    main()
