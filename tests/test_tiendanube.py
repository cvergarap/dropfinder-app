"""Conector de Tiendanube contra el servidor falso local (tests/fake_tiendanube.py). Sin red real."""
import json
import logging
import os
import re
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["DROPFINDER_SYNTH_HISTORY"] = "0"
for _k in ("no_proxy", "NO_PROXY"):                 # el servidor falso es local: nada de proxies
    os.environ[_k] = "127.0.0.1,localhost"

import store_connectors as sc  # noqa: E402
from store_connectors import tiendanube  # noqa: E402
from store_connectors.base import (ListingRef, StoreAuthError, StoreError, StoreNotFound, StoreProduct,  # noqa: E402
                                   StoreRateLimited, StoreValidationError)
from store_connectors import http as shttp  # noqa: E402
from fake_tiendanube import FakeTiendanube  # noqa: E402

STORE = "123456"
TOKEN = "tn_token_valido_123"
EMAIL = "ayuda@dropfinder.test"
C = tiendanube.TiendanubeConnector
OWN_TAGS = ("dropfinder", "dfi-abc123")


class Base(unittest.TestCase):
    def setUp(self):
        self.fake = FakeTiendanube().start()
        self.st = self.fake.add_store(STORE)
        self.sleeps = []
        p = [mock.patch.object(C, "API_URL_TEMPLATE", self.fake.api_template),
             mock.patch.object(tiendanube, "_sleep", side_effect=lambda s: self.sleeps.append(s))]
        for x in p:
            x.start()
            self.addCleanup(x.stop)
        self.addCleanup(self.fake.stop)
        tiendanube.clear_caches()
        self.addCleanup(tiendanube.clear_caches)
        old_loop, shttp.ALLOW_LOOPBACK = shttp.ALLOW_LOOPBACK, True          # solo para el servidor falso local
        self.addCleanup(lambda: setattr(shttp, "ALLOW_LOOPBACK", old_loop))

    def conn(self, token=TOKEN, email=EMAIL, domain=STORE):
        return C(domain, {"access_token": token, "contact_email": email})

    def product(self, **kw):
        d = dict(title="Lámpara <LED> & más", price=19.9, sku="DF-1", stock=7, description="Línea 1\nLínea <b>2</b>",
                 image_urls=["https://cdn.x.com/a.jpg", "http://x.com/b.jpg", "javascript:alert(1)", "https://u:p@x.com/c.jpg"],
                 compare_at_price=None, vendor="Prov", product_type="Hogar", tags=list(OWN_TAGS), cost=7.5)
        d.update(kw)
        return StoreProduct(**d)

    def products(self):
        return self.fake.products(STORE)

    def assertNoRequests(self):
        self.assertEqual(self.fake.requests, [], self.fake.summary())

    def publish(self, **kw):
        return self.conn().publish(self.product(**kw))


class DomainTest(unittest.TestCase):
    def test_valid(self):
        for raw, exp in [("123456", "123456"), ("  123456\n", "123456"), ("123", "123"), ("999999999999", "999999999999"),
                         ("https://www.tiendanube.com/stores/123456/products", "123456"),
                         ("https://mitienda.mitiendanube.com/admin/v2/654321/products/98765", "654321"),
                         ("http://x.com/admin/v2/123456", "123456"), ("https://x.com/stores/123456?a=1#b", "123456"),
                         ("mi tienda es la 123456", "123456"), ("id: 123456 (123456)", "123456")]:
            self.assertEqual(C.normalize_domain(raw), exp, raw)

    def test_invalid(self):
        bad = ["", None, 123456, "abc", "tienda", "12", "1", "0", "1234567890123", "99999999999999999999", "-123456",
               "- 123456", "+123456", "0123456", "123456.5", "123.456", "123456 654321", "https://x.com/stores/123456/stores/654321",
               "٣٤٥٦٧٨", "１２３４５６", "12345​6", "123456\x00", "123456\n654321", "ftp://x.com/stores/123456",
               "https://x.com/stores/12", "javascript:alert(123456)", "user@x.com 123456", "123456%2f", "1234\\56",
               "x" * 400 + "123456", "https://x.com/stores/", "https://x.com/stores/abc", "1e6", "0x1e240", "12 34 56"]
        for raw in bad:
            with self.assertRaises(StoreValidationError, msg=repr(raw)):
                C.normalize_domain(raw)

    def test_host_cannot_be_influenced(self):
        self.assertTrue(C.API_URL_TEMPLATE.startswith("https://api.tiendanube.com/v1/"))
        for raw in ("https://evil.com/stores/123456", "evil.com:8080/stores/123456", "http://127.0.0.1/stores/123456"):
            self.assertEqual(C.normalize_domain(raw), "123456")          # solo queda el número: nunca un host


class CredentialsTest(unittest.TestCase):
    def test_registered_and_fields(self):
        self.assertIs(sc.get_class("tiendanube"), C)
        self.assertEqual((C.platform, C.label), ("tiendanube", "Tiendanube"))
        self.assertTrue(C.idempotent_publish)
        f = {x["key"]: x for x in C.credential_fields}
        self.assertEqual(set(f), {"access_token", "contact_email"})
        self.assertTrue(f["access_token"]["secret"])
        self.assertFalse(f["contact_email"]["secret"])
        for w in ("puede cambiar", "write_products", "ID", "access_token", "stores/123456"):
            self.assertIn(w, C.setup_help)

    def test_valid(self):
        C.check_credentials({"access_token": "abcdef123456", "contact_email": "a.b+c@x.co"})

    def test_invalid(self):
        ok = {"access_token": "abcdef123456", "contact_email": "a@x.co"}
        bad = [{}, {"access_token": "abcdef123456"}, {"contact_email": "a@x.co"}, {**ok, "access_token": "corto"},
               {**ok, "access_token": "con espacio y\nsalto"}, {**ok, "access_token": "x" * 501},
               {**ok, "contact_email": "sin-arroba"}, {**ok, "contact_email": "a@b"}, {**ok, "contact_email": "a b@x.co"},
               {**ok, "contact_email": "a@x.co\r\nX-Evil: 1"}, {**ok, "contact_email": "a@x.co>"},
               {**ok, "contact_email": "ñ@x.co"}, {**ok, "contact_email": "a@x..co"}, {**ok, "contact_email": "@x.co"},
               {**ok, "contact_email": "a@x.co " + "y" * 300}, {**ok, "contact_email": "(a)@x.co"},
               {**ok, "contact_email": ""}, {"access_token": None, "contact_email": None}]
        for creds in bad:
            with self.assertRaises(StoreValidationError, msg=creds):
                C.check_credentials(creds)

    def test_error_does_not_echo_secrets(self):
        try:
            C.check_credentials({"access_token": "tn_secreto\nmalo", "contact_email": "a@x.co"})
        except StoreValidationError as e:
            self.assertNotIn("tn_secreto", str(e))


