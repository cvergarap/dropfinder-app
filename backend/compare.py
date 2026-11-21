"""Comparador de productos y agrupación del mismo producto en varios proveedores (products.match_key).

Módulo de funcionalidad: expone ROUTES (método, regex, handler(match, qs, body)) que server.py agrega
a sus rutas, y se registra en hooks.on_catalog_write para mantener products.match_key al día.

match_key = "<categoría normalizada>|<raíces del título ordenadas y sin repetir>"
  - Se normaliza con las mismas piezas que el buscador (search.tokenize: minúsculas y sin tildes;
    search.stem: plural/género/diminutivo), así "Lámparas LED plegable" y "lampara plegable led"
    dan la misma clave.
  - Se quitan las palabras vacías del buscador (search.STOPWORDS: "de", "para", "con"…) y las palabras
    de relleno comercial de NOISE_WORDS ("nuevo", "original", "oferta", "envío gratis"…), que los
    proveedores agregan al título pero no cambian el producto.
  - La categoría forma parte de la clave: el mismo título en categorías distintas no se agrupa.
  - Coincidencia exacta del conjunto de raíces (conservador): "Lámpara LED" y "Lámpara LED plegable"
    NO se agrupan. Preferimos no mezclar productos distintos a encontrar todos los equivalentes.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import threading
from functools import lru_cache

import db
import hooks
import search
from errors import ApiError

ROUTES: list = []

# Relleno comercial que no identifica al producto (se comparan por raíz: "nuevos" == "nuevo").
NOISE_WORDS = {
    "nuevo", "nueva", "new", "original", "originales", "oferta", "ofertas", "promo", "promocion", "descuento",
    "gratis", "envio", "free", "shipping", "calidad", "premium", "top", "hot", "sale", "venta", "tendencia", "unidad", "unidades", "pieza", "piezas", "pcs", "pc", "und", "uds",
    "x", "producto", "articulo", "2023", "2024", "2025", "2026", "2027",
}
_NOISE_STEMS = frozenset(search.stem(w) for w in NOISE_WORDS) | frozenset(NOISE_WORDS)
_SKIP = frozenset(search.STOPWORDS) | _NOISE_STEMS

MAX_COUNT_IDS = 100            # /api/alternatives/counts
MAX_ALTERNATIVES = 50          # filas devueltas por /api/products/{id}/alternatives (el grupo puede ser grande)
COMPARE_MIN, COMPARE_MAX = 2, 4
# Campo → True si gana el mayor valor, False si gana el menor
COMPARE_FIELDS = {
    "total_cost": False, "suggested_price": False, "profit": True, "margin": True, "shipping_days": False,
    "rating": True, "orders": True, "trend": True, "score": True, "stock": True,
}
_SELECT = "SELECT p.*, i.id AS imported_id FROM products p LEFT JOIN imported_products i ON i.product_id = p.id"


# ---------------------------------------------------------------- clave de agrupación

def _words(text: str | None, skip=_SKIP) -> list[str]:
    return [search.stem(t) for t in search.tokenize(text) if t not in skip]


@lru_cache(maxsize=200_000)
def match_key(title: str | None, category: str | None) -> str:
    """Firma normalizada del producto (ver docstring del módulo). Nunca devuelve '' (= "sin calcular")."""
    cat = " ".join(sorted(set(_words(category, search.STOPWORDS)))) or " ".join(search.tokenize(category))
    stems = sorted(set(_words(title)))
    if not stems:   # título hecho solo de relleno: se usan todas sus palabras
        stems = sorted({search.stem(t) for t in search.tokenize(title)})
    return f"{cat}|{' '.join(stems)}"


def _update(conn, rows) -> int:
    """Calcula la clave de cada fila (id, title, category, match_key) y escribe solo las que cambian."""
    n = 0
    for chunk in iter(lambda: rows.fetchmany(5000), []):
        changes = []
        for r in chunk:
            k = match_key(r[1], r[2]) if r[1] and r[1].strip() else f"#{r[0]}"
            if k != r[3]:
                changes.append((k, r[0]))
        if changes:
            conn.executemany("UPDATE products SET match_key = ? WHERE id = ?", changes)
            n += len(changes)
    return n


def update_keys(conn, ids=None) -> int:
    """Recalcula match_key. ids=None: filas sin clave y las de la última carga (mismo updated_at máximo)."""
    if ids is None:
        rows = conn.execute("SELECT id, title, category, match_key FROM products WHERE match_key = '' "
                            "OR updated_at = (SELECT MAX(updated_at) FROM products)")
        return _update(conn, rows)
    ids = [int(i) for i in ids]
    if len(ids) > 900:      # cargas grandes: una sola consulta (json_each) en vez de muchos IN (...)
        rows = conn.execute("SELECT id, title, category, match_key FROM products "
                            "WHERE id IN (SELECT value FROM json_each(?))", (json.dumps(ids),))
        return _update(conn, rows)
    if not ids:
        return 0
    rows = conn.execute(f"SELECT id, title, category, match_key FROM products "
                        f"WHERE id IN ({','.join('?' * len(ids))})", ids)
    return _update(conn, rows)


@hooks.on_catalog_write
def _on_catalog_write(conn, ids) -> None:
    """Se ejecuta dentro de la transacción que escribió el catálogo (misma conexión)."""
    update_keys(conn, ids)


_backfill_lock = threading.Lock()


def _ensure_keys(conn) -> None:
    """Relleno perezoso: bases existentes (v3 → v4) o escrituras que no pasaron por el hook dejan filas con
    match_key = ''. La comprobación usa el índice ix_products_match y es casi gratis. Si otra escritura
    larga tiene la base bloqueada, se omite (la API responde igual: esas filas no se agrupan todavía)."""
    if not conn.execute("SELECT 1 FROM products WHERE match_key = '' LIMIT 1").fetchone():
        return
    with _backfill_lock:
        conn.execute("PRAGMA busy_timeout = 500")
        try:
            conn.execute("SAVEPOINT compare_backfill")
            rows = conn.execute("SELECT id, title, category, match_key FROM products WHERE match_key = ''")
            _update(conn, rows)
            conn.execute("RELEASE compare_backfill")
            conn.commit()
        except sqlite3.OperationalError:
            conn.execute("ROLLBACK TO compare_backfill")
            conn.execute("RELEASE compare_backfill")
        finally:
            conn.execute("PRAGMA busy_timeout = 5000")


# ---------------------------------------------------------------- utilidades de la API

def _server():
    """Módulo del servidor (para server._item, el formato de producto de la API). server.py importa este
    módulo, así que se resuelve en diferido. Si el servidor corre como script (`python3 server.py`) su
    módulo es __main__; se usa ese en vez de importar "server" (crearía una segunda copia del módulo)."""
    mod = sys.modules.get("server")
    if mod is None:
        main = sys.modules.get("__main__")
        if main is not None and hasattr(main, "_item") and hasattr(main, "Handler"):
            return main
        import server as mod  # noqa: PLC0415
    return mod


def _parse_ids(qs: dict, max_n: int) -> list[int]:
    raw = ",".join(qs.get("ids", []))
    parts = [x.strip() for x in raw.split(",") if x.strip()]
    if not parts:
        raise ApiError(400, "Indica los ids de los productos (ids=1,2,3)")
    if not all(re.fullmatch(r"\d{1,12}", x) for x in parts):
        raise ApiError(400, "Parámetro inválido: ids")
    ids = list(dict.fromkeys(int(x) for x in parts))      # sin repetidos, en el orden pedido
    if len(ids) > max_n:
        raise ApiError(400, f"Máximo {max_n} productos")
    return ids


def _fetch(conn, ids) -> dict:
    if not ids:
        return {}
    rows = conn.execute(f"{_SELECT} WHERE p.id IN ({','.join('?' * len(ids))})", ids).fetchall()
    item = _server()._item
    return {r["id"]: item(r) for r in rows}


def _best(items: list, field: str, higher: bool, tiebreak=None):
    """Id del mejor valor de `field` (el primero en caso de empate, o el que gane `tiebreak`)."""
    if not items:
        return None
    sign = -1 if higher else 1
    return min(items, key=lambda d: (sign * d[field], *(tiebreak(d) if tiebreak else ())))["id"]


# ---------------------------------------------------------------- handlers

def alternatives(pid: int) -> dict:
    """El mismo producto en otros proveedores (incluye el producto pedido), ordenado por costo total."""
    srv = _server()
    with db.connect() as conn:
        _ensure_keys(conn)
        me = conn.execute("SELECT id, title, category, match_key FROM products WHERE id = ?", (pid,)).fetchone()
        if not me:
            raise ApiError(404, "Producto no encontrado")
        key = me["match_key"] or match_key(me["title"], me["category"])
        rows = conn.execute(f"{_SELECT} WHERE p.match_key = ? OR p.id = ?", (key, pid)).fetchall()
    group = [srv._item(r) for r in rows]
    by_cost = lambda d: (d["total_cost"], d["id"])  # noqa: E731
    best = {
        "cheapest": _best(group, "total_cost", False, lambda d: (d["shipping_days"], d["id"])),
        "fastest": _best(group, "shipping_days", False, lambda d: by_cost(d)),
        "best_margin": _best(group, "margin", True, lambda d: by_cost(d)),
        "best_rating": _best(group, "rating", True, lambda d: (-d["reviews"], d["id"])),
    }
    group.sort(key=by_cost)
    keep = {pid, *best.values()}
    items = [d for i, d in enumerate(group) if i < MAX_ALTERNATIVES or d["id"] in keep]
    return {"product_id": pid, "match_key": key, "total": len(group), "items": items, "best": best}


def alternative_counts(qs: dict) -> dict:
    """counts[id] = productos que comparten la clave (incluido él mismo); suppliers[id] = proveedores
    distintos entre ellos (el badge "N proveedores" usa este). Los ids desconocidos se omiten."""
    ids = _parse_ids(qs, MAX_COUNT_IDS)
    with db.connect() as conn:
        _ensure_keys(conn)
        rows = conn.execute(
            f"SELECT p.id, COUNT(q.id), COUNT(DISTINCT q.supplier) FROM products p "
            f"LEFT JOIN products q ON q.match_key = p.match_key AND p.match_key != '' "
            f"WHERE p.id IN ({','.join('?' * len(ids))}) GROUP BY p.id", ids).fetchall()
    return {"counts": {str(r[0]): max(1, r[1]) for r in rows},
            "suppliers": {str(r[0]): max(1, r[2]) for r in rows}}


def compare(qs: dict) -> dict:
    """Comparación lado a lado de 2 a 4 productos con el mejor valor de cada métrica.
    best[campo] = id del ganador (el primero si hay empate) o None si todos empatan."""
    ids = _parse_ids(qs, COMPARE_MAX)
    if len(ids) < COMPARE_MIN:
        raise ApiError(400, f"Elige entre {COMPARE_MIN} y {COMPARE_MAX} productos para comparar")
    with db.connect() as conn:
        found = _fetch(conn, ids)
    items = [found[i] for i in ids if i in found]
    best = {}
    for field, higher in COMPARE_FIELDS.items():
        values = {d[field] for d in items}
        best[field] = _best(items, field, higher) if len(values) > 1 else None
    return {"items": items, "best": best, "missing": [i for i in ids if i not in found]}


ROUTES += [
    ("GET", r"/api/products/(\d+)/alternatives", lambda m, qs, b: alternatives(int(m[1]))),
    ("GET", r"/api/alternatives/counts", lambda m, qs, b: alternative_counts(qs)),
    ("GET", r"/api/compare", lambda m, qs, b: compare(qs)),
]
