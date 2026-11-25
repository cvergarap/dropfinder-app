"""Conectores de proveedor.

Cada proveedor (AliExpress, CJ, Amazon, CSV de un mayorista...) se implementa como una subclase de
SupplierConnector que devuelve productos en el esquema normalizado del catálogo.
"""
from .base import NormalizedProduct, SupplierConnector
from .csv_connector import CsvSupplierConnector
from .mock import MockSupplierConnector
from .cj import CJConnector

CJDropshippingConnector = CJConnector      # nombre del esqueleto anterior

__all__ = ["SupplierConnector", "NormalizedProduct", "MockSupplierConnector", "CsvSupplierConnector",
           "CJConnector", "CJDropshippingConnector"]
