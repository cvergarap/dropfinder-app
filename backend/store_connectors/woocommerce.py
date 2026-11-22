"""Conector de WooCommerce (REST API v3 de WooCommerce sobre WordPress). Solo biblioteca estándar.

QUÉ NO SE PUDO VERIFICAR (sin red en el entorno de desarrollo; todo está escrito de memoria contra la documentación
pública de WooCommerce y probado solo contra tests/fake_woocommerce.py). Antes de usarlo en serio, conéctalo a una
TIENDA DE DESARROLLO y revisa estos supuestos:
  1. Base de la API: https://<dominio>/wp-json/wc/v3/… (permalinks "bonitos" activos; con permalinks "simples" la ruta
     sería /?rest_route=/wc/v3/… y NO está soportada). Instalaciones de WordPress en subcarpeta tampoco.
  2. Autenticación: HTTP Basic (Authorization: Basic base64(consumer_key:consumer_secret)) sobre HTTPS. Que el hosting
     no elimine la cabecera Authorization (si la elimina, la respuesta es 401).
  3. GET /wp-json devuelve JSON con `name` (nombre del sitio) y `namespaces` (lista que incluye "wc/v3"); es público.
  4. Moneda: GET /wc/v3/settings/general/woocommerce_currency -> {"value": "USD"} (requiere permiso de gestión de la
     tienda; si no hay permiso, respaldo GET /wc/v3/system_status -> {"settings": {"currency": "USD"}}).
  5. Lectura: GET /wc/v3/products?per_page=1 -> lista JSON.
  6. Búsqueda por SKU: GET /wc/v3/products?sku=<sku>&status=any -> lista; se supone coincidencia EXACTA (el conector
     además filtra por igualdad exacta del campo `sku`). Se supone que `status=any` es válido y que incluye borradores.
  7. Crear: POST /wc/v3/products con name, type "simple", status "draft", sku, regular_price (cadena "19.90"),
     manage_stock true, stock_quantity, description (HTML), images [{src}], tags [{name}] (WooCommerce crea la etiqueta
     si no existe), sale_price (cadena, opcional). Respuesta 201 con `id` entero y `status`.
  8. Valores de `status` aceptados: "draft" y "publish" (también existen "pending", "private"; no se usan).
  9. SKU repetido al crear: HTTP 400 con code `woocommerce_rest_product_sku_already_exists` (ADR 0003); se recuerda que
     versiones reales usan `product_invalid_sku`: el conector trata ambos como señal de adopción.
 10. Actualizar: PUT /wc/v3/products/{id} con regular_price, manage_stock, stock_quantity, sale_price (opcional) y status.
     Un producto en borrador o en la papelera sigue aceptando el PUT. Nunca se envía sale_price vacío ("" borraría la
     oferta que el usuario puso a mano).
 11. Borrar: DELETE /wc/v3/products/{id}?force=true (sin force solo va a la papelera) -> 200 con el producto borrado.
 12. 404 de un producto inexistente: code `woocommerce_rest_product_invalid_id` (se acepta también
     `woocommerce_rest_invalid_id`); 404 de ruta inexistente: code `rest_no_route`. Cualquier otro 404 (HTML, otro
     code) se trata como "no es una tienda WooCommerce con la API activa", NUNCA como producto inexistente.
 13. Cuerpo de error: {"code": "...", "message": "...", "data": {"status": N}}. 401 por clave inválida o sin
     permiso; 403 por permisos; 429 y 5xx dependen del hosting/WAF y pueden venir con HTML y sin Retry-After.
 14. Una clave de solo Lectura se detecta únicamente al escribir (401/403 al publicar): validate() no escribe nada.
 15. WooCommerce descarga las imágenes `src` desde SU servidor (no desde DropFinder); por eso solo se envían https.
 16. Las claves reales son `ck_` / `cs_` + 40 caracteres hexadecimales; la comprobación de forma acepta [A-Za-z0-9]{16,80}.
 17. La redirección (www <-> sin www, http -> https) la rechaza el cliente compartido con un mensaje reconocible por el
     texto «redirección»; si ese texto cambia en http.py, el mensaje pasaría a ser el genérico (sigue siendo un error).

Decisiones de seguridad (ADR 0002/0003): el dominio es LIBRE (cada tienda tiene el suyo), así que la protección
anti-SSRF se reparte: normalize_domain() rechaza IP en cualquier notación, puertos, usuario@, unicode engañoso, rutas
raras y nombres reservados; el cliente compartido (http.py) vuelve a comprobar tras la resolución DNS (IP pública,
fijada, puerto 443, sin redirecciones, sin http). Credenciales solo en la cabecera Authorization (nunca en la URL ni en
el cuerpo), nunca en excepciones, repr ni registros. Las URL salen de plantillas de CLASE (solo los tests las
reemplazan) y de ids numéricos validados.

Idempotencia al publicar: ANTES de crear se busca por SKU y se ADOPTA el producto existente en vez de duplicar; si la
creación responde "SKU repetido" se vuelve a buscar y se adopta. Un producto adoptado nunca se borra en la limpieza
(puede ser anterior). El producto se crea SIEMPRE como borrador y solo se muestra al final si `product.active`.
Si ese último paso falla se intenta borrar lo creado (reintentos acotados) y se relanza el error ORIGINAL; si no se
puede borrar, el error lleva `orphan_external_id` (y el mensaje lo indica).
"""
from __future__ import annotations

