"""Usuario de la petición en curso (contexto por hilo).

server.py fija el usuario autenticado al empezar cada petición y lo limpia al terminar. Los módulos
consultan current_user_id() para separar sus datos por usuario, sin depender de cómo se autentica.
Fuera de una petición (hilos de fondo, pruebas) devuelve DEFAULT_USER_ID, salvo que se fije otro.
"""
from __future__ import annotations

import threading

DEFAULT_USER_ID = 1           # instalación de un solo usuario / datos anteriores a las cuentas
_local = threading.local()


def set_current(user_id: int | None, role: str | None = None) -> None:
    _local.user_id = user_id
    _local.role = role


def clear() -> None:
    _local.__dict__.clear()


def current_user_id() -> int:
    uid = getattr(_local, "user_id", None)
    return DEFAULT_USER_ID if uid is None else uid


def current_role() -> str:
    """'owner' (propietario: administra catálogo, reglas y usuarios) o 'member'."""
    return getattr(_local, "role", None) or "owner"


class as_user:
    """Contexto para pruebas y tareas de fondo: `with as_user(2): ...`."""

    def __init__(self, user_id: int, role: str = "member"):
        self.user_id, self.role = user_id, role

    def __enter__(self):
        self.prev = (getattr(_local, "user_id", None), getattr(_local, "role", None))
        set_current(self.user_id, self.role)
        return self

    def __exit__(self, *exc):
        set_current(*self.prev)
