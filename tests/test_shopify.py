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
                 compare_at_price=29.9, vendor="Prov", product_type="Hogar", tags=["a", "b,c"], cost=7.5)
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
        self.assertEqual(self.fake.ops(), ["locations", "productCreate", "productVariantsBulkUpdate", "inventorySetQuantities"])
        create = self.fake.requests_for("productCreate")[0]["body"]["variables"]
        p = create["product"]
        self.assertEqual(p["status"], "DRAFT")
        self.assertEqual(p["title"], "Lámpara <LED> & más")
        self.assertEqual(p["descriptionHtml"], "Línea 1<br>Línea &lt;b&gt;2&lt;/b&gt;")
        self.assertEqual((p["vendor"], p["productType"], p["tags"]), ("Prov", "Hogar", ["a", "b c"]))
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
        self.assertIsNone(bulk["compareAtPrice"])
        self.assertNotIn("cost", bulk["inventoryItem"])

    def test_max_images_and_location_setting(self):
        urls = [f"https://x.com/{i}.jpg" for i in range(15)]
        self.conn(settings={"location_id": "gid://shopify/Location/9"}).publish(self.product(image_urls=urls))
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
        self.fake.inject("http500", op="productDelete")
        with self.assertLogs("dropfinder.shopify", "WARNING"), self.assertRaises(StoreError) as cm:
            self.conn().publish(self.product())
        gid = list(self.st["products"])[0]
        self.assertEqual(cm.exception.orphan_external_id, gid)
        self.assertIn(gid, str(cm.exception))

    def test_create_failure_nothing_to_clean(self):
        self.fake.inject("http500", op="productCreate")
        with self.assertRaises(StoreError):
            self.conn().publish(self.product())
        self.assertEqual(self.fake.ops(), ["locations", "productCreate"])      # sin producto creado, nada que limpiar

    def test_missing_scope_on_publish(self):
        self.st["scopes"].discard("write_products")
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
        self.assertIsNone(self.st["variants"][self.ref.variant_id]["compareAtPrice"])
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
        self.assertEqual(self.fake.ops()[-2:], ["productDelete", "productDelete"])

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

    def test_404_not_found_outside_validate(self):
        self.fake.inject("http404")
        with self.assertRaises(StoreNotFound):
            self.conn().set_active(ListingRef("gid://shopify/Product/5"), True)

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


if __name__ == "__main__":
    unittest.main()
