"""Cliente HTTP compartido por todos los conectores de tienda (anti-SSRF).

Reglas, iguales para todas las plataformas:
- Solo HTTPS (http únicamente hacia 127.0.0.1 cuando una PRUEBA activa ALLOW_LOOPBACK; nunca desde datos del usuario).
- Se resuelve el nombre una sola vez y se conecta a ESA IP (no hay una segunda resolución que un atacante pueda
  cambiar: DNS rebinding). Se rechazan direcciones privadas, de bucle, de enlace local, multicast, reservadas
  y la de metadatos de la nube, tanto IPv4 como IPv6 (incluidas las IPv4 mapeadas en IPv6).
- Sin seguir redirecciones (una redirección es un error: podría apuntar a la red local).
- Tiempo máximo por operación y tope de bytes de la respuesta; la verificación TLS no se puede desactivar.
- Puertos: solo 443 (o el de pruebas).
Los errores de red se convierten en StoreError(retryable=True); los mensajes no incluyen cabeceras ni cuerpos de
la petición (podrían contener credenciales).
"""
from __future__ import annotations

import http.client
import ipaddress
import json
import socket
import ssl
from dataclasses import dataclass
from urllib.parse import urlsplit

from .base import StoreError

ALLOW_LOOPBACK = False          # SOLO pruebas: permite http://127.0.0.1:<puerto> (nunca se lee de datos del usuario)
DEFAULT_TIMEOUT = 20.0
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
USER_AGENT = "DropFinder/1.0"
_ctx = ssl.create_default_context()      # verificación de certificado y nombre activas


@dataclass
class Response:
    status: int
    headers: dict
    body: bytes

    def json(self):
        try:
            return json.loads(self.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise StoreError("Respuesta inválida de la tienda (no es JSON)", retryable=True) from None


def _is_public(ip: ipaddress._BaseAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved
                or ip.is_unspecified or (isinstance(ip, ipaddress.IPv4Address) and ip in ipaddress.ip_network("100.64.0.0/10")))


def resolve_public(host: str, port: int) -> str:
    """IP pública de `host`; lanza StoreError si el nombre no resuelve o apunta a una red no pública."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        raise StoreError("No se pudo resolver el dominio de la tienda", retryable=True, not_sent=True) from None
    ips = []
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not _is_public(ip):
            raise StoreError("El dominio de la tienda apunta a una dirección no permitida")
        ips.append(info[4][0])
    if not ips:
        raise StoreError("No se pudo resolver el dominio de la tienda", retryable=True, not_sent=True)
    return ips[0]


class _PinnedHTTPS(http.client.HTTPSConnection):
    """Conecta a una IP ya validada, pero habla TLS con el nombre real (SNI y verificación de certificado)."""

    def __init__(self, host: str, ip: str, port: int, timeout: float):
        super().__init__(host, port, timeout=timeout, context=_ctx)
        self._ip = ip

    def connect(self):
        sock = socket.create_connection((self._ip, self.port), self.timeout)
        self.sock = _ctx.wrap_socket(sock, server_hostname=self.host)


def request(method: str, url: str, *, headers: dict | None = None, body: bytes | None = None,
            timeout: float = DEFAULT_TIMEOUT, max_bytes: int = MAX_RESPONSE_BYTES) -> Response:
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.username or parts.password or not host:
        raise StoreError("URL de tienda inválida")
    loopback_test = ALLOW_LOOPBACK and parts.scheme == "http" and host == "127.0.0.1"
    if parts.scheme != "https" and not loopback_test:
        raise StoreError("Solo se permiten conexiones HTTPS con la tienda")
    port = parts.port or (80 if loopback_test else 443)
    if not loopback_test and port != 443:
        raise StoreError("Puerto de tienda no permitido")
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json", **(headers or {})}
    try:
        if loopback_test:
            c: http.client.HTTPConnection = http.client.HTTPConnection(host, port, timeout=timeout)
        else:
            c = _PinnedHTTPS(host, resolve_public(host, port), port, timeout)
        r = None
        try:
            try:
                c.connect()                  # conectar (y TLS) por separado: un fallo aquí significa "no se envió nada"
            except (socket.timeout, TimeoutError):
                raise StoreError("No se pudo conectar con la tienda a tiempo", retryable=True, not_sent=True) from None
            except ssl.SSLError:
                raise StoreError("No se pudo verificar el certificado de la tienda") from None
            except OSError:
                raise StoreError("No se pudo conectar con la tienda", retryable=True, not_sent=True) from None
            c.request(method, path, body=body, headers=hdrs)
            r = c.getresponse()
            data = r.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise StoreError("La respuesta de la tienda es demasiado grande")
            resp = Response(r.status, {k.lower(): v for k, v in r.getheaders()}, data)
        finally:
            if r is not None:
                r.close()                    # higiene: cerrar siempre la respuesta aunque se haya leído solo parte del cuerpo
            c.close()
    except StoreError:
        raise
    except (socket.timeout, TimeoutError):
        raise StoreError("La tienda tardó demasiado en responder", retryable=True) from None
    except ssl.SSLError:
        raise StoreError("No se pudo verificar el certificado de la tienda") from None
    except (OSError, http.client.HTTPException):
        raise StoreError("No se pudo conectar con la tienda", retryable=True) from None
    if 300 <= resp.status < 400:
        raise StoreError("La tienda respondió con una redirección (no se sigue por seguridad)")
    return resp


def request_json(method: str, url: str, payload=None, **kw) -> Response:
    headers = dict(kw.pop("headers", None) or {})
    body = None
    if payload is not None:
        headers.setdefault("Content-Type", "application/json")
        body = json.dumps(payload).encode("utf-8")
    return request(method, url, headers=headers, body=body, **kw)