class ValidateTest(Base):
    def test_ok(self):
        self.st["name"] = {"es": "Mi Tienda", "pt": "Minha Loja"}
        self.st["currency"] = "COP"
        info = self.conn().validate()
        self.assertEqual((info.name, info.currency, info.domain, info.scopes, info.missing_scopes),
                         ("Mi Tienda", "COP", STORE, [], []))
        r = self.fake.requests_for("store")[0]
        self.assertEqual(r["headers"]["authentication"], f"bearer {TOKEN}")
        self.assertEqual(r["headers"]["user-agent"], f"DropFinder ({EMAIL})")
        self.assertEqual(r["path"], f"/v1/{STORE}/store")
        self.assertEqual(self.fake.ops(), ["store", "products_list"])

    def test_multilingual_name(self):
        for name, exp in [({"pt": "Loja", "es": "Tienda", "en": "Shop"}, "Tienda"), ({"pt": "Loja", "en": "Shop"}, "Loja"),
                          ({"en": "Shop", "fr": "Boutique"}, "Shop"), ({"fr": "Boutique"}, "Boutique"),
                          ({"es": "   ", "pt": "Loja"}, "Loja"), ({"es": 5, "en": "Shop"}, "Shop"),
                          ("Solo texto", "Solo texto"), ({}, STORE), (None, STORE), ({"es": "A\x07\nB"}, "A  B"),
                          ({"es": "x" * 500}, "x" * 100)]:
            self.st["name"] = name
            self.assertEqual(self.conn().validate().name, exp, name)

    def test_name_is_scrubbed_of_tokens(self):
        self.st["name"] = {"es": f"Tienda {TOKEN}"}
        self.assertEqual(self.conn().validate().name.count(TOKEN), 1)      # el nombre se muestra tal cual (no es un error)
        # pero un error de la tienda nunca repite el token
        self.fake.inject("http422", body={"description": f"boom {TOKEN}"})
        with self.assertRaises(StoreError) as cm:
            self.conn().validate()
        self.assertNotIn(TOKEN, str(cm.exception))

    def test_empty_store_list_404_is_ok(self):
        self.assertEqual(self.products(), {})
        self.assertEqual(self.conn().validate().domain, STORE)

    def test_auth_errors(self):
        with self.assertRaises(StoreAuthError):
            self.conn(token="incorrecto_999").validate()
        self.st["scopes"] = set()
        with self.assertRaises(StoreAuthError):
            self.conn().validate()                               # sin permiso de lectura de productos

    def test_missing_store_is_validation_error(self):
        with self.assertRaises(StoreValidationError) as cm:
            self.conn(domain="999999").validate()
        self.assertNotIsInstance(cm.exception, StoreNotFound)
        self.assertIn("ID de la tienda", str(cm.exception))

    def test_bad_responses(self):
        self.st["currency"] = "dólares"
        with self.assertRaises(StoreError) as cm:
            self.conn().validate()
        self.assertFalse(cm.exception.retryable)
        self.st["currency"] = "USD"
        self.fake.inject("malformed")
        with self.assertRaises(StoreError) as cm:
            self.conn().validate()
        self.assertTrue(cm.exception.retryable)

    def test_domain_from_pasted_url_is_normalized_at_call(self):
        self.assertEqual(self.conn(domain="https://x.com/stores/123456/p").validate().domain, STORE)


class PublishTest(Base):
    def test_exact_create_body(self):
        ref = self.publish(active=False)
        r = self.fake.requests_for("product_create")[0]
        self.assertEqual(r["method"], "POST")
        self.assertEqual(r["path"], f"/v1/{STORE}/products")
        self.assertEqual(r["headers"]["content-type"], "application/json")
        self.assertEqual(r["body"], {
            "name": {"es": "Lámpara <LED> & más"},
            "description": {"es": "Línea 1<br>Línea &lt;b&gt;2&lt;/b&gt;"},
            "published": False, "brand": "Prov", "tags": "dropfinder,dfi-abc123",
            "images": [{"src": "https://cdn.x.com/a.jpg"}],
            "variants": [{"price": "19.90", "stock": 7, "stock_management": True, "sku": "DF-1",
                           "cost": "7.50"}]})
        self.assertIs(r["body"]["published"], False)
        self.assertEqual(self.fake.ops(), ["store", "sku_lookup", "product_create"])
        p = list(self.products().values())[0]
        self.assertEqual((ref.external_id, ref.variant_id), (str(p["id"]), str(p["variants"][0]["id"])))
        self.assertEqual(ref.inventory_item_id, "")
        self.assertTrue(ref.url.startswith("https://") and ref.external_id in ref.url and STORE in ref.url)

    def test_always_created_unpublished_then_published_when_active(self):
        ref = self.publish(active=True)
        self.assertIs(self.fake.requests_for("product_create")[0]["body"]["published"], False)
        up = self.fake.requests_for("product_update")
        self.assertEqual([(u["method"], u["path"], u["body"]) for u in up],
                         [("PUT", f"/v1/{STORE}/products/{ref.external_id}", {"published": True})])
        self.assertTrue(self.products()[int(ref.external_id)]["published"])
        ref2 = self.publish(active=False, sku="DF-2")
        self.assertFalse(self.products()[int(ref2.external_id)]["published"])
        self.assertEqual(len(self.fake.requests_for("product_update")), 1)

    def test_minimal_product(self):
        self.publish(title="X", description="", vendor="", tags=[], image_urls=[], cost=None, compare_at_price=None)
        self.assertEqual(self.fake.requests_for("product_create")[0]["body"],
                         {"name": {"es": "X"}, "description": {"es": ""}, "published": False,
                          "variants": [{"price": "19.90", "stock": 7, "stock_management": True, "sku": "DF-1"}]})

    def test_promotional_only_when_lower(self):
        self.publish(compare_at_price=15.5)
        self.assertEqual(self.fake.requests_for("product_create")[0]["body"]["variants"][0]["promotional_price"], "15.50")
        for i, c in enumerate((19.9, 29.9, 19.899)):
            self.publish(sku=f"S{i}", compare_at_price=c)
            self.assertNotIn("promotional_price", self.fake.requests_for("product_create")[-1]["body"]["variants"][0])

    def test_images_https_only_and_capped(self):
        urls = [f"https://cdn.x.com/{i}.jpg" for i in range(15)]
        self.publish(image_urls=["http://a.com/x.jpg", "ftp://a.com/x", "https://1.2.3.4/x.jpg", "https://[::1]/x",
                                 "https://ü.com/x.jpg", "https://a.com/x y.jpg", "https://a.com/" + "x" * 2100,
                                 "https://u@a.com/x", "//a.com/x", "https://localhost/x", 5, None, *urls])
        imgs = self.fake.requests_for("product_create")[0]["body"]["images"]
        self.assertEqual(imgs, [{"src": u} for u in urls[:10]])
        self.publish(sku="D2", image_urls="https://cdn.x.com/solo.jpg")
        self.assertEqual(self.fake.requests_for("product_create")[-1]["body"]["images"], [{"src": "https://cdn.x.com/solo.jpg"}])
        self.publish(sku="D3", image_urls=["http://a.com/x"])
        self.assertNotIn("images", self.fake.requests_for("product_create")[-1]["body"])

    def test_sku_in_path_is_encoded(self):
        self.publish(sku="DF.1_x-9")
        r = self.fake.requests_for("sku_lookup")[0]
        self.assertEqual(r["path"], f"/v1/{STORE}/products/sku/DF.1_x-9")
        self.assertEqual(list(self.products().values())[0]["variants"][0]["sku"], "DF.1_x-9")

    def test_description_escaped(self):
        self.publish(description="<script>alert(1)</script>\r\n\"q\" & 'a'")
        d = self.fake.requests_for("product_create")[0]["body"]["description"]["es"]
        self.assertNotIn("<script>", d)
        self.assertIn("&lt;script&gt;", d)
        self.assertIn("<br>", d)


