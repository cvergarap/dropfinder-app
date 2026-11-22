"""Búsquedas guardadas con alertas de productos nuevos que cumplen los criterios.

Módulo de funcionalidad: expone ROUTES (método, regex, handler(match, qs, body)) que server.py agrega
a sus rutas, y se registra en hooks.on_catalog_write.

Conceptos
---------
- `saved_searches.params`: parámetros de /api/products en JSON (normalizados por `normalize_params`).
- `saved_search_hits`: productos que alguna vez cumplieron la búsqueda. Al crearla se registran TODAS las
  coincidencias actuales (línea base); después, cada revisión registra los candidatos que cumplen. Un
  producto en hits no vuelve a generar alerta aunque deje de cumplir y vuelva a cumplir.
- Candidatos: los ids que el hook de catálogo informó desde la última revisión (tabla propia
  `saved_search_pending`, llenada dentro de la transacción del escritor) más los de id mayor que la marca
  de agua `saved_searches_hwm` (red de seguridad para escrituras sin hook). Solo un candidato puede ser
  "nuevo": si los productos más recientes desaparecen (reemplazo de CSV, borrado, sin stock) no entran
  productos antiguos como falsos nuevos. Las búsquedas sin aviso también registran hits (sin alertas).
- "Visto": en `meta` se guarda `saved_search_seen:<id>` = marca de tiempo de la última vez que el usuario
  abrió los resultados. Nuevo = cumple y (hit posterior a esa marca, o candidato aún no revisado que nunca
  cumplió). Las marcas (`_stamp`) tienen microsegundos y son estrictamente crecientes en el proceso.
- Tope: `new_ids` devuelve como máximo MAX_IDS (5.000) ids, los más recientes; las alertas, 5 + resumen.
- El texto se busca tal cual (como `exact=1`): sin autocorrección, para que las alertas sean predecibles.

Revisión en segundo plano
-------------------------
`server.search_products` y esta revisión abren su propia conexión, así que no se puede revisar dentro del
hook de escritura (otra conexión no ve las filas sin commit y escribir bloquearía). El hook solo deja la
marca `saved_searches_pending` en `meta` (en la misma transacción: si la escritura se revierte, la marca
también) y despierta un hilo daemon. El hilo espera a que la marca sea visible (es decir, al commit),
ejecuta `check_all()` con su propia conexión y borra la marca solo si nadie la cambió mientras tanto.
Además revisa la marca cada IDLE_POLL_S segundos por si se perdió un aviso. Nunca deja caer el servidor.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import auth_context
import db
import hooks
import search
from errors import ApiError

log = logging.getLogger("dropfinder.saved_searches")

MAX_SEARCHES = 50            # búsquedas guardadas por usuario
MAX_NAME = 80
MAX_IDS = 5_000              # productos coincidentes considerados por búsqueda (los más recientes)
MAX_ALERTS_PER_CHECK = 5     # alertas individuales por búsqueda y revisión (+ 1 de resumen)
MAX_LIST_VALUES = 50         # valores por filtro de lista (categorías, proveedores…)
MAX_NUMBER = 2 ** 53         # tope de los filtros numéricos (SQLite guarda enteros de 64 bits)

PENDING_KEY = "saved_searches_pending"
SEEN_PREFIX = "saved_search_seen:"

BACKGROUND = True            # las pruebas lo apagan para revisar de forma determinista
SETTLE_POLL_S = 0.25         # cada cuánto se mira si ya hubo commit tras un aviso
SETTLE_WINDOW_S = 120.0      # cuánto tiempo seguir mirando tras un aviso (cargas de CSV largas)
IDLE_POLL_S = 30.0           # revisión periódica de la marca pendiente

LIST_KEYS = ("source", "category", "supplier", "warehouse")
FLOAT_KEYS = ("min_cost", "max_cost", "min_margin", "min_score", "min_rating")
INT_KEYS = ("min_orders", "max_ship_days")
FLAG_KEYS = ("trending", "in_stock")
DROP_KEYS = ("page", "page_size", "exact")
SOURCE_NAMES = {"mock": "Demo", "csv": "CSV", "api": "API"}


# ---------------------------------------------------------------- utilidades

def _srv():
    """Módulo del servidor, importado tarde (server importa este módulo al cargarse).
    Si el servidor corre como script (`python3 server.py`) se usa ese mismo módulo (__main__) para no
    cargar (y ejecutar) una segunda copia de server.py."""
    mod = sys.modules.get("server")
    if mod is None:
        main = sys.modules.get("__main__")
        if main is not None and hasattr(main, "search_products") and hasattr(main, "_filters"):
            return main
        import server as mod  # noqa: PLC0415
    return mod


def _error(status: int, message: str) -> Exception:
    return ApiError(status, message)


_stamp_lock = threading.Lock()
_last_stamp = ""


def _stamp() -> str:
    """Marca de tiempo ISO con microsegundos, estrictamente creciente dentro del proceso."""
    global _last_stamp
    with _stamp_lock:
        t = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        if t <= _last_stamp:
            t = (datetime.fromisoformat(_last_stamp) + timedelta(microseconds=1)).isoformat(timespec="microseconds")
        _last_stamp = t
        return t


def _num_es(v, decimals: int = 1) -> str:
    """Número al estilo español: 1.250 · 4,5 · 45."""
    if float(v).is_integer():
        return f"{int(v):,}".replace(",", ".")
    s = f"{float(v):,.{decimals}f}".rstrip("0").rstrip(".")
    return s.replace(",", "_").replace(".", ",").replace("_", ".")


def _amount(v) -> str:
    return _num_es(v) if float(v).is_integer() else f"{float(v):.2f}".replace(".", ",")


def _usd(v) -> str:
    return "US$ " + _amount(v)


# ---------------------------------------------------------------- parámetros

def normalize_params(raw) -> dict:
    """Valida y normaliza los parámetros de búsqueda (objeto JSON o qs de parse_qs).
    Descarta page/page_size/exact y las claves desconocidas; ApiError(400) si un valor es inválido."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise _error(400, "params debe ser un objeto")
    out: dict = {}

    def scalar(v):
        if isinstance(v, list):
            v = v[0] if v else ""
        return v

    q = scalar(raw.get("q"))
    if q not in (None, ""):
        if not isinstance(q, str):
            raise _error(400, "Parámetro inválido: q")
        q = " ".join(q.split())[:200]
        if q:
            out["q"] = q
    for key in LIST_KEYS:
        v = raw.get(key)
        if v in (None, "", []):
            continue
        vals = v if isinstance(v, list) else [v]
        clean = []
        for x in vals:
            if not isinstance(x, str) or len(x) > 100:
                raise _error(400, f"Parámetro inválido: {key}")
            x = x.strip()
            if x and x not in clean:
                clean.append(x)
        if len(clean) > MAX_LIST_VALUES:
            raise _error(400, f"Demasiados valores en {key} (máx. {MAX_LIST_VALUES})")
        if clean:
            out[key] = clean
    for key, cast in [*((k, float) for k in FLOAT_KEYS), *((k, int) for k in INT_KEYS)]:
        v = scalar(raw.get(key))
        if v is None or (isinstance(v, str) and not v.strip()):
            continue
        if isinstance(v, bool):
            raise _error(400, f"Parámetro inválido: {key}")
        try:
            n = float(v)
            if n != n or n in (float("inf"), float("-inf")):
                raise ValueError
            if cast is int:
                if not n.is_integer():
                    raise ValueError
                n = int(n)
            elif n.is_integer():
                n = int(n)
        except (TypeError, ValueError, OverflowError):
            raise _error(400, f"Parámetro inválido: {key}")
        if n < 0 or n > MAX_NUMBER:
            raise _error(400, f"Parámetro fuera de rango: {key}")
        out[key] = n
    for key in FLAG_KEYS:
        v = scalar(raw.get(key))
        if v in (True, 1, "1", "true", "on"):
            out[key] = 1
    sort = scalar(raw.get("sort"))
    if isinstance(sort, str) and sort in _srv().SORTS:
        out["sort"] = sort
    return out


