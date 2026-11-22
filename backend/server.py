"""DropFinder — servidor HTTP (solo librería estándar).

Uso:  python3 server.py [--port 8000]
"""
from __future__ import annotations

import argparse
import hmac
import json
import math
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import auth
import auth_context
import compare
import dashboard
import collections_mod
import currency
import db
from auth_context import current_user_id
from errors import ApiError, FileResponse  # noqa: F401  (los módulos y pruebas usan server.ApiError)
import history
import hooks
import saved_searches
import store_sync
import stores_mod
import search
from connectors import CsvSupplierConnector, MockSupplierConnector
from connectors.base import PriceStock
from metrics import DEFAULT_SETTINGS, profit_for

FRONTEND = os.path.join(os.path.dirname(__file__), "..", "frontend")
# sort -> (columna, descendente). "relevance" (orden por BM25) solo aplica cuando hay texto de búsqueda;
# es el orden por defecto si hay `q` y no se pide otro. Sin `q` el orden por defecto es "score".
SORTS = {
    "relevance": (None, False),
    "score": ("score", True), "orders": ("orders", True), "trend": ("trend", True),
    "margin": ("margin", True), "profit": ("profit", True), "price_asc": ("suggested_price", False),
    "price_desc": ("suggested_price", True), "shipping": ("shipping_days", False), "rating": ("rating", True),
}
MAX_BODY = 20 * 1024 * 1024   # 20 MB (catálogos CSV grandes)
# Fuentes con consulta en vivo de precio/stock. Los CSV se actualizan volviendo a subir el archivo.
LIVE_CONNECTORS = {"mock": MockSupplierConnector()}


def _num(qs, key, cast=float):
    v = qs.get(key, [""])[0].strip()
    if v == "":
        return None
    try:
        return cast(v)
    except ValueError:
        raise ApiError(400, f"Parámetro inválido: {key}")


# ---------------------------------------------------------------- handlers

def _item(r) -> dict:
    """Fila de products (con métricas precalculadas) → JSON de la API (mismo formato que antes)."""
    d = dict(r)
    d["tags"] = [t for t in (d.get("tags") or "").split(",") if t]
    d["trending"] = bool(d["trending"])
    return d


def _filters(qs: dict) -> tuple[list, list]:
    """Filtros de la búsqueda como SQL sobre las columnas (indexadas) de products."""
    where, args = [], []
    for key in ("category", "supplier", "warehouse", "source"):
        vals = [v for v in qs.get(key, []) if v]
        if vals:
            where.append(f"p.{key} IN ({','.join('?' * len(vals))})")
            args += vals
    for key, col, op, cast in [
        ("min_cost", "p.total_cost", ">=", float), ("max_cost", "p.total_cost", "<=", float),
        ("min_orders", "p.orders", ">=", int), ("min_rating", "p.rating", ">=", float),
        ("max_ship_days", "p.shipping_days", "<=", int), ("min_margin", "p.margin", ">=", float),
        ("min_score", "p.score", ">=", float),
    ]:
        v = _num(qs, key, cast)
        if v is not None:
            where.append(f"{col} {op} ?")
            args.append(v)
    if qs.get("in_stock", ["0"])[0] == "1":
        where.append("p.stock > 0")
    if qs.get("trending", ["0"])[0] == "1":
        where.append("p.trending = 1")
    return where, args


def _query(conn, tokens, use_fts, where, args, sort, page, size, count_only=False):
    """Ejecuta la búsqueda en SQL: devuelve (total, items de la página)."""
    where, args = list(where), list(args)
    source = "products p"
    if tokens and use_fts:
        source = "products_fts JOIN products p ON p.id = products_fts.rowid"
        where.insert(0, "products_fts MATCH ?")
        args.insert(0, search.build_match(tokens))
    elif tokens:   # SQLite sin FTS5: búsqueda simple como en la versión anterior
        for t in tokens:
            where.append("(unaccent(p.title) LIKE ? OR unaccent(p.tags) LIKE ?)")
            args += [f"%{t}%", f"%{t}%"]
    cond = (" WHERE " + " AND ".join(where)) if where else ""
    if tokens and use_fts and len(where) == 1:      # solo texto: se cuenta directo en el índice
        total = conn.execute("SELECT COUNT(*) FROM products_fts WHERE products_fts MATCH ?", args).fetchone()[0]
    else:
        total = conn.execute(f"SELECT COUNT(*) FROM {source}{cond}", args).fetchone()[0]
    if count_only or not total:
        return total, []
    if sort == "relevance":
        order = f"{search.rank_sql()}, p.id DESC"
    else:
        col, desc = SORTS[sort]
        order = f"p.{col} DESC, p.id DESC" if desc else f"p.{col}, p.id"
    rows = conn.execute(
        f"SELECT p.*, i.id AS imported_id FROM {source} LEFT JOIN imported_products i "
        f"ON i.product_id = p.id AND i.user_id = ?{cond} ORDER BY {order} LIMIT ? OFFSET ?",
        [current_user_id()] + args + [size, (page - 1) * size]).fetchall()
    return total, [_item(r) for r in rows]


MAX_PAGE = 100_000            # evita OFFSET fuera de rango en SQLite


def _best_effort(conn, fn, *args) -> None:
    """Escrituras secundarias de una búsqueda (indexar pendientes, registrar la consulta).
    Si otra escritura larga tiene la base bloqueada (carga de CSV, monitor), se omiten en vez de
    hacer esperar la búsqueda 5 s y fallar con 500: la búsqueda es lectura y debe responder siempre."""
    conn.execute("PRAGMA busy_timeout = 200")
    try:
        conn.execute("SAVEPOINT best_effort")
        fn(conn, *args)
        conn.execute("RELEASE best_effort")
    except sqlite3.OperationalError:
        conn.execute("ROLLBACK TO best_effort")
        conn.execute("RELEASE best_effort")
    finally:
        conn.execute("PRAGMA busy_timeout = 5000")


