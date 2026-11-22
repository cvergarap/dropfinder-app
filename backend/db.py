from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone

import currency
import hooks
import metrics
import search
from connectors import MockSupplierConnector
from metrics import DEFAULT_SETTINGS, METRIC_COLS
from search import unaccent  # noqa: F401  (se mantiene db.unaccent por compatibilidad)

DB_PATH = os.environ.get("DROPFINDER_DB", os.path.join(os.path.dirname(__file__), "dropfinder.db"))

SCHEMA_VERSION = 6

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
  -- Métricas precalculadas (v3): se recalculan al cambiar las reglas de precio o los datos del producto
  total_cost REAL NOT NULL DEFAULT 0, suggested_price REAL NOT NULL DEFAULT 0, profit REAL NOT NULL DEFAULT 0,
  margin REAL NOT NULL DEFAULT 0, break_even REAL NOT NULL DEFAULT 0, trend REAL NOT NULL DEFAULT 0,
  trending INTEGER NOT NULL DEFAULT 0, score INTEGER NOT NULL DEFAULT 0,
  match_key TEXT NOT NULL DEFAULT '',           -- v4: clave para agrupar el mismo producto entre proveedores
  UNIQUE (supplier, supplier_sku)
);
"""

# Columnas agregadas en v3 (ALTER TABLE ADD COLUMN en bases existentes)
METRIC_DDL = {
    "total_cost": "REAL NOT NULL DEFAULT 0", "suggested_price": "REAL NOT NULL DEFAULT 0",
    "profit": "REAL NOT NULL DEFAULT 0", "margin": "REAL NOT NULL DEFAULT 0", "break_even": "REAL NOT NULL DEFAULT 0",
    "trend": "REAL NOT NULL DEFAULT 0", "trending": "INTEGER NOT NULL DEFAULT 0", "score": "INTEGER NOT NULL DEFAULT 0",
}

# Índices para filtrar y ordenar en SQL. Los de una columna sirven también para ORDER BY col, id
# (SQLite agrega el rowid al final de cada índice).
INDEXES = """
CREATE INDEX IF NOT EXISTS ix_products_cat ON products(category);
CREATE INDEX IF NOT EXISTS ix_products_sup ON products(supplier);
CREATE INDEX IF NOT EXISTS ix_products_source ON products(source);
CREATE INDEX IF NOT EXISTS ix_products_wh ON products(warehouse);
CREATE INDEX IF NOT EXISTS ix_products_score ON products(score);
CREATE INDEX IF NOT EXISTS ix_products_margin ON products(margin);
CREATE INDEX IF NOT EXISTS ix_products_orders ON products(orders);
CREATE INDEX IF NOT EXISTS ix_products_trend ON products(trend);
CREATE INDEX IF NOT EXISTS ix_products_price ON products(suggested_price);
CREATE INDEX IF NOT EXISTS ix_products_ship ON products(shipping_days);
CREATE INDEX IF NOT EXISTS ix_products_rating ON products(rating);
CREATE INDEX IF NOT EXISTS ix_products_total ON products(total_cost);
CREATE INDEX IF NOT EXISTS ix_products_cat_sup ON products(category, supplier);
CREATE INDEX IF NOT EXISTS ix_products_match ON products(match_key);
CREATE INDEX IF NOT EXISTS ix_products_updated ON products(updated_at);
CREATE INDEX IF NOT EXISTS ix_hits_product ON saved_search_hits(product_id);
CREATE INDEX IF NOT EXISTS ix_alerts_product ON alerts(product_id);
CREATE INDEX IF NOT EXISTS ix_alerts_saved ON alerts(saved_search_id);
CREATE INDEX IF NOT EXISTS ix_alerts_imported ON alerts(imported_id);
CREATE INDEX IF NOT EXISTS ix_imported_user ON imported_products(user_id);
CREATE INDEX IF NOT EXISTS ix_alerts_user ON alerts(user_id, read);
CREATE INDEX IF NOT EXISTS ix_saved_user ON saved_searches(user_id);
CREATE INDEX IF NOT EXISTS ix_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS ix_search_log_user ON search_log(user_id, q_norm);
"""

# v5: cada usuario tiene sus propios productos importados (un producto puede importarlo más de un usuario)
IMPORTED_TABLE = """
CREATE TABLE IF NOT EXISTS imported_products (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL DEFAULT 1,
  product_id INTEGER NOT NULL REFERENCES products(id),
  title TEXT NOT NULL, sell_price REAL NOT NULL,
  last_cost REAL NOT NULL, last_stock INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'draft', created_at TEXT NOT NULL,
  UNIQUE (user_id, product_id)
);
"""

# v4: una alerta puede venir de un producto importado (monitor) o de una búsqueda guardada
ALERTS_TABLE = """
CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY,
  imported_id INTEGER REFERENCES imported_products(id) ON DELETE CASCADE,
  saved_search_id INTEGER REFERENCES saved_searches(id) ON DELETE CASCADE,
  product_id INTEGER REFERENCES products(id) ON DELETE CASCADE,
  type TEXT NOT NULL, message TEXT NOT NULL, created_at TEXT NOT NULL, read INTEGER NOT NULL DEFAULT 0,
  user_id INTEGER NOT NULL DEFAULT 1
);
"""

SCHEMA = PRODUCTS_TABLE + """
IMPORTED_TABLE_DEF

