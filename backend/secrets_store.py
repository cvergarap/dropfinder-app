"""Almacén de secretos: cifra credenciales de tiendas en reposo (ver docs/adr/0002-integracion-tiendas.md).

Esquema (solo biblioteca estándar): flujo de claves HMAC-SHA256 en modo contador + etiqueta HMAC-SHA256
(cifrar y luego autenticar), nonce aleatorio de 16 bytes y claves de cifrado/autenticación derivadas por
separado de la clave maestra. `aad` (datos asociados, p. ej. "user:3|store:7") entra en la etiqueta: un valor
cifrado para una fila no se puede copiar a otra.

Protege la base de datos copiada SIN el archivo de clave. No protege contra quien lea también `dropfinder.key`.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import threading

import db

VERSION = "v1"
_lock = threading.Lock()
_cache: dict[str, bytes] = {}          # ruta de clave -> clave maestra


class SecretError(Exception):
    """Valor cifrado alterado, de otra fila o con otra clave."""


def key_path() -> str:
    override = os.environ.get("DROPFINDER_KEY_FILE")
    return override or os.path.join(os.path.dirname(os.path.abspath(db.DB_PATH)), "dropfinder.key")


def _master() -> bytes:
    path = key_path()
    with _lock:
        if path in _cache:
            return _cache[path]
        try:
            with open(path, "rb") as f:
                key = base64.b64decode(f.read().strip())
        except FileNotFoundError:
            key = secrets.token_bytes(32)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)   # O_EXCL: nunca pisa una clave existente
            with os.fdopen(fd, "wb") as f:
                f.write(base64.b64encode(key))
        if len(key) != 32:
            raise SecretError("El archivo de clave es inválido")
        _cache[path] = key
        return key


def forget_cache() -> None:
    """Pruebas: olvida las claves leídas (cambiar de base de datos cambia la ruta de clave)."""
    with _lock:
        _cache.clear()


def _subkey(label: bytes) -> bytes:
    return hmac.new(_master(), b"dropfinder|" + label, hashlib.sha256).digest()


def _stream(key: bytes, nonce: bytes, n: int) -> bytes:
    out, counter = bytearray(), 0
    while len(out) < n:
        out += hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
        counter += 1
    return bytes(out[:n])


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def encrypt(plaintext: str, aad: str = "") -> str:
    data = plaintext.encode("utf-8")
    nonce = secrets.token_bytes(16)
    ct = bytes(a ^ b for a, b in zip(data, _stream(_subkey(b"enc"), nonce, len(data))))
    tag = hmac.new(_subkey(b"mac"), aad.encode() + b"|" + nonce + ct, hashlib.sha256).digest()
    return f"{VERSION}${_b64(nonce)}${_b64(ct)}${_b64(tag)}"


def decrypt(token: str, aad: str = "") -> str:
    try:
        version, n, c, t = token.split("$")
        nonce, ct, tag = _unb64(n), _unb64(c), _unb64(t)
    except (ValueError, AttributeError):
        raise SecretError("Formato de secreto inválido") from None
    if version != VERSION:
        raise SecretError("Versión de secreto desconocida")
    expected = hmac.new(_subkey(b"mac"), aad.encode() + b"|" + nonce + ct, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expected):
        raise SecretError("El secreto no es válido (alterado, de otra fila o clave distinta)")
    return bytes(a ^ b for a, b in zip(ct, _stream(_subkey(b"enc"), nonce, len(ct)))).decode("utf-8")


def mask(value: str, keep: int = 4) -> str:
    """Para mostrar en la interfaz: '••••••••abcd'."""
    return ("•" * 8 + value[-keep:]) if len(value) > keep else "•" * len(value)
