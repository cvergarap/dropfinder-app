"""Tiendas conectadas (Shopify, Tiendanube, WooCommerce…): conexión, validación y ajustes por usuario.

Publicar y sincronizar productos vive en store_sync.py (motor de trabajos). Aquí solo se gestionan las
conexiones. Las credenciales se cifran con secrets_store y NUNCA se devuelven al navegador.
Ver docs/adr/0002-integracion-tiendas.md.
"""
from __future__ import annotations

import json
import re

import auth_context
import db
import secrets_store
import store_connectors as sc
from errors import ApiError

MAX_CONNECTIONS = 10

# Ajustes de sincronización por tienda (el motor de sincronización los lee con load_settings)
DEFAULT_SETTINGS: dict = {
    "publish_as_draft": True,           # publicar como borrador: el usuario lo revisa antes de que sea visible
    "sync_stock": True,                 # mantener existencias al día
    "hide_when_out_of_stock": True,     # ocultar si el proveedor se queda sin stock
    "show_when_back_in_stock": True,    # volver a mostrar si regresa el stock
    "auto_raise_price": False,          # subir el precio si sube el costo y el margen cae bajo el mínimo
    "min_margin_pct": 20.0,
    "max_price_increase_pct": 30.0,     # tope de cada subida automática
}
_BOOLS = {"publish_as_draft", "sync_stock", "hide_when_out_of_stock", "show_when_back_in_stock", "auto_raise_price"}
_NUMS = {"min_margin_pct": (0.0, 95.0), "max_price_increase_pct": (1.0, 200.0)}


def _uid() -> int:
    try:
        return auth_context.current_user_id()
    except auth_context.NoUserContext:
        raise ApiError(401, "Inicia sesión") from None


def _aad(user_id: int, platform: str, domain: str) -> str:
    return f"user:{user_id}|{platform}|{domain}"


# ---------------------------------------------------------------- API para otros módulos (motor de sincronización)

def load_settings(row) -> dict:
    """Ajustes de la tienda con valores por defecto para lo que falte."""
    try:
        saved = json.loads(row["settings"] or "{}")
    except (ValueError, TypeError):
        saved = {}
    return {**DEFAULT_SETTINGS, **{k: v for k, v in saved.items() if k in DEFAULT_SETTINGS}}


def get_connection(conn, user_id: int, connection_id: int):
    """Fila de la conexión del usuario, o None si no existe o es de otra persona."""
    return conn.execute("SELECT * FROM store_connections WHERE id = ? AND user_id = ?",
                        (connection_id, user_id)).fetchone()


def connector_for(row) -> sc.StoreConnector:
    """Conector listo para usar con las credenciales descifradas de la conexión."""
    creds = json.loads(secrets_store.decrypt(row["credentials_enc"],
                                             _aad(row["user_id"], row["platform"], row["domain"])))
    return sc.create(row["platform"], row["domain"], creds, load_settings(row))


def mark_error(conn, connection_id: int, message: str) -> None:
    conn.execute("UPDATE store_connections SET status = 'error', last_error = ? WHERE id = ?",
                 (message[:300], connection_id))


def mark_ok(conn, connection_id: int) -> None:
    conn.execute("UPDATE store_connections SET status = 'active', last_error = NULL, last_validated_at = ? "
                 "WHERE id = ?", (db.now(), connection_id))


# ---------------------------------------------------------------- validación de entrada

def _clean_settings(raw) -> dict:
    if not isinstance(raw, dict):
        raise ApiError(400, "Los ajustes deben ser un objeto")
    out = {}
    for k, v in raw.items():
        if k in _BOOLS:
            if not isinstance(v, bool):
                raise ApiError(400, f"Ajuste inválido: {k}")
            out[k] = v
        elif k in _NUMS:
            try:
                f = float(v)
            except (TypeError, ValueError):
                raise ApiError(400, f"Ajuste inválido: {k}") from None
            lo, hi = _NUMS[k]
            if not (lo <= f <= hi):
                raise ApiError(400, f"{k} debe estar entre {lo:g} y {hi:g}")
            out[k] = f
        else:
            raise ApiError(400, f"Ajuste desconocido: {k}")
    return out


