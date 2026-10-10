"""SQLite storage."""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    sku TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    brand TEXT,
    url TEXT,
    image TEXT,
    price REAL,
    list_price REAL,
    discount_pct REAL,
    discount_text TEXT,
    unit_price_text TEXT,
    qty_label TEXT,
    category_id TEXT,
    category_id1 TEXT,
    category_id2 TEXT,
    rating REAL,
    sales_text TEXT,
    in_stock INTEGER,
    seller TEXT,
    keyword TEXT,
    first_seen REAL,
    updated_at REAL,
    compared_at REAL
);
CREATE TABLE IF NOT EXISTS categories (
    id TEXT PRIMARY KEY,
    name TEXT,
    parent_id TEXT,
    level INTEGER
);
CREATE TABLE IF NOT EXISTS offers (
    sku TEXT NOT NULL,
    shop TEXT NOT NULL,
    title TEXT,
    price REAL,
    unit_price_text TEXT,
    url TEXT,
    image TEXT,
    ean TEXT,
    match_kind TEXT,
    score REAL,
    comparable_price REAL,
    fetched_at REAL,
    PRIMARY KEY (sku, shop)
);
CREATE TABLE IF NOT EXISTS searches (
    sku TEXT NOT NULL,
    shop TEXT NOT NULL,
    query TEXT,
    status TEXT,
    searched_at REAL,
    raw TEXT,
    PRIMARY KEY (sku, shop)
);
CREATE TABLE IF NOT EXISTS price_history (
    sku TEXT NOT NULL,
    shop TEXT NOT NULL,
    price REAL,
    ts REAL
);
CREATE INDEX IF NOT EXISTS ix_hist ON price_history(sku, shop, ts);
CREATE TABLE IF NOT EXISTS keywords (
    term TEXT PRIMARY KEY,
    source TEXT,            -- catalog | default | user
    added REAL,
    last_crawled REAL,
    n_items INTEGER
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    _migrate(con)
    return con


def _migrate(con: sqlite3.Connection) -> None:
    """Add columns introduced after a DB was created."""
    wanted = {"products": {"category_id1": "TEXT", "category_id2": "TEXT"}, "searches": {"raw": "TEXT"}}
    for table, cols in wanted.items():
        have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        for col, typ in cols.items():
            if col not in have:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")


@contextmanager
def session(path: Path | None = None):
    con = connect(path)
    try:
        yield con
        con.commit()
    finally:
        con.close()


def _last_price(con, sku, shop):
    r = con.execute("SELECT price FROM price_history WHERE sku=? AND shop=? ORDER BY ts DESC LIMIT 1", (sku, shop)).fetchone()
    return r["price"] if r else None


def upsert_product(con: sqlite3.Connection, p: dict) -> None:
    now = time.time()
    p = {**p, "updated_at": now}
    cols = ["sku", "title", "brand", "url", "image", "price", "list_price", "discount_pct", "discount_text",
            "unit_price_text", "qty_label", "category_id", "category_id1", "category_id2", "rating", "sales_text", "in_stock", "seller", "keyword", "updated_at"]
    vals = [p.get(c) for c in cols]
    con.execute(
        f"INSERT INTO products ({','.join(cols)}, first_seen) VALUES ({','.join('?' * len(cols))}, ?) "
        f"ON CONFLICT(sku) DO UPDATE SET {','.join(f'{c}=excluded.{c}' for c in cols if c not in ('sku', 'keyword'))}",
        vals + [now],
    )
    if p.get("price") is not None and _last_price(con, p["sku"], "joybuy") != p["price"]:
        con.execute("INSERT INTO price_history VALUES (?,?,?,?)", (p["sku"], "joybuy", p["price"], now))


def upsert_category(con, cid, name, parent_id, level):
    con.execute("INSERT INTO categories VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name, "
                "parent_id=COALESCE(excluded.parent_id, parent_id), level=excluded.level", (cid, name, parent_id, level))


def save_offer(con, sku: str, shop: str, o: dict | None, query: str, status: str, raw: list | None = None,
               record_search: bool = True) -> None:
    now = time.time()
    if record_search:
        con.execute("INSERT OR REPLACE INTO searches VALUES (?,?,?,?,?,?)",
                    (sku, shop, query, status, now, json.dumps(raw, ensure_ascii=False) if raw is not None else None))
    else:
        con.execute("UPDATE searches SET status=? WHERE sku=? AND shop=?", (status, sku, shop))
    if o is None:
        if status == "nomatch":
            con.execute("DELETE FROM offers WHERE sku=? AND shop=?", (sku, shop))
        return
    con.execute(
        "INSERT OR REPLACE INTO offers VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (sku, shop, o.get("title"), o.get("price"), o.get("unit_price_text"), o.get("url"), o.get("image"),
         o.get("ean"), o["match_kind"], o["score"], o["comparable_price"], now),
    )
    if _last_price(con, sku, shop) != o.get("price"):
        con.execute("INSERT INTO price_history VALUES (?,?,?,?)", (sku, shop, o.get("price"), now))


def get_setting(key: str, default: str | None = None) -> str | None:
    with session() as con:
        r = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_setting(key: str, value: str) -> None:
    with session() as con:
        con.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, value))


def zip_code() -> str | None:
    """PLZ entered by the user in the web UI (env JBC_ZIP overrides)."""
    import os
    return os.environ.get("JBC_ZIP") or get_setting("zip")


def add_keywords(terms: list[str], source: str) -> int:
    now = time.time()
    with session() as con:
        before = con.execute("SELECT COUNT(*) FROM keywords").fetchone()[0]
        con.executemany("INSERT OR IGNORE INTO keywords (term, source, added) VALUES (?,?,?)",
                        [(t.strip(), source, now) for t in terms if t and t.strip()])
        return con.execute("SELECT COUNT(*) FROM keywords").fetchone()[0] - before


def due_keywords(limit: int | None = None, max_age_h: float = 0) -> list[str]:
    """Least recently crawled search terms first (never crawled before all others)."""
    cutoff = time.time() - max_age_h * 3600
    with session() as con:
        rows = con.execute("SELECT term FROM keywords WHERE last_crawled IS NULL OR last_crawled < ? "
                           "ORDER BY last_crawled IS NOT NULL, last_crawled, source != 'default', term LIMIT ?",
                           (cutoff, limit or -1)).fetchall()
    return [r[0] for r in rows]


def mark_keyword(term: str, n_items: int) -> None:
    with session() as con:
        con.execute("INSERT INTO keywords (term, source, added, last_crawled, n_items) VALUES (?, 'user', ?, ?, ?) "
                    "ON CONFLICT(term) DO UPDATE SET last_crawled=excluded.last_crawled, n_items=excluded.n_items",
                    (term, time.time(), time.time(), n_items))
