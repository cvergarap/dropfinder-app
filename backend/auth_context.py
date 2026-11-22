"""Usuario de la petición en curso (contexto por hilo).

server.py fija el usuario autenticado al empezar cada petición y lo limpia al terminar (finally).
Los módulos consultan current_user_id() para separar sus datos por usuario.

Sin contexto NO hay usuario: current_user_id() lanza NoUserContext. Así una ruta o un hilo de fondo
que olvide fijarlo falla de forma visible en vez de actuar como otro usuario (o como propietario).
Hilos de fondo y pruebas usan `with as_user(id, rol): ...` con el usuario dueño de cada dato.
"""
from __future__ import annotations

import threading

LEGACY_USER_ID = 1            # dueño de los datos anteriores a las cuentas (se reasignan al primer propietario)
_local = threading.local()


class NoUserContext(RuntimeError):
    """Se pidió el usuario actual fuera de una petición autenticada o de un `as_user`."""


def set_current(user_id: int | None, role: str | None = None) -> None:
    _local.user_id = user_id
    _local.role = role


def clear() -> None:
    _local.__dict__.clear()


def current_user_id() -> int:
    uid = getattr(_local, "user_id", None)
    if uid is None:
        raise NoUserContext("No hay un usuario en el contexto de esta petición")
    return uid


def current_role() -> str | None:
    """'owner' (propietario: administra catálogo, reglas y usuarios), 'member' o None sin contexto."""
    return getattr(_local, "role", None)


def is_owner() -> bool:
    return current_role() == "owner"


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
