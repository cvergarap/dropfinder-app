"""Conector de Shopify (Admin GraphQL API). Solo biblioteca estándar.

QUÉ NO SE PUDO VERIFICAR (sin red en el entorno de desarrollo; todo está escrito de memoria contra la documentación
pública de Shopify y probado solo contra tests/fake_shopify.py). Antes de usarlo en serio, conéctalo a una TIENDA DE
DESARROLLO y revisa estos supuestos:
  1. Versión de API (API_VERSION_DEFAULT) vigente y con estas mutaciones: productCreate(product:, media:),
     productVariantsBulkUpdate (con inventoryItem {sku, cost, tracked}), inventorySetQuantities
     (ignoreCompareQuantity), inventoryActivate, productUpdate(product:), productDelete(input:).
  2. Concesión "client credentials" (Dev Dashboard): POST https://{tienda}/admin/oauth/access_token con JSON
     {client_id, client_secret, grant_type: "client_credentials"} -> {access_token, scope, expires_in}.
  3. Consulta de validación: shop{name myshopifyDomain currencyCode}, currentAppInstallation{accessScopes{handle}},
     locations(first:N){nodes{id name isActive}}.
  4. Códigos de error: HTTP 401 por token inválido/caducado; ACCESS_DENIED por scope faltante; THROTTLED con
     extensions.cost.throttleStatus; userErrors con "Product does not exist" para productos borrados; código
     ITEM_NOT_STOCKED_AT_LOCATION cuando el artículo no está activado en la ubicación.
  5. Que el producto recién creado trae una variante por defecto y que su artículo de inventario queda activado en
     la ubicación principal (si no, se usa inventoryActivate como respaldo).
  6. Que write_X implica read_X al calcular los permisos que faltan.
  7. Que los textos de la guía (setup_help) sobre el Dev Dashboard / apps personalizadas siguen vigentes.

Decisiones de seguridad (ADR 0002): dominio estricto <tienda>.myshopify.com, solo HTTPS, sin seguir redirecciones,
verificación TLS siempre activa, tiempo máximo, tope de tamaño de respuesta, URL fija (plantillas de CLASE que solo
los tests reemplazan; nunca salen de datos del usuario ni del entorno), y ningún secreto en excepciones ni logs.

Orfandad al publicar: productCreate -> productVariantsBulkUpdate -> inventorySetQuantities (-> productUpdate ACTIVE).
El producto se crea SIEMPRE como borrador y solo se activa al final, para que nunca esté visible a precio 0.
Si un paso posterior falla se intenta productDelete y se relanza el error ORIGINAL (el motor no recibe ListingRef,
así que no queda un anuncio fantasma). Si el borrado también falla, el error lleva el atributo
`orphan_external_id` (y el mensaje lo indica) para que se pueda limpiar a mano.
"""
from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import re
import threading
import time
import urllib.parse
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
REQUIRED_SCOPES = ("write_products", "write_inventory", "read_locations")
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

_GID_PRODUCT_RE = re.compile(r"^gid://shopify/Product/(\d{1,20})$")
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
M_INVENTORY_SET = """mutation InventorySet($input: InventorySetQuantitiesInput!) {
  inventorySetQuantities(input: $input) {
    inventoryAdjustmentGroup { id }
    userErrors { field message code }
  }
}"""
M_INVENTORY_ACTIVATE = """mutation InventoryActivate($inventoryItemId: ID!, $locationId: ID!) {
  inventoryActivate(inventoryItemId: $inventoryItemId, locationId: $locationId) {
    inventoryLevel { id }
    userErrors { field message }
  }
}"""
M_PRODUCT_UPDATE = """mutation ProductUpdate($product: ProductUpdateInput!) {
  productUpdate(product: $product) { product { id status } userErrors { field message } }
}"""
M_PRODUCT_DELETE = """mutation ProductDelete($input: ProductDeleteInput!) {
  productDelete(input: $input) { deletedProductId userErrors { field message } }
}"""


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


def _money(value, what: str = "precio") -> str:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise StoreValidationError(f"El {what} no es un número válido") from None
    if not d.is_finite() or d < 0 or d > Decimal("99999999"):
        raise StoreValidationError(f"El {what} está fuera de rango")
    return f"{d.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):.2f}"


def _description_html(text: str) -> str:
    """Las descripciones de DropFinder son texto plano: se escapa todo y los saltos de línea pasan a <br>."""
    t = (text or "").replace("\r\n", "\n").replace("\r", "\n")[:100000]
    return html.escape(t, quote=True).replace("\n", "<br>")


