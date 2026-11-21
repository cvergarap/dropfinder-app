"""Puntos de extensión entre el núcleo (db/server) y los módulos de funcionalidades.

Los módulos (history, compare, saved_searches…) se registran al importarse; el núcleo solo
llama a `run_catalog_write` cuando cambian productos, sin conocer a los módulos.
"""
from __future__ import annotations

_catalog_write: list = []


def on_catalog_write(fn):
    """Registra fn(conn, product_ids) — product_ids es una lista o None (= no se sabe cuáles, revisar todo).
    Se llama dentro de la misma transacción que hizo el cambio. Usable como decorador."""
    if fn not in _catalog_write:
        _catalog_write.append(fn)
    return fn


def run_catalog_write(conn, product_ids=None) -> None:
    ids = list(product_ids) if product_ids is not None else None
    for fn in list(_catalog_write):
        fn(conn, ids)