def _clean_credentials(cls, raw) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise ApiError(400, "Las credenciales deben ser un objeto")
    allowed = {f["key"] for f in cls.credential_fields}
    out = {}
    for f in cls.credential_fields:
        v = raw.get(f["key"])
        if v is None or (isinstance(v, str) and not v.strip()):
            if f.get("required", True):
                raise ApiError(400, f"Falta: {f['label']}")
            continue
        if not isinstance(v, str):
            raise ApiError(400, f"{f['label']} debe ser texto")
        if len(v) > 500:
            raise ApiError(400, f"{f['label']} es demasiado largo")
        out[f["key"]] = v.strip()
    extra = set(raw) - allowed
    if extra:
        raise ApiError(400, f"Campos no permitidos: {', '.join(sorted(extra))}")
    _call_store(lambda: cls.check_credentials(out))
    return out


def _call_store(fn):
    """Convierte errores de la tienda en respuestas HTTP. Nunca 401 (el navegador lo tomaría por sesión perdida)."""
    try:
        return fn()
    except sc.StoreAuthError as e:
        raise ApiError(400, f"La tienda rechazó las credenciales: {e}") from None
    except sc.StoreValidationError as e:
        raise ApiError(400, str(e)) from None
    except sc.StoreRateLimited as e:
        raise ApiError(429, f"La tienda pidió esperar; reintenta en unos segundos ({e})") from None
    except sc.StoreError as e:
        raise ApiError(502, f"No se pudo hablar con la tienda: {e}") from None


def _view(row, listings: int = 0) -> dict:
    try:
        creds = json.loads(secrets_store.decrypt(row["credentials_enc"],
                                                 _aad(row["user_id"], row["platform"], row["domain"])))
        cls = sc.get_class(row["platform"])
        masked = {f["key"]: secrets_store.mask(creds.get(f["key"], "")) for f in cls.credential_fields if f["secret"]}
    except (secrets_store.SecretError, ValueError, sc.StoreError):
        masked = {}
    return {"id": row["id"], "platform": row["platform"], "name": row["name"], "domain": row["domain"],
            "status": row["status"], "last_error": row["last_error"], "currency": row["currency"],
            "settings": load_settings(row), "credentials": masked, "listings": listings,
            "created_at": row["created_at"], "last_validated_at": row["last_validated_at"]}


# ---------------------------------------------------------------- handlers

def list_platforms() -> dict:
    return {"items": [{"platform": c.platform, "label": c.label, "credential_fields": c.credential_fields,
                       "setup_help": c.setup_help}
                      for c in sc.platforms()], "default_settings": DEFAULT_SETTINGS}


def list_stores() -> dict:
    uid = _uid()
    with db.connect() as conn:
        counts = dict(conn.execute("SELECT connection_id, COUNT(*) FROM store_listings WHERE user_id = ? "
                                   "GROUP BY connection_id", (uid,)))
        rows = conn.execute("SELECT * FROM store_connections WHERE user_id = ? ORDER BY id", (uid,)).fetchall()
        return {"items": [_view(r, counts.get(r["id"], 0)) for r in rows], "max": MAX_CONNECTIONS}


def connect_store(body: dict) -> dict:
    uid = _uid()
    platform = str(body.get("platform") or "")
    cls = _call_store(lambda: sc.get_class(platform))
    domain = _call_store(lambda: cls.normalize_domain(str(body.get("domain") or "")))
    creds = _clean_credentials(cls, body.get("credentials"))
    settings = _clean_settings(body["settings"]) if "settings" in body else {}
    name = str(body.get("name") or "").strip()[:80]
    with db.connect() as conn:
        if conn.execute("SELECT COUNT(*) FROM store_connections WHERE user_id = ?", (uid,)).fetchone()[0] >= MAX_CONNECTIONS:
            raise ApiError(409, f"Máximo {MAX_CONNECTIONS} tiendas conectadas")
        if conn.execute("SELECT 1 FROM store_connections WHERE user_id = ? AND platform = ? AND domain = ?",
                        (uid, platform, domain)).fetchone():
            raise ApiError(409, "Esa tienda ya está conectada")
    # La validación habla con la tienda: se hace sin ninguna transacción abierta
    info = _call_store(lambda: cls(domain, creds, settings).validate())
    if info.missing_scopes:
        raise ApiError(400, "Faltan permisos en la aplicación de la tienda: " + ", ".join(info.missing_scopes))
    enc = secrets_store.encrypt(json.dumps(creds), _aad(uid, platform, domain))
    with db.connect() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO store_connections(user_id, platform, name, domain, credentials_enc, status, settings, "
                "currency, location_id, created_at, last_validated_at) VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?)",
                (uid, platform, name or info.name or domain, domain, enc, json.dumps(settings),
                 info.currency, info.location_id, db.now(), db.now()))
        except Exception as e:  # noqa: BLE001 — carrera: otra petición la conectó entre la comprobación y el insert
            if "UNIQUE" in str(e):
                raise ApiError(409, "Esa tienda ya está conectada") from None
            raise
        return _view(get_connection(conn, uid, cur.lastrowid))


