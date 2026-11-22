"""Conector de Shopify (Admin GraphQL API). Solo biblioteca estándar.

QUÉ NO SE PUDO VERIFICAR (sin red en el entorno de desarrollo; todo está escrito de memoria contra la documentación
pública de Shopify y probado solo contra tests/fake_shopify.py). Antes de usarlo en serio, conéctalo a una TIENDA DE
DESARROLLO y revisa estos supuestos:
  1. Versión de API (API_VERSION_DEFAULT) vigente y con estas mutaciones: productCreate(product:, media:),
     productVariantsBulkUpdate (con inventoryItem {sku, cost, tracked}), inventorySetQuantities
     (ignoreCompareQuantity), inventoryActivate, productUpdate(product:), productDelete(input:).
  2. Concesión "client credentials" (Dev Dashboard): POST https://{tienda}/admin/oauth/access_token con cuerpo
     FORM-ENCODED (application/x-www-form-urlencoded: client_id=…&client_secret=…&grant_type=client_credentials,
     Accept: application/json) -> {access_token, scope, expires_in}. El formato del cuerpo (form vs JSON) es lo que
     recuerda quien revisó la documentación: SIN VERIFICAR.
  3. Consulta de validación: shop{name myshopifyDomain currencyCode}, currentAppInstallation{accessScopes{handle}},
     locations(first:N){nodes{id name isActive}}.
  4. Códigos de error: HTTP 401 por token inválido/caducado; ACCESS_DENIED por scope faltante; THROTTLED con
     extensions.cost.throttleStatus; userErrors con "Product does not exist" para productos borrados; código
     ITEM_NOT_STOCKED_AT_LOCATION cuando el artículo no está activado en la ubicación.
  5. Que el producto recién creado trae una variante por defecto y que su artículo de inventario queda activado en
     la ubicación principal (si no, se usa inventoryActivate como respaldo).
  6. Que write_X implica read_X al calcular los permisos que faltan.
  7. DIRECTIVA @idempotent (BAJA confianza, SIN VERIFICAR): se recuerda que desde la API 2026-04 las mutaciones de
     inventario exigen `@idempotent(key: …)`. `_needs_idempotency(version)` la agrega (clave uuid4 por operación,
     en la variable $key; el reintento tras un 401 reutiliza las mismas variables y por tanto la misma clave) a
     inventorySetQuantities e inventoryActivate cuando la versión es >= 2026-04. Si Shopify devuelve un error de
     directiva en la primera conexión, ajusta API_VERSION a 2026-01 (DROPFINDER_SHOPIFY_API_VERSION) o revisa la
     documentación. La sintaxis exacta (variable vs literal) también es supuesto.
  8. Que products(first:, query: "tag:'…'") encuentra por etiqueta (se usa para publicar de forma idempotente).
  9. Que los textos de la guía (setup_help) sobre el Dev Dashboard / apps personalizadas siguen vigentes.

Decisiones de seguridad (ADR 0002): dominio estricto <tienda>.myshopify.com, solo HTTPS, sin seguir redirecciones,
verificación TLS siempre activa, tiempo máximo, tope de tamaño de respuesta, URL fija (plantillas de CLASE que solo
los tests reemplazan; nunca salen de datos del usuario ni del entorno), y ningún secreto en excepciones ni logs.

Idempotencia al publicar: cada producto creado lleva la etiqueta determinista `df-sku:<sku seguro>` (el SKU solo si
es [A-Za-z0-9_-], si no un hash sha256) y ANTES de crear se busca por esa etiqueta; si existe se ADOPTA (se continúa
con variante, inventario y estado sobre él) en vez de duplicar. Eso cubre la respuesta perdida tras un create
exitoso y los borradores huérfanos cuando el borrado de limpieza no pudo hacerse. Un producto adoptado nunca se
borra en la limpieza (puede ser anterior). `read_products` se pide como permiso (write_X implica read_X).

Orfandad al publicar: productCreate -> productVariantsBulkUpdate -> inventorySetQuantities (-> productUpdate ACTIVE).
El producto se crea SIEMPRE como borrador y solo se activa al final, para que nunca esté visible a precio 0.
Si un paso posterior falla se intenta productDelete y se relanza el error ORIGINAL (el motor no recibe ListingRef,
así que no queda un anuncio fantasma). Si el borrado también falla, el error lleva el atributo
`orphan_external_id` (y el mensaje lo indica) para que se pueda limpiar a mano.
"""
from __future__ import annotations

import functools
import hashlib
import ipaddress
import html
import json
import logging
import math
import os
import re
import threading
import time
import urllib.parse
import uuid
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from . import http as shttp, register, scrub
from .base import (ListingRef, StoreAuthError, StoreConnector, StoreError, StoreInfo, StoreNotFound,
                   StoreProduct, StoreRateLimited, StoreValidationError)

log = logging.getLogger("dropfinder.shopify")