import base64
import functools
import html
import ipaddress
import json
import logging
import math
import re
import time
import unicodedata
import urllib.parse
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from . import http as shttp, register, scrub
from .base import (ListingRef, StoreAuthError, StoreConnector, StoreError, StoreInfo, StoreNotFound,
                   StoreProduct, StoreRateLimited, StoreValidationError)

log = logging.getLogger("dropfinder.woocommerce")

# ---------------------------------------------------------------- constantes
TIMEOUT = 20.0
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_IMAGES = 10
MAX_STOCK = 1_000_000_000
MAX_PRICE = Decimal("1000000000")
MAX_RETRY_AFTER = 120.0
DEFAULT_RETRY_AFTER = 5.0
CLEANUP_TRIES = 3
CLEANUP_MAX_SLEEP = 10.0
MAX_ERROR_TEXT = 200
API_PREFIX = "/wp-json/wc/v3"
ROOT_PREFIX = "/wp-json"

SKU_EXISTS_CODES = ("woocommerce_rest_product_sku_already_exists", "product_invalid_sku")
PRODUCT_NOT_FOUND_CODES = ("woocommerce_rest_product_invalid_id", "woocommerce_rest_invalid_id")
ROUTE_MISSING_CODES = ("rest_no_route",)
STATUSES = {"draft", "publish"}

_KEY_RE = re.compile(r"^ck_[A-Za-z0-9]{16,80}$")
_SECRET_RE = re.compile(r"^cs_[A-Za-z0-9]{16,80}$")
_ID_RE = re.compile(r"^[1-9]\d{0,17}$")
_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_TLD_RE = re.compile(r"^[a-z]{2,63}$")          # los TLD IDN reales son no latinos (rechazados de todos modos)
_SCHEME_RE = re.compile(r"^([a-z][a-z0-9+.-]*)://", re.I)
_TAG_RE = re.compile(r"<[^>]*>")
_RESERVED_TLDS = {"localhost", "local", "internal", "localdomain", "lan", "home", "corp", "intranet", "invalid", "test",
                  "example", "onion", "arpa", "private", "box", "router", "gateway", "domain", "wan"}

AUTH_HINT = ("Causas habituales: el hosting elimina la cabecera Authorization (pídele a tu proveedor que la deje pasar), "
             "el dominio no es exactamente el que usa la tienda (con o sin www) o la clave no tiene permisos de "
             "Lectura/Escritura")


# ---------------------------------------------------------------- utilidades de módulo
def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _money(value, what: str = "precio", *, allow_zero: bool = False) -> str:
    """Decimal con 2 decimales. Se cuantiza PRIMERO y se rechaza lo <= 0 (salvo allow_zero), NaN/inf/no numérico y > 1e9."""
    if isinstance(value, bool) or value is None:
        raise StoreValidationError(f"El {what} no es un número válido")
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise StoreValidationError(f"El {what} no es un número válido") from None
    if not d.is_finite():
        raise StoreValidationError(f"El {what} no es un número válido")
    if abs(d) > MAX_PRICE:
        raise StoreValidationError(f"El {what} está fuera de rango")
    d = d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if d < 0 or (d == 0 and not allow_zero):
        raise StoreValidationError(f"El {what} debe ser mayor que cero")
    return f"{d:.2f}"