class IdempotentPublishTest(Base):
    def seed(self, sku="DF-1", published=False, extra_variant=False):
        ref = self.publish(sku=sku, active=published)
        self.fake.requests.clear()
        return ref

    def test_adopts_existing_sku_without_creating(self):
        ref0 = self.seed()
        ref = self.publish(price=25, stock=3, active=False)
        self.assertEqual((ref.external_id, ref.variant_id), (ref0.external_id, ref0.variant_id))
        self.assertEqual(len(self.products()), 1)
        self.assertEqual(self.fake.ops(), ["sku_lookup", "variant_update"])
        v = list(self.products().values())[0]["variants"][0]
        self.assertEqual((v["price"], v["stock"]), ("25.00", 3))
        self.assertEqual(self.fake.requests_for("variant_update")[0]["body"], {"price": "25.00", "stock": 3, "stock_management": True})

    def test_adopt_and_publish_when_active(self):
        ref = self.seed()
        self.publish(active=True)
        self.assertTrue(self.products()[int(ref.external_id)]["published"])
        self.assertEqual(self.fake.ops(), ["sku_lookup", "variant_update", "product_update"])

    def test_adopt_already_published_does_not_republish(self):
        self.seed(published=True)
        self.publish(active=True)
        self.assertEqual(self.fake.ops(), ["sku_lookup", "variant_update"])

    def test_adopt_matches_the_sku_variant_in_multi_variant_product(self):
        ref = self.seed()
        p = self.products()[int(ref.external_id)]
        p["variants"].insert(0, {"id": 777, "product_id": p["id"], "price": "1.00", "stock": 1, "sku": "OTRO",
                                 "cost": None, "promotional_price": None})
        out = self.publish(active=False)
        self.assertEqual(out.variant_id, ref.variant_id)

    def test_lookup_ignores_product_without_that_sku(self):
        self.seed()
        self.fake.stores[STORE]["products"].clear()
        with mock.patch.object(C, "_call", return_value={"id": 5, "variants": [{"id": 6, "sku": "OTRO"}]}) as c:
            self.assertIsNone(self.conn()._find_existing("DF-1", OWN_TAGS))
        own = "dropfinder,dfi-abc123"
        with mock.patch.object(C, "_call", return_value=[{"id": 5, "tags": own, "variants": [{"id": 6, "sku": "DF-1"}]}]):
            self.assertEqual(self.conn()._find_existing("DF-1", OWN_TAGS)["id"], 5)
        with mock.patch.object(C, "_call", return_value=None):
            self.assertIsNone(self.conn()._find_existing("DF-1", OWN_TAGS))
        with mock.patch.object(C, "_call", return_value={"id": "x", "tags": own, "variants": [{"id": 6, "sku": "DF-1"}]}):
            self.assertIsNone(self.conn()._find_existing("DF-1", OWN_TAGS))

    def test_lost_response_on_create_then_retry_gives_one_product(self):
        self.fake.inject("execute_then_drop", op="product_create")
        with self.assertRaises(StoreError) as cm:
            self.publish(active=True)
        self.assertTrue(cm.exception.retryable)
        self.assertFalse(cm.exception.not_sent)
        self.assertEqual(len(self.products()), 1)
        ref = self.publish(active=True)
        self.assertEqual(len(self.products()), 1)
        self.assertEqual(ref.external_id, str(list(self.products())[0]))
        self.assertTrue(list(self.products().values())[0]["published"])
        self.assertEqual(len(self.fake.requests_for("product_create")), 1)

    def test_lost_response_on_publish_step(self):
        self.fake.inject("execute_then_drop", op="product_update")
        with self.assertRaises(StoreError):
            self.publish(active=True)
        # la limpieza borró lo que creó esta llamada; el reintento crea uno solo
        self.publish(active=True)
        self.assertEqual(len(self.products()), 1)
        self.assertTrue(list(self.products().values())[0]["published"])

    def test_throttled_mid_publish_gives_one_product(self):
        self.fake.inject("http429", op="product_update", count=2, reset_ms=2000)   # agota la espera interna
        with self.assertRaises(StoreRateLimited) as cm:
            self.publish(active=True)
        self.assertEqual(cm.exception.retry_after, 2.0)
        self.assertEqual(len(self.products()), 0)                # limpieza: nada huérfano
        self.publish(active=True)
        self.assertEqual(len(self.products()), 1)
        self.assertEqual(len(self.fake.requests_for("product_create")), 2)

    def test_throttled_on_create_recovers_inline(self):
        self.fake.inject("http429", op="product_create", count=1, reset_ms=2000)
        self.publish()
        self.assertEqual(self.sleeps, [2.0])
        self.assertEqual(len(self.products()), 1)

    def test_throttled_on_lookup_then_create_once(self):
        self.fake.inject("http429", op="sku_lookup", count=2, reset_ms=1000)
        with self.assertRaises(StoreRateLimited):
            self.publish()
        self.assertEqual(self.products(), {})
        self.publish()
        self.assertEqual(len(self.products()), 1)

    def test_partial_failure_cleans_up_orphan(self):
        self.fake.inject("http500", op="product_update")
        with self.assertRaises(StoreError) as cm:
            self.publish(active=True)
        self.assertTrue(cm.exception.retryable)
        self.assertFalse(hasattr(cm.exception, "orphan_external_id"))
        self.assertEqual(self.products(), {})
        self.assertEqual(len(self.fake.requests_for("product_delete")), 1)

    def test_cleanup_failure_exposes_orphan_and_next_publish_adopts(self):
        self.fake.inject("http500", op="product_update")
        self.fake.inject("http500", op="product_delete", count=tiendanube.CLEANUP_TRIES)
        with self.assertRaises(StoreError) as cm:
            self.publish(active=True)
        pid = next(iter(self.products()))
        self.assertEqual(cm.exception.orphan_external_id, str(pid))
        self.assertIn(str(pid), str(cm.exception))
        self.assertEqual(len(self.fake.requests_for("product_delete")), tiendanube.CLEANUP_TRIES)
        ref = self.publish(active=True)                           # adopta el huérfano: sigue habiendo uno
        self.assertEqual((len(self.products()), ref.external_id), (1, str(pid)))
        self.assertEqual(len(self.fake.requests_for("product_create")), 1)

    def test_cleanup_retries_honouring_retry_after(self):
        self.fake.inject("http500", op="product_update")
        self.fake.inject("http429", op="product_delete", count=1, reset_ms=60000)
        self.fake.inject("http503", op="product_delete", count=1)
        with self.assertRaises(StoreError) as cm:
            self.publish(active=True)
        self.assertFalse(hasattr(cm.exception, "orphan_external_id"))
        self.assertEqual(self.products(), {})
        self.assertEqual(self.sleeps[0], tiendanube.CLEANUP_MAX_SLEEP)      # 60 s acotado a 10
        self.assertEqual(len(self.fake.requests_for("product_delete")), 3)

    def test_cleanup_does_not_retry_non_retryable(self):
        self.fake.inject("http500", op="product_update")
        self.fake.inject("http403", op="product_delete", count=5)
        with self.assertRaises(StoreError) as cm:
            self.publish(active=True)
        self.assertEqual(len(self.fake.requests_for("product_delete")), 1)
        self.assertTrue(hasattr(cm.exception, "orphan_external_id"))

    def test_cleanup_when_product_already_gone(self):
        self.fake.inject("http500", op="product_update")
        orig = C._cleanup_orphan

        def gone(self_, pid, e):
            self.products().pop(int(pid), None)
            return orig(self_, pid, e)
        with mock.patch.object(C, "_cleanup_orphan", gone):
            with self.assertRaises(StoreError) as cm:
                self.publish(active=True)
        self.assertFalse(hasattr(cm.exception, "orphan_external_id"))

    def test_adopted_product_is_never_deleted_on_failure(self):
        self.seed()
        self.fake.inject("http500", op="variant_update")
        with self.assertRaises(StoreError) as cm:
            self.publish()
        self.assertTrue(cm.exception.retryable)
        self.assertEqual(len(self.products()), 1)
        self.assertEqual(self.fake.requests_for("product_delete"), [])

    def test_vanished_during_publish(self):
        self.seed()
        self.fake.inject("http404", op="variant_update")
        with self.assertRaises(StoreError) as cm:
            self.publish()
        self.assertNotIsInstance(cm.exception, StoreNotFound)
        self.assertTrue(cm.exception.retryable)
        self.assertEqual(len(self.products()), 1)

    def test_create_without_id_is_retryable_error(self):
        with mock.patch.object(C, "_call", side_effect=[None, {"variants": []}]):
            with self.assertRaises(StoreError) as cm:
                self.publish()
        self.assertTrue(cm.exception.retryable)


