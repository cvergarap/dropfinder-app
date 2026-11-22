"""Conector de Shopify contra el servidor falso local (tests/fake_shopify.py). Sin red real."""
import logging
import os
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
from store_connectors import shopify  # noqa: E402
from store_connectors.base import (ListingRef, StoreAuthError, StoreError, StoreNotFound, StoreProduct,  # noqa: E402
                                   StoreRateLimited, StoreValidationError)
from store_connectors import http as shttp  # noqa: E402
from fake_shopify import FakeShopify  # noqa: E402

SHOP = "mitienda.myshopify.com"
TOKEN = "shpat_valido_123"
CID, CSECRET = "client_abc123", "secreto_super_privado_987"
C = shopify.ShopifyConnector


class Base(unittest.TestCase):
    def setUp(self):
        self.fake = FakeShopify().start()
        self.st = self.fake.add_shop(SHOP, apps={CID: CSECRET})
        p = [mock.patch.object(C, "ADMIN_URL_TEMPLATE", self.fake.admin_template),
             mock.patch.object(C, "TOKEN_URL_TEMPLATE", self.fake.token_template),
             mock.patch.object(shopify, "_sleep", side_effect=lambda s: self.sleeps.append(s))]
        self.sleeps = []
        for x in p:
            x.start()
            self.addCleanup(x.stop)
        shopify.clear_caches()
        self.addCleanup(shopify.clear_caches)
        self.addCleanup(self.fake.stop)
        old_loop, shttp.ALLOW_LOOPBACK = shttp.ALLOW_LOOPBACK, True          # solo para el servidor falso local
        self.addCleanup(lambda: setattr(shttp, "ALLOW_LOOPBACK", old_loop))

    def conn(self, mode="token", settings=None, token=TOKEN):
        creds = {"access_token": token} if mode == "token" else {"client_id": CID, "client_secret": CSECRET}
        return C(SHOP, creds, settings)

    def product(self, **kw):
        d = dict(title="Lámpara <LED> & más", price=19.9, sku="DF-1", stock=7, description="Línea 1\nLínea <b>2</b>",
                 image_urls=["https://cdn.x.com/a.jpg", "http://x.com/b.jpg", "javascript:alert(1)", "https://u:p@x.com/c.jpg"],
                 compare_at_price=29.9, vendor="Prov", product_type="Hogar", tags=["dropfinder", "dfi-test1"], cost=7.5)
        d.update(kw)
        return StoreProduct(**d)


class DomainTest(unittest.TestCase):
    def test_valid(self):
        for raw, exp in [("mitienda.myshopify.com", SHOP), ("  MiTienda.MyShopify.com ", SHOP),
                         ("https://mitienda.myshopify.com/admin/products/1?x=1", SHOP),
                         ("https://MITIENDA.myshopify.com/", SHOP), ("http://a1-b.myshopify.com", "a1-b.myshopify.com"),
                         ("0abc.myshopify.com", "0abc.myshopify.com")]:
            self.assertEqual(C.normalize_domain(raw), exp, raw)

    def test_invalid(self):
        bad = ["", None, 5, "127.0.0.1", "1.2.3.4.myshopify.com", "x.myshopify.com:443", "user@x.myshopify.com",
               "x.myshopify.com.evil.com", "evil.com/x.myshopify.com", "x.myshopify.com@evil.com", "ｘ.myshopify.com",
               "K.myshopify.com", "-x.myshopify.com", "x.myshopify.com.", "ftp://x.myshopify.com", "javascript:x",
               "https://x.myshopify.com\\@evil.com", "x.myshopify.com%2f", "a" * 62 + ".myshopify.com", "x y.myshopify.com",
               "myshopify.com", ".myshopify.com", "x.myshopify.co", "https://evil.com#x.myshopify.com", "x_y.myshopify.com",
               "https://user:pw@x.myshopify.com", "tienda.com", "x.myshopify.com\n.evil.com", "[::1]", "localhost",
               "https://admin.shopify.com/store/x"]
        for raw in bad:
            with self.assertRaises(StoreValidationError, msg=repr(raw)):
                C.normalize_domain(raw)

    def test_registered(self):
        self.assertIs(sc.get_class("shopify"), C)
        self.assertEqual(C.label, "Shopify")
        self.assertTrue(all(f["required"] is False for f in C.credential_fields))
        for w in ("write_products", "write_inventory", "read_locations", "puede cambiar"):
            self.assertIn(w, C.setup_help)


class CredentialsTest(unittest.TestCase):
    def test_modes(self):
        C.check_credentials({"access_token": "shpat_abcdef123"})
        C.check_credentials({"client_id": "abc123", "client_secret": "s3cr3t_xyz"})

    def test_invalid(self):
        for creds in [{}, {"access_token": "shpat_abcdef123", "client_id": "abc123", "client_secret": "s3cr3t_xyz"},
                      {"access_token": "shpat_abcdef123", "client_id": "abc123"}, {"client_id": "abc123"},
                      {"client_secret": "s3cr3t_xyz"}, {"access_token": "con espacio y\nsalto"},
                      {"access_token": "corto"}, {"client_id": "bad id!", "client_secret": "s3cr3t_xyz"}]:
            with self.assertRaises(StoreValidationError, msg=creds):
                C.check_credentials(creds)

    def test_error_does_not_echo_secrets(self):
        try:
            C.check_credentials({"access_token": "shpat_secreto\nmalo"})
        except StoreValidationError as e:
            self.assertNotIn("shpat_secreto", str(e))


class ValidateTest(Base):
    def test_ok_both_modes(self):
        for mode in ("token", "client"):
            info = self.conn(mode).validate()
            self.assertEqual((info.currency, info.location_id, info.missing_scopes),
                             ("USD", "gid://shopify/Location/1", []))
            self.assertEqual(info.scopes, ["read_locations", "write_inventory", "write_products"])
            self.assertTrue(info.name)
        h = self.fake.requests_for("validate")[0]["headers"]
        self.assertEqual(h["x-shopify-access-token"], TOKEN)
        self.assertEqual(h["user-agent"], "DropFinder/1.0")
        self.assertIn("/admin/api/" + shopify.API_VERSION_DEFAULT + "/graphql.json", self.fake.requests[0]["path"])

    def test_missing_scopes(self):
        self.st["scopes"] = {"write_products", "read_locations"}
        self.assertEqual(self.conn().validate().missing_scopes, ["write_inventory"])
        self.st["scopes"] = {"write_products", "write_inventory"}
        info = self.conn().validate()                      # sin read_locations: no falla, informa
        self.assertEqual((info.missing_scopes, info.location_id), (["read_locations"], ""))

    def test_write_implies_read(self):
        self.st["scopes"] = {"write_products", "write_inventory", "write_locations"}
        self.assertEqual(self.conn().validate().missing_scopes, [])

    def test_bad_token_and_revoked(self):
        with self.assertRaises(StoreAuthError):
            self.conn(token="shpat_incorrecto_1").validate()
        self.st["revoked"] = True
        with self.assertRaises(StoreAuthError):
            self.conn().validate()

    def test_bad_client_secret(self):
        with self.assertRaises(StoreAuthError):
            C(SHOP, {"client_id": CID, "client_secret": "otro_secreto_1"}).validate()

    def test_unknown_shop(self):
        with self.assertRaises(StoreValidationError):
            C("otra.myshopify.com", {"access_token": TOKEN}).validate()

    def test_api_version_env(self):
        with mock.patch.dict(os.environ, {shopify.API_VERSION_ENV: "2025-10"}):
            self.conn().validate()
        self.assertIn("/2025-10/", self.fake.requests[-1]["path"])
        for bad in ("2025-13", "latest", "../x", "2025-1"):
            with mock.patch.dict(os.environ, {shopify.API_VERSION_ENV: bad}):
                with self.assertRaises(StoreValidationError):
                    self.conn().validate()

    def test_endpoint_not_from_settings(self):
        self.conn(settings={"ADMIN_URL_TEMPLATE": "http://evil.example/x", "admin_url": "http://evil.example"}).validate()
        self.assertEqual(len(self.fake.requests), 1)


