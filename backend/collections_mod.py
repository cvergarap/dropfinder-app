"""(collections_mod: el nombre 'collections' choca con la librería estándar.)

Favoritos, colecciones y exportación a CSV/Excel.

Módulo de funcionalidad: expone ROUTES (método, regex, handler(match, qs, body)) que server.py agrega.
Datos por usuario: cada consulta filtra por auth_context.current_user_id(). Una colección de otro
usuario responde 404 (no 403), igual que una que no existe: no se revela que existe.

Tablas (se crean al primer uso con ensure_schema, sin tocar db.py):
  collections        id, user_id, name, description, color, is_favorites, created_at, updated_at
                     "Favoritos" es una colección especial por usuario (is_favorites = 1): se crea al
                     primer uso, no se puede borrar ni renombrar y no cuenta para el tope de colecciones.
  collection_items   id (orden de llegada: el más reciente primero), collection_id, product_id, note,
                     added_at. UNIQUE (collection_id, product_id): agregar dos veces no duplica.
                     ON DELETE CASCADE hacia collections y products: si el proveedor retira el producto
                     del catálogo, desaparece también de las colecciones.
  user_id no tiene FOREIGN KEY a users: en una instalación sin cuentas el usuario 1 no existe en users.

Exportación (GET /api/export/{search|collection|imports}.csv): CSV pensado para Excel en español.
  - UTF-8 con BOM (sin él, Excel en Windows lee las tildes como "Ã¡") y fin de línea CRLF.
  - sep=';' por defecto: Excel con configuración regional es-CO/es-ES usa ';' como separador de listas
    y ',' como decimal; con sep=';' los números van con coma decimal. sep=',' usa punto decimal.
  - Números sin separador de miles (Excel los reconoce como número en cualquier configuración).
  - currency=USD|COP (por defecto, la moneda que ve el usuario). En COP: precios de venta con el
    redondeo comercial del usuario (currency.round_price); costo, envío y ganancia con redondeo simple
    (currency.convert). Los encabezados de dinero llevan la moneda: "Costo (COP)".
  - Inyección de fórmulas: las celdas de TEXTO que empiezan con = + - @ tab o CR llevan un apóstrofo
    delante (Excel/LibreOffice las ejecutarían como fórmula). Las celdas numéricas las genera la app
    (p. ej. una ganancia negativa "-3,50") y no se alteran.
"""
from __future__ import annotations

import csv
import io
import math
import os
import re
import sys
from datetime import datetime

import auth_context
import currency
import db
from errors import ApiError, FileResponse
from metrics import profit_for

ROUTES: list = []

MAX_COLLECTIONS = 100          # por usuario (sin contar Favoritos)
MAX_ITEMS = 2000               # por colección
MAX_NAME = 60
MAX_DESCRIPTION = 200
MAX_NOTE = 500
MAX_IDS_QUERY = 200            # /api/favorites/ids
EXPORT_CAP = 10_000            # filas máximas de una exportación
PAGE_SIZE = 48
FAVORITES_NAME = "Favoritos"
# Paleta pequeña: el frontend la pinta con sus propios tokens (claro/oscuro)
COLORS = ("indigo", "teal", "green", "amber", "orange", "rose", "violet", "slate")
DEFAULT_COLOR = "indigo"
FAVORITES_COLOR = "rose"

