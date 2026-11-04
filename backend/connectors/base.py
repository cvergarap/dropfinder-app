from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict


@dataclass
class NormalizedProduct:
    """Esquema común al que todo proveedor se traduce."""
    supplier: str
    supplier_sku: str
    title: str
    category: str
    cost: float                # costo del producto (USD)
    shipping_cost: float       # costo de envío (USD)
    shipping_days: int         # días estimados de entrega
    warehouse: str             # país del almacén (ISO-2)
    stock: int
    orders: int                # pedidos históricos
    orders_7d: int             # pedidos últimos 7 días
    orders_prev_7d: int        # pedidos 7 días anteriores
    rating: float
    reviews: int
    image: str = ""
    product_url: str = ""      # ficha original del producto en el proveedor
    tags: list[str] = field(default_factory=list)

    def to_row(self) -> dict:
        d = asdict(self)
        d["tags"] = ",".join(self.tags)
        return d


@dataclass
class PriceStock:
    cost: float
    shipping_cost: float
    stock: int


class SupplierConnector(ABC):
    name: str

    @abstractmethod
    def iter_catalog(self):
        """Itera el catálogo completo (ingesta inicial / periódica)."""

    @abstractmethod
    def get_price_stock(self, supplier_sku: str) -> PriceStock | None:
        """Consulta precio y stock actuales (usado por el monitor)."""


class CJDropshippingConnector(SupplierConnector):
    """Esqueleto para el conector real de CJ Dropshipping (Fase 2).

    API: https://developers.cjdropshipping.com/  — requiere API key.
    - Catálogo: GET /api2.0/v1/product/list
    - Stock:    GET /api2.0/v1/product/stock/queryByVid
    """
    name = "CJ Dropshipping"

    def __init__(self, api_key: str):
        self.api_key = api_key

    def iter_catalog(self):
        raise NotImplementedError("Implementar en Fase 2")

    def get_price_stock(self, supplier_sku: str):
        raise NotImplementedError("Implementar en Fase 2")