def to_qs(params: dict) -> dict:
    """Parámetros normalizados → formato de parse_qs ({clave: [valores]}) que usa server.search_products."""
    qs = {}
    for k, v in params.items():
        qs[k] = [str(x) for x in v] if isinstance(v, list) else [str(v)]
    return qs


def criteria_chips(params: dict) -> list[dict]:
    """Criterios en español, en el orden en que se muestran. Los costos van en USD (la API filtra en USD)."""
    chips = []

    def add(key, label):
        chips.append({"key": key, "label": label})

    if params.get("q"):
        add("q", f"«{params['q']}»")
    if params.get("category"):
        add("category", ", ".join(params["category"]))
    if params.get("supplier"):
        add("supplier", "Proveedor: " + ", ".join(params["supplier"]))
    if params.get("warehouse"):
        add("warehouse", "Almacén: " + ", ".join(params["warehouse"]))
    if params.get("source"):
        add("source", "Origen: " + ", ".join(SOURCE_NAMES.get(s, s) for s in params["source"]))
    lo, hi = params.get("min_cost"), params.get("max_cost")
    if lo is not None and hi is not None:
        add("cost", f"costo {_usd(lo)}–{_amount(hi)}")
    elif lo is not None:
        add("cost", f"costo ≥ {_usd(lo)}")
    elif hi is not None:
        add("cost", f"costo ≤ {_usd(hi)}")
    if params.get("min_margin") is not None:
        add("min_margin", f"margen ≥ {_num_es(params['min_margin'])} %")
    if params.get("min_orders") is not None:
        add("min_orders", f"pedidos ≥ {_num_es(params['min_orders'])}")
    if params.get("min_score") is not None:
        add("min_score", f"score ≥ {_num_es(params['min_score'])}")
    if params.get("min_rating") is not None:
        add("min_rating", f"rating ≥ {_num_es(params['min_rating'])}")
    if params.get("max_ship_days") is not None:
        d = params["max_ship_days"]
        add("max_ship_days", f"envío ≤ {d} día" + ("" if d == 1 else "s"))
    if params.get("trending"):
        add("trending", "en tendencia")
    if params.get("in_stock"):
        add("in_stock", "con stock")
    return chips


