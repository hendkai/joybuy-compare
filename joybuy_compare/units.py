"""Parse prices, package sizes and unit prices from German shop strings."""
from __future__ import annotations

import re
from dataclasses import dataclass

_NUM = r"(\d+(?:[.,]\d+)?)"

UNIT_MAP = {
    # unit -> (dimension, factor to base unit) ; base: g, ml, stk
    "mg": ("mass", 0.001), "g": ("mass", 1.0), "gr": ("mass", 1.0), "gramm": ("mass", 1.0),
    "kg": ("mass", 1000.0), "kilo": ("mass", 1000.0), "kilogramm": ("mass", 1000.0),
    "ml": ("volume", 1.0), "cl": ("volume", 10.0), "dl": ("volume", 100.0),
    "l": ("volume", 1000.0), "liter": ("volume", 1000.0), "ltr": ("volume", 1000.0),
    "stück": ("count", 1.0), "stk": ("count", 1.0), "st": ("count", 1.0), "tabs": ("count", 1.0),
    "tabletten": ("count", 1.0), "kapseln": ("count", 1.0), "pads": ("count", 1.0),
    "rollen": ("count", 1.0), "beutel": ("count", 1.0), "blatt": ("count", 1.0),
    "wl": ("count", 1.0), "waschladungen": ("count", 1.0), "klingen": ("count", 1.0),
    "windeln": ("count", 1.0), "teebeutel": ("count", 1.0), "sticks": ("count", 1.0),
}
_UNITS_RE = "|".join(sorted((re.escape(u) for u in UNIT_MAP), key=len, reverse=True))
BASE_LABEL = {"mass": "kg", "volume": "l", "count": "Stk"}
BASE_PER = {"mass": 1000.0, "volume": 1000.0, "count": 1.0}  # unit price is per kg / per l / per piece


def to_float(s: str) -> float:
    s = s.strip().replace("\u00a0", "").replace(" ", "")
    if "," in s and "." in s:  # 1.234,56
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", ".")
    return float(s)


def parse_price(text: str | None) -> float | None:
    if not text:
        return None
    m = re.search(r"(\d{1,3}(?:\.\d{3})*,\d{1,2}|\d+[.,]\d{1,2}|\d+)\s*(?:€|EUR)?", text.replace("\u00a0", " "))
    if not m:
        return None
    try:
        return to_float(m.group(1))
    except ValueError:
        return None


@dataclass
class Quantity:
    dimension: str  # mass | volume | count
    amount: float   # in base unit (g, ml, stk), total over multipack
    pieces: int = 1

    @property
    def base_amount(self) -> float:
        """Amount in the unit-price reference unit (kg, l, Stk)."""
        return self.amount / BASE_PER[self.dimension]

    def label(self) -> str:
        if self.dimension == "count":
            return f"{self.amount:g} Stk"
        u = "g" if self.dimension == "mass" else "ml"
        a = self.amount
        if a >= 1000:
            return f"{a / 1000:g} {'kg' if u == 'g' else 'l'}"
        return f"{a:g} {u}"


def parse_quantity(title: str | None) -> Quantity | None:
    """Best-effort package size from a product title.

    Handles '750 g', '1,5 L', '6 x 330 ml', '6x22g', '3 x Nutella … 750 g',
    'Packung mit 2', '2er Pack', '40 Tabs'.
    """
    if not title:
        return None
    t = title.lower().replace("\u00a0", " ")
    t = re.sub(r"(\d)-(?=(?:mg|g|kg|ml|cl|l|liter|stück)\b)", r"\1 ", t)  # "750-g-Glas"

    # "750 + 75 g" (bonus packs)
    m = re.search(rf"{_NUM}\s*\+\s*{_NUM}\s*({_UNITS_RE})\b", t)
    if m:
        dim, f = UNIT_MAP[m.group(3)]
        return Quantity(dim, (to_float(m.group(1)) + to_float(m.group(2))) * f)

    multi = 1
    m = re.search(r"(?:packung|pack|packs?)\s*(?:mit|à|a|von)?\s*(\d+)\b", t) \
        or re.search(r"\b(\d+)\s*er[\s-]*(?:pack|set|packung)", t) \
        or re.search(r"\b(\d+)\s*[x×]\s+[a-zäöü]", t)
    if m:
        multi = max(1, min(int(m.group(1)), 500))
    elif re.search(r"\b(duo|doppelpack|2er)\b", t):
        multi = 2
    elif re.search(r"\b(trio|3er)\b", t):
        multi = 3

    # explicit "N x <amount><unit>"
    m = re.search(rf"(\d+)\s*[x×]\s*{_NUM}\s*({_UNITS_RE})\b", t)
    if m:
        n, num, unit = int(m.group(1)), to_float(m.group(2)), m.group(3)
        dim, f = UNIT_MAP[unit]
        if n == 1:
            n = multi
        return Quantity(dim, n * num * f, n)
    # "<amount><unit> x N"  e.g. "16 g × 20"
    m = re.search(rf"{_NUM}\s*({_UNITS_RE})\s*[x×]\s*(\d+)\b", t)
    if m:
        num, unit, n = to_float(m.group(1)), m.group(2), int(m.group(3))
        dim, f = UNIT_MAP[unit]
        return Quantity(dim, n * num * f, n)

    amounts = [(to_float(a), u) for a, u in re.findall(rf"{_NUM}\s*({_UNITS_RE})\b", t)]
    # prefer mass/volume over count
    for want in ("mass", "volume", "count"):
        for num, unit in amounts:
            dim, f = UNIT_MAP[unit]
            if dim == want and num > 0:
                if dim == "count":
                    return Quantity(dim, num * multi, multi)
                return Quantity(dim, num * f * multi, multi)
    if multi > 1:
        return Quantity("count", float(multi), multi)
    return None


def parse_unit_price(text: str | None) -> tuple[str, float] | None:
    """Return (dimension, € per kg|l|Stk) from strings like '(7,45 €/kg)', '1 kg = 7,72 €',
    '14,88 € je 1 l', '7,72 € pro kg', '1,05 €/100 g'."""
    if not text:
        return None
    t = text.lower().replace("\u00a0", " ")
    pats = [
        rf"{_NUM}\s*€\s*(?:/|pro|je)\s*{_NUM}?\s*({_UNITS_RE})\b",
        rf"{_NUM}?\s*({_UNITS_RE})\s*=\s*{_NUM}\s*€",
    ]
    m = re.search(pats[0], t)
    if m:
        price, ref, unit = to_float(m.group(1)), m.group(2), m.group(3)
    else:
        m = re.search(pats[1], t)
        if not m:
            return None
        ref, unit, price = m.group(1), m.group(2), to_float(m.group(3))
    refn = to_float(ref) if ref else 1.0
    dim, f = UNIT_MAP[unit]
    base = refn * f  # amount in g/ml/stk the price refers to
    if base <= 0:
        return None
    return dim, price / base * BASE_PER[dim]


def unit_price(price: float | None, q: Quantity | None) -> tuple[str, float] | None:
    if price is None or q is None or q.amount <= 0:
        return None
    return q.dimension, price / q.base_amount
