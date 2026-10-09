from joybuy_compare.matching import match, model_tokens, search_query
from joybuy_compare.units import parse_price, parse_quantity, parse_unit_price


def test_parse_price():
    assert parse_price("5,79 €") == 5.79
    assert parse_price("ab5,59 €") == 5.59
    assert parse_price("1.234,56 €") == 1234.56
    assert parse_price("€14.99") == 14.99


def test_parse_quantity():
    q = parse_quantity("Nutella Nuss-Nugat-Creme 750 g")
    assert (q.dimension, q.amount) == ("mass", 750)
    q = parse_quantity("Vorteilspack 3 x Nutella Haselnuss-Kakaocreme 750 g")
    assert q.amount == 2250 and q.pieces == 3
    q = parse_quantity("Coca-Cola 6 x 330 ml Dose")
    assert (q.dimension, q.amount) == ("volume", 1980)
    q = parse_quantity("nutella B-Ready Kekse 6x22g")
    assert q.amount == 132
    q = parse_quantity("Mineralwasser 1,5 L")
    assert q.amount == 1500
    q = parse_quantity("Ferrero Nutella (750g)")
    assert q.amount == 750
    q = parse_quantity("Finish Spülmaschinentabs 60 Tabs")
    assert (q.dimension, q.amount) == ("count", 60)
    assert parse_quantity("Sony WH-1000XM5 Kopfhörer") is None


def test_unit_price():
    assert parse_unit_price("(7,45 €/kg)") == ("mass", 7.45)
    assert parse_unit_price("750g (1 kg = 7,72 €)") == ("mass", 7.72)
    assert parse_unit_price("0,4 l (14,88 € je 1 l)") == ("volume", 14.88)
    assert parse_unit_price("7,72 € pro kg") == ("mass", 7.72)
    d, v = parse_unit_price("1,05 €/100 g")
    assert d == "mass" and abs(v - 10.5) < 1e-9


def test_match_exact_and_unit():
    jq = parse_quantity("Nutella Nuss-Nugat-Creme 750 g")
    m = match("Nutella Nuss-Nugat-Creme 750 g", "Nutella", 5.79, jq, "Nutella 750g", 5.79)
    assert m and m.kind == "exact"
    m = match("Nutella Nuss-Nugat-Creme 750 g", "Nutella", 5.79, jq, "Nutella Nuss-Nugat-Creme 450g", 3.99)
    assert m and m.kind == "unit" and abs(m.comparable_price - 6.65) < 0.01
    # wrong brand -> no match
    assert match("Nutella Nuss-Nugat-Creme 750 g", "Nutella", 5.79, jq, "Nusspli Nuss-Nougat 750g", 2.49) is None


def test_model_numbers():
    assert "wh1000xm5" in model_tokens("Sony WH-1000XM5 Kopfhörer")
    assert match("Sony WH-1000XM5 Kopfhörer schwarz", "Sony", 299, None,
                 "Sony WH-1000XM4 Kopfhörer schwarz", 199) is None


def test_search_query():
    q = search_query("Nutella Nuss-Nugat-Creme 750 g", "Nutella")
    assert q.startswith("Nutella") and "750g" in q


def test_jb_quantity_crosscheck():
    from joybuy_compare.matching import jb_quantity
    # title claims 400 g, but Joybuy's own Grundpreis says 0,75 €/kg at 0,30 € -> 400 g ok
    assert jb_quantity("Reis 400 g", 0.30, "0,75 € pro kg").amount == 400
    # title wrong: 1 kg at 7,72 €/kg would be 7,72 €, price is 5,79 -> 750 g
    assert round(jb_quantity("Nutella 1 kg", 5.79, "7,72 € pro kg").amount) == 750


def test_price_ratio_guard():
    jq = parse_quantity("Indomie Mi Goreng 400 g")
    assert match("Indomie Mi Goreng 400 g", "Indomie", 0.60, jq, "IndoMie Mi Goreng 80g x 40", 29.0) is None