def summary(params: dict) -> str:
    chips = criteria_chips(params)
    return " · ".join(c["label"] for c in chips) if chips else "Todos los productos"


def _default_name(params: dict) -> str:
    """Resumen de los criterios; si no cabe en MAX_NAME se cortan criterios completos (no a mitad de palabra)."""
    s = summary(params)
    if len(s) <= MAX_NAME:
        return s
    name = ""
    for c in criteria_chips(params):
        cand = f"{name} · {c['label']}" if name else c["label"]
        if len(cand) + 2 > MAX_NAME:
            break
        name = cand
    return (name + " …") if name else s[:MAX_NAME - 1].rstrip() + "…"


# ---------------------------------------------------------------- coincidencias (misma semántica que /api/products)

# Candidatos a "nuevo": productos que el hook de catálogo informó (insertados o actualizados) desde la
# última revisión, más los de id mayor que la marca de agua (red de seguridad para escrituras sin hook).
# Tabla propia del módulo (se crea sola; no toca el esquema de db.py). Se vacía en cada check_all().
PENDING_TABLE_DDL = ("CREATE TABLE IF NOT EXISTS saved_search_pending ("
                     "product_id INTEGER PRIMARY KEY REFERENCES products(id) ON DELETE CASCADE)")
HWM_KEY = "saved_searches_hwm"      # meta: mayor products.id ya considerado por la última revisión

_CAND = "(p.id IN (SELECT product_id FROM saved_search_pending) OR p.id > ?)"
_NOT_HIT = "NOT EXISTS (SELECT 1 FROM saved_search_hits h WHERE h.saved_search_id = ? AND h.product_id = p.id)"
_HIT_AFTER = ("EXISTS (SELECT 1 FROM saved_search_hits h WHERE h.saved_search_id = ? AND h.product_id = p.id "
              "AND h.first_seen_at > ?)")


