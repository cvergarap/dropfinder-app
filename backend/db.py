from __future__ import annotations

import json
import os
import sqlite3
import unicodedata
from datetime import datetime, timezone

from connectors import MockSupplierConnector
from metrics import DEFAULT_SETTINGS

DB_PATH = os.environ.get("DROPFINDER_DB", os.path.join(os.path.dirname(__file__), "dropfinder.db"))

SCHEMA_VERSION = 2

PRODUCTS_TABLE = """
CREATE TABLE IF NOT EXISTS products (
  id INTEGER PRIMARY KEY,
  supplier TEXT NOT NULL, supplier_sku TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'mock',          -- mock | csv | api
  title TEXT NOT NULL, category TEXT NOT NULL, tags TEXT, image TEXT,
  product_url TEXT NOT NULL DEFAULT '',         -- enlace a la ficha original en el proveedor
  cost REAL NOT NULL, shipping_cost REAL NOT NULL, shipping_days INTEGER NOT NULL,
  warehouse TEXT NOT NULL, stock INTEGER NOT NULL,
  orders INTEGER NOT NULL, orders_7d INTEGER NOT NULL, orders_prev_7d INTEGER NOT NULL,
  rating REAL NOT NULL, reviews INTEGER NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (supplier, supplier_sku)
);
"""

SCHEMA = PRODUCTS_TABLE + """
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
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

PRODUCT_COLS = ["supplier", "supplier_sku", "source", "title", "category", "tags", "image", "product_url",
                "cost", "shipping_cost", "shipping_days", "warehouse", "stock", "orders", "orders_7d",
                "orders_prev_7d", "rating", "reviews", "updated_at"]


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


def _migrate_v1_to_v2(conn) -> None:
    """v1: supplier_sku UNIQUE global, sin source/product_url. Se reconstruye la tabla conservando los ids
    (así los productos importados y sus alertas siguen apuntando al mismo producto)."""
    # Procedimiento recomendado por SQLite: crear nueva → copiar → borrar vieja → renombrar nueva.
    # (Renombrar la vieja reescribiría los REFERENCES de imported_products.) Requiere foreign_keys OFF.
    conn.execute(PRODUCTS_TABLE.replace("IF NOT EXISTS products", "products_new"))
    old = [c for c in ("id", *PRODUCT_COLS) if c not in ("source", "product_url")]
    conn.execute(f"INSERT INTO products_new({','.join(old)}, source, product_url) "
                 f"SELECT {','.join(old)}, 'mock', '' FROM products")
    conn.execute("DROP TABLE products")
    conn.execute("ALTER TABLE products_new RENAME TO products")
    # Las imágenes de ejemplo ahora se generan en el navegador (sin depender de internet)
    conn.execute("UPDATE products SET image = '' WHERE image LIKE 'https://placehold.co/%'")


def upsert_products(conn, products, source: str) -> tuple[int, int]:
    """Inserta o actualiza productos normalizados. Devuelve (nuevos, actualizados)."""
    ts = now()
    new = upd = 0
    for p in products:
        r = p.to_row()
        r.update(source=source, updated_at=ts)
        exists = conn.execute("SELECT 1 FROM products WHERE supplier = ? AND supplier_sku = ?",
                              (r["supplier"], r["supplier_sku"])).fetchone()
        conn.execute(
            f"INSERT INTO products({','.join(PRODUCT_COLS)}) VALUES ({','.join(':' + c for c in PRODUCT_COLS)}) "
            f"ON CONFLICT(supplier, supplier_sku) DO UPDATE SET "
            + ", ".join(f"{c} = excluded.{c}" for c in PRODUCT_COLS if c not in ("supplier", "supplier_sku")),
            r)
        if exists:
            upd += 1
        else:
            new += 1
    return new, upd


def init_db(seed_size: int = 1500) -> None:
    conn = connect()
    try:
        has_products = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'products'").fetchone()
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if has_products and version < 2:
            conn.execute("PRAGMA foreign_keys = OFF")   # debe hacerse fuera de una transacción
            try:
                _migrate_v1_to_v2(conn)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(SCHEMA)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        with conn:
            for k, v in DEFAULT_SETTINGS.items():
                conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, json.dumps(v)))
            # La demo se carga una sola vez: si el usuario la elimina, no vuelve a aparecer al reiniciar.
            seeded = conn.execute("SELECT 1 FROM meta WHERE key = 'demo_seeded'").fetchone()
            if not seeded:
                if conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 0:
                    upsert_products(conn, MockSupplierConnector(size=seed_size).iter_catalog(), "mock")
                conn.execute("INSERT INTO meta(key, value) VALUES ('demo_seeded', '1')")
    finally:
        conn.close()


def get_settings(conn) -> dict:
    return {r["key"]: json.loads(r["value"]) for r in conn.execute("SELECT key, value FROM settings")}
