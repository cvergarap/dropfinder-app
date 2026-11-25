"""Proveedor CJ Dropshipping: conexión (API key cifrada), validación, importación de catálogo y apoyo al monitor.

Ver docs/adr/0004-proveedor-cj.md. El conector de red está en connectors/cj.py (SIN VERIFICAR contra la API real).

Decisiones:
- La conexión es DE LA INSTALACIÓN (una fila en `supplier_connections`), no de cada usuario: el catálogo de proveedores
  es compartido y solo el propietario lo cambia (igual que la carga de CSV). Por eso conectar, validar, desconectar,
  listar categorías e importar son solo-propietario. Cualquier usuario puede ver el ESTADO (sin ningún dato de la clave),
  y el monitor de cualquier usuario consulta precio/stock de los productos de CJ que importó con esa conexión.
- La API key se guarda cifrada con secrets_store (AAD «supplier:cj») y nunca vuelve al navegador, a un registro ni a un
  mensaje de error (todo texto de CJ pasa por scrub con la clave como secreto conocido).
- Sin versión nueva de esquema: la tabla se crea de forma perezosa e idempotente (CREATE TABLE IF NOT EXISTS, atómico).
- La importación corre en un hilo (con el límite de tasa de CJ puede tardar minutos) y se consulta por sondeo en
  GET /api/suppliers/cj. Solo hay una a la vez.
"""
from __future__ import annotations

import logging
import re
import threading
import time

import auth_context
import db
import hooks
import secrets_store
from connectors import cj
from connectors.base import PriceStock
from errors import ApiError
from store_connectors.base import StoreAuthError, StoreError, StoreRateLimited, scrub

log = logging.getLogger("dropfinder.cj")

PROVIDER = "cj"
AAD = "supplier:cj"
MAX_IMPORT = 200                  # tope por importación
DEFAULT_IMPORT = 50
MAX_PAGES = 30
IMPORT_DEADLINE = 15 * 60         # plazo total de una importación (s)
MONITOR_MAX_PRODUCTS = 40         # consultas de CJ por ejecución del monitor (2 peticiones cada una)
MONITOR_BUDGET = 120.0            # plazo total de las consultas del monitor (s)
CONNECTOR_KWARGS: dict = {}       # SOLO pruebas: base_url del fake, min_interval=0, reloj simulado…

_SCHEMA = """
CREATE TABLE IF NOT EXISTS supplier_connections (
  supplier TEXT PRIMARY KEY,
  credentials_enc TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'error')),
  last_error TEXT, connected_by INTEGER NOT NULL, created_at TEXT NOT NULL,
  last_validated_at TEXT, last_import_at TEXT
)"""
_after_import = None              # server.py registra aquí la generación de alertas de los productos tocados


def set_after_import(fn) -> None:
    global _after_import
    _after_import = fn


def ensure_schema(conn) -> None:
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'supplier_connections'").fetchone():
        conn.execute(_SCHEMA)


def _uid() -> int:
    try:
        return auth_context.current_user_id()
    except auth_context.NoUserContext:
        raise ApiError(401, "Inicia sesión") from None


def _require_owner() -> None:
    if not auth_context.is_owner():
        raise ApiError(403, "Solo el propietario puede administrar el proveedor CJ Dropshipping.")


def _row(conn):
    ensure_schema(conn)
    return conn.execute("SELECT * FROM supplier_connections WHERE supplier = ?", (PROVIDER,)).fetchone()


def _build(api_key: str) -> cj.CJConnector:
    return cj.CJConnector(api_key, **CONNECTOR_KWARGS)


def _key_of(row) -> str:
    try:
        return secrets_store.decrypt(row["credentials_enc"], AAD)
    except secrets_store.SecretError:
        raise StoreAuthError("No se pudo leer la API key guardada (la clave de cifrado cambió o se perdió). "
                             "Vuelve a conectar CJ.") from None


