"""Contrato común de los conectores de tienda (ver docs/adr/0002-integracion-tiendas.md).

Cada plataforma (Shopify, Tiendanube, WooCommerce…) implementa StoreConnector. El resto de la app solo
conoce esta interfaz y los errores de abajo, nunca detalles de una plataforma.
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

# Formas conocidas de credenciales (Shopify shpat_/shpca_/shppa_/shpss_, WooCommerce ck_/cs_, JWT, hex/base64 largos)
_TOKEN_RE = re.compile(r"(?i)\b(?:shp(?:at|ca|pa|ss)_[a-z0-9]{8,}|c[ks]_[a-f0-9]{16,}|eyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]*"
                       r"|(?:bearer|token|secret|key|password)\s*[:=]?\s*[\w.+/=-]{8,}|[a-f0-9]{32,}|[A-Za-z0-9_+/=-]{40,})")


def tags_match(existing, expected) -> bool:
    """¿El producto que ya está en la tienda fue creado por esta instalación de DropFinder? Se comprueba que lleve TODAS
    las etiquetas que DropFinder pone al publicar (`dropfinder` y la de la instalación, `dfi-<id>`). El SKU `DF-<id>`
    por sí solo NO prueba nada: el id es local a cada base de datos y puede coincidir con un producto del cliente o de
    otra instalación. Sin etiquetas esperadas no hay nada que comprobar y la respuesta es siempre False."""
    def norm(t):
        return str(t).strip().lower()
    want = {norm(t) for t in (expected or []) if str(t).strip()}
    have = {norm(t) for t in (existing or []) if str(t).strip()}
    return bool(want) and want <= have


def scrub(text, secrets=(), limit: int = 300) -> str:
    """Quita de un mensaje de error todo lo que parezca una credencial (y los valores secretos conocidos) y lo
    acorta. Se aplica a CUALQUIER texto que venga de una tienda antes de guardarlo, registrarlo o mostrarlo."""
    s = str(text or "")
    for v in secrets:
        if v and len(v) >= 6:
            s = s.replace(v, "••••")
    s = _TOKEN_RE.sub("••••", s)
    s = "".join(c if c >= " " or c in "\n\t" else " " for c in s)       # sin caracteres de control
    return s[:limit]


# ---------------------------------------------------------------- errores

class StoreError(Exception):
    """Fallo al hablar con la tienda. `retryable`: tiene sentido reintentar más tarde."""
    retryable = False
    # True solo si se sabe que la petición NO llegó a la tienda (falló al resolver el nombre o al conectar). Un
    # tiempo agotado o una conexión cortada DESPUÉS de enviar es ambiguo: la tienda pudo haber hecho el cambio.
    # El motor solo repite una publicación en un conector sin idempotent_publish si not_sent es True (o si la
    # tienda respondió explícitamente que no la procesó: StoreRateLimited).
    not_sent = False

    def __init__(self, message: str, *, retryable: bool | None = None, not_sent: bool | None = None):
        super().__init__(message)
        if retryable is not None:
            self.retryable = retryable
        if not_sent is not None:
            self.not_sent = not_sent


class StoreAuthError(StoreError):
    """Credenciales inválidas, revocadas o sin los permisos necesarios (no se reintenta)."""


class StoreRateLimited(StoreError):
    """La plataforma pidió esperar. `retry_after`: segundos sugeridos."""
    retryable = True

    def __init__(self, message: str, retry_after: float = 5.0):
        super().__init__(message)
        self.retry_after = retry_after


class StoreNotFound(StoreError):
    """El producto ya no existe en la tienda (borrado a mano, por ejemplo)."""


class StoreValidationError(StoreError):
    """La plataforma rechazó los datos del producto (título vacío, precio inválido…). No se reintenta."""


# ---------------------------------------------------------------- datos

@dataclass
class StoreProduct:
    """Producto listo para publicar. Precios en la moneda de la tienda (ver StoreInfo.currency)."""
    title: str
    price: float
    sku: str
    stock: int
    description: str = ""
    image_urls: list[str] = field(default_factory=list)
    compare_at_price: float | None = None
    vendor: str = ""
    product_type: str = ""
    tags: list[str] = field(default_factory=list)
    cost: float | None = None               # costo del producto (algunas plataformas lo guardan en el inventario)
    active: bool = True                     # publicado (visible) o borrador


@dataclass
class ListingRef:
    """Identificadores del producto publicado en la tienda (se guardan en store_listings)."""
    external_id: str
    variant_id: str = ""
    inventory_item_id: str = ""
    url: str = ""                           # enlace al producto en la tienda (admin o público)


@dataclass
class StoreInfo:
    """Resultado de validate(): confirma que las credenciales funcionan."""
    name: str
    domain: str
    currency: str = "USD"
    location_id: str = ""                   # ubicación de inventario usada para el stock
    scopes: list[str] = field(default_factory=list)
    missing_scopes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- contrato

class StoreConnector(ABC):
    platform: str = ""                      # clave de registro: "shopify", "tiendanube", "woocommerce"
    label: str = ""                         # nombre para mostrar
    # Campos de credenciales que pide la plataforma: [{key, label, secret, help, required (por defecto True)}].
    # Si hay formas alternativas (p. ej. token fijo o id+secreto), márcalos required=False y valida la
    # combinación en check_credentials.
    credential_fields: list[dict] = []
    setup_help: str = ""                    # pasos para crear las credenciales en la plataforma (se muestra en la UI)
    # ¿publish() es seguro de repetir tras un resultado desconocido (respuesta perdida, error interno justo después
    # de crear)? True solo si el conector puede reconocer el producto que ya creó y adoptarlo en vez de duplicarlo
    # (Shopify: etiqueta df-sku:<sku>). El motor de sincronización SOLO reintenta una publicación que ya llamó a la
    # tienda si esto es True; si es False, un resultado desconocido deja el anuncio en error para que el usuario revise.
    idempotent_publish: bool = False

    def __init__(self, domain: str, credentials: dict[str, str], settings: dict | None = None):
        self.domain = domain
        self.credentials = credentials
        self.settings = settings or {}

    @classmethod
    def check_credentials(cls, credentials: dict[str, str]) -> None:
        """Valida la combinación de campos (ya limpios). Lanza StoreValidationError con un mensaje claro."""

    @classmethod
    def normalize_domain(cls, raw: str) -> str:
        """Valida y normaliza el dominio de la tienda (anti-SSRF). Lanza StoreValidationError si no es válido."""
        raise NotImplementedError

    @abstractmethod
    def validate(self) -> StoreInfo:
        """Comprueba credenciales y permisos. StoreAuthError si no sirven."""

    @abstractmethod
    def publish(self, product: StoreProduct) -> ListingRef:
        """Crea el producto en la tienda (idempotencia a cargo del llamador: no se llama dos veces)."""

    @abstractmethod
    def update_price_stock(self, ref: ListingRef, price: float, stock: int,
                           compare_at_price: float | None = None) -> None:
        """Actualiza precio y existencias de un producto ya publicado."""

    @abstractmethod
    def set_active(self, ref: ListingRef, active: bool) -> None:
        """Muestra (activo) u oculta (borrador/archivado) el producto sin borrarlo."""

    @abstractmethod
    def delete(self, ref: ListingRef) -> None:
        """Elimina el producto de la tienda. Si ya no existe, no es error."""
