"""Motor de publicación y sincronización con las tiendas (ver docs/adr/0002-integracion-tiendas.md y
docs/stores-api.md).

Piezas
------
- **Cola persistente** (`store_jobs`): publish | sync | hide | show | delete. Las peticiones del usuario y las
  reglas solo ENCOLAN y responden al instante; un hilo daemon (o `run_due_jobs()` en pruebas y en
  `POST /api/stores/sync`) llama a la tienda.
- **Reglas automáticas**: un hook de `hooks.on_catalog_write` (dentro de la transacción del escritor) decide, por
  anuncio publicado/oculto de un producto real, si hay que ocultar/mostrar, empujar stock o subir el precio, y solo
  encola (nunca habla con la tienda).
- **Monitor programado**: el mismo hilo ejecuta el monitor de precios/stock de cada usuario con tiendas activas
  cada N minutos (`meta.monitor_interval_minutes`, 0 = apagado).

Máquina de estados
------------------
Anuncio (`store_listings.status`):
    pending --publish ok--> published | hidden (si se publicó como borrador)
    published <--hide/show--> hidden
    cualquiera con id externo --NotFound--> removed         (borrado a mano en la tienda)
    cualquiera --error de validación / reintentos agotados--> error
    error --reintentar--> (tras éxito) vuelve a su estado previo (`_prev` del trabajo; por defecto hidden)
    removed/error sin id externo --reintentar--> pending (nuevo publish)
Trabajo (`store_jobs.status`): pending -> running -> done | failed; running -> pending (reintento con espera
creciente, error de límite de velocidad o conexión en error). Un trabajo `running` más de LEASE_S se recupera.

Espera entre reintentos (solo errores `retryable`): 1 min, 5 min, 30 min, 2 h; el 5.º fallo deja el trabajo en
`failed`. Límite de velocidad: usa `retry_after` y no gasta intentos (tope de 20 esperas). Credenciales rechazadas:
la conexión pasa a `error` y sus trabajos NO se reclaman (no se martilla un token revocado) hasta revalidarla.

Anuncio oculto POR REGLA: solo se vuelve a mostrar solo si lo ocultó la regla de stock. Se marca con la columna
`store_listings.hidden_by_rule` (=1 cuando el trabajo `hide` con `by_rule` tiene éxito; cualquier hide/show manual
la pone en 0).
Un borrador (`hidden` con hidden_by_rule=0) nunca se muestra solo.
`meta.ss:base:<listing_id>` guarda precio y costo al publicar (tope de 2x y "el costo subió").

Riesgo residual (publicación ambigua)
-------------------------------------
El contrato no permite buscar por SKU, así que no se puede comprobar si el producto ya existe. Reglas:
(1) El id externo se guarda y el trabajo se marca `done` en UNA transacción en cuanto `publish()` responde, con
    reintentos acotados (SAVE_TRIES); por un fallo local jamás se vuelve a llamar a `publish()`.
(2) Antes de llamar a la tienda se deja la marca `_remote_started`. Si el proceso muere con ella puesta, o si no se
    pudo guardar el resultado, el anuncio pasa a `error` (aviso con el SKU `DF-<id>`): solo un conector con
    `idempotent_publish` (Shopify adopta por etiqueta) repite la llamada solo.
(3) Si `publish()` falla en un conector SIN `idempotent_publish`, solo se reintenta cuando la tienda lo rechazó
    explícitamente (`StoreRateLimited`) o la petición nunca salió (`StoreError.not_sent`). Cualquier error ambiguo
    (timeouts, 5xx, conexión cortada tras enviar, errores internos) deja el anuncio en `error` con "No sabemos si la
    tienda creó el producto…" y el usuario decide con Reintentar (la respuesta de `retry` trae un `warning`: puede
    duplicar). Credenciales/validación/no encontrado son rechazos definitivos y siguen su política normal.
Riesgo que queda: el usuario reintenta sin revisar la tienda y el producto ya existía (duplicado, avisado).
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import auth_context
import currency
import db
import hooks
import secrets_store
import store_connectors as sc
import stores_mod
from errors import ApiError

log = logging.getLogger("dropfinder.store_sync")

BACKGROUND = True                    # las pruebas lo apagan y llaman run_due_jobs() directamente
BACKOFF_S = (60, 300, 1800, 7200)    # espera tras el 1.º, 2.º, 3.º y 4.º fallo; el 5.º deja el trabajo en failed
MAX_ATTEMPTS = len(BACKOFF_S) + 1
MAX_RATE_WAITS = 20                  # esperas por límite de velocidad antes de rendirse
LEASE_S = 600                       # concesión de un trabajo `running` (locked_until); vencida = proceso caído
JOB_RETENTION_DAYS = 7
LOG_KEEP_PER_USER = 500
PUBLISH_MAX_IDS = 50
PUBLISH_PER_MINUTE = 50
STOCK_CAP = 9999
POLL_S = 15.0
SETTLE_S = 1.0
PRUNE_EVERY_S = 600
MONITOR_CHOICES = (0, 15, 30, 60, 180, 360, 720, 1440)
SUPPORTED_CURRENCIES = ("USD", "COP")
ACTIONS = ("publish", "sync", "hide", "show", "delete")

_wake = threading.Event()
_worker_lock = threading.Lock()
_worker: threading.Thread | None = None
_last_prune: float | None = None
_monitor_runner = None


def set_monitor_runner(fn) -> None:
    """server.py registra aquí `run_monitor(user_id=…)` (evita importar server desde este módulo)."""
    global _monitor_runner
    _monitor_runner = fn


# ---------------------------------------------------------------- utilidades

def _ts(dt: datetime | None = None) -> str:
    return (dt or datetime.now(timezone.utc)).isoformat(timespec="seconds")


_SECRET_RES = [
    (re.compile(r"shp[a-z]{0,3}_[A-Za-z0-9_\-]{6,}"), "***"),
    (re.compile(r"(?i)\b(bearer|token|secret|api[_ -]?key|password|clave|access[_-]?token)\b(\W{0,4})"
                r"(?=[A-Za-z0-9+/=_\-.]*\d)[A-Za-z0-9+/=_\-.]{12,}"), r"\1\2***"),
    (re.compile(r"\b[A-Fa-f0-9]{32,}\b"), "***"),
    (re.compile(r"[A-Za-z0-9+/=_\-]{40,}"), "***"),
]


def clean_message(text, secrets=(), limit: int = 300) -> str:
    """Mensaje de la tienda seguro para guardar y mostrar: sin tokens ni credenciales, ≤ 300 caracteres."""
    msg = " ".join(str(text or "").split())
    for s in secrets:
        if isinstance(s, str) and len(s) >= 6:
            msg = msg.replace(s, "***")
    for rx, rep in _SECRET_RES:
        msg = rx.sub(rep, msg)
    return sc.scrub(msg, secrets, limit)


def _log_exc(what: str, exc: BaseException, secrets=()) -> None:
    """Registra un fallo SIN traza ni mensaje crudo (pueden traer texto de la tienda con credenciales)."""
    log.error("%s: %s: %s", what, type(exc).__name__, clean_message(exc, secrets))


def _uid() -> int:
    try:
        return auth_context.current_user_id()
    except auth_context.NoUserContext:
        raise ApiError(401, "Inicia sesión") from None


def _log(conn, user_id, connection_id, listing_id, action: str, ok: bool, message: str) -> None:
    conn.execute("INSERT INTO store_sync_log(user_id, connection_id, listing_id, action, ok, message, created_at) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (user_id, connection_id, listing_id, action, 1 if ok else 0, clean_message(message), _ts()))


def _cap_stock(n) -> int:
    try:
        return max(0, min(STOCK_CAP, int(n)))
    except (TypeError, ValueError):
        return 0


def _payload(row) -> dict:
    try:
        d = json.loads(row["payload"] or "{}")
        return d if isinstance(d, dict) else {}
    except (ValueError, TypeError):
        return {}


# ---------------------------------------------------------------- precios

def _mode(state: dict, code: str) -> str:
    setting = currency.CURRENCIES[code]["rounding_setting"]
    return state["settings"].get(setting, "none") if setting else "none"


def store_price(usd: float, state: dict, code: str) -> float | None:
    """Precio de venta en USD → moneda de la tienda con el redondeo comercial del usuario. None si no hay tasa o
    la moneda no está soportada."""
    if code not in SUPPORTED_CURRENCIES:
        return None
    rate = state["rates"].get(code)
    if rate is None:
        return None
    return currency.round_price(float(usd) * rate, code, _mode(state, code))


def _round_up(value: float, state: dict, code: str) -> float:
    """Redondeo hacia arriba (para que el margen mínimo se cumpla): comercial si el usuario lo eligió."""
    mode = _mode(state, code)
    if mode in currency.ROUNDING_STEPS and mode in currency.CURRENCIES[code]["rounding_modes"]:
        return currency.round_price(value, code, mode)
    dec = currency.CURRENCIES[code]["decimals"]
    return math.ceil(round(value * 10 ** dec, 6)) / 10 ** dec


def _floor_money(value: float, code: str) -> float:
    dec = currency.CURRENCIES[code]["decimals"]
    return math.floor(round(value * 10 ** dec, 6)) / 10 ** dec


def _margin_pct(price: float, cost_usd: float, rate: float, s: dict) -> float:
    """Margen % al precio dado (moneda de la tienda). Comisiones como en metrics.profit_for."""
    fee = price * s["fee_pct"] / 100 + s["fee_fixed"] * rate
    return (price - cost_usd * rate - fee) / price * 100 if price > 0 else 0.0


# ---------------------------------------------------------------- cola

def _enqueue(conn, user_id: int, listing_id: int, action: str, payload: dict | None = None,
             run_after: str | None = None) -> int:
    """Encola de forma idempotente: el índice único (listing_id, action) para pending/running hace que un doble clic
    no cree dos trabajos (INSERT OR IGNORE). Devuelve el id nuevo o 0 si ya había uno activo."""
    ts = _ts()
    cur = conn.execute("INSERT OR IGNORE INTO store_jobs(user_id, listing_id, action, status, attempts, run_after, "
                       "payload, created_at, updated_at) VALUES (?, ?, ?, 'pending', 0, ?, ?, ?, ?)",
                       (user_id, listing_id, action, run_after or ts, json.dumps(payload or {}), ts, ts))
    return cur.lastrowid if cur.rowcount else 0


def _pending(conn, listing_id: int, action: str | None = None):
    sql, args = "SELECT * FROM store_jobs WHERE listing_id = ? AND status = 'pending'", [listing_id]
    if action:
        sql += " AND action = ?"
        args.append(action)
    return conn.execute(sql + " ORDER BY id", args).fetchall()


def _enqueue_sync(conn, user_id: int, listing_id: int, new: dict) -> bool:
    """Un solo `sync` pendiente por anuncio: si ya hay uno, se fusiona (stock: el último; precio: el mayor,
    nunca baja; `manual` es pegajoso). Devuelve True si algo cambió."""
    for old in _pending(conn, listing_id, "sync")[:1]:
        merged = _payload(old)
        before = dict(merged)
        if new.get("manual"):
            merged["manual"] = True
        if new.get("stock") is not None:
            merged["stock"] = new["stock"]
        if new.get("price") is not None:
            merged["price"] = max(new["price"], merged.get("price") or 0)
        if new.get("reason"):
            merged["reason"] = new["reason"]
        if merged == before:
            return False
        if conn.execute("UPDATE store_jobs SET payload = ?, updated_at = ? WHERE id = ? AND status = 'pending'",
                        (json.dumps(merged), _ts(), old["id"])).rowcount:
            return True
    # Si hay un sync EN CURSO (el índice único impide un segundo), este cambio se recogerá en el siguiente cambio
    # del catálogo o con una sincronización manual.
    return bool(_enqueue(conn, user_id, listing_id, "sync", new))


def _enqueue_visibility(conn, user_id: int, listing_id: int, action: str, by_rule: bool) -> None:
    """hide/show: quita el pendiente contrario; si ya hay uno igual no apila (lo manual gana a la regla)."""
    other = "show" if action == "hide" else "hide"
    conn.execute("DELETE FROM store_jobs WHERE listing_id = ? AND status = 'pending' AND action = ?",
                 (listing_id, other))
    same = _pending(conn, listing_id, action)
    if same:
        if not by_rule:
            conn.execute("UPDATE store_jobs SET payload = ? WHERE id = ? AND status = 'pending'",
                         (json.dumps({"by_rule": False, "manual": True}), same[0]["id"]))
        return
    _enqueue(conn, user_id, listing_id, action, {"by_rule": by_rule, "manual": not by_rule})


def _kick() -> None:
    if BACKGROUND:
        ensure_worker()
        _wake.set()


# ---------------------------------------------------------------- publicar

def _description(title: str, category: str) -> str:
    """Descripción corta, texto plano y factual (sin proveedor ni costo). Deja espacio a una descripción con IA."""
    parts = [title.strip()]
    if category and category.strip():
        parts.append(f"Categoría: {category.strip()}.")
    return re.sub(r"[<>]", "", "\n\n".join(parts))[:1000]


def _https_image(url) -> str | None:
    if isinstance(url, str):
        u = url.strip()
        if u.lower().startswith("https://") and len(u) <= 2000 and not re.search(r"\s", u):
            return u
    return None


def _snapshot(crow, imp, state: dict, as_draft: bool) -> tuple[dict | None, str, str]:
    """(payload del trabajo publish, razón de omisión, mensaje). payload None si no se puede publicar."""
    code = (crow["currency"] or "").upper()
    if code not in SUPPORTED_CURRENCIES:
        return None, "currency_unsupported", f"Moneda de la tienda no soportada: {code or '?'} (solo USD y COP)"
    price = store_price(imp["sell_price"], state, code)
    if price is None or price <= 0:
        return None, "currency_unsupported", "No hay tasa de cambio disponible para convertir el precio"
    image = _https_image(imp["image"])
    prod = {"title": imp["title"][:200], "price": price, "sku": f"DF-{imp['pid']}", "stock": _cap_stock(imp["stock"]),
            "description": _description(imp["title"], imp["category"]), "image_urls": [image] if image else [],
            "vendor": (crow["name"] or "")[:80], "product_type": (imp["category"] or "")[:100],
            "tags": ["dropfinder"], "active": not as_draft}
    return {"product": prod, "cost_usd": round(imp["cost"] + imp["shipping_cost"], 4), "currency": code}, "", ""


_IMP_SQL = ("SELECT i.id, i.title, i.sell_price, p.id AS pid, p.category, p.stock, p.image, p.source, p.cost, "
            "p.shipping_cost FROM imported_products i JOIN products p ON p.id = i.product_id")


MAX_ID = 2 ** 63 - 1                 # SQLite no admite enteros mayores: sin esto sería un 500 (OverflowError)


def _chk_id(n: int) -> int:
    if not (0 < n <= MAX_ID):
        raise ApiError(404, "No encontrado")
    return n


def _ids_from(body: dict, key: str = "imported_ids") -> list[int]:
    raw = body.get(key) if isinstance(body, dict) else None
    if not isinstance(raw, list) or not raw:
        raise ApiError(400, "Indica los productos a publicar (imported_ids)")
    if len(raw) > PUBLISH_MAX_IDS:
        raise ApiError(400, f"Máximo {PUBLISH_MAX_IDS} productos por solicitud")
    ids: list[int] = []
    for v in raw:
        if isinstance(v, bool) or not isinstance(v, int) or not (0 < v <= MAX_ID):
            raise ApiError(400, "imported_ids debe ser una lista de números válidos")
        if v not in ids:
            ids.append(v)
    return ids


def publish(cid: int, body: dict) -> dict:
    uid = _uid()
    _chk_id(cid)
    if not isinstance(body, dict):
        raise ApiError(400, "Cuerpo inválido")
    ids = _ids_from(body)
    as_draft_req, allow_demo = body.get("as_draft"), body.get("allow_demo", False)
    if as_draft_req is not None and not isinstance(as_draft_req, bool):
        raise ApiError(400, "as_draft debe ser verdadero o falso")
    if not isinstance(allow_demo, bool):
        raise ApiError(400, "allow_demo debe ser verdadero o falso")
    with db.connect() as conn:
        crow = stores_mod.get_connection(conn, uid, cid)
        if not crow:
            raise ApiError(404, "Tienda no encontrada")
        as_draft = stores_mod.load_settings(crow)["publish_as_draft"] if as_draft_req is None else as_draft_req
        state = currency.get_state(conn, user_id=uid)
        imps = {r["id"]: r for r in conn.execute(
            _IMP_SQL + f" WHERE i.user_id = ? AND i.id IN ({','.join('?' * len(ids))})", [uid, *ids])}
        existing = {r["imported_id"] for r in conn.execute(
            f"SELECT imported_id FROM store_listings WHERE connection_id = ? AND user_id = ? "
            f"AND imported_id IN ({','.join('?' * len(ids))})", [cid, uid, *ids])}
    skipped, todo = [], []
    for iid in ids:
        imp = imps.get(iid)
        if imp is None:
            skipped.append({"imported_id": iid, "reason": "not_found", "message": "Producto no encontrado"})
        elif crow["status"] != "active":
            skipped.append({"imported_id": iid, "reason": "store_error",
                            "message": "La tienda tiene un error de conexión; revalídala primero"})
        elif iid in existing:
            skipped.append({"imported_id": iid, "reason": "already_published",
                            "message": "Ya está publicado en esta tienda (si falló, usa Reintentar)"})
        elif imp["source"] == "mock" and not allow_demo:
            skipped.append({"imported_id": iid, "reason": "demo_not_allowed",
                            "message": "Es un producto de demostración (ficticio); confirma que quieres publicarlo"})
        else:
            payload, reason, msg = _snapshot(crow, imp, state, as_draft)
            if payload is None:
                skipped.append({"imported_id": iid, "reason": reason, "message": msg})
            else:
                todo.append((iid, imp, payload))
    queued = []
    if todo:
        with db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            # Ventana deslizante persistida por usuario (meta), en la MISMA transacción que el encolado: no depende de
            # los anuncios/trabajos (que se pueden borrar) ni tiene carrera entre peticiones paralelas.
            key, t0 = f"ss:pub:{uid}", time.time()
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            try:
                stamps = [x for x in json.loads(row[0]) if t0 - x < 60] if row else []
            except (ValueError, TypeError):
                stamps = []
            if len(stamps) + len(todo) > PUBLISH_PER_MINUTE:
                raise ApiError(429, f"Demasiadas publicaciones seguidas (máximo {PUBLISH_PER_MINUTE} por minuto). "
                                    "Espera un momento.", "publish_rate_limited")
            for iid, imp, payload in todo:
                try:
                    cur = conn.execute(
                        "INSERT INTO store_listings(user_id, imported_id, product_id, is_demo, connection_id, title, status, "
                        "created_at) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
                        (uid, iid, imp["pid"], 1 if imp["source"] == "mock" else 0, cid, imp["title"][:200], _ts()))
                except sqlite3.IntegrityError:        # carrera: otra petición lo publicó entre la lectura y aquí
                    skipped.append({"imported_id": iid, "reason": "already_published",
                                    "message": "Ya está publicado en esta tienda"})
                    continue
                _enqueue(conn, uid, cur.lastrowid, "publish", payload)
                _log(conn, uid, cid, cur.lastrowid, "publish", True,
                     f"Publicación en cola: «{imp['title'][:80]}»" + (" (borrador)" if as_draft else ""))
                queued.append({"listing_id": cur.lastrowid, "imported_id": iid})
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                         (key, json.dumps(stamps + [t0] * len(queued))))
        _kick()
    return {"queued": queued, "skipped": skipped}


# ---------------------------------------------------------------- vistas

_LISTING_SQL = (
    "SELECT l.*, c.name AS store_name, c.platform AS platform, COALESCE(l.currency, c.currency) AS view_currency, "
    "(SELECT j.action FROM store_jobs j WHERE j.listing_id = l.id AND j.status IN ('pending', 'running') "
    " ORDER BY j.id LIMIT 1) AS pending_action "
    "FROM store_listings l JOIN store_connections c ON c.id = l.connection_id")


def _view(r) -> dict:
    return {"id": r["id"], "imported_id": r["imported_id"], "connection_id": r["connection_id"],
            "store_name": r["store_name"], "platform": r["platform"], "title": r["title"], "status": r["status"],
            "external_url": r["external_url"], "last_price": r["last_price"], "last_stock": r["last_stock"],
            "last_synced_at": r["last_synced_at"], "last_error": r["last_error"],
            "pending_action": r["pending_action"], "is_demo": bool(r["is_demo"]),
            "currency": r["view_currency"], "hidden_by_rule": bool(r["hidden_by_rule"]), "created_at": r["created_at"]}


def _own_listing(conn, lid: int, uid: int):
    r = conn.execute(_LISTING_SQL + " WHERE l.id = ? AND l.user_id = ?", (lid, uid)).fetchone()
    if not r:
        raise ApiError(404, "Anuncio no encontrado")
    return r


def listings_for_imports(qs: dict) -> dict:
    uid = _uid()
    raw = (qs.get("imported_ids") or [""])[0]
    try:
        ids = [int(x) for x in raw.split(",") if x.strip()]
    except ValueError:
        raise ApiError(400, "imported_ids inválido") from None
    if any(not (0 < i <= MAX_ID) for i in ids):
        raise ApiError(400, "imported_ids inválido")
    if len(ids) > 200:
        raise ApiError(400, "Máximo 200 ids")
    if not ids:
        return {"items": []}
    with db.connect() as conn:
        rows = conn.execute(_LISTING_SQL + f" WHERE l.user_id = ? AND l.imported_id IN ({','.join('?' * len(ids))}) "
                            "ORDER BY l.id", [uid, *ids]).fetchall()
        return {"items": [_view(r) for r in rows]}


def _int_qs(qs, key, default, lo, hi) -> int:
    try:
        v = int((qs.get(key) or [default])[0])
    except (TypeError, ValueError):
        raise ApiError(400, f"{key} inválido") from None
    return max(lo, min(hi, v))


def store_listings(cid: int, qs: dict) -> dict:
    uid = _uid()
    _chk_id(cid)
    status = (qs.get("status") or [""])[0]
    if status and status not in ("pending", "published", "hidden", "error", "removed"):
        raise ApiError(400, "Estado inválido")
    page, size = _int_qs(qs, "page", 1, 1, 100000), _int_qs(qs, "page_size", 25, 1, 100)
    with db.connect() as conn:
        if not stores_mod.get_connection(conn, uid, cid):
            raise ApiError(404, "Tienda no encontrada")
        where, args = "l.connection_id = ? AND l.user_id = ?", [cid, uid]
        if status:
            where += " AND l.status = ?"
            args.append(status)
        total = conn.execute(f"SELECT COUNT(*) FROM store_listings l WHERE {where}", args).fetchone()[0]
        rows = conn.execute(_LISTING_SQL + f" WHERE {where} ORDER BY l.id DESC LIMIT ? OFFSET ?",
                            args + [size, (page - 1) * size]).fetchall()
        return {"items": [_view(r) for r in rows], "total": total, "page": page, "page_size": size,
                "pages": max(1, math.ceil(total / size))}


def store_log(cid: int, qs: dict) -> dict:
    uid = _uid()
    _chk_id(cid)
    limit = _int_qs(qs, "limit", 50, 1, 200)
    with db.connect() as conn:
        if not stores_mod.get_connection(conn, uid, cid):
            raise ApiError(404, "Tienda no encontrada")
        rows = conn.execute("SELECT id, listing_id, action, ok, message, created_at FROM store_sync_log "
                            "WHERE user_id = ? AND connection_id = ? ORDER BY id DESC LIMIT ?",
                            (uid, cid, limit)).fetchall()
        return {"items": [{**dict(r), "ok": bool(r["ok"])} for r in rows]}


def queue_state() -> dict:
    uid = _uid()
    with db.connect() as conn:
        c = dict(conn.execute("SELECT status, COUNT(*) FROM store_jobs WHERE user_id = ? AND status IN "
                              "('pending', 'running') GROUP BY status", (uid,)))
        # `failed` solo cuenta lo que sigue pendiente de atender: el último trabajo del anuncio (si un reintento
        # posterior funcionó, el fallo viejo ya no cuenta)
        failed = conn.execute(
            "SELECT COUNT(*) FROM store_jobs j WHERE j.user_id = ? AND j.status = 'failed' AND NOT EXISTS "
            "(SELECT 1 FROM store_jobs n WHERE n.listing_id = j.listing_id AND n.id > j.id)", (uid,)).fetchone()[0]
    return {"pending": c.get("pending", 0), "running": c.get("running", 0), "failed": failed}


# ---------------------------------------------------------------- acciones del usuario sobre un anuncio

def _need_remote(r, what: str) -> None:
    if not r["external_id"] or r["status"] not in ("published", "hidden"):
        raise ApiError(409, f"El anuncio todavía no está publicado en la tienda: no se puede {what}", "not_published")


def listing_action(lid: int, action: str) -> dict:
    uid = _uid()
    _chk_id(lid)
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        r = _own_listing(conn, lid, uid)
        warning = None
        if action == "retry":
            warning = _retry(conn, r, uid)
        elif action == "sync":
            if not r["external_id"] or r["status"] not in ("published", "hidden", "error"):
                raise ApiError(409, "El anuncio todavía no está publicado en la tienda: no se puede sincronizar",
                               "not_published")
            if r["imported_id"] is None:
                raise ApiError(409, "El producto ya no está en Mis productos: no hay datos para sincronizar",
                               "no_product")
            _enqueue_sync(conn, uid, lid, {"manual": True, "reason": "manual"})
            _need_queued(conn, lid, "sync")
            _log(conn, uid, r["connection_id"], lid, "sync", True, "Sincronización pedida por el usuario")
        else:
            _need_remote(r, "ocultar" if action == "hide" else "mostrar")
            _enqueue_visibility(conn, uid, lid, action, by_rule=False)
            _need_queued(conn, lid, action)
            _log(conn, uid, r["connection_id"], lid, action, True,
                 "Se pidió ocultar el producto" if action == "hide" else "Se pidió mostrar el producto")
        view = _view(_own_listing(conn, lid, uid))
    _kick()
    return {"listing": view, **({"warning": warning} if warning else {})}


def _need_queued(conn, lid: int, action: str) -> None:
    """Una acción manual jamás se pierde en silencio: si ya hay una igual EN CURSO (el índice único impide otra),
    se responde 409 en vez de fingir éxito."""
    if not _pending(conn, lid, action):
        raise ApiError(409, "Ya hay una acción igual en curso para este anuncio; espera a que termine e inténtalo "
                            "de nuevo.", "action_in_progress")


def _retry(conn, r, uid: int) -> str | None:
    """Devuelve un aviso (o None) para mostrar al usuario."""
    warning = None
    lid = r["id"]
    active = {x[0] for x in conn.execute("SELECT action FROM store_jobs WHERE listing_id = ? AND status IN "
                                         "('pending', 'running')", (lid,))}
    failed = conn.execute("SELECT * FROM store_jobs WHERE listing_id = ? AND status = 'failed' "
                          "ORDER BY id DESC LIMIT 1", (lid,)).fetchone()
    if (failed is not None and failed["action"] in active) or (failed is None and active):
        raise ApiError(409, "Ya hay una acción en curso para este anuncio", "action_in_progress")
    need_publish = r["status"] == "removed" or (r["status"] == "error" and not r["external_id"]) \
        or (failed is not None and failed["action"] == "publish" and not r["external_id"])
    if need_publish:
        if r["imported_id"] is None:
            raise ApiError(409, "El producto ya no está en Mis productos: no se puede volver a publicar", "no_product")
        crow = stores_mod.get_connection(conn, uid, r["connection_id"])
        imp = conn.execute(_IMP_SQL + " WHERE i.id = ? AND i.user_id = ?", (r["imported_id"], uid)).fetchone()
        if not crow or not imp:
            raise ApiError(409, "El producto ya no está en Mis productos: no se puede volver a publicar", "no_product")
        old = _payload(failed) if failed is not None and failed["action"] == "publish" else {}
        if old.get("_remote_started"):
            warning = ("Es posible que el producto ya exista en tu tienda: si lo reintentas podría duplicarse. "
                       f"Revisa el SKU {_sku_of(old)} antes de continuar.")
        as_draft = not old.get("product", {}).get("active", not stores_mod.load_settings(crow)["publish_as_draft"])
        payload, reason, msg = _snapshot(crow, imp, currency.get_state(conn, user_id=uid), as_draft)
        if payload is None:
            raise ApiError(409, msg)
        conn.execute("UPDATE store_listings SET status = 'pending', external_id = NULL, external_variant_id = NULL, "
                     "external_inventory_id = NULL, external_url = NULL, last_error = NULL WHERE id = ?", (lid,))
        _enqueue(conn, uid, lid, "publish", payload)
        _log(conn, uid, r["connection_id"], lid, "publish", True,
             "Nueva publicación en cola (reintento del usuario)" + (f". Aviso: {warning}" if warning else ""))
    elif failed is not None:
        payload = _payload(failed)
        payload.pop("_remote_started", None)
        if failed["action"] in ("hide", "show"):
            payload["force"] = True              # el usuario reintenta a propósito: no se omite por "regla obsoleta"
        _enqueue(conn, uid, lid, failed["action"], payload)
        _log(conn, uid, r["connection_id"], lid, failed["action"], True, "Reintento pedido por el usuario")
    elif r["status"] == "error" and r["external_id"] and r["imported_id"] is not None:
        _enqueue_sync(conn, uid, lid, {"manual": True, "reason": "reintento"})
        _log(conn, uid, r["connection_id"], lid, "sync", True, "Reintento pedido por el usuario")
    else:
        raise ApiError(409, "Este anuncio no necesita reintento", "no_retry_needed")
    return warning


def delete_listing(lid: int, qs: dict) -> dict:
    uid = _uid()
    _chk_id(lid)
    remove_remote = (qs.get("remove_remote") or ["0"])[0] == "1"
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        r = _own_listing(conn, lid, uid)
        if conn.execute("SELECT 1 FROM store_jobs WHERE listing_id = ? AND status = 'running'", (lid,)).fetchone():
            raise ApiError(409, "Hay una acción en curso para este anuncio; inténtalo en unos segundos",
                           "action_in_progress")
        if remove_remote and r["external_id"] and r["status"] != "removed":
            conn.execute("DELETE FROM store_jobs WHERE listing_id = ? AND status = 'pending' AND action != 'delete'",
                         (lid,))
            if not _pending(conn, lid, "delete"):
                _enqueue(conn, uid, lid, "delete", {})
            _log(conn, uid, r["connection_id"], lid, "delete", True, "Se pidió eliminar el producto de la tienda")
            queued = True
        else:
            _log(conn, uid, r["connection_id"], lid, "delete", True,       # antes de borrar: el log queda con listing NULL
                 f"Anuncio «{r['title'][:80]}» quitado de DropFinder (el producto sigue en la tienda)"
                 if r["external_id"] and r["status"] != "removed" else f"Anuncio «{r['title'][:80]}» quitado")
            _drop_listing(conn, lid)
            queued = False
    if queued:
        _kick()
    return {"ok": True, "queued_remote_delete": queued}


def _drop_listing(conn, lid: int) -> None:
    conn.execute("DELETE FROM store_listings WHERE id = ?", (lid,))      # los trabajos caen en cascada
    conn.execute("DELETE FROM meta WHERE key IN (?, ?)", (f"ss:base:{lid}", f"ss:prev:{lid}"))


SYNC_NOW_LIMIT = 5                  # llamadas a la tienda dentro de una petición HTTP; el resto lo hace el hilo


def sync_now() -> dict:
    res = run_due_jobs(user_id=_uid(), limit=SYNC_NOW_LIMIT)
    _kick()                                  # lo que quede lo procesa el hilo de fondo
    return res


# ---------------------------------------------------------------- ejecución de trabajos

def _claim(user_id: int | None, now: datetime):
    """Reclama (pending → running) un trabajo vencido de forma atómica: BEGIN IMMEDIATE serializa a los
    trabajadores y el UPDATE exige status='pending'. No se reclaman trabajos de conexiones en error, ni uno
    mientras otro anterior del mismo anuncio siga pendiente o en curso (se conserva el orden)."""
    sql = ("SELECT j.id FROM store_jobs j JOIN store_listings l ON l.id = j.listing_id "
           "JOIN store_connections c ON c.id = l.connection_id "
           "WHERE j.status = 'pending' AND j.run_after <= ? AND c.status = 'active' "
           "AND NOT EXISTS (SELECT 1 FROM store_jobs o WHERE o.listing_id = j.listing_id AND o.id < j.id "
           "                AND o.status IN ('pending', 'running'))")
    args: list = [_ts(now)]
    if user_id is not None:
        sql += " AND j.user_id = ?"
        args.append(user_id)
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        r = conn.execute(sql + " ORDER BY j.run_after, j.id LIMIT 1", args).fetchone()
        if not r:
            return None
        n = conn.execute("UPDATE store_jobs SET status = 'running', attempts = attempts + 1, updated_at = ?, "
                         "locked_until = ? WHERE id = ? AND status = 'pending'",
                         (_ts(now), _ts(now + timedelta(seconds=LEASE_S)), r["id"])).rowcount
        return conn.execute("SELECT * FROM store_jobs WHERE id = ?", (r["id"],)).fetchone() if n else None


def recover_stuck(now: datetime | None = None) -> int:
    """Trabajos `running` con la concesión (`locked_until`) vencida (el proceso murió): vuelven a la cola, salvo un publish
    que ya había empezado a llamar a la tienda (puede existir el producto): ese pasa a `error` para que el usuario
    revise antes de reintentar. Los que ya gastaron todos los intentos quedan `failed`."""
    now = now or datetime.now(timezone.utc)
    cutoff = _ts(now)
    n = 0
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        for j in conn.execute("SELECT * FROM store_jobs WHERE status = 'running' AND (locked_until IS NULL OR locked_until < ?)",
                              (cutoff,)).fetchall():
            n += 1
            pl = _payload(j)
            lst = conn.execute("SELECT * FROM store_listings WHERE id = ?", (j["listing_id"],)).fetchone()
            plat = conn.execute("SELECT platform FROM store_connections WHERE id = ?",
                                (lst["connection_id"],)).fetchone() if lst is not None else None
            risky = j["action"] == "publish" and pl.get("_remote_started") and lst is not None \
                and not lst["external_id"] and not (plat and _idempotent(plat[0]))
            if risky:
                msg = ("Se interrumpió mientras se publicaba y no se sabe si el producto llegó a crearse. Revisa tu "
                       f"tienda (SKU DF-{pl.get('product', {}).get('sku', '')[3:] or '?'}) y, si no está, usa Reintentar.")
                conn.execute("UPDATE store_jobs SET locked_until = NULL, status = 'failed', last_error = ?, updated_at = ? WHERE id = ?",
                             (msg, _ts(now), j["id"]))
                conn.execute("UPDATE store_listings SET status = 'error', last_error = ? WHERE id = ?",
                             (msg, j["listing_id"]))
                _log(conn, j["user_id"], lst["connection_id"], j["listing_id"], "publish", False, msg)
            elif j["attempts"] >= MAX_ATTEMPTS:
                conn.execute("UPDATE store_jobs SET locked_until = NULL, status = 'failed', last_error = ?, updated_at = ? WHERE id = ?",
                             ("Se interrumpió varias veces", _ts(now), j["id"]))
            else:
                conn.execute("UPDATE store_jobs SET locked_until = NULL, status = 'pending', run_after = ?, updated_at = ?, "
                             "last_error = 'Se interrumpió; se reintenta' WHERE id = ?",
                             (_ts(now), _ts(now), j["id"]))
                if lst is not None:
                    _log(conn, j["user_id"], lst["connection_id"], j["listing_id"], j["action"], False,
                         "El trabajo se interrumpió; se reintentará")
    return n


def run_due_jobs(user_id: int | None = None, limit: int = 20, now: datetime | None = None) -> dict:
    """Ejecuta hasta `limit` trabajos vencidos (de un usuario o de todos). Devuelve {processed, failed}:
    `failed` cuenta los que no terminaron bien en esta pasada (aunque queden para reintento)."""
    global _last_prune
    now = now or datetime.now(timezone.utc)
    try:
        recover_stuck(now)
        if _last_prune is None or time.monotonic() - _last_prune > PRUNE_EVERY_S:
            _last_prune = time.monotonic()
            prune(now)
    except Exception as e:  # noqa: BLE001
        _log_exc("Error al recuperar/podar trabajos de tiendas", e)
    processed = failed = 0
    while processed < limit:
        job = _claim(user_id, now)
        if job is None:
            break
        processed += 1
        try:
            if not _run_job(job, now):
                failed += 1
        except Exception as e:  # noqa: BLE001 — un trabajo nunca debe tumbar el hilo
            _log_exc(f"Error inesperado en el trabajo {job['id']}", e)
            failed += 1
            _safe_requeue(job, now)
    return {"processed": processed, "failed": failed}


def _safe_requeue(job, now) -> None:
    try:
        with db.connect() as conn:
            conn.execute("UPDATE store_jobs SET locked_until = NULL, status = 'pending', run_after = ?, updated_at = ?, "
                         "last_error = 'Error interno' WHERE id = ? AND status = 'running'",
                         (_ts(now + timedelta(seconds=BACKOFF_S[0])), _ts(now), job["id"]))
    except Exception as e:  # noqa: BLE001
        _log_exc(f"No se pudo reencolar el trabajo {job['id']}", e)


_sleep = time.sleep


def _write_once(fn):
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        return fn(conn)


def _write(fn, tries: int = 4):
    """Escritura corta y reintentada con espera creciente (0,2 → 2 s): la base puede estar ocupada un instante.
    Nunca durante una llamada a la tienda."""
    for i in range(tries):
        try:
            return _write_once(fn)
        except sqlite3.OperationalError:
            if i == tries - 1:
                raise
            _sleep(min(2.0, 0.2 * 2 ** i))


class _Blocked(Exception):
    """Un publish que no se puede reintentar sin riesgo de duplicar el producto: el anuncio pasa a `error`."""


class _Unsaved(Exception):
    """No se pudo ni dejar constancia en la base: el trabajo queda `running` y la recuperación lo marcará."""


SAVE_TRIES = 6                       # intentos de guardar el resultado de un publish ya creado en la tienda


def _sku_of(payload: dict) -> str:
    return str((payload.get("product") or {}).get("sku") or "DF-?")


def _idempotent(platform: str) -> bool:
    try:
        return bool(getattr(sc.get_class(platform), "idempotent_publish", False))
    except sc.StoreError:
        return False


def _ambiguous(e: BaseException) -> bool:
    """¿Pudo la tienda haber creado el producto aunque publish() fallara? No si rechazó explícitamente (límite de
    velocidad, credenciales, validación, no encontrado, error no reintentable) ni si la petición nunca salió
    (`not_sent`). Sí: errores reintentables ambiguos (timeouts, 5xx, resets) y cualquier error que no sea de la tienda."""
    if not isinstance(e, sc.StoreError):
        return True
    if isinstance(e, (sc.StoreRateLimited, sc.StoreAuthError, sc.StoreValidationError, sc.StoreNotFound)):
        return False
    return bool(e.retryable) and getattr(e, "not_sent", False) is not True


def _mark_unsafe(c, job, lst, msg: str, now) -> None:
    """Terminal sin reintento automático: trabajo `failed` y anuncio `error` con el aviso."""
    c.execute("UPDATE store_jobs SET locked_until = NULL, status = 'failed', last_error = ?, updated_at = ? WHERE id = ?",
              (clean_message(msg), _ts(now), job["id"]))
    if c.execute("UPDATE store_listings SET status = 'error', last_error = ? WHERE id = ? AND external_id IS NULL",
                 (clean_message(msg), lst["id"])).rowcount:
        _log(c, job["user_id"], lst["connection_id"], lst["id"], "publish", False, msg)


def _set_prev(c, lst) -> None:
    """Guarda (de forma durable) la visibilidad que tenía el anuncio antes de pasar a `error`."""
    if lst["status"] in ("published", "hidden"):
        c.execute("INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)", (f"ss:prev:{lst['id']}", lst["status"]))


def _prev_status(lst, payload: dict) -> str | None:
    """Visibilidad previa a un `error` (meta; si falta, la anotada en el trabajo). None si no se conoce."""
    with db.connect() as conn:
        r = conn.execute("SELECT value FROM meta WHERE key = ?", (f"ss:prev:{lst['id']}",)).fetchone()
    for v in (r[0] if r else None, payload.get("_prev")):
        if v in ("published", "hidden"):
            return v
    return None


def _alert(c, lst, typ: str, msg: str) -> None:
    c.execute("INSERT INTO alerts(imported_id, product_id, user_id, type, message, created_at) VALUES (?, ?, ?, ?, ?, ?)",
              (lst["imported_id"], lst["product_id"], lst["user_id"], typ, msg, db.now()))


def _fresh_payload(job_id: int, fallback: dict) -> dict:
    try:
        with db.connect() as conn:
            r = conn.execute("SELECT payload FROM store_jobs WHERE id = ?", (job_id,)).fetchone()
        return _payload(r) if r else fallback
    except sqlite3.Error:
        return fallback


class _Skip(Exception):
    """El trabajo ya no aplica (el anuncio cambió): se marca hecho sin llamar a la tienda."""


def _ref(r) -> sc.ListingRef:
    return sc.ListingRef(external_id=r["external_id"] or "", variant_id=r["external_variant_id"] or "",
                         inventory_item_id=r["external_inventory_id"] or "", url=r["external_url"] or "")


def _run_job(job, now: datetime) -> bool:
    """Ejecuta un trabajo ya reclamado. True si terminó bien."""
    uid = job["user_id"]
    with auth_context.as_user(uid, "member"):
        with db.connect() as conn:
            lst = conn.execute("SELECT * FROM store_listings WHERE id = ? AND user_id = ?",
                               (job["listing_id"], uid)).fetchone()
            crow = stores_mod.get_connection(conn, uid, lst["connection_id"]) if lst else None
        if not lst or not crow:
            return True                          # se borró mientras tanto (cascada)
        secrets: list = []
        try:
            connector = stores_mod.connector_for(crow)
            secrets = [v for v in connector.credentials.values() if isinstance(v, str)]
            payload = _payload(job)
            handler = {"publish": _do_publish, "sync": _do_sync, "hide": _do_visibility, "show": _do_visibility,
                       "delete": _do_delete}.get(job["action"])
            if handler is None:
                raise sc.StoreValidationError(f"Acción desconocida: {job['action']}")
            handler(job, lst, crow, connector, payload, now)
            _reeval(lst["id"])
            return True
        except _Unsaved:
            return False
        except _Blocked as e:
            _write(lambda c: _mark_unsafe(c, job, lst, str(e), now))
            return False
        except _Skip as e:
            _write(lambda c: (c.execute("UPDATE store_jobs SET locked_until = NULL, status = 'done', last_error = ?, updated_at = ? "
                                        "WHERE id = ?", (str(e), _ts(now), job["id"])),
                              _log(c, uid, crow["id"], lst["id"], job["action"], True, str(e))))
            _reeval(lst["id"])
            return True
        except secrets_store.SecretError:
            _on_error(job, lst, crow, sc.StoreAuthError("No se pudieron descifrar las credenciales guardadas; "
                                                        "vuelve a ingresarlas"), secrets, now)
        except sc.StoreError as e:
            _on_error(job, lst, crow, e, secrets, now)
        except Exception as e:  # noqa: BLE001 — error nuestro o transitorio: se reintenta con espera
            _log_exc(f"Fallo inesperado en el trabajo {job['id']}", e, secrets)
            internal = sc.StoreError("Error interno al procesar el trabajo", retryable=True)
            internal.internal = True             # conserva `_remote_started`: no se sabe si la tienda creó algo
            _on_error(job, lst, crow, internal, secrets, now)
        return False


def _reeval(listing_id: int) -> None:
    try:
        _write(lambda c: reevaluate(c, listing_id))
    except Exception as e:  # noqa: BLE001 — la próxima pasada del hook lo corregirá; no se pierde el resultado del trabajo
        _log_exc(f"No se pudieron reevaluar las reglas del anuncio {listing_id}", e)


def _done(conn, job, lst, action: str, message: str, updates: dict | None = None, now=None) -> None:
    """Resultado exitoso: anuncio + trabajo + bitácora en la misma transacción."""
    if updates:
        cols = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(f"UPDATE store_listings SET {cols} WHERE id = ?", [*updates.values(), lst["id"]])
    conn.execute("UPDATE store_jobs SET locked_until = NULL, status = 'done', last_error = NULL, updated_at = ? WHERE id = ?",
                 (_ts(now), job["id"]))
    _log(conn, job["user_id"], lst["connection_id"], lst["id"], action, True, message)


def _do_publish(job, lst, crow, connector, payload, now) -> None:
    uid = job["user_id"]
    prod = payload.get("product")
    if lst["external_id"]:                       # idempotente: ya se creó (otra pasada o recuperación)
        status = lst["status"] if lst["status"] in ("published", "hidden") else (
            "published" if (prod or {}).get("active", True) else "hidden")
        _write(lambda c: _done(c, job, lst, "publish", "Ya estaba publicado; no se creó otro producto",
                               {"status": status, "last_error": None}, now))
        return
    if not isinstance(prod, dict):
        raise sc.StoreValidationError("Faltan los datos del producto a publicar")
    allowed = ("title", "price", "sku", "stock", "description", "image_urls", "vendor", "product_type", "tags",
               "active")
    sp = sc.StoreProduct(**{k: prod[k] for k in allowed if k in prod})
    sku = _sku_of(payload)
    if payload.get("_remote_started") and not getattr(connector, "idempotent_publish", False):
        # Ya se llamó a la tienda antes (caída o error interno): puede existir el producto. Solo un conector que
        # reconoce lo que creó (idempotent_publish) puede repetir la llamada sin duplicar.
        raise _Blocked(f"Se interrumpió mientras se publicaba y no se sabe si el producto llegó a crearse. Revisa tu "
                       f"tienda (SKU {sku}) y, si no está, usa Reintentar.")
    # Marca de intención ANTES de llamar a la tienda (ver riesgo residual en el docstring del módulo)
    marked = _write(lambda c: c.execute(
        "UPDATE store_jobs SET payload = ? WHERE id = ? AND status = 'running'",
        (json.dumps({**payload, "_remote_started": _ts(now)}), job["id"])).rowcount)
    if not marked:
        return                                   # lo recuperó otro trabajador
    try:
        ref = connector.publish(sp)              # ← llamada a la tienda: sin transacción abierta
    except Exception as e:  # noqa: BLE001
        if not getattr(connector, "idempotent_publish", False) and _ambiguous(e):
            # Tiempo agotado / 5xx / conexión cortada tras enviar / error interno: la tienda pudo crear el producto
            # y perderse la respuesta. Sin idempotencia NO se repite solo (duplicaría un producto en vivo).
            raise _Blocked(f"No sabemos si la tienda creó el producto: revisa tu tienda (SKU {sku}) antes de "
                           "reintentar.") from None
        raise
    # El resultado se guarda con reintentos acotados (el producto YA existe en la tienda y el id está en memoria):
    # jamás se vuelve a llamar a publish() por un fallo local.

    def save(c):
        n = c.execute(
            "UPDATE store_listings SET external_id = ?, external_variant_id = ?, external_inventory_id = ?, "
            "external_url = ?, status = ?, last_price = ?, last_stock = ?, last_synced_at = ?, last_error = NULL, "
            "currency = ? WHERE id = ? AND user_id = ?",
            (ref.external_id, ref.variant_id, ref.inventory_item_id, ref.url, "published" if sp.active else "hidden",
             sp.price, sp.stock, _ts(now), payload.get("currency") or crow["currency"], lst["id"], uid)).rowcount
        if not n:
            return False                         # el usuario quitó el anuncio durante la llamada
        c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                  (f"ss:base:{lst['id']}", json.dumps({"price": sp.price, "cost": payload.get("cost_usd")})))
        _done(c, job, lst, "publish", f"Publicado «{sp.title[:80]}» a {sp.price:g} {crow['currency']}"
              + ("" if sp.active else " (borrador)"), None, now)
        return True
    try:
        saved = _write(save, tries=SAVE_TRIES)
    except sqlite3.Error as e:
        msg = (f"Se creó el producto en la tienda pero no se pudo guardar: revisa tu tienda (SKU {sku}, "
               f"id remoto {ref.external_id}).")
        _log_exc(f"No se pudo guardar el publish del trabajo {job['id']} (id remoto {ref.external_id})", e)
        try:
            _write(lambda c: _mark_unsafe(c, job, lst, msg, now), tries=3)
        except sqlite3.Error:
            raise _Unsaved() from None
        return
    if not saved:
        try:                                     # compensación: no dejar un producto huérfano en la tienda
            connector.delete(ref)
            msg = "El anuncio se quitó durante la publicación; se eliminó el producto recién creado en la tienda"
        except sc.StoreError as e:
            msg = ("El anuncio se quitó durante la publicación y no se pudo eliminar el producto creado "
                   f"(id {ref.external_id}): {clean_message(e, list(connector.credentials.values()))}")
        _write(lambda c: _log(c, uid, crow["id"], None, "publish", False, msg))   # el anuncio ya no existe


def _stock_price_now(conn, lst, uid: int, crow) -> tuple[float, int]:
    """Sincronización manual: precio de venta e inventario actuales de Mis productos."""
    if lst["imported_id"] is None:
        raise sc.StoreValidationError("El producto ya no está en Mis productos: no hay datos para sincronizar")
    r = conn.execute(_IMP_SQL + " WHERE i.id = ? AND i.user_id = ?", (lst["imported_id"], uid)).fetchone()
    if not r:
        raise sc.StoreValidationError("El producto ya no está en Mis productos: no hay datos para sincronizar")
    price = store_price(r["sell_price"], currency.get_state(conn, user_id=uid), (crow["currency"] or "").upper())
    if price is None:
        raise sc.StoreValidationError("No hay tasa de cambio para la moneda de la tienda")
    return price, _cap_stock(r["stock"])


def _do_sync(job, lst, crow, connector, payload, now) -> None:
    uid = job["user_id"]
    if not lst["external_id"] or lst["status"] == "pending":
        raise sc.StoreValidationError("El anuncio todavía no está publicado en la tienda")
    if lst["status"] == "removed":
        raise _Skip("El producto ya no existe en la tienda; no se sincroniza")
    ccur = (crow["currency"] or "").upper()
    if payload.get("manual"):
        with db.connect() as conn:
            price, stock = _stock_price_now(conn, lst, uid, crow)
    else:
        lcur = (lst["currency"] or "").upper()
        if lcur and lcur != ccur:
            raise sc.StoreValidationError(_currency_msg(lcur, ccur))
        price = payload.get("price")
        price = lst["last_price"] if price is None else (
            max(price, lst["last_price"]) if lst["last_price"] is not None else price)    # las reglas nunca bajan
        stock = payload.get("stock")
        stock = lst["last_stock"] if stock is None else stock
        if price is None or stock is None:
            raise sc.StoreValidationError("Faltan datos de precio o stock para sincronizar")
    connector.update_price_stock(_ref(lst), price, stock)
    why = payload.get("reason") or "automática"
    updates = {"last_price": price, "last_stock": stock, "last_synced_at": _ts(now), "last_error": None,
               "currency": ccur or None}
    restore = lst["status"] == "error"
    prev = _prev_status(lst, payload) if restore else None
    if restore:
        if prev:
            updates["status"] = prev
        else:                                    # nunca se inventa una visibilidad que no se conoce
            updates["last_error"] = ("Sincronizado, pero no se conoce si el producto está visible en la tienda: "
                                     "usa Mostrar u Ocultar.")
    raised = (not payload.get("manual") and lst["last_price"] is not None and price > lst["last_price"]
              and lst["imported_id"] is not None)
    msg = f"Sincronizado ({why}): precio {price:g} {crow['currency']}, stock {stock}"
    if lst["last_price"] is not None and price > lst["last_price"]:
        msg += f" (subió de {lst['last_price']:g})"

    def save(c):
        _done(c, job, lst, "sync", msg, updates, now)
        if restore and prev:
            c.execute("DELETE FROM meta WHERE key = ?", (f"ss:prev:{lst['id']}",))
        if raised:
            # DropFinder debe mostrar el precio real de la tienda (y un sync/reintento manual no lo revierte)
            rate = currency.get_state(c, user_id=uid)["rates"].get(ccur)
            if rate:
                c.execute("UPDATE imported_products SET sell_price = ? WHERE id = ? AND user_id = ?",
                          (price / rate, lst["imported_id"], uid))
            _log(c, uid, lst["connection_id"], lst["id"], "sync", True,
                 f"Precio subido automáticamente a {price:g} {crow['currency']} y actualizado en Mis productos")
            _alert(c, lst, "store_price_raised",
                   f"Subimos el precio de «{lst['title'][:60]}» a {price:g} {crow['currency']} en «{crow['name']}» "
                   "porque subió el costo del proveedor.")
    _write(save)


def _currency_msg(old: str, new: str) -> str:
    return (f"La moneda de la tienda cambió ({old}→{new}): revisa los precios y vuelve a publicar o sincronizar "
            "manualmente.")


def _do_visibility(job, lst, crow, connector, payload, now) -> None:
    show = job["action"] == "show"
    by_rule = bool(payload.get("by_rule"))
    if not lst["external_id"] or lst["status"] == "pending":
        raise sc.StoreValidationError("El anuncio todavía no está publicado en la tienda")
    if lst["status"] == "removed":
        raise _Skip("El producto ya no existe en la tienda")
    if by_rule and not payload.get("force"):     # la regla pudo quedar obsoleta mientras esperaba
        if (show and (lst["status"] != "hidden" or not lst["hidden_by_rule"])) or \
                (not show and lst["status"] != "published"):
            raise _Skip("La regla de stock ya no aplica; no se cambió la visibilidad")
    connector.set_active(_ref(lst), show)

    def save(c):
        _done(c, job, lst, job["action"],
              ("Producto mostrado en la tienda" + (" (volvió el stock)" if by_rule else "")) if show else
              ("Producto ocultado en la tienda" + (" (sin stock en el proveedor)" if by_rule else "")),
              {"status": "published" if show else "hidden", "last_error": None,
               "hidden_by_rule": 1 if (not show and by_rule and not payload.get("manual")) else 0}, now)
        c.execute("DELETE FROM meta WHERE key = ?", (f"ss:prev:{lst['id']}",))
    _write(save)


def _do_delete(job, lst, crow, connector, payload, now) -> None:
    if lst["external_id"] and lst["status"] != "removed":
        connector.delete(_ref(lst))              # si ya no existe, el conector no lanza error

    def save(c):
        _log(c, job["user_id"], lst["connection_id"], lst["id"], "delete", True,
             f"Producto «{lst['title'][:80]}» eliminado de la tienda y de DropFinder")
        _drop_listing(c, lst["id"])
    _write(save)


def _on_error(job, lst, crow, exc: sc.StoreError, secrets: list, now: datetime) -> None:
    """Política de errores (ver docstring del módulo)."""
    uid, lid, cid = job["user_id"], lst["id"], crow["id"]
    msg = clean_message(exc, secrets)
    attempts, action = job["attempts"], job["action"]
    pl = _fresh_payload(job["id"], _payload(job))
    if isinstance(exc, sc.StoreError) and not getattr(exc, "internal", False):
        pl.pop("_remote_started", None)          # publish() informó su propio fallo: el conector sabe que no creó nada
    if lst["status"] not in ("error", "pending") and "_prev" not in pl:
        pl["_prev"] = lst["status"]

    def requeue(c, delay: float, attempts_back: int, note: str, extra=None):
        pl.update(extra or {})
        c.execute("UPDATE store_jobs SET locked_until = NULL, status = 'pending', run_after = ?, attempts = ?, last_error = ?, "
                  "payload = ?, updated_at = ? WHERE id = ?",
                  (_ts(now + timedelta(seconds=delay)), max(0, attempts - attempts_back), msg, json.dumps(pl),
                   _ts(now), job["id"]))
        c.execute("UPDATE store_listings SET last_error = ? WHERE id = ?", (clean_message(note), lid))
        _log(c, uid, cid, lid, action, False, note)

    def fail(c, listing_status: str | None, note: str):
        c.execute("UPDATE store_jobs SET locked_until = NULL, status = 'failed', last_error = ?, payload = ?, updated_at = ? WHERE id = ?",
                  (msg, json.dumps(pl), _ts(now), job["id"]))
        if action in ("hide", "show"):
            listing_status = None                # nunca se afirma una visibilidad que la tienda no confirmó
            title = lst["title"][:60]
            _alert(c, lst, "store_visibility_failed",
                   (f"No pudimos ocultar «{title}»" + (" (sin stock)" if pl.get("by_rule") else "")
                    + ": sigue visible en tu tienda. Reintenta desde Mis productos.") if action == "hide" else
                   f"No pudimos mostrar «{title}»: sigue oculto en tu tienda. Reintenta desde Mis productos.")
        if listing_status:
            _set_prev(c, lst)
            c.execute("UPDATE store_listings SET status = ?, last_error = ? WHERE id = ?",
                      (listing_status, clean_message(note), lid))
        else:
            c.execute("UPDATE store_listings SET last_error = ? WHERE id = ?", (clean_message(note), lid))
        _log(c, uid, cid, lid, action, False, note)

    if isinstance(exc, sc.StoreAuthError):
        note = f"La tienda rechazó las credenciales: {msg}. Revalida la tienda para reanudar los trabajos."

        def auth_fail(c):
            stores_mod.mark_error(c, cid, clean_message(note))
            requeue(c, 0, 1, note)               # no gasta intentos; no se reclama hasta revalidar
        _write(auth_fail)
    elif isinstance(exc, sc.StoreRateLimited):
        waits = int(pl.get("_rl", 0)) + 1
        delay = min(3600.0, max(1.0, float(exc.retry_after or 5)))
        if waits > MAX_RATE_WAITS:
            _write(lambda c: fail(c, "error" if not lst["external_id"] else None,
                                  "La tienda sigue limitando la velocidad; se canceló el trabajo"))
        else:
            _write(lambda c: requeue(c, delay, 1, f"La tienda pidió esperar; se reintenta en {delay:.0f} s", {"_rl": waits}))
    elif isinstance(exc, sc.StoreNotFound) and action != "publish":
        if action == "delete":
            _write(lambda c: (_done(c, job, lst, "delete", "El producto ya no estaba en la tienda", None, now),
                              _drop_listing(c, lid)))
            return
        note = "El producto ya no existe en la tienda (se borró a mano). Puedes volver a publicarlo con Reintentar."

        def removed(c):
            c.execute("UPDATE store_jobs SET locked_until = NULL, status = 'done', last_error = ?, updated_at = ? WHERE id = ?",
                      (note, _ts(now), job["id"]))
            c.execute("UPDATE store_listings SET status = 'removed', hidden_by_rule = 0, last_error = ? WHERE id = ?",
                      (note, lid))
            c.execute("DELETE FROM store_jobs WHERE listing_id = ? AND status = 'pending'", (lid,))
            _log(c, uid, cid, lid, action, False, note)
        _write(removed)
    elif exc.retryable and attempts < MAX_ATTEMPTS:
        delay = BACKOFF_S[min(attempts, len(BACKOFF_S)) - 1]
        _write(lambda c: requeue(c, delay, 0,
                                 f"Error al hablar con la tienda ({msg}); intento {attempts} de {MAX_ATTEMPTS}, "
                                 f"se reintenta en {delay // 60} min"))
    else:
        reason = "sin reintento" if not exc.retryable else f"tras {attempts} intentos"
        note = f"No se pudo completar la acción ({reason}): {msg}"
        _write(lambda c: fail(c, "error", note))


# ---------------------------------------------------------------- mantenimiento

def prune(now: datetime | None = None) -> dict:
    """Trabajos terminados/fallidos de más de 7 días, bitácora a 500 por usuario y marcas de anuncios borrados."""
    now = now or datetime.now(timezone.utc)
    cutoff = _ts(now - timedelta(days=JOB_RETENTION_DAYS))
    with db.connect() as conn:
        jobs = conn.execute("DELETE FROM store_jobs WHERE status IN ('done', 'failed') AND updated_at < ?",
                            (cutoff,)).rowcount
        logs = 0
        for (u,) in conn.execute("SELECT DISTINCT user_id FROM store_sync_log").fetchall():
            edge = conn.execute("SELECT id FROM store_sync_log WHERE user_id = ? ORDER BY id DESC LIMIT 1 OFFSET ?",
                                (u, LOG_KEEP_PER_USER)).fetchone()
            if edge:
                logs += conn.execute("DELETE FROM store_sync_log WHERE user_id = ? AND id <= ?",
                                     (u, edge[0])).rowcount
        marks = 0
        for prefix in ("ss:base:", "ss:prev:"):
            marks += conn.execute(
                "DELETE FROM meta WHERE key GLOB ? AND CAST(substr(key, ?) AS INTEGER) NOT IN "
                "(SELECT id FROM store_listings)", (prefix + "*", len(prefix) + 1)).rowcount
    return {"jobs": jobs, "log": logs, "marks": marks}


# ---------------------------------------------------------------- reglas automáticas (hook del catálogo)

HOOK_SQL_IDS = 2000                  # hasta aquí se filtra por p.id IN (…) en SQL (trozos de 500)

_RULE_SQL = (
    "SELECT l.id AS lid, l.user_id, l.connection_id, l.status, l.last_price, l.last_stock, l.title AS ltitle, "
    "l.hidden_by_rule, l.currency AS lcur, i.id AS imp_id, p.id AS pid, p.cost, p.shipping_cost, p.stock, c.settings AS settings, "
    "c.currency AS currency, c.name AS store_name FROM store_listings l "
    "JOIN imported_products i ON i.id = l.imported_id JOIN products p ON p.id = i.product_id "
    "JOIN store_connections c ON c.id = l.connection_id "
    "WHERE l.status IN ('published', 'hidden') AND l.is_demo = 0 AND p.source != 'mock'")


def _on_catalog_write(conn, ids) -> None:
    """Dentro de la transacción del escritor: SOLO encola (jamás llama a la tienda). Se parte de los anuncios
    (pocos, con índices por clave primaria hacia importado y producto), no de los ids del catálogo, así cargar
    20 000 productos cuesta lo mismo que 20."""
    try:
        if not conn.execute("SELECT 1 FROM store_listings LIMIT 1").fetchone():
            return
    except sqlite3.OperationalError:             # base sin las tablas de tiendas (esquema anterior a v7)
        return
    if ids is None:
        rows = conn.execute(_RULE_SQL).fetchall()
    else:
        wanted = {int(i) for i in ids}
        if len(wanted) <= HOOK_SQL_IDS:        # pocos ids: se filtra en SQL (usa el índice de la clave primaria)
            lst, rows = sorted(wanted), []
            for k in range(0, len(lst), 500):
                chunk = lst[k:k + 500]
                rows += conn.execute(_RULE_SQL + f" AND p.id IN ({','.join('?' * len(chunk))})", chunk).fetchall()
        else:                                  # cargas enormes: un solo recorrido de anuncios y filtro en Python
            rows = [r for r in conn.execute(_RULE_SQL) if r["pid"] in wanted]
    _evaluate(conn, rows)


def _evaluate(conn, rows, price: bool = True) -> None:
    """Deriva el estado deseado de cada anuncio a partir del catálogo ACTUAL y encola lo que falte."""
    if not rows:
        return
    fees = db.get_settings(conn)
    pend: dict[int, dict[str, list]] = {}
    for j in conn.execute("SELECT * FROM store_jobs WHERE status = 'pending' AND action IN ('sync', 'hide', 'show')"):
        pend.setdefault(j["listing_id"], {}).setdefault(j["action"], []).append(j)
    states: dict[int, dict] = {}
    queued = False
    for r in rows:
        lcur, ccur = (r["lcur"] or "").upper(), (r["currency"] or "").upper()
        if lcur and lcur != ccur:                # la tienda cambió de moneda tras publicar: no se empuja nada
            msg = _currency_msg(lcur, ccur)
            if conn.execute("UPDATE store_listings SET status = 'error', last_error = ? WHERE id = ? "
                            "AND status IN ('published', 'hidden')", (msg, r["lid"])).rowcount:
                conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)", (f"ss:prev:{r['lid']}", r["status"]))
                _log(conn, r["user_id"], r["connection_id"], r["lid"], "sync", False, msg)
            continue
        cfg = stores_mod.load_settings(r)
        mine = pend.get(r["lid"], {})
        queued |= _rule_visibility(conn, r, cfg, mine, bool(r["hidden_by_rule"]))
        queued |= _rule_stock_price(conn, r, cfg, mine, fees, states, price)
    if queued:
        _kick()


def reevaluate(conn, listing_id: int) -> None:
    """Tras terminar un trabajo de un anuncio: vuelve a derivar las reglas contra el catálogo actual. Cubre los
    cambios que llegaron MIENTRAS el trabajo corría (el índice único impidió encolarlos) sin banderas que
    arrastrar: si nada difiere de lo que ahora tiene la tienda, no se encola nada (no hay bucles)."""
    _evaluate(conn, conn.execute(_RULE_SQL + " AND l.id = ?", (listing_id,)).fetchall(), price=False)


def _rule_visibility(conn, r, cfg: dict, mine: dict, marked: bool) -> bool:
    stock = _cap_stock(r["stock"])
    ph, ps = mine.get("hide", []), mine.get("show", [])
    # Un pendiente de regla puede cancelarse; uno que pidió el usuario (manual / reintento) no
    by_rule = lambda jobs: bool(jobs) and all(                                           # noqa: E731
        _payload(j).get("by_rule") and not _payload(j).get("force") and not _payload(j).get("manual") for j in jobs)
    if stock == 0 and cfg["hide_when_out_of_stock"]:
        if ps and by_rule(ps):
            conn.execute("DELETE FROM store_jobs WHERE id IN (%s)" % ",".join(str(j["id"]) for j in ps))
            ps = []
        if r["status"] == "published" and not ph and not ps:
            _enqueue_visibility(conn, r["user_id"], r["lid"], "hide", True)
            return True
    elif stock > 0 and cfg["show_when_back_in_stock"]:
        if ph and by_rule(ph):
            conn.execute("DELETE FROM store_jobs WHERE id IN (%s)" % ",".join(str(j["id"]) for j in ph))
        elif r["status"] == "hidden" and marked and not ph and not ps:
            _enqueue_visibility(conn, r["user_id"], r["lid"], "show", True)
            return True
    return False


def _rule_stock_price(conn, r, cfg: dict, mine: dict, fees: dict, states: dict, price: bool = True) -> bool:
    uid, lid = r["user_id"], r["lid"]
    sync = mine.get("sync", [])
    pl = _payload(sync[0]) if sync else {}
    want: dict = {}
    stock = _cap_stock(r["stock"])
    if cfg["sync_stock"] and stock != (pl["stock"] if "stock" in pl else r["last_stock"]):
        want["stock"] = stock
    # --- precio: solo subir (nunca bajar) o avisar
    code = (r["currency"] or "").upper()
    # (la subida de precio solo se evalúa ante un cambio real del catálogo: reevaluar tras un trabajo la encadenaría)
    if price and code in SUPPORTED_CURRENCIES and r["last_price"]:
        if uid not in states:
            states[uid] = currency.get_state(conn, user_id=uid)
        rate = states[uid]["rates"].get(code)
        cost = r["cost"] + r["shipping_cost"]
        if rate:
            _price_rule(conn, r, cfg, fees, states[uid], code, rate, cost, pl, want)
    if not want:
        return False
    if sync and all(pl.get(k) == v for k, v in want.items()):
        return False
    want.setdefault("reason", "automática por cambio en el proveedor")
    return _enqueue_sync(conn, uid, lid, want)


def _price_rule(conn, r, cfg, fees, state, code, rate, cost, pl, want) -> None:
    uid, lid = r["user_id"], r["lid"]
    base = {}
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (f"ss:base:{lid}",)).fetchone()
    if row:
        try:
            base = json.loads(row[0])
        except (ValueError, TypeError):
            base = {}
    base_cost = base.get("cost")

    def remember(c_now):                         # último costo EVALUADO: solo un costo mayor a este reacciona
        base["cost"] = c_now
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (f"ss:base:{lid}", json.dumps(base)))
    if base_cost is None or cost <= base_cost + 0.005:
        if base_cost is None or cost < base_cost - 0.005:
            remember(cost)
        return                                   # el costo no subió desde la última evaluación: nada que corregir
    remember(cost)
    cur = max(r["last_price"], pl.get("price") or 0)
    margin = _margin_pct(cur, cost, rate, fees)
    minimum = cfg["min_margin_pct"]
    if margin >= minimum:
        return
    new_price = None
    if cfg["auto_raise_price"]:
        denom = 1 - fees["fee_pct"] / 100 - minimum / 100
        if denom > 0.02:
            needed = _round_up((cost + fees["fee_fixed"]) * rate / denom, state, code)
            cap = min(cur * (1 + cfg["max_price_increase_pct"] / 100), 2 * (base.get("price") or r["last_price"]))
            target = min(needed, _floor_money(cap, code))
            if target > cur:
                new_price = target
                want["price"] = new_price
                want["reason"] = "subida automática por aumento de costo"
        if new_price is not None and _margin_pct(new_price, cost, rate, fees) >= minimum:
            return                               # se restableció el margen mínimo
    unread = conn.execute("SELECT 1 FROM alerts WHERE user_id = ? AND type = 'store_margin_low' AND imported_id = ? "
                          "AND read = 0", (uid, r["imp_id"])).fetchone()
    if unread:
        return
    if new_price is not None:
        txt = (f"Subimos el precio de «{r['ltitle'][:60]}» en «{r['store_name']}» hasta el tope permitido, pero el "
               f"margen sigue bajo el mínimo ({minimum:g}%). Revisa el precio.")
    elif cfg["auto_raise_price"]:
        txt = (f"El margen de «{r['ltitle'][:60]}» en «{r['store_name']}» es {margin:.0f}% (mínimo {minimum:g}%) y "
               "el tope de subida automática no alcanza. Revisa el precio.")
    else:
        txt = (f"El costo subió y el margen de «{r['ltitle'][:60]}» en «{r['store_name']}» es {margin:.0f}% "
               f"(mínimo {minimum:g}%). Sube el precio o activa la subida automática.")
    conn.execute("INSERT INTO alerts(imported_id, product_id, user_id, type, message, created_at) "
                 "VALUES (?, ?, ?, 'store_margin_low', ?, ?)", (r["imp_id"], r["pid"], uid, txt, db.now()))


hooks.on_catalog_write(_on_catalog_write)


# ---------------------------------------------------------------- monitor programado

def _meta_get(conn, key: str, default=None):
    r = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return r[0] if r else default


def monitor_minutes(conn) -> int:
    try:
        v = int(_meta_get(conn, "monitor_interval_minutes", "0"))
    except (TypeError, ValueError):
        return 0
    return v if v in MONITOR_CHOICES else 0


def monitor_tick(now: datetime | None = None) -> list:
    """Si el monitor programado está activo y toca, lo ejecuta para cada usuario con tiendas activas. Devuelve los
    ids de usuario procesados. Nunca lanza excepciones."""
    now = now or datetime.now(timezone.utc)
    done: list = []
    try:
        with db.connect() as conn:
            minutes = monitor_minutes(conn)
            if not minutes:
                return done
            last = _meta_get(conn, "monitor_last_run")
            if last and last > _ts(now - timedelta(minutes=minutes)):
                return done
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('monitor_last_run', ?)", (_ts(now),))
            users = [r[0] for r in conn.execute("SELECT DISTINCT user_id FROM store_connections "
                                                "WHERE status = 'active' ORDER BY user_id")]
        if _monitor_runner is None:
            log.warning("Monitor programado: no hay función de monitor registrada")
            return done
        for uid in users:
            try:
                with auth_context.as_user(uid, "member"):
                    res = _monitor_runner(user_id=uid)
                done.append(uid)
                with db.connect() as conn:
                    _log(conn, uid, None, None, "monitor", True,
                         f"Monitor programado: {res.get('checked', 0)} revisados, {len(res.get('alerts', []))} alertas"
                         if isinstance(res, dict) else "Monitor programado ejecutado")
            except Exception as e:  # noqa: BLE001
                _log_exc(f"Falló el monitor programado del usuario {uid}", e)
                try:
                    with db.connect() as conn:
                        _log(conn, uid, None, None, "monitor", False, f"Falló el monitor programado: {e}")
                except Exception:  # noqa: BLE001
                    pass
    except Exception as e:  # noqa: BLE001
        _log_exc("Error en el monitor programado", e)
    return done


def get_schedule() -> dict:
    with db.connect() as conn:
        return {"minutes": monitor_minutes(conn), "allowed": list(MONITOR_CHOICES),
                "last_run": _meta_get(conn, "monitor_last_run")}


def set_schedule(body: dict) -> dict:
    m = body.get("minutes") if isinstance(body, dict) else None
    if isinstance(m, bool) or not isinstance(m, int) or m not in MONITOR_CHOICES:
        raise ApiError(400, "minutes debe ser uno de: " + ", ".join(str(x) for x in MONITOR_CHOICES))
    with db.connect() as conn:
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('monitor_interval_minutes', ?)", (str(m),))
        if m and not _meta_get(conn, "monitor_last_run"):
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('monitor_last_run', ?)", (_ts(),))
    if m:
        _kick()
    return get_schedule()


# ---------------------------------------------------------------- hilo de fondo

def _loop() -> None:
    while True:
        try:
            woke = _wake.wait(POLL_S)
            if woke:
                _wake.clear()
                time.sleep(SETTLE_S)             # deja que el escritor confirme (commit) antes de leer
            if not BACKGROUND or not os.path.exists(db.DB_PATH):
                continue
            for _ in range(50):
                if not run_due_jobs(limit=10)["processed"]:
                    break
            monitor_tick()
        except Exception as e:  # noqa: BLE001 — el hilo nunca debe morir ni tumbar el servidor
            _log_exc("Error en el hilo de sincronización de tiendas", e)
            time.sleep(5.0)


def ensure_worker() -> None:
    """Arranca el hilo (daemon: no impide que el proceso o las pruebas terminen) una sola vez."""
    global _worker
    if not BACKGROUND:
        return
    if _worker is None or not _worker.is_alive():
        with _worker_lock:
            if _worker is None or not _worker.is_alive():
                _worker = threading.Thread(target=_loop, name="store-sync", daemon=True)
                _worker.start()


def _autostart() -> None:
    """Al correr el servidor (python3 server.py) el hilo arranca solo: hay trabajos y programación persistentes."""
    main = sys.modules.get("__main__")
    if os.path.basename(getattr(main, "__file__", "") or "") == "server.py":
        t = threading.Timer(2.0, ensure_worker)
        t.daemon = True
        t.start()


_autostart()


# ---------------------------------------------------------------- rutas

ROUTES: list = [
    ("POST", r"/api/stores/(\d+)/publish", lambda m, qs, b: publish(int(m[1]), b)),
    ("GET", r"/api/store-listings", lambda m, qs, b: listings_for_imports(qs)),
    ("GET", r"/api/stores/(\d+)/listings", lambda m, qs, b: store_listings(int(m[1]), qs)),
    ("POST", r"/api/store-listings/(\d+)/(sync|hide|show|retry)", lambda m, qs, b: listing_action(int(m[1]), m[2])),
    ("DELETE", r"/api/store-listings/(\d+)", lambda m, qs, b: delete_listing(int(m[1]), qs)),
    ("GET", r"/api/stores/(\d+)/log", lambda m, qs, b: store_log(int(m[1]), qs)),
    ("POST", r"/api/stores/sync", lambda m, qs, b: sync_now()),
    ("GET", r"/api/stores/queue", lambda m, qs, b: queue_state()),
    ("GET", r"/api/stores/monitor-schedule", lambda m, qs, b: get_schedule()),
    ("PUT", r"/api/stores/monitor-schedule", lambda m, qs, b: set_schedule(b)),
]
