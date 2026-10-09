"""Debug helper: python -m joybuy_compare.debug_shop <shop> "<joybuy title>" <price> [brand]"""
import asyncio
import json
import logging
import sys

from .browser import Browser
from .crawler import pick_best
from .matching import match, search_query
from .sources.shops import SHOPS
from .units import parse_quantity


async def main():
    shop, title, price = sys.argv[1], sys.argv[2], float(sys.argv[3])
    brand = sys.argv[4] if len(sys.argv) > 4 else None
    q = search_query(title, brand)
    print("query:", q)
    async with Browser(parallel=1) as b:
        raw = await SHOPS[shop].search(b, q)
    jq = parse_quantity(title)
    for o in raw:
        m = match(title, brand, price, jq, o["title"], o["price"], o.get("unit_price_text"))
        print(f"{o['price']:>8.2f}  {str(o.get('unit_price_text'))[:28]:28}  {m.kind if m else '-':6} "
              f"{m.score if m else 0:5.1f} {m.comparable_price if m else '':>7}  {o['title'][:80]}")
    print("BEST:", json.dumps(pick_best({"title": title, "brand": brand, "price": price}, raw), ensure_ascii=False)[:400])


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