def search_products(qs: dict, *, log: bool = True) -> dict:
    """Búsqueda con texto completo, filtros, orden y paginación en SQL.

    Además de total/page/page_size/pages/items devuelve:
      query           texto efectivamente buscado (el corregido si hubo autocorrección)
      sort            orden aplicado
      corrected_from  si `q` no dio resultados y se encontró una corrección que sí da, se muestran los
                      resultados de la corrección y aquí va el `q` original ("Mostrando resultados para …")
      suggestion      si hay pocos resultados (1 o 2) y existe una consulta parecida con más resultados:
                      "¿Quisiste decir …?" (no se aplica sola). null en otro caso.
      took_ms         tiempo de la consulta en el servidor (milisegundos)

    Con `exact=1` no se autocorrige (el usuario pidió buscar su texto tal cual).
    log=False: no registra la consulta para el autocompletado (p. ej. al exportar resultados).
    """
    started = time.perf_counter()
    exact = qs.get("exact", ["0"])[0] == "1"
    q = qs.get("q", [""])[0].strip()[:200]
    page = min(MAX_PAGE, max(1, _num(qs, "page", int) or 1))
    size = min(100, max(1, _num(qs, "page_size", int) or 24))
    where, args = _filters(qs)
    tokens = search.query_tokens(q)
    sort = qs.get("sort", [""])[0]
    if sort not in SORTS:
        sort = "relevance" if tokens else "score"
    if sort == "relevance" and not tokens:
        sort = "score"

    suggestion = corrected_from = None
    effective = q
    with db.connect() as conn:
        use_fts = search.fts_enabled(conn)
        if sort == "relevance" and not use_fts:
            sort = "score"             # sin FTS5 no hay ranking de relevancia
        if use_fts:
            _best_effort(conn, search.sync)    # por si alguna escritura dejó productos sin indexar
        total, items = _query(conn, tokens, use_fts, where, args, sort, page, size)
        if tokens and use_fts and not exact and total < search.FEW_RESULTS:
            fix = search.correct(conn, q, current_total=total)
            if fix and fix != q:
                fix_tokens = search.query_tokens(fix)
                if total == 0:
                    t2, i2 = _query(conn, fix_tokens, use_fts, where, args, sort, page, size)
                    if t2:
                        total, items, corrected_from, effective = t2, i2, q, fix
                elif _query(conn, fix_tokens, use_fts, where, args, sort, page, size, count_only=True)[0] > total:
                    suggestion = fix
        if tokens and page == 1 and log:   # para el autocompletado se guarda la consulta ya corregida
            _best_effort(conn, search.log_search, effective, total, db.now(), current_user_id())
    return {
        "total": total, "page": page, "page_size": size, "pages": math.ceil(total / size) if total else 0,
        "items": items, "query": effective, "sort": sort,
        "suggestion": suggestion, "corrected_from": corrected_from,
        "took_ms": round((time.perf_counter() - started) * 1000, 1),
    }


def suggest(qs: dict) -> dict:
    q = qs.get("q", [""])[0][:100]
    limit = min(20, max(1, _num(qs, "limit", int) or 8))
    with db.connect() as conn:
        return {"items": search.suggest(conn, q, limit, user_id=current_user_id())}


def get_product(pid: int) -> dict:
    with db.connect() as conn:
        r = conn.execute("SELECT p.*, i.id AS imported_id FROM products p LEFT JOIN imported_products i "
                         "ON i.product_id = p.id AND i.user_id = ? WHERE p.id = ?", (current_user_id(), pid)).fetchone()
        if not r:
            raise ApiError(404, "Producto no encontrado")
        return _item(r)


def facets() -> dict:
    """Conteos por categoría, proveedor, almacén y fuente (en caché hasta que cambie el catálogo)."""
    def build():
        with db.connect() as conn:
            def col(c):
                return [{"value": r[0], "count": r[1]} for r in
                        conn.execute(f"SELECT {c}, COUNT(*) FROM products GROUP BY {c} ORDER BY COUNT(*) DESC")]
            return {"category": col("category"), "supplier": col("supplier"), "warehouse": col("warehouse"),
                    "source": col("source"),
                    "total": conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]}
    return search.cached("facets", build)


def get_settings() -> dict:
    with db.connect() as conn:
        return db.get_settings(conn)


def update_settings(body: dict) -> dict:
    with db.connect() as conn:
        for k in DEFAULT_SETTINGS:
            if k in body:
                try:
                    v = float(body[k])
                except (TypeError, ValueError):
                    raise ApiError(400, f"Valor inválido: {k}")
                if k == "multiplier" and v < 1:
                    raise ApiError(400, "El multiplicador debe ser ≥ 1")
                if v < 0 or (k == "fee_pct" and v >= 100):
                    raise ApiError(400, f"Valor fuera de rango: {k}")
                conn.execute("UPDATE settings SET value = ? WHERE key = ?", (json.dumps(v), k))
        s = db.get_settings(conn)
        db.recompute_metrics(conn, s)      # precio, margen y score precalculados con las nuevas reglas
        return s


def _imported_view(conn, r, s) -> dict:
    d = dict(r)
    total = d["cost"] + d["shipping_cost"]
    d["profit"], d["margin"] = profit_for(d["sell_price"], total, s)
    d["total_cost"] = round(total, 2)
    d["alerts"] = conn.execute("SELECT COUNT(*) FROM alerts WHERE imported_id = ? AND read = 0",
                               (d["id"],)).fetchone()[0]
    return d


IMPORT_SQL = ("SELECT i.*, p.cost, p.shipping_cost, p.stock, p.supplier, p.image, p.category, p.shipping_days, "
              "p.source, p.product_url FROM imported_products i JOIN products p ON p.id = i.product_id")


def list_imports() -> list:
    with db.connect() as conn:
        s = db.get_settings(conn)
        return [_imported_view(conn, r, s) for r in conn.execute(
            IMPORT_SQL + " WHERE i.user_id = ? ORDER BY i.created_at DESC, i.id DESC", (current_user_id(),))]


