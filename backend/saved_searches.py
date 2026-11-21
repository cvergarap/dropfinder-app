"""Búsquedas guardadas con alertas de productos nuevos que cumplen los criterios.

Módulo de funcionalidad: expone ROUTES (método, regex, handler(match, qs, body)) que server.py agrega
a sus rutas, y se registra en hooks.on_catalog_write.

Conceptos
---------
- `saved_searches.params`: parámetros de /api/products en JSON (normalizados por `normalize_params`).
- `saved_search_hits`: productos que la búsqueda ya "conoce" (registrados al crearla, al revisarla o al
  marcarla como vista). Un producto en hits no vuelve a generar alerta aunque deje de cumplir y vuelva a cumplir.
- "Visto": en `meta` se guarda `saved_search_seen:<id>` = marca de tiempo de la última vez que el usuario
  abrió los resultados. Un producto es *nuevo* para el usuario si cumple los criterios y no tiene un hit
  anterior a esa marca. Las marcas (`_stamp`) tienen microsegundos y son estrictamente crecientes en el
  proceso, así "visto" y "detectado después" nunca empatan.
- Tope: cada búsqueda considera como máximo los MAX_IDS (5.000) productos coincidentes más recientes
  (mayor id). Así una búsqueda muy amplia no genera escrituras enormes; los productos que entran al catálogo
  tienen ids nuevos y siempre caen dentro de la ventana. Lo que queda fuera (productos antiguos que
  empiezan a cumplir en una búsqueda con más de 5.000 resultados) no genera alerta.
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

import db
import hooks
import search
from errors import ApiError

log = logging.getLogger("dropfinder.saved_searches")

MAX_SEARCHES = 50            # búsquedas guardadas por instalación
MAX_NAME = 80
MAX_IDS = 5_000              # productos coincidentes considerados por búsqueda (los más recientes)
MAX_ALERTS_PER_CHECK = 5     # alertas individuales por búsqueda y revisión (+ 1 de resumen)
MAX_LIST_VALUES = 50         # valores por filtro de lista (categorías, proveedores…)

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
        except (TypeError, ValueError):
            raise _error(400, f"Parámetro inválido: {key}")
        if n < 0:
            raise _error(400, f"Parámetro inválido: {key}")
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

def _match_sql(conn, params: dict) -> tuple[str, list]:
    """FROM … WHERE … de los productos que cumplen los criterios (sin orden ni límite)."""
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
    cond = (" WHERE " + " AND ".join(where)) if where else ""
    return f"FROM {source}{cond}", args


def _window_sql(conn, params: dict) -> tuple[str, list]:
    """Ids coincidentes más recientes (máximo MAX_IDS)."""
    frm, args = _match_sql(conn, params)
    return f"SELECT p.id AS id {frm} ORDER BY p.id DESC LIMIT {MAX_IDS}", args


def matching_ids(conn, params: dict) -> list[int]:
    sql, args = _window_sql(conn, params)
    return [r[0] for r in conn.execute(sql, args)]


def _count_total(conn, params: dict) -> int:
    frm, args = _match_sql(conn, params)
    return conn.execute(f"SELECT COUNT(*) {frm}", args).fetchone()[0]


def _seen_marker(conn, sid: int) -> str:
    r = conn.execute("SELECT value FROM meta WHERE key = ?", (f"{SEEN_PREFIX}{sid}",)).fetchone()
    return r[0] if r else ""


def _new_sql(conn, sid: int, params: dict) -> tuple[str, list]:
    """Coincidencias (dentro de la ventana) que el usuario aún no vio."""
    window, args = _window_sql(conn, params)
    sql = (f"SELECT w.id FROM ({window}) w WHERE NOT EXISTS (SELECT 1 FROM saved_search_hits h "
           "WHERE h.saved_search_id = ? AND h.product_id = w.id AND h.first_seen_at <= ?)")
    return sql, args + [sid, _seen_marker(conn, sid)]


def new_ids(conn, sid: int, params: dict) -> list[int]:
    sql, args = _new_sql(conn, sid, params)
    return [r[0] for r in conn.execute(sql, args)]


def _record_hits(conn, sid: int, params: dict) -> tuple[str, int]:
    """Agrega a hits las coincidencias actuales que aún no estaban. Devuelve (marca usada, cuántas)."""
    window, args = _window_sql(conn, params)
    ts = _stamp()
    n = conn.execute(
        f"INSERT OR IGNORE INTO saved_search_hits(saved_search_id, product_id, first_seen_at) "
        f"SELECT ?, w.id, ? FROM ({window}) w", [sid, ts] + args).rowcount
    return ts, n


def _mark_seen(conn, sid: int, params: dict) -> None:
    _record_hits(conn, sid, params)
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (f"{SEEN_PREFIX}{sid}", _stamp()))


# ---------------------------------------------------------------- vista

def _row(conn, sid: int):
    r = conn.execute("SELECT * FROM saved_searches WHERE id = ?", (sid,)).fetchone()
    if not r:
        raise _error(404, "Búsqueda guardada no encontrada")
    return r


def _view(conn, r) -> dict:
    params = json.loads(r["params"])
    sql, args = _new_sql(conn, r["id"], params)
    return {
        "id": r["id"], "name": r["name"], "params": params, "notify": bool(r["notify"]),
        "created_at": r["created_at"], "last_checked_at": r["last_checked_at"],
        "total": _count_total(conn, params),
        "new": conn.execute(f"SELECT COUNT(*) FROM ({sql})", args).fetchone()[0],
        "summary": summary(params), "chips": criteria_chips(params),
    }


def _sync_fts(conn) -> None:
    if search.fts_enabled(conn):
        _srv()._best_effort(conn, search.sync)
        conn.commit()


# ---------------------------------------------------------------- API

def list_saved(qs=None, body=None) -> dict:
    _ensure_worker()
    with db.connect() as conn:
        _sync_fts(conn)
        rows = conn.execute("SELECT * FROM saved_searches ORDER BY created_at DESC, id DESC").fetchall()
        return {"items": [_view(conn, r) for r in rows], "max": MAX_SEARCHES}


def preview(qs: dict) -> dict:
    """Resumen y coincidencias de unos criterios aún no guardados (para el formulario de guardar)."""
    params = normalize_params(qs)
    with db.connect() as conn:
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
    with db.connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM saved_searches").fetchone()[0]
        if n >= MAX_SEARCHES:
            raise _error(409, f"Llegaste al máximo de {MAX_SEARCHES} búsquedas guardadas. Elimina alguna.")
        for r in conn.execute("SELECT name, params FROM saved_searches"):
            if json.dumps(json.loads(r["params"]), sort_keys=True, ensure_ascii=False) == canon:
                raise _error(409, f"Ya tienes esta búsqueda guardada como «{r['name']}»")
        _sync_fts(conn)
        ts = db.now()
        sid = conn.execute("INSERT INTO saved_searches(name, params, notify, created_at, last_checked_at) "
                           "VALUES (?, ?, ?, ?, ?)", (name, canon, int(notify), ts, ts)).lastrowid
        _mark_seen(conn, sid, params)     # lo que ya existe no genera alertas
        return _view(conn, _row(conn, sid))


def update(sid: int, body: dict) -> dict:
    if not isinstance(body, dict):
        raise _error(400, "JSON inválido")
    with db.connect() as conn:
        r = _row(conn, sid)
        if "name" in body:
            name = body["name"]
            name = " ".join(name.split()) if isinstance(name, str) else ""
            if not name or len(name) > MAX_NAME:
                raise _error(400, f"El nombre debe tener entre 1 y {MAX_NAME} caracteres")
            conn.execute("UPDATE saved_searches SET name = ? WHERE id = ?", (name, sid))
        if "notify" in body:
            if not isinstance(body["notify"], bool):
                raise _error(400, "notify debe ser true o false")
            if body["notify"] and not r["notify"]:
                # Al reactivar no se avisa de lo que llegó mientras estaba apagada (sigue contando como nuevo)
                _record_hits(conn, sid, json.loads(r["params"]))
            conn.execute("UPDATE saved_searches SET notify = ? WHERE id = ?", (int(body["notify"]), sid))
        return _view(conn, _row(conn, sid))


def delete(sid: int) -> dict:
    with db.connect() as conn:
        _row(conn, sid)
        conn.execute("DELETE FROM saved_searches WHERE id = ?", (sid,))   # hits y alertas: ON DELETE CASCADE
        conn.execute("DELETE FROM meta WHERE key = ?", (f"{SEEN_PREFIX}{sid}",))
    return {"ok": True}


def results(sid: int, qs: dict) -> dict:
    """Respuesta de /api/products para los criterios guardados + new_ids (aún no vistos, máx. MAX_IDS)."""
    with db.connect() as conn:
        params = json.loads(_row(conn, sid)["params"])
    q = to_qs(params)
    q["exact"] = ["1"]
    for k in ("page", "page_size"):
        if qs.get(k):
            q[k] = qs[k][:1]
    data = _srv().search_products(q)
    with db.connect() as conn:
        data["new_ids"] = new_ids(conn, sid, params)
    data["saved_search_id"] = sid
    return data


def mark_seen(sid: int) -> dict:
    with db.connect() as conn:
        params = json.loads(_row(conn, sid)["params"])
        _mark_seen(conn, sid, params)
        conn.execute("UPDATE alerts SET read = 1 WHERE saved_search_id = ?", (sid,))
        return _view(conn, _row(conn, sid))


# ---------------------------------------------------------------- revisión

_check_lock = threading.Lock()


def _alert_message(name: str, p) -> str:
    days = p["shipping_days"]
    return (f"Nuevo producto para «{name}»: {p['title']} (margen {round(p['margin'])} %, "
            f"envío {days} día{'' if days == 1 else 's'})")


def check_all() -> dict:
    """Revisa todas las búsquedas con aviso activo: registra las coincidencias nuevas en hits y crea alertas
    (hasta MAX_ALERTS_PER_CHECK por búsqueda, las de mejor score, + una de resumen con el resto)."""
    created, checked = [], 0
    with _check_lock, db.connect() as conn:
        _sync_fts(conn)
        for r in conn.execute("SELECT * FROM saved_searches WHERE notify = 1 ORDER BY id").fetchall():
            sid, name = r["id"], r["name"]
            ts, n = _record_hits(conn, sid, json.loads(r["params"]))
            checked += 1
            if n:
                top = conn.execute(
                    "SELECT p.id, p.title, p.margin, p.shipping_days FROM saved_search_hits h "
                    "JOIN products p ON p.id = h.product_id WHERE h.saved_search_id = ? AND h.first_seen_at = ? "
                    "ORDER BY p.score DESC, p.id DESC LIMIT ?", (sid, ts, MAX_ALERTS_PER_CHECK)).fetchall()
                now = db.now()
                for p in top:
                    msg = _alert_message(name, p)
                    conn.execute("INSERT INTO alerts(saved_search_id, product_id, type, message, created_at) "
                                 "VALUES (?, ?, 'saved_search_new', ?, ?)", (sid, p["id"], msg, now))
                    created.append({"saved_search_id": sid, "product_id": p["id"], "title": p["title"],
                                    "type": "saved_search_new", "message": msg})
                rest = n - len(top)
                if rest > 0:
                    msg = f"…y {_num_es(rest)} producto{'' if rest == 1 else 's'} nuevo{'' if rest == 1 else 's'} más para «{name}»"
                    conn.execute("INSERT INTO alerts(saved_search_id, type, message, created_at) "
                                 "VALUES (?, 'saved_search_new', ?, ?)", (sid, msg, now))
                    created.append({"saved_search_id": sid, "product_id": None, "title": name,
                                    "type": "saved_search_new", "message": msg})
            conn.execute("UPDATE saved_searches SET last_checked_at = ? WHERE id = ?", (db.now(), sid))
    return {"checked": checked, "alerts": created}


def check_now() -> dict:
    _ensure_worker()
    return check_all()


# ---------------------------------------------------------------- hook + hilo en segundo plano

_wake = threading.Event()
_worker_lock = threading.Lock()
_worker: threading.Thread | None = None


def _on_catalog_write(conn, ids) -> None:
    """Dentro de la transacción del escritor: solo deja la marca de pendiente. Nunca lanza excepciones
    (haría revertir la carga del catálogo)."""
    if ids is not None and not ids:
        return
    try:
        if not conn.execute("SELECT 1 FROM saved_searches WHERE notify = 1 LIMIT 1").fetchone():
            return
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
