"""Errores compartidos entre server.py y los módulos de funcionalidades (evita importaciones circulares)."""


class ApiError(Exception):
    """Error con código HTTP; el servidor responde {"error": mensaje} con ese código."""

    def __init__(self, status: int, message: str, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code          # identificador estable para que la interfaz distinga casos con el mismo estado (409…)


class FileResponse:
    """Respuesta de archivo (descarga) en lugar de JSON: un handler la devuelve y server.py escribe los
    bytes tal cual con Content-Type y, si hay filename, Content-Disposition: attachment."""

    def __init__(self, body: bytes, content_type: str, filename: str | None = None, headers: dict | None = None):
        self.body, self.content_type, self.headers = body, content_type, dict(headers or {})
        # Solo caracteres seguros en el nombre (evita inyectar cabeceras o rutas)
        self.filename = "".join(c for c in filename if c.isascii() and (c.isalnum() or c in "._-")) if filename else None
