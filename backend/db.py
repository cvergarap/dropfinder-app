from __future__ import annotations

import json
import os
import sqlite3
import unicodedata
from datetime import datetime, timezone

from connectors import MockSupplierConnector
from metrics import DEFAULT_SETTINGS

DB_PATH = os.environ.get("DROPFINDER_DB", os.path.join(os.path.dirname(__file__), "dropfinder.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
  id INTEGER PRIMARY KEY,
  supplier TEXT NOT NULL, supplier_sku TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL, category TEXT NOT NULL, tags TEXT, image TEXT,
  cost REAL NOT NULL, shipping_cost REAL NOT NULL, shipping_days INTEGER NOT NULL,
  warehouse TEXT NOT NULL, stock INTEGER NOT NULL,
  orders INTEGER NOT NULL, orders_7d INTEGER NOT NULL, orders_prev_7d INTEGER NOT NULL,
  rating REAL NOT NULL, reviews INTEGER NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_products_cat ON products(category);
CREATE INDEX IF NOT EXISTS ix_products_sup ON products(supplier);

CREATE TABLE IF NOT EXISTS imported_products (
  id INTEGER PRIMARY KEY,
  product_id INTEGER NOT NULL UNIQUE REFERENCES products(id),
  title TEXT NOT NULL, sell_price REAL NOT NULL,
  last_cost REAL NOT NULL, last_stock INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'draft', created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY,
  imported_id INTEGER NOT NULL REFERENCES imported_products(id) ON DELETE CASCADE,
  type TEXT NOT NULL, message TEXT NOT NULL, created_at TEXT NOT NULL, read INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def unaccent(text: str | None) -> str:
    """Minúsculas y sin tildes, para búsquedas en español ("magnetico" == "magnético")."""
    if not text:
        return ""
    nfkd = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in nfkd if not unicodedata.combining(c))


class _Conn(sqlite3.Connection):
    """`with connect() as conn:` hace commit/rollback y además cierra la conexión."""

    def __exit__(self, *exc):
        try:
            return super().__exit__(*exc)
        finally:
            self.close()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, factory=_Conn)
    conn.create_function("unaccent", 1, unaccent, deterministic=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(seed_size: int = 1500) -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        for k, v in DEFAULT_SETTINGS.items():
            conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, json.dumps(v)))
        if conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 0:
            ts = now()
            rows = []
            for p in MockSupplierConnector(size=seed_size).iter_catalog():
                r = p.to_row()
                r["updated_at"] = ts
                rows.append(r)
            cols = list(rows[0])
            conn.executemany(
                f"INSERT INTO products({','.join(cols)}) VALUES ({','.join(':' + c for c in cols)})", rows
            )


def get_settings(conn) -> dict:
    return {r["key"]: json.loads(r["value"]) for r in conn.execute("SELECT key, value FROM settings")}