def _stock(value) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StoreValidationError("El stock debe ser un número entero")
    if isinstance(value, float) and not math.isfinite(value):
        raise StoreValidationError("El stock no es un número válido")
    n = int(value)
    if n < 0 or n > MAX_STOCK:
        raise StoreValidationError(f"El stock debe estar entre 0 y {MAX_STOCK}")
    return n


def _foreign_errors(fn):
    """El conector nunca deja escapar excepciones que no sean StoreError."""
    @functools.wraps(fn)
    def wrapper(*a, **k):
        try:
            return fn(*a, **k)
        except StoreError:
            raise
        except (ValueError, TypeError, OverflowError, ArithmeticError, AttributeError, KeyError):
            raise StoreValidationError("Los datos del producto no son válidos") from None
    return wrapper


def _description_html(text) -> str:
    """Las descripciones de DropFinder son texto plano: se escapa todo; párrafos <p> y saltos simples <br>."""
    t = (text if isinstance(text, str) else "").replace("\r\n", "\n").replace("\r", "\n")[:100000].strip()
    paras = [p.strip() for p in re.split(r"\n\s*\n", t) if p.strip()]
    return "".join("<p>" + html.escape(p, quote=True).replace("\n", "<br>") + "</p>" for p in paras)


def _safe_images(urls) -> list[dict]:
    """Solo https, ASCII, sin IP literal ni usuario, <= 2048 caracteres, máximo 10. Las malas se descartan."""
    out = []
    if isinstance(urls, str):
        urls = [urls]
    for u in urls if isinstance(urls, (list, tuple)) else []:
        if not isinstance(u, str) or len(u) > 2048 or not u.isascii() or any(c.isspace() or ord(c) < 32 for c in u):
            continue
        try:
            p = urllib.parse.urlsplit(u)
            host = p.hostname or ""
            p.port
        except ValueError:
            continue
        if p.scheme != "https" or "." not in host or p.username or p.password:
            continue
        try:
            ipaddress.ip_address(host)
            continue                                         # IP literal
        except ValueError:
            pass
        out.append({"src": u})
        if len(out) >= MAX_IMAGES:
            break
    return out


def _clean_tags(tags) -> list[dict]:
    if isinstance(tags, str):
        tags = [tags]                                        # un str es UNA etiqueta
    out, seen = [], set()
    for t in tags if isinstance(tags, (list, tuple)) else []:
        if isinstance(t, str):
            t = " ".join(t.replace(",", " ").split())[:100]
            if t and t.lower() not in seen:
                seen.add(t.lower())
                out.append({"name": t})
    return out[:50]


def _plain(text, secrets=(), limit: int = MAX_ERROR_TEXT) -> str:
    """Texto de la tienda (puede ser HTML): sin etiquetas, sin entidades, espacios colapsados, acotado y sin secretos."""
    s = _TAG_RE.sub(" ", str(text or "")[:20000])
    s = html.unescape(s)
    s = " ".join(s.split())
    return scrub(s, secrets, limit=limit)


class _Creds(dict):
    """Credenciales que no se muestran en repr/str (volcados de __dict__, depuradores, excepciones)."""

    def __repr__(self) -> str:
        return "<credenciales ocultas>"

    __str__ = __repr__


# ---------------------------------------------------------------- dominio
def _latin_accented(ch: str) -> bool:
    """Letra latina minúscula con tilde/diéresis/ñ (a + marca combinante). Rechaza ı, ſ, ß, ø, æ…, que se parecen a
    otras letras o no se pueden validar contra homógrafos."""
    d = unicodedata.normalize("NFD", ch)
    return len(d) > 1 and "a" <= d[0] <= "z" and all(unicodedata.combining(c) for c in d[1:])