def _map(fn, secrets=()):
    """Errores del conector → respuestas HTTP. Nunca 401 (el navegador lo tomaría por sesión perdida); todo texto de
    CJ pasa por scrub (con la API key como secreto conocido)."""
    try:
        return fn()
    except StoreAuthError as e:
        raise ApiError(400, f"CJ rechazó la API key: {scrub(e, secrets)}", "cj_auth") from None
    except StoreRateLimited as e:
        raise ApiError(429, f"CJ pidió esperar: {scrub(e, secrets)}", "cj_rate_limited") from None
    except StoreError as e:
        raise ApiError(502, f"No se pudo hablar con CJ Dropshipping: {scrub(e, secrets)}", "cj_unreachable") from None
    except (OSError, ValueError, UnicodeError) as e:
        raise ApiError(502, f"No se pudo hablar con CJ Dropshipping: {scrub(type(e).__name__, secrets)}",
                       "cj_unreachable") from None


def _mark(conn, ok: bool, message: str = "") -> None:
    if ok:
        conn.execute("UPDATE supplier_connections SET status = 'active', last_error = NULL, last_validated_at = ? "
                     "WHERE supplier = ?", (db.now(), PROVIDER))
    else:
        conn.execute("UPDATE supplier_connections SET status = 'error', last_error = ? WHERE supplier = ?",
                     (scrub(message), PROVIDER))


def mark_error(message: str) -> None:
    try:
        with db.connect() as conn:
            ensure_schema(conn)
            _mark(conn, False, message)
    except Exception:  # noqa: BLE001 — registrar el fallo nunca debe ocultar el error original
        log.exception("No se pudo marcar la conexión de CJ con error")


# ---------------------------------------------------------------- estado de la importación (un solo trabajo)

_job_lock = threading.Lock()
_job: dict | None = None
_thread: threading.Thread | None = None


def _snapshot() -> dict | None:
    with _job_lock:
        return None if _job is None else {**_job, "warnings": list(_job["warnings"])}


def _set(**kw) -> None:
    with _job_lock:
        if _job is not None:
            _job.update(kw)


def _bump(key: str, n: int = 1) -> None:
    with _job_lock:
        if _job is not None:
            _job[key] += n


def wait_for_import(timeout: float = 30.0) -> None:
    """Pruebas: espera a que termine la importación en curso."""
    t = _thread
    if t is not None:
        t.join(timeout)


def reset_job() -> None:
    """Pruebas: olvida el último trabajo."""
    global _job, _thread
    wait_for_import()
    with _job_lock:
        _job = None
    _thread = None


# ---------------------------------------------------------------- handlers

def status() -> dict:
    _uid()
    with db.connect() as conn:
        row = _row(conn)
        n = conn.execute("SELECT COUNT(*) FROM products WHERE supplier = ? AND source = 'api'",
                         (cj.SUPPLIER,)).fetchone()[0]
        out = {"provider": cj.SUPPLIER, "connected": row is not None, "status": row["status"] if row else None,
               "last_error": row["last_error"] if row else None, "connected_at": row["created_at"] if row else None,
               "last_validated_at": row["last_validated_at"] if row else None,
               "last_import_at": row["last_import_at"] if row else None, "products": n,
               "can_manage": auth_context.is_owner(), "max_import": MAX_IMPORT, "unverified": True,
               "import": _snapshot()}
    return out


def _object(body) -> dict:
    if not isinstance(body, dict):
        raise ApiError(400, "El cuerpo de la petición debe ser un objeto JSON")
    return body


def connect(body) -> dict:
    _require_owner()
    uid = _uid()
    key = _object(body).get("api_key")
    if not isinstance(key, str) or not key.strip():
        raise ApiError(400, "Escribe la API key de CJ")
    key = key.strip()
    if not re.fullmatch(r"[\x21-\x7e]{1,200}", key):
        raise ApiError(400, "La API key tiene caracteres no permitidos o es demasiado larga (máx. 200, sin espacios)")
    _map(lambda: _build(key).validate(), [key])                      # contra CJ y SIN transacción abierta
    enc = secrets_store.encrypt(key, AAD)
    now = db.now()
    conn = db.connect()
    try:
        ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO supplier_connections(supplier, credentials_enc, status, connected_by, created_at, "
            "last_validated_at) VALUES (?, ?, 'active', ?, ?, ?) ON CONFLICT(supplier) DO UPDATE SET "
            "credentials_enc = excluded.credentials_enc, status = 'active', last_error = NULL, "
            "connected_by = excluded.connected_by, last_validated_at = excluded.last_validated_at",
            (PROVIDER, enc, uid, now, now))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return status()


