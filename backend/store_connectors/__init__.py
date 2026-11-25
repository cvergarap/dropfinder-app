"""Conectores de tienda. Cada módulo de plataforma se registra al importarse (ver base.StoreConnector)."""
from __future__ import annotations

from .base import (ListingRef, StoreAuthError, StoreConnector, StoreError, StoreInfo, StoreNotFound,
                   StoreProduct, StoreRateLimited, StoreValidationError, is_owned_by, safe_tags, scrub, tags_match)

_registry: dict[str, type[StoreConnector]] = {}


def register(cls: type[StoreConnector]) -> type[StoreConnector]:
    """Decorador: registra una plataforma por su `platform`."""
    if not cls.platform:
        raise ValueError("El conector necesita `platform`")
    _registry[cls.platform] = cls
    return cls


def platforms() -> list[type[StoreConnector]]:
    return list(_registry.values())


def get_class(platform: str) -> type[StoreConnector]:
    try:
        return _registry[platform]
    except KeyError:
        raise StoreValidationError(f"Plataforma no soportada: {platform}") from None


def create(platform: str, domain: str, credentials: dict[str, str], settings: dict | None = None) -> StoreConnector:
    return get_class(platform)(domain, credentials, settings)


# Plataformas reales: se importan si existen (cada rama de funcionalidad agrega su módulo)
for _mod in ("shopify", "tiendanube", "woocommerce"):
    try:
        __import__(f"{__name__}.{_mod}")
    except ModuleNotFoundError as _e:
        if _e.name != f"{__name__}.{_mod}":
            raise

__all__ = ["scrub", "tags_match", "safe_tags", "is_owned_by", "register", "platforms", "get_class", "create", "StoreConnector", "StoreProduct", "ListingRef",
           "StoreInfo", "StoreError", "StoreAuthError", "StoreRateLimited", "StoreNotFound", "StoreValidationError"]
