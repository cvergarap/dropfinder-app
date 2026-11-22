"""Ayuda para pruebas con cuentas de usuario (Sprint 3).

- Importarlo fija en el hilo principal el contexto del propietario 1 (auth_context.LEGACY_USER_ID), así las
  pruebas que llaman a los handlers directamente (server.upload_csv, server.create_import…) siguen
  funcionando como antes. Para otro usuario: `with auth_context.as_user(uid, "member"): ...`.
- auth_headers(): cabeceras (cookie de sesión + X-CSRF-Token) del propietario de la base actual
  (db.DB_PATH). Lo crea la primera vez (en una base nueva recibe el id 1) junto con una sesión.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

import auth  # noqa: E402
import auth_context  # noqa: E402
import db  # noqa: E402

OWNER_EMAIL = "propietario@prueba.local"
OWNER_PASSWORD = "clave-de-prueba-123"
_hash: list = []
_sessions: dict = {}     # DB_PATH -> (token, csrf, uid)

auth_context.set_current(auth_context.LEGACY_USER_ID, "owner")


def owner_session() -> tuple[str, str, int]:
    """(token, csrf, user_id) del propietario de la base actual; crea usuario y sesión si hace falta."""
    if not _hash:
        _hash.append(auth.hash_password(OWNER_PASSWORD))
    cached = _sessions.get(db.DB_PATH)
    with db.connect() as conn:
        if cached and conn.execute("SELECT 1 FROM sessions WHERE token_hash = ?",
                                   (auth._hash_token(cached[0]),)).fetchone():
            return cached
        row = conn.execute("SELECT id FROM users WHERE role = 'owner' AND disabled = 0 ORDER BY id LIMIT 1").fetchone()
        uid = row[0] if row else auth.create_first_owner(conn, OWNER_EMAIL, "Propietaria", _hash[0])
        token, csrf = auth.create_session(conn, uid)
    _sessions[db.DB_PATH] = (token, csrf, uid)
    return token, csrf, uid


def session_headers(token: str, csrf: str | None = None) -> dict:
    h = {"Cookie": f"{auth.COOKIE_NAME}={token}"}
    if csrf:
        h["X-CSRF-Token"] = csrf
    return h


def auth_headers() -> dict:
    token, csrf, uid = owner_session()
    assert uid == auth_context.LEGACY_USER_ID, "las pruebas antiguas esperan que el propietario sea el usuario 1"
    return session_headers(token, csrf)