ALERTS_TABLE_DEF


CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

-- Búsquedas realizadas (para sugerir las populares en el autocompletado)
CREATE TABLE IF NOT EXISTS search_log (
  id INTEGER PRIMARY KEY, q TEXT NOT NULL, q_norm TEXT NOT NULL,
  results INTEGER NOT NULL, created_at TEXT NOT NULL,
  user_id INTEGER                    -- v6: quién buscó (autocompletado por usuario); NULL = anterior a v6
);
CREATE INDEX IF NOT EXISTS ix_search_log_q ON search_log(q_norm);

-- v4: historial diario por producto (pedidos acumulados, pedidos del día, costo, stock)
CREATE TABLE IF NOT EXISTS product_snapshots (
  product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
  date TEXT NOT NULL,                 -- YYYY-MM-DD
  orders INTEGER NOT NULL,            -- pedidos acumulados a esa fecha
  orders_day INTEGER NOT NULL,        -- pedidos de ese día
  cost REAL NOT NULL, shipping_cost REAL NOT NULL, stock INTEGER NOT NULL,
  PRIMARY KEY (product_id, date)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_snapshots_date ON product_snapshots(date);

-- v4: búsquedas guardadas (params = parámetros de /api/products en JSON)
CREATE TABLE IF NOT EXISTS saved_searches (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, params TEXT NOT NULL,
  notify INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, last_checked_at TEXT,
  user_id INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS saved_search_hits (     -- productos ya vistos por cada búsqueda guardada
  saved_search_id INTEGER NOT NULL REFERENCES saved_searches(id) ON DELETE CASCADE,
  product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
  first_seen_at TEXT NOT NULL,
  PRIMARY KEY (saved_search_id, product_id)
) WITHOUT ROWID;
"""
SCHEMA += """
-- v5: cuentas de usuario. role: 'owner' (administra catálogo, reglas de precio y usuarios) o 'member'
-- AUTOINCREMENT: un id de usuario borrado nunca se reutiliza (sus datos no pasan a otra persona)
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL UNIQUE COLLATE NOCASE, name TEXT NOT NULL,
  password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'member',
  created_at TEXT NOT NULL, last_login_at TEXT, disabled INTEGER NOT NULL DEFAULT 0
);
-- token_hash: SHA-256 del token de la cookie (el token en claro nunca se guarda)
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  csrf_token TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
  user_agent TEXT
);
CREATE TABLE IF NOT EXISTS login_attempts (
  id INTEGER PRIMARY KEY, email TEXT NOT NULL COLLATE NOCASE, ip TEXT NOT NULL,
  ok INTEGER NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_login_attempts ON login_attempts(email, created_at);
CREATE INDEX IF NOT EXISTS ix_login_attempts_ip ON login_attempts(ip, created_at);
CREATE INDEX IF NOT EXISTS ix_sessions_expires ON sessions(expires_at);
-- v6: última vez que el monitor actualizó cada producto (enfriamiento entre ejecuciones)
CREATE TABLE IF NOT EXISTS monitor_checks (
  product_id INTEGER PRIMARY KEY REFERENCES products(id) ON DELETE CASCADE, checked_at TEXT NOT NULL
);
"""
SCHEMA = SCHEMA.replace("ALERTS_TABLE_DEF", ALERTS_TABLE).replace("IMPORTED_TABLE_DEF", IMPORTED_TABLE)

PRODUCT_COLS = ["supplier", "supplier_sku", "source", "title", "category", "tags", "image", "product_url",
                "cost", "shipping_cost", "shipping_days", "warehouse", "stock", "orders", "orders_7d",
                "orders_prev_7d", "rating", "reviews", "updated_at"]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class _Conn(sqlite3.Connection):
    """`with connect() as conn:` hace commit/rollback y además cierra la conexión."""

    def __exit__(self, *exc):
        try:
            result = super().__exit__(*exc)
            if exc[0] is None and getattr(self, "df_invalidate_on_commit", False):
                search.invalidate()      # ver search.invalidate: evita cachés reconstruidas con datos viejos
            return result
        finally:
            self.close()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, factory=_Conn)
    conn.create_function("unaccent", 1, unaccent, deterministic=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")   # seguro con WAL y mucho más rápido al escribir
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


def _check_fk(conn) -> None:
    """Antes de confirmar una migración hecha con FOREIGN KEYS desactivadas, verifica la integridad."""
    bad = conn.execute("PRAGMA foreign_key_check").fetchall()
    if bad:
        raise sqlite3.IntegrityError(f"La migración dejó referencias rotas: {bad[:5]}")


def _migrate_v4_to_v5(conn) -> None:
    """v5: datos por usuario. Los datos existentes quedan del usuario 1 (el primer propietario que se
    registre los recibe). imported_products se reconstruye (UNIQUE pasa a ser por usuario) conservando
    ids, así las alertas siguen apuntando a su producto importado. Requiere foreign_keys = OFF."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(imported_products)")}
    if cols and "user_id" not in cols:
        conn.execute("DROP TABLE IF EXISTS imported_products_v5")     # restos de un intento anterior
        conn.execute(IMPORTED_TABLE.replace("IF NOT EXISTS imported_products", "imported_products_v5"))
        conn.execute("INSERT INTO imported_products_v5(id, user_id, product_id, title, sell_price, last_cost, "
                     "last_stock, status, created_at) SELECT id, 1, product_id, title, sell_price, last_cost, "
                     "last_stock, status, created_at FROM imported_products")
        conn.execute("DROP TABLE imported_products")
        conn.execute("ALTER TABLE imported_products_v5 RENAME TO imported_products")
    for table in ("alerts", "saved_searches"):
        tcols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if tcols and "user_id" not in tcols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1")