class UpdateTest(Base):
    def setUp(self):
        super().setUp()
        self.ref = self.publish(sku="U-1", active=True)
        self.fake.requests.clear()

    def test_update_body_and_state(self):
        self.conn().update_price_stock(self.ref, 33.333, 4)
        r = self.fake.requests_for("variant_update")
        self.assertEqual([(x["method"], x["path"], x["body"]) for x in r],
                         [("PUT", f"/v1/{STORE}/products/{self.ref.external_id}/variants/{self.ref.variant_id}",
                           {"price": "33.33", "stock": 4, "stock_management": True})])
        self.assertNotIn("promotional_price", r[0]["raw"].decode())      # nunca null ni presente si no se pasó
        v = list(self.products().values())[0]["variants"][0]
        self.assertEqual((v["price"], v["stock"]), ("33.33", 4))

    def test_never_erases_manual_promotional_price(self):
        v = list(self.products().values())[0]["variants"][0]
        v["promotional_price"] = "5.00"
        self.conn().update_price_stock(self.ref, 30, 2)
        self.conn().update_price_stock(self.ref, 30, 2, compare_at_price=30)      # no menor: no se envía
        self.conn().update_price_stock(self.ref, 30, 2, compare_at_price=45)
        for r in self.fake.requests_for("variant_update"):
            self.assertNotIn("promotional_price", r["body"])
        self.assertEqual(v["promotional_price"], "5.00")

    def test_promotional_sent_when_lower(self):
        self.conn().update_price_stock(self.ref, 30, 2, compare_at_price=24.5)
        self.assertEqual(self.fake.requests_for("variant_update")[0]["body"],
                         {"price": "30.00", "stock": 2, "stock_management": True,
                          "promotional_price": "24.50"})

    def test_set_active_toggles(self):
        c = self.conn()
        c.set_active(self.ref, False)
        self.assertFalse(self.products()[int(self.ref.external_id)]["published"])
        c.set_active(self.ref, True)
        self.assertTrue(self.products()[int(self.ref.external_id)]["published"])
        self.assertEqual([r["body"] for r in self.fake.requests_for("product_update")], [{"published": False}, {"published": True}])

    def test_delete_and_idempotent_delete(self):
        c = self.conn()
        c.delete(self.ref)
        self.assertEqual(self.products(), {})
        c.delete(self.ref)                                         # ya no existe: éxito
        self.assertEqual(self.fake.requests_for("product_delete")[0]["method"], "DELETE")

    def test_missing_product_is_not_found(self):
        self.products().clear()
        for fn in (lambda c: c.update_price_stock(self.ref, 10, 1), lambda c: c.set_active(self.ref, False)):
            with self.assertRaises(StoreNotFound):
                fn(self.conn())

    def test_missing_store_is_validation_not_notfound(self):
        c = self.conn(domain="999999")
        ref = ListingRef("5001", "5002")
        for fn in (lambda: c.update_price_stock(ref, 10, 1), lambda: c.set_active(ref, True), lambda: c.delete(ref)):
            with self.assertRaises(StoreValidationError) as cm:
                fn()
            self.assertNotIsInstance(cm.exception, StoreNotFound)

    def test_endpoint_404_is_not_notfound(self):
        for fn in (lambda c: c.delete(self.ref), lambda c: c.set_active(self.ref, True),
                   lambda c: c.update_price_stock(self.ref, 10, 1)):
            self.fake.inject("http404", count=2)                   # el producto Y la comprobación de tienda: ruta inexistente
            with self.assertRaises(StoreValidationError) as cm:
                fn(self.conn())
            self.assertNotIsInstance(cm.exception, StoreNotFound)
        self.assertEqual(len(self.products()), 1)

    def test_delete_does_not_swallow_other_errors(self):
        for kind, exc in (("http500", StoreError), ("http401", StoreAuthError), ("http403", StoreAuthError)):
            self.fake.inject(kind, op="product_delete")
            with self.assertRaises(exc):
                self.conn().delete(self.ref)
        self.assertEqual(len(self.products()), 1)

    def test_lost_response_on_delete_then_retry_succeeds(self):
        self.fake.inject("execute_then_drop", op="product_delete")
        with self.assertRaises(StoreError) as cm:
            self.conn().delete(self.ref)
        self.assertTrue(cm.exception.retryable)
        self.assertEqual(self.products(), {})
        self.conn().delete(self.ref)

    def test_bad_refs_rejected_before_any_call(self):
        c = self.conn()
        for ref in (ListingRef(""), ListingRef("abc"), ListingRef("1/../2"), ListingRef("12 3"), ListingRef("1" * 25),
                    ListingRef("5001", ""), ListingRef("5001", "x")):
            with self.assertRaises(StoreValidationError):
                c.update_price_stock(ref, 10, 1)
        for ref in (ListingRef(""), ListingRef("abc"), ListingRef("1?x=1")):
            with self.assertRaises(StoreValidationError):
                c.delete(ref)
            with self.assertRaises(StoreValidationError):
                c.set_active(ref, True)
        self.assertNoRequests()

    def test_update_validation_before_call(self):
        for price, stock in ((0, 1), (-5, 1), (float("nan"), 1), (10, -1), (10, True), (10, 10**10), (10, "3")):
            with self.assertRaises(StoreValidationError, msg=(price, stock)):
                self.conn().update_price_stock(self.ref, price, stock)
        with self.assertRaises(StoreValidationError):
            self.conn().update_price_stock(self.ref, 10, 1, compare_at_price=float("inf"))
        self.assertNoRequests()


