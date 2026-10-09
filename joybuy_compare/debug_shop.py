"""Debug helper: python -m joybuy_compare.debug_shop <shop> "<joybuy title>" <price> [brand] [unit_price_text]"""
import asyncio
import json
import logging
import sys

from .browser import Browser
from .crawler import queries_for, search_and_match
from .matching import jb_quantity, match


async def main():
    shop, title, price = sys.argv[1], sys.argv[2], float(sys.argv[3])
    brand = sys.argv[4] if len(sys.argv) > 4 else None
    p = {"title": title, "brand": brand, "price": price, "unit_price_text": sys.argv[5] if len(sys.argv) > 5 else None}
    print("queries:", queries_for(shop, p))
    async with Browser(parallel=1) as b:
        query, raw, best = await search_and_match(b, shop, p)
    print("used query:", query)
    jq = jb_quantity(title, price, p["unit_price_text"])
    for o in raw:
        m = match(title, brand, price, jq, o["title"], o["price"], o.get("unit_price_text"))
        print(f"{o['price']:>8.2f}  {str(o.get('unit_price_text'))[:28]:28}  {m.kind if m else '-':6} "
              f"{m.score if m else 0:5.1f} {m.comparable_price if m else '':>7}  {o['title'][:80]}")
    print("BEST:", json.dumps(best, ensure_ascii=False)[:400])


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