def create_import(body: dict) -> dict:
    pid = body.get("product_id")
    p = get_product(int(pid)) if isinstance(pid, (int, str)) and str(pid).isdigit() else None
    if not p:
        raise ApiError(400, "product_id requerido")
    if p["imported_id"]:
        raise ApiError(409, "El producto ya fue importado")
    try:
        price = float(body.get("sell_price") or p["suggested_price"])
    except (TypeError, ValueError):
        raise ApiError(400, "Precio inválido")
    if not math.isfinite(price) or price <= 0:
        raise ApiError(400, "Precio inválido")
    with db.connect() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO imported_products(user_id, product_id, title, sell_price, last_cost, last_stock, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (current_user_id(), p["id"], p["title"], price, p["cost"], p["stock"], db.now()))
        except sqlite3.IntegrityError:          # dos peticiones simultáneas: UNIQUE(user_id, product_id)
            raise ApiError(409, "El producto ya fue importado")
        r = conn.execute(IMPORT_SQL + " WHERE i.id = ?", (cur.lastrowid,)).fetchone()
        return _imported_view(conn, r, db.get_settings(conn))


def _own_import(conn, iid: int) -> None:
    """404 si el producto importado no existe o es de otro usuario (no se revela cuál de las dos)."""
    if not conn.execute("SELECT 1 FROM imported_products WHERE id = ? AND user_id = ?",
                        (iid, current_user_id())).fetchone():
        raise ApiError(404, "No encontrado")


def update_import(iid: int, body: dict) -> dict:
    with db.connect() as conn:
        _own_import(conn, iid)
        if "sell_price" in body:
            try:
                price = float(body["sell_price"])
            except (TypeError, ValueError):
                raise ApiError(400, "Precio inválido")
            if not math.isfinite(price) or price <= 0:
                raise ApiError(400, "Precio inválido")
            conn.execute("UPDATE imported_products SET sell_price = ? WHERE id = ?", (price, iid))
        if "status" in body:
            if body["status"] not in ("draft", "active"):
                raise ApiError(400, "Estado inválido")
            conn.execute("UPDATE imported_products SET status = ? WHERE id = ?", (body["status"], iid))
        if body.get("title"):
            conn.execute("UPDATE imported_products SET title = ? WHERE id = ?", (str(body["title"])[:200], iid))
        r = conn.execute(IMPORT_SQL + " WHERE i.id = ?", (iid,)).fetchone()
        return _imported_view(conn, r, db.get_settings(conn))


def delete_import(iid: int) -> dict:
    with db.connect() as conn:
        _own_import(conn, iid)
        conn.execute("DELETE FROM imported_products WHERE id = ?", (iid,))
    return {"ok": True}


def _check_imported(conn, r, new: PriceStock, s: dict, created: list) -> None:
    """Compara el último estado conocido de un producto importado con el nuevo y genera alertas."""
    msgs = []
    if new.cost > r["last_cost"] * 1.05:
        pct = (new.cost / r["last_cost"] - 1) * 100
        msgs.append(("price_up", f"El costo subió {pct:.0f}% (${r['last_cost']:.2f} → ${new.cost:.2f})"))
    elif new.cost < r["last_cost"] * 0.95:
        msgs.append(("price_down", f"El costo bajó (${r['last_cost']:.2f} → ${new.cost:.2f})"))
    if new.stock == 0 and r["last_stock"] > 0:
        msgs.append(("out_of_stock", "El proveedor se quedó sin stock"))
    elif new.stock > 0 and r["last_stock"] == 0:
        msgs.append(("back_in_stock", "El producto volvió a tener stock"))
    profit, _ = profit_for(r["sell_price"], new.cost + new.shipping_cost, s)
    if profit < 0:
        msgs.append(("negative_margin", f"Vendes a pérdida: ganancia ${profit:.2f}. Ajusta el precio."))
    for t, m in msgs:
        # La alerta es del dueño del producto importado (no de quien ejecutó el monitor o subió el CSV)
        conn.execute("INSERT INTO alerts(imported_id, user_id, type, message, created_at) VALUES (?, ?, ?, ?, ?)",
                     (r["id"], r["user_id"], t, m, db.now()))
        created.append({"imported_id": r["id"], "user_id": r["user_id"], "title": r["title"], "type": t, "message": m})
    conn.execute("UPDATE imported_products SET last_cost = ?, last_stock = ? WHERE id = ?",
                 (new.cost, new.stock, r["id"]))


MONITOR_COOLDOWN_MIN = 15     # minutos entre dos actualizaciones del mismo producto por el monitor


def run_monitor(user_id: int | None = None, force: bool = False) -> dict:
    """Re-consulta precio/stock en el proveedor para cada producto importado por el usuario y genera
    alertas. Si otro usuario importó el mismo producto, también recibe sus alertas (cada alerta es del
    dueño de su producto importado); la respuesta solo informa las del usuario.

    Enfriamiento: el catálogo es compartido, así que un producto que el monitor actualizó hace menos de
    MONITOR_COOLDOWN_MIN minutos no se vuelve a consultar ni a escribir (lo ejecute quien lo ejecute). Sus
    alertas para el usuario se evalúan igual contra el último estado conocido. Solo el propietario puede
    saltarse el enfriamiento con force=True. Corre con el bloqueo de escritura tomado (dos monitores a la vez
    se serializan y no consultan dos veces el mismo producto)."""
    uid = current_user_id() if user_id is None else user_id
    force = bool(force) and auth_context.is_owner()
    created, checked, skipped, changed, cooled = [], 0, 0, [], 0
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=MONITOR_COOLDOWN_MIN)).isoformat(timespec="seconds")
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        s = db.get_settings(conn)
        for r in conn.execute(IMPORT_SQL + " WHERE i.user_id = ?", (uid,)).fetchall():
            connector = LIVE_CONNECTORS.get(r["source"])
            if connector is None:
                skipped += 1
                continue
            last = conn.execute("SELECT checked_at FROM monitor_checks WHERE product_id = ?",
                                (r["product_id"],)).fetchone()
            if last and last[0] > cutoff and not force:
                cooled += 1                    # sin consultar ni escribir el catálogo; alertas con lo último
                _check_imported(conn, r, PriceStock(r["cost"], r["shipping_cost"], r["stock"]), s, created)
                continue
            cur = PriceStock(r["cost"], r["shipping_cost"], r["stock"])
            new = connector.get_price_stock(r["supplier"], current=cur)
            if new is None:
                skipped += 1
                continue
            checked += 1
            conn.execute("UPDATE products SET cost = ?, shipping_cost = ?, stock = ?, updated_at = ? WHERE id = ?",
                         (new.cost, new.shipping_cost, new.stock, db.now(), r["product_id"]))
            conn.execute("INSERT OR REPLACE INTO monitor_checks(product_id, checked_at) VALUES (?, ?)",
                         (r["product_id"], db.now()))
            changed.append(r["product_id"])
            for other in conn.execute(IMPORT_SQL + " WHERE i.product_id = ?", (r["product_id"],)).fetchall():
                _check_imported(conn, other, new, s, created)
        db.recompute_metrics(conn, s, ids=changed)
        hooks.run_catalog_write(conn, changed)
    mine = [{k: v for k, v in a.items() if k != "user_id"} for a in created if a["user_id"] == uid]
    return {"checked": checked, "skipped": skipped, "cooldown": cooled, "alerts": mine}