def get_store(cid: int) -> dict:
    uid = _uid()
    with db.connect() as conn:
        row = get_connection(conn, uid, cid)
        if not row:
            raise ApiError(404, "Tienda no encontrada")
        n = conn.execute("SELECT COUNT(*) FROM store_listings WHERE connection_id = ?", (cid,)).fetchone()[0]
        return _view(row, n)


def update_store(cid: int, body: dict) -> dict:
    """Cambia nombre, ajustes de sincronización y/o rota las credenciales (se vuelven a validar)."""
    uid = _uid()
    with db.connect() as conn:
        row = get_connection(conn, uid, cid)
        if not row:
            raise ApiError(404, "Tienda no encontrada")
    updates, args = [], []
    if "name" in body:
        name = str(body["name"] or "").strip()[:80]
        if not name:
            raise ApiError(400, "El nombre no puede estar vacío")
        updates.append("name = ?"); args.append(name)
    if "settings" in body:
        merged = {**load_settings(row), **_clean_settings(body["settings"])}
        updates.append("settings = ?"); args.append(json.dumps(merged))
    if "credentials" in body:
        cls = _call_store(lambda: sc.get_class(row["platform"]))
        creds = _clean_credentials(cls, body["credentials"])
        info = _call_store(lambda: cls(row["domain"], creds, load_settings(row)).validate())
        if info.missing_scopes:
            raise ApiError(400, "Faltan permisos en la aplicación de la tienda: " + ", ".join(info.missing_scopes))
        updates += ["credentials_enc = ?", "status = 'active'", "last_error = NULL", "last_validated_at = ?",
                    "currency = ?", "location_id = ?"]
        args += [secrets_store.encrypt(json.dumps(creds), _aad(uid, row["platform"], row["domain"])),
                 db.now(), info.currency, info.location_id]
    if updates:
        with db.connect() as conn:
            conn.execute(f"UPDATE store_connections SET {', '.join(updates)} WHERE id = ? AND user_id = ?",
                         args + [cid, uid])
    return get_store(cid)


def validate_store(cid: int) -> dict:
    uid = _uid()
    with db.connect() as conn:
        row = get_connection(conn, uid, cid)
        if not row:
            raise ApiError(404, "Tienda no encontrada")
    try:
        info = _call_store(lambda: connector_for(row).validate())
    except ApiError as e:
        with db.connect() as conn:
            mark_error(conn, cid, str(e))
        raise
    with db.connect() as conn:
        mark_ok(conn, cid)
        conn.execute("UPDATE store_connections SET currency = ?, location_id = ? WHERE id = ?",
                     (info.currency, info.location_id, cid))
    return get_store(cid)


def disconnect_store(cid: int, qs: dict) -> dict:
    """Desconecta: DropFinder deja de sincronizar. Los productos ya publicados QUEDAN en la tienda."""
    uid = _uid()
    with db.connect() as conn:
        row = get_connection(conn, uid, cid)
        if not row:
            raise ApiError(404, "Tienda no encontrada")
        n = conn.execute("SELECT COUNT(*) FROM store_listings WHERE connection_id = ?", (cid,)).fetchone()[0]
        if n and qs.get("confirm", ["0"])[0] != "1":
            raise ApiError(409, f"Hay {n} producto(s) publicados en esta tienda. Seguirán en la tienda, pero "
                                "DropFinder dejará de sincronizarlos. Confirma para continuar.")
        conn.execute("DELETE FROM store_connections WHERE id = ? AND user_id = ?", (cid, uid))
    return {"ok": True, "listings_left_in_store": n}


ROUTES: list = []
ROUTES += [
    ("GET", r"/api/stores/platforms", lambda m, qs, b: list_platforms()),
    ("GET", r"/api/stores", lambda m, qs, b: list_stores()),
    ("POST", r"/api/stores", lambda m, qs, b: connect_store(b)),
    ("GET", r"/api/stores/(\d+)", lambda m, qs, b: get_store(int(m[1]))),
    ("PATCH", r"/api/stores/(\d+)", lambda m, qs, b: update_store(int(m[1]), b)),
    ("POST", r"/api/stores/(\d+)/validate", lambda m, qs, b: validate_store(int(m[1]))),
    ("DELETE", r"/api/stores/(\d+)", lambda m, qs, b: disconnect_store(int(m[1]), qs)),
]