def _migrate_v5_to_v6(conn) -> None:
    """v6: search_log.user_id (el autocompletado muestra a cada usuario solo sus búsquedas). Las búsquedas
    anteriores quedan con user_id NULL: no se atribuyen a nadie (no se sabe quién las hizo)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(search_log)")}
    if cols and "user_id" not in cols:
        conn.execute("ALTER TABLE search_log ADD COLUMN user_id INTEGER")


def _migrate_v3_to_v4(conn) -> None:
    """v4: products.match_key (mismo producto en varios proveedores) y alertas que pueden venir de
    búsquedas guardadas (imported_id deja de ser obligatorio: se reconstruye la tabla conservando todo)."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(products)")}
    if "match_key" not in have:
        conn.execute("ALTER TABLE products ADD COLUMN match_key TEXT NOT NULL DEFAULT ''")
    cols = {r[1]: r for r in conn.execute("PRAGMA table_info(alerts)")}
    if cols and "saved_search_id" not in cols:
        conn.execute("DROP TABLE IF EXISTS alerts_v4")                # restos de un intento anterior
        conn.execute(ALERTS_TABLE.replace("IF NOT EXISTS alerts", "alerts_v4"))
        # Se omiten alertas huérfanas (sin producto importado): con FOREIGN KEYS activas impedirían migrar
        conn.execute("INSERT INTO alerts_v4(id, imported_id, type, message, created_at, read) "
                     "SELECT id, imported_id, type, message, created_at, read FROM alerts "
                     "WHERE imported_id IN (SELECT id FROM imported_products)")
        conn.execute("DROP TABLE alerts")
        conn.execute("ALTER TABLE alerts_v4 RENAME TO alerts")