class PublishTest(Base):
    def test_happy_path_sequence_and_inputs(self):
        ref = self.conn().publish(self.product(active=False))
        self.assertEqual(self.fake.ops(), ["locations", "products", "productCreate", "productVariantsBulkUpdate",
                                            "inventorySetQuantities"])
        create = self.fake.requests_for("productCreate")[0]["body"]["variables"]
        p = create["product"]
        self.assertEqual(p["status"], "DRAFT")
        self.assertEqual(p["tags"][-1], "df-sku:DF-1")
        self.assertEqual(p["title"], "Lámpara <LED> & más")
        self.assertEqual(p["descriptionHtml"], "Línea 1<br>Línea &lt;b&gt;2&lt;/b&gt;")
        self.assertEqual((p["vendor"], p["productType"], p["tags"]), ("Prov", "Hogar", ["dropfinder", "dfi-test1", "df-sku:DF-1"]))
        self.assertEqual(create["media"], [{"originalSource": "https://cdn.x.com/a.jpg", "mediaContentType": "IMAGE",
                                            "alt": "Lámpara <LED> & más"}])
        bulk = self.fake.requests_for("productVariantsBulkUpdate")[0]["body"]["variables"]
        self.assertEqual(bulk["productId"], ref.external_id)
        self.assertEqual(bulk["variants"], [{"id": ref.variant_id, "price": "19.90", "compareAtPrice": "29.90",
                                             "inventoryItem": {"sku": "DF-1", "tracked": True, "cost": "7.50"}}])
        inv = self.fake.requests_for("inventorySetQuantities")[0]["body"]["variables"]["input"]
        self.assertEqual(inv, {"name": "available", "reason": "correction", "ignoreCompareQuantity": True,
                               "quantities": [{"inventoryItemId": ref.inventory_item_id,
                                               "locationId": "gid://shopify/Location/1", "quantity": 7}]})
        num = ref.external_id.rsplit("/", 1)[1]
        self.assertEqual(ref.url, f"https://{SHOP}/admin/products/{num}")
        rec = self.st["products"][ref.external_id]
        self.assertEqual(rec["status"], "DRAFT")
        self.assertEqual(self.st["levels"][(ref.inventory_item_id, "gid://shopify/Location/1")], 7)

    def test_active_published_last(self):
        ref = self.conn().publish(self.product(active=True, compare_at_price=None, cost=None))
        self.assertEqual(self.fake.ops()[-1], "productUpdate")
        self.assertEqual(self.st["products"][ref.external_id]["status"], "ACTIVE")
        bulk = self.fake.requests_for("productVariantsBulkUpdate")[0]["body"]["variables"]["variants"][0]
        self.assertNotIn("compareAtPrice", bulk)
        self.assertNotIn("cost", bulk["inventoryItem"])
        self.assertEqual(self.fake.requests_for("productCreate")[0]["body"]["variables"]["product"]["status"], "DRAFT")
        self.assertEqual(self.fake.requests_for("productUpdate")[0]["body"]["variables"]["product"]["status"], "ACTIVE")

    def test_max_images_and_location_setting(self):
        urls = [f"https://x.com/{i}.jpg" for i in range(15)]
        self.conn(settings={"location_id": "gid://shopify/Location/1"}).publish(self.product(image_urls=urls))
        self.assertNotIn("locations", self.fake.ops())
        self.assertEqual(len(self.fake.requests_for("productCreate")[0]["body"]["variables"]["media"]), 10)

    def test_not_stocked_activates(self):
        self.st["unstocked_on_create"] = True
        ref = self.conn().publish(self.product())
        self.assertIn("inventoryActivate", self.fake.ops())
        self.assertEqual(self.st["levels"][(ref.inventory_item_id, "gid://shopify/Location/1")], 7)

    def test_local_validation(self):
        with self.assertRaises(StoreValidationError):
            self.conn().publish(self.product(title="   "))
        with self.assertRaises(StoreValidationError):
            self.conn().publish(self.product(price=float("nan")))
        with self.assertRaises(StoreValidationError):
            self.conn().publish(self.product(price=-1))
        self.assertEqual(self.fake.requests, [])

    def test_partial_failure_cleans_orphan(self):
        self.fake.inject("http500", op="inventorySetQuantities")
        with self.assertRaises(StoreError) as cm:
            self.conn().publish(self.product())
        self.assertTrue(cm.exception.retryable)
        self.assertFalse(hasattr(cm.exception, "orphan_external_id"))
        self.assertEqual(self.fake.ops()[-1], "productDelete")
        self.assertEqual(self.st["products"], {})

    def test_user_error_cleans_orphan(self):
        with mock.patch.object(shopify, "_money", lambda *a, **k: "abc"):
            with self.assertRaises(StoreValidationError) as cm:
                self.conn().publish(self.product(compare_at_price=None, cost=None))
        self.assertIn("Shopify rechazó los datos", str(cm.exception))
        self.assertIn("price", str(cm.exception))
        self.assertEqual(self.st["products"], {})

    def test_cleanup_failure_reports_orphan(self):
        self.fake.inject("http500", op="inventorySetQuantities")
        self.fake.inject("http500", op="productDelete", count=5)
        with self.assertLogs("dropfinder.shopify", "WARNING"), self.assertRaises(StoreError) as cm:
            self.conn().publish(self.product())
        gid = list(self.st["products"])[0]
        self.assertEqual(cm.exception.orphan_external_id, gid)
        self.assertIn(gid, str(cm.exception))

    def test_create_failure_nothing_to_clean(self):
        self.fake.inject("http500", op="productCreate")
        with self.assertRaises(StoreError):
            self.conn().publish(self.product())
        self.assertEqual(self.fake.ops(), ["locations", "products", "productCreate"])      # nada que limpiar

    def test_missing_scope_on_publish(self):
        self.st["scopes"].discard("write_products")
        self.st["scopes"].add("read_products")
        with self.assertRaises(StoreAuthError) as cm:
            self.conn().publish(self.product())
        self.assertIn("write_products", str(cm.exception))