def validate_connection() -> dict:
    _require_owner()
    with db.connect() as conn:
        row = _row(conn)
    if row is None:
        raise ApiError(409, "CJ no está conectado", "not_connected")
    try:
        key = _key_of(row)
    except StoreAuthError as e:
        mark_error(str(e))
        raise ApiError(409, scrub(e), "credentials_unreadable") from None
    try:
        _map(lambda: _build(key).validate(), [key])
    except ApiError as e:
        if e.code == "cj_auth":
            mark_error(str(e))
        raise
    with db.connect() as conn:
        _mark(conn, True)
    return status()


def disconnect() -> dict:
    _require_owner()
    snap = _snapshot()
    if snap and snap["state"] == "running":
        raise ApiError(409, "Hay una importación en curso. Espera a que termine.", "import_running")
    with db.connect() as conn:
        ensure_schema(conn)
        conn.execute("DELETE FROM supplier_connections WHERE supplier = ?", (PROVIDER,))
        n = conn.execute("SELECT COUNT(*) FROM products WHERE supplier = ? AND source = 'api'",
                         (cj.SUPPLIER,)).fetchone()[0]
    cj.forget_tokens()
    return {"ok": True, "products_kept": n}


def _connector_or_409() -> tuple[cj.CJConnector, str]:
    with db.connect() as conn:
        row = _row(conn)
    if row is None:
        raise ApiError(409, "Conecta CJ Dropshipping primero", "not_connected")
    try:
        key = _key_of(row)
    except StoreAuthError as e:
        mark_error(str(e))
        raise ApiError(409, scrub(e), "credentials_unreadable") from None
    return _map(lambda: _build(key), [key]), key


def categories() -> dict:
    _require_owner()
    c, key = _connector_or_409()
    try:
        return {"items": _map(c.get_categories, [key])}
    except ApiError as e:
        if e.code == "cj_auth":
            mark_error(str(e))
        raise


def _clean_import(body) -> dict:
    body = _object(body)
    query = cj.clean_text(body.get("query"), 100)
    raw_cat = body.get("category_id")
    cat = ""
    if raw_cat not in (None, ""):
        cat = cj.parse_pid(raw_cat)
        if not cat:
            raise ApiError(400, "Categoría inválida")
    if not query and not cat:
        raise ApiError(400, "Escribe una palabra clave o elige una categoría")
    limit = body.get("limit", DEFAULT_IMPORT)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_IMPORT:
        raise ApiError(400, f"El límite debe ser un número entre 1 y {MAX_IMPORT}")
    return {"query": query, "category_id": cat, "limit": limit}


def start_import(body) -> dict:
    global _job, _thread
    _require_owner()
    uid = _uid()
    params = _clean_import(body)
    c, key = _connector_or_409()
    with _job_lock:
        if _job is not None and _job["state"] == "running":
            raise ApiError(409, "Ya hay una importación en curso", "import_running")
        _job = {"id": int(time.time() * 1000), "state": "running", "phase": "search",
                "message": "Buscando productos en CJ…", **params, "fetched": 0, "total": 0, "done": 0, "new": 0,
                "updated": 0, "invalid": 0, "conflicts": 0, "stock_unknown": 0, "unknown_skipped": 0, "stock_failed": 0,
                "rate_limited": False, "alerts": 0, "error": None, "warnings": [], "started_at": db.now(),
                "finished_at": None}
        _thread = threading.Thread(target=_run_import, args=(c, key, params, uid), daemon=True, name="cj-import")
        _thread.start()
        snap = {**_job, "warnings": []}
    return {"import": snap}


def _run_import(c: cj.CJConnector, key: str, params: dict, uid: int) -> None:
    try:
        with auth_context.as_user(uid, "owner"):
            products, unknown = _gather(c, params)
            _set(phase="save", message="Guardando en el catálogo…")
            _save(products, unknown)
        _set(state="done", phase="done", message="Importación terminada", finished_at=db.now())
    except StoreAuthError as e:
        mark_error(str(e))
        _set(state="error", error=scrub(e, [key]), message="CJ rechazó la API key", finished_at=db.now())
    except StoreRateLimited as e:
        _set(state="error", error=scrub(e, [key]), message="CJ pidió esperar: reintenta en unos minutos",
             rate_limited=True, finished_at=db.now())
    except StoreError as e:
        _set(state="error", error=scrub(e, [key]), message="No se pudo hablar con CJ", finished_at=db.now())
    except Exception:  # noqa: BLE001 — el hilo no debe morir sin dejar rastro
        log.exception("Falló la importación de CJ")
        _set(state="error", error="Error interno durante la importación", message="Error interno",
             finished_at=db.now())