def _ensure_schema(conn) -> None:
    conn.execute(PENDING_TABLE_DDL)


def _max_id(conn) -> int:
    return conn.execute("SELECT COALESCE(MAX(id), 0) FROM products").fetchone()[0]


def _hwm(conn) -> int:
    r = conn.execute("SELECT value FROM meta WHERE key = ?", (HWM_KEY,)).fetchone()
    return int(r[0]) if r else _max_id(conn)


def _match_sql(conn, params: dict, extra=(), extra_args=()) -> tuple[str, list]:
    """FROM … WHERE … de los productos que cumplen los criterios (+ condiciones extra), sin orden ni límite."""
    srv = _srv()
    where, args = srv._filters(to_qs(params))
    tokens = search.query_tokens(params.get("q", ""))
    source = "products p"
    if tokens and search.fts_enabled(conn):
        source = "products_fts JOIN products p ON p.id = products_fts.rowid"
        where.insert(0, "products_fts MATCH ?")
        args.insert(0, search.build_match(tokens))
    elif tokens:     # SQLite sin FTS5: igual que server._query
        for t in tokens:
            where.append("(unaccent(p.title) LIKE ? OR unaccent(p.tags) LIKE ?)")
            args += [f"%{t}%", f"%{t}%"]
    where += list(extra)
    args += list(extra_args)
    cond = (" WHERE " + " AND ".join(where)) if where else ""
    return f"FROM {source}{cond}", args


def _count_total(conn, params: dict) -> int:
    frm, args = _match_sql(conn, params)
    return conn.execute(f"SELECT COUNT(*) {frm}", args).fetchone()[0]


def _seen_marker(conn, sid: int) -> str:
    r = conn.execute("SELECT value FROM meta WHERE key = ?", (f"{SEEN_PREFIX}{sid}",)).fetchone()
    return r[0] if r else ""


def _new_sql(conn, sid: int, params: dict) -> tuple[str, list]:
    """Nuevos para el usuario = coinciden y (se detectaron después de la última vez que abrió la búsqueda, o
    son candidatos aún no revisados que nunca coincidieron antes). Misma definición para el contador
    "N nuevos", new_ids y lo que la siguiente revisión alertaría."""
    return _match_sql(conn, params, [f"({_HIT_AFTER} OR ({_CAND} AND {_NOT_HIT}))"],
                      [sid, _seen_marker(conn, sid), _hwm(conn), sid])


def new_ids(conn, sid: int, params: dict) -> list[int]:
    """Ids nuevos (los MAX_IDS más recientes)."""
    frm, args = _new_sql(conn, sid, params)
    return [r[0] for r in conn.execute(f"SELECT p.id {frm} ORDER BY p.id DESC LIMIT {MAX_IDS}", args)]


def _count_new(conn, sid: int, params: dict) -> int:
    frm, args = _new_sql(conn, sid, params)
    return conn.execute(f"SELECT COUNT(*) {frm}", args).fetchone()[0]


def new_counts(conn, user_id: int) -> dict[int, int]:
    """API pública: {id de búsqueda guardada: productos nuevos} de las búsquedas del usuario, con la misma
    definición de "nuevo" que el contador de la pestaña. Solo lee (no crea tablas: si la tabla de
    candidatos aún no existe, no hay candidatos pendientes). En caché hasta que cambie el catálogo, una
    búsqueda guardada, sus coincidencias o sus marcas de "visto"."""
    rows = conn.execute("SELECT id, params FROM saved_searches WHERE user_id = ? ORDER BY id", (user_id,)).fetchall()
    if not rows:
        return {}
    sids = [r["id"] for r in rows]
    marks = {r[0]: r[1] for r in conn.execute(
        f"SELECT key, value FROM meta WHERE key IN ({','.join('?' * len(sids))})", [f"{SEEN_PREFIX}{i}" for i in sids])}
    pending = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'saved_search_pending'").fetchone()
    pend_sig = conn.execute("SELECT COUNT(*), MAX(product_id) FROM saved_search_pending").fetchone()[:] if pending else None
    hits_sig = conn.execute(f"SELECT COUNT(*), MAX(first_seen_at) FROM saved_search_hits "
                            f"WHERE saved_search_id IN ({','.join('?' * len(sids))})", sids).fetchone()[:]
    hwm = _hwm(conn)
    key = repr(("saved:new_counts", db.DB_PATH, user_id, tuple((r["id"], r["params"]) for r in rows),
                tuple(sorted(marks.items())), pend_sig, hits_sig, hwm))
    cand = _CAND if pending else "p.id > ?"

    def build():
        out = {}
        for r in rows:
            try:
                params = json.loads(r["params"])
            except ValueError:
                out[r["id"]] = 0
                continue
            sid = r["id"]
            frm, args = _match_sql(conn, params, [f"({_HIT_AFTER} OR ({cand} AND {_NOT_HIT}))"],
                                   [sid, marks.get(f"{SEEN_PREFIX}{sid}", ""), hwm, sid])
            out[sid] = conn.execute(f"SELECT COUNT(*) {frm}", args).fetchone()[0]
        return out
    return dict(search.cached(key, build))