def _label_to_ascii(label: str, bad: StoreValidationError) -> str:
    """Una etiqueta del dominio a ASCII. Unicode solo si son letras LATINAS (con tildes/ñ), NFC y la conversión a
    punycode vuelve exactamente a la misma etiqueta. Cirílico, griego, ancho completo, CJK… se rechazan: no se pueden
    validar contra homógrafos."""
    if label.isascii():
        ascii_label = label
    else:
        if unicodedata.normalize("NFC", label) != label:
            raise bad
        for ch in label:
            if ch.isascii():
                if not (ch.isalnum() or ch == "-"):
                    raise bad
                continue
            if not _latin_accented(ch):
                raise bad
        try:
            ascii_label = label.encode("idna").decode("ascii")
            if ascii_label.encode("ascii").decode("idna") != label:
                raise bad
        except (UnicodeError, ValueError):
            raise bad from None
    if ascii_label.startswith("xn--"):
        try:
            decoded = ascii_label.encode("ascii").decode("idna")
            if decoded.encode("idna").decode("ascii") != ascii_label:
                raise bad
        except (UnicodeError, ValueError):
            raise bad from None
        if decoded.isascii():                              # xn-- de algo que no necesita punycode: truco
            raise bad
        for ch in decoded:                                 # el punycode también debe ser solo latino
            if not ch.isascii() and not _latin_accented(ch):
                raise bad
            if ch.isascii() and not (ch.isalnum() or ch == "-"):
                raise bad
    elif len(ascii_label) >= 4 and ascii_label[2:4] == "--":
        raise bad                                          # "--" en 3.ª y 4.ª posición está reservado para IDNA
    if len(ascii_label) > 63 or not _LABEL_RE.fullmatch(ascii_label):
        raise bad
    return ascii_label