def _migrate_v2_to_v3(conn) -> None:
    """v3: agrega las columnas de métricas precalculadas (los valores se calculan después en init_db)."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(products)")}
    for col, ddl in METRIC_DDL.items():
        if col not in have:
            conn.execute(f"ALTER TABLE products ADD COLUMN {col} {ddl}")


_METRIC_INPUTS = "id, cost, shipping_cost, orders, orders_7d, orders_prev_7d, rating, shipping_days"
_UPDATE_METRICS = ("UPDATE products SET " + ", ".join(f"{c} = ?" for c in METRIC_COLS) + " WHERE id = ?")


def recompute_metrics(conn, s: dict | None = None, ids=None) -> int:
    """Recalcula las métricas precalculadas (todas o las de `ids`) con metrics.compute en una sola
    transacción (executemany). Devuelve cuántas filas cambiaron."""
    s = s or get_settings(conn)
    sql = f"SELECT {_METRIC_INPUTS}, {', '.join(METRIC_COLS)} FROM products"
    if ids is not None:
        ids = list(ids)
        if not ids:
            return 0
        sql += f" WHERE id IN ({','.join('?' * len(ids))})"
    cur = conn.execute(sql, ids or [])
    n = 0
    for chunk in iter(lambda: cur.fetchmany(5000), []):
        updates = []
        for r in chunk:
            m = metrics.compute(r, s)
            vals = tuple(m[c] for c in METRIC_COLS)
            if vals != tuple(r[c] for c in METRIC_COLS):      # solo escribe lo que cambió
                updates.append((*vals, r["id"]))
        conn.executemany(_UPDATE_METRICS, updates)
        n += len(updates)
    if n:
        search.invalidate(conn)
    return n


_UPSERT_COLS = PRODUCT_COLS + list(METRIC_COLS)
_UPSERT = (f"INSERT INTO products({','.join(_UPSERT_COLS)}) VALUES ({','.join(':' + c for c in _UPSERT_COLS)}) "
           "ON CONFLICT(supplier, supplier_sku) DO UPDATE SET "
           + ", ".join(f"{c} = excluded.{c}" for c in _UPSERT_COLS if c not in ("supplier", "supplier_sku")))


def upsert_products(conn, products, source: str, *, run_hooks: bool = True,
                    collect_ids: list | None = None) -> tuple[int, int]:
    """Inserta o actualiza productos normalizados (con sus métricas y el índice de búsqueda).
    Devuelve (nuevos, actualizados).

    run_hooks=False: el llamador ejecuta hooks.run_catalog_write al terminar su operación completa
    (p. ej. carga CSV con reemplazo), así los módulos no ven un catálogo a medio escribir.
    collect_ids: si se pasa una lista, se le agregan los ids de los productos insertados/actualizados."""
    ts = now()
    s = get_settings(conn)
    count = [0]
    keys: list = []

    def rows():
        for p in products:
            if hasattr(p, "__dict__") and isinstance(getattr(p, "tags", None), list):
                r = dict(p.__dict__)                 # mucho más rápido que dataclasses.asdict (to_row)
                r["tags"] = ",".join(p.tags)
            else:
                r = p.to_row()
            r.update(source=source, updated_at=ts)
            keys.append((r["supplier"], r["supplier_sku"]))
            r.update(metrics.compute(r, s))
            count[0] += 1
            yield r

    before = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    conn.executemany(_UPSERT, rows())
    new = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] - before
    if search.fts_enabled(conn):
        search.sync(conn)
    search.invalidate(conn)
    # Ids exactos de lo escrito en esta llamada (la marca de tiempo tiene precisión de segundos y
    # podría incluir filas de otra escritura del mismo segundo, incluso ya borradas)
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS _df_upserted(supplier TEXT, sku TEXT, PRIMARY KEY (supplier, sku))")
    conn.execute("DELETE FROM _df_upserted")
    conn.executemany("INSERT OR IGNORE INTO _df_upserted VALUES (?, ?)", keys)
    ids = [r[0] for r in conn.execute(
        "SELECT p.id FROM _df_upserted u JOIN products p ON p.supplier = u.supplier AND p.supplier_sku = u.sku")]
    conn.execute("DELETE FROM _df_upserted")
    if collect_ids is not None:
        collect_ids.extend(ids)
    if run_hooks:
        hooks.run_catalog_write(conn, ids)  # módulos (historial, comparador, búsquedas guardadas…)
    return new, count[0] - new


def _meta(conn, key: str):
    r = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return r[0] if r else None


def _set_meta(conn, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))


def init_db(seed_size: int = 1500) -> None:
    """Crea o migra la base (v1 → v2 → v3, conservando ids, importados y alertas) y carga la demo.
    Es idempotente: se puede llamar en cada arranque."""
    conn = connect()
    try:
        conn.execute("PRAGMA journal_mode = WAL")    # lecturas sin bloquear durante las escrituras
        has_products = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'products'").fetchone()
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if has_products and version < 2:
            conn.execute("PRAGMA foreign_keys = OFF")   # debe hacerse fuera de una transacción
            try:
                conn.execute("BEGIN")                   # todo o nada: sin BEGIN, el DDL se confirma solo
                _migrate_v1_to_v2(conn)
                _check_fk(conn)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            conn.execute("PRAGMA foreign_keys = ON")
        if has_products and version < 5:
            # v4 -> v5 reconstruye imported_products (referenciada por alerts): con FOREIGN KEYS desactivadas
            conn.execute("PRAGMA foreign_keys = OFF")
            try:
                conn.execute("BEGIN")                   # todo o nada (ver arriba)
                if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'alerts'").fetchone():
                    _migrate_v3_to_v4(conn)         # alertas en formato v4 antes de agregar user_id
                _migrate_v4_to_v5(conn)
                _check_fk(conn)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            conn.execute("PRAGMA foreign_keys = ON")
        if has_products and version < 6:
            conn.execute("BEGIN")                       # todo o nada
            try:
                _migrate_v5_to_v6(conn)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        conn.executescript(SCHEMA)
        conn.execute("BEGIN")
        try:
            _migrate_v2_to_v3(conn)
            _migrate_v3_to_v4(conn)
            fts = search.create_schema(conn)
            currency.ensure_schema(conn)
            for k, v in DEFAULT_SETTINGS.items():
                conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, json.dumps(v)))
            # Métricas e índice de búsqueda: se recalculan si la base viene de una versión anterior
            if _meta(conn, "metrics_version") != metrics.FORMULA_VERSION:
                recompute_metrics(conn)
                _set_meta(conn, "metrics_version", metrics.FORMULA_VERSION)
            if fts and _meta(conn, "fts_version") != search.STEMMER_VERSION:
                search.reindex_all(conn)
                _set_meta(conn, "fts_version", search.STEMMER_VERSION)
            # La demo se carga una sola vez: si el usuario la elimina, no vuelve a aparecer al reiniciar.
            if not _meta(conn, "demo_seeded"):
                if conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 0:
                    upsert_products(conn, MockSupplierConnector(size=seed_size).iter_catalog(), "mock")
                _set_meta(conn, "demo_seeded", "1")
            # Índices al final: en una base nueva se construyen de una vez tras la carga masiva (más rápido)
            for stmt in INDEXES.strip().split(";"):
                if stmt.strip():
                    conn.execute(stmt)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.execute("PRAGMA optimize")
    finally:
        conn.close()
    search.invalidate()


def get_settings(conn) -> dict:
    s = dict(DEFAULT_SETTINGS)
    s.update({r["key"]: json.loads(r["value"]) for r in conn.execute("SELECT key, value FROM settings")})
    return s