def test_quantity_suffix_multiplier_and_query():
    q = parse_quantity("Trung Nguyen G7 Instantkaffee 3-in-1 16 g × 20")
    assert q.amount == 320
    assert search_query("CHIN-SU Chilisauce 480 g", "CHIN-SU") == "CHIN-SU Chilisauce 480g"


def _m(jb, brand, price, offer, oprice, unit=None):
    return match(jb, brand, price, parse_quantity(jb), offer, oprice, unit)


def test_false_positives_rejected():
    # other brand that merely contains a brand word
    assert _m("Coca-Cola Zero Sugar Zitrone 6x1,25L", "Coca-Cola", 3.99,
              "Pepsi Zero Zucker Lemon, 6x1,25l – koffeinhaltige Cola Zitrone", 8.94) is None
    # variant mismatch
    assert _m("Paulaner Spezi 24x0,33L", "Paulaner", 11.56, "Paulaner Spezi Zero, Cola & Orange 24x0,33l", 15.89) is None
    assert _m("Fanta Orange 24x0,33L", "Fanta", 13.49, "Fanta Orange Zero sugar (24x0,33l Dosen)", 20.0) is None
    # different product of the same brand
    assert _m("Barilla No. 5 Spaghetti Pasta 500g", "Barilla", 1.11, "Barilla Capellini No. 1 (500 g)", 1.05) is None
    assert _m("Barilla No. 5 Spaghetti Pasta 500g", "Barilla", 1.11,
              "Barilla Pasta Nudeln Klassische Fusilli n.98 aus hochwertigem Hartweizen 500g", 1.99) is None
    # duo set is 2x
    assert parse_quantity("AVENE Cicalfate+ Restorative Schutzcreme 100ml Duo Set").amount == 200
    assert parse_quantity("EAGLOBE - Rote Bohnen, (1 X 400 GR) (Packung mit 3)").amount == 1200


def test_true_positives_kept():
    assert _m("Barilla No. 5 Spaghetti Pasta 500g", "Barilla", 1.11, "Barilla Spaghetti Nr.5 500g", 1.99).kind == "exact"
    assert _m("Nutella Nuss-Nugat-Creme 750 g", "Nutella", 5.79, "Ferrero Nutella (750g)", 5.59, "(7,45 €/kg)").kind == "exact"
    assert _m("Coca-Cola Zero Sugar Zitrone 6x1,25L", "Coca-Cola", 3.99, "Coca-Cola Zitrone Ohne Zucker 6x1,25l", 8.32)
    assert _m("Monster Energy Mango Loco 12x0,5L", "Monster Energy", 9.99, "Monster Juiced Mango Loco 12x0,5l", 15.99)


def test_brand_and_variant_rules():
    assert _m("Golden Lily Parfüm-Jasmin-Duftreis 18 kg", "Golden Lily", 21.99,
              "Golden Phoenix Reis Duft, 1er Pack (1 x 18 kg)", 40.0) is None
    assert _m("Barilla No. 5 Spaghetti Pasta 500g", "Barilla", 1.11,
              "Barilla Integrale Vollkorn Spaghetti n.5 (500g)", 1.69) is None
    assert _m("Thinmax Hokkaido Toastbrot 85 g", "Thinmax", 1.69, "Thinmax Goldener Dänischer Toast Keks 85 g", 1.99) is None


def test_aldi_titles():
    assert parse_quantity("FERRERO Nutella 750-g-Glas").amount == 750
    assert parse_quantity("FERRERO Nutella 750 + 75 g").amount == 825
    assert _m("Nutella Nuss-Nugat-Creme 750 g", "Nutella", 5.79, "FERRERO Nutella 750-g-Glas", 5.79, "1 kg = 7.72 €").kind == "exact"
    m = _m("Nutella Nuss-Nugat-Creme 750 g", "Nutella", 5.79, "FERRERO Nutella 750 + 75 g", 5.79, "7,02 €/1 kg")
    assert m.kind == "unit" and abs(m.comparable_price - 5.27) < 0.01