class ErrorMappingTest(Base):
    """Tabla del ADR 0003 §4 (set_active sobre un producto existente)."""

    def setUp(self):
        super().setUp()
        self.ref = self.publish(sku="E-1")
        self.fake.requests.clear()

    def run_kind(self, kind, **kw):
        self.fake.inject(kind, op="product_update", **kw)
        with self.assertRaises(StoreError) as cm:
            self.conn().set_active(self.ref, True)
        return cm.exception

    def test_table(self):
        table = [("http401", StoreAuthError, False), ("http403", StoreAuthError, False),
                 ("http400", StoreValidationError, False), ("http422", StoreValidationError, False),
                 ("http402", StoreError, False), ("http423", StoreError, False), ("http409", StoreError, False),
                 ("http418", StoreError, False), ("http408", StoreError, True), ("http500", StoreError, True),
                 ("http502", StoreError, True), ("http503", StoreError, True), ("http504", StoreError, True),
                 ("malformed", StoreError, True), ("notjson200", StoreError, True), ("redirect", StoreError, False),
                 ("oversize", StoreError, False)]
        for kind, exc, retryable in table:
            kw = {"size": 6 * 1024 * 1024} if kind == "oversize" else {}
            e = self.run_kind(kind, **kw)
            self.assertIs(type(e), exc, kind)
            self.assertEqual(e.retryable, retryable, kind)
            self.assertFalse(e.not_sent, kind)
        self.assertEqual(self.fake.evil_hits, 0)

    def test_429(self):
        e = self.run_kind("http429", count=2, reset_ms=45000)
        self.assertIs(type(e), StoreRateLimited)
        self.assertTrue(e.retryable)
        self.assertEqual(e.retry_after, 45.0)
        self.assertEqual(self.sleeps, [])                          # demasiado largo para esperar dentro de la llamada

    def test_retry_after_parsing(self):
        for kw, exp in [({"reset_ms": 45000}, 45.0), ({"reset_ms": 200}, 1.0), ({"reset_ms": 10**9}, 120.0),
                        ({"reset_ms": "abc"}, 5.0), ({"reset_ms": "nan"}, 5.0), ({"reset_ms": -5}, 1.0),
                        ({}, 5.0), ({"retry_after": 30, "reset_ms": 90000}, 30.0), ({"retry_after": 99999}, 120.0),
                        ({"retry_after": "x", "reset_ms": 20000}, 20.0)]:
            self.sleeps.clear()
            self.fake.injections.clear()
            e = self.run_kind("http429", count=2, **kw)
            self.assertEqual(e.retry_after, exp, kw)
            self.assertEqual(self.sleeps, [exp] if exp <= tiendanube.MAX_INLINE_WAIT else [], kw)

    def test_404_of_product_is_notfound_only_if_store_exists(self):
        e = self.run_kind("http404")
        self.assertIs(type(e), StoreNotFound)
        self.assertEqual(self.fake.ops()[-2:], ["product_update", "store"])

    def test_422_text_comes_from_store_and_is_bounded(self):
        e = self.run_kind("http422", body={"description": "Precio inválido", "price": ["debe ser > 0"]})
        self.assertIn("Precio inválido", str(e))
        self.assertIn("price: debe ser > 0", str(e))
        e = self.run_kind("http422", body={"description": "x" * 5000})
        self.assertLessEqual(len(str(e)), 400)

    def test_network_errors(self):
        with mock.patch.object(C, "TIMEOUT", 0.2):
            self.fake.inject("sleep", op="product_update", seconds=0.8)
            with self.assertRaises(StoreError) as cm:
                self.conn().set_active(self.ref, True)
        self.assertTrue(cm.exception.retryable)
        self.assertFalse(cm.exception.not_sent)                    # ambiguo: pudo aplicarse
        self.fake.stop()
        with self.assertRaises(StoreError) as cm:
            self.conn().set_active(self.ref, True)
        self.assertTrue(cm.exception.retryable)
        self.assertTrue(cm.exception.not_sent)                     # no se pudo conectar: no se envió nada

    def test_dns_failure_is_not_sent(self):
        shttp.ALLOW_LOOPBACK = False
        with mock.patch.object(C, "API_URL_TEMPLATE", "https://api.tiendanube.com/v1/{store}{path}"), \
                mock.patch("socket.getaddrinfo", side_effect=OSError("dns")):
            with self.assertRaises(StoreError) as cm:
                self.conn().validate()
        self.assertTrue(cm.exception.retryable and cm.exception.not_sent)

    def test_auth_message_has_no_token(self):
        with self.assertRaises(StoreAuthError) as cm:
            self.conn(token="incorrecto_999").validate()
        self.assertNotIn("incorrecto_999", str(cm.exception))

    def test_only_store_errors_escape(self):
        c = self.conn()
        with mock.patch.object(C, "_call", side_effect=ValueError("boom")):
            with self.assertRaises(StoreValidationError):
                c.set_active(self.ref, True)
        with mock.patch.object(C, "_call", side_effect=KeyError("x")):
            with self.assertRaises(StoreValidationError):
                c.validate()


class SharedClientTest(Base):
    """El conector usa el cliente compartido: sin él no hay IP fijada ni filtros anti-SSRF."""

    def _real(self, template="https://api.tiendanube.com/v1/{store}{path}"):
        shttp.ALLOW_LOOPBACK = False
        return mock.patch.object(C, "API_URL_TEMPLATE", template)

    def test_private_resolution_refused(self):
        for ip in ("10.0.0.5", "127.0.0.1", "169.254.169.254", "192.168.1.9", "::1", "::ffff:10.0.0.1", "100.64.0.1"):
            fam = 10 if ":" in ip else 2
            with self._real(), mock.patch("socket.getaddrinfo", return_value=[(fam, 1, 6, "", (ip, 443))]):
                with self.assertRaises(StoreError, msg=ip) as cm:
                    self.conn().validate()
            self.assertFalse(cm.exception.retryable)
        self.assertNoRequests()

    def test_mixed_resolution_refused(self):
        with self._real(), mock.patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("8.8.8.8", 443)),
                                                                          (2, 1, 6, "", ("10.0.0.1", 443))]):
            with self.assertRaises(StoreError):
                self.conn().validate()

    def test_non_443_port_refused(self):
        with self._real("https://api.tiendanube.com:8443/v1/{store}{path}"), mock.patch("socket.getaddrinfo") as gai:
            with self.assertRaises(StoreError):
                self.conn().validate()
        gai.assert_not_called()

    def test_plain_http_refused_without_loopback_flag(self):
        shttp.ALLOW_LOOPBACK = False
        with self.assertRaises(StoreError) as cm:
            self.conn().validate()
        self.assertFalse(cm.exception.retryable)
        self.assertNoRequests()
        with self._real("http://api.tiendanube.com/v1/{store}{path}"):
            with self.assertRaises(StoreError):
                self.conn().validate()

    def test_redirect_refused(self):
        self.fake.inject("redirect")
        with self.assertRaises(StoreError) as cm:
            self.conn().validate()
        self.assertFalse(cm.exception.retryable)
        self.assertEqual(self.fake.evil_hits, 0)

    def test_uses_shared_client_only(self):
        with open(tiendanube.__file__, encoding="utf-8") as fh:
            src = fh.read()
        code = "\n".join(l for l in src.splitlines() if l.lstrip().startswith(("import ", "from ")))
        for banned in ("urllib.request", "http.client", "socket", "requests", "ssl"):
            self.assertNotIn(banned, code)
        calls = []
        real = shttp.request_json
        with mock.patch.object(shttp, "request_json", side_effect=lambda *a, **k: (calls.append(a), real(*a, **k))[1]):
            self.conn().validate()
        self.assertEqual([c[0] for c in calls], ["GET", "GET"])


class SecretsTest(Base):
    def test_no_secrets_in_errors_or_logs(self):
        records = []

        class H(logging.Handler):
            def emit(self, r):
                records.append(r.getMessage() + repr(r.args))
        h = H()
        root = logging.getLogger()
        old = root.level
        root.addHandler(h)
        root.setLevel(logging.DEBUG)
        self.addCleanup(lambda: (root.removeHandler(h), root.setLevel(old)))
        secrets = (TOKEN, "tn_incorrecto_1", "0123456789abcdef0123456789abcdef01234567")
        msgs = []

        def grab(fn):
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                msgs.append(str(e) + repr(e) + repr(e.args) + repr(vars(e)))
        grab(lambda: self.conn(token="tn_incorrecto_1").validate())
        self.fake.inject("http500", op="product_update")
        self.fake.inject("http500", op="product_delete", count=3)
        grab(lambda: self.publish(active=True))
        for kind in ("http422", "http400", "http500", "http409", "http404"):
            self.fake.inject(kind, body={"description": f"eco {TOKEN} y {secrets[2]} Authentication: bearer {TOKEN}",
                                         "message": f"token={secrets[2]}"})
            grab(lambda: self.conn().validate())
        self.st["scopes"].discard("write_products")
        grab(lambda: self.publish(sku="Z"))
        self.fake.inject("malformed")
        grab(lambda: self.conn().validate())
        self.assertTrue(msgs)
        for text in msgs + records:
            for s in secrets:
                self.assertNotIn(s, text)

    def test_repr_and_str_hide_credentials(self):
        c = self.conn()
        for text in (repr(c), str(c), f"{c!r}", repr([c]), repr({"c": c})):
            self.assertNotIn(TOKEN, text)
            self.assertNotIn(EMAIL, text)
        self.assertIn(STORE, repr(c))

    def test_redact_echoed_secret(self):
        self.assertNotIn(TOKEN, self.conn()._redact(f"error con {TOKEN} dentro"))

    def test_store_text_is_scrubbed(self):
        evil = f"boom {TOKEN} abcdefghijklmnopqrstuvwxyz0123456789ABCDEF\x07 fin"
        self.fake.inject("http422", body={"description": evil})
        with self.assertRaises(StoreValidationError) as cm:
            self.conn().validate()
        text = str(cm.exception)
        self.assertIn("boom", text)
        for bad in (TOKEN, "abcdefghijklmnopqrstuvwxyz0123456789ABCDEF", "\x07"):
            self.assertNotIn(bad, text)

    def test_token_only_sent_in_header(self):
        self.publish()
        for r in self.fake.requests:
            self.assertNotIn(TOKEN, r["path"])
            self.assertNotIn(TOKEN.encode(), r["raw"])
        self.assertNotIn(TOKEN, " ".join(self.fake.summary()))


