"""Runtime configuration (env vars, paths)."""
from __future__ import annotations

import os
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


DATA_DIR = Path(os.environ.get("JBC_DATA_DIR", Path.home() / ".local/share/joybuy-compare")).expanduser()
DB_PATH = Path(os.environ.get("JBC_DB", DATA_DIR / "joybuy.db")).expanduser()
PROFILE_DIR = Path(os.environ.get("JBC_PROFILE", DATA_DIR / "browser-profile")).expanduser()

# Postleitzahl for location-dependent shops (REWE) is entered by the user in the web UI
# (stored in the DB, see db.zip_code()); JBC_ZIP overrides it.
# Use DNS-over-HTTPS inside the scraping browser. Helps when a local ad-blocking
# DNS (Pi-hole, AdGuard, NextDNS ...) blocks joybuy / joy-sourcing hosts.
# "auto" (default): only if the system resolver can't resolve Joybuy's hosts; "1"/"0" force it on/off.
USE_DOH = os.environ.get("JBC_DOH", "auto").strip().lower()
# DoH servers tried in this order (the first reachable one is used); JBC_DOH_TEMPLATE forces one.
DOH_TEMPLATES = [t for t in os.environ.get("JBC_DOH_TEMPLATE", "").split(",") if t] or [
    "https://dns.google/dns-query",
    "https://cloudflare-dns.com/dns-query",
    "https://unfiltered.adguard-dns.com/dns-query",
    "https://dns.quad9.net/dns-query",
]
HEADLESS = _bool("JBC_HEADLESS", True)
USER_AGENT = os.environ.get(
    "JBC_USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36",
)

DEFAULT_KEYWORDS = [
    # Lebensmittel & Getränke
    "kaffee", "nutella", "schokolade", "chips", "nudeln", "reis", "olivenöl", "müsli", "tee",
    "haribo", "energy drink", "mineralwasser", "kekse", "honig", "erdnussbutter",
    # Drogerie & Haushalt
    "waschmittel", "spülmaschinentabs", "toilettenpapier", "zahnpasta", "shampoo", "duschgel",
    "rasierklingen", "windeln", "nivea", "küchenrolle", "müllbeutel",
    # Elektronik
    "kopfhörer", "powerbank", "usb c kabel", "ladegerät", "smartwatch", "staubsauger", "airfryer",
    "lego", "batterien",
]
