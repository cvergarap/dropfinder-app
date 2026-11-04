"""Conectores de proveedor.

Cada proveedor (AliExpress, CJ, Amazon...) se implementa como una subclase de
SupplierConnector que devuelve productos en el esquema normalizado del catálogo.
"""
from .base import SupplierConnector, NormalizedProduct
from .mock import MockSupplierConnector

__all__ = ["SupplierConnector", "NormalizedProduct", "MockSupplierConnector"]