ITEM_SORTS = {
    "added": "ci.id DESC", "added_asc": "ci.id", "title": "p.title COLLATE NOCASE, p.id",
    "score": "p.score DESC, p.id DESC", "margin": "p.margin DESC, p.id DESC", "profit": "p.profit DESC, p.id DESC",
    "orders": "p.orders DESC, p.id DESC", "price_asc": "p.suggested_price, p.id",
    "price_desc": "p.suggested_price DESC, p.id DESC",
}

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS collections (
      id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,
      name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', color TEXT NOT NULL DEFAULT 'indigo',
      is_favorites INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS ix_collections_user ON collections(user_id, updated_at)",
    # Un solo "Favoritos" por usuario, aunque dos peticiones lo creen a la vez
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_collections_fav ON collections(user_id) WHERE is_favorites = 1",
    """CREATE TABLE IF NOT EXISTS collection_items (
      id INTEGER PRIMARY KEY,
      collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
      product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
      note TEXT NOT NULL DEFAULT '', added_at TEXT NOT NULL,
      UNIQUE (collection_id, product_id))""",
    "CREATE INDEX IF NOT EXISTS ix_collection_items_product ON collection_items(product_id)",
)


def ensure_schema(conn) -> None:
    """Crea las tablas del módulo si faltan (idempotente y barato: se llama en cada petición)."""
    for stmt in SCHEMA:
        conn.execute(stmt)


def _open():
    conn = db.connect()
    ensure_schema(conn)
    conn.commit()
    return conn


def _srv():
    """Módulo del servidor (search_products, _item), importado tarde: server.py importa este módulo.
    Si el servidor corre como script (`python3 server.py`) se usa ese mismo módulo (__main__)."""
    mod = sys.modules.get("server")
    if mod is None:
        main = sys.modules.get("__main__")
        if main is not None and hasattr(main, "search_products") and hasattr(main, "_item"):
            return main
        import server as mod  # noqa: PLC0415
    return mod


# SOLO DESARROLLO: DROPFINDER_DEV_USER=<id> hace que las peticiones sin usuario actúen como ese usuario
# (para probar este módulo en una rama sin el middleware de autenticación). Nunca usarlo en producción:
# con cuentas de usuario, server.py fija el usuario real de cada petición y esta variable no debe existir.
_DEV_USER = os.environ.get("DROPFINDER_DEV_USER", "").strip()


def _uid() -> int:
    """Usuario de la petición. Sin usuario (sin sesión) → 401."""
    try:
        return auth_context.current_user_id()
    except auth_context.NoUserContext:
        if _DEV_USER.isdigit():
            return int(_DEV_USER)
        raise ApiError(401, "Inicia sesión") from None


# ---------------------------------------------------------------- validación