# ---------------------------------------------------------------- proveedores / catálogo

def list_suppliers() -> list:
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT p.supplier, p.source, COUNT(*) AS products, COUNT(i.id) AS imported, MAX(p.updated_at) AS updated_at "
            "FROM products p LEFT JOIN imported_products i ON i.product_id = p.id AND i.user_id = ? "
            "GROUP BY p.supplier, p.source ORDER BY p.source, p.supplier", (current_user_id(),))]


def upload_csv(body: dict) -> dict:
    supplier = str(body.get("supplier") or "").strip()
    text = body.get("csv")
    if not supplier or len(supplier) > 60:
        raise ApiError(400, "Indica el nombre del proveedor (máx. 60 caracteres)")
    if not isinstance(text, str) or not text.strip():
        raise ApiError(400, "El archivo CSV está vacío")
    parsed = CsvSupplierConnector(supplier, text).result
    if not parsed.products:
        raise ApiError(400, "; ".join(parsed.errors[:5]) or "No se encontraron productos")

    created_alerts: list = []
    removed = 0
    with db.connect() as conn:
        s = db.get_settings(conn)
        touched: list = []
        new, updated = db.upsert_products(conn, parsed.products, "csv", run_hooks=False, collect_ids=touched)
        if body.get("replace"):
            skus = [p.supplier_sku for p in parsed.products]
            conn.execute("CREATE TEMP TABLE IF NOT EXISTS _skus(sku TEXT PRIMARY KEY)")
            conn.execute("DELETE FROM _skus")
            conn.executemany("INSERT OR IGNORE INTO _skus VALUES (?)", [(k,) for k in skus])
            gone = "supplier = ? AND source = 'csv' AND supplier_sku NOT IN (SELECT sku FROM _skus)"
            # Los que ya importaste no se borran: quedan sin stock (y el monitor lo avisa)
            touched += [r[0] for r in conn.execute(
                f"SELECT id FROM products WHERE {gone} AND id IN (SELECT product_id FROM imported_products)", (supplier,))]
            conn.execute(f"UPDATE products SET stock = 0, updated_at = ? WHERE {gone} "
                         "AND id IN (SELECT product_id FROM imported_products)", (db.now(), supplier))
            removed = conn.execute(f"DELETE FROM products WHERE {gone} "
                                   "AND id NOT IN (SELECT product_id FROM imported_products)", (supplier,)).rowcount
            search.invalidate(conn)
        # Los módulos ven el catálogo ya completo (después del reemplazo), una sola vez
        hooks.run_catalog_write(conn, touched)
        # Alertas para los productos importados de este proveedor cuyo costo/stock cambió
        for r in conn.execute(IMPORT_SQL + " WHERE p.supplier = ? AND p.source = 'csv'", (supplier,)).fetchall():
            _check_imported(conn, r, PriceStock(r["cost"], r["shipping_cost"], r["stock"]), s, created_alerts)
    return {"supplier": supplier, "new": new, "updated": updated, "removed": removed,
            "errors": parsed.errors, "columns": parsed.columns, "alerts": len(created_alerts)}


def delete_supplier_catalog(qs: dict) -> dict:
    """Elimina el catálogo de un proveedor (o la demo completa). Conserva los productos ya importados."""
    source = qs.get("source", [""])[0]
    supplier = qs.get("supplier", [""])[0]
    if source not in ("mock", "csv"):
        raise ApiError(400, "source debe ser mock o csv")
    where, args = "source = ?", [source]
    if supplier:
        where += " AND supplier = ?"
        args.append(supplier)
    elif source != "mock":
        raise ApiError(400, "Indica el proveedor")
    with db.connect() as conn:
        n = conn.execute(f"DELETE FROM products WHERE {where} "
                         "AND id NOT IN (SELECT product_id FROM imported_products)", args).rowcount
    search.invalidate()
    return {"deleted": n}


def alerts_count() -> dict:
    with db.connect() as conn:
        return {"unread": conn.execute("SELECT COUNT(*) FROM alerts WHERE user_id = ? AND read = 0",
                                       (current_user_id(),)).fetchone()[0]}


def list_alerts() -> list:
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT a.*, COALESCE(i.title, p.title, s.name, '') AS title, s.name AS saved_search_name "
            "FROM alerts a LEFT JOIN imported_products i ON i.id = a.imported_id "
            "LEFT JOIN products p ON p.id = a.product_id "
            "LEFT JOIN saved_searches s ON s.id = a.saved_search_id "
            "WHERE a.user_id = ? ORDER BY a.id DESC LIMIT 200", (current_user_id(),))]


