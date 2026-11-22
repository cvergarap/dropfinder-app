"""Panel del usuario: indicadores, gráficos, recomendaciones y primeros pasos.

Módulo de funcionalidad: expone ROUTES (método, regex, handler(match, qs, body)) que server.py agrega.
Datos por usuario: usar auth_context.current_user_id().
"""
from __future__ import annotations

ROUTES: list = []
