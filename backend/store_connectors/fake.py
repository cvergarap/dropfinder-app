"""Conector falso en memoria, solo para pruebas (NO se registra solo: `register(FakeStore)` en la prueba).

Permite simular lo que hace una tienda real: ids externos, existencias, productos ocultos, productos borrados a
mano, límite de velocidad y credenciales revocadas.
"""
from __future__ import annotations

import itertools
import re
import threading

from . import register
from .base import (ListingRef, StoreAuthError, StoreConnector, StoreInfo, StoreNotFound, StoreProduct,
                   StoreRateLimited, StoreValidationError)

_ids = itertools.count(1000)


class FakeStore(StoreConnector):
    platform = "fake"
    label = "Tienda falsa (pruebas)"
    idempotent_publish = False               # como un conector que no sabe reconocer lo que ya creó
    credential_fields = [{"key": "token", "label": "Token", "secret": True, "help": "Cualquier texto"}]
    # Estado compartido entre instancias del mismo dominio (cada operación crea un conector nuevo)
    stores: dict[str, dict] = {}
    lock = threading.Lock()
    calls: list[tuple] = []
    fail_next: list[Exception] = []          # excepciones que lanzará la siguiente operación (en orden)

    @classmethod
    def reset(cls) -> None:
        with cls.lock:
            cls.stores.clear()
            cls.calls.clear()
            cls.fail_next.clear()

    @classmethod
    def normalize_domain(cls, raw: str) -> str:
        d = (raw or "").strip().lower()
        if not re.fullmatch(r"[a-z0-9-]+\.fake\.test", d):
            raise StoreValidationError("Dominio inválido (usa nombre.fake.test)")
        return d

    def _state(self) -> dict:
        with self.lock:
            return self.stores.setdefault(self.domain, {"products": {}, "currency": "USD", "revoked": False})

    def _enter(self, op: str, *args) -> dict:
        with self.lock:
            self.calls.append((op, self.domain) + args)
            if self.fail_next:
                raise self.fail_next.pop(0)
        st = self._state()
        if st["revoked"] or self.credentials.get("token") == "revocado":
            raise StoreAuthError("Credenciales revocadas")
        return st

    def validate(self) -> StoreInfo:
        self._enter("validate")
        return StoreInfo(name=self.domain, domain=self.domain, currency=self._state()["currency"],
                         location_id="loc-1", scopes=["write_products", "write_inventory"])

    def publish(self, product: StoreProduct) -> ListingRef:
        st = self._enter("publish", product.sku)
        if not product.title.strip():
            raise StoreValidationError("Título vacío")
        pid = str(next(_ids))
        st["products"][pid] = {"title": product.title, "price": product.price, "stock": product.stock,
                               "active": product.active, "sku": product.sku, "image_urls": list(product.image_urls)}
        return ListingRef(external_id=pid, variant_id=f"v{pid}", inventory_item_id=f"i{pid}",
                          url=f"https://{self.domain}/admin/products/{pid}")

    def _product(self, st: dict, ref: ListingRef) -> dict:
        if ref.external_id not in st["products"]:
            raise StoreNotFound("El producto ya no existe en la tienda")
        return st["products"][ref.external_id]

    def update_price_stock(self, ref, price, stock, compare_at_price=None) -> None:
        p = self._product(self._enter("update", ref.external_id, price, stock), ref)
        p["price"], p["stock"] = price, stock

    def set_active(self, ref, active) -> None:
        self._product(self._enter("set_active", ref.external_id, active), ref)["active"] = active

    def delete(self, ref) -> None:
        st = self._enter("delete", ref.external_id)
        st["products"].pop(ref.external_id, None)