# ---------------------------------------------------------------- constantes (todo lo que depende de la plataforma)
API_VERSION_DEFAULT = "2026-04"                 # SIN VERIFICAR; sobreescribible con DROPFINDER_SHOPIFY_API_VERSION
API_VERSION_ENV = "DROPFINDER_SHOPIFY_API_VERSION"
API_VERSION_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
SHOP_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,60}\.myshopify\.com$")
REQUIRED_SCOPES = ("read_products", "write_products", "write_inventory", "read_locations")
TOKEN_HEADER = "X-Shopify-Access-Token"
USER_AGENT = "DropFinder/1.0"
TIMEOUT = 20.0                                  # segundos por operación de socket
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_IMAGES = 10
TOKEN_EXPIRY_MARGIN = 300.0                     # se renueva el token 5 min antes de que caduque
DEFAULT_TOKEN_TTL = 86399.0
LOW_COST_THRESHOLD = 100                        # si queda menos costo disponible, se espera un poco
MAX_PACE_SLEEP = 3.0
LOCATION_TTL = 600.0
MAX_RETRY_AFTER = 120.0
MAX_THROTTLE_WAIT = 60.0
CLEANUP_TRIES = 3
CLEANUP_MAX_SLEEP = 10.0
MAX_TOKEN_ENTRIES = 200
MAX_STOCK = 1_000_000_000
MAX_PRICE = Decimal("1000000000")
IDEMPOTENCY_FROM = (2026, 4)                    # SIN VERIFICAR (ver docstring)
SKU_TAG_PREFIX = "df-sku:"

_GID_PRODUCT_RE = re.compile(r"^gid://shopify/Product/(\d{1,20})$")
_LOCATION_GID_RE = re.compile(r"^gid://shopify/Location/\d{1,20}$")
_SAFE_SKU_RE = re.compile(r"^[A-Za-z0-9_-]{1,60}$")
_GID_RE = re.compile(r"^gid://shopify/[A-Za-z]+/\d{1,20}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.\-]{8,500}$")
_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]{6,200}$")
_NOT_FOUND_RE = re.compile(r"does not exist|doesn't exist|not found|could not be found|no longer exists", re.I)

# ---------------------------------------------------------------- consultas
Q_VALIDATE = """query DropFinderValidate {
  shop { name myshopifyDomain currencyCode }
  currentAppInstallation { accessScopes { handle } }
  locations(first: 10) { nodes { id name isActive } }
}"""
Q_LOCATIONS = """query DropFinderLocations { locations(first: 10) { nodes { id name isActive } } }"""
Q_FIND_BY_TAG = """query DropFinderFindByTag($q: String!) {
  products(first: 5, query: $q) {
    nodes { id status tags variants(first: 1) { nodes { id inventoryItem { id } } } }
  }
}"""
M_PRODUCT_CREATE = """mutation ProductCreate($product: ProductCreateInput!, $media: [CreateMediaInput!]) {
  productCreate(product: $product, media: $media) {
    product { id variants(first: 1) { nodes { id inventoryItem { id } } } }
    userErrors { field message }
  }
}"""
M_VARIANTS_UPDATE = """mutation VariantsBulkUpdate($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
  productVariantsBulkUpdate(productId: $productId, variants: $variants) {
    productVariants { id inventoryItem { id } }
    userErrors { field message code }
  }
}"""
M_PRODUCT_UPDATE = """mutation ProductUpdate($product: ProductUpdateInput!) {
  productUpdate(product: $product) { product { id status } userErrors { field message } }
}"""
M_PRODUCT_DELETE = """mutation ProductDelete($input: ProductDeleteInput!) {
  productDelete(input: $input) { deletedProductId userErrors { field message } }
}"""


def _needs_idempotency(api_version: str) -> bool:
    """True si la versión exige @idempotent en las mutaciones de inventario (SUPUESTO sin verificar: >= 2026-04)."""
    try:
        y, m = api_version.split("-")
        return (int(y), int(m)) >= IDEMPOTENCY_FROM
    except (ValueError, AttributeError):
        return False


def _inventory_mutation(kind: str, idem: bool) -> str:
    key_decl = ", $key: String!" if idem else ""
    directive = " @idempotent(key: $key)" if idem else ""
    if kind == "set":
        return ("mutation InventorySet($input: InventorySetQuantitiesInput!%s) {\n"
                "  inventorySetQuantities(input: $input)%s {\n    inventoryAdjustmentGroup { id }\n"
                "    userErrors { field message code }\n  }\n}" % (key_decl, directive))
    return ("mutation InventoryActivate($inventoryItemId: ID!, $locationId: ID!%s) {\n"
            "  inventoryActivate(inventoryItemId: $inventoryItemId, locationId: $locationId)%s {\n"
            "    inventoryLevel { id }\n    userErrors { field message }\n  }\n}" % (key_decl, directive))


# ---------------------------------------------------------------- utilidades de módulo
def _now() -> float:
    return time.time()


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _api_version() -> str:
    v = os.environ.get(API_VERSION_ENV)
    if v is None or not v.strip():
        return API_VERSION_DEFAULT
    v = v.strip()
    if not API_VERSION_RE.fullmatch(v):
        raise StoreValidationError(f"{API_VERSION_ENV} no es válida (formato AAAA-MM)")
    return v


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
        except (ValueError, TypeError, OverflowError, ArithmeticError, AttributeError):
            raise StoreValidationError("Los datos del producto no son válidos") from None
    return wrapper


