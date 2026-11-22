"""Cuentas de usuario: contraseñas, sesiones, CSRF, límite de intentos y administración de usuarios.

Decisiones de seguridad (ver docs/adr/0001-stack.md y README):
- Contraseñas con hashlib.scrypt (n=2**14, r=8, p=1, sal aleatoria de 16 bytes). Se guarda
  "scrypt$n$r$p$sal$hash" (base64) para poder subir los parámetros más adelante sin romper las cuentas.
  La comparación es en tiempo constante (hmac.compare_digest).
- Sesión = token aleatorio de 32 bytes (secrets.token_urlsafe) en la cookie `df_session`
  (HttpOnly, SameSite=Lax, Path=/). Detrás del proxy HTTPS de confianza la cookie es `__Host-df_session`
  con Secure (el navegador no la envía por http ni deja que un subdominio la fije). En la base solo se
  guarda su SHA-256: quien lea la base no puede usar las sesiones.
- Caducidad deslizante: 14 días sin uso. last_seen_at se escribe como mucho cada TOUCH_INTERVAL_S.
  Al iniciar sesión se crea un token nuevo (y se borra el anterior); al cambiar la contraseña se cierran
  todas las sesiones del usuario.
- CSRF: cada sesión tiene su csrf_token; toda petición que modifica datos debe enviarlo en la cabecera
  X-CSRF-Token (lo comprueba server.py). Además se rechazan peticiones con Origin/Referer de otro sitio.
- Límite de intentos: 5 fallos por correo o 20 por IP en 15 minutos → 429 con Retry-After. El mensaje
  de error es siempre el mismo y un correo desconocido también calcula un scrypt (no se puede saber
  qué correos tienen cuenta midiendo el tiempo de respuesta).

Uso desde otros módulos: auth_context.current_user_id() / current_role() (los fija server.py en cada
petición). Para borrar datos propios al eliminar un usuario: auth.on_user_delete(fn(conn, user_id)).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

import auth_context
import db
from errors import ApiError

# ---------------------------------------------------------------- parámetros

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1
SALT_BYTES = 16
HASH_BYTES = 64
MIN_PASSWORD = 10
MAX_PASSWORD = 1024          # evita gastar CPU con contraseñas gigantes
MAX_NAME = 80
MAX_EMAIL = 254

COOKIE_NAME = "df_session"
SECURE_COOKIE_NAME = "__Host-df_session"     # solo por HTTPS (proxy de confianza)
SESSION_TTL = timedelta(days=14)
TOUCH_INTERVAL_S = 300       # last_seen_at/expires_at se actualizan como mucho cada 5 minutos
PURGE_INTERVAL_S = 3600      # limpieza periódica de sesiones vencidas e intentos viejos

LOGIN_WINDOW = timedelta(minutes=15)
MAX_FAILS_EMAIL = 5
MAX_FAILS_IP = 20

ROLES = ("owner", "member")
BAD_LOGIN = "Correo o contraseña incorrectos"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(t: datetime) -> str:
    return t.isoformat(timespec="seconds")


def _parse(ts: str) -> datetime:
    t = datetime.fromisoformat(ts)
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------- contraseñas

def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int, dklen: int) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=dklen,
                          maxmem=2 * 128 * r * (n + p + 2))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(SALT_BYTES)
    dk = _scrypt(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P, HASH_BYTES)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(dk)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, expected = stored.split("$")
        if algo != "scrypt":
            return False
        expected_b = base64.b64decode(expected)
        dk = _scrypt(password, base64.b64decode(salt), int(n), int(r), int(p), len(expected_b))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk, expected_b)


_dummy_hash: str | None = None
_dummy_lock = threading.Lock()


def _dummy() -> str:
    """Hash de relleno: un correo desconocido cuesta lo mismo que uno real (sin enumeración por tiempo)."""
    global _dummy_hash
    with _dummy_lock:
        if _dummy_hash is None:
            _dummy_hash = hash_password(secrets.token_urlsafe(16))
        return _dummy_hash


def check_password_policy(password, email: str = "") -> str:
    if not isinstance(password, str) or len(password) < MIN_PASSWORD:
        raise ApiError(400, f"La contraseña debe tener al menos {MIN_PASSWORD} caracteres")
    if len(password) > MAX_PASSWORD:
        raise ApiError(400, f"La contraseña admite como máximo {MAX_PASSWORD} caracteres")
    if email and norm_email(password) == norm_email(email):
        raise ApiError(400, "La contraseña no puede ser igual a tu correo")
    return password


def norm_email(email) -> str:
    """Correo normalizado (Unicode: casefold; COLLATE NOCASE de SQLite solo pliega ASCII)."""
    return email.strip().casefold() if isinstance(email, str) else ""


def clean_email(email) -> str:
    email = norm_email(email)
    if not EMAIL_RE.match(email) or len(email) > MAX_EMAIL:
        raise ApiError(400, "Escribe un correo válido")
    return email


def clean_name(name) -> str:
    name = " ".join(name.split()) if isinstance(name, str) else ""
    if not name or len(name) > MAX_NAME:
        raise ApiError(400, f"El nombre debe tener entre 1 y {MAX_NAME} caracteres")
    return name


# ---------------------------------------------------------------- contexto de la petición

class RequestInfo:
    """Datos de la petición HTTP que necesitan las rutas de cuentas (los fija server.py)."""

    def __init__(self, ip: str = "", user_agent: str = "", token: str | None = None, secure: bool = False):
        self.ip, self.user_agent, self.token, self.secure = ip, user_agent[:300], token, secure
        self.session: dict | None = None      # sesión válida (si hay)
        self.set_cookies: list[str] = []      # cabeceras Set-Cookie a enviar en la respuesta


_req = threading.local()


def set_request(info: RequestInfo | None) -> None:
    _req.info = info


def request() -> RequestInfo:
    info = getattr(_req, "info", None)
    if info is None:          # llamadas directas (pruebas): petición vacía
        info = RequestInfo()
        _req.info = info
    return info


class Reply:
    """Respuesta con código distinto de 200 (las cookies van en request().set_cookies)."""

    def __init__(self, data, status: int = 200):
        self.data, self.status = data, status


def cookie_name(secure: bool) -> str:
    return SECURE_COOKIE_NAME if secure else COOKIE_NAME


def cookie_header(token: str | None, secure: bool) -> str:
    parts = [f"{cookie_name(secure)}={token or ''}", "Path=/", "HttpOnly", "SameSite=Lax"]
    parts.append(f"Max-Age={int(SESSION_TTL.total_seconds())}" if token else "Max-Age=0")
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def require_owner(action: str) -> None:
    """Para los módulos: lanza 403 si el usuario de la petición no es propietario."""
    if not auth_context.is_owner():
        raise ApiError(403, f"Solo el propietario puede {action}.")


# ---------------------------------------------------------------- sesiones

def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(conn, user_id: int, user_agent: str = "") -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    now = _now()
    conn.execute("INSERT INTO sessions(token_hash, user_id, csrf_token, created_at, expires_at, last_seen_at, "
                 "user_agent) VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (_hash_token(token), user_id, csrf, _iso(now), _iso(now + SESSION_TTL), _iso(now), user_agent[:300]))
    return token, csrf


_last_purge = [0.0]
_purge_lock = threading.Lock()


def maybe_purge() -> None:
    """Borra sesiones vencidas e intentos de inicio de sesión fuera de la ventana (como mucho cada hora).
    La llama server.py en las peticiones; nunca lanza."""
    with _purge_lock:
        if time.monotonic() - _last_purge[0] < PURGE_INTERVAL_S:
            return
        _last_purge[0] = time.monotonic()
    try:
        with db.connect() as conn:
            conn.execute("PRAGMA busy_timeout = 200")
            purge(conn)
    except sqlite3.Error:
        _last_purge[0] = 0.0         # base ocupada: se reintenta en la próxima petición


def purge(conn) -> None:
    now = _now()
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (_iso(now),))
    conn.execute("DELETE FROM login_attempts WHERE created_at < ?", (_iso(now - LOGIN_WINDOW),))


def load_session(token: str | None, touch: bool = True) -> dict | None:
    """Sesión válida (con datos del usuario) o None. Desliza la caducidad como mucho cada TOUCH_INTERVAL_S;
    en ese caso marca session['touched'] para que el servidor renueve la cookie."""
    if not token or len(token) > 200:
        return None
    th = _hash_token(token)
    with db.connect() as conn:
        r = conn.execute("SELECT s.token_hash, s.csrf_token, s.expires_at, s.last_seen_at, u.id, u.email, u.name, "
                         "u.role, u.disabled FROM sessions s JOIN users u ON u.id = s.user_id "
                         "WHERE s.token_hash = ?", (th,)).fetchone()
        if not r:
            return None
        now = _now()
        if _parse(r["expires_at"]) <= now or r["disabled"]:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (th,))
            return None
        sess = {"token_hash": th, "csrf_token": r["csrf_token"], "touched": False,
                "user": {"id": r["id"], "email": r["email"], "name": r["name"], "role": r["role"]}}
        if touch and (now - _parse(r["last_seen_at"])).total_seconds() >= TOUCH_INTERVAL_S:
            try:
                conn.execute("PRAGMA busy_timeout = 200")
                conn.execute("UPDATE sessions SET last_seen_at = ?, expires_at = ? WHERE token_hash = ?",
                             (_iso(now), _iso(now + SESSION_TTL), th))
                sess["touched"] = True
            except sqlite3.OperationalError:
                pass                 # base ocupada: se desliza en la próxima petición
        return sess


def _start_session(conn, user_id: int) -> str:
    """Inicia sesión para la petición en curso: borra la sesión anterior del navegador (rotación) y
    deja la cookie nueva en la respuesta. Devuelve el csrf_token."""
    info = request()
    if info.token:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash_token(info.token),))
    token, csrf = create_session(conn, user_id, info.user_agent)
    info.token = token
    info.set_cookies.append(cookie_header(token, info.secure))
    return csrf


def _user_json(r) -> dict:
    return {"id": r["id"], "email": r["email"], "name": r["name"], "role": r["role"]}


# ---------------------------------------------------------------- límite de intentos

def _retry_after(conn, email: str, ip: str) -> int:
    """Segundos de espera si el correo o la IP superaron el límite de fallos (0 si no)."""
    now = _now()
    since = _iso(now - LOGIN_WINDOW)
    waits = []
    for col, val, limit in (("email", email, MAX_FAILS_EMAIL), ("ip", ip, MAX_FAILS_IP)):
        rows = conn.execute(f"SELECT created_at FROM login_attempts WHERE {col} = ? AND ok = 0 AND created_at > ? "
                            "ORDER BY created_at DESC LIMIT ?", (val, since, limit)).fetchall()
        if len(rows) >= limit:
            oldest = _parse(rows[-1][0])           # cuando este fallo salga de la ventana, se libera un intento
            waits.append(max(1, int((oldest + LOGIN_WINDOW - now).total_seconds()) + 1))
    return max(waits, default=0)


def _record_attempt(conn, email: str, ip: str, ok: bool) -> None:
    now = _now()
    conn.execute("INSERT INTO login_attempts(email, ip, ok, created_at) VALUES (?, ?, ?, ?)",
                 (email, ip, int(ok), _iso(now)))
    if ok:     # un acceso correcto libera al correo (los fallos por IP siguen contando)
        conn.execute("DELETE FROM login_attempts WHERE email = ? AND ok = 0", (email,))
    conn.execute("DELETE FROM login_attempts WHERE created_at < ?", (_iso(now - LOGIN_WINDOW),))


def _too_many(seconds: int) -> ApiError:
    minutes = max(1, round(seconds / 60))
    e = ApiError(429, f"Demasiados intentos fallidos. Espera {minutes} minuto{'s' if minutes != 1 else ''} "
                      "y vuelve a intentarlo.")
    e.headers = {"Retry-After": str(seconds)}
    return e


# ---------------------------------------------------------------- borrado de datos de un usuario

_user_delete_hooks: dict = {}


def on_user_delete(fn):
    """Registra fn(conn, user_id): borra los datos propios del módulo (dentro de la misma transacción)."""
    _user_delete_hooks[f"{fn.__module__}.{fn.__qualname__}"] = fn
    return fn


_NOT_USER_DATA = {"users", "sessions", "login_attempts", "sqlite_sequence"}


def user_tables(conn) -> list[str]:
    """Tablas con datos por usuario: toda tabla con columna user_id (imported_products, alerts,
    saved_searches, user_currency_settings y las que creen los módulos, p. ej. collections y
    collection_items). Ordenadas para borrar primero las que referencian a otras (hijas antes que padres)."""
    names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
    tables = [t for t in names if t not in _NOT_USER_DATA
              and any(c[1] == "user_id" for c in conn.execute(f'PRAGMA table_info("{t}")'))]
    refs = {t: {r[2] for r in conn.execute(f'PRAGMA foreign_key_list("{t}")')} & set(tables) for t in tables}
    ordered: list[str] = []

    def visit(t, path=()):
        if t in ordered or t in path:
            return
        for u in sorted(t2 for t2 in tables if t in refs[t2]):   # primero las que referencian a t
            visit(u, (*path, t))
        ordered.append(t)
    for t in sorted(tables):
        visit(t)
    return ordered


def _delete_user_data(conn, uid: int) -> None:
    for fn in list(_user_delete_hooks.values()):
        fn(conn, uid)
    for (sid,) in conn.execute("SELECT id FROM saved_searches WHERE user_id = ?", (uid,)).fetchall():
        conn.execute("DELETE FROM meta WHERE key = ?", (f"saved_search_seen:{sid}",))
    for t in user_tables(conn):
        conn.execute(f'DELETE FROM "{t}" WHERE user_id = ?', (uid,))
    conn.execute("DELETE FROM sessions WHERE user_id = ?", (uid,))


# ---------------------------------------------------------------- rutas públicas

def has_users(conn) -> bool:
    return conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None


def status() -> dict:
    with db.connect() as conn:
        users = has_users(conn)
    info = request()
    sess = info.session or load_session(info.token, touch=False)
    return {"has_users": users, "authenticated": bool(sess)}


def create_first_owner(conn, email: str, name: str, pw_hash: str) -> int:
    """Crea el primer propietario y le pasa los datos anteriores a las cuentas (user_id = LEGACY_USER_ID),
    en la transacción de quien llama (no se depende de que su id sea 1). Requiere que no haya usuarios."""
    if has_users(conn):
        raise ApiError(403, "Ya existe una cuenta de propietario. Pide al propietario que te cree una cuenta.")
    uid = conn.execute("INSERT INTO users(email, name, password_hash, role, created_at, last_login_at) "
                       "VALUES (?, ?, ?, 'owner', ?, ?)", (email, name, pw_hash, db.now(), db.now())).lastrowid
    for t in user_tables(conn):
        # OR IGNORE: si una fila chocara con un índice único por usuario (p. ej. la colección "Favoritos" de
        # collections), se omite en vez de impedir el registro. No debería pasar: el nuevo usuario aún no tiene datos.
        conn.execute(f'UPDATE OR IGNORE "{t}" SET user_id = ? WHERE user_id = ?', (uid, auth_context.LEGACY_USER_ID))
    return uid


def register(body: dict) -> Reply:
    """Primer arranque: crea la cuenta del propietario (id 1, dueño de los datos existentes)."""
    if not isinstance(body, dict):
        raise ApiError(400, "JSON inválido")
    email = clean_email(body.get("email"))
    name = clean_name(body.get("name"))
    password = check_password_policy(body.get("password"), email)
    pw_hash = hash_password(password)
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")           # dos registros simultáneos se serializan
        uid = create_first_owner(conn, email, name, pw_hash)
        csrf = _start_session(conn, uid)
        user = _user_json(conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone())
    return Reply({"user": user, "csrf_token": csrf}, 201)


def login(body: dict) -> dict:
    if not isinstance(body, dict):
        raise ApiError(400, "JSON inválido")
    email = norm_email(body.get("email"))
    password = body.get("password") if isinstance(body.get("password"), str) else ""
    if not email or not password:
        raise ApiError(400, "Escribe tu correo y tu contraseña")
    email, password = email[:MAX_EMAIL], password[:MAX_PASSWORD + 1]
    info = request()
    with db.connect() as conn:
        wait = _retry_after(conn, email, info.ip)
        if wait:
            raise _too_many(wait)
        u = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        ok = verify_password(password, u["password_hash"] if u else _dummy())
        if not (u and ok):
            _record_attempt(conn, email, info.ip, False)
            conn.commit()
            raise ApiError(401, BAD_LOGIN)
        _record_attempt(conn, email, info.ip, True)
        if u["disabled"]:
            conn.commit()
            raise ApiError(403, "Tu cuenta está desactivada. Habla con el propietario de la instalación.")
        conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (db.now(), u["id"]))
        csrf = _start_session(conn, u["id"])
        return {"user": _user_json(u), "csrf_token": csrf}


# ---------------------------------------------------------------- rutas con sesión

def _session() -> dict:
    sess = request().session
    if not sess:
        raise ApiError(401, "Inicia sesión para continuar")
    return sess


def logout() -> dict:
    info = request()
    with db.connect() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_session()["token_hash"],))
    info.set_cookies.append(cookie_header(None, info.secure))
    info.session = None
    return {"ok": True}


def me() -> dict:
    sess = _session()
    return {"user": sess["user"], "csrf_token": sess["csrf_token"]}


def update_me(body: dict) -> dict:
    """Cambiar el nombre y/o la contraseña (con la actual). Cambiar la contraseña cierra todas las
    sesiones del usuario y abre una nueva en este navegador."""
    if not isinstance(body, dict):
        raise ApiError(400, "JSON inválido")
    sess = _session()
    uid = sess["user"]["id"]
    info = request()
    csrf = sess["csrf_token"]
    with db.connect() as conn:
        u = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
        name = clean_name(body["name"]) if "name" in body else None
        new_pw = body.get("new_password")
        if new_pw is not None:
            current = body.get("current_password")
            wait = _retry_after(conn, u["email"], info.ip)
            if wait:
                raise _too_many(wait)
            if not isinstance(current, str) or not verify_password(current[:MAX_PASSWORD + 1], u["password_hash"]):
                _record_attempt(conn, u["email"], info.ip, False)
                conn.commit()
                raise ApiError(400, "La contraseña actual no es correcta")
            check_password_policy(new_pw, u["email"])
            conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(new_pw), uid))
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (uid,))
            csrf = _start_session(conn, uid)
        if name is not None:
            conn.execute("UPDATE users SET name = ? WHERE id = ?", (name, uid))
        user = _user_json(conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone())
    return {"user": user, "csrf_token": csrf}


# ---------------------------------------------------------------- administración (solo propietario)

def _admin_view(r) -> dict:
    return {"id": r["id"], "email": r["email"], "name": r["name"], "role": r["role"],
            "disabled": bool(r["disabled"]), "created_at": r["created_at"], "last_login_at": r["last_login_at"]}


def _active_owners(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM users WHERE role = 'owner' AND disabled = 0").fetchone()[0]


def list_users() -> dict:
    with db.connect() as conn:
        return {"items": [_admin_view(r) for r in conn.execute("SELECT * FROM users ORDER BY id")]}


def create_user(body: dict) -> Reply:
    if not isinstance(body, dict):
        raise ApiError(400, "JSON inválido")
    email = clean_email(body.get("email"))
    name = clean_name(body.get("name"))
    role = body.get("role", "member")
    if role not in ROLES:
        raise ApiError(400, "Rol inválido (owner o member)")
    password = check_password_policy(body.get("password"), email)
    pw_hash = hash_password(password)
    with db.connect() as conn:
        try:
            uid = conn.execute("INSERT INTO users(email, name, password_hash, role, created_at) VALUES (?, ?, ?, ?, ?)",
                               (email, name, pw_hash, role, db.now())).lastrowid
        except sqlite3.IntegrityError:
            raise ApiError(409, "Ya existe una cuenta con ese correo")
        return Reply(_admin_view(conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()), 201)


def update_user(uid: int, body: dict) -> dict:
    if not isinstance(body, dict):
        raise ApiError(400, "JSON inválido")
    me_id = auth_context.current_user_id()
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")           # la comprobación del último propietario no admite carreras
        u = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
        if not u:
            raise ApiError(404, "Usuario no encontrado")
        role = body.get("role", u["role"])
        if role not in ROLES:
            raise ApiError(400, "Rol inválido (owner o member)")
        disabled = body.get("disabled", bool(u["disabled"]))
        if not isinstance(disabled, bool):
            raise ApiError(400, "disabled debe ser true o false")
        loses_owner = u["role"] == "owner" and not u["disabled"] and (role != "owner" or disabled)
        if uid == me_id and loses_owner:
            raise ApiError(409, "No puedes quitarte el rol de propietario ni desactivar tu propia cuenta")
        if loses_owner and _active_owners(conn) <= 1:
            raise ApiError(409, "Debe quedar al menos un propietario activo")
        if "name" in body:
            conn.execute("UPDATE users SET name = ? WHERE id = ?", (clean_name(body["name"]), uid))
        if body.get("password") is not None:
            check_password_policy(body["password"], u["email"])
            conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(body["password"]), uid))
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (uid,))
        conn.execute("UPDATE users SET role = ?, disabled = ? WHERE id = ?", (role, int(disabled), uid))
        if disabled or role != u["role"]:
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (uid,))   # vuelve a entrar con su nuevo rol
        return _admin_view(conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone())


def delete_user(uid: int) -> dict:
    if uid == auth_context.current_user_id():
        raise ApiError(409, "No puedes eliminar tu propia cuenta")
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        u = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
        if not u:
            raise ApiError(404, "Usuario no encontrado")
        if u["role"] == "owner" and not u["disabled"] and _active_owners(conn) <= 1:
            raise ApiError(409, "Debe quedar al menos un propietario activo")
        _delete_user_data(conn, uid)
        conn.execute("DELETE FROM users WHERE id = ?", (uid,))
    return {"ok": True}


# ---------------------------------------------------------------- rutas

PUBLIC_PATHS = {"/api/auth/login", "/api/auth/register", "/api/auth/status"}

ROUTES: list = [
    ("GET", r"/api/auth/status", lambda m, qs, b: status()),
    ("POST", r"/api/auth/register", lambda m, qs, b: register(b)),
    ("POST", r"/api/auth/login", lambda m, qs, b: login(b)),
    ("POST", r"/api/auth/logout", lambda m, qs, b: logout()),
    ("GET", r"/api/auth/me", lambda m, qs, b: me()),
    ("PATCH", r"/api/auth/me", lambda m, qs, b: update_me(b)),
    ("GET", r"/api/users", lambda m, qs, b: list_users()),
    ("POST", r"/api/users", lambda m, qs, b: create_user(b)),
    ("PATCH", r"/api/users/(\d+)", lambda m, qs, b: update_user(int(m[1]), b)),
    ("DELETE", r"/api/users/(\d+)", lambda m, qs, b: delete_user(int(m[1]))),
]

# (método, patrón) → acción para el mensaje "Solo el propietario puede …"
OWNER_ONLY = {
    ("GET", r"/api/users"): "administrar usuarios",
    ("POST", r"/api/users"): "administrar usuarios",
    ("PATCH", r"/api/users/(\d+)"): "administrar usuarios",
    ("DELETE", r"/api/users/(\d+)"): "administrar usuarios",
}