def _safe_images(urls) -> list[dict]:
    out = []
    for u in urls or []:
        if not isinstance(u, str) or len(u) > 2000 or any(c.isspace() or ord(c) < 32 for c in u):
            continue
        try:
            p = urllib.parse.urlsplit(u)
        except ValueError:
            continue
        if p.scheme != "https" or not p.hostname or p.username or p.password:
            continue
        out.append({"originalSource": u, "mediaContentType": "IMAGE"})
        if len(out) >= MAX_IMAGES:
            break
    return out


# ---------------------------------------------------------------- caché de tokens (por proceso)
_TOKEN_LOCK = threading.Lock()
_TOKEN_CACHE: dict[tuple, tuple[str, float]] = {}
_FETCH_LOCKS: dict[tuple, threading.Lock] = {}
_LOC_CACHE: dict[str, tuple[str, float]] = {}


def clear_caches() -> None:
    """Vacía token y ubicación en memoria (para pruebas)."""
    with _TOKEN_LOCK:
        _TOKEN_CACHE.clear()
        _FETCH_LOCKS.clear()
        _LOC_CACHE.clear()


@register
class ShopifyConnector(StoreConnector):
    platform = "shopify"
    label = "Shopify"
    credential_fields = [
        {"key": "access_token", "label": "Token de acceso de la API Admin (shpat_…)", "secret": True, "required": False,
         "help": "Modo A: token de una app personalizada ya existente. Déjalo vacío si usas Client ID y secreto."},
        {"key": "client_id", "label": "Client ID (app del Dev Dashboard)", "secret": False, "required": False,
         "help": "Modo B: ID de cliente de tu app del Dev Dashboard instalada en la tienda."},
        {"key": "client_secret", "label": "Client secret (app del Dev Dashboard)", "secret": True, "required": False,
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

    # ------------------------------------------------------------ HTTP
    def _shop(self) -> str:
        return self.normalize_domain(self.domain)

    def _secrets(self) -> list[str]:
        return [v for k in ("access_token", "client_secret") if isinstance(v := self.credentials.get(k), str) and v]

    def _redact(self, text: str) -> str:
        return scrub(text, self._secrets())

    def _post(self, url: str, payload: dict, token: str | None) -> tuple[int, object, dict]:
        """POST JSON con el cliente compartido (HTTPS fijo, IP fijada, sin redirecciones, tope de tamaño).
        Devuelve (estado HTTP, JSON o None, cabeceras). Los fallos de red salen como StoreError."""
        headers = {TOKEN_HEADER: token} if token else {}
        resp = shttp.request_json("POST", url, payload, headers=headers, timeout=self.TIMEOUT, max_bytes=self.MAX_BYTES)
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
            return max(1.0, min(float(hdrs.get("retry-after", default)), 120.0))
        except (TypeError, ValueError):
            return default

    # ------------------------------------------------------------ tokens
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
            _TOKEN_CACHE[key] = (token, _now() + ttl)
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
        status, data, hdrs = self._post(url, {"client_id": c["client_id"].strip(), "client_secret": c["client_secret"].strip(),
                                              "grant_type": "client_credentials"}, None)
        if status in (400, 401, 403):
            raise StoreAuthError("Shopify rechazó el Client ID / Client secret (o la app no está instalada en la tienda)")
        self._raise_http(status, hdrs, validating=False)
        tok = data.get("access_token") if isinstance(data, dict) else None
        if not isinstance(tok, str) or not _TOKEN_RE.fullmatch(tok):
            raise StoreError("Shopify no devolvió un token válido", retryable=False)
        try:
            ttl = float(data.get("expires_in", DEFAULT_TOKEN_TTL))
        except (TypeError, ValueError):
            ttl = DEFAULT_TOKEN_TTL
        return tok, max(60.0, min(ttl, 7 * 86400.0))

    # ------------------------------------------------------------ interpretación de respuestas
    def _raise_http(self, status: int, hdrs: dict, *, validating: bool) -> None:
        if status < 400:
            return
        if status in (401, 403):
            raise StoreAuthError("Shopify rechazó las credenciales o la app no tiene permisos suficientes")
        if status == 404:
            if validating:
                raise StoreValidationError("No se encontró la tienda: revisa el dominio (nombre.myshopify.com)")
            raise StoreNotFound("Shopify no encontró la tienda o el recurso")
        if status == 429:
            raise StoreRateLimited("Shopify pidió esperar (demasiadas solicitudes)", self._retry_after_header(hdrs, 2.0))
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
                return max(1.0, min(need / rate, 60.0))
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

    def _graphql(self, query: str, variables: dict | None = None, *, validating: bool = False,
                 partial_access: bool = False) -> dict:
        """Ejecuta una consulta. Devuelve `data`. Renueva el token una vez ante 401 en modo client credentials."""
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
        self._raise_http(status, hdrs, validating=validating)
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

    def _mutate(self, query: str, field: str, variables: dict, *, ref_op: bool = False) -> dict:
        """Ejecuta una mutación y convierte userErrors en excepciones. Devuelve el payload de `field`."""
        data = self._graphql(query, variables)
        payload = data.get(field)
        if not isinstance(payload, dict):
            raise StoreError("Respuesta inesperada de Shopify", retryable=True)
        uerrs = [e for e in (payload.get("userErrors") or []) if isinstance(e, dict)]
        if uerrs:
            texts = [str(e.get("message", "")) for e in uerrs]
            codes = [str(e.get("code", "")) for e in uerrs if e.get("code")]
            if ref_op and any(_NOT_FOUND_RE.search(t) or c.endswith(("DOES_NOT_EXIST", "NOT_FOUND")) for t, c in
                              zip(texts, [str(e.get("code", "")) for e in uerrs])):
                raise StoreNotFound("El producto ya no existe en la tienda")
            parts = []
            for e in uerrs[:3]:
                f = ".".join(str(x) for x in (e.get("field") or []) if x not in ("input",))
                parts.append(f"{f}: {e.get('message', '')}" if f else str(e.get("message", "")))
            err = StoreValidationError(self._redact("Shopify rechazó los datos: " + "; ".join(parts)))
            err.codes = codes
            raise err
        return payload

    # ------------------------------------------------------------ ubicación
    def _location_id(self) -> str:
        """Ubicación del stock: settings['location_id'] si existe; si no, la primera activa (con caché breve)."""
        loc = self.settings.get("location_id")
        if isinstance(loc, str) and _GID_RE.fullmatch(loc):
            return loc
        shop = self._shop()
        with _TOKEN_LOCK:
            hit = _LOC_CACHE.get(shop)
        if hit and hit[1] > _now():
            return hit[0]
        loc = self._first_location(self._graphql(Q_LOCATIONS))
        if not loc:
            raise StoreValidationError("La tienda no tiene ubicaciones de inventario activas")
        with _TOKEN_LOCK:
            _LOC_CACHE[shop] = (loc, _now() + LOCATION_TTL)
        return loc

    @staticmethod
    def _first_location(data: dict) -> str:
        nodes = ((data.get("locations") or {}).get("nodes")) or []
        for n in nodes:
            if isinstance(n, dict) and n.get("isActive", True) and isinstance(n.get("id"), str):
                return n["id"]
        return ""

    # ------------------------------------------------------------ contrato
    def validate(self) -> StoreInfo:
        data = self._graphql(Q_VALIDATE, validating=True, partial_access=True)
        shop = data.get("shop") or {}
        scopes = sorted({s.get("handle") for s in ((data.get("currentAppInstallation") or {}).get("accessScopes") or [])
                         if isinstance(s, dict) and isinstance(s.get("handle"), str)})
        granted = set(scopes)
        missing = [s for s in REQUIRED_SCOPES
                   if s not in granted and not (s.startswith("read_") and "write_" + s[5:] in granted)]
        # Sin ajuste 'location_id' aún: se usa la primera ubicación activa
        loc = self.settings.get("location_id") if isinstance(self.settings.get("location_id"), str) else ""
        loc = loc or self._first_location(data)
        if loc and not self.settings.get("location_id"):
            with _TOKEN_LOCK:
                _LOC_CACHE[self._shop()] = (loc, _now() + LOCATION_TTL)
        return StoreInfo(name=str(shop.get("name") or self._shop()), domain=str(shop.get("myshopifyDomain") or self._shop()),
                         currency=str(shop.get("currencyCode") or "USD"), location_id=loc,
                         scopes=scopes, missing_scopes=missing)

    def _admin_url(self, gid: str) -> str:
        m = _GID_PRODUCT_RE.fullmatch(gid or "")
        return f"https://{self._shop()}/admin/products/{m.group(1)}" if m else ""

    def publish(self, product: StoreProduct) -> ListingRef:
        title = (product.title or "").strip()[:255]
        if not title:
            raise StoreValidationError("El producto necesita un título")
        price = _money(product.price)
        compare = _money(product.compare_at_price, "precio de comparación") if product.compare_at_price is not None else None
        if compare is not None and Decimal(compare) <= Decimal(price):
            compare = None
        cost = _money(product.cost, "costo") if product.cost is not None else None
        stock = max(0, int(product.stock))
        location = self._location_id()
        tags = [t.replace(",", " ").strip()[:255] for t in (product.tags or []) if isinstance(t, str) and t.strip()][:250]
        pinput = {"title": title, "descriptionHtml": _description_html(product.description),
                  "vendor": (product.vendor or "")[:255], "productType": (product.product_type or "")[:255],
                  "tags": tags, "status": "DRAFT"}        # siempre borrador al crear; se activa al final
        media = _safe_images(product.image_urls)
        for m in media:
            m["alt"] = title[:250]

        payload = self._mutate(M_PRODUCT_CREATE, "productCreate", {"product": pinput, "media": media or None})
        prod = payload.get("product") or {}
        gid = prod.get("id")
        if not isinstance(gid, str) or not _GID_PRODUCT_RE.fullmatch(gid):
            raise StoreError("Shopify no devolvió el id del producto creado", retryable=True)
        try:
            nodes = (prod.get("variants") or {}).get("nodes") or []
            if not nodes or not isinstance(nodes[0], dict):
                raise StoreError("Shopify creó el producto sin variante por defecto", retryable=True)
            vid = nodes[0].get("id")
            inv_id = (nodes[0].get("inventoryItem") or {}).get("id")
            if not vid or not inv_id:
                raise StoreError("Shopify creó el producto sin artículo de inventario", retryable=True)
            item = {"sku": product.sku or "", "tracked": True}
            if cost is not None:
                item["cost"] = cost
            self._mutate(M_VARIANTS_UPDATE, "productVariantsBulkUpdate", {
                "productId": gid, "variants": [{"id": vid, "price": price, "compareAtPrice": compare, "inventoryItem": item}]})
            self._set_stock(inv_id, location, stock)
            if product.active:
                self._mutate(M_PRODUCT_UPDATE, "productUpdate", {"product": {"id": gid, "status": "ACTIVE"}})
        except StoreError as e:
            self._cleanup_orphan(gid, e)
            raise
        return ListingRef(external_id=gid, variant_id=vid, inventory_item_id=inv_id, url=self._admin_url(gid))

    def _cleanup_orphan(self, gid: str, original: StoreError) -> None:
        try:
            self._mutate(M_PRODUCT_DELETE, "productDelete", {"input": {"id": gid}}, ref_op=True)
        except StoreNotFound:
            pass
        except Exception:                                          # el error original es el importante
            original.orphan_external_id = gid
            original.args = (f"{original.args[0] if original.args else original}. "
                             f"Además no se pudo borrar el borrador incompleto ({gid}); bórralo a mano en Shopify",)
            log.warning("shopify: producto huérfano %s en %s", gid, self._shop())

    def _set_stock(self, inventory_item_id: str, location: str, qty: int) -> None:
        variables = {"input": {"name": "available", "reason": "correction", "ignoreCompareQuantity": True,
                               "quantities": [{"inventoryItemId": inventory_item_id, "locationId": location,
                                               "quantity": int(qty)}]}}
        try:
            self._mutate(M_INVENTORY_SET, "inventorySetQuantities", variables, ref_op=True)
        except StoreValidationError as e:
            if "ITEM_NOT_STOCKED_AT_LOCATION" not in getattr(e, "codes", []):
                raise
            self._mutate(M_INVENTORY_ACTIVATE, "inventoryActivate",
                         {"inventoryItemId": inventory_item_id, "locationId": location})
            self._mutate(M_INVENTORY_SET, "inventorySetQuantities", variables, ref_op=True)

    @staticmethod
    def _check_ref(ref: ListingRef, need_variant: bool = False) -> None:
        if not _GID_PRODUCT_RE.fullmatch(ref.external_id or ""):
            raise StoreValidationError("Identificador de producto de Shopify inválido")
        if need_variant and not (_GID_RE.fullmatch(ref.variant_id or "") and _GID_RE.fullmatch(ref.inventory_item_id or "")):
            raise StoreValidationError("Al anuncio le faltan los identificadores de variante o inventario")

    def update_price_stock(self, ref: ListingRef, price: float, stock: int,
                           compare_at_price: float | None = None) -> None:
        self._check_ref(ref, need_variant=True)
        p = _money(price)
        compare = _money(compare_at_price, "precio de comparación") if compare_at_price is not None else None
        if compare is not None and Decimal(compare) <= Decimal(p):
            compare = None
        location = self._location_id()
        self._mutate(M_VARIANTS_UPDATE, "productVariantsBulkUpdate", {
            "productId": ref.external_id, "variants": [{"id": ref.variant_id, "price": p, "compareAtPrice": compare}]},
            ref_op=True)
        self._set_stock(ref.inventory_item_id, location, max(0, int(stock)))

    def set_active(self, ref: ListingRef, active: bool) -> None:
        self._check_ref(ref)
        self._mutate(M_PRODUCT_UPDATE, "productUpdate",
                     {"product": {"id": ref.external_id, "status": "ACTIVE" if active else "DRAFT"}}, ref_op=True)

    def delete(self, ref: ListingRef) -> None:
        self._check_ref(ref)
        try:
            self._mutate(M_PRODUCT_DELETE, "productDelete", {"input": {"id": ref.external_id}}, ref_op=True)
        except StoreNotFound:
            return