def mark_alerts_read() -> dict:
    with db.connect() as conn:
        conn.execute("UPDATE alerts SET read = 1 WHERE user_id = ? AND read = 0", (current_user_id(),))
    return {"ok": True}


# ---------------------------------------------------------------- moneda

def currency_state() -> dict:
    with db.connect() as conn:
        return currency.get_state(conn)


def currency_update(body: dict) -> dict:
    """Preferencias de moneda del usuario; la tasa (modo, tasa manual, cobertura) es de la instalación y
    solo la cambia el propietario. Un miembro puede reenviar esos valores sin cambios (el formulario
    completo), pero no modificarlos."""
    with db.connect() as conn:
        if not auth_context.is_owner() and isinstance(body, dict):
            current = currency.get_settings(conn)
            for k in currency.INSTALLATION_KEYS:
                if k in body:
                    try:
                        same = currency._validate(k, body[k]) == current[k]
                    except ValueError:
                        same = False
                    if not same:
                        raise ApiError(403, "Solo el propietario puede cambiar la tasa de cambio y la cobertura.")
            body = {k: v for k, v in body.items() if k not in currency.INSTALLATION_KEYS}
        try:
            currency.update_settings(conn, body)
        except ValueError as e:
            raise ApiError(400, str(e))
        return currency.get_state(conn)


def currency_refresh() -> dict:
    with db.connect() as conn:
        return currency.refresh_trm(conn)


def _auto_refresh_trm() -> None:
    """Actualiza la TRM al arrancar, en segundo plano: si datos.gov.co no responde, el servidor no espera."""
    try:
        with db.connect() as conn:
            currency.maybe_auto_refresh(conn)
    except Exception:  # noqa: BLE001 — nunca debe tumbar el servidor
        pass


# ---------------------------------------------------------------- routing

ROUTES = [
    ("GET", r"/api/products", lambda m, qs, b: search_products(qs)),
    ("GET", r"/api/suggest", lambda m, qs, b: suggest(qs)),
    ("GET", r"/api/products/(\d+)", lambda m, qs, b: get_product(int(m[1]))),
    ("GET", r"/api/facets", lambda m, qs, b: facets()),
    ("GET", r"/api/settings", lambda m, qs, b: get_settings()),
    ("PUT", r"/api/settings", lambda m, qs, b: update_settings(b)),
    ("GET", r"/api/imports", lambda m, qs, b: list_imports()),
    ("POST", r"/api/imports", lambda m, qs, b: create_import(b)),
    ("PATCH", r"/api/imports/(\d+)", lambda m, qs, b: update_import(int(m[1]), b)),
    ("DELETE", r"/api/imports/(\d+)", lambda m, qs, b: delete_import(int(m[1]))),
    ("POST", r"/api/monitor/run", lambda m, qs, b: run_monitor(force=isinstance(b, dict) and b.get("force") is True)),
    ("GET", r"/api/alerts", lambda m, qs, b: list_alerts()),
    ("GET", r"/api/suppliers", lambda m, qs, b: list_suppliers()),
    ("POST", r"/api/catalog/csv", lambda m, qs, b: upload_csv(b)),
    ("DELETE", r"/api/catalog", lambda m, qs, b: delete_supplier_catalog(qs)),
    ("POST", r"/api/alerts/read", lambda m, qs, b: mark_alerts_read()),
    ("GET", r"/api/alerts/count", lambda m, qs, b: alerts_count()),
    ("GET", r"/api/currency", lambda m, qs, b: currency_state()),
    ("PUT", r"/api/currency", lambda m, qs, b: currency_update(b)),
    ("POST", r"/api/currency/refresh", lambda m, qs, b: currency_refresh()),
]

# Rutas de los módulos de funcionalidades (cada uno define su propia lista ROUTES)
for _module in (auth, history, compare, saved_searches, dashboard, collections_mod, stores_mod, store_sync):
    ROUTES.extend(_module.ROUTES)
store_sync.set_monitor_runner(run_monitor)      # monitor programado de tiendas (mismo código que el botón)