class UpdateTest(Base):
    def setUp(self):
        super().setUp()
        self.ref = self.conn().publish(self.product())
        self.fake.requests.clear()

    def test_update_price_stock(self):
        self.conn().update_price_stock(self.ref, 24, 3, 30)
        self.assertEqual(self.fake.ops(), ["productVariantsBulkUpdate", "inventorySetQuantities"])   # ubicación ya en caché
        v = self.st["variants"][self.ref.variant_id]
        self.assertEqual((v["price"], v["compareAtPrice"]), ("24.00", "30.00"))
        self.assertEqual(self.st["levels"][(self.ref.inventory_item_id, "gid://shopify/Location/1")], 3)
        self.conn().update_price_stock(self.ref, 24, 0, None)
        # compare_at None: NO se envía (no borra el precio de comparación puesto a mano)
        self.assertNotIn("compareAtPrice", self.fake.requests_for("productVariantsBulkUpdate")[-1]["body"]["variables"]["variants"][0])
        self.assertEqual(self.st["variants"][self.ref.variant_id]["compareAtPrice"], "30.00")
        self.assertEqual(self.st["levels"][(self.ref.inventory_item_id, "gid://shopify/Location/1")], 0)
        self.assertNotIn("locations", self.fake.ops())

    def test_hide_show(self):
        self.conn().set_active(self.ref, True)
        self.assertEqual(self.st["products"][self.ref.external_id]["status"], "ACTIVE")
        self.conn().set_active(self.ref, False)
        self.assertEqual(self.st["products"][self.ref.external_id]["status"], "DRAFT")

    def test_gone_product(self):
        self.st["products"].clear()
        with self.assertRaises(StoreNotFound):
            self.conn().update_price_stock(self.ref, 10, 1)
        with self.assertRaises(StoreNotFound):
            self.conn().set_active(self.ref, True)

    def test_delete_idempotent(self):
        self.conn().delete(self.ref)
        self.assertEqual(self.st["products"], {})
        self.conn().delete(self.ref)
        self.assertEqual(self.fake.ops()[-3:], ["product", "productDelete", "product"])   # 2.ª vez: ya no existe

    def test_bad_refs(self):
        for bad in (ListingRef("123"), ListingRef("gid://shopify/Product/1\"}"), ListingRef("")):
            with self.assertRaises(StoreValidationError):
                self.conn().set_active(bad, True)
        with self.assertRaises(StoreValidationError):
            self.conn().update_price_stock(ListingRef(self.ref.external_id), 1, 1)

    def test_price_rounding(self):
        self.conn().update_price_stock(self.ref, 10.005, 1)
        self.assertEqual(self.st["variants"][self.ref.variant_id]["price"], "10.01")


class ErrorMappingTest(Base):
    def test_throttled(self):
        self.fake.inject("throttled")
        with self.assertRaises(StoreRateLimited) as cm:
            self.conn().validate()
        self.assertTrue(cm.exception.retryable)
        self.assertGreaterEqual(cm.exception.retry_after, 1.0)

    def test_http429_retry_after(self):
        self.fake.inject("http429", retry_after=7)
        with self.assertRaises(StoreRateLimited) as cm:
            self.conn().validate()
        self.assertEqual(cm.exception.retry_after, 7.0)

    def test_5xx_retryable(self):
        for kind in ("http500", "http502", "http503"):
            self.fake.inject(kind)
            with self.assertRaises(StoreError) as cm:
                self.conn().validate()
            self.assertTrue(cm.exception.retryable)
            self.assertNotIsInstance(cm.exception, (StoreAuthError, StoreRateLimited, StoreNotFound))

    def test_other_4xx_not_retryable(self):
        self.fake.inject("http422")
        with self.assertRaises(StoreError) as cm:
            self.conn().validate()
        self.assertFalse(cm.exception.retryable)

    def test_403_auth(self):
        self.fake.inject("http403")
        with self.assertRaises(StoreAuthError):
            self.conn().validate()

    def test_404_is_never_not_found(self):
        # Un 404 del endpoint es "revisa el dominio", no "producto borrado": delete() no puede tragárselo.
        ref = ListingRef("gid://shopify/Product/5", "gid://shopify/ProductVariant/5", "gid://shopify/InventoryItem/5")
        for call in (lambda c: c.set_active(ref, True), lambda c: c.delete(ref), lambda c: c.update_price_stock(ref, 5, 1),
                     lambda c: c.validate()):
            self.fake.inject("http404")
            with self.assertRaises(StoreValidationError) as cm:
                call(self.conn(settings={"location_id": "gid://shopify/Location/1"}))
            self.assertIn("dominio", str(cm.exception))

    def test_token_endpoint_404_both_modes(self):
        self.fake.inject("http404", op="oauth")
        with self.assertRaises(StoreValidationError) as cm:
            self.conn("client").validate()
        self.assertIn("dominio", str(cm.exception))
        ref = ListingRef("gid://shopify/Product/5")
        self.fake.inject("http404", op="oauth")
        with self.assertRaises(StoreValidationError):
            self.conn("client").delete(ref)               # no se confunde con "ya borrado"
        self.fake.inject("http404")
        with self.assertRaises(StoreValidationError):
            self.conn().validate()

    def test_timeout(self):
        self.fake.inject("sleep", seconds=0.8)
        with mock.patch.object(C, "TIMEOUT", 0.15):
            with self.assertRaises(StoreError) as cm:
                self.conn().validate()
        self.assertTrue(cm.exception.retryable)

    def test_malformed_json(self):
        for kind in ("malformed", "notjson200"):
            self.fake.inject(kind)
            with self.assertRaises(StoreError) as cm:
                self.conn().validate()
            self.assertTrue(cm.exception.retryable)

    def test_redirect_refused(self):
        self.fake.inject("redirect")
        with self.assertRaises(StoreError) as cm:
            self.conn().validate()
        self.assertFalse(cm.exception.retryable)
        self.assertEqual(self.fake.evil_hits, 0)

    def test_redirect_on_token_endpoint_refused(self):
        self.fake.inject("redirect", op="oauth")
        with self.assertRaises(StoreError):
            self.conn("client").validate()
        self.assertEqual(self.fake.evil_hits, 0)

    def test_oversize_refused(self):
        self.fake.inject("oversize", size=5000)
        with mock.patch.object(C, "MAX_BYTES", 1000):
            with self.assertRaises(StoreError) as cm:
                self.conn().validate()
        self.assertIn("grande", str(cm.exception))
        self.assertFalse(cm.exception.retryable)

    def test_connection_refused(self):
        self.fake.stop()
        with self.assertRaises(StoreError) as cm:
            self.conn().validate()
        self.assertTrue(cm.exception.retryable)

    def test_pacing_only_when_low(self):
        self.conn().validate()
        self.assertEqual(self.sleeps, [])
        self.fake.cost_available = 10
        self.conn().validate()
        self.assertEqual(self.sleeps, [min(shopify.MAX_PACE_SLEEP, 90 / 50.0)])
        self.fake.cost_available = 0
        self.conn().validate()
        self.assertLessEqual(max(self.sleeps), shopify.MAX_PACE_SLEEP)