def _record_hits(conn, sid: int, params: dict, candidates_only: bool = True) -> tuple[str, int]:
    """Agrega a hits las coincidencias que aún no estaban: solo candidatos (revisión, marcar visto) o todas
    (al crear la búsqueda: línea base completa, así nada que ya cumplía se alerta después al actualizarse).
    Devuelve (marca usada, cuántas)."""
    extra, extra_args = [_NOT_HIT], [sid]
    if candidates_only:
        extra.append(_CAND)
        extra_args.append(_hwm(conn))
    frm, args = _match_sql(conn, params, extra, extra_args)
    ts = _stamp()
    n = conn.execute(f"INSERT OR IGNORE INTO saved_search_hits(saved_search_id, product_id, first_seen_at) "
                     f"SELECT ?, p.id, ? {frm}", [sid, ts] + args).rowcount
    return ts, n


def _mark_seen(conn, sid: int, params: dict) -> None:
    _record_hits(conn, sid, params)       # los candidatos que ya vio no se alertan después
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (f"{SEEN_PREFIX}{sid}", _stamp()))


# ---------------------------------------------------------------- vista

def _row(conn, sid: int):
    """Búsqueda guardada del usuario de la petición; 404 si no existe o es de otro usuario."""
    r = conn.execute("SELECT * FROM saved_searches WHERE id = ? AND user_id = ?",
                     (sid, auth_context.current_user_id())).fetchone()
    if not r:
        raise _error(404, "Búsqueda guardada no encontrada")
    return r


def _view(conn, r) -> dict:
    params = json.loads(r["params"])
    return {
        "id": r["id"], "name": r["name"], "params": params, "notify": bool(r["notify"]),
        "created_at": r["created_at"], "last_checked_at": r["last_checked_at"],
        "total": _count_total(conn, params), "new": _count_new(conn, r["id"], params),
        "summary": summary(params), "chips": criteria_chips(params),
    }


def _sync_fts(conn) -> None:
    if search.fts_enabled(conn):
        _srv()._best_effort(conn, search.sync)
        conn.commit()


def _open():
    conn = db.connect()
    _ensure_schema(conn)
    return conn


# ---------------------------------------------------------------- API

def list_saved(qs=None, body=None) -> dict:
    _ensure_worker()
    with _open() as conn:
        _sync_fts(conn)
        rows = conn.execute("SELECT * FROM saved_searches WHERE user_id = ? ORDER BY created_at DESC, id DESC",
                            (auth_context.current_user_id(),)).fetchall()
        return {"items": [_view(conn, r) for r in rows], "max": MAX_SEARCHES}


def preview(qs: dict) -> dict:
    """Resumen y coincidencias de unos criterios aún no guardados (para el formulario de guardar)."""
    params = normalize_params(qs)
    with _open() as conn:
        _sync_fts(conn)
        return {"params": params, "summary": summary(params), "chips": criteria_chips(params),
                "default_name": _default_name(params), "total": _count_total(conn, params)}


