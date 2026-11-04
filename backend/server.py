"""DropFinder — servidor HTTP (solo librería estándar).

Uso:  python3 server.py [--port 8000]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import db
from connectors import CsvSupplierConnector, MockSupplierConnector
from connectors.base import PriceStock
from metrics import DEFAULT_SETTINGS, enrich, profit_for

FRONTEND = os.path.join(os.path.dirname(__file__), "..", "frontend")
SORTS = {
    "score": ("score", True), "orders": ("orders", True), "trend": ("trend", True),
    "margin": ("margin", True), "profit": ("profit", True), "price_asc": ("suggested_price", False),
    "price_desc": ("suggested_price", True), "shipping": ("shipping_days", False), "rating": ("rating", True),
}
MAX_BODY = 20 * 1024 * 1024   # 20 MB (catálogos CSV grandes)
# Fuentes con consulta en vivo de precio/stock. Los CSV se actualizan volviendo a subir el archivo.
LIVE_CONNECTORS = {"mock": MockSupplierConnector()}


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _num(qs, key, cast=float):
    v = qs.get(key, [""])[0].strip()
    if v == "":
        return None
    try:
        return cast(v)
    except ValueError:
        raise ApiError(400, f"Parámetro inválido: {key}")


# ---------------------------------------------------------------- handlers

def search_products(qs: dict) -> dict:
    where, args = [], []
    q = db.unaccent(qs.get("q", [""])[0].strip())
    if q:
        for term in q.split():
            where.append("(unaccent(p.title) LIKE ? OR unaccent(p.tags) LIKE ?)")
            args += [f"%{term}%", f"%{term}%"]
    for key in ("category", "supplier", "warehouse", "source"):
        vals = [v for v in qs.get(key, []) if v]
        if vals:
            where.append(f"p.{key} IN ({','.join('?' * len(vals))})")
            args += vals
    for key, col, op, cast in [
        ("min_cost", "p.cost + p.shipping_cost", ">=", float), ("max_cost", "p.cost + p.shipping_cost", "<=", float),
        ("min_orders", "p.orders", ">=", int), ("min_rating", "p.rating", ">=", float),
        ("max_ship_days", "p.shipping_days", "<=", int),
    ]:
        v = _num(qs, key, cast)
        if v is not None:
            where.append(f"{col} {op} ?")
            args.append(v)
    if qs.get("in_stock", ["0"])[0] == "1":
        where.append("p.stock > 0")

    sql = "SELECT p.*, i.id AS imported_id FROM products p LEFT JOIN imported_products i ON i.product_id = p.id"
    if where:
        sql += " WHERE " + " AND ".join(where)

    with db.connect() as conn:
        s = db.get_settings(conn)
        items = [enrich(dict(r), s) for r in conn.execute(sql, args)]

    # Filtros sobre métricas derivadas
    min_margin = _num(qs, "min_margin")
    if min_margin is not None:
        items = [p for p in items if p["margin"] >= min_margin]
    min_score = _num(qs, "min_score")
    if min_score is not None:
        items = [p for p in items if p["score"] >= min_score]
    if qs.get("trending", ["0"])[0] == "1":
        items = [p for p in items if p["trending"]]

    key, desc = SORTS.get(qs.get("sort", ["score"])[0], SORTS["score"])
    items.sort(key=lambda p: (p[key], p["id"]), reverse=desc)

    page = max(1, _num(qs, "page", int) or 1)
    size = min(100, max(1, _num(qs, "page_size", int) or 24))
    total = len(items)
    return {
        "total": total, "page": page, "page_size": size, "pages": math.ceil(total / size) if total else 0,
        "items": items[(page - 1) * size: page * size],
    }


def get_product(pid: int) -> dict:
    with db.connect() as conn:
        r = conn.execute("SELECT p.*, i.id AS imported_id FROM products p "
                         "LEFT JOIN imported_products i ON i.product_id = p.id WHERE p.id = ?", (pid,)).fetchone()
        if not r:
            raise ApiError(404, "Producto no encontrado")
        return enrich(dict(r), db.get_settings(conn))


def facets() -> dict:
    with db.connect() as conn:
        def col(c):
            return [{"value": r[0], "count": r[1]} for r in
                    conn.execute(f"SELECT {c}, COUNT(*) FROM products GROUP BY {c} ORDER BY COUNT(*) DESC")]
        return {"category": col("category"), "supplier": col("supplier"), "warehouse": col("warehouse"),
                "source": col("source"),
                "total": conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]}


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
        return db.get_settings(conn)


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
        return [_imported_view(conn, r, s) for r in conn.execute(IMPORT_SQL + " ORDER BY i.created_at DESC, i.id DESC")]


def create_import(body: dict) -> dict:
    pid = body.get("product_id")
    p = get_product(int(pid)) if isinstance(pid, (int, str)) and str(pid).isdigit() else None
    if not p:
        raise ApiError(400, "product_id requerido")
    if p["imported_id"]:
        raise ApiError(409, "El producto ya fue importado")
    price = float(body.get("sell_price") or p["suggested_price"])
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO imported_products(product_id, title, sell_price, last_cost, last_stock, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)", (p["id"], p["title"], price, p["cost"], p["stock"], db.now()))
        r = conn.execute(IMPORT_SQL + " WHERE i.id = ?", (cur.lastrowid,)).fetchone()
        return _imported_view(conn, r, db.get_settings(conn))


def update_import(iid: int, body: dict) -> dict:
    with db.connect() as conn:
        if not conn.execute("SELECT 1 FROM imported_products WHERE id = ?", (iid,)).fetchone():
            raise ApiError(404, "No encontrado")
        if "sell_price" in body:
            try:
                price = float(body["sell_price"])
            except (TypeError, ValueError):
                raise ApiError(400, "Precio inválido")
            if price <= 0:
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
        conn.execute("INSERT INTO alerts(imported_id, type, message, created_at) VALUES (?, ?, ?, ?)",
                     (r["id"], t, m, db.now()))
        created.append({"imported_id": r["id"], "title": r["title"], "type": t, "message": m})
    conn.execute("UPDATE imported_products SET last_cost = ?, last_stock = ? WHERE id = ?",
                 (new.cost, new.stock, r["id"]))


def run_monitor() -> dict:
    """Re-consulta precio/stock en el proveedor para cada producto importado y genera alertas."""
    created, checked, skipped = [], 0, 0
    with db.connect() as conn:
        s = db.get_settings(conn)
        for r in conn.execute(IMPORT_SQL).fetchall():
            connector = LIVE_CONNECTORS.get(r["source"])
            if connector is None:
                skipped += 1
                continue
            cur = PriceStock(r["cost"], r["shipping_cost"], r["stock"])
            new = connector.get_price_stock(r["supplier"], current=cur)
            if new is None:
                skipped += 1
                continue
            checked += 1
            conn.execute("UPDATE products SET cost = ?, shipping_cost = ?, stock = ?, updated_at = ? WHERE id = ?",
                         (new.cost, new.shipping_cost, new.stock, db.now(), r["product_id"]))
            _check_imported(conn, r, new, s, created)
    return {"checked": checked, "skipped": skipped, "alerts": created}


# ---------------------------------------------------------------- proveedores / catálogo

def list_suppliers() -> list:
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT p.supplier, p.source, COUNT(*) AS products, COUNT(i.id) AS imported, MAX(p.updated_at) AS updated_at "
            "FROM products p LEFT JOIN imported_products i ON i.product_id = p.id "
            "GROUP BY p.supplier, p.source ORDER BY p.source, p.supplier")]


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
        new, updated = db.upsert_products(conn, parsed.products, "csv")
        if body.get("replace"):
            skus = [p.supplier_sku for p in parsed.products]
            conn.execute("CREATE TEMP TABLE IF NOT EXISTS _skus(sku TEXT PRIMARY KEY)")
            conn.execute("DELETE FROM _skus")
            conn.executemany("INSERT OR IGNORE INTO _skus VALUES (?)", [(k,) for k in skus])
            gone = "supplier = ? AND source = 'csv' AND supplier_sku NOT IN (SELECT sku FROM _skus)"
            # Los que ya importaste no se borran: quedan sin stock (y el monitor lo avisa)
            conn.execute(f"UPDATE products SET stock = 0, updated_at = ? WHERE {gone} "
                         "AND id IN (SELECT product_id FROM imported_products)", (db.now(), supplier))
            removed = conn.execute(f"DELETE FROM products WHERE {gone} "
                                   "AND id NOT IN (SELECT product_id FROM imported_products)", (supplier,)).rowcount
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
    return {"deleted": n}


def list_alerts() -> list:
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT a.*, i.title FROM alerts a JOIN imported_products i ON i.id = a.imported_id "
            "ORDER BY a.id DESC LIMIT 200")]


def mark_alerts_read() -> dict:
    with db.connect() as conn:
        conn.execute("UPDATE alerts SET read = 1")
    return {"ok": True}


# ---------------------------------------------------------------- routing

ROUTES = [
    ("GET", r"/api/products", lambda m, qs, b: search_products(qs)),
    ("GET", r"/api/products/(\d+)", lambda m, qs, b: get_product(int(m[1]))),
    ("GET", r"/api/facets", lambda m, qs, b: facets()),
    ("GET", r"/api/settings", lambda m, qs, b: get_settings()),
    ("PUT", r"/api/settings", lambda m, qs, b: update_settings(b)),
    ("GET", r"/api/imports", lambda m, qs, b: list_imports()),
    ("POST", r"/api/imports", lambda m, qs, b: create_import(b)),
    ("PATCH", r"/api/imports/(\d+)", lambda m, qs, b: update_import(int(m[1]), b)),
    ("DELETE", r"/api/imports/(\d+)", lambda m, qs, b: delete_import(int(m[1]))),
    ("POST", r"/api/monitor/run", lambda m, qs, b: run_monitor()),
    ("GET", r"/api/alerts", lambda m, qs, b: list_alerts()),
    ("GET", r"/api/suppliers", lambda m, qs, b: list_suppliers()),
    ("POST", r"/api/catalog/csv", lambda m, qs, b: upload_csv(b)),
    ("DELETE", r"/api/catalog", lambda m, qs, b: delete_supplier_catalog(qs)),
    ("POST", r"/api/alerts/read", lambda m, qs, b: mark_alerts_read()),
]


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=FRONTEND, **kw)

    def log_message(self, fmt, *args):
        pass

    def _send_json(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _api(self, method):
        url = urlparse(self.path)
        for m, pattern, fn in ROUTES:
            match = re.fullmatch(pattern, url.path)
            if m == method and match:
                try:
                    body = {}
                    length = int(self.headers.get("Content-Length") or 0)
                    if length > MAX_BODY:
                        raise ApiError(413, "Archivo demasiado grande (máx. 20 MB)")
                    if length:
                        body = json.loads(self.rfile.read(length) or b"{}")
                    return self._send_json(200, fn(match, parse_qs(url.query), body))
                except ApiError as e:
                    return self._send_json(e.status, {"error": str(e)})
                except json.JSONDecodeError:
                    return self._send_json(400, {"error": "JSON inválido"})
                except Exception as e:  # noqa: BLE001
                    self.log_error("Error interno: %r", e)
                    return self._send_json(500, {"error": "Error interno del servidor"})
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "Ruta no encontrada"})

    def do_GET(self):
        if self.path.startswith("/api/"):
            return self._api("GET")
        return super().do_GET()

    def do_POST(self):
        self._api("POST")

    def do_PUT(self):
        self._api("PUT")

    def do_PATCH(self):
        self._api("PATCH")

    def do_DELETE(self):
        self._api("DELETE")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    db.init_db()
    print(f"DropFinder en http://{args.host}:{args.port}")
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