def _description_html(text) -> str:
    """Las descripciones de DropFinder son texto plano: se escapa todo y los saltos de línea pasan a <br>."""
    t = (text if isinstance(text, str) else "").replace("\r\n", "\n").replace("\r", "\n")[:100000]
    return html.escape(t, quote=True).replace("\n", "<br>")


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
        out.append({"originalSource": u, "mediaContentType": "IMAGE"})
        if len(out) >= MAX_IMAGES:
            break
    return out


def _clean_tags(tags) -> list[str]:
    if isinstance(tags, str):
        tags = [tags]                                        # un str es UNA etiqueta
    out = []
    for t in tags if isinstance(tags, (list, tuple)) else []:
        if isinstance(t, str):
            t = t.replace(",", " ").strip()[:255]
            if t:
                out.append(t)
    return out[:249]


def _sku_tag(sku: str) -> str:
    """Etiqueta determinista y segura (solo [A-Za-z0-9_-]) para encontrar el producto por SKU."""
    slug = sku if _SAFE_SKU_RE.fullmatch(sku) else "sha-" + hashlib.sha256(sku.encode("utf-8", "replace")).hexdigest()[:32]
    return SKU_TAG_PREFIX + slug


# ---------------------------------------------------------------- cachés (por proceso)
_TOKEN_LOCK = threading.Lock()
_TOKEN_CACHE: dict[tuple, tuple[str, float]] = {}
_FETCH_LOCKS: dict[tuple, threading.Lock] = {}
_LOC_CACHE: dict[tuple, tuple[str, float]] = {}


def clear_caches() -> None:
    """Vacía tokens y ubicaciones en memoria (para pruebas)."""
    with _TOKEN_LOCK:
        _TOKEN_CACHE.clear()
        _FETCH_LOCKS.clear()
        _LOC_CACHE.clear()


def _sweep_locked() -> None:
    """Poda entradas caducadas y limita el tamaño. Llamar con _TOKEN_LOCK tomado."""
    now = _now()
    for k in [k for k, (_, exp) in _TOKEN_CACHE.items() if exp <= now]:
        del _TOKEN_CACHE[k]
    for k in [k for k, (_, exp) in _LOC_CACHE.items() if exp <= now]:
        del _LOC_CACHE[k]
    while len(_TOKEN_CACHE) > MAX_TOKEN_ENTRIES:
        del _TOKEN_CACHE[min(_TOKEN_CACHE, key=lambda k: _TOKEN_CACHE[k][1])]
    while len(_LOC_CACHE) > MAX_TOKEN_ENTRIES:
        del _LOC_CACHE[min(_LOC_CACHE, key=lambda k: _LOC_CACHE[k][1])]
    if len(_FETCH_LOCKS) > MAX_TOKEN_ENTRIES:
        for k in [k for k, lk in _FETCH_LOCKS.items() if k not in _TOKEN_CACHE and not lk.locked()]:
            del _FETCH_LOCKS[k]


