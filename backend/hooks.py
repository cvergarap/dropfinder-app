"""Puntos de extensión entre el núcleo (db/server) y los módulos de funcionalidades.

Los módulos (history, compare, saved_searches…) se registran al importarse; el núcleo solo
llama a `run_catalog_write` cuando cambian productos, sin conocer a los módulos.
"""
from __future__ import annotations

import logging
import sqlite3

log = logging.getLogger("dropfinder.hooks")
_catalog_write: dict = {}     # "modulo.funcion" -> fn (re-importar un módulo no lo registra dos veces)


def on_catalog_write(fn):
    """Registra fn(conn, product_ids) para reaccionar a cambios del catálogo. Usable como decorador.

    - Se llama dentro de la misma transacción que hizo el cambio, cuando la operación ya terminó
      (carga, reemplazo de CSV, monitor). Usar `conn`; no abrir otra conexión (vería datos viejos o
      se bloquearía).
    - product_ids: lista de ids insertados/actualizados ([] = nada cambió; no se llama). None = no se
      sabe cuáles (revisar todo).
    - Los borrados no llaman al hook: las tablas de los módulos usan ON DELETE CASCADE.
    - Si el hook falla, su trabajo se deshace (SAVEPOINT) y se registra el error, pero la escritura
      del catálogo continúa: un módulo con errores no debe impedir cargar productos ni arrancar la app."""
    _catalog_write[f"{fn.__module__}.{fn.__qualname__}"] = fn
    return fn


def run_catalog_write(conn, product_ids=None) -> None:
    ids = list(product_ids) if product_ids is not None else None
    if ids == []:
        return
    for name, fn in list(_catalog_write.items()):
        conn.execute("SAVEPOINT df_hook")
        try:
            fn(conn, ids)
            conn.execute("RELEASE df_hook")
        except (Exception, sqlite3.Error):  # noqa: BLE001
            conn.execute("ROLLBACK TO df_hook")
            conn.execute("RELEASE df_hook")
            log.exception("Falló el hook de catálogo %s; se omitió su trabajo", name)