class SharedClientTest(Base):
    """El conector usa el cliente compartido: sin él no hay IP fijada ni filtros anti-SSRF."""

    def _real(self):
        shttp.ALLOW_LOOPBACK = False
        return mock.patch.object(C, "ADMIN_URL_TEMPLATE", "https://{shop}/admin/api/{version}/graphql.json")

    def test_private_resolution_refused(self):
        for ip in ("10.0.0.5", "127.0.0.1", "169.254.169.254", "192.168.1.9", "::1", "::ffff:10.0.0.1", "100.64.0.1"):
            fam = 10 if ":" in ip else 2
            with self._real(), mock.patch("socket.getaddrinfo", return_value=[(fam, 1, 6, "", (ip, 443))]):
                with self.assertRaises(StoreError, msg=ip) as cm:
                    self.conn().validate()
            self.assertFalse(cm.exception.retryable)
        self.assertEqual(self.fake.requests, [])

    def test_mixed_resolution_refused(self):
        with self._real(), mock.patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("8.8.8.8", 443)),
                                                                          (2, 1, 6, "", ("10.0.0.1", 443))]):
            with self.assertRaises(StoreError):
                self.conn().validate()

    def test_non_443_port_refused(self):
        shttp.ALLOW_LOOPBACK = False
        with mock.patch.object(C, "ADMIN_URL_TEMPLATE", "https://{shop}:8443/admin/api/{version}/graphql.json"):
            with mock.patch("socket.getaddrinfo") as gai:
                with self.assertRaises(StoreError):
                    self.conn().validate()
        gai.assert_not_called()

    def test_plain_http_refused_without_loopback_flag(self):
        shttp.ALLOW_LOOPBACK = False
        with self.assertRaises(StoreError) as cm:
            self.conn().validate()                               # la plantilla de prueba es http://127.0.0.1
        self.assertFalse(cm.exception.retryable)
        self.assertEqual(self.fake.requests, [])
        with mock.patch.object(C, "ADMIN_URL_TEMPLATE", "http://{shop}/admin/api/{version}/graphql.json"):
            with self.assertRaises(StoreError):
                self.conn().validate()

    def test_redirect_refused(self):
        self.fake.inject("redirect")
        with self.assertRaises(StoreError) as cm:
            self.conn().validate()
        self.assertFalse(cm.exception.retryable)
        self.assertEqual(self.fake.evil_hits, 0)

    def test_shopify_text_is_scrubbed(self):
        evil = f"boom {CSECRET} shpat_abcdefghijklmnop0123\x07 fin"
        for body in ({"errors": [{"message": evil}]},
                     {"data": {"productCreate": {"product": None, "userErrors": [{"field": ["title"], "message": evil}]}}}):
            with mock.patch.object(C, "_post", return_value=(200, body, {})):
                with self.assertRaises(StoreError) as cm:
                    self.conn(settings={"location_id": "gid://shopify/Location/1"}).publish(self.product()) if "data" in body else self.conn()._graphql("query{x}")
            text = str(cm.exception)
            self.assertIn("boom", text)
            self.assertNotIn(CSECRET, text)
            self.assertNotIn("shpat_abcdefghijklmnop0123", text)
            self.assertNotIn("\x07", text)