# ---------------------------------------------------------------- conector
@register
class WooCommerceConnector(StoreConnector):
    platform = "woocommerce"
    label = "WooCommerce"
    idempotent_publish = True               # publish() busca por SKU y adopta el producto si ya existe
    credential_fields = [
        {"key": "consumer_key", "label": "Clave de cliente (ck_…)", "secret": True, "required": True,
         "help": "Clave de cliente de la API REST de WooCommerce (empieza con ck_), con permisos de Lectura/Escritura."},
        {"key": "consumer_secret", "label": "Secreto de cliente (cs_…)", "secret": True, "required": True,
         "help": "Secreto de cliente de la misma clave (empieza con cs_). Se muestra una sola vez al crearla."},
    ]
    setup_help = (
        "Conectar WooCommerce (puede cambiar: verifica en la documentación de WooCommerce).\n"
        "1. Dominio: escribe el dominio EXACTO con el que carga tu tienda (por ejemplo www.mitienda.com si así se abre; "
        "mitienda.com si no lleva www). DropFinder no sigue redirecciones por seguridad: si tu sitio redirige de un "
        "dominio a otro, la conexión falla. Solo se admiten tiendas con HTTPS estándar (sin puerto).\n"
        "2. En el panel de WordPress: WooCommerce → Ajustes → Avanzado → API REST → Añadir clave.\n"
        "3. Descripción: DropFinder. Usuario: un administrador. Permisos: Lectura/Escritura. Pulsa «Generar clave de API».\n"
        "4. Copia la Clave de cliente (ck_…) y el Secreto de cliente (cs_…): el secreto se muestra una sola vez.\n"
        "5. La tienda debe usar HTTPS y enlaces permanentes «bonitos» (Ajustes → Enlaces permanentes, no «Simple»).\n"
        "6. Si al conectar ves un error 401 aunque las claves sean correctas, probablemente tu hosting elimina la "
        "cabecera Authorization: pide a tu proveedor que la deje pasar (en Apache suele resolverse con una regla en "
        ".htaccess; en Cloudflare o un firewall de WordPress, revisa que no bloqueen /wp-json).\n"
        "7. Los productos se crean como borrador. Prueba primero en una tienda de desarrollo."
    )

    # Plantilla de URL: atributo de CLASE (solo los tests la reemplazan; no viene de datos del usuario).
    BASE_URL_TEMPLATE = "https://{host}"
    TIMEOUT = TIMEOUT
    MAX_BYTES = MAX_RESPONSE_BYTES

    def __init__(self, domain: str, credentials: dict[str, str], settings: dict | None = None):
        super().__init__(domain, _Creds(credentials or {}), settings)

    def __repr__(self) -> str:
        return f"<WooCommerceConnector {self.domain}>"          # nunca muestra credenciales

    # ------------------------------------------------------------ dominio y credenciales
    @classmethod
    def normalize_domain(cls, raw: str) -> str:
        bad = StoreValidationError("Dominio inválido: escribe solo el dominio de la tienda, por ejemplo www.mitienda.com "
                                   "(sin IP, usuario@, rutas raras ni caracteres especiales)")
        if not isinstance(raw, str):
            raise bad
        s = raw.strip()
        if not s or len(s) > 600:
            raise bad
        if any(c.isspace() or ord(c) < 32 or ord(c) == 127 or c in "\\%" or unicodedata.category(c) in ("Cf", "Cc", "Co", "Cs", "Cn")
               for c in s):
            raise bad
        m = _SCHEME_RE.match(s)
        if m:
            if m.group(1).lower() not in ("https", "http"):
                raise bad
            s = s[m.end():]
        authority = re.split(r"[/?#]", s, maxsplit=1)[0]
        if not authority or "@" in s or "[" in authority or "]" in authority:
            raise bad
        host = authority
        if ":" in authority:
            host, _, port = authority.rpartition(":")
            if ":" in host or not port.isascii() or not port.isdigit():
                raise bad
            if int(port) != 443:
                raise StoreValidationError("Solo se admiten tiendas con HTTPS estándar (puerto 443): quita el puerto del dominio")
        if any(not c.isascii() and c.lower().isascii() for c in host):        # K (Kelvin) -> k, etc.: lookalike
            raise bad
        host = host.lower()
        if not host or len(host) > 253 or host.startswith(".") or host.endswith(".") or ".." in host:
            raise bad
        labels = [_label_to_ascii(lb, bad) for lb in host.split(".")]
        ascii_host = ".".join(labels)
        if len(labels) < 2 or len(ascii_host) > 253:
            raise bad
        tld = labels[-1]
        if not _TLD_RE.fullmatch(tld) or tld in _RESERVED_TLDS:      # un TLD real no es numérico: bloquea 127.1, 0x7f.1, 2130706433
            raise bad
        try:
            ipaddress.ip_address(ascii_host)
        except ValueError:
            pass
        else:
            raise bad
        return ascii_host

    @classmethod
    def check_credentials(cls, credentials: dict[str, str]) -> None:
        c = credentials or {}
        k, s = c.get("consumer_key"), c.get("consumer_secret")
        k = k.strip() if isinstance(k, str) else ""
        s = s.strip() if isinstance(s, str) else ""
        if not k or not s:
            raise StoreValidationError("Faltan la clave de cliente (ck_…) y el secreto de cliente (cs_…)")
        if not _KEY_RE.fullmatch(k):
            raise StoreValidationError("La clave de cliente no tiene el formato esperado (empieza con ck_, solo letras y números)")
        if not _SECRET_RE.fullmatch(s):
            raise StoreValidationError("El secreto de cliente no tiene el formato esperado (empieza con cs_, solo letras y números)")

    # ------------------------------------------------------------ HTTP
    def _host(self) -> str:
        return self.normalize_domain(self.domain)

    def _auth_header(self) -> str:
        c = self.credentials
        k, s = str(c.get("consumer_key", "")).strip(), str(c.get("consumer_secret", "")).strip()
        return "Basic " + base64.b64encode(f"{k}:{s}".encode("utf-8")).decode("ascii")

    def _secrets(self) -> list[str]:
        out = [str(v).strip() for k in ("consumer_key", "consumer_secret") if (v := self.credentials.get(k))]
        if out:
            header = self._auth_header()
            out += [header, header[len("Basic "):]]
        return out

    def _redact(self, text) -> str:
        return scrub(text, self._secrets())

    def _text(self, text) -> str:
        return _plain(text, self._secrets())

    def _send(self, method: str, prefix: str, path: str, query: dict | None, payload):
        url = self.BASE_URL_TEMPLATE.format(host=self._host()) + prefix + path
        if query:
            url += "?" + urllib.parse.urlencode(query, quote_via=urllib.parse.quote)
        try:
            return shttp.request_json(method, url, payload, headers={"Authorization": self._auth_header()},
                                      timeout=self.TIMEOUT, max_bytes=self.MAX_BYTES)
        except StoreError as e:
            msg = str(e)
            if "redirecci" in msg.lower():
                raise StoreValidationError(
                    "La tienda redirige a otra dirección (por ejemplo de www a sin www o de http a https), y por "
                    "seguridad no se siguen redirecciones. Escribe el dominio EXACTO con el que carga tu tienda.") from None
            raise StoreError(self._text(msg), retryable=e.retryable, not_sent=e.not_sent) from None

    def _call(self, method: str, path: str, *, query: dict | None = None, payload=None, ref_op: bool = False,
              root: bool = False):
        """Una llamada a la API. Devuelve el JSON (dict/list). Todo fallo sale como StoreError tipado."""
        resp = self._send(method, ROOT_PREFIX if root else API_PREFIX, path, query, payload)
        data, parsed = None, False
        body = resp.body or b""
        if body.strip():
            try:
                data, parsed = json.loads(body.decode("utf-8")), True
            except (ValueError, UnicodeDecodeError):
                pass
        if resp.status >= 400:
            raise self._http_error(resp, data if parsed else None, ref_op)
        if not parsed:
            head = body.lstrip()[:1]
            if head in (b"{", b"["):
                raise StoreError("La tienda devolvió una respuesta JSON incompleta o dañada", retryable=True)
            raise StoreValidationError(
                "La tienda no respondió con JSON: parece una página de inicio de sesión de WordPress, un bloqueo de un "
                "firewall/Cloudflare o una dirección que no es la API de WooCommerce. Revisa el dominio exacto y que "
                "/wp-json no esté bloqueado")
        return data

    @staticmethod
    def _retry_after(hdrs: dict) -> float:
        try:
            v = float(hdrs.get("retry-after", DEFAULT_RETRY_AFTER))
            if not math.isfinite(v):
                return DEFAULT_RETRY_AFTER
            return max(1.0, min(v, MAX_RETRY_AFTER))
        except (TypeError, ValueError):
            return DEFAULT_RETRY_AFTER

    def _http_error(self, resp, data, ref_op: bool) -> StoreError:
        st = resp.status
        code = ""
        if isinstance(data, dict):
            code = str(data.get("code") or "")[:100]
            detail = self._text(data.get("message", ""))
        else:
            detail = self._text(resp.body.decode("utf-8", "replace") if resp.body else "")
        if not re.fullmatch(r"[A-Za-z0-9_.\-]{1,100}", code):       # el code solo se compara: nunca se muestra ni se confía
            code = ""
        extra = f" Detalle de la tienda: {detail}" if detail else ""
        if st in (401, 403):
            return StoreAuthError(f"WooCommerce rechazó las credenciales o los permisos (HTTP {st}). {AUTH_HINT}.{extra}")
        if st == 404:
            if code in PRODUCT_NOT_FOUND_CODES and ref_op:
                return StoreNotFound("El producto ya no existe en la tienda")
            # Ruta inexistente, HTML o cualquier otro 404: jamás StoreNotFound (un delete() lo tomaría por éxito)
            e = StoreValidationError("No parece una tienda WooCommerce con la API REST activa (404 en /wp-json/wc/v3): "
                                     "revisa el dominio exacto, que WooCommerce esté activo y los enlaces permanentes "
                                     "(no «Simple»)." + extra)
            e.wc_code = code
            return e
        if st == 429:
            return StoreRateLimited("La tienda pidió esperar (demasiadas solicitudes)", self._retry_after(resp.headers))
        if st >= 500:
            return StoreError(f"La tienda tuvo un error temporal (HTTP {st}).{extra}", retryable=True)
        if st == 408:
            return StoreError("La tienda tardó demasiado en responder (HTTP 408)", retryable=True)
        if st in (400, 409, 422):
            e = StoreValidationError(f"WooCommerce rechazó los datos (HTTP {st}).{extra}")
            e.wc_code = code
            return e
        return StoreError(f"La tienda rechazó la solicitud (HTTP {st}).{extra}", retryable=False)

    # ------------------------------------------------------------ contrato
    @_foreign_errors
    def validate(self) -> StoreInfo:
        host = self._host()
        root = self._call("GET", "", root=True)
        if not isinstance(root, dict):
            raise StoreValidationError("No parece una tienda WordPress con la API REST activa (/wp-json no devolvió un objeto)")
        ns = root.get("namespaces")
        if isinstance(ns, list) and "wc/v3" not in ns:
            raise StoreValidationError("No parece una tienda WooCommerce con la API REST activa: falta el espacio wc/v3 "
                                       "(¿WooCommerce está desactivado?)")
        name = root.get("name")
        name = self._text(name)[:80] if isinstance(name, str) and name.strip() else host
        prods = self._call("GET", "/products", query={"per_page": 1})
        if not isinstance(prods, list):
            raise StoreValidationError("No parece una tienda WooCommerce con la API REST activa (la lista de productos no es válida)")
        return StoreInfo(name=name or host, domain=host, currency=self._currency(), scopes=[], missing_scopes=[])

    def _currency(self) -> str:
        """Moneda de la tienda: ajuste general; respaldo system_status si no hay permiso/ruta para el ajuste."""
        cur = None
        try:
            data = self._call("GET", "/settings/general/woocommerce_currency")
            cur = data.get("value") if isinstance(data, dict) else None
        except StoreAuthError:
            cur = None
        except StoreValidationError as e:
            if not hasattr(e, "wc_code"):                    # HTML, no JSON, redirección…: no es "falta de permiso/ruta"
                raise
            cur = None
        if not (isinstance(cur, str) and re.fullmatch(r"[A-Za-z]{3}", cur.strip())):
            try:
                data = self._call("GET", "/system_status")
                cur = ((data.get("settings") or {}).get("currency")) if isinstance(data, dict) else None
            except StoreAuthError:
                cur = None
            except StoreValidationError as e:
                if not hasattr(e, "wc_code"):
                    raise
                cur = None
        if not (isinstance(cur, str) and re.fullmatch(r"[A-Za-z]{3}", cur.strip())):
            raise StoreValidationError("No se pudo leer la moneda de la tienda: la clave necesita permiso para ver los "
                                       "ajustes de WooCommerce (usa una clave de un administrador con Lectura/Escritura)")
        return cur.strip().upper()

    def _admin_url(self, pid: str) -> str:
        return f"https://{self._host()}/wp-admin/post.php?post={pid}&action=edit"

    @staticmethod
    def _pid(data) -> str:
        pid = data.get("id") if isinstance(data, dict) else None
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            raise StoreError("La tienda no devolvió el id del producto creado", retryable=True)
        return str(pid)

    def _find_by_sku(self, sku: str) -> dict | None:
        data = self._call("GET", "/products", query={"sku": sku, "status": "any"})
        if not isinstance(data, list):
            raise StoreError("Respuesta inesperada de la tienda al buscar por SKU", retryable=True)
        found = [d for d in data if isinstance(d, dict) and d.get("sku") == sku and d.get("type") != "variation"
                 and isinstance(d.get("id"), int) and not isinstance(d.get("id"), bool) and d["id"] > 0]
        return min(found, key=lambda d: d["id"]) if found else None

    @staticmethod
    def _sale(price: str, compare) -> str | None:
        """sale_price solo si se pasó y es MENOR que el precio. Nunca una cadena vacía."""
        if compare is None:
            return None
        c = _money(compare, "precio de comparación")
        return c if Decimal(c) < Decimal(price) else None

    @_foreign_errors
    def publish(self, product: StoreProduct) -> ListingRef:
        # 1. Todo se valida ANTES de tocar la tienda
        title = str(product.title or "").strip()[:255]
        if not title:
            raise StoreValidationError("El producto necesita un título")
        sku = product.sku
        if not isinstance(sku, str) or not sku.strip() or sku != sku.strip() or len(sku) > 255 \
                or any(ord(c) < 32 or ord(c) == 127 for c in sku):
            raise StoreValidationError("El producto necesita un SKU válido (sin espacios al borde ni caracteres de control, "
                                       "máximo 255)")
        price = _money(product.price)
        sale = self._sale(price, product.compare_at_price)
        stock = _stock(product.stock)
        body = {"name": title, "type": "simple", "status": "draft", "sku": sku, "regular_price": price,
                "manage_stock": True, "stock_quantity": stock, "description": _description_html(product.description)}
        if sale is not None:
            body["sale_price"] = sale
        images = _safe_images(product.image_urls)
        if images:
            body["images"] = images
        tags = _clean_tags(product.tags)
        if tags:
            body["tags"] = tags

        # 2. Adoptar un producto ya creado por un intento anterior, o crearlo
        existing = self._find_by_sku(sku)
        adopted = existing is not None
        if adopted:
            pid, status = str(existing["id"]), existing.get("status")
        else:
            try:
                created = self._call("POST", "/products", payload=body)
            except StoreValidationError as e:
                if getattr(e, "wc_code", "") not in SKU_EXISTS_CODES:
                    raise
                existing = self._find_by_sku(sku)        # SKU repetido: alguien (un intento perdido) lo creó -> adoptar
                if existing is None:
                    raise StoreValidationError("El SKU ya está en uso en la tienda por otro producto (¿en la papelera o "
                                               "como variación?): libéralo o cambia el SKU") from None
                adopted, pid, status = True, str(existing["id"]), existing.get("status")
            else:
                pid, status = self._pid(created), created.get("status") if isinstance(created, dict) else None
        try:
            if adopted:                              # lo adoptado puede venir de otro precio/stock: se deja como se pide
                upd = {"regular_price": price, "manage_stock": True, "stock_quantity": stock}
                if sale is not None:
                    upd["sale_price"] = sale
                self._call("PUT", f"/products/{pid}", payload=upd, ref_op=True)
            if product.active and status != "publish":
                self._call("PUT", f"/products/{pid}", payload={"status": "publish"}, ref_op=True)
        except StoreError as e:
            if not adopted:
                self._cleanup_orphan(pid, e)         # solo se borra lo que creó esta llamada
            if isinstance(e, StoreNotFound):
                raise StoreError("El producto desapareció de la tienda durante la publicación", retryable=True) from None
            raise
        return ListingRef(external_id=pid, url=self._admin_url(pid))

    def _cleanup_orphan(self, pid: str, original: StoreError) -> None:
        """Borra el producto a medio crear; ante límite de velocidad o error temporal espera y reintenta (acotado)."""
        for attempt in range(CLEANUP_TRIES):
            try:
                self._call("DELETE", f"/products/{pid}", query={"force": "true"}, ref_op=True)
                return
            except StoreNotFound:
                return
            except StoreError as e:
                if not e.retryable or attempt == CLEANUP_TRIES - 1:
                    break
                _sleep(min(float(getattr(e, "retry_after", 1.0)), CLEANUP_MAX_SLEEP))
            except Exception:
                break
        original.orphan_external_id = pid
        original.args = (f"{original.args[0] if original.args else original}. "
                         f"Además no se pudo borrar el borrador incompleto ({pid}); el próximo intento lo reutiliza "
                         "(o bórralo a mano en WooCommerce)",)
        log.warning("woocommerce: producto huérfano %s en %s", pid, self._host())

    @staticmethod
    def _check_ref(ref: ListingRef) -> str:
        pid = ref.external_id if isinstance(ref.external_id, str) else ""
        if not _ID_RE.fullmatch(pid):
            raise StoreValidationError("Identificador de producto de WooCommerce inválido")
        return pid

    @_foreign_errors
    def update_price_stock(self, ref: ListingRef, price: float, stock: int,
                           compare_at_price: float | None = None) -> None:
        pid = self._check_ref(ref)
        p = _money(price)
        n = _stock(stock)
        body = {"regular_price": p, "manage_stock": True, "stock_quantity": n}
        sale = self._sale(p, compare_at_price)           # None = no tocar la oferta que haya puesto el usuario
        if sale is not None:
            body["sale_price"] = sale
        self._call("PUT", f"/products/{pid}", payload=body, ref_op=True)

    @_foreign_errors
    def set_active(self, ref: ListingRef, active: bool) -> None:
        pid = self._check_ref(ref)
        self._call("PUT", f"/products/{pid}", payload={"status": "publish" if active else "draft"}, ref_op=True)

    @_foreign_errors
    def delete(self, ref: ListingRef) -> None:
        pid = self._check_ref(ref)
        try:
            self._call("DELETE", f"/products/{pid}", query={"force": "true"}, ref_op=True)
        except StoreNotFound:
            return