# ---------------------------------------------------------------- acceso por ruta
# TODA ruta debe estar en una de estas tres tablas (lo comprueba tests/test_auth.py, que recorre ROUTES):
# - PUBLIC_ROUTES: sin sesión (deben coincidir con auth.PUBLIC_PATHS).
# - OWNER_ONLY: solo el propietario → (método, patrón): acción del mensaje "Solo el propietario puede …".
#   Catálogo, reglas de precio, tasa de cambio y usuarios son de la instalación.
# - USER_ROUTES: cualquier usuario con sesión (los datos propios se separan con auth_context).
# Una ruta sin clasificar se trata como solo-propietario si modifica datos (fallo seguro) y como de
# usuario si es de lectura; la prueba igual falla para obligar a clasificarla.
PUBLIC_ROUTES = {("GET", r"/api/auth/status"), ("POST", r"/api/auth/register"), ("POST", r"/api/auth/login")}
OWNER_ONLY = {
    ("PUT", r"/api/settings"): "cambiar las reglas de precio",
    ("POST", r"/api/catalog/csv"): "cargar catálogos de proveedores",
    ("DELETE", r"/api/catalog"): "eliminar catálogos",
    ("POST", r"/api/currency/refresh"): "actualizar la TRM de la instalación",
    ("PUT", r"/api/stores/monitor-schedule"): "programar el monitor de precios y stock",
    **auth.OWNER_ONLY,
}
USER_ROUTES = {
    # núcleo
    ("GET", r"/api/products"), ("GET", r"/api/suggest"), ("GET", r"/api/products/(\d+)"), ("GET", r"/api/facets"),
    ("GET", r"/api/settings"), ("GET", r"/api/imports"), ("POST", r"/api/imports"),
    ("PATCH", r"/api/imports/(\d+)"), ("DELETE", r"/api/imports/(\d+)"),
    ("POST", r"/api/monitor/run"),           # revisa los importados propios (con enfriamiento por producto)
    ("GET", r"/api/alerts"), ("GET", r"/api/suppliers"), ("POST", r"/api/alerts/read"), ("GET", r"/api/alerts/count"),
    ("GET", r"/api/currency"),
    ("PUT", r"/api/currency"),               # preferencias propias; la tasa la valida currency_update (403)
    # cuenta propia
    ("POST", r"/api/auth/logout"), ("GET", r"/api/auth/me"), ("PATCH", r"/api/auth/me"),
    # historial y comparador (lectura del catálogo compartido)
    ("GET", r"/api/products/(\d+)/history"), ("GET", r"/api/rankings"), ("GET", r"/api/seasonal"),
    ("GET", r"/api/suppliers/stats"), ("GET", r"/api/products/(\d+)/alternatives"),
    ("GET", r"/api/alternatives/counts"), ("GET", r"/api/compare"),
    # búsquedas guardadas (propias)
    ("GET", r"/api/saved-searches"), ("GET", r"/api/saved-searches/preview"), ("POST", r"/api/saved-searches"),
    ("POST", r"/api/saved-searches/check"), ("PATCH", r"/api/saved-searches/(\d+)"),
    ("DELETE", r"/api/saved-searches/(\d+)"), ("GET", r"/api/saved-searches/(\d+)/results"),
    ("POST", r"/api/saved-searches/(\d+)/seen"),
    # panel (indicadores propios; descartar un paso de "primeros pasos" guarda una preferencia propia)
    ("GET", r"/api/dashboard"), ("POST", r"/api/dashboard/onboarding/([a-z_]+)/dismiss"),
    # colecciones y exportación (propias)
    ("GET", r"/api/collections"), ("POST", r"/api/collections"), ("PATCH", r"/api/collections/(\d+)"),
    ("DELETE", r"/api/collections/(\d+)"), ("GET", r"/api/collections/(\d+)/items"),
    ("POST", r"/api/collections/(\d+)/items"), ("DELETE", r"/api/collections/(\d+)/items/(\d+)"),
    ("PATCH", r"/api/collections/(\d+)/items/(\d+)"), ("POST", r"/api/favorites/(\d+)"),
    ("GET", r"/api/favorites/ids"), ("GET", r"/api/product-collections/(\d+)"),
    ("GET", r"/api/export/(search|collection|imports)\.csv"),
    # tiendas conectadas (propias; las credenciales nunca salen del servidor)
    ("GET", r"/api/stores/platforms"), ("GET", r"/api/stores"), ("POST", r"/api/stores"),
    ("GET", r"/api/stores/(\d+)"), ("PATCH", r"/api/stores/(\d+)"), ("POST", r"/api/stores/(\d+)/validate"),
    ("DELETE", r"/api/stores/(\d+)"),
    # publicación y sincronización (propias; la cola solo actúa con el usuario dueño de cada trabajo)
    ("POST", r"/api/stores/(\d+)/publish"), ("GET", r"/api/store-listings"), ("GET", r"/api/stores/(\d+)/listings"),
    ("POST", r"/api/store-listings/(\d+)/(sync|hide|show|retry)"), ("DELETE", r"/api/store-listings/(\d+)"),
    ("GET", r"/api/stores/(\d+)/log"), ("POST", r"/api/stores/sync"), ("GET", r"/api/stores/queue"),
    ("GET", r"/api/stores/monitor-schedule"),
}


def route_access(method: str, pattern: str) -> str:
    """'public', 'owner' o 'user' para una ruta de ROUTES (sin clasificar: 'owner' si modifica datos)."""
    key = (method, pattern)
    if key in PUBLIC_ROUTES:
        return "public"
    if key in OWNER_ONLY:
        return "owner"
    if key in USER_ROUTES:
        return "user"
    return "user" if method in ("GET", "HEAD") else "owner"


# Tamaño máximo del cuerpo: 20 MB solo para cargar un catálogo CSV (propietario con sesión); cuentas y
# usuarios, 16 KB (rutas públicas incluidas); el resto, 1 MB.
MAX_BODY_AUTH = 16 * 1024
MAX_BODY_DEFAULT = 1024 * 1024


def body_limit(method: str, path: str) -> int:
    if path.startswith(("/api/auth/", "/api/users")):
        return MAX_BODY_AUTH
    if method == "POST" and path == "/api/catalog/csv":
        return MAX_BODY
    return MAX_BODY_DEFAULT

# ---------------------------------------------------------------- seguridad HTTP

# Política de contenido: solo recursos propios; imágenes de productos externas por https y las
# ilustraciones locales (data:). Sin estilos ni scripts en línea.
CSP = ("default-src 'self'; img-src 'self' data: https:; style-src 'self'; script-src 'self'; "
       "connect-src 'self'; font-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; "
       "form-action 'self'")
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
}
SAFE_METHODS = ("GET", "HEAD")
# Hosts permitidos en la cabecera Host (defensa contra DNS rebinding). Se agregan los de
# DROPFINDER_ALLOWED_HOSTS (separados por coma) y el --host con que se inicia el servidor.
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"} | {
    h.strip().lower() for h in os.environ.get("DROPFINDER_ALLOWED_HOSTS", "").split(",") if h.strip()}
# IP del proxy HTTPS de confianza (Caddy/nginx). Solo de esa IP se aceptan X-Forwarded-Proto/For.
TRUSTED_PROXY = os.environ.get("DROPFINDER_TRUSTED_PROXY", "").strip()
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
LOOPBACK_IPS = {"127.0.0.1", "::1"}
_xff_warned = [False]
# Modo desarrollo: las peticiones SIN sesión actúan como este usuario (id). Solo lo activa main() si
# DROPFINDER_ENV=development, DROPFINDER_DEV_USER=<id> y el servidor escucha en loopback. Es el único
# atajo de autenticación de la app (los módulos no deben tener el suyo). None = desactivado.
DEV_USER: int | None = None


