"""Comparador de productos y agrupación del mismo producto en varios proveedores (products.match_key).

Módulo de funcionalidad: expone ROUTES (método, regex, handler(match, qs, body)) que server.py agrega
a sus rutas, y se registra en hooks.on_catalog_write para mantener products.match_key al día.

match_key = "<categoría>|<palabras del título>|<números y medidas en orden>"   (ver key_parts)
  Tokenizador propio (no el del buscador, que es más permisivo a propósito):
  - Minúsculas y sin tildes; "1,5" → "1.5"; un número seguido de su unidad se une ("128 GB" → "128gb",
    "1,5 L" → "1.5l", "10 pulgadas" → "10in"). Se conservan decimales ("3.0") y códigos de modelo con
    guion o dígitos ("USB-C" → "usbc" ≠ "usba", "E27", "2.4A").
  - Palabras: solo se normaliza el plural (lámparas → lampara, cargadores → cargador, luces → luz;
    plegable/plegables → plegabl). NO se quita el género: gorro ≠ gorra, puerta ≠ puerto.
    Se quitan las palabras vacías de KEY_STOPWORDS (artículos y preposiciones largas; las letras sueltas
    "a", "e", "o", "y" se conservan: USB-A a USB-C, vitamina E) y el relleno comercial de NOISE_WORDS.
    Van ordenadas alfabéticamente pero con repetición: "Lámparas LED plegable" == "lampara plegable led",
    y "Cable USB-C a USB-C" ≠ "Cable USB-C".
  - Números y medidas: secuencia EN ORDEN y con repetición ("2 x 3 m" ≠ "3 x 2 m",
    "Hub USB 3.0 de 4 puertos" ≠ "Hub USB 4.0 de 3 puertos", "Agenda 2025" ≠ "Agenda 2026").
    "1 unidad / 1 pieza" se ignora.
  - Coincidencia exacta (conservador): "Lámpara LED" y "Lámpara LED plegable" NO se agrupan. Preferimos no
    mezclar productos distintos a encontrar todos los equivalentes.
  Categoría: es texto libre de cada proveedor ("Hogar", "Hogar y jardín", "Hogar y cocina"…). Para no
  separar el mismo producto por cómo cada uno nombra la categoría, se usa solo su PRIMERA palabra
  significativa ("hogar"); el título igual debe coincidir exactamente. Límite conocido: dos catálogos
  con categorías de nombre distinto ("Casa" vs "Hogar") o un CSV sin categoría ("Sin categoría") no se
  agrupan con los demás; haría falta un mapeo de categorías (siguiente iteración).
  MATCH_KEY_VERSION se guarda en meta: si cambia la normalización, todas las claves se recalculan.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import threading
from functools import lru_cache

import auth_context
import db
import hooks
import search
from errors import ApiError

ROUTES: list = []

# Cambiar cuando cambie la normalización (obliga a recalcular todas las claves; ver _check_version)
MATCH_KEY_VERSION = f"2/{search.STEMMER_VERSION}"
_META_KEY = "match_key_version"

# Palabras vacías de la clave. A diferencia de search.STOPWORDS, NO incluye letras sueltas
# ("a", "e", "o", "y"), que pueden identificar el producto (USB-A, vitamina E).
KEY_STOPWORDS = frozenset({
    "de", "del", "la", "las", "el", "los", "un", "una", "unos", "unas", "en", "con", "para", "por", "al",
    "su", "sus", "lo", "que", "the", "for", "and", "of", "with",
})
# Relleno comercial que no identifica al producto (se comparan en singular: "ofertas" == "oferta").
# No incluye años (agendas, calendarios, modelos), "pc" ni "top" (Ventilador PC, Top deportivo).
NOISE_WORDS = {
    "nuevo", "nueva", "new", "original", "oferta", "promo", "promocion", "descuento", "gratis", "envio",
    "free", "shipping", "calidad", "premium", "hot", "sale", "venta", "tendencia", "unidad", "pieza", "pcs",
    "und", "uds", "x", "producto", "articulo",
}
_UNIT_COUNT = {"unidad", "unidades", "pieza", "piezas", "pcs", "und", "uds"}   # "1 unidad" no cuenta

MAX_COUNT_IDS = 100            # /api/alternatives/counts
MAX_ALTERNATIVES = 50          # filas devueltas por /api/products/{id}/alternatives (el grupo puede ser grande)
COMPARE_MIN, COMPARE_MAX = 2, 4
# Campo → True si gana el mayor valor, False si gana el menor
COMPARE_FIELDS = {
    "total_cost": False, "suggested_price": False, "profit": True, "margin": True, "shipping_days": False,
    "rating": True, "orders": True, "trend": True, "score": True, "stock": True,
}


def _select() -> str:
    """SELECT de productos con imported_id del usuario de la petición (cada usuario tiene sus importados)."""
    uid = int(auth_context.current_user_id())
    return ("SELECT p.*, i.id AS imported_id FROM products p LEFT JOIN imported_products i "
            f"ON i.product_id = p.id AND i.user_id = {uid}")


# ---------------------------------------------------------------- clave de agrupación

_VOWELS = set("aeiou")
_DEC_COMMA = re.compile(r"(?<=\d),(?=\d)")
_UNITS = ("gb|tb|mb|kb|mah|wh|kw|w|v|hz|khz|mhz|ghz|cm|mm|km|m|ml|cl|lt|l|kg|gr|g|mg|oz|lb|pulgadas|pulgada|in|"
          "mp|fps|db|rpm|k|p")
_NUM_UNIT = re.compile(rf"(\d+(?:\.\d+)?)\s+({_UNITS})\b")
_TOKEN = re.compile(r"[a-z0-9]+(?:[.\-][a-z0-9]+)*")


def singular(w: str) -> str:
    """Solo plural (sin género ni diminutivos): luces → luz, lámparas → lampara, cargadores → cargador.
    La "e" final tras consonante se quita siempre para que plegable/plegables y cargador/cargadores
    coincidan (plegabl, cargador). Palabras cortas o con dígitos no se tocan."""
    if len(w) < 4 or not w.isalpha():
        return w
    if len(w) >= 5 and w.endswith("ces") and w[-4] in _VOWELS:
        return w[:-3] + "z"
    if w.endswith("s") and w[-2] in _VOWELS:
        w = w[:-1]
    if len(w) >= 4 and w.endswith("e") and w[-2] not in _VOWELS:
        w = w[:-1]
    return w


_NOISE = frozenset(singular(w) for w in NOISE_WORDS)


def _tokens(text: str | None) -> list[str]:
    t = search.unaccent(text or "")
    t = _DEC_COMMA.sub(".", t)
    t = _NUM_UNIT.sub(lambda m: m[1] + m[2], t)
    out = []
    for tok in _TOKEN.findall(t):
        tok = tok.replace("-", "").rstrip(".")
        tok = re.sub(r"pulgadas?$", "in", tok) if tok[:1].isdigit() else tok
        if tok:
            out.append(tok)
    return out


@lru_cache(maxsize=4096)
def _category_key(category: str | None) -> str:
    words = [singular(t) for t in _tokens(category) if t not in KEY_STOPWORDS and t != "y"]
    return words[0] if words else ""


def key_parts(title: str | None) -> tuple[list[str], list[str]]:
    """(palabras ordenadas con repetición, números/medidas en orden) del título."""
    toks = _tokens(title)
    words, nums = [], []
    for i, tok in enumerate(toks):
        if any(c.isdigit() for c in tok):
            nxt = toks[i + 1] if i + 1 < len(toks) else ""
            if tok == "1" and nxt in _UNIT_COUNT:     # "1 unidad", "1 pieza"
                continue
            nums.append(tok)
        elif tok not in KEY_STOPWORDS:
            w = singular(tok)
            if w not in _NOISE:
                words.append(w)
    if not words and not nums:   # título hecho solo de relleno: se usan todas sus palabras
        words = [singular(t) for t in toks]
    return sorted(words), nums


@lru_cache(maxsize=200_000)
def match_key(title: str | None, category: str | None) -> str:
    """Firma normalizada del producto (ver docstring del módulo). Nunca devuelve '' (= "sin calcular")."""
    words, nums = key_parts(title)
    return f"{_category_key(category)}|{' '.join(words)}|{' '.join(nums)}"


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


def _backfill_empty(conn) -> int:
    return _update(conn, conn.execute("SELECT id, title, category, match_key FROM products WHERE match_key = ''"))


def _check_version(conn) -> bool:
    """Si las claves guardadas son de otra versión de la normalización, las marca para recalcular
    (match_key = '') y guarda la versión actual. Devuelve True si las marcó. Una lectura por PK."""
    r = conn.execute("SELECT value FROM meta WHERE key = ?", (_META_KEY,)).fetchone()
    if r and r[0] == MATCH_KEY_VERSION:
        return False
    conn.execute("UPDATE products SET match_key = '' WHERE match_key != ''")
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (_META_KEY, MATCH_KEY_VERSION))
    return True


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
    if _check_version(conn):
        _backfill_empty(conn)      # cambio de versión: se recalcula todo una vez
    update_keys(conn, ids)


_backfill_lock = threading.Lock()


def _ensure_keys(conn) -> None:
    """Relleno perezoso en el primer uso de la API: bases existentes (v3 → v4), cambio de MATCH_KEY_VERSION
    o escrituras que no pasaron por el hook. Las comprobaciones son una lectura por PK en meta y una por el
    índice ix_products_match: casi gratis.
    Si otra escritura tiene la base bloqueada, se omite (la API responde igual; esas filas no se agrupan
    todavía). Nota: el SELECT ya abrió una transacción de lectura y SQLite no espera (busy handler) al
    pasar de lectura a escritura, así que el UPDATE falla al instante con SQLITE_BUSY: es lo deseable en
    una petición GET."""
    ver = conn.execute("SELECT value FROM meta WHERE key = ?", (_META_KEY,)).fetchone()
    if (ver and ver[0] == MATCH_KEY_VERSION
            and not conn.execute("SELECT 1 FROM products WHERE match_key = '' LIMIT 1").fetchone()):
        return
    with _backfill_lock:
        try:
            conn.execute("SAVEPOINT compare_backfill")
            _check_version(conn)
            _backfill_empty(conn)
            conn.execute("RELEASE compare_backfill")
            conn.commit()
        except sqlite3.OperationalError:
            conn.execute("ROLLBACK TO compare_backfill")
            conn.execute("RELEASE compare_backfill")


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
    rows = conn.execute(f"{_select()} WHERE p.id IN ({','.join('?' * len(ids))})", ids).fetchall()
    item = _server()._item
    return {r["id"]: item(r) for r in rows}


def _best(items: list, field: str, higher: bool, tiebreak=None):
    """Id del mejor valor de `field` (el primero en caso de empate, o el que gane `tiebreak`)."""
    if not items:
        return None
    sign = -1 if higher else 1
    return min(items, key=lambda d: (sign * d[field], *(tiebreak(d) if tiebreak else ())))["id"]


# ---------------------------------------------------------------- handlers

# Ganadores del grupo: orden SQL (con desempates) → el primero gana
# Producto de CJ con envío NO informado (costo 0 y 15 días por defecto): no compite por precio total ni por rapidez
_SHIP_UNKNOWN = "(p.source = 'api' AND p.supplier = 'CJ Dropshipping' AND p.shipping_cost = 0 AND p.shipping_days = 15)"
_SHIP_FIELDS = {"total_cost", "suggested_price", "profit", "margin", "shipping_days"}
_BEST_ORDER = {
    "cheapest": "p.total_cost, p.shipping_days, p.id",
    "fastest": "p.shipping_days, p.total_cost, p.id",
    "best_margin": "p.margin DESC, p.total_cost, p.id",
    "best_rating": "p.rating DESC, p.reviews DESC, p.id",
}


def alternatives(pid: int) -> dict:
    """El mismo producto en otros proveedores (incluye el producto pedido), ordenado por costo total.
    Solo se leen las MAX_ALTERNATIVES más baratas más el producto y los ganadores (calculados en SQL sobre
    todo el grupo), así un grupo enorme (títulos genéricos) no se carga entero en memoria."""
    srv = _server()
    with db.connect() as conn:
        _ensure_keys(conn)
        me = conn.execute("SELECT id, title, category, match_key FROM products WHERE id = ?", (pid,)).fetchone()
        if not me:
            raise ApiError(404, "Producto no encontrado")
        key = me["match_key"] or match_key(me["title"], me["category"])
        where, args = "(p.match_key = ? OR p.id = ?)", (key, pid)
        total = conn.execute(f"SELECT COUNT(*) FROM products p WHERE {where}", args).fetchone()[0]
        best = {}
        for name, order in _BEST_ORDER.items():
            extra = f" AND NOT {_SHIP_UNKNOWN}" if name in ("cheapest", "fastest") else ""
            row = conn.execute(f"SELECT p.id FROM products p WHERE {where}{extra} ORDER BY {order} LIMIT 1",
                               args).fetchone()
            best[name] = row[0] if row else None
        rows = conn.execute(f"{_select()} WHERE {where} ORDER BY p.total_cost, p.id LIMIT ?",
                            (*args, MAX_ALTERNATIVES)).fetchall()
        items = {r["id"]: srv._item(r) for r in rows}
        items.update(_fetch(conn, [i for i in {pid, *best.values()} if i is not None and i not in items]))
    ordered = sorted(items.values(), key=lambda d: (d["total_cost"], d["id"]))
    return {"product_id": pid, "match_key": key, "total": total, "items": ordered, "best": best}


def alternative_counts(qs: dict) -> dict:
    """counts[id] = productos que comparten la clave (incluido él mismo); suppliers[id] = proveedores
    distintos entre ellos (el badge "N proveedores" usa este). Los ids desconocidos se omiten.
    Se agrega una vez por clave distinta (no por id): 100 ids de un grupo de 20.000 cuestan lo mismo que 1."""
    ids = _parse_ids(qs, MAX_COUNT_IDS)
    marks = ",".join("?" * len(ids))
    with db.connect() as conn:
        _ensure_keys(conn)
        keys = {r[0]: r[1] for r in conn.execute(f"SELECT id, match_key FROM products WHERE id IN ({marks})", ids)}
        agg = {r[0]: (r[1], r[2]) for r in conn.execute(
            f"SELECT match_key, COUNT(*), COUNT(DISTINCT supplier) FROM products WHERE match_key IN "
            f"(SELECT match_key FROM products WHERE id IN ({marks}) AND match_key != '') GROUP BY match_key", ids)}
    counts, suppliers = {}, {}
    for pid, key in keys.items():
        n, sup = agg.get(key, (1, 1))
        counts[str(pid)], suppliers[str(pid)] = max(1, n), max(1, sup)
    return {"counts": counts, "suppliers": suppliers}


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
        pool = [d for d in items if d.get("shipping_known", True)] if field in _SHIP_FIELDS else items
        values = {d[field] for d in pool}
        best[field] = _best(pool, field, higher) if len(values) > 1 else None
    return {"items": items, "best": best, "missing": [i for i in ids if i not in found]}


ROUTES += [
    ("GET", r"/api/products/(\d+)/alternatives", lambda m, qs, b: alternatives(int(m[1]))),
    ("GET", r"/api/alternatives/counts", lambda m, qs, b: alternative_counts(qs)),
    ("GET", r"/api/compare", lambda m, qs, b: compare(qs)),
]