def create(body: dict) -> dict:
    _ensure_worker()
    if not isinstance(body, dict):
        raise _error(400, "JSON inválido")
    params = normalize_params(body.get("params"))
    name = body.get("name")
    if name is not None and not isinstance(name, str):
        raise _error(400, "Nombre inválido")
    name = " ".join((name or "").split())
    if len(name) > MAX_NAME:
        raise _error(400, f"El nombre admite como máximo {MAX_NAME} caracteres")
    name = name or _default_name(params)
    notify = body.get("notify", True)
    if not isinstance(notify, bool):
        raise _error(400, "notify debe ser true o false")
    canon = json.dumps(params, sort_keys=True, ensure_ascii=False)
    uid = auth_context.current_user_id()
    with _open() as conn:
        _sync_fts(conn)
        # Tope y duplicados se comprueban con el bloqueo de escritura tomado: dos POST simultáneos se serializan
        conn.execute("BEGIN IMMEDIATE")
        n = conn.execute("SELECT COUNT(*) FROM saved_searches WHERE user_id = ?", (uid,)).fetchone()[0]
        if n >= MAX_SEARCHES:
            raise _error(409, f"Llegaste al máximo de {MAX_SEARCHES} búsquedas guardadas. Elimina alguna.")
        for r in conn.execute("SELECT name, params FROM saved_searches WHERE user_id = ?", (uid,)):
            if json.dumps(json.loads(r["params"]), sort_keys=True, ensure_ascii=False) == canon:
                raise _error(409, f"Ya tienes esta búsqueda guardada como «{r['name']}»")
        ts = db.now()
        sid = conn.execute("INSERT INTO saved_searches(user_id, name, params, notify, created_at, last_checked_at) "
                           "VALUES (?, ?, ?, ?, ?, ?)", (uid, name, canon, int(notify), ts, ts)).lastrowid
        _record_hits(conn, sid, params, candidates_only=False)    # lo que ya existe no genera alertas
        conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)", (HWM_KEY, str(_max_id(conn))))
        _mark_seen(conn, sid, params)
        return _view(conn, _row(conn, sid))


def update(sid: int, body: dict) -> dict:
    if not isinstance(body, dict):
        raise _error(400, "JSON inválido")
    with _open() as conn:
        _row(conn, sid)
        if "name" in body:
            name = body["name"]
            name = " ".join(name.split()) if isinstance(name, str) else ""
            if not name or len(name) > MAX_NAME:
                raise _error(400, f"El nombre debe tener entre 1 y {MAX_NAME} caracteres")
            conn.execute("UPDATE saved_searches SET name = ? WHERE id = ?", (name, sid))
        if "notify" in body:
            # Las búsquedas sin aviso también registran sus coincidencias en cada revisión (sin alertas), así
            # al reactivar el aviso no llega una avalancha de lo que entró mientras estaba apagado.
            if not isinstance(body["notify"], bool):
                raise _error(400, "notify debe ser true o false")
            conn.execute("UPDATE saved_searches SET notify = ? WHERE id = ?", (int(body["notify"]), sid))
        return _view(conn, _row(conn, sid))


def delete(sid: int) -> dict:
    with _open() as conn:
        _row(conn, sid)
        conn.execute("DELETE FROM saved_searches WHERE id = ?", (sid,))   # hits y alertas: ON DELETE CASCADE
        conn.execute("DELETE FROM meta WHERE key = ?", (f"{SEEN_PREFIX}{sid}",))
    return {"ok": True}


def results(sid: int, qs: dict) -> dict:
    """Respuesta de /api/products para los criterios guardados + new_ids (aún no vistos, máx. MAX_IDS)."""
    with _open() as conn:
        params = json.loads(_row(conn, sid)["params"])
    q = to_qs(params)
    q["exact"] = ["1"]
    for k in ("page", "page_size"):
        if qs.get(k):
            q[k] = qs[k][:1]
    data = _srv().search_products(q)
    with _open() as conn:
        data["new_ids"] = new_ids(conn, sid, params)
    data["saved_search_id"] = sid
    return data