def dev_user_from_env(host: str) -> int | None:
    """Usuario del modo desarrollo o None. Si se pide (DROPFINDER_ENV=development + DROPFINDER_DEV_USER) en
    una configuración que puede estar expuesta (proxy de confianza, hosts permitidos o --host que no son
    locales), el servidor NO arranca: es un error de configuración, no algo que se pueda dejar pasar."""
    raw = os.environ.get("DROPFINDER_DEV_USER", "").strip()
    if not raw:
        return None
    if os.environ.get("DROPFINDER_ENV") != "development" or not raw.isdigit():
        print("AVISO: DROPFINDER_DEV_USER se ignora: requiere DROPFINDER_ENV=development y un id numérico.",
              flush=True)
        return None
    exposed = [x for x in ([f"--host {host}"] if _hostname(host) not in LOOPBACK else [])
               + ([f"DROPFINDER_TRUSTED_PROXY={TRUSTED_PROXY}"] if TRUSTED_PROXY else [])
               + [f"host permitido {h}" for h in sorted(ALLOWED_HOSTS - LOOPBACK)]]
    if exposed:
        raise SystemExit("ERROR: el modo desarrollo (DROPFINDER_DEV_USER) no se puede usar con "
                         + ", ".join(exposed) + ". Quita DROPFINDER_DEV_USER o esa configuración.")
    print("\n" + "!" * 78 + f"\n!! MODO DESARROLLO: las peticiones sin sesión actúan como el usuario {raw} "
          "(sin contraseña ni CSRF).\n!! No lo uses con datos reales ni expongas este servidor.\n" + "!" * 78 + "\n",
          flush=True)
    return int(raw)


def _hostname(hostport: str) -> str:
    """'ejemplo.com:8000' → 'ejemplo.com'; '[::1]:8000' → '::1'."""
    hp = (hostport or "").strip().lower()
    if hp.startswith("["):
        return hp[1:hp.find("]")] if "]" in hp else ""
    return hp.rsplit(":", 1)[0] if hp.count(":") == 1 else hp


def _parse_cookies(header: str) -> dict:
    out = {}
    for part in (header or "").split(";"):
        k, sep, v = part.strip().partition("=")
        if sep and k not in out:
            out[k] = v
    return out


def _dev_session_allowed(handler) -> bool:
    """El modo desarrollo solo vale para peticiones directas desde este equipo: IP loopback, Host
    loopback y sin cabeceras de proxy (X-Forwarded-* / Forwarded)."""
    return (handler.client_address[0] in LOOPBACK_IPS and _hostname(handler.headers.get("Host", "")) in LOOPBACK
            and not handler._has_forwarded())


def _dev_session() -> dict:
    """Sesión ficticia del modo desarrollo (ver DEV_USER): el rol sale de la cuenta si existe."""
    with db.connect() as conn:
        r = conn.execute("SELECT id, email, name, role, disabled FROM users WHERE id = ?", (DEV_USER,)).fetchone()
    user = ({"id": r["id"], "email": r["email"], "name": r["name"], "role": r["role"]} if r and not r["disabled"]
            else {"id": DEV_USER, "email": "", "name": "Desarrollo", "role": "owner"})
    return {"token_hash": "", "csrf_token": "", "touched": False, "dev": True, "user": user}