def _warn(msg: str) -> None:
    with _job_lock:
        if _job is not None and len(_job["warnings"]) < 10:
            _job["warnings"].append(msg)


def _gather(c: cj.CJConnector, params: dict) -> tuple[list, set]:
    deadline = time.monotonic() + IMPORT_DEADLINE
    limit = params["limit"]
    items, seen, page = [], set(), 1
    raw_seen, last = 0, None
    size = min(cj.PAGE_SIZE, limit)               # constante en todo el recorrido: las páginas de CJ se numeran con él
    while len(items) < limit and page <= MAX_PAGES:
        try:
            res = c.search(params["query"], category_id=params["category_id"], page=page,
                           page_size=size)
        except StoreRateLimited:
            if not items:
                raise
            _set(rate_limited=True)
            _warn("CJ pidió esperar durante la búsqueda: se importan solo los productos encontrados hasta ahora")
            break
        last = res
        raw_seen += res.raw_count
        _bump("invalid", res.skipped)
        for p in res.items:
            if p.supplier_sku not in seen:
                seen.add(p.supplier_sku)
                items.append(p)
        _set(fetched=min(len(items), limit))
        if not res.has_more or not res.raw_count:
            break
        if time.monotonic() > deadline:
            _warn("Se agotó el tiempo de la importación durante la búsqueda")
            break
        page += 1
    if len(items) < limit and last is not None:
        if last.has_more:
            _warn("La búsqueda se cortó antes de llegar al máximo pedido (límite de páginas o de tiempo); "
                  "puede haber más productos en CJ. Afina la palabra clave o importa de nuevo.")
        elif last.total_known and last.total > raw_seen:
            _warn(f"CJ anunció {last.total} productos pero solo devolvió {raw_seen}: puede que recorte el tamaño de "
                  "página. Vuelve a importar o afina la búsqueda.")
        elif not last.total_known and len(items) and raw_seen >= limit:
            _warn("CJ no informó el total de resultados: no se puede asegurar que se hayan traído todos.")
    items = items[:limit]
    _set(phase="stock", total=len(items), done=0, message="Consultando stock…")
    ready, unknown = [], set()
    for i, p in enumerate(items):
        if time.monotonic() > deadline:
            _bump("stock_failed", len(items) - i)
            _warn("Se agotó el tiempo de la importación: vuelve a importar para completar el resto")
            break
        try:
            st = c.get_stock(p.supplier_sku)
        except StoreRateLimited:
            _set(rate_limited=True)
            _bump("stock_failed", len(items) - i)
            _warn("CJ pidió esperar (límite de peticiones): los productos restantes no se importaron; vuelve a "
                  "importar en unos minutos")
            break
        except StoreAuthError:
            raise
        except StoreError:
            _bump("stock_failed")
            continue
        if st is None:
            _bump("stock_unknown")                  # sin dato real de stock: NO se asume 0 (ver _save)
            unknown.add(p.supplier_sku)
        else:
            p.stock, p.warehouse = st.total, st.warehouse or cj.UNKNOWN_WAREHOUSE
        ready.append(p)
        _set(done=i + 1)
    return ready, unknown