def mark_seen(sid: int) -> dict:
    with _open() as conn:
        params = json.loads(_row(conn, sid)["params"])
        _mark_seen(conn, sid, params)
        conn.execute("UPDATE alerts SET read = 1 WHERE saved_search_id = ? AND user_id = ?",
                     (sid, auth_context.current_user_id()))
        return _view(conn, _row(conn, sid))


# ---------------------------------------------------------------- revisión

_check_lock = threading.Lock()


def _alert_message(name: str, p) -> str:
    days = p["shipping_days"]
    return (f"Nuevo producto para «{name}»: {p['title']} (margen {round(p['margin'])} %, "
            f"envío {days} día{'' if days == 1 else 's'})")


def check_all(only_user: int | None = None) -> dict:
    """Revisa los candidatos (productos informados por el hook o con id nuevo) contra todas las búsquedas:
    los que cumplen y nunca cumplieron antes se registran en hits; en las búsquedas con aviso se crean
    alertas (hasta MAX_ALERTS_PER_CHECK, las de mejor score, + una de resumen con el resto).
    Corre fuera de una petición (hilo de fondo): cada alerta lleva el user_id de su búsqueda, nunca el
    del contexto. La respuesta incluye todas, o solo las búsquedas/alertas de `only_user` si se indica.

    Toda la revisión corre con el bloqueo de escritura tomado (BEGIN IMMEDIATE): las búsquedas se leen ya
    bloqueadas, así que no pueden borrarse a mitad de la revisión."""
    created, checked = [], []
    with _check_lock, _open() as conn:
        _sync_fts(conn)
        conn.execute("BEGIN IMMEDIATE")
        top_id = _max_id(conn)
        for r in conn.execute("SELECT * FROM saved_searches ORDER BY id").fetchall():
            sid, name, owner = r["id"], r["name"], r["user_id"]
            ts, n = _record_hits(conn, sid, json.loads(r["params"]))
            if not r["notify"]:
                continue
            checked.append(owner)
            if n:
                top = conn.execute(
                    "SELECT p.id, p.title, p.margin, p.shipping_days FROM saved_search_hits h "
                    "JOIN products p ON p.id = h.product_id WHERE h.saved_search_id = ? AND h.first_seen_at = ? "
                    "ORDER BY p.score DESC, p.id DESC LIMIT ?", (sid, ts, MAX_ALERTS_PER_CHECK)).fetchall()
                now = db.now()
                for p in top:
                    msg = _alert_message(name, p)
                    conn.execute("INSERT INTO alerts(user_id, saved_search_id, product_id, type, message, created_at) "
                                 "VALUES (?, ?, ?, 'saved_search_new', ?, ?)", (owner, sid, p["id"], msg, now))
                    created.append({"user_id": owner, "saved_search_id": sid, "product_id": p["id"], "title": p["title"],
                                    "type": "saved_search_new", "message": msg})
                rest = n - len(top)
                if rest > 0:
                    msg = f"…y {_num_es(rest)} producto{'' if rest == 1 else 's'} nuevo{'' if rest == 1 else 's'} más para «{name}»"
                    conn.execute("INSERT INTO alerts(user_id, saved_search_id, type, message, created_at) "
                                 "VALUES (?, ?, 'saved_search_new', ?, ?)", (owner, sid, msg, now))
                    created.append({"user_id": owner, "saved_search_id": sid, "product_id": None, "title": name,
                                    "type": "saved_search_new", "message": msg})
            conn.execute("UPDATE saved_searches SET last_checked_at = ? WHERE id = ?", (db.now(), sid))
        # Candidatos consumidos (con el bloqueo tomado ningún escritor pudo agregar otros entretanto)
        conn.execute("DELETE FROM saved_search_pending")
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (HWM_KEY, str(top_id)))
    if only_user is not None:
        checked = [u for u in checked if u == only_user]
        created = [a for a in created if a["user_id"] == only_user]
    return {"checked": len(checked), "alerts": [{k: v for k, v in a.items() if k != "user_id"} for a in created]}


