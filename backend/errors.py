"""Errores compartidos entre server.py y los módulos de funcionalidades (evita importaciones circulares)."""


class ApiError(Exception):
    """Error con código HTTP; el servidor responde {"error": mensaje} con ese código."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