class TokenCacheTest(Base):
    def test_cache_and_reuse(self):
        c = self.conn("client")
        c.validate()
        self.conn("client").validate()
        self.assertEqual(self.fake.oauth_hits, 1)
        body = [r for r in self.fake.requests if r["op"] == "oauth"][0]["body"]
        self.assertEqual(body, {"client_id": CID, "client_secret": CSECRET, "grant_type": "client_credentials"})
        h = [r for r in self.fake.requests if r["op"] == "oauth"][0]["headers"]
        self.assertEqual(h["content-type"], "application/x-www-form-urlencoded")
        self.assertEqual(h["accept"], "application/json")

    def test_refresh_on_401(self):
        self.conn("client").validate()
        self.fake.expire_tokens(SHOP)
        ref = self.conn("client").publish(self.product())           # el 401 renueva y reintenta una vez
        self.assertEqual(self.fake.oauth_hits, 2)
        self.assertIn(ref.external_id, self.st["products"])

    def test_refresh_only_once(self):
        self.conn("client").validate()
        self.st["revoked"] = True
        with self.assertRaises(StoreAuthError):
            self.conn("client").validate()
        self.assertLessEqual(self.fake.oauth_hits, 2)
        self.assertLessEqual(len(self.fake.requests_for("validate")), 3)

    def test_static_token_never_refreshes(self):
        with self.assertRaises(StoreAuthError):
            self.conn(token="shpat_nope_12345").validate()
        self.assertEqual(self.fake.oauth_hits, 0)

    def test_expiry_margin(self):
        self.conn("client").validate()
        real = shopify._now()
        with mock.patch.object(shopify, "_now", lambda: real + 86399 - 100):   # dentro del margen de 5 min
            self.conn("client").validate()
        self.assertEqual(self.fake.oauth_hits, 2)
        with mock.patch.object(shopify, "_now", lambda: real + 3600):
            self.conn("client").validate()
        self.assertEqual(self.fake.oauth_hits, 2)

    def test_wrong_secret_not_served_from_cache(self):
        self.conn("client").validate()
        with self.assertRaises(StoreAuthError):
            C(SHOP, {"client_id": CID, "client_secret": "secreto_equivocado"}).validate()

    def test_concurrency_single_fetch(self):
        errs, refs = [], []

        def work(i):
            try:
                refs.append(self.conn("client").publish(self.product(sku=f"S{i}")))
            except Exception as e:                               # pragma: no cover
                errs.append(e)
        ts = [threading.Thread(target=work, args=(i,)) for i in range(6)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(30)
        self.assertEqual(errs, [])
        self.assertEqual(len(refs), 6)
        self.assertEqual(self.fake.oauth_hits, 1)
        self.assertEqual(len(self.st["products"]), 6)

    def test_concurrent_refresh_single_refetch(self):
        self.conn("client").validate()
        self.fake.expire_tokens(SHOP)
        errs = []

        def work():
            try:
                self.conn("client").validate()
            except Exception as e:                               # pragma: no cover
                errs.append(e)
        ts = [threading.Thread(target=work) for _ in range(5)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(30)
        self.assertEqual(errs, [])
        self.assertEqual(self.fake.oauth_hits, 2)


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
        secrets = (TOKEN, CSECRET, "shpat_incorrecto_1", "secreto_equivocado")
        msgs = []

        def grab(fn):
            try:
                fn()
            except Exception as e:
                msgs.append(str(e) + repr(e))
        grab(lambda: self.conn(token="shpat_incorrecto_1").validate())
        grab(lambda: C(SHOP, {"client_id": CID, "client_secret": "secreto_equivocado"}).validate())
        self.fake.inject("http500", op="inventorySetQuantities")
        self.fake.inject("http500", op="productDelete")
        grab(lambda: self.conn().publish(self.product()))
        self.fake.inject("throttled")
        grab(lambda: self.conn().validate())
        self.fake.inject("malformed")
        grab(lambda: self.conn("client").validate())
        self.st["scopes"].discard("write_products")
        grab(lambda: self.conn().publish(self.product()))
        self.assertTrue(msgs)
        for text in msgs + records:
            for s in secrets:
                self.assertNotIn(s, text)

    def test_redact_echoed_secret(self):
        c = self.conn()
        self.assertNotIn(TOKEN, c._redact(f"error con {TOKEN} dentro"))


def _raw(fake, query, variables, version="2026-04", token=TOKEN):
    resp = shttp.request_json("POST", fake.admin_template.format(shop=SHOP, version=version),
                              {"query": query, "variables": variables}, headers={shopify.TOKEN_HEADER: token})
    return resp.json()


class InputValidationTest(Base):
    """Se valida TODO antes de tocar la tienda; nunca escapa una excepción que no sea StoreError."""

    def test_bad_prices_rejected_before_any_request(self):
        for price in (0, 0.004, 1e-7, -1, float("nan"), float("inf"), "abc", None, True, 1e10, object(), 10 ** 400):
            with self.assertRaises(StoreValidationError, msg=repr(price)):
                self.conn().publish(self.product(price=price))
            ref = ListingRef("gid://shopify/Product/1", "gid://shopify/ProductVariant/1", "gid://shopify/InventoryItem/1")
            with self.assertRaises(StoreValidationError, msg=repr(price)):
                self.conn().update_price_stock(ref, price, 1)
        self.assertEqual(self.fake.requests, [])

    def test_price_quantized_first(self):
        ref = self.conn().publish(self.product(price=0.005, compare_at_price=None))
        self.assertEqual(self.st["variants"][ref.variant_id]["price"], "0.01")

    def test_bad_stock_rejected_before_any_request(self):
        for stock in (float("nan"), float("inf"), None, -1, "5", 10 ** 15, True, [], 1e30):
            with self.assertRaises(StoreValidationError, msg=repr(stock)):
                self.conn().publish(self.product(stock=stock))
            ref = ListingRef("gid://shopify/Product/1", "gid://shopify/ProductVariant/1", "gid://shopify/InventoryItem/1")
            with self.assertRaises(StoreValidationError, msg=repr(stock)):
                self.conn().update_price_stock(ref, 5, stock)
        self.assertEqual(self.fake.requests, [])

    def test_stock_limits(self):
        self.conn().publish(self.product(stock=0, sku="Z0"))
        self.conn().publish(self.product(stock=10 ** 9, sku="Z1"))

    def test_never_foreign_exceptions(self):
        weird = [dict(title=object()), dict(description=object()), dict(vendor=object(), product_type=5), dict(tags=5),
                 dict(image_urls=5), dict(image_urls=[None, 5, object()]), dict(sku=None), dict(cost="x"),
                 dict(compare_at_price="x"), dict(price=[1])]
        for kw in weird:
            try:
                self.conn().publish(self.product(**kw))
            except StoreError:
                pass
        for call in (lambda c: c.publish(None), lambda c: c.delete(None), lambda c: c.set_active(None, True),
                     lambda c: c.update_price_stock(None, 1, 1)):
            with self.assertRaises(StoreValidationError):
                call(self.conn())

    def test_compare_not_above_price_is_dropped(self):
        for compare in (19.9, 10.0, 19.899):
            self.fake.requests.clear()
            self.conn().publish(self.product(compare_at_price=compare, sku=f"C{compare}"))
            v = self.fake.requests_for("productVariantsBulkUpdate")[0]["body"]["variables"]["variants"][0]
            self.assertNotIn("compareAtPrice", v, compare)
        ref = self.conn().publish(self.product(compare_at_price=25, sku="Chi"))
        self.assertEqual(self.st["variants"][ref.variant_id]["compareAtPrice"], "25.00")
        self.fake.requests.clear()
        self.conn().update_price_stock(ref, 30, 1, 30)
        self.assertNotIn("compareAtPrice", self.fake.requests_for("productVariantsBulkUpdate")[0]["body"]["variables"]["variants"][0])

    def test_title_and_sku_truncated(self):
        self.conn().publish(self.product(title="T" * 400, sku="s" * 400))
        v = self.fake.requests_for("productCreate")[0]["body"]["variables"]["product"]
        self.assertEqual(len(v["title"]), 255)
        bulk = self.fake.requests_for("productVariantsBulkUpdate")[0]["body"]["variables"]["variants"][0]
        self.assertEqual(len(bulk["inventoryItem"]["sku"]), 255)

    def test_tags_normalized(self):
        self.conn().publish(self.product(tags="abc,def", sku="T1"))
        self.assertEqual(self.fake.requests_for("productCreate")[0]["body"]["variables"]["product"]["tags"],
                         ["abc def", "df-sku:T1"])
        self.conn().publish(self.product(tags=[f"t{i}" for i in range(400)] + [5, None, "  ", "x" * 400], sku="T2"))
        tags = self.fake.requests_for("productCreate")[1]["body"]["variables"]["product"]["tags"]
        self.assertEqual(len(tags), 250)
        self.assertEqual(tags[-1], "df-sku:T2")
        self.conn().publish(self.product(tags=["x" * 400], sku="T3"))
        self.assertEqual(len(self.fake.requests_for("productCreate")[2]["body"]["variables"]["product"]["tags"][0]), 255)

    def test_bad_images_dropped_not_failed(self):
        urls = ["https://ex.com/ünï.jpg", "https://127.0.0.1/x.png", "https://[::1]/x.png", "http://ok.com/a.jpg",
                "https://localhost/a.jpg", "https://ok.com/" + "a" * 2100, "https://ok.com/a b.jpg", "https://ok.com:99999/x",
                "https://good.com/1.jpg"] + [f"https://good.com/{i}.jpg" for i in range(2, 14)]
        self.conn().publish(self.product(image_urls=urls))
        media = self.fake.requests_for("productCreate")[0]["body"]["variables"]["media"]
        self.assertEqual(len(media), 10)
        self.assertTrue(all(m["originalSource"].startswith("https://good.com/") for m in media))
        self.conn().publish(self.product(image_urls=["https://127.0.0.1/x.png"], sku="NOIMG"))
        self.assertIsNone(self.fake.requests_for("productCreate")[1]["body"]["variables"]["media"])

    def test_location_setting_format(self):
        for bad in ("gid://shopify/Location/1; drop", "gid://shopify/Product/1", "1", "gid://shopify/Location/", 5,
                    "gid://shopify/Location/1\n"):
            with self.assertRaises(StoreValidationError, msg=repr(bad)):
                self.conn(settings={"location_id": bad}).publish(self.product())
            with self.assertRaises(StoreValidationError):
                self.conn(settings={"location_id": bad}).validate()
        self.assertEqual(self.fake.requests, [])
        self.conn(settings={"location_id": "gid://shopify/Location/1"}).publish(self.product())


class MissingClassificationTest(Base):
    def test_location_error_is_not_product_not_found(self):
        ref = self.conn().publish(self.product())
        for attempt in range(2):
            with self.assertRaises(StoreError) as cm:
                self.conn(settings={"location_id": "gid://shopify/Location/99"}).update_price_stock(ref, 10, 2)
            self.assertNotIsInstance(cm.exception, StoreNotFound)
            self.assertFalse(cm.exception.retryable)
            self.assertIn("ubicación", str(cm.exception))

    def test_location_cache_invalidated(self):
        ref = self.conn().publish(self.product())                 # cachea Location/1
        self.st["locations"] = [{"id": "gid://shopify/Location/2", "name": "Nueva", "isActive": True}]
        with self.assertRaises(StoreError) as cm:
            self.conn().update_price_stock(ref, 10, 2)
        self.assertNotIsInstance(cm.exception, StoreNotFound)
        self.assertEqual(shopify._LOC_CACHE, {})
        self.fake.requests.clear()
        self.st["levels"][(ref.inventory_item_id, "gid://shopify/Location/2")] = 0
        self.conn().update_price_stock(ref, 10, 2)                # vuelve a consultar y funciona
        self.assertIn("locations", self.fake.ops())

    def test_location_cache_per_credentials(self):
        self.conn().validate()
        self.assertEqual(len(shopify._LOC_CACHE), 1)
        C(SHOP, {"access_token": "shpat_otro_token_1"}, {})._remember_location("gid://shopify/Location/7")
        self.assertEqual(len(shopify._LOC_CACHE), 2)

    def test_variant_missing_is_not_product_not_found(self):
        ref = self.conn().publish(self.product())
        bad = ListingRef(ref.external_id, "gid://shopify/ProductVariant/424242", ref.inventory_item_id)
        with self.assertRaises(StoreValidationError):
            self.conn().update_price_stock(bad, 10, 1)

    def test_product_missing_variants(self):
        e = shopify.ShopifyConnector._is_product_missing
        self.assertTrue(e({"message": "Product does not exist"}))
        self.assertTrue(e({"message": "x", "code": "PRODUCT_DOES_NOT_EXIST"}))
        self.assertTrue(e({"message": "Not found", "field": ["productId"]}))
        self.assertFalse(e({"message": "The specified location could not be found."}))
        self.assertFalse(e({"message": "Variant does not exist", "field": ["variants", "0", "id"]}))
        self.assertFalse(e({"message": "The specified inventory item could not be found."}))
        self.assertFalse(e({"message": "Title can't be blank"}))


class IdempotentPublishTest(Base):
    def test_throttled_midflow_then_retry_one_product(self):
        self.fake.inject("throttled", op="productVariantsBulkUpdate")
        with self.assertRaises(StoreRateLimited):
            self.conn().publish(self.product())
        self.assertEqual(self.st["products"], {})                  # la limpieza funcionó
        ref = self.conn().publish(self.product())
        self.assertEqual(len(self.st["products"]), 1)
        self.assertIn(ref.external_id, self.st["products"])

    def test_throttled_cleanup_waits_and_retries(self):
        self.fake.inject("throttled", op="productVariantsBulkUpdate")
        self.fake.inject("throttled", op="productDelete", count=1)
        with self.assertRaises(StoreRateLimited):
            self.conn().publish(self.product())
        self.assertEqual(self.st["products"], {})                  # el 2.º intento de borrado funcionó
        self.assertEqual(self.fake.ops().count("productDelete"), 2)
        self.assertTrue(self.sleeps and all(0 < x <= shopify.CLEANUP_MAX_SLEEP for x in self.sleeps))

    def test_cleanup_gives_up_then_retry_adopts(self):
        self.fake.inject("throttled", op="productVariantsBulkUpdate")
        self.fake.inject("throttled", op="productDelete", count=10)
        with self.assertRaises(StoreRateLimited) as cm:
            self.conn().publish(self.product())
        self.assertEqual(self.fake.ops().count("productDelete"), shopify.CLEANUP_TRIES)
        orphan = cm.exception.orphan_external_id
        self.assertIn(orphan, self.st["products"])
        self.assertTrue(all(x <= shopify.CLEANUP_MAX_SLEEP for x in self.sleeps))
        self.fake.injections.clear()
        ref = self.conn().publish(self.product())
        self.assertEqual(ref.external_id, orphan)
        self.assertEqual(len(self.st["products"]), 1)
        self.assertEqual(self.st["variants"][ref.variant_id]["price"], "19.90")
        self.assertEqual(self.fake.ops().count("productCreate"), 1)

    def test_lost_response_then_retry_adopts(self):
        self.fake.inject("execute_then_drop", op="productCreate")
        with self.assertRaises(StoreError) as cm:
            self.conn().publish(self.product())
        self.assertTrue(cm.exception.retryable)
        self.assertEqual(len(self.st["products"]), 1)              # se creó aunque la respuesta se perdió
        ref = self.conn().publish(self.product(active=True))
        self.assertEqual(len(self.st["products"]), 1)
        self.assertEqual(self.fake.ops().count("productCreate"), 1)
        self.assertEqual(self.st["products"][ref.external_id]["status"], "ACTIVE")
        self.assertEqual(self.st["levels"][(ref.inventory_item_id, "gid://shopify/Location/1")], 7)

    def test_adopted_product_never_deleted_on_failure(self):
        self.conn().publish(self.product())
        self.fake.inject("http500", op="inventorySetQuantities", count=1)
        with self.assertRaises(StoreError):
            self.conn().publish(self.product())
        self.assertEqual(len(self.st["products"]), 1)
        self.assertNotIn("productDelete", self.fake.ops())

    def test_other_sku_not_adopted(self):
        a = self.conn().publish(self.product(sku="A-1"))
        b = self.conn().publish(self.product(sku="A-2"))
        self.assertNotEqual(a.external_id, b.external_id)
        self.assertEqual(len(self.st["products"]), 2)

    def test_tag_is_injection_safe(self):
        evil = ["x' OR tag:'y", "a\\b", "line\nbreak", "ñandú", "q\"uote", "", "a" * 300, "df-sku:x", "sha-" + "0" * 32]
        seen = set()
        for sku in evil:
            tag = shopify._sku_tag(sku)
            self.assertRegex(tag, r"^df-sku:[A-Za-z0-9_-]+$", sku)
            seen.add(tag)
        self.assertEqual(len(seen), len(evil))
        self.assertEqual(shopify._sku_tag("DF-1"), "df-sku:DF-1")
        self.assertEqual(shopify._sku_tag("ñandú"), shopify._sku_tag("ñandú"))
        self.conn().publish(self.product(sku="x' OR tag:'y\n\\"))
        q = self.fake.requests_for("products")[0]["body"]["variables"]["q"]
        self.assertRegex(q, r"^tag:'df-sku:[A-Za-z0-9_-]+'$")


class CredentialsHygieneTest(Base):
    def test_groups(self):
        g = {f["key"]: f.get("group") for f in C.credential_fields}
        self.assertEqual(g, {"access_token": "token", "client_id": "app", "client_secret": "app"})

    def test_repr_hides_credentials(self):
        c = self.conn("client")
        c.validate()
        text = repr(c)
        for secret in (CSECRET, TOKEN):
            self.assertNotIn(secret, text)
        self.assertNotIn(CSECRET, repr(self.conn()) + repr(C(SHOP, {"access_token": "shpat_zzzzzzzzzz"})))
        self.assertNotIn("shpat_zzzzzzzzzz", repr(C(SHOP, {"access_token": "shpat_zzzzzzzzzz"})))

    def test_cached_token_is_a_scrub_secret(self):
        c = self.conn("client")
        shopify._TOKEN_CACHE[c._cache_key()] = ("tokenraro_sin_prefijo_99", shopify._now() + 5000)
        self.assertIn("tokenraro_sin_prefijo_99", c._secrets())
        self.assertNotIn("tokenraro_sin_prefijo_99", c._redact("error con tokenraro_sin_prefijo_99 adentro"))
        body = {"errors": [{"message": "bad tokenraro_sin_prefijo_99 token"}]}
        with mock.patch.object(C, "_post", return_value=(200, body, {})), mock.patch.object(C, "_token", return_value="x" * 10):
            with self.assertRaises(StoreError) as cm:
                c._graphql("query{x}")
        self.assertNotIn("tokenraro_sin_prefijo_99", str(cm.exception))

    def test_cache_pruned(self):
        for i in range(5):
            self.st["apps"][f"client_{i}_abc"] = CSECRET
        with mock.patch.object(shopify, "MAX_TOKEN_ENTRIES", 3):
            for i in range(5):
                C(SHOP, {"client_id": f"client_{i}_abc", "client_secret": CSECRET},
                  {"location_id": "gid://shopify/Location/1"}).validate()      # sin barrer por la caché de ubicación
        self.assertLessEqual(len(shopify._TOKEN_CACHE), 3)
        real = shopify._now()
        with mock.patch.object(shopify, "_now", lambda: real + 10 ** 6):
            C(SHOP, {"client_id": CID, "client_secret": CSECRET}, {"location_id": "gid://shopify/Location/1"}).validate()
        self.assertEqual(len(shopify._TOKEN_CACHE), 1)             # lo caducado se barrió
        self.assertLessEqual(len(shopify._FETCH_LOCKS), 5)

    def test_invalidate_only_failing_token(self):
        c = self.conn("client")
        key = c._cache_key()
        shopify._TOKEN_CACHE[key] = ("tokennuevo_123456", shopify._now() + 9000)
        c._invalidate("tokenviejo_123456")
        self.assertIn(key, shopify._TOKEN_CACHE)
        c._invalidate("tokennuevo_123456")
        self.assertNotIn(key, shopify._TOKEN_CACHE)

    def test_cache_key_includes_client_id(self):
        a = C(SHOP, {"client_id": "client_aaaa1", "client_secret": CSECRET})
        b = C(SHOP, {"client_id": "client_bbbb2", "client_secret": CSECRET})
        self.assertNotEqual(a._cache_key(), b._cache_key())
        self.st["apps"]["client_aaaa1"] = CSECRET
        self.st["apps"]["client_bbbb2"] = CSECRET
        a.validate()
        b.validate()
        self.assertEqual(self.fake.oauth_hits, 2)

    def test_static_token_401_single_request(self):
        with self.assertRaises(StoreAuthError):
            self.conn(token="shpat_nope_12345").validate()
        self.assertEqual(len(self.fake.requests), 1)


class LimitsTest(Base):
    def test_retry_after_cap(self):
        self.fake.inject("http429", retry_after=99999)
        with self.assertRaises(StoreRateLimited) as cm:
            self.conn().validate()
        self.assertEqual(cm.exception.retry_after, shopify.MAX_RETRY_AFTER)
        self.assertEqual(shopify.MAX_RETRY_AFTER, 120.0)
        body = {"extensions": {"cost": {"requestedQueryCost": 10 ** 6, "throttleStatus": {"currentlyAvailable": 0, "restoreRate": 1}}}}
        self.assertEqual(self.conn()._throttle_wait(body, {}), 60.0)

    def test_402_423_specific_message(self):
        for code in (402, 423):
            self.fake.inject(f"http{code}")
            with self.assertRaises(StoreError) as cm:
                self.conn().validate()
            self.assertIn("suspendida", str(cm.exception))
            self.assertFalse(cm.exception.retryable)


class IdempotencyDirectiveTest(Base):
    def test_needs_idempotency_by_version(self):
        n = shopify._needs_idempotency
        self.assertTrue(n("2026-04") and n("2026-07") and n("2027-01"))
        self.assertFalse(n("2026-01") or n("2025-10") or n("garbage") or n(None))

    def test_directive_and_key_on_inventory_ops(self):
        r1 = self.conn().publish(self.product(sku="K1"))
        r2 = self.conn().publish(self.product(sku="K2"))
        reqs = self.fake.requests_for("inventorySetQuantities")
        self.assertEqual(len(reqs), 2)
        for r in reqs:
            self.assertIn("@idempotent(key: $key)", r["body"]["query"])
            self.assertRegex(r["body"]["variables"]["key"], r"^[0-9a-f-]{36}$")
        self.assertNotEqual(reqs[0]["body"]["variables"]["key"], reqs[1]["body"]["variables"]["key"])
        for r in (self.fake.requests_for("productCreate") + self.fake.requests_for("productUpdate")):
            self.assertNotIn("@idempotent", r["body"]["query"])
        self.assertTrue(r1 and r2)

    def test_old_version_has_no_directive(self):
        with mock.patch.dict(os.environ, {shopify.API_VERSION_ENV: "2026-01"}):
            self.conn().publish(self.product())
        r = self.fake.requests_for("inventorySetQuantities")[0]["body"]
        self.assertNotIn("@idempotent", r["query"])
        self.assertNotIn("key", r["variables"])

    def test_activate_has_directive(self):
        self.st["unstocked_on_create"] = True
        self.conn().publish(self.product())
        r = self.fake.requests_for("inventoryActivate")[0]["body"]
        self.assertIn("@idempotent(key: $key)", r["query"])
        keys = [x["body"]["variables"]["key"] for x in self.fake.requests_for("inventorySetQuantities")]
        self.assertEqual(len(set(keys + [r["variables"]["key"]])), 3)

    def test_same_key_on_token_refresh_retry(self):
        self.fake.inject("http401", op="inventorySetQuantities", count=1)
        self.conn("client").publish(self.product())
        reqs = self.fake.requests_for("inventorySetQuantities")
        self.assertEqual(len(reqs), 2)
        self.assertEqual(reqs[0]["body"]["variables"]["key"], reqs[1]["body"]["variables"]["key"])
        self.assertEqual(self.fake.oauth_hits, 2)


class FakeStrictnessTest(Base):
    """El fake debe rechazar peticiones mal formadas (menos creencias compartidas con el conector)."""

    def test_rejects_bad_requests(self):
        ok_set = {"input": {"name": "available", "reason": "correction", "ignoreCompareQuantity": True,
                            "quantities": [{"inventoryItemId": "gid://shopify/InventoryItem/1",
                                            "locationId": "gid://shopify/Location/1", "quantity": 1}]}, "key": "0" * 8 + "-" + "0" * 27}
        q_set = shopify._inventory_mutation("set", True)
        self.assertNotIn("errors", _raw(self.fake, q_set, ok_set))
        bad_cases = [
            ("mutation Hacky { productDelete(input: {id: \"x\"}) { deletedProductId } }", {}),
            (shopify.M_PRODUCT_CREATE, {"product": {}}),
            (shopify.M_PRODUCT_CREATE, {}),
            (shopify.M_PRODUCT_CREATE, {"product": {"title": "x"}, "media": [{"originalSource": 5}]}),
            (shopify.M_VARIANTS_UPDATE, {"productId": "gid://shopify/Product/1", "variants": [{"price": "1.00"}]}),
            (shopify.M_VARIANTS_UPDATE, {"variants": []}),
            (shopify.M_VARIANTS_UPDATE, {"productId": "gid://shopify/Product/1", "variants": [{"id": "x", "price": 5}]}),
            (shopify.M_PRODUCT_UPDATE, {"product": {"status": "DRAFT"}}),
            (shopify.M_PRODUCT_DELETE, {"input": {}}),
            (q_set, {**ok_set, "input": {**ok_set["input"], "quantities": [{**ok_set["input"]["quantities"][0], "quantity": "1"}]}}),
            (q_set, {**ok_set, "input": {**ok_set["input"], "ignoreCompareQuantity": None}}),
            (q_set, {k: v for k, v in ok_set.items() if k != "key"}),
            (shopify._inventory_mutation("set", False), ok_set),                # sin @idempotent en 2026-04
            (shopify.Q_FIND_BY_TAG, {}),
        ]
        for q, v in bad_cases:
            r = _raw(self.fake, q, v)
            self.assertIn("errors", r, q[:60])
        self.assertEqual(self.st["products"], {})
        r = _raw(self.fake, shopify._inventory_mutation("set", False), ok_set, version="2026-01")
        self.assertNotIn("errors", r)
        r = _raw(self.fake, q_set, ok_set, version="2026-01")                   # directiva desconocida en versión vieja
        self.assertIn("errors", r)

    def test_oauth_rejects_json_body(self):
        resp = shttp.request_json("POST", self.fake.token_template.format(shop=SHOP),
                                  {"client_id": CID, "client_secret": CSECRET, "grant_type": "client_credentials"})
        self.assertEqual(resp.status, 400)


class OwnershipTest(Base):
    """El SKU DF-<id> es local a cada base de datos: solo se adopta o borra lo que lleva las etiquetas de DropFinder."""

    def seed(self, tags):
        return self.fake.seed_product(SHOP, tags)

    def test_foreign_installation_tag_is_a_conflict(self):
        gid = self.seed(["dropfinder", "dfi-otra", "df-sku:DF-1"])
        with self.assertRaises(StoreValidationError) as cm:
            self.conn().publish(self.product())
        self.assertIn("no creó esta instalación", str(cm.exception))
        self.assertEqual(self.fake.ops().count("productCreate"), 0)
        self.assertNotIn("productVariantsBulkUpdate", self.fake.ops())
        self.assertEqual(list(self.st["products"]), [gid])
        self.assertEqual(self.st["variants"][self.st["products"][gid]["variant"]]["price"], "0.00")   # intacto

    def test_customer_product_with_sku_tag_only_is_a_conflict(self):
        self.seed(["df-sku:DF-1"])
        with self.assertRaises(StoreValidationError):
            self.conn().publish(self.product())
        self.assertNotIn("productCreate", self.fake.ops())

    def test_missing_dfi_or_dropfinder_is_a_conflict(self):
        for tags in (["dropfinder", "df-sku:DF-1"], ["dfi-test1", "df-sku:DF-1"]):
            self.fake.requests.clear()
            self.st["products"].clear()
            self.seed(tags)
            with self.assertRaises(StoreValidationError, msg=tags):
                self.conn().publish(self.product())
            self.assertNotIn("productCreate", self.fake.ops())

    def test_no_ownership_tags_never_adopts(self):
        self.seed(["df-sku:DF-1"])
        with self.assertRaises(StoreValidationError):
            self.conn().publish(self.product(tags=[]))

    def test_own_tags_adopted_case_insensitive(self):
        gid = self.seed(["DropFinder", "DFI-TEST1", "df-sku:DF-1"])
        ref = self.conn().publish(self.product())
        self.assertEqual(ref.external_id, gid)
        self.assertNotIn("productCreate", self.fake.ops())

    def test_foreign_and_own_both_present_adopts_own(self):
        self.seed(["dropfinder", "dfi-otra", "df-sku:DF-1"])
        own = self.seed(["dropfinder", "dfi-test1", "df-sku:DF-1"])
        self.assertEqual(self.conn().publish(self.product()).external_id, own)

    def test_delete_refuses_foreign_products(self):
        for tags in (["x"], [], ["dropfinder"], ["dfi-test1"], ["dropfinder", "dfi-"], ["dropfinder-x", "dfi"]):
            self.fake.requests.clear()
            gid = self.seed(tags)
            with self.assertRaises(StoreValidationError, msg=tags):
                self.conn().delete(ListingRef(gid))
            self.assertNotIn("productDelete", self.fake.ops())
            self.assertIn(gid, self.st["products"])

    def test_delete_own_and_gone(self):
        gid = self.seed(["Dropfinder", "dfi-abc"])
        self.conn().delete(ListingRef(gid))
        self.assertNotIn(gid, self.st["products"])
        self.fake.requests.clear()
        self.conn().delete(ListingRef(gid))                      # ya no existe: sin error y sin productDelete
        self.assertNotIn("productDelete", self.fake.ops())

    def test_orphan_cleanup_deletes_only_what_this_call_created(self):
        foreign = self.seed(["x"])
        self.fake.inject("http500", op="inventorySetQuantities")
        with self.assertRaises(StoreError):
            self.conn().publish(self.product())
        self.assertEqual(list(self.st["products"]), [foreign])
        deletes = self.fake.requests_for("productDelete")
        self.assertEqual(len(deletes), 1)
        self.assertNotEqual(deletes[0]["body"]["variables"]["input"]["id"], foreign)


class InlineTimeBudgetTest(Base):
    def test_cleanup_deadline(self):
        self.fake.inject("throttled", op="productVariantsBulkUpdate")
        self.fake.inject("throttled", op="productDelete", count=10)
        clock = iter(range(0, 1000, 10))
        with mock.patch.object(shopify, "_mono", lambda: next(clock)):
            with self.assertRaises(StoreRateLimited) as cm:
                self.conn().publish(self.product())
        self.assertEqual(self.fake.ops().count("productDelete"), 1)          # 10 s + espera > 6 s: no se reintenta
        self.assertTrue(cm.exception.orphan_external_id)
        self.assertEqual(self.sleeps, [])

    def test_total_sleeps_bounded_per_call(self):
        self.fake.cost_available = 0                                          # cada respuesta pide esperar
        self.conn().publish(self.product())
        self.assertGreater(len(self.sleeps), 0)
        self.assertLessEqual(sum(self.sleeps), shopify.MAX_INLINE_SLEEP)
        self.sleeps.clear()
        self.conn().validate()                                                # el presupuesto es por llamada
        self.assertGreater(sum(self.sleeps), 0)

    def test_cleanup_waits_fit_in_budget(self):
        self.fake.cost_available = 0
        self.fake.inject("throttled", op="productVariantsBulkUpdate")
        self.fake.inject("throttled", op="productDelete", count=10)
        with self.assertRaises(StoreRateLimited):
            self.conn().publish(self.product())
        self.assertLessEqual(sum(self.sleeps), shopify.MAX_INLINE_SLEEP)
        self.assertLessEqual(shopify.CLEANUP_DEADLINE, 6.0)
        self.assertEqual(self.fake.ops().count("productDelete"), 1)          # presupuesto agotado: sin reintentos inútiles

    def test_total_timeout_passed_to_shared_client(self):
        seen = []
        real_json, real = shttp.request_json, shttp.request

        def spy_json(*a, **k):
            seen.append(k.get("total_timeout"))
            return real_json(*a, **k)

        def spy(*a, **k):
            seen.append(k.get("total_timeout"))
            return real(*a, **k)
        with mock.patch.object(shttp, "request_json", spy_json), mock.patch.object(shttp, "request", spy):
            self.conn("client").validate()
        self.assertGreaterEqual(len(seen), 2)
        self.assertTrue(all(t is not None and 0 < t <= 45.0 for t in seen), seen)


if __name__ == "__main__":
    unittest.main()