class InputValidationTest(Base):
    def bad(self, **kw):
        with self.assertRaises(StoreValidationError, msg=kw):
            self.conn().publish(self.product(**kw))
        self.assertNoRequests()

    def test_price(self):
        for p in (0, 0.0, -1, -0.01, 0.004, float("nan"), float("inf"), float("-inf"), "abc", None, True, 1e10,
                  1_000_000_000.01, [], "1e400"):
            self.bad(price=p)

    def test_price_rounding(self):
        for p, exp in ((0.005, "0.01"), (19.995, "20.00"), ("19.9", "19.90"), (1_000_000_000, "1000000000.00"), (5, "5.00")):
            self.fake.requests.clear()
            self.products().clear()
            self.publish(price=p)
            self.assertEqual(self.fake.requests_for("product_create")[0]["body"]["variants"][0]["price"], exp, p)

    def test_stock(self):
        for s in (-1, 1_000_000_001, float("nan"), float("inf"), True, None, "5", [], 10**30):
            self.bad(stock=s)
        for s, exp in ((0, 0), (1_000_000_000, 1_000_000_000), (7.9, 7)):
            self.products().clear()
            self.publish(stock=s)
            v = self.fake.requests_for("product_create")[-1]["body"]["variants"][0]["stock"]
            self.assertEqual((v, type(v)), (exp, int))

    def test_title(self):
        for t in ("", "   ", None, "\x00\x01 \n"):
            self.bad(title=t)
        self.publish(title="T" * 400)
        self.assertEqual(self.fake.requests_for("product_create")[0]["body"]["name"], {"es": "T" * 255})
        self.publish(sku="C2", title="a\x00b\x07c\nd")
        self.assertEqual(self.fake.requests_for("product_create")[-1]["body"]["name"], {"es": "a b c d"})

    def test_sku(self):
        for s in ("", "   ", None, "\x00", "S" * 256, "S" * 400, ".", "..", "a/b", "a b", "a?x=1", "a#b", "%2e%2e", "a%2fb",
                  "..\\x", "ñandú", " DF-1", "DF-1\n", "a:b", 5):
            self.bad(sku=s)
        self.publish(sku="S" * 255)
        self.assertEqual(self.fake.requests_for("product_create")[0]["body"]["variants"][0]["sku"], "S" * 255)
        for ok in ("...", ".a", "a.", "A_b-c.9"):
            self.publish(sku=ok)

    def test_sku_path_injection_never_reaches_the_store(self):
        for s in ("..", ".", "%2e%2e", "a/b", "?x=1", "../products/1", "a/../b"):
            with self.assertRaises(StoreValidationError, msg=s):
                self.conn().publish(self.product(sku=s))
        self.assertNoRequests()

    def test_other_amounts(self):
        self.bad(compare_at_price=float("nan"))
        self.bad(compare_at_price=-3)
        self.bad(compare_at_price=0)
        self.bad(cost=-1)
        self.bad(cost=float("inf"))
        self.publish(cost=0)
        self.assertEqual(self.fake.requests_for("product_create")[0]["body"]["variants"][0]["cost"], "0.00")

    def test_tags(self):
        cases = [(["a", "b,c", " d ", "", None, 5, "x" * 100], "a,b c,d," + "x" * 60), ("solo,uno", "solo uno"), ([], None),
                 (None, None), (["a"] * 80, ",".join(["a"] * 50))]
        for i, (tags, exp) in enumerate(cases):
            self.publish(sku=f"T{i}", tags=tags)
            body = self.fake.requests_for("product_create")[-1]["body"]
            self.assertEqual(body.get("tags"), exp, tags)
            if exp is not None:
                self.assertLessEqual(len(body["tags"]), 1000)

    def test_vendor_truncated(self):
        self.publish(vendor="V" * 400)
        self.assertEqual(self.fake.requests_for("product_create")[0]["body"]["brand"], "V" * 255)

    def test_bad_credentials_rejected_before_calls(self):
        with self.assertRaises(StoreValidationError):
            C(STORE, {"access_token": TOKEN, "contact_email": "malo"}).publish(self.product())
        with self.assertRaises(StoreValidationError):
            C("abc", {"access_token": TOKEN, "contact_email": EMAIL}).publish(self.product())
        with self.assertRaises(StoreValidationError):
            C(STORE, {}).validate()
        self.assertNoRequests()

    def test_wrong_object_types_never_leak_foreign_exceptions(self):
        with self.assertRaises(StoreError):
            self.conn().publish(object())