class Handler(SimpleHTTPRequestHandler):
    server_version = "DropFinder"       # sin versión de Python en la cabecera Server
    sys_version = ""
    timeout = 30                        # segundos por conexión: una conexión a medias no retiene un hilo

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=FRONTEND, **kw)

    def version_string(self):
        return self.server_version

    def log_message(self, fmt, *args):
        pass

    def end_headers(self):
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        if getattr(self, "headers", None) is not None and self._secure():
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        if self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    # ------------------------------------------------------------ datos de la conexión
    def _from_proxy(self) -> bool:
        return bool(TRUSTED_PROXY) and self.client_address[0] == TRUSTED_PROXY

    def _secure(self) -> bool:
        """HTTPS solo si lo dice el proxy de confianza (la app no habla HTTPS por sí misma)."""
        return self._from_proxy() and (self.headers.get("X-Forwarded-Proto") or "").strip().lower() == "https"

    def _has_forwarded(self) -> bool:
        return any(k.lower().startswith("x-forwarded-") or k.lower() == "forwarded" for k in self.headers.keys())

    def _local_direct(self) -> bool:
        """Conexión directa desde este equipo: IP y Host loopback, sin proxy de por medio."""
        return (self.client_address[0] in LOOPBACK_IPS and _hostname(self.headers.get("Host", "")) in LOOPBACK
                and not self._has_forwarded())

    def _client_ip(self) -> str:
        if not self._from_proxy() and self.headers.get("X-Forwarded-For") and not _xff_warned[0]:
            _xff_warned[0] = True
            print(f"AVISO: llegó X-Forwarded-For desde {self.client_address[0]}, que no es el proxy de confianza: "
                  "se ignora. Si la app está detrás de un proxy, configura DROPFINDER_TRUSTED_PROXY (si no, todos "
                  "los clientes comparten una IP para el límite de intentos).", flush=True)
        if self._from_proxy():
            fwd = [x.strip() for x in (self.headers.get("X-Forwarded-For") or "").split(",") if x.strip()]
            if fwd:
                return fwd[-1][:64]          # la última IP la agregó nuestro proxy (las anteriores las envía el cliente)
        return self.client_address[0]

    def _host_ok(self) -> bool:
        return _hostname(self.headers.get("Host", "")) in ALLOWED_HOSTS

    def _same_origin(self) -> bool:
        """Peticiones que modifican datos: si el navegador envía Origin (o Referer), debe ser este sitio."""
        host = (self.headers.get("Host") or "").strip().lower()
        origin = self.headers.get("Origin")
        if origin is not None:
            u = urlparse(origin.strip().lower())
            return origin.strip().lower() != "null" and u.scheme in ("http", "https") and u.netloc == host
        referer = self.headers.get("Referer")
        if referer:
            return urlparse(referer.strip().lower()).netloc == host
        return True                          # sin Origin ni Referer (clientes que no son navegadores): basta el CSRF

    def _send_json(self, status, data, headers=None):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or []):
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_file(self, f: FileResponse, headers=None):
        """Descarga (p. ej. CSV): bytes tal cual con su tipo y nombre de archivo."""
        self.send_response(200)
        self.send_header("Content-Type", f.content_type)
        self.send_header("Content-Length", str(len(f.body)))
        if f.filename:
            self.send_header("Content-Disposition", f'attachment; filename="{f.filename}"')
        for k, v in list(f.headers.items()) + list(headers or []):
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(f.body)

    # ------------------------------------------------------------ API
    def _api(self, method):
        url = urlparse(self.path)
        info = auth.RequestInfo(ip=self._client_ip(), user_agent=self.headers.get("User-Agent") or "",
                                secure=self._secure())
        info.local_direct = self._local_direct()
        info.token = _parse_cookies(self.headers.get("Cookie", "")).get(auth.cookie_name(info.secure))
        auth.set_request(info)
        auth_context.clear()
        try:
            status, data, extra = self._dispatch(method, url, info)
        finally:
            auth_context.clear()
            auth.set_request(None)
        headers = list(extra.items())
        sess = info.session
        if sess and sess.get("touched") and info.token and not info.set_cookies:
            info.set_cookies.append(auth.cookie_header(info.token, info.secure))   # caducidad deslizante
        headers += [("Set-Cookie", c) for c in info.set_cookies]
        if isinstance(data, FileResponse):
            return self._send_file(data, headers)
        self._send_json(status, data, headers)

    def _dispatch(self, method, url, info) -> tuple[int, object, dict]:
        """Autenticación, CSRF, rol y la ruta. Devuelve (código, JSON, cabeceras extra)."""
        if method not in SAFE_METHODS and not self._same_origin():
            return 403, {"error": "Petición rechazada: no viene de esta aplicación"}, {}
        public = url.path in auth.PUBLIC_PATHS
        auth.maybe_purge()
        if not public:
            info.session = auth.load_session(info.token)
            if not info.session and DEV_USER is not None and _dev_session_allowed(self):
                info.session = _dev_session()
            if not info.session:
                return 401, {"error": "Inicia sesión para continuar"}, {}
            if method not in SAFE_METHODS and not info.session.get("dev"):
                sent = self.headers.get("X-CSRF-Token") or ""
                if not hmac.compare_digest(sent.encode(), info.session["csrf_token"].encode()):
                    return 403, {"error": "Token CSRF inválido o ausente. Recarga la página."}, {}
            user = info.session["user"]
            auth_context.set_current(user["id"], user["role"])
        else:
            info.session = auth.load_session(info.token, touch=False) if info.token else None
        for m, pattern, fn in ROUTES:
            match = re.fullmatch(pattern, url.path)
            if m != method or not match:
                continue
            if route_access(m, pattern) == "owner" and not auth_context.is_owner():
                action = OWNER_ONLY.get((m, pattern), "usar esta función")
                return 403, {"error": f"Solo el propietario puede {action}."}, {}
            try:
                body = {}
                raw_len = (self.headers.get("Content-Length") or "0").strip()
                if not raw_len.isdigit():
                    self.close_connection = True
                    return 400, {"error": "Content-Length inválido"}, {}
                length = int(raw_len)
                limit = body_limit(method, url.path)
                if length > limit:
                    self.close_connection = True       # no se lee el cuerpo: se cierra la conexión
                    size = f"{limit // 1048576} MB" if limit >= 1048576 else f"{limit // 1024} KB"
                    raise ApiError(413, f"Petición demasiado grande (máx. {size})")
                if length:
                    body = json.loads(self.rfile.read(length) or b"{}")
                result = fn(match, parse_qs(url.query), body)
                if isinstance(result, auth.Reply):
                    return result.status, result.data, {}
                return 200, result, {}
            except ApiError as e:
                return e.status, {"error": str(e)}, getattr(e, "headers", None) or {}
            except (json.JSONDecodeError, UnicodeDecodeError):
                return 400, {"error": "JSON inválido"}, {}
            except OverflowError:              # ids enormes (/api/users/99999999999999999999…)
                return 400, {"error": "Identificador inválido"}, {}
            except Exception as e:  # noqa: BLE001
                self.log_error("Error interno: %r", e)
                return 500, {"error": "Error interno del servidor"}, {}
        return HTTPStatus.NOT_FOUND, {"error": "Ruta no encontrada"}, {}

    def _guard(self) -> bool:
        """Host permitido (DNS rebinding). Si no, responde 421 y devuelve False."""
        if self._host_ok():
            return True
        self._send_json(421, {"error": "Host no permitido. Agrégalo a DROPFINDER_ALLOWED_HOSTS."})
        return False

    def do_HEAD(self):
        if not self._guard():
            return
        if self.path.startswith("/api/"):
            return self._api("GET")
        return super().do_HEAD()

    def do_GET(self):
        if not self._guard():
            return
        if self.path.startswith("/api/"):
            return self._api("GET")
        return super().do_GET()

    def _mutating(self, method):
        if not self._guard():
            return
        if not self.path.startswith("/api/"):
            return self._send_json(405, {"error": "Método no permitido"})
        self._api(method)

    def do_POST(self):
        self._mutating("POST")

    def do_PUT(self):
        self._mutating("PUT")

    def do_PATCH(self):
        self._mutating("PATCH")

    def do_DELETE(self):
        self._mutating("DELETE")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    global DEV_USER
    if ":" in args.host:
        raise SystemExit("ERROR: DropFinder solo escucha en IPv4 (p. ej. --host 127.0.0.1). "
                         "Para IPv6 usa un proxy (Caddy/nginx) delante.")
    if args.host not in ("0.0.0.0", ""):
        ALLOWED_HOSTS.add(_hostname(args.host))
    DEV_USER = dev_user_from_env(args.host)
    db.init_db()
    auth.init_setup_token()
    threading.Thread(target=_auto_refresh_trm, daemon=True).start()
    print(f"DropFinder en http://{args.host}:{args.port}")
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
