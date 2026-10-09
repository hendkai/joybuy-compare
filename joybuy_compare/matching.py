"""Fuzzy matching of Joybuy products against competitor search results."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from rapidfuzz import fuzz

from .units import BASE_PER, UNIT_MAP, Quantity, parse_quantity, parse_unit_price, unit_price

STOP = {
    "der", "die", "das", "und", "mit", "für", "fur", "von", "in", "im", "aus", "zu", "ohne", "the", "and", "for", "with",
    "neu", "new", "original", "vorteilspack", "sparpack", "großpackung", "grosspackung", "packung", "pack", "set",
    "stück", "stuck", "stk", "x", "er", "inkl", "ca", "edition", "gratis", "versand", "deutschland", "de",
    "verpackung", "kann", "variieren", "glas", "dose", "flasche", "beutel", "no", "nr",
}
PARENT_BRANDS = {"ferrero", "nestle", "mondelez", "unilever", "henkel", "beiersdorf", "oetker", "mars", "kraft", "heinz"}
VARIANT_PATTERNS = {
    "zero": r"\bzero\b|zuckerfrei|ohne zucker|sugar ?free|\bdiet\b",
    "light": r"\blight\b",
    "bio": r"\bbio\b|\borganic\b",
    "vegan": r"\bvegan\b|plant[ -]?based",
    "decaf": r"koffeinfrei|entkoffeiniert|decaf",
    "alkoholfrei": r"alkoholfrei|\b0[,.]0 ?%",
    "laktosefrei": r"laktosefrei|lactose[ -]?free",
    "mini": r"\bmini\b",
    "vollkorn": r"vollkorn|integrale|whole ?grain",
}
GENERIC_BRAND_WORDS = {"energy", "drink", "drinks", "food", "foods", "company", "cosmetics", "deutschland", "germany",
                       "group", "services", "official", "store", "shop", "brand", "gmbh", "ag", "co", "international"}


def variants(title: str) -> set[str]:
    t = fold(title)
    return {k for k, pat in VARIANT_PATTERNS.items() if re.search(pat, t)}


QTY_RE = re.compile(r"\d+(?:[.,]\d+)?\s*(?:mg|g|gr|kg|ml|cl|l|ltr|liter|stück|stk|st|tabs|rollen|wl|er)\b")


def fold(s: str) -> str:
    s = s.lower().replace("ß", "ss")
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c))


def tokens(title: str) -> list[str]:
    t = QTY_RE.sub(" ", fold(title))
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return [w for w in t.split() if w not in STOP and len(w) > 1 and not w.isdigit()]


def model_tokens(title: str) -> set[str]:
    """Tokens that look like model numbers (letters+digits), e.g. 'wh1000xm5', 'a52s'."""
    raw = re.sub(r"[^a-z0-9\- ]+", " ", fold(title))
    out = set()
    for w in raw.split():
        w2 = w.replace("-", "")
        if len(w2) >= 3 and re.search(r"[a-z]", w2) and re.search(r"\d", w2) and not QTY_RE.fullmatch(w):
            out.add(w2)
    return out


def search_query(title: str, brand: str | None = None, max_tokens: int = 7, with_size: bool = True) -> str:
    """Condensed search string: brand + first significant words (original spelling) + size."""
    words = []
    for w in re.findall(r"\w+", title):
        f = fold(w)
        if f in STOP or len(f) < 2 or f.isdigit() or f in UNIT_MAP or QTY_RE.fullmatch(f):
            continue
        words.append((f, w))
    q: list[str] = []
    if brand:
        btoks = set(tokens(brand)) | {fold(brand).replace("-", "").replace("'", "").replace(" ", "")}
        q.append(brand)
        words = [(f, w) for f, w in words if f not in btoks]
    seen = set()
    for f, w in words:
        if len(q) >= max_tokens:
            break
        if f not in seen and not re.fullmatch(r"\d+(?:[.,]\d+)?(?:mg|g|gr|kg|ml|cl|l|ltr|x)?", f):
            seen.add(f)
            q.append(w)
    qty = parse_quantity(title)
    if with_size and qty and qty.dimension != "count" and qty.pieces == 1:
        q.append(qty.label().replace(" ", ""))
    return " ".join(q)


@dataclass
class Match:
    score: float
    kind: str              # exact | unit | loose
    comparable_price: float  # competitor price normalised to the Joybuy package size


def _hit(t: str, offer_tokens: list[str], joined: str) -> bool:
    return t in offer_tokens or (len(t) >= 4 and t in joined) or any(
        len(t) >= 5 and len(o) >= 5 and o.startswith(t[:5]) and t.startswith(o[:5]) for o in offer_tokens)


def weighted_coverage(a: list[str], b: list[str]) -> float:
    """Like coverage() but long (= more specific) words weigh more."""
    if not a:
        return 1.0
    joined = "".join(b)
    total = sum(len(t) for t in a)
    return sum(len(t) for t in a if _hit(t, b, joined)) / total


def coverage(jb_tokens: list[str], offer_tokens: list[str]) -> float:
    """Share of the Joybuy title's significant words that appear in the offer title
    (prefix match handles German compounds: 'haselnuss' ~ 'haselnusscreme')."""
    if not jb_tokens:
        return 0.0
    joined = "".join(offer_tokens)
    return sum(1 for t in jb_tokens if _hit(t, offer_tokens, joined)) / len(jb_tokens)


def match(jb_title: str, jb_brand: str | None, jb_price: float, jb_qty: Quantity | None,
          offer_title: str, offer_price: float | None, offer_unit_text: str | None = None) -> Match | None:
    m = _match(jb_title, jb_brand, jb_price, jb_qty, offer_title, offer_price, offer_unit_text)
    # a >3x price gap almost always means mismatched package sizes / wrong product
    if m and not (0.3 <= jb_price / max(m.comparable_price, 0.01) <= 3.3):
        return None
    return m


def jb_quantity(title: str, price: float | None, unit_price_text: str | None) -> Quantity | None:
    """Joybuy package size, cross-checked against Joybuy's own Grundpreis."""
    q = parse_quantity(title)
    up = parse_unit_price(unit_price_text)
    if not up or not price:
        return q
    dim, per = up
    derived = Quantity(dim, price / per * BASE_PER[dim])
    if q is None or q.dimension != dim:
        return derived if dim != "count" else q
    if abs(q.amount - derived.amount) > 0.15 * derived.amount:
        return derived
    return q


