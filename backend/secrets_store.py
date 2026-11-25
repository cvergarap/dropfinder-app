"""Almacén de secretos: cifra credenciales de tiendas en reposo (ver docs/adr/0002-integracion-tiendas.md).

Esquema (solo biblioteca estándar): flujo de claves HMAC-SHA256 en modo contador + etiqueta HMAC-SHA256
(cifrar y luego autenticar), nonce aleatorio de 16 bytes y claves de cifrado/autenticación derivadas por
separado de la clave maestra. La etiqueta cubre `versión | len(aad) | aad | len(nonce) | nonce | texto cifrado`
con longitudes explícitas, así que ninguna reubicación de bytes entre campos conserva una etiqueta válida.
`aad` (datos asociados, p. ej. "user:3|shopify|mitienda.myshopify.com") ata el valor a su fila.

Dónde vive la clave maestra (`dropfinder.key`): por defecto en el directorio de datos del USUARIO del sistema
(Windows: %LOCALAPPDATA%\\DropFinder, Linux/macOS: ~/.local/share/dropfinder), NUNCA dentro del repositorio ni
junto a la base de datos, para que copiar la carpeta del proyecto o un respaldo de la base no copie también la
clave. Se puede cambiar con DROPFINDER_KEY_FILE. Si se pierde, las credenciales guardadas son irrecuperables:
hay que volver a conectar las tiendas (no se genera una clave nueva en silencio si ya hay credenciales).

Protege la base de datos copiada SIN el archivo de clave. No protege contra quien lea también `dropfinder.key`
(ni contra malware con acceso a la cuenta del usuario).
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import secrets
import tempfile
import threading

import db

VERSION = "v1"
NONCE_BYTES = 16
_lock = threading.Lock()
_cache: dict[str, bytes] = {}          # ruta de clave -> clave maestra


class SecretError(Exception):
    """Valor cifrado alterado, de otra fila, o clave maestra ausente/inválida."""


def data_dir() -> str:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
        return os.path.join(base, "DropFinder")
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "dropfinder")


def key_path() -> str:
    return os.environ.get("DROPFINDER_KEY_FILE") or os.path.join(data_dir(), "dropfinder.key")


def _has_encrypted_data() -> bool:
    """¿Hay credenciales ya cifradas (tiendas o proveedores)? Entonces una clave ausente es una pérdida, no un primer
    arranque."""
    try:
        with db.connect() as conn:
            for table in ("store_connections", "supplier_connections"):
                if (conn.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (table,)).fetchone()
                        and conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()):
                    return True
            return False
    except Exception:  # noqa: BLE001 — sin base legible no se puede afirmar nada
        return False


def _read_key(path: str) -> bytes:
    with open(path, "rb") as f:
        raw = f.read().strip()
    try:
        key = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        raise SecretError("El archivo de clave de cifrado está dañado. Vuelve a conectar tus tiendas.") from None
    if len(key) != 32:
        raise SecretError("El archivo de clave de cifrado es inválido. Vuelve a conectar tus tiendas.")
    return key


def _create_key(path: str) -> bytes:
    """Crea la clave de forma atómica: se escribe completa en un temporal y se enlaza al nombre final solo si
    no existe (otro proceso que gane la carrera deja su clave; la leemos en vez de pisarla)."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, mode=0o700, exist_ok=True)
    key = secrets.token_bytes(32)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".df-key-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(base64.b64encode(key))
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass                                  # Windows: los permisos los da la carpeta del perfil del usuario
        try:
            os.link(tmp, path)                    # falla si ya existe: nunca pisa una clave
        except FileExistsError:
            return _read_key(path)
        except OSError:                           # sistemas de archivos sin enlaces duros
            if os.path.exists(path):
                return _read_key(path)
            os.replace(tmp, path)
            tmp = ""
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)
    return key


def _master() -> bytes:
    path = key_path()
    with _lock:
        if path in _cache:
            return _cache[path]
        if os.path.exists(path):
            key = _read_key(path)
        elif _has_encrypted_data():
            raise SecretError("Falta el archivo de clave de cifrado (dropfinder.key). Las credenciales guardadas "
                              "no se pueden recuperar: vuelve a conectar tus tiendas.")
        else:
            key = _create_key(path)
        _cache[path] = key
        return key


def forget_cache() -> None:
    """Pruebas: olvida las claves leídas (cambiar de base de datos o de archivo de clave)."""
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


def _mac(aad: bytes, nonce: bytes, ct: bytes) -> bytes:
    framed = (VERSION.encode() + len(aad).to_bytes(4, "big") + aad + len(nonce).to_bytes(1, "big") + nonce + ct)
    return hmac.new(_subkey(b"mac"), framed, hashlib.sha256).digest()


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    if not s or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for c in s):
        raise SecretError("Formato de secreto inválido")
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def encrypt(plaintext: str, aad: str = "") -> str:
    data = plaintext.encode("utf-8")
    nonce = secrets.token_bytes(NONCE_BYTES)
    ct = bytes(a ^ b for a, b in zip(data, _stream(_subkey(b"enc"), nonce, len(data))))
    return f"{VERSION}${_b64(nonce)}${_b64(ct)}${_b64(_mac(aad.encode(), nonce, ct))}"


def decrypt(token: str, aad: str = "") -> str:
    try:
        version, n, c, t = token.split("$")
        nonce, tag = _unb64(n), _unb64(t)
        ct = _unb64(c) if c else b""
    except (ValueError, AttributeError):
        raise SecretError("Formato de secreto inválido") from None
    if version != VERSION or len(nonce) != NONCE_BYTES:
        raise SecretError("Formato de secreto inválido")
    if not hmac.compare_digest(tag, _mac(aad.encode(), nonce, ct)):
        raise SecretError("El secreto no es válido (alterado, de otra fila o clave distinta)")
    try:
        return bytes(a ^ b for a, b in zip(ct, _stream(_subkey(b"enc"), nonce, len(ct)))).decode("utf-8")
    except UnicodeDecodeError:
        raise SecretError("El secreto no es válido") from None


def mask(value: str, keep: int = 4) -> str:
    """Para mostrar en la interfaz: '••••••••abcd'. Un secreto corto no revela nada."""
    if len(value) < keep * 3:
        return "•" * 8
    return "•" * 8 + value[-keep:]
