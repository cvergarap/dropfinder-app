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
import threading
from dataclasses import dataclass
from urllib.parse import urlsplit

from .base import StoreError

ALLOW_LOOPBACK = False          # SOLO pruebas: permite http://127.0.0.1:<puerto> (nunca se lee de datos del usuario)
DEFAULT_TIMEOUT = 20.0          # por operación de red (conectar, enviar, recibir un trozo)
TOTAL_TIMEOUT = 45.0            # TOTAL de la petición: un servidor que gotea bytes no puede retener el hilo del motor
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


# Redes que nunca se contactan, listadas explícitamente (no dependen de la versión de Python ni de ip.is_private)
_BLOCKED_NETS = [ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24",
    "192.0.2.0/24", "192.88.99.0/24", "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24",
    "224.0.0.0/4", "240.0.0.0/4",
    "::/96", "::ffff:0:0/96", "64:ff9b::/96", "64:ff9b:1::/48", "100::/64", "2001::/23", "2001:db8::/32",
    "2002::/16", "fc00::/7", "fe80::/10", "fec0::/10", "ff00::/8")]


def _is_public(ip: ipaddress._BaseAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if any(ip.version == n.version and ip in n for n in _BLOCKED_NETS):
        return False
    return ip.is_global                       # además de la lista: cualquier cosa que Python considere no global


_UNREACHABLE = "No se pudo conectar con el dominio de la tienda: comprueba que esté bien escrito y sea público"


def resolve_all_public(host: str, port: int) -> list[str]:
    """Todas las IP de `host`, ya validadas como públicas. Un nombre que no resuelve y uno que apunta a una red no
    pública dan el MISMO mensaje: un miembro no debe poder distinguir (y así enumerar) nombres de la red interna."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        raise StoreError(_UNREACHABLE, retryable=True, not_sent=True) from None
    ips: list[str] = []
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            raise StoreError(_UNREACHABLE, not_sent=True) from None
        if not _is_public(ip):
            raise StoreError(_UNREACHABLE, not_sent=True)
        if info[4][0] not in ips:
            ips.append(info[4][0])
    if not ips:
        raise StoreError(_UNREACHABLE, retryable=True, not_sent=True)
    return ips


def resolve_public(host: str, port: int) -> str:
    return resolve_all_public(host, port)[0]


class _PinnedHTTPS(http.client.HTTPSConnection):
    """Conecta a IP ya validadas (prueba cada una en orden), pero habla TLS con el nombre real (SNI y verificación)."""

    def __init__(self, host: str, ips, port: int, timeout: float):
        super().__init__(host, port, timeout=timeout, context=_ctx)
        self._ips = [ips] if isinstance(ips, str) else list(ips)

    def connect(self):
        last: Exception | None = None
        for ip in self._ips:
            try:
                sock = socket.create_connection((ip, self.port), self.timeout)
            except OSError as e:
                last = e
                continue
            try:
                self.sock = _ctx.wrap_socket(sock, server_hostname=self.host)
            except Exception:
                sock.close()
                raise
            return
        raise last or OSError("sin direcciones")


def request(method: str, url: str, *, headers: dict | None = None, body: bytes | None = None,
            timeout: float = DEFAULT_TIMEOUT, max_bytes: int = MAX_RESPONSE_BYTES,
            total_timeout: float = TOTAL_TIMEOUT) -> Response:
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

    # Vigilante: el tiempo por operación NO limita el total (un servidor que envía 1 byte cada pocos segundos
    # retendría el hilo del motor para siempre). Al vencer el tiempo total se corta el socket, aunque esté en una lectura.
    state = {"fired": False, "sock": None}

    def abort():
        state["fired"] = True
        sock = state["sock"]
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    watchdog = threading.Timer(max(0.05, total_timeout), abort)
    watchdog.daemon = True
    watchdog.start()
    too_slow = StoreError("La tienda tardó demasiado en responder", retryable=True)
    try:
        if loopback_test:
            c: http.client.HTTPConnection = http.client.HTTPConnection(host, port, timeout=timeout)
        else:
            c = _PinnedHTTPS(host, resolve_all_public(host, port), port, timeout)
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
            state["sock"] = c.sock
            if state["fired"]:
                abort()
                raise StoreError("No se pudo conectar con la tienda a tiempo", retryable=True, not_sent=True)
            c.request(method, path, body=body, headers=hdrs)
            r = c.getresponse()
            data = r.read(max_bytes + 1)
            if state["fired"]:               # el corte puede dejar una lectura "completa" pero truncada
                raise too_slow
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
        raise too_slow from None
    except ssl.SSLError:
        raise StoreError("No se pudo verificar el certificado de la tienda") from None
    except (OSError, http.client.HTTPException):
        raise (too_slow if state["fired"] else StoreError("No se pudo conectar con la tienda", retryable=True)) from None
    finally:
        watchdog.cancel()
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