@register
class ShopifyConnector(StoreConnector):
    platform = "shopify"
    label = "Shopify"
    credential_fields = [
        {"key": "access_token", "label": "Token de acceso de la API Admin (shpat_…)", "secret": True, "required": False,
         "group": "token",
         "help": "Modo A: token de una app personalizada ya existente. Déjalo vacío si usas Client ID y secreto."},
        {"key": "client_id", "label": "Client ID (app del Dev Dashboard)", "secret": False, "required": False,
         "group": "app",
         "help": "Modo B: ID de cliente de tu app del Dev Dashboard instalada en la tienda."},
        {"key": "client_secret", "label": "Client secret (app del Dev Dashboard)", "secret": True, "required": False,
         "group": "app",
         "help": "Modo B: secreto de cliente de la misma app."},
    ]
    setup_help = (
        "Conectar Shopify (puede cambiar: verifica en la documentación de Shopify).\n"
        "1. Dominio: es el de administración, con forma nombre-tienda.myshopify.com (lo ves en la URL del admin: "
        "https://admin.shopify.com/store/NOMBRE → NOMBRE.myshopify.com, o en Configuración > Dominios). "
        "No sirve el dominio propio de la tienda.\n"
        "2. Permisos (scopes) que necesita la app: write_products, write_inventory y read_locations "
        "(read_products es opcional). No pidas permisos de clientes ni pedidos.\n"
        "3. Modo A, token de una app personalizada existente: en el admin, Configuración > Apps y canales de venta > "
        "Desarrollar apps > tu app > Credenciales de la API > 'Token de acceso de la API Admin' (empieza con shpat_). "
        "Se muestra una sola vez.\n"
        "4. Modo B, app del Dev Dashboard (si tu tienda ya no deja crear apps personalizadas desde el admin, algo "
        "que desde 2026 ocurre en algunas tiendas): en dev.shopify.com crea una app, activa los permisos del paso 2, "
        "publica una versión e instálala en tu tienda. Copia el Client ID y el Client secret (Configuración de la "
        "app). DropFinder obtiene solo un token temporal (dura unas 24 h) y lo renueva.\n"
        "5. Usa un solo modo: o el token, o Client ID + secreto. Prueba primero en una tienda de desarrollo."
    )

    # Plantillas de URL: atributos de CLASE (solo los tests las reemplazan; no vienen de datos del usuario).
    ADMIN_URL_TEMPLATE = "https://{shop}/admin/api/{version}/graphql.json"
    TOKEN_URL_TEMPLATE = "https://{shop}/admin/oauth/access_token"
    TIMEOUT = TIMEOUT
    MAX_BYTES = MAX_RESPONSE_BYTES

    # ------------------------------------------------------------ dominio y credenciales
    @classmethod
    def normalize_domain(cls, raw: str) -> str:
        bad = StoreValidationError("Dominio inválido: usa nombre-tienda.myshopify.com (sin puerto, ruta ni usuario)")
        if not isinstance(raw, str):
            raise bad
        s = raw.strip()
        if not s or len(s) > 300 or not s.isascii() or any(c.isspace() or c in "\\%" or ord(c) < 32 for c in s):
            raise bad
        s = s.lower()
        m = re.match(r"^([a-z][a-z0-9+.-]*)://", s)
        if m:
            if m.group(1) not in ("https", "http"):
                raise bad
            s = s[m.end():]
        host = re.split(r"[/?#]", s, maxsplit=1)[0]
        if "@" in host or ":" in host or not SHOP_RE.fullmatch(host):
            raise bad
        return host

    @classmethod
    def _mode(cls, credentials: dict) -> str:
        """'token' o 'client'; lanza StoreValidationError si la combinación no es válida."""
        c = {k: (v.strip() if isinstance(v, str) else "") for k, v in (credentials or {}).items()}
        tok, cid, sec = c.get("access_token", ""), c.get("client_id", ""), c.get("client_secret", "")
        if tok and (cid or sec):
            raise StoreValidationError("Usa un solo modo: el token de acceso, o Client ID + Client secret (no ambos)")
        if tok:
            if not _TOKEN_RE.fullmatch(tok):
                raise StoreValidationError("El token de acceso tiene un formato inválido (sin espacios; suele empezar con shpat_)")
            return "token"
        if cid or sec:
            if not (cid and sec):
                raise StoreValidationError("Faltan datos: Client ID y Client secret van juntos")
            if not _CLIENT_ID_RE.fullmatch(cid) or not _TOKEN_RE.fullmatch(sec):
                raise StoreValidationError("El Client ID o el Client secret tienen un formato inválido")
            return "client"
        raise StoreValidationError("Falta la credencial: token de acceso, o Client ID + Client secret")

    @classmethod
    def check_credentials(cls, credentials: dict[str, str]) -> None:
        cls._mode(credentials)

    def __repr__(self) -> str:
        return f"<ShopifyConnector {self.domain}>"              # nunca muestra credenciales

    # ------------------------------------------------------------ HTTP
    def _shop(self) -> str:
        return self.normalize_domain(self.domain)

    def _secrets(self) -> list[str]:
        out = [v for k in ("access_token", "client_secret") if isinstance(v := self.credentials.get(k), str) and v]
        try:
            hit = _TOKEN_CACHE.get(self._cache_key())          # el token de corta vida también es secreto
            if hit:
                out.append(hit[0])
        except StoreError:
            pass
        return out

    def _redact(self, text) -> str:
        return scrub(text, self._secrets())

    def _post(self, url: str, payload: dict, token: str | None) -> tuple[int, object, dict]:
        """POST JSON con el cliente compartido (HTTPS fijo, IP fijada, sin redirecciones, tope de tamaño).
        Devuelve (estado HTTP, JSON o None, cabeceras). Los fallos de red salen como StoreError."""
        headers = {TOKEN_HEADER: token} if token else {}
        resp = shttp.request_json("POST", url, payload, headers=headers, timeout=self.TIMEOUT, max_bytes=self.MAX_BYTES)
        return self._parse(resp)

    @staticmethod
    def _parse(resp) -> tuple[int, object, dict]:
        parsed = None
        if resp.body:
            try:
                parsed = json.loads(resp.body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                if resp.status < 400:
                    raise StoreError("Shopify devolvió una respuesta que no es JSON válido", retryable=True) from None
        return resp.status, parsed, resp.headers

    @staticmethod
    def _retry_after_header(hdrs: dict, default: float) -> float:
        try:
            return max(1.0, min(float(hdrs.get("retry-after", default)), MAX_RETRY_AFTER))
        except (TypeError, ValueError):
            return default

    # ------------------------------------------------------------ tokens
    def _cred_digest(self) -> str:
        c = self.credentials
        raw = "|".join(str(c.get(k, "")) for k in ("access_token", "client_id", "client_secret"))
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def _cache_key(self) -> tuple:
        c = self.credentials
        digest = hashlib.sha256(c.get("client_secret", "").encode()).hexdigest()[:16]
        return (self._shop(), c.get("client_id", ""), digest)   # el digest evita aceptar un secreto distinto con token en caché

    def _token(self) -> str:
        mode = self._mode(self.credentials)
        if mode == "token":
            return self.credentials["access_token"].strip()
        key = self._cache_key()
        with _TOKEN_LOCK:
            lock = _FETCH_LOCKS.setdefault(key, threading.Lock())
        with lock:                                              # una sola obtención aunque haya varios hilos
            hit = _TOKEN_CACHE.get(key)
            if hit and hit[1] - TOKEN_EXPIRY_MARGIN > _now():
                return hit[0]
            token, ttl = self._fetch_token()
            with _TOKEN_LOCK:
                _TOKEN_CACHE[key] = (token, _now() + ttl)
                _sweep_locked()
            return token

    def _invalidate(self, bad_token: str) -> None:
        key = self._cache_key()
        with _TOKEN_LOCK:
            hit = _TOKEN_CACHE.get(key)
            if hit and hit[0] == bad_token:                      # no borra un token ya renovado por otro hilo
                del _TOKEN_CACHE[key]

    def _fetch_token(self) -> tuple[str, float]:
        url = self.TOKEN_URL_TEMPLATE.format(shop=self._shop())
        c = self.credentials
        body = urllib.parse.urlencode({"client_id": c["client_id"].strip(), "client_secret": c["client_secret"].strip(),
                                       "grant_type": "client_credentials"}).encode()
        resp = shttp.request("POST", url, headers={"Content-Type": "application/x-www-form-urlencoded",
                                                   "Accept": "application/json"},
                             body=body, timeout=self.TIMEOUT, max_bytes=self.MAX_BYTES)
        status, data, hdrs = self._parse(resp)
        if status in (400, 401, 403):
            raise StoreAuthError("Shopify rechazó el Client ID / Client secret (o la app no está instalada en la tienda)")
        self._raise_http(status, hdrs)
        tok = data.get("access_token") if isinstance(data, dict) else None
        if not isinstance(tok, str) or not _TOKEN_RE.fullmatch(tok):
            raise StoreError("Shopify no devolvió un token válido", retryable=False)
        try:
            ttl = float(data.get("expires_in", DEFAULT_TOKEN_TTL))
        except (TypeError, ValueError):
            ttl = DEFAULT_TOKEN_TTL
        if not math.isfinite(ttl):
            ttl = DEFAULT_TOKEN_TTL
        return tok, max(60.0, min(ttl, 7 * 86400.0))

    # ------------------------------------------------------------ interpretación de respuestas
    def _raise_http(self, status: int, hdrs: dict) -> None:
        if status < 400:
            return
        if status in (401, 403):
            raise StoreAuthError("Shopify rechazó las credenciales o la app no tiene permisos suficientes")
        if status == 404:
            # Nunca StoreNotFound: eso significa "el producto ya no existe" (y delete() lo tomaría por éxito).
            raise StoreValidationError("No se encontró la tienda: revisa el dominio de la tienda (nombre.myshopify.com)")
        if status == 429:
            raise StoreRateLimited("Shopify pidió esperar (demasiadas solicitudes)", self._retry_after_header(hdrs, 2.0))
        if status in (402, 423):
            raise StoreError(f"La tienda de Shopify está suspendida o bloqueada (HTTP {status})", retryable=False)
        if status >= 500:
            raise StoreError(f"Shopify tuvo un error temporal (HTTP {status})", retryable=True)
        raise StoreError(f"Shopify rechazó la solicitud (HTTP {status})", retryable=False)

    @staticmethod
    def _errors_list(body) -> list:
        errs = body.get("errors") if isinstance(body, dict) else None
        if errs is None:
            return []
        if isinstance(errs, str):
            return [{"message": errs}]
        if isinstance(errs, dict):
            return [errs]
        return [e if isinstance(e, dict) else {"message": str(e)} for e in errs] if isinstance(errs, list) else []

    def _throttle_wait(self, body, hdrs) -> float:
        try:
            cost = body["extensions"]["cost"]
            ts = cost["throttleStatus"]
            need = float(cost.get("requestedQueryCost", LOW_COST_THRESHOLD)) - float(ts["currentlyAvailable"])
            rate = float(ts["restoreRate"])
            if rate > 0:
                return max(1.0, min(need / rate, MAX_THROTTLE_WAIT))
        except (KeyError, TypeError, ValueError):
            pass
        return self._retry_after_header(hdrs, 2.0)

    def _pace(self, body) -> None:
        """Respeta el límite por costo: solo espera si el cupo disponible quedó bajo."""
        try:
            ts = body["extensions"]["cost"]["throttleStatus"]
            avail, rate = float(ts["currentlyAvailable"]), float(ts["restoreRate"])
        except (KeyError, TypeError, ValueError):
            return
        if rate > 0 and avail < LOW_COST_THRESHOLD:
            _sleep(min(MAX_PACE_SLEEP, (LOW_COST_THRESHOLD - avail) / rate))

    def _graphql(self, query: str, variables: dict | None = None, *, partial_access: bool = False) -> dict:
        """Ejecuta una consulta. Devuelve `data`. Renueva el token una vez ante 401 en modo client credentials
        (se reenvían las mismas variables, o sea la misma clave de idempotencia)."""
        url = self.ADMIN_URL_TEMPLATE.format(shop=self._shop(), version=_api_version())
        payload = {"query": query, "variables": variables or {}}
        status = body = hdrs = None
        for attempt in (0, 1):
            token = self._token()
            status, body, hdrs = self._post(url, payload, token)
            if status == 401 and attempt == 0 and self._mode(self.credentials) == "client":
                self._invalidate(token)
                continue
            break
        self._raise_http(status, hdrs)
        if not isinstance(body, dict):
            raise StoreError("Respuesta inesperada de Shopify", retryable=True)
        errors = self._errors_list(body)
        if errors:
            self._raise_graphql_errors(errors, body, hdrs, partial_access and isinstance(body.get("data"), dict))
        data = body.get("data")
        if not isinstance(data, dict):
            raise StoreError("Respuesta de Shopify sin datos", retryable=True)
        self._pace(body)
        return data

    def _raise_graphql_errors(self, errors: list, body: dict, hdrs: dict, tolerate_access: bool) -> None:
        codes = [str((e.get("extensions") or {}).get("code", "")).upper() for e in errors]
        msg = self._redact("; ".join(str(e.get("message", "")) for e in errors[:3]))
        if "THROTTLED" in codes or any("throttled" in str(e.get("message", "")).lower() for e in errors):
            raise StoreRateLimited("Shopify limitó la velocidad de las consultas", self._throttle_wait(body, hdrs))
        non_access = [c for c in codes if c != "ACCESS_DENIED"]
        if "ACCESS_DENIED" in codes:
            if tolerate_access and not non_access:
                return                                           # validate(): falta un scope; se informa como missing_scopes
            raise StoreAuthError(f"Shopify denegó el acceso (faltan permisos de la app): {msg}")
        if any(x in msg.lower() for x in ("invalid api key", "invalid access token", "unrecognized login")):
            raise StoreAuthError("Shopify rechazó el token de acceso")
        if any(c in ("INTERNAL_SERVER_ERROR", "SHOP_INACTIVE") for c in codes):
            raise StoreError(f"Shopify tuvo un error temporal: {msg}", retryable=True)
        raise StoreError(f"Shopify rechazó la consulta: {msg}", retryable=False)

    @staticmethod
    def _is_missing(e: dict) -> bool:
        code = str(e.get("code") or "")
        return bool(_NOT_FOUND_RE.search(str(e.get("message", "")))) or code.endswith(("DOES_NOT_EXIST", "NOT_FOUND"))

    @classmethod
    def _is_product_missing(cls, e: dict) -> bool:
        """Solo cuando el userError habla claramente del PRODUCTO (no de ubicación, variante o inventario)."""
        if not cls._is_missing(e):
            return False
        low = str(e.get("message", "")).lower()
        code = str(e.get("code") or "")
        field = [str(x) for x in (e.get("field") or [])]
        if code in ("PRODUCT_DOES_NOT_EXIST", "PRODUCT_NOT_FOUND"):
            return True
        if re.search(r"\bproduct (does not exist|doesn't exist|not found|could not be found|no longer exists)", low):
            return True
        return field[-1:] == ["productId"] and not re.search(r"variant|location|inventory", low)

    @classmethod
    def _is_location_missing(cls, e: dict) -> bool:
        field = " ".join(str(x) for x in (e.get("field") or [])).lower()
        low = str(e.get("message", "")).lower()
        return cls._is_missing(e) and ("location" in low or "locationid" in field or "LOCATION" in str(e.get("code") or ""))

    def _mutate(self, query: str, field: str, variables: dict, *, ref_op: bool = False) -> dict:
        """Ejecuta una mutación y convierte userErrors en excepciones. Devuelve el payload de `field`.
        StoreNotFound solo si el userError se refiere claramente al producto (y ref_op)."""
        data = self._graphql(query, variables)
        payload = data.get(field)
        if not isinstance(payload, dict):
            raise StoreError("Respuesta inesperada de Shopify", retryable=True)
        uerrs = [e for e in (payload.get("userErrors") or []) if isinstance(e, dict)]
        if uerrs:
            if any(self._is_location_missing(e) for e in uerrs):
                self._forget_location()
                raise StoreError("La ubicación de inventario ya no existe o no está disponible; vuelve a validar la tienda "
                                 "o revisa el ajuste location_id", retryable=False)
            if ref_op and any(self._is_product_missing(e) for e in uerrs):
                raise StoreNotFound("El producto ya no existe en la tienda")
            codes = [str(e.get("code")) for e in uerrs if e.get("code")]
            parts = []
            for e in uerrs[:3]:
                f = ".".join(str(x) for x in (e.get("field") or []) if x not in ("input",))
                parts.append(f"{f}: {e.get('message', '')}" if f else str(e.get("message", "")))
            err = StoreValidationError(self._redact("Shopify rechazó los datos: " + "; ".join(parts)))
            err.codes = codes
            raise err
        return payload

    # ------------------------------------------------------------ ubicación
    def _setting_location(self) -> str:
        loc = self.settings.get("location_id")
        if loc in (None, ""):
            return ""
        if not isinstance(loc, str) or not _LOCATION_GID_RE.fullmatch(loc):
            raise StoreValidationError("El ajuste location_id no es válido (formato gid://shopify/Location/123)")
        return loc

    def _loc_key(self) -> tuple:
        return (self._shop(), self._cred_digest())

    def _forget_location(self) -> None:
        with _TOKEN_LOCK:
            _LOC_CACHE.pop(self._loc_key(), None)

    def _remember_location(self, loc: str) -> None:
        with _TOKEN_LOCK:
            _LOC_CACHE[self._loc_key()] = (loc, _now() + LOCATION_TTL)
            _sweep_locked()

    def _location_id(self) -> str:
        """Ubicación del stock: settings['location_id'] si existe; si no, la primera activa (caché breve)."""
        loc = self._setting_location()
        if loc:
            return loc
        with _TOKEN_LOCK:
            hit = _LOC_CACHE.get(self._loc_key())
        if hit and hit[1] > _now():
            return hit[0]
        loc = self._first_location(self._graphql(Q_LOCATIONS))
        if not loc:
            raise StoreValidationError("La tienda no tiene ubicaciones de inventario activas")
        self._remember_location(loc)
        return loc

    @staticmethod
    def _first_location(data: dict) -> str:
        nodes = ((data.get("locations") or {}).get("nodes")) or []
        for n in nodes:
            if isinstance(n, dict) and n.get("isActive", True) and isinstance(n.get("id"), str) \
                    and _LOCATION_GID_RE.fullmatch(n["id"]):
                return n["id"]
        return ""

    # ------------------------------------------------------------ contrato
    @_foreign_errors
    def validate(self) -> StoreInfo:
        setting_loc = self._setting_location()
        data = self._graphql(Q_VALIDATE, partial_access=True)
        shop = data.get("shop") or {}
        scopes = sorted({s.get("handle") for s in ((data.get("currentAppInstallation") or {}).get("accessScopes") or [])
                         if isinstance(s, dict) and isinstance(s.get("handle"), str)})
        granted = set(scopes)
        missing = [s for s in REQUIRED_SCOPES
                   if s not in granted and not (s.startswith("read_") and "write_" + s[5:] in granted)]
        # Sin ajuste 'location_id' aún: se usa la primera ubicación activa
        loc = setting_loc or self._first_location(data)
        if loc and not setting_loc:
            self._remember_location(loc)
        return StoreInfo(name=str(shop.get("name") or self._shop()), domain=str(shop.get("myshopifyDomain") or self._shop()),
                         currency=str(shop.get("currencyCode") or "USD"), location_id=loc,
                         scopes=scopes, missing_scopes=missing)

    def _admin_url(self, gid: str) -> str:
        m = _GID_PRODUCT_RE.fullmatch(gid or "")
        return f"https://{self._shop()}/admin/products/{m.group(1)}" if m else ""

    @staticmethod
    def _variant_ids(prod: dict) -> tuple[str, str]:
        nodes = (prod.get("variants") or {}).get("nodes") or []
        if not nodes or not isinstance(nodes[0], dict):
            raise StoreError("Shopify creó el producto sin variante por defecto", retryable=True)
        vid = nodes[0].get("id")
        inv_id = (nodes[0].get("inventoryItem") or {}).get("id")
        if not vid or not inv_id:
            raise StoreError("Shopify creó el producto sin artículo de inventario", retryable=True)
        return vid, inv_id

    def _find_existing(self, tag: str) -> dict | None:
        """Producto ya creado por DropFinder con esa etiqueta (publicación idempotente)."""
        data = self._graphql(Q_FIND_BY_TAG, {"q": f"tag:'{tag}'"})
        for n in ((data.get("products") or {}).get("nodes")) or []:
            if isinstance(n, dict) and tag in (n.get("tags") or []) and _GID_PRODUCT_RE.fullmatch(str(n.get("id"))):
                return n
        return None

    @_foreign_errors
    def publish(self, product: StoreProduct) -> ListingRef:
        # 1. Todo se valida ANTES de tocar la tienda
        title = str(product.title or "").strip()[:255]
        if not title:
            raise StoreValidationError("El producto necesita un título")
        sku = str(product.sku or "")[:255]
        price = _money(product.price)
        compare = None
        if product.compare_at_price is not None:
            c = _money(product.compare_at_price, "precio de comparación")
            compare = c if Decimal(c) > Decimal(price) else None     # comparación <= precio: no se envía
        cost = _money(product.cost, "costo", allow_zero=True) if product.cost is not None else None
        stock = _stock(product.stock)
        location = self._location_id()
        tag = _sku_tag(sku)
        tags = _clean_tags(product.tags) + [tag]
        pinput = {"title": title, "descriptionHtml": _description_html(product.description),
                  "vendor": str(product.vendor or "")[:255], "productType": str(product.product_type or "")[:255],
                  "tags": tags, "status": "DRAFT"}        # siempre borrador al crear; se activa al final
        media = _safe_images(product.image_urls)
        for m in media:
            m["alt"] = title[:250]

        # 2. Adoptar un producto ya creado por un intento anterior, o crearlo
        existing = self._find_existing(tag)
        adopted = existing is not None
        if adopted:
            prod, gid = existing, existing["id"]
        else:
            payload = self._mutate(M_PRODUCT_CREATE, "productCreate", {"product": pinput, "media": media or None})
            prod = payload.get("product") or {}
            gid = prod.get("id")
            if not isinstance(gid, str) or not _GID_PRODUCT_RE.fullmatch(gid):
                raise StoreError("Shopify no devolvió el id del producto creado", retryable=True)
        try:
            vid, inv_id = self._variant_ids(prod)
            item = {"sku": sku, "tracked": True}
            if cost is not None:
                item["cost"] = cost
            variant = {"id": vid, "price": price, "inventoryItem": item}
            if compare is not None:
                variant["compareAtPrice"] = compare
            self._mutate(M_VARIANTS_UPDATE, "productVariantsBulkUpdate", {"productId": gid, "variants": [variant]},
                         ref_op=True)
            self._set_stock(inv_id, location, stock)
            if product.active and prod.get("status") != "ACTIVE":
                self._mutate(M_PRODUCT_UPDATE, "productUpdate", {"product": {"id": gid, "status": "ACTIVE"}}, ref_op=True)
        except StoreError as e:
            if not adopted:
                self._cleanup_orphan(gid, e)             # solo se borra lo que creó esta llamada
            if isinstance(e, StoreNotFound):
                raise StoreError("El producto desapareció de la tienda durante la publicación", retryable=True) from None
            raise
        return ListingRef(external_id=gid, variant_id=vid, inventory_item_id=inv_id, url=self._admin_url(gid))

    def _cleanup_orphan(self, gid: str, original: StoreError) -> None:
        """Borra el producto a medio crear; ante límite de velocidad espera y reintenta (hasta CLEANUP_TRIES)."""
        for attempt in range(CLEANUP_TRIES):
            try:
                self._mutate(M_PRODUCT_DELETE, "productDelete", {"input": {"id": gid}}, ref_op=True)
                return
            except StoreNotFound:
                return
            except StoreError as e:
                if not e.retryable or attempt == CLEANUP_TRIES - 1:
                    break
                _sleep(min(float(getattr(e, "retry_after", 1.0)), CLEANUP_MAX_SLEEP))
            except Exception:
                break
        original.orphan_external_id = gid
        original.args = (f"{original.args[0] if original.args else original}. "
                         f"Además no se pudo borrar el borrador incompleto ({gid}); el próximo intento lo reutiliza "
                         "(o bórralo a mano en Shopify)",)
        log.warning("shopify: producto huérfano %s en %s", gid, self._shop())

    def _set_stock(self, inventory_item_id: str, location: str, qty: int) -> None:
        idem = _needs_idempotency(_api_version())

        def run(kind: str, field: str, variables: dict):
            if idem:
                variables = {**variables, "key": str(uuid.uuid4())}
            return self._mutate(_inventory_mutation(kind, idem), field, variables)
        variables = {"input": {"name": "available", "reason": "correction", "ignoreCompareQuantity": True,
                               "quantities": [{"inventoryItemId": inventory_item_id, "locationId": location,
                                               "quantity": int(qty)}]}}
        try:
            run("set", "inventorySetQuantities", variables)
        except StoreValidationError as e:
            if "ITEM_NOT_STOCKED_AT_LOCATION" not in getattr(e, "codes", []):
                raise
            run("activate", "inventoryActivate", {"inventoryItemId": inventory_item_id, "locationId": location})
            run("set", "inventorySetQuantities", variables)

    @staticmethod
    def _check_ref(ref: ListingRef, need_variant: bool = False) -> None:
        if not _GID_PRODUCT_RE.fullmatch(ref.external_id or ""):
            raise StoreValidationError("Identificador de producto de Shopify inválido")
        if need_variant and not (_GID_RE.fullmatch(ref.variant_id or "") and _GID_RE.fullmatch(ref.inventory_item_id or "")):
            raise StoreValidationError("Al anuncio le faltan los identificadores de variante o inventario")

    @_foreign_errors
    def update_price_stock(self, ref: ListingRef, price: float, stock: int,
                           compare_at_price: float | None = None) -> None:
        self._check_ref(ref, need_variant=True)
        p = _money(price)
        n = _stock(stock)
        variant = {"id": ref.variant_id, "price": p}
        if compare_at_price is not None:                         # None = no tocar lo que haya puesto el usuario
            c = _money(compare_at_price, "precio de comparación")
            if Decimal(c) > Decimal(p):
                variant["compareAtPrice"] = c
        location = self._location_id()
        self._mutate(M_VARIANTS_UPDATE, "productVariantsBulkUpdate",
                     {"productId": ref.external_id, "variants": [variant]}, ref_op=True)
        self._set_stock(ref.inventory_item_id, location, n)

    @_foreign_errors
    def set_active(self, ref: ListingRef, active: bool) -> None:
        self._check_ref(ref)
        self._mutate(M_PRODUCT_UPDATE, "productUpdate",
                     {"product": {"id": ref.external_id, "status": "ACTIVE" if active else "DRAFT"}}, ref_op=True)

    @_foreign_errors
    def delete(self, ref: ListingRef) -> None:
        self._check_ref(ref)
        try:
            self._mutate(M_PRODUCT_DELETE, "productDelete", {"input": {"id": ref.external_id}}, ref_op=True)
        except StoreNotFound:
            return