def _clean_text(value, label: str, max_len: int, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ApiError(400, f"{label} inválido")
    text = " ".join(value.split())
    if required and not text:
        raise ApiError(400, f"{label}: escribe entre 1 y {max_len} caracteres")
    if len(text) > max_len:
        raise ApiError(400, f"{label}: máximo {max_len} caracteres")
    return text


def _clean_note(value) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ApiError(400, "Nota inválida")
    text = value.strip().replace("\r\n", "\n").replace("\r", "\n")
    if len(text) > MAX_NOTE:
        raise ApiError(400, f"La nota admite como máximo {MAX_NOTE} caracteres")
    return text


def _clean_color(value) -> str:
    if value is None or value == "":
        return DEFAULT_COLOR
    if value not in COLORS:
        raise ApiError(400, f"Color inválido. Opciones: {', '.join(COLORS)}")
    return value


def _body(body) -> dict:
    if not isinstance(body, dict):
        raise ApiError(400, "JSON inválido")
    return body


def _product_ids(value, max_n: int = MAX_ITEMS) -> list[int]:
    if isinstance(value, (int, str)) and not isinstance(value, bool):
        value = [value]
    if not isinstance(value, list) or not value:
        raise ApiError(400, "Indica product_ids (lista de ids de productos)")
    ids = []
    for v in value:
        if isinstance(v, bool) or not (isinstance(v, int) or (isinstance(v, str) and v.isdigit())):
            raise ApiError(400, "product_ids inválido")
        ids.append(int(v))
    ids = list(dict.fromkeys(ids))
    if len(ids) > max_n:
        raise ApiError(400, f"Máximo {max_n} productos por petición")
    return ids


def _name_taken(conn, name: str, exclude_id: int | None = None) -> bool:
    """¿Nombre repetido entre MIS colecciones (sin distinguir mayúsculas)? "Favoritos" está reservado."""
    if name.casefold() == FAVORITES_NAME.casefold():
        return True
    return any(r["id"] != exclude_id and r["name"].casefold() == name.casefold()
               for r in conn.execute("SELECT id, name FROM collections WHERE user_id = ?", (_uid(),)))


# ---------------------------------------------------------------- acceso

def _get(conn, cid: int):
    """Colección del usuario actual o 404 (también si es de otro usuario)."""
    r = conn.execute("SELECT * FROM collections WHERE id = ? AND user_id = ?", (cid, _uid())).fetchone()
    if not r:
        raise ApiError(404, "Colección no encontrada")
    return r


def _favorites_id(conn, create: bool = True) -> int | None:
    r = conn.execute("SELECT id FROM collections WHERE user_id = ? AND is_favorites = 1", (_uid(),)).fetchone()
    if r or not create:
        return r[0] if r else None
    ts = db.now()
    conn.execute("INSERT OR IGNORE INTO collections(user_id, name, description, color, is_favorites, created_at, "
                 "updated_at) VALUES (?, ?, '', ?, 1, ?, ?)", (_uid(), FAVORITES_NAME, FAVORITES_COLOR, ts, ts))
    return conn.execute("SELECT id FROM collections WHERE user_id = ? AND is_favorites = 1", (_uid(),)).fetchone()[0]


def _touch(conn, cid: int) -> None:
    conn.execute("UPDATE collections SET updated_at = ? WHERE id = ?", (db.now(), cid))


def _count(conn, cid: int) -> int:
    return conn.execute("SELECT COUNT(*) FROM collection_items WHERE collection_id = ?", (cid,)).fetchone()[0]


def _view(conn, r) -> dict:
    prev = conn.execute("SELECT p.id, p.title, p.category, p.image FROM collection_items ci "
                        "JOIN products p ON p.id = ci.product_id WHERE ci.collection_id = ? "
                        "ORDER BY ci.id DESC LIMIT 4", (r["id"],)).fetchall()
    return {
        "id": r["id"], "name": r["name"], "description": r["description"], "color": r["color"],
        "is_favorites": bool(r["is_favorites"]), "count": _count(conn, r["id"]),
        "created_at": r["created_at"], "updated_at": r["updated_at"],
        # Las fotos pueden venir vacías: `preview` trae título y categoría para dibujar la ilustración
        "preview_images": [p["image"] or "" for p in prev],
        "preview": [dict(p) for p in prev],
    }


# ---------------------------------------------------------------- colecciones

def list_collections() -> dict:
    with _open() as conn:
        _favorites_id(conn)       # Favoritos siempre existe (se crea la primera vez)
        rows = conn.execute("SELECT * FROM collections WHERE user_id = ? "
                            "ORDER BY is_favorites DESC, updated_at DESC, id DESC", (_uid(),)).fetchall()
        return {"items": [_view(conn, r) for r in rows], "max": MAX_COLLECTIONS, "max_items": MAX_ITEMS,
                "colors": list(COLORS)}


def create_collection(body) -> dict:
    body = _body(body)
    name = _clean_text(body.get("name"), "Nombre", MAX_NAME, required=True)
    desc = _clean_text(body.get("description"), "Descripción", MAX_DESCRIPTION)
    color = _clean_color(body.get("color"))
    with _open() as conn:
        conn.execute("BEGIN IMMEDIATE")    # tope y nombre repetido con el bloqueo de escritura tomado
        n = conn.execute("SELECT COUNT(*) FROM collections WHERE user_id = ? AND is_favorites = 0",
                         (_uid(),)).fetchone()[0]
        if n >= MAX_COLLECTIONS:
            raise ApiError(409, f"Llegaste al máximo de {MAX_COLLECTIONS} colecciones. Elimina alguna.")
        if _name_taken(conn, name):
            raise ApiError(409, f"Ya tienes una colección llamada «{name}»")
        ts = db.now()
        cid = conn.execute("INSERT INTO collections(user_id, name, description, color, created_at, updated_at) "
                           "VALUES (?, ?, ?, ?, ?, ?)", (_uid(), name, desc, color, ts, ts)).lastrowid
        return _view(conn, _get(conn, cid))


def update_collection(cid: int, body) -> dict:
    body = _body(body)
    with _open() as conn:
        r = _get(conn, cid)
        sets, args = [], []
        if "name" in body:
            name = _clean_text(body["name"], "Nombre", MAX_NAME, required=True)
            if r["is_favorites"]:
                if name != r["name"]:
                    raise ApiError(400, "La colección Favoritos no se puede renombrar")
            elif _name_taken(conn, name, exclude_id=cid):
                raise ApiError(409, f"Ya tienes una colección llamada «{name}»")
            else:
                sets.append("name = ?")
                args.append(name)
        if "description" in body:
            sets.append("description = ?")
            args.append(_clean_text(body["description"], "Descripción", MAX_DESCRIPTION))
        if "color" in body:
            sets.append("color = ?")
            args.append(_clean_color(body["color"]))
        if sets:
            conn.execute(f"UPDATE collections SET {', '.join(sets)}, updated_at = ? WHERE id = ?",
                         (*args, db.now(), cid))
        return _view(conn, _get(conn, cid))


def delete_collection(cid: int) -> dict:
    with _open() as conn:
        if _get(conn, cid)["is_favorites"]:
            raise ApiError(400, "La colección Favoritos no se puede eliminar")
        conn.execute("DELETE FROM collections WHERE id = ?", (cid,))     # ítems: ON DELETE CASCADE
    return {"ok": True}


# ---------------------------------------------------------------- productos de una colección

def _items_sql(sort: str) -> str:
    # imported_id: el importado de ESTE usuario (un producto lo pueden importar varios usuarios)
    return ("SELECT p.*, i.id AS imported_id, ci.note, ci.added_at FROM collection_items ci "
            "JOIN products p ON p.id = ci.product_id "
            "LEFT JOIN imported_products i ON i.product_id = p.id AND i.user_id = ? "
            f"WHERE ci.collection_id = ? ORDER BY {ITEM_SORTS[sort]}")


def _sort(qs: dict) -> str:
    sort = qs.get("sort", ["added"])[0] or "added"
    if sort not in ITEM_SORTS:
        raise ApiError(400, f"Orden inválido. Opciones: {', '.join(ITEM_SORTS)}")
    return sort


def _int_param(qs: dict, key: str, default: int, lo: int, hi: int) -> int:
    raw = qs.get(key, [""])[0].strip()
    if not raw:
        return default
    if not raw.isdigit():
        raise ApiError(400, f"Parámetro inválido: {key}")
    return min(hi, max(lo, int(raw)))


def list_items(cid: int, qs: dict) -> dict:
    sort = _sort(qs)
    size = _int_param(qs, "page_size", PAGE_SIZE, 1, 100)
    page = _int_param(qs, "page", 1, 1, 10_000)
    item = _srv()._item
    with _open() as conn:
        coll = _view(conn, _get(conn, cid))
        total = coll["count"]
        rows = conn.execute(_items_sql(sort) + " LIMIT ? OFFSET ?",
                            (_uid(), cid, size, (page - 1) * size)).fetchall()
        return {"collection": coll, "total": total, "page": page, "page_size": size,
                "pages": math.ceil(total / size) if total else 0, "sort": sort,
                "items": [item(r) for r in rows]}


def _add(conn, cid: int, ids: list[int], note: str | None) -> dict:
    """Agrega productos. Los que ya estaban no se duplican ni cambian de posición (si se envía una nota,
    se actualiza también en ellos). Comprueba el tope de MAX_ITEMS."""
    found = {r[0] for r in conn.execute(f"SELECT id FROM products WHERE id IN ({','.join('?' * len(ids))})", ids)}
    missing = [i for i in ids if i not in found]
    ids = [i for i in ids if i in found]
    if not ids:
        raise ApiError(404, "Producto no encontrado")
    have = {r[0] for r in conn.execute(
        f"SELECT product_id FROM collection_items WHERE collection_id = ? "
        f"AND product_id IN ({','.join('?' * len(ids))})", [cid, *ids])}
    new = [i for i in ids if i not in have]
    count = _count(conn, cid)
    if count + len(new) > MAX_ITEMS:
        raise ApiError(409, f"Una colección admite como máximo {MAX_ITEMS} productos "
                            f"(tiene {count}; intentas agregar {len(new)})")
    ts = db.now()
    conn.executemany("INSERT OR IGNORE INTO collection_items(collection_id, product_id, note, added_at) "
                     "VALUES (?, ?, ?, ?)", [(cid, i, note or "", ts) for i in new])
    if note is not None:
        conn.executemany("UPDATE collection_items SET note = ? WHERE collection_id = ? AND product_id = ?",
                         [(note, cid, i) for i in ids if i in have])
    if new or note is not None:
        _touch(conn, cid)
    return {"added": len(new), "already": len(ids) - len(new), "missing": missing, "count": count + len(new)}


def add_items(cid: int, body) -> dict:
    body = _body(body)
    ids = _product_ids(body.get("product_ids", body.get("product_id")))
    note = _clean_note(body["note"]) if body.get("note") is not None else None
    with _open() as conn:
        conn.execute("BEGIN IMMEDIATE")
        _get(conn, cid)
        return _add(conn, cid, ids, note)


def remove_item(cid: int, pid: int) -> dict:
    with _open() as conn:
        _get(conn, cid)
        n = conn.execute("DELETE FROM collection_items WHERE collection_id = ? AND product_id = ?",
                         (cid, pid)).rowcount
        if n:
            _touch(conn, cid)
        return {"ok": True, "removed": bool(n), "count": _count(conn, cid)}


def update_item(cid: int, pid: int, body) -> dict:
    body = _body(body)
    if "note" not in body:
        raise ApiError(400, "Indica la nota")
    note = _clean_note(body["note"])
    with _open() as conn:
        _get(conn, cid)
        n = conn.execute("UPDATE collection_items SET note = ? WHERE collection_id = ? AND product_id = ?",
                         (note, cid, pid)).rowcount
        if not n:
            raise ApiError(404, "El producto no está en la colección")
        _touch(conn, cid)
    return {"product_id": pid, "note": note}


# ---------------------------------------------------------------- favoritos

def toggle_favorite(pid: int) -> dict:
    with _open() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if not conn.execute("SELECT 1 FROM products WHERE id = ?", (pid,)).fetchone():
            raise ApiError(404, "Producto no encontrado")
        fid = _favorites_id(conn)
        if conn.execute("DELETE FROM collection_items WHERE collection_id = ? AND product_id = ?",
                        (fid, pid)).rowcount:
            _touch(conn, fid)
            on = False
        else:
            _add(conn, fid, [pid], None)
            on = True
        return {"product_id": pid, "favorite": on, "total": _count(conn, fid)}


def favorite_ids(qs: dict) -> dict:
    """{"ids": favoritos entre los pedidos (en el orden pedido), "total": cuántos favoritos tengo}.
    Sin ids devuelve solo el total (pastilla de la pestaña). No crea Favoritos."""
    raw = ",".join(qs.get("ids", []))
    parts = [x.strip() for x in raw.split(",") if x.strip()]
    if not all(re.fullmatch(r"\d{1,12}", x) for x in parts):
        raise ApiError(400, "Parámetro inválido: ids")
    ids = list(dict.fromkeys(int(x) for x in parts))
    if len(ids) > MAX_IDS_QUERY:
        raise ApiError(400, f"Máximo {MAX_IDS_QUERY} ids")
    with _open() as conn:
        fid = _favorites_id(conn, create=False)
        if fid is None:
            return {"ids": [], "total": 0}
        have = set()
        if ids:
            have = {r[0] for r in conn.execute(
                f"SELECT product_id FROM collection_items WHERE collection_id = ? "
                f"AND product_id IN ({','.join('?' * len(ids))})", [fid, *ids])}
        return {"ids": [i for i in ids if i in have], "total": _count(conn, fid)}


def product_collections(pid: int) -> dict:
    """Todas mis colecciones con contains/note para un producto (casillas del detalle);
    collection_ids = las que lo contienen."""
    with _open() as conn:
        if not conn.execute("SELECT 1 FROM products WHERE id = ?", (pid,)).fetchone():
            raise ApiError(404, "Producto no encontrado")
        _favorites_id(conn)
        rows = conn.execute(
            "SELECT c.id, c.name, c.color, c.is_favorites, ci.note, ci.product_id IS NOT NULL AS has_it, "
            "(SELECT COUNT(*) FROM collection_items x WHERE x.collection_id = c.id) AS n "
            "FROM collections c LEFT JOIN collection_items ci ON ci.collection_id = c.id AND ci.product_id = ? "
            "WHERE c.user_id = ? ORDER BY c.is_favorites DESC, c.updated_at DESC, c.id DESC", (pid, _uid())).fetchall()
    items = [{"id": r["id"], "name": r["name"], "color": r["color"], "is_favorites": bool(r["is_favorites"]),
              "contains": bool(r["has_it"]), "note": r["note"] or "", "count": r["n"]} for r in rows]
    return {"product_id": pid, "collection_ids": [c["id"] for c in items if c["contains"]], "items": items}


# ---------------------------------------------------------------- exportación CSV

FORMULA_CHARS = ("=", "+", "-", "@", "\t", "\r")
BASE_COLUMNS = ["Título", "Proveedor", "SKU", "Categoría", "Costo", "Envío", "Costo total", "Precio sugerido",
                "Ganancia", "Margen %", "Días de envío", "Almacén", "Stock", "Pedidos", "Rating", "Score", "Enlace"]
MONEY_COLUMNS = {"Costo", "Envío", "Costo total", "Precio sugerido", "Ganancia", "Precio de venta"}
STATUS_NAMES = {"active": "Activo", "draft": "Borrador"}


def safe_text(value) -> str:
    """Texto de una celda, neutralizando fórmulas (=1+1, +cmd, -2+3, @SUM…) con un apóstrofo."""
    s = "" if value is None else str(value)
    return "'" + s if s.startswith(FORMULA_CHARS) else s


class _Formatter:
    """Convierte y formatea los números de la exportación (moneda y separador decimal)."""

    def __init__(self, state: dict, code: str, dec_comma: bool):
        self.state, self.code, self.dec_comma = state, code, dec_comma
        meta = currency.CURRENCIES[code]
        self.decimals = meta["decimals"]
        self.rounding = state["settings"].get(meta["rounding_setting"], "none") if meta["rounding_setting"] else "none"

    def num(self, v, decimals: int) -> str:
        if v is None:
            return ""
        v = round(float(v), decimals)
        s = f"{(0.0 if v == 0 else v):.{decimals}f}"           # sin "-0,00"
        return s.replace(".", ",") if self.dec_comma else s

    def amount(self, usd) -> str:
        """Costo, envío, ganancia: conversión con redondeo simple."""
        if self.code == "USD":
            return self.num(usd, 2)
        return self.num(currency.convert(usd, self.state, self.code), self.decimals)

    def price(self, usd) -> str:
        """Precio de venta: redondeo comercial en moneda local (p. ej. terminado en 900)."""
        if self.code == "USD":
            return self.num(usd, 2)
        rate = self.state["rates"][self.code]
        return self.num(currency.round_price(float(usd) * rate, self.code, self.rounding), self.decimals)


def _row(p: dict, f: _Formatter, profit=None, margin=None) -> list:
    return [
        safe_text(p["title"]), safe_text(p["supplier"]), safe_text(p["supplier_sku"]), safe_text(p["category"]),
        f.amount(p["cost"]), f.amount(p["shipping_cost"]), f.amount(p["total_cost"]),
        f.price(p["suggested_price"]),
        f.amount(p["profit"] if profit is None else profit),
        f.num(p["margin"] if margin is None else margin, 1),
        str(int(p["shipping_days"])), safe_text(p["warehouse"]), str(int(p["stock"])), str(int(p["orders"])),
        f.num(p["rating"], 1), str(int(p["score"])), safe_text(p.get("product_url") or ""),
    ]


def _export_opts(qs: dict, conn) -> tuple[str, _Formatter]:
    sep = qs.get("sep", [";"])[0] or ";"
    if sep not in (";", ","):
        raise ApiError(400, "Separador inválido: usa ; o ,")
    state = currency.get_state(conn)
    code = (qs.get("currency", [""])[0] or state["settings"].get("display_currency") or "USD").upper()
    if code not in currency.CURRENCIES:
        raise ApiError(400, f"Moneda no soportada. Opciones: {', '.join(currency.SUPPORTED)}")
    if not state["rates"].get(code):
        raise ApiError(400, f"No hay tasa de cambio disponible para {code}")
    return sep, _Formatter(state, code, dec_comma=(sep == ";"))


def _headers(extra: list, code: str) -> list:
    return [f"{c} ({code})" if c in MONEY_COLUMNS else c for c in BASE_COLUMNS + extra]


def csv_bytes(header: list, rows: list, sep: str) -> bytes:
    """UTF-8 con BOM, CRLF, comillas solo donde hace falta (separador, comillas o saltos de línea)."""
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=sep, quotechar='"', quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
    w.writerow(header)
    w.writerows(rows)
    return ("﻿" + buf.getvalue()).encode("utf-8")


def _file(kind: str, header: list, rows: list, sep: str, truncated: bool) -> FileResponse:
    day = datetime.now(currency.CO_TZ).strftime("%Y%m%d")     # fecha de Colombia
    return FileResponse(csv_bytes(header, rows, sep), "text/csv; charset=utf-8",
                        filename=f"dropfinder-{kind}-{day}.csv",
                        headers={"X-Export-Rows": str(len(rows)), "X-Export-Truncated": "1" if truncated else "0",
                                 "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


def search_export_items(qs: dict, cap: int = EXPORT_CAP) -> tuple[list, int]:
    """Todos los productos de una búsqueda (mismos parámetros que /api/products), en el mismo orden,
    hasta `cap`. Reutiliza server.search_products página a página (mismos filtros, orden y
    autocorrección); desde la 2.ª página busca el texto efectivo con exact=1 para que la corrección no
    cambie entre páginas. Devuelve (items, total)."""
    srv = _srv()
    q = {k: list(v) for k, v in qs.items() if k not in ("page", "page_size", "sep", "currency", "id")}
    q["page_size"] = ["100"]
    first = srv.search_products({**q, "page": ["1"]})
    total = first["total"]
    items, seen = [], set()

    def take(batch):
        for it in batch:
            if it["id"] not in seen and len(items) < cap:
                seen.add(it["id"])
                items.append(it)

    take(first["items"])
    q.update(q=[first.get("query") or ""], sort=[first["sort"]], exact=["1"])
    page = 1
    while len(items) < min(total, cap) and page < first["pages"]:
        page += 1
        batch = srv.search_products({**q, "page": [str(page)]})["items"]
        if not batch:
            break
        take(batch)
    return items, total


def export_csv(kind: str, qs: dict) -> FileResponse:
    with _open() as conn:
        sep, f = _export_opts(qs, conn)
        if kind == "collection":
            raw = qs.get("id", [""])[0].strip()
            if not raw.isdigit():
                raise ApiError(400, "Indica la colección (id)")
            cid = int(raw)
            _get(conn, cid)
            item = _srv()._item
            data = [item(r) for r in conn.execute(_items_sql(_sort(qs)) + " LIMIT ?", (_uid(), cid, EXPORT_CAP))]
            return _file(kind, _headers(["Nota"], f.code), [_row(p, f) + [safe_text(p["note"])] for p in data],
                         sep, False)
        if kind == "imports":
            settings = db.get_settings(conn)
            total = conn.execute("SELECT COUNT(*) FROM imported_products WHERE user_id = ?", (_uid(),)).fetchone()[0]
            data = conn.execute(
                "SELECT p.*, i.title AS import_title, i.sell_price, i.status FROM imported_products i "
                "JOIN products p ON p.id = i.product_id WHERE i.user_id = ? "
                "ORDER BY i.created_at DESC, i.id DESC LIMIT ?", (_uid(), EXPORT_CAP)).fetchall()
            rows = []
            for r in data:
                p = dict(r)
                p["title"] = p["import_title"]          # el título que el usuario le puso al importar
                # Ganancia y margen con MI precio de venta (como la tabla de Mis productos)
                profit, margin = profit_for(p["sell_price"], p["cost"] + p["shipping_cost"], settings)
                rows.append(_row(p, f, profit, margin) +
                            [f.price(p["sell_price"]), STATUS_NAMES.get(p["status"], safe_text(p["status"]))])
            return _file(kind, _headers(["Precio de venta", "Estado"], f.code), rows, sep, total > EXPORT_CAP)
    # kind == "search": search_products abre sus propias conexiones (fuera del with)
    items, total = search_export_items(qs)
    return _file("search", _headers([], f.code), [_row(p, f) for p in items], sep, total > len(items))


# ---------------------------------------------------------------- rutas

def _auth(fn):
    """Todas las rutas exigen usuario antes de validar nada (sin sesión: 401, no un 400 que revele algo)."""
    def handler(m, qs, b):
        _uid()
        return fn(m, qs, b)
    return handler


ROUTES += [
    ("GET", r"/api/collections", lambda m, qs, b: list_collections()),
    ("POST", r"/api/collections", lambda m, qs, b: create_collection(b)),
    ("PATCH", r"/api/collections/(\d+)", lambda m, qs, b: update_collection(int(m[1]), b)),
    ("DELETE", r"/api/collections/(\d+)", lambda m, qs, b: delete_collection(int(m[1]))),
    ("GET", r"/api/collections/(\d+)/items", lambda m, qs, b: list_items(int(m[1]), qs)),
    ("POST", r"/api/collections/(\d+)/items", lambda m, qs, b: add_items(int(m[1]), b)),
    ("DELETE", r"/api/collections/(\d+)/items/(\d+)", lambda m, qs, b: remove_item(int(m[1]), int(m[2]))),
    ("PATCH", r"/api/collections/(\d+)/items/(\d+)", lambda m, qs, b: update_item(int(m[1]), int(m[2]), b)),
    ("POST", r"/api/favorites/(\d+)", lambda m, qs, b: toggle_favorite(int(m[1]))),
    ("GET", r"/api/favorites/ids", lambda m, qs, b: favorite_ids(qs)),
    ("GET", r"/api/product-collections/(\d+)", lambda m, qs, b: product_collections(int(m[1]))),
    ("GET", r"/api/export/(search|collection|imports)\.csv", lambda m, qs, b: export_csv(m[1], qs)),
]
ROUTES[:] = [(meth, pat, _auth(fn)) for meth, pat, fn in ROUTES]