class ConcurrencyTest(Base):
    def test_parallel_publishes_distinct_products(self):
        errs, refs = [], []

        def work(i):
            try:
                refs.append(self.conn().publish(self.product(sku=f"P-{i}", title=f"P {i}", active=i % 2 == 0)))
            except Exception as e:  # noqa: BLE001
                errs.append(e)
        ts = [threading.Thread(target=work, args=(i,)) for i in range(12)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(errs, [])
        self.assertEqual(len({r.external_id for r in refs}), 12)
        self.assertEqual(len(self.products()), 12)
        self.assertEqual(sum(1 for p in self.products().values() if p["published"]), 6)

    def test_parallel_updates_and_deletes(self):
        refs = [self.publish(sku=f"Q-{i}") for i in range(6)]
        errs = []

        def work(r):
            try:
                self.conn().update_price_stock(r, 11, 2)
                self.conn().delete(r)
                self.conn().delete(r)
            except Exception as e:  # noqa: BLE001
                errs.append(e)
        ts = [threading.Thread(target=work, args=(r,)) for r in refs]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(errs, [])
        self.assertEqual(self.products(), {})


class FakeStrictnessTest(Base):
    """El servidor falso rechaza lo que la plataforma real rechazaría: si no, las demás pruebas no valdrían nada."""

    def raw(self, method, path, body=None, headers=None, store=STORE):
        h = {"Authentication": f"bearer {TOKEN}", "User-Agent": f"DropFinder ({EMAIL})"}
        h.update(headers or {})
        h = {k: v for k, v in h.items() if v is not None}
        r = shttp.request_json(method, self.fake.api_template.format(store=store, path=path), body, headers=h)
        return r.status, (r.json() if r.body else None)

    GOOD = {"name": {"es": "X"}, "published": False, "variants": [{"price": "1.00", "stock": 1, "sku": "A"}]}

    def test_auth_and_user_agent(self):
        self.assertEqual(self.raw("POST", "/products", self.GOOD)[0], 201)
        self.assertEqual(self.raw("GET", "/store", headers={"Authentication": None})[0], 401)
        self.assertEqual(self.raw("GET", "/store", headers={"Authentication": TOKEN})[0], 401)
        self.assertEqual(self.raw("GET", "/store", headers={"Authentication": "bearer otro_token_1"})[0], 401)
        self.assertEqual(self.raw("GET", "/store", headers={"Authorization": f"bearer {TOKEN}", "Authentication": None})[0], 401)
        self.assertEqual(self.raw("GET", "/store", headers={"User-Agent": None})[0], 400)
        self.assertEqual(self.raw("GET", "/store", headers={"User-Agent": "DropFinder/1.0"})[0], 400)
        self.assertEqual(self.raw("GET", "/store", headers={"User-Agent": "DropFinder (sin-correo)"})[0], 400)

    def test_content_type_and_unknown_routes(self):
        self.assertEqual(self.raw("POST", "/products", self.GOOD, headers={"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.raw("GET", "/otra-cosa")[0], 404)
        self.assertEqual(self.raw("GET", "/store", store="999999")[0], 404)
        self.assertEqual(self.raw("DELETE", "/store")[0], 404)

    def test_body_validation(self):
        g = self.GOOD
        bad = [{**g, "name": "texto"}, {**g, "name": {}}, {**g, "name": {"es": ""}}, {**g, "name": {"es": 5}},
               {**g, "published": "false"}, {**g, "published": 0}, {k: v for k, v in g.items() if k != "published"},
               {**g, "extra": 1}, {**g, "variants": []}, {**g, "variants": "x"},
               {**g, "variants": [{"price": 1.0, "stock": 1}]}, {**g, "variants": [{"price": "1", "stock": 1}]},
               {**g, "variants": [{"price": "1.00", "stock": "1"}]}, {**g, "variants": [{"price": "1.00", "stock": True}]},
               {**g, "variants": [{"price": "1.00", "stock": -1}]}, {**g, "variants": [{"price": "0.00"}]},
               {**g, "variants": [{"price": "1.00", "bogus": 1}]}, {**g, "variants": [{"stock": 1}]},
               {**g, "variants": [{"price": "1.00", "sku": 5}]}, {**g, "tags": ["a"]}, {**g, "images": [{"src": "http://x"}]},
               {**g, "images": [{"url": "https://x"}]}, {**g, "images": ["https://x"]}, {**g, "brand": 5},
               {**g, "description": "texto"}]
        for body in bad:
            self.assertEqual(self.raw("POST", "/products", body)[0], 422, body)
        self.assertEqual(self.products(), {})

    def test_put_validation(self):
        pid = self.raw("POST", "/products", self.GOOD)[1]
        vid = pid["variants"][0]["id"]
        pid = pid["id"]
        self.assertEqual(self.raw("PUT", f"/products/{pid}", {"published": "true"})[0], 422)
        self.assertEqual(self.raw("PUT", f"/products/{pid}", {"name": "x"})[0], 422)
        self.assertEqual(self.raw("PUT", f"/products/{pid}", {"published": True})[0], 200)
        self.assertEqual(self.raw("PUT", f"/products/{pid}/variants/{vid}", {"price": 5})[0], 422)
        self.assertEqual(self.raw("PUT", f"/products/{pid}/variants/{vid}", {"sku": "x"})[0], 422)
        self.assertEqual(self.raw("PUT", f"/products/{pid}/variants/{vid}", {"price": "0.00"})[0], 422)
        self.assertEqual(self.raw("PUT", f"/products/{pid}/variants/{vid}", {"price": "2.00", "stock": 3})[0], 200)
        self.assertEqual(self.raw("PUT", f"/products/{pid}/variants/99999999", {"price": "2.00"})[0], 404)
        self.assertEqual(self.raw("PUT", "/products/99999999", {"published": True})[0], 404)
        self.assertEqual(self.raw("DELETE", "/products/99999999")[0], 404)

    def test_scopes_and_lookup(self):
        self.raw("POST", "/products", self.GOOD)
        self.assertEqual(self.raw("GET", "/products/sku/A")[0], 200)
        self.assertEqual(self.raw("GET", "/products/sku/B")[0], 404)
        self.st["scopes"] = {"read_products"}
        self.assertEqual(self.raw("POST", "/products", self.GOOD)[0], 403)
        self.assertEqual(self.raw("GET", "/products/sku/A")[0], 200)
        self.st["scopes"] = {"write_products"}
        self.assertEqual(self.raw("GET", "/products/sku/A")[0], 403)

    def test_clean_shutdown_no_leftovers(self):
        f = FakeTiendanube().start()
        f.stop()
        self.assertFalse(f.thread.is_alive())
        f.stop()                                                   # idempotente


class StockManagementTest(Base):
    """Sin stock_management=true la variante real queda con stock ilimitado (null): sobreventa de un producto agotado."""

    def variant(self, ref):
        return next(v for v in self.products()[int(ref.external_id)]["variants"] if str(v["id"]) == ref.variant_id)

    def test_bodies_carry_stock_management(self):
        ref = self.publish(stock=0)
        self.assertIs(self.fake.requests_for("product_create")[0]["body"]["variants"][0]["stock_management"], True)
        self.conn().update_price_stock(ref, 10, 3)
        self.publish(price=12, stock=2, active=False)                       # adopción
        puts = self.fake.requests_for("variant_update")
        self.assertEqual(len(puts), 2)
        for r in puts:
            self.assertIs(r["body"]["stock_management"], True)
            self.assertIn("stock", r["body"])

    def test_end_to_end_numeric_stock_including_zero(self):
        ref = self.publish(stock=0, active=True)
        v = self.variant(ref)
        self.assertEqual((v["stock"], v["stock_management"]), (0, True))      # agotado de verdad: no comprable
        self.conn().update_price_stock(ref, 10, 5)
        self.assertEqual(self.variant(ref)["stock"], 5)
        self.conn().update_price_stock(ref, 10, 0)
        self.assertEqual(self.variant(ref)["stock"], 0)
        self.publish(stock=9, active=False)                                   # adopta
        self.assertEqual(self.variant(ref)["stock"], 9)

    def test_adopting_a_variant_without_management_turns_it_on(self):
        ref = self.publish(stock=4)
        v = self.variant(ref)
        v["stock_management"], v["stock"] = False, None                       # alguien lo dejó ilimitado
        self.publish(stock=0, active=False)
        self.assertEqual((v["stock"], v["stock_management"]), (0, True))

    def test_fake_models_unlimited_semantics(self):
        body = {"name": {"es": "X"}, "published": False, "variants": [{"price": "1.00", "stock": 0, "sku": "A"}]}
        r = shttp.request_json("POST", self.fake.api_template.format(store=STORE, path="/products"), body, headers={
            "Authentication": f"bearer {TOKEN}", "User-Agent": f"DropFinder ({EMAIL})"}).json()
        self.assertIsNone(r["variants"][0]["stock"])                          # sin gestión: ilimitado, ignora stock


class SkuLookupFormatsTest(Base):
    def test_all_answers_when_absent(self):
        for mode in ("404", "empty_list", "null"):
            self.st["sku_mode"] = mode
            self.products().clear()
            ref = self.publish()
            self.assertEqual(len(self.products()), 1, mode)
            ref2 = self.publish(price=21)                                      # ahora existe: adopta
            self.assertEqual((len(self.products()), ref2.external_id), (1, ref.external_id), mode)

    def test_list_answer_picks_matching_item_not_first(self):
        self.st["sku_mode"] = "list"
        other = self.publish(sku="OTRO-1")
        ref = self.publish(sku="DF-1")
        self.assertNotEqual(other.external_id, ref.external_id)
        n = len(self.fake.requests_for("product_create"))
        again = self.publish(sku="DF-1", price=30)
        self.assertEqual(again.external_id, ref.external_id)
        self.assertEqual(len(self.fake.requests_for("product_create")), n)
        lookup = self.conn()._call("GET", "/products/sku/DF-1")
        self.assertEqual(lookup[0]["variants"][0]["sku"], "OTRO-1")           # el coincidente va después

    def test_list_without_matching_sku_creates(self):
        items = [{"id": 5, "variants": [{"id": 6, "sku": "OTRO"}]}, {"id": 7, "variants": [{"id": 8, "sku": "TAMBIEN-OTRO"}]}]
        with mock.patch.object(C, "_call", return_value=items):
            self.assertIsNone(self.conn()._find_existing("DF-1", OWN_TAGS))
        items.append({"id": 9, "tags": "dropfinder,dfi-abc123", "variants": [{"id": 10, "sku": "DF-1"}]})
        with mock.patch.object(C, "_call", return_value=items):
            self.assertEqual(self.conn()._find_existing("DF-1", OWN_TAGS)["id"], 9)


class LanguageTest(Base):
    def names(self):
        return self.fake.requests_for("product_create")[-1]["body"]

    def test_uses_main_language(self):
        self.st["main_language"] = "pt"
        self.publish()
        b = self.names()
        self.assertEqual(list(b["name"]), ["pt"])
        self.assertEqual(list(b["description"]), ["pt"])

    def test_fallbacks(self):
        for main, langs, exp in [("pt-BR", {"pt": "x"}, "pt"), ("ES", {"es": "x"}, "es"), (None, {"es": "x", "en": "y"}, "es"),
                                 (None, {"pt": "x", "en": "y"}, "pt"), (None, {}, "es"), (None, None, "es"),
                                 ("e$!", {"en": "y"}, "en"), ("", ["en", "pt"], "en"), (5, {"fr": "z"}, "fr"),
                                 ("xxxxx", {"es": "x"}, "es"), (None, ["es_AR"], "es")]:
            tiendanube.clear_caches()
            self.st["main_language"], self.st["languages"] = main, langs
            self.products().clear()
            self.publish()
            self.assertEqual(list(self.names()["name"]), [exp], (main, langs))

    def test_cached_per_store_and_token_for_ten_minutes(self):
        clock = [1000.0]
        with mock.patch.object(tiendanube, "_now", side_effect=lambda: clock[0]):
            self.publish(sku="A1")
            self.publish(sku="A2")
            self.assertEqual(len(self.fake.requests_for("store")), 1)
            clock[0] += 599
            self.publish(sku="A3")
            self.assertEqual(len(self.fake.requests_for("store")), 1)
            clock[0] += 2
            self.st["main_language"] = "pt"
            self.publish(sku="A4")
            self.assertEqual(len(self.fake.requests_for("store")), 2)
            self.assertEqual(list(self.names()["name"]), ["pt"])
            self.st["tokens"].add("tn_otro_token_999")
            self.conn(token="tn_otro_token_999").publish(self.product(sku="A5"))      # otro token: otra entrada
            self.assertEqual(len(self.fake.requests_for("store")), 3)

    def test_validate_primes_cache(self):
        self.conn().validate()
        self.publish()
        self.assertEqual(len(self.fake.requests_for("store")), 1)

    def test_cache_is_bounded_and_pruned(self):
        clock = [0.0]
        with mock.patch.object(tiendanube, "MAX_LANG_ENTRIES", 3), mock.patch.object(tiendanube, "_now", side_effect=lambda: clock[0]):
            for i in range(6):
                clock[0] += 1
                self.fake.add_store(str(100000 + i))
                self.st_i = self.fake.stores[str(100000 + i)]
                self.st_i["tokens"].add(TOKEN)
                C(str(100000 + i), {"access_token": TOKEN, "contact_email": EMAIL})._language()
            self.assertEqual(len(tiendanube._LANG_CACHE), 3)
            clock[0] += 10_000
            self.fake.add_store("200000").get("tokens").add(TOKEN)
            C("200000", {"access_token": TOKEN, "contact_email": EMAIL})._language()
            self.assertEqual(len(tiendanube._LANG_CACHE), 1)                      # lo caducado se poda

    def test_language_lookup_errors_propagate(self):
        self.fake.inject("http500", op="store")
        with self.assertRaises(StoreError) as cm:
            self.publish()
        self.assertTrue(cm.exception.retryable)
        self.assertEqual(self.products(), {})
        with self.assertRaises(StoreValidationError):
            self.conn(domain="999999").publish(self.product())


class AdoptionVisibilityTest(Base):
    def test_published_product_stays_visible_when_publish_inactive(self):
        ref = self.publish(active=True)
        self.publish(active=False, price=50)
        self.assertTrue(self.products()[int(ref.external_id)]["published"])
        self.assertEqual(self.fake.requests_for("product_update")[1:], [])
        self.assertEqual(len(self.products()), 1)

    def test_draft_is_published_when_active(self):
        ref = self.publish(active=False)
        self.publish(active=True)
        self.assertTrue(self.products()[int(ref.external_id)]["published"])

    def test_draft_stays_draft_when_inactive(self):
        ref = self.publish(active=False)
        self.publish(active=False)
        self.assertFalse(self.products()[int(ref.external_id)]["published"])
        self.assertEqual(self.fake.requests_for("product_update"), [])


class ThrottleLimitsTest(Base):
    def test_constants(self):
        self.assertEqual((tiendanube.MAX_INLINE_WAIT, tiendanube.CLEANUP_MAX_SLEEP), (2.0, 3.0))

    def test_inline_wait_boundary(self):
        self.fake.inject("http429", op="product_create", reset_ms=2000)
        self.publish()
        self.assertEqual(self.sleeps, [2.0])
        self.sleeps.clear()
        self.fake.inject("http429", op="product_create", reset_ms=2001, count=1)
        with self.assertRaises(StoreRateLimited) as cm:
            self.publish(sku="B1")
        self.assertEqual(cm.exception.retry_after, 2.001)
        self.assertEqual(self.sleeps, [])                                       # no bloquea el hilo único
        self.assertEqual(len(self.fake.requests_for("product_create")), 3)       # 2 de la primera + 1 sola de la segunda

    def test_cleanup_pause_capped_at_three_seconds(self):
        self.fake.inject("http500", op="product_update")
        self.fake.inject("http429", op="product_delete", count=1, reset_ms=60000)
        with self.assertRaises(StoreError):
            self.publish(active=True)
        self.assertEqual(self.sleeps, [3.0])
        self.assertEqual(self.products(), {})


class BoundaryTest(Base):
    def test_shop_name_length(self):
        for n, exp in ((99, 99), (100, 100), (101, 100)):
            self.st["name"] = {"es": "n" * n}
            self.assertEqual(len(self.conn().validate().name), exp)

    def test_email_length(self):
        def mail(total):
            dom = ["b" * 63, "c" * 63, "d" * (total - 65 - 63 - 63 - 2)]
            return "a" * 64 + "@" + ".".join(dom)
        ok, bad = mail(254), mail(255)
        self.assertEqual((len(ok), len(bad)), (254, 255))
        C.check_credentials({"access_token": "abcdef123456", "contact_email": ok})
        with self.assertRaises(StoreValidationError):
            C.check_credentials({"access_token": "abcdef123456", "contact_email": bad})

    def test_description_quotes_escaped(self):
        self.publish(description="\"q\" & 'a'")
        d = self.fake.requests_for("product_create")[0]["body"]["description"]["es"]
        self.assertEqual(d, "&quot;q&quot; &amp; &#x27;a&#x27;")


if __name__ == "__main__":
    unittest.main()