def _match(jb_title: str, jb_brand: str | None, jb_price: float, jb_qty: Quantity | None,
           offer_title: str, offer_price: float | None, offer_unit_text: str | None = None) -> Match | None:
    if not offer_price or offer_price <= 0:
        return None
    # parent brands ("Ferrero Nutella") carry no information about the product itself
    jt = [t for t in tokens(jb_title) if t not in PARENT_BRANDS][:12]
    ot = [t for t in tokens(offer_title) if t not in PARENT_BRANDS]
    a, b = " ".join(jt), " ".join(ot)
    if not a or not b:
        return None
    c1, c2 = coverage(jt, ot), coverage(ot[:12], jt)
    cov = max(c1, c2)
    score = 0.45 * fuzz.token_set_ratio(a, b) + 0.25 * fuzz.token_sort_ratio(a, b) + 30 * (2 * cov + min(c1, c2)) / 3

    btoks: set[str] = set()
    if jb_brand:
        brand_toks = [t for t in tokens(jb_brand) if len(t) > 1]
        btoks = set(brand_toks)
        joined_offer = b.replace(" ", "")
        core = [t for t in brand_toks if t not in GENERIC_BRAND_WORDS] or brand_toks
        if brand_toks and not ("".join(brand_toks) in joined_offer or all(t in joined_offer for t in core)):
            return None

    # variant words (zero, light, bio …) must agree
    if variants(jb_title) != variants(offer_title):
        return None

    # the product words beyond the brand must overlap ("Spaghetti" ≠ "Fusilli")
    jnb = [t for t in jt if t not in btoks and t not in PARENT_BRANDS]
    onb = [t for t in ot[:12] if t not in btoks and t not in PARENT_BRANDS]
    nb_cov = max(weighted_coverage(jnb, onb), weighted_coverage(onb, jnb))
    if nb_cov < 0.6:
        return None

    jm, om = model_tokens(jb_title), model_tokens(offer_title)
    if jm and om and not (jm & om):
        return None
    if jm and jm & om:
        score = min(100.0, score + 10)

    oq = parse_quantity(offer_title)
    if jb_qty and oq and jb_qty.dimension == oq.dimension:
        stated = parse_unit_price(offer_unit_text)
        if stated and stated[0] == oq.dimension:
            implied = offer_price / oq.base_amount
            if abs(implied - stated[1]) > 0.15 * stated[1]:
                # title size is wrong (multipacks etc.) -> trust the shop's own unit price
                if score >= 74 and cov >= 0.67:
                    return Match(score, "unit", round(stated[1] * jb_qty.base_amount, 2))
                return None
        if abs(jb_qty.amount - oq.amount) <= 0.03 * jb_qty.amount:
            return Match(score, "exact", offer_price) if score >= 50 and cov >= 0.5 else None
        # different package size -> compare unit prices; require good name match
        if score < 72 or cov < 0.67:
            return None
        up = unit_price(offer_price, oq)
        if up is None:
            return None
        return Match(score, "unit", round(up[1] * jb_qty.base_amount, 2))
    if jb_qty and offer_unit_text:
        up = parse_unit_price(offer_unit_text)
        if up and up[0] == jb_qty.dimension and score >= 74 and cov >= 0.67:
            return Match(score, "unit", round(up[1] * jb_qty.base_amount, 2))
    if jb_qty and oq and jb_qty.dimension != oq.dimension:
        return None
    # no comparable quantity: only accept very similar names and plausible price ratio
    if score >= 85 and cov >= 0.75 and 0.25 <= jb_price / offer_price <= 4:
        return Match(score, "loose", offer_price)
    return None