def _save(products: list, unknown: set = frozenset()) -> None:
    """Mismos caminos que el CSV: upsert por UNIQUE(supplier, sku), hooks de catálogo y alertas de los importados.
    No pisa filas del mismo proveedor y sku que cargó el usuario por CSV o la demo (source distinto de 'api')."""
    with db.connect() as conn:
        ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        taken = {r[0] for r in conn.execute("SELECT supplier_sku FROM products WHERE supplier = ? AND source != 'api'",
                                            (cj.SUPPLIER,))}
        keep = [p for p in products if p.supplier_sku not in taken]
        # Stock desconocido: un producto NUEVO no se importa (no hay un cero verdadero que guardar) y uno ya importado
        # conserva su stock y almacén anteriores (así no nace una alerta falsa de «sin stock»).
        prev = {r[0]: (r[1], r[2]) for r in conn.execute(
            "SELECT supplier_sku, stock, warehouse FROM products WHERE supplier = ? AND source = 'api'", (cj.SUPPLIER,))}
        final, unknown_skipped = [], 0
        for p in keep:
            if p.supplier_sku in unknown:
                if p.supplier_sku not in prev:
                    unknown_skipped += 1
                    continue
                p.stock, p.warehouse = prev[p.supplier_sku]
            final.append(p)
        conflicts = len(products) - len(keep)
        keep = final
        touched: list = []
        new = updated = 0
        if keep:
            new, updated = db.upsert_products(conn, keep, "api", run_hooks=False, collect_ids=touched)
            hooks.run_catalog_write(conn, touched)
        alerts = 0
        if touched and _after_import is not None:
            alerts = _after_import(conn, touched) or 0
        conn.execute("UPDATE supplier_connections SET last_import_at = ?, status = 'active', last_error = NULL "
                     "WHERE supplier = ?", (db.now(), PROVIDER))
    _set(new=new, updated=updated, conflicts=conflicts, alerts=alerts, unknown_skipped=unknown_skipped)


# ---------------------------------------------------------------- monitor de precio/stock

def prefetch_for_monitor(rows: list[dict]) -> tuple[dict, str | None]:
    """Consulta CJ (fuera de cualquier transacción) para los productos de `rows` [{product_id, supplier_sku, cost,
    shipping_cost, stock}]. Devuelve ({product_id: PriceStock}, aviso o None). Respeta el límite de tasa del conector;
    si CJ pide esperar (agotados los reintentos) o rechaza la clave, deja de consultar y avisa. Un producto cuya
    consulta falla o devuelve datos inválidos simplemente no aparece (el monitor lo cuenta como omitido)."""
    if not rows:
        return {}, None
    with db.connect() as conn:
        row = _row(conn)
    if row is None:
        return {}, "CJ no está conectado: sus productos no se revisaron"
    try:
        key = _key_of(row)
        c = _build(key)
    except StoreAuthError as e:
        mark_error(str(e))
        return {}, scrub(e)
    out: dict = {}
    note = None
    deadline = time.monotonic() + MONITOR_BUDGET
    for n, r in enumerate(rows):
        if n >= MONITOR_MAX_PRODUCTS:
            note = (f"Solo se revisaron {MONITOR_MAX_PRODUCTS} productos de CJ en esta ejecución "
                    "(límite de peticiones); el resto se revisará en la siguiente")
            break
        if time.monotonic() > deadline:
            note = "Se agotó el tiempo de revisión de CJ; el resto se revisará en la siguiente ejecución"
            break
        c.call_deadline = max(1.0, min(cj.CALL_DEADLINE, (deadline - time.monotonic()) / 2))   # acota el peor caso
        try:
            new = c.get_price_stock(r["supplier_sku"], current=PriceStock(r["cost"], r["shipping_cost"], r["stock"]))
        except StoreAuthError as e:
            mark_error(str(e))
            note = f"CJ rechazó la API key: {scrub(e, [key])}"
            break
        except StoreRateLimited:
            note = "CJ pidió esperar (límite de peticiones): se revisará en la siguiente ejecución"
            break
        except StoreError:
            note = "Algunos productos de CJ no se pudieron consultar ahora"
            continue
        except Exception:  # noqa: BLE001 — un dato hostil de un producto no debe tumbar el monitor entero
            log.exception("Error inesperado al revisar un producto de CJ")
            note = "Algunos productos de CJ no se pudieron revisar (datos inesperados)"
            continue
        if new is not None:
            out[r["product_id"]] = new
    return out, note


ROUTES: list = [
    ("GET", r"/api/suppliers/cj", lambda m, qs, b: status()),
    ("POST", r"/api/suppliers/cj", lambda m, qs, b: connect(b)),
    ("DELETE", r"/api/suppliers/cj", lambda m, qs, b: disconnect()),
    ("POST", r"/api/suppliers/cj/validate", lambda m, qs, b: validate_connection()),
    ("GET", r"/api/suppliers/cj/categories", lambda m, qs, b: categories()),
    ("POST", r"/api/suppliers/cj/import", lambda m, qs, b: start_import(b)),
]
