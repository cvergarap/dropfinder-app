"""Comparador de productos y agrupación del mismo producto en varios proveedores (products.match_key).

Módulo de funcionalidad: expone ROUTES (método, regex, handler(match, qs, body)) que server.py agrega
a sus rutas, y puede registrarse en hooks.on_catalog_write.
"""
from __future__ import annotations

ROUTES: list = []
