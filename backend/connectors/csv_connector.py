"""Conector CSV: carga el catálogo de cualquier proveedor desde un archivo CSV/Excel exportado.

Acepta encabezados en español o inglés, separador "," o ";" (Excel en español usa ";")
y decimales con coma o punto. Columnas obligatorias: sku, titulo, costo.
"""
from __future__ import annotations

import csv
import io
import re
import unicodedata
from dataclasses import dataclass, field

from .base import NormalizedProduct, SupplierConnector

# columna normalizada -> alias aceptados (en minúsculas, sin tildes ni espacios extra)
ALIASES = {
    "supplier_sku": ["sku", "codigo", "id", "referencia", "ref", "supplier_sku"],
    "title": ["titulo", "nombre", "producto", "title", "name"],
    "cost": ["costo", "precio", "precio costo", "precio proveedor", "cost", "price"],
    "category": ["categoria", "category"],
    "shipping_cost": ["costo envio", "envio", "shipping", "shipping cost", "shipping_cost"],
    "shipping_days": ["dias envio", "dias de envio", "tiempo envio", "shipping days", "shipping_days"],
    "warehouse": ["almacen", "pais", "pais almacen", "warehouse", "country"],
    "stock": ["stock", "inventario", "existencias", "cantidad"],
    "orders": ["pedidos", "ventas", "orders", "sales"],
    "orders_7d": ["pedidos 7d", "ventas 7d", "orders_7d"],
    "orders_prev_7d": ["pedidos 7d anteriores", "ventas 7d anteriores", "orders_prev_7d"],
    "rating": ["rating", "valoracion", "calificacion", "estrellas"],
    "reviews": ["resenas", "opiniones", "reviews"],
    "image": ["imagen", "foto", "url imagen", "image", "image_url"],
    "product_url": ["url", "enlace", "link", "url producto", "product_url"],
    "tags": ["etiquetas", "tags", "palabras clave"],
}
REQUIRED = ("supplier_sku", "title", "cost")
MAX_ROWS = 50_000


def _norm_header(h: str) -> str:
    h = unicodedata.normalize("NFKD", (h or "").strip().lower().lstrip("﻿"))
    h = "".join(c for c in h if not unicodedata.combining(c))
    return re.sub(r"[\s_\-]+", " ", h).strip()


_LOOKUP = {_norm_header(a): col for col, aliases in ALIASES.items() for a in aliases}


def _num(v: str, cast=float, default=0):
    v = re.sub(r"[^\d,.\-]", "", v or "")     # "USD 1.234,50" -> "1.234,50"
    if not v:
        return default
    if "," in v and "." in v:          # 1.234,56  o  1,234.56
        v = v.replace(".", "").replace(",", ".") if v.rfind(",") > v.rfind(".") else v.replace(",", "")
    elif "," in v:                     # 12,50
        v = v.replace(",", ".")
    return cast(float(v))


def _safe_url(u: str) -> str:
    u = (u or "").strip()
    return u if re.match(r"^https?://", u, re.I) else ""


@dataclass
class CsvImportResult:
    products: list[NormalizedProduct] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    columns: dict[str, str] = field(default_factory=dict)   # encabezado original -> columna


class CsvSupplierConnector(SupplierConnector):
    """No tiene fuente en vivo: precio y stock se actualizan volviendo a subir el CSV."""

    def __init__(self, supplier: str, text: str):
        self.name = supplier
        self.result = self._parse(supplier, text)

    def iter_catalog(self):
        return iter(self.result.products)

    def get_price_stock(self, supplier_sku, current=None):
        return None

    @staticmethod
    def _parse(supplier: str, text: str) -> CsvImportResult:  # noqa: C901
        res = CsvImportResult()
        # El separador se decide con la fila de encabezados: en las filas de datos las comas
        # decimales ("8,90") confundirían a un detector automático.
        header_line = text.lstrip("﻿").splitlines()[0] if text.strip() else ""
        delimiter = max(";,\t", key=header_line.count)
        reader = csv.reader(io.StringIO(text), delimiter=delimiter)
        try:
            headers = next(reader)
        except StopIteration:
            res.errors.append("El archivo está vacío")
            return res
        index = {}
        for i, h in enumerate(headers):
            col = _LOOKUP.get(_norm_header(h))
            if col and col not in index:
                index[col] = i
                res.columns[h.strip().lstrip("﻿")] = col
        missing = [c for c in REQUIRED if c not in index]
        if missing:
            names = {"supplier_sku": "sku", "title": "titulo", "cost": "costo"}
            res.errors.append("Faltan columnas obligatorias: " + ", ".join(names[c] for c in missing))
            return res

        seen = set()
        for line_no, row in enumerate(reader, start=2):
            if not any(c.strip() for c in row):
                continue
            if len(res.products) >= MAX_ROWS:
                res.errors.append(f"Se cargaron solo las primeras {MAX_ROWS} filas")
                break

            def get(col, _row=row):
                i = index.get(col)
                return _row[i].strip() if i is not None and i < len(_row) else ""

            try:
                sku, title = get("supplier_sku"), get("title")
                if not sku or not title:
                    raise ValueError("sku y titulo no pueden estar vacíos")
                if sku in seen:
                    raise ValueError(f"sku repetido ({sku})")
                cost = _num(get("cost"))
                if cost <= 0:
                    raise ValueError("costo debe ser mayor que 0")
                rating = _num(get("rating"), float, 0)
                category = get("category") or "Sin categoría"
                tags = [t.strip().lower() for t in re.split(r"[,|]", get("tags")) if t.strip()]
                orders_7d = _num(get("orders_7d"), int, 0)
                res.products.append(NormalizedProduct(
                    supplier=supplier, supplier_sku=sku[:100], title=title[:200], category=category[:80],
                    cost=round(cost, 2),
                    shipping_cost=round(_num(get("shipping_cost")), 2),
                    shipping_days=max(0, _num(get("shipping_days"), int, 10)),
                    warehouse=(get("warehouse") or "—").upper()[:3],
                    stock=max(0, _num(get("stock"), int, 0)),
                    orders=max(0, _num(get("orders"), int, 0)),
                    orders_7d=max(0, orders_7d),
                    orders_prev_7d=max(0, _num(get("orders_prev_7d"), int, orders_7d)),
                    rating=min(5.0, max(0.0, rating)),
                    reviews=max(0, _num(get("reviews"), int, 0)),
                    image=_safe_url(get("image")),
                    product_url=_safe_url(get("product_url")),
                    tags=[category.lower(), *tags],
                ))
                seen.add(sku)
            except ValueError as e:
                msg = str(e)
                if msg.startswith(("could not convert", "invalid literal")):
                    msg = "número inválido"
                if len(res.errors) < 50:
                    res.errors.append(f"Fila {line_no}: {msg}")
        return res