def check_now() -> dict:
    """Revisión pedida por un usuario: revisa todo (es la misma revisión del hilo de fondo) pero solo
    informa sus búsquedas y sus alertas."""
    _ensure_worker()
    return check_all(only_user=auth_context.current_user_id())


# ---------------------------------------------------------------- hook + hilo en segundo plano

_wake = threading.Event()
_worker_lock = threading.Lock()
_worker: threading.Thread | None = None


def _on_catalog_write(conn, ids) -> None:
    """Dentro de la transacción del escritor: guarda los ids informados como candidatos y deja la marca de
    pendiente (no busca: otra conexión no vería estas filas). Nunca lanza excepciones (haría revertir la
    carga del catálogo). ids=None (no se sabe cuáles) → solo cuenta la marca de agua de ids."""
    if ids is not None and not ids:
        return
    try:
        if not conn.execute("SELECT 1 FROM saved_searches LIMIT 1").fetchone():
            return
        _ensure_schema(conn)
        if ids:
            # Solo ids que aún existen (una operación puede informar ids que luego borró, p. ej. reemplazo de CSV)
            conn.executemany("INSERT OR IGNORE INTO saved_search_pending(product_id) "
                             "SELECT id FROM products WHERE id = ?", ((int(i),) for i in ids))
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (PENDING_KEY, _stamp()))
    except Exception:  # noqa: BLE001
        log.exception("No se pudo marcar la revisión de búsquedas guardadas")
        return
    if BACKGROUND:
        _ensure_worker()
        _wake.set()


hooks.on_catalog_write(_on_catalog_write)


def _pending_token():
    if not os.path.exists(db.DB_PATH):      # no crear bases vacías (p. ej. pruebas que ya borraron la suya)
        return None
    conn = db.connect()
    try:
        r = conn.execute("SELECT value FROM meta WHERE key = ?", (PENDING_KEY,)).fetchone()
        return r[0] if r else None
    finally:
        conn.close()


def run_pending() -> bool:
    """Si hay una revisión pendiente ya confirmada (commit), la ejecuta. Devuelve True si revisó."""
    token = _pending_token()
    if not token:
        return False
    check_all()
    with db.connect() as conn:      # se borra solo si ninguna escritura nueva cambió la marca entretanto
        conn.execute("DELETE FROM meta WHERE key = ? AND value = ?", (PENDING_KEY, token))
    return True


def _loop() -> None:
    settle_until = 0.0
    while True:
        try:
            busy = time.monotonic() < settle_until
            if _wake.wait(SETTLE_POLL_S if busy else IDLE_POLL_S):
                _wake.clear()
                settle_until = time.monotonic() + SETTLE_WINDOW_S
            if BACKGROUND and run_pending():
                settle_until = 0.0
        except Exception:  # noqa: BLE001 — el hilo nunca debe morir ni tumbar el servidor
            log.exception("Error al revisar búsquedas guardadas")
            time.sleep(1.0)


def _ensure_worker() -> None:
    """Arranca el hilo (daemon: no impide que el proceso o las pruebas terminen) una sola vez."""
    global _worker
    if not BACKGROUND or (_worker is not None and _worker.is_alive()):
        return
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_loop, name="saved-searches-check", daemon=True)
            _worker.start()


# ---------------------------------------------------------------- rutas

ROUTES: list = [
    ("GET", r"/api/saved-searches", lambda m, qs, b: list_saved()),
    ("GET", r"/api/saved-searches/preview", lambda m, qs, b: preview(qs)),
    ("POST", r"/api/saved-searches", lambda m, qs, b: create(b)),
    ("POST", r"/api/saved-searches/check", lambda m, qs, b: check_now()),
    ("PATCH", r"/api/saved-searches/(\d+)", lambda m, qs, b: update(int(m[1]), b)),
    ("DELETE", r"/api/saved-searches/(\d+)", lambda m, qs, b: delete(int(m[1]))),
    ("GET", r"/api/saved-searches/(\d+)/results", lambda m, qs, b: results(int(m[1]), qs)),
    ("POST", r"/api/saved-searches/(\d+)/seen", lambda m, qs, b: mark_seen(int(m[1]))),
]
