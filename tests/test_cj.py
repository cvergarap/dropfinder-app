"""Conector de CJ Dropshipping contra el servidor falso local (tests/fake_cj.py). Sin red real.

El fake implementa lo que el conector SUPONE de la API (ver «SIN VERIFICAR» en backend/connectors/cj.py): estas pruebas
demuestran que el conector hace lo que dice con esas suposiciones, no que CJ las cumpla.
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DROPFINDER_DB", os.path.join(tempfile.mkdtemp(), "test.db"))
os.environ["DROPFINDER_SYNTH_HISTORY"] = "0"
for _k in ("no_proxy", "NO_PROXY"):                 # el servidor falso es local: nada de proxies
    os.environ[_k] = "127.0.0.1,localhost"

import authtest  # noqa: E402,F401  (fija el contexto de propietario 1 en el hilo principal)
import auth_context  # noqa: E402
import cj_mod  # noqa: E402
import db  # noqa: E402
import hooks  # noqa: E402
import search  # noqa: E402
import secrets_store  # noqa: E402
import server  # noqa: E402
from connectors import cj  # noqa: E402
from connectors.base import PriceStock  # noqa: E402
from store_connectors import http as shttp  # noqa: E402
from store_connectors.base import StoreAuthError, StoreError, StoreRateLimited  # noqa: E402
from fake_cj import API_KEY, FakeCJ  # noqa: E402


class Clock:
    """Reloj y sueño simulados: `sleep` solo suma al reloj (las pruebas de espera no esperan de verdad)."""

    def __init__(self):
        self.t = 0.0
        self.wall0 = time.time()
        self.sleeps: list[float] = []

    def monotonic(self):
        return self.t

    def wall(self):
        return self.wall0 + self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


class CJBase(unittest.TestCase):
    def setUp(self):
        self.old_loop = shttp.ALLOW_LOOPBACK
        shttp.ALLOW_LOOPBACK = True
        cj.forget_tokens()
        self.fake = FakeCJ().start()
        self.clock = Clock()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self.fake.stop()
        shttp.ALLOW_LOOPBACK = self.old_loop
        cj.forget_tokens()

    def conn(self, key=API_KEY, **kw):
        kw.setdefault("min_interval", 0)
        kw.setdefault("clock", self.clock.monotonic)
        kw.setdefault("wall", self.clock.wall)
        kw.setdefault("sleep", self.clock.sleep)
        return cj.CJConnector(key, base_url=self.fake.base_url, **kw)

    def seed(self, n=3, **kw):
        for i in range(n):
            self.fake.add_product(f"p{i}", name=f"Producto {i}", price=f"{i + 1}.50", **kw)


class TokenTest(CJBase):
    def test_token_is_fetched_once_and_cached(self):
        self.seed()
        c = self.conn()
        c.search("")
        c.search("")
        c.get_stock("p0")
        self.assertEqual(self.fake.ops().count("token"), 1)
        self.assertTrue(all(r["token_valid"] for r in self.fake.requests if r["op"] == "list"))
        # otro conector con la misma clave reutiliza el token (la obtención está limitada por CJ)
        self.conn().search("")
        self.assertEqual(self.fake.ops().count("token"), 1)

    def test_expired_access_token_is_refreshed_with_refresh_token(self):
        self.seed()
        c = self.conn()
        c.search("")
        self.clock.t += 15 * 86400                       # el token de acceso caduca (el de refresco, no)
        c.search("")
        self.assertEqual(self.fake.ops(), ["token", "list", "refresh", "list"])
        self.assertEqual(self.fake.requests_for("refresh")[0]["body"]["refreshToken"][:10], "ref-token-")

    def test_refresh_rejected_falls_back_to_api_key(self):
        self.seed()
        c = self.conn()
        c.search("")
        self.fake.reject_refresh = True
        self.clock.t += 15 * 86400
        c.search("")
        self.assertEqual(self.fake.ops(), ["token", "list", "refresh", "token", "list"])

    def test_refresh_token_expired_goes_straight_to_api_key(self):
        self.seed()
        c = self.conn()
        c.search("")
        self.clock.t += 181 * 86400
        c.search("")
        self.assertEqual(self.fake.ops(), ["token", "list", "token", "list"])

    def test_401_refreshes_once_and_retries(self):
        self.seed()
        c = self.conn()
        c.search("")
        self.fake.expire_access()                        # el servidor ya no acepta el token que el cliente cree vigente
        res = c.search("")
        self.assertEqual(len(res.items), 3)
        self.assertEqual(self.fake.ops(), ["token", "list", "list", "refresh", "list"])
        self.assertEqual([r["token_valid"] for r in self.fake.requests_for("list")], [True, False, True])

    def test_repeated_401_gives_up_after_one_refresh(self):
        self.seed()
        c = self.conn()
        c.search("")
        self.fake.inject("http401", op="list", count=50)
        with self.assertRaises(StoreAuthError) as cm:
            c.search("")
        self.assertEqual(self.fake.ops().count("refresh"), 1)
        self.assertEqual(self.fake.ops().count("list"), 3)    # 1 inicial + 1 con el token viejo + 1 con el renovado
        self.assertNotIn(API_KEY, str(cm.exception))

    def test_code_in_envelope_also_means_token_expired(self):
        self.seed()
        c = self.conn()
        c.search("")
        self.fake.inject("code_token", op="list", count=1)
        self.assertEqual(len(c.search("").items), 3)
        self.assertEqual(self.fake.ops().count("refresh"), 1)

    def test_invalid_api_key_message_does_not_leak_it(self):
        bad = "mala-clave-" + "z" * 30
        with self.assertRaises(StoreAuthError) as cm:
            self.conn(bad).validate()
        self.assertNotIn(bad, str(cm.exception))
        self.assertNotIn(bad, repr(self.conn(bad)))
        self.assertEqual(self.fake.ops(), ["token"])           # no sigue intentando

    def test_token_endpoint_rate_limit_is_clear_and_not_retried(self):
        self.fake.token_rate_limited = True
        with self.assertRaises(StoreRateLimited) as cm:
            self.conn().validate()
        self.assertIn("5 minutos", str(cm.exception))
        self.assertEqual(self.fake.ops(), ["token"])
        self.assertEqual(self.clock.sleeps, [])

    def test_hostile_token_responses(self):
        for data in (None, [], "x", {}, {"accessToken": ""}, {"accessToken": "con espacios y \n"},
                     {"accessToken": 12345678}, {"accessToken": "a" * 5000}):
            cj.forget_tokens()
            fake = self.fake
            with fake.lock:
                fake.injections = [{"kind": "raw", "op": "token", "count": 1, "data": data}]
            with self.assertRaises(StoreError):
                self.conn().validate()

    def test_unparseable_expiry_does_not_break_and_is_bounded(self):
        now = 2_000_000_000.0
        self.assertEqual(cj._parse_expiry("basura", now), now + cj.DEFAULT_TOKEN_TTL)
        self.assertEqual(cj._parse_expiry("2001-01-01T00:00:00+08:00", now), now + 60)           # ya vencido
        self.assertEqual(cj._parse_expiry("9999-12-31T00:00:00Z", now), now + cj.MAX_TOKEN_TTL)  # a siglos vista
        self.assertEqual(cj._parse_expiry(None, now), now + cj.DEFAULT_TOKEN_TTL)
        self.assertEqual(cj._parse_expiry("2001-01-01T00:00:00", now), now + 60)                  # sin zona


class RateLimitTest(CJBase):
    def test_429_backs_off_with_retry_after_then_succeeds(self):
        self.seed()
        c = self.conn()
        self.fake.inject("http429", op="list", count=2, retry_after=3)
        self.assertEqual(len(c.search("").items), 3)
        self.assertEqual(self.clock.sleeps, [3.0, 3.0])
        self.assertEqual(self.fake.ops().count("list"), 3)

    def test_429_without_retry_after_is_exponential(self):
        self.seed()
        c = self.conn()
        self.fake.inject("http429", op="list", count=3)
        c.search("")
        self.assertEqual(self.clock.sleeps, [1.0, 2.0, 4.0])

    def test_envelope_code_429_is_rate_limit_too(self):
        self.seed()
        c = self.conn()
        self.fake.inject("code429", op="list", count=1)
        c.search("")
        self.assertEqual(self.clock.sleeps, [1.0])

    def test_backoff_is_capped_even_if_cj_asks_for_more(self):
        self.seed()
        c = self.conn(call_deadline=600)
        self.fake.inject("http429", op="list", count=1, retry_after=99999)
        c.search("")
        self.assertEqual(self.clock.sleeps, [cj.BACKOFF_CAP])

    def test_gives_up_after_max_retries_with_rate_limited_error(self):
        self.seed()
        c = self.conn(call_deadline=10_000)
        self.fake.inject("http429", op="list", count=99)
        with self.assertRaises(StoreRateLimited) as cm:
            c.search("")
        self.assertEqual(self.fake.ops().count("list"), cj.MAX_RETRIES + 1)
        self.assertEqual(len(self.clock.sleeps), cj.MAX_RETRIES)
        self.assertGreater(cm.exception.retry_after, 0)

    def test_total_deadline_stops_waiting(self):
        self.seed()
        c = self.conn(call_deadline=5)                    # 1 + 2 caben; la espera de 4 s ya no
        self.fake.inject("http429", op="list", count=99)
        t0 = self.clock.t
        with self.assertRaises(StoreRateLimited):
            c.search("")
        self.assertLessEqual(self.clock.t - t0, 5)
        self.assertEqual(self.clock.sleeps, [1.0, 2.0])

    def test_writes_are_never_retried(self):
        self.seed()
        c = self.conn()
        self.fake.inject("http429", op="freight", count=5)
        with self.assertRaises(StoreRateLimited):
            c._call("POST", cj.PATHS["freight"], payload={"products": []}, safe=False)
        self.assertEqual(self.fake.ops().count("freight"), 1)
        self.assertEqual(self.clock.sleeps, [])

    def test_min_interval_spaces_requests(self):
        self.seed()
        c = self.conn(min_interval=1.0)
        c.search("")
        c.search("")
        c.search("")
        self.assertEqual(self.clock.sleeps, [1.0, 1.0])

    def test_5xx_is_retried_for_reads(self):
        self.seed()
        c = self.conn()
        self.fake.inject("http500", op="list", count=2)
        self.assertEqual(len(c.search("").items), 3)
        self.assertEqual(self.clock.sleeps, [1.0, 2.0])


class HostileResponsesTest(CJBase):
    def test_not_json_truncated_dropped_are_errors_not_crashes(self):
        self.seed()
        for kind in ("notjson", "truncated", "drop"):
            c = self.conn(max_retries=0)
            self.fake.inject(kind, op="list", count=1)
            with self.assertRaises(StoreError, msg=kind) as cm:
                c.search("")
            self.assertTrue(cm.exception.retryable, kind)

    def test_not_json_is_retried_for_reads(self):
        self.seed()
        self.fake.inject("notjson", op="list", count=1)
        self.assertEqual(len(self.conn().search("").items), 3)

    def test_oversize_response_is_refused(self):
        self.seed()
        self.fake.inject("oversize", op="list", count=1)
        with self.assertRaises(StoreError) as cm:
            self.conn().search("")
        self.assertIn("demasiado grande", str(cm.exception))

    def test_redirect_is_not_followed(self):
        self.seed()
        self.fake.inject("redirect", op="list", count=1)
        with self.assertRaises(StoreError) as cm:
            self.conn().search("")
        self.assertIn("redirección", str(cm.exception))
        self.assertEqual(self.fake.ops().count("list"), 1)

    def test_error_messages_talk_about_cj_not_a_store(self):
        self.seed()
        self.fake.inject("drop", op="list", count=1)
        with self.assertRaises(StoreError) as cm:
            self.conn(max_retries=0).search("")
        self.assertNotIn("tienda", str(cm.exception).lower())

    def test_wrong_shapes_of_data(self):
        for data in (None, [], "texto", 42, {"list": "no soy lista"}, {"list": [None, 1, "x", [], {}]},
                     {"list": [{"pid": 1}]}, {"total": "abc", "list": []}):
            cj.forget_tokens()
            with self.fake.lock:
                self.fake.injections = [{"kind": "raw", "op": "list", "count": 1, "data": data}]
            c = self.conn(max_retries=0)
            try:
                res = c.search("")
            except StoreError:
                continue                       # error controlado: bien
            self.assertEqual(res.items, [], repr(data))

    def test_hostile_product_fields_are_sanitized_or_dropped(self):
        evil = [
            {"pid": "ok1", "productNameEn": "<img src=x onerror=alert(1)>Taza <b>azul</b>‮\x00", "sellPrice": "2.5",
             "categoryName": "<script>x</script>Hogar", "productImage": "javascript:alert(1)"},
            {"pid": "ok2", "productNameEn": "Con imagen", "sellPrice": "1.2 -- 3.4",
             "productImage": "https://u:p@evil.example.com/a.jpg"},
            {"pid": "ok3", "productNameEn": "Lista", "sellPrice": 5, "productImage": '["https://cdn.example.com/a.png"]'},
            {"pid": "bad1", "productNameEn": "Precio NaN", "sellPrice": "NaN"},
            {"pid": "bad2", "productNameEn": "Negativo", "sellPrice": "-5"},
            {"pid": "bad3", "productNameEn": "", "sellPrice": "5"},
            {"pid": "../../etc/passwd", "productNameEn": "Ruta", "sellPrice": "5"},
            {"pid": "bad4", "productNameEn": "Infinito", "sellPrice": 1e999},
            {"pid": "bad5", "productNameEn": "Objeto", "sellPrice": {"a": 1}},
            {"pid": "ok1", "productNameEn": "Duplicado", "sellPrice": "9"},
            {"pid": "ok6", "productNameEn": "x" * 5000, "sellPrice": "7", "categoryName": ["no", "texto"]},
        ]
        with self.fake.lock:
            self.fake.injections = [{"kind": "raw", "op": "list", "count": 1, "data": {"total": 11, "list": evil}}]
        res = self.conn().search("")
        by = {p.supplier_sku: p for p in res.items}
        self.assertEqual(sorted(by), ["ok1", "ok2", "ok3", "ok6"])
        self.assertEqual(res.skipped, 7)
        p1 = by["ok1"]
        self.assertNotIn("<", p1.title)
        self.assertNotIn("onerror=alert(1)>", p1.title)
        self.assertEqual(p1.title, "Taza azul")
        self.assertEqual(p1.category, "Hogar")
        self.assertEqual(p1.image, "")                                   # javascript: descartado
        self.assertEqual(by["ok2"].image, "")                            # con usuario:clave descartado
        self.assertEqual(by["ok2"].cost, 3.4)                            # rango → máximo
        self.assertEqual(by["ok3"].image, "https://cdn.example.com/a.png")
        self.assertEqual(len(by["ok6"].title), 200)
        self.assertEqual(by["ok6"].category, "Sin categoría")
        for p in res.items:
            self.assertEqual((p.orders, p.orders_7d, p.orders_prev_7d, p.rating, p.reviews, p.stock), (0,) * 5 + (0,))
            self.assertEqual(p.supplier, "CJ Dropshipping")
            self.assertTrue(p.product_url.startswith("https://"))

    def test_helpers_money(self):
        for raw, want in [("1.2 -- 3.4", 3.4), ("1.2-3.4", 3.4), (" USD 3.5 ", 3.5), ("1,234.50", 1234.5), (7, 7.0),
                          (0.1 + 0.2, 0.3), ("5", 5.0)]:
            self.assertEqual(float(cj.parse_money(raw)), want, raw)
        for raw in (None, "", "abc", "NaN", "inf", -1, "-3", 0, "0", float("nan"), float("inf"), True, [], {}, "1e999",
                    "9" * 40, 10 ** 12, "1 2 3 4 5"):
            self.assertIsNone(cj.parse_money(raw), repr(raw))

    def test_helpers_text_url_int(self):
        self.assertEqual(cj.clean_text("  a\n\tb  ​c "), "a b c")
        self.assertEqual(cj.clean_text("&lt;b&gt;hola&lt;/b&gt;"), "hola")
        self.assertEqual(cj.clean_text(None), "")
        self.assertEqual(cj.clean_text({"a": 1}), "")
        self.assertEqual(cj.clean_text("\ud800 suelto"), "suelto")
        self.assertEqual(cj.clean_text("a" * 500, 10), "a" * 10)
        for ok in ("https://a.com/x.jpg", "http://a.com:8080/p?q=1"):
            self.assertEqual(cj.safe_url(ok), ok)
        for bad in ("javascript:alert(1)", "data:text/html,x", "//a.com/x", "ftp://a.com/x", "https://", "http://u:p@a.com/",
                    "https://a.com/ x", "https://a.com/\n/x", "https://a.com:99999/", "x" * 600, None, 5, ["a"]):
            self.assertEqual(cj.safe_url(bad), "", repr(bad))
        self.assertEqual(cj.parse_int("12"), 12)
        self.assertEqual(cj.parse_int(-5), 0)
        self.assertEqual(cj.parse_int(float("nan")), 0)
        self.assertEqual(cj.parse_int("1e30"), cj.MAX_STOCK)
        self.assertEqual(cj.parse_int(None, 7), 7)
        self.assertEqual(cj.parse_int(True, 7), 7)

    def test_real_total_deadline_with_a_slow_server(self):
        self.seed()
        self.fake.inject("sleep", op="list", count=99, seconds=3)
        c = cj.CJConnector(API_KEY, base_url=self.fake.base_url, min_interval=0, call_deadline=1.5, max_retries=5)
        t0 = time.monotonic()
        with self.assertRaises(StoreError):
            c.search("")
        self.assertLess(time.monotonic() - t0, 6)


class HostTest(CJBase):
    def test_host_is_fixed(self):
        for bad in ("https://evil.example.com/api2.0/v1", "https://developers.cjdropshipping.com.evil.com/api2.0/v1",
                    "http://developers.cjdropshipping.com/api2.0/v1", "https://developers.cjdropshipping.com:8443/x",
                    "http://127.0.0.1.evil.com:80/x", "file:///etc/passwd", "http://user@127.0.0.1:9/"):
            with self.assertRaises(StoreError, msg=bad):
                cj.CJConnector(API_KEY, base_url=bad)
        c = cj.CJConnector(API_KEY, base_url="https://developers.cjdropshipping.com/otra/ruta")
        self.assertEqual(c.base_url, cj.BASE_URL)                     # el prefijo oficial es fijo
        shttp.ALLOW_LOOPBACK = False
        with self.assertRaises(StoreError):                           # fuera de pruebas, loopback no se permite
            cj.CJConnector(API_KEY, base_url=self.fake.base_url)

    def test_bad_api_keys_are_rejected_locally(self):
        for bad in ("", "con espacio", "a\nb", "x" * 201, None, 5):
            with self.assertRaises(StoreAuthError):
                cj.CJConnector(bad)


class ProductsTest(CJBase):
    def test_pagination_and_iter_catalog_cap(self):
        for i in range(120):
            self.fake.add_product(f"p{i:03d}", name=f"Lámpara {i}", price="2.00")
        c = self.conn()
        first = c.search("lámpara", page=1, page_size=50)
        self.assertEqual((len(first.items), first.total, first.has_more), (50, 120, True))
        last = c.search("lámpara", page=3, page_size=50)
        self.assertEqual((len(last.items), last.has_more), (20, False))
        self.assertEqual(len(list(c.iter_catalog("lámpara", limit=200))), 120)
        self.assertEqual(len(list(c.iter_catalog("lámpara", limit=75))), 75)
        self.assertEqual(c.search("nada que ver").items, [])
        # el tope de página se respeta aunque se pida más
        c.search("", page_size=10_000)
        self.assertEqual(self.fake.requests_for("list")[-1]["qs"]["pageSize"], str(cj.MAX_PAGE_SIZE))

    def test_query_and_category_are_sent_sanitized(self):
        self.seed()
        self.conn().search("<b>taza</b>\n&", category_id="cat-cocina")
        qs = self.fake.requests_for("list")[-1]["qs"]
        self.assertEqual(qs["productNameEn"], "taza &")
        self.assertEqual(qs["categoryId"], "cat-cocina")
        self.conn().search("x", category_id="../../x y")
        self.assertNotIn("categoryId", self.fake.requests_for("list")[-1]["qs"])

    def test_detail_with_variants_stock_and_shipping(self):
        self.fake.add_product("d1", name="Detalle", price="4.00", stock={"US": 5, "CN": 30},
                              variants=[{"vid": "d1-a", "variantSku": "S-A", "variantNameEn": "<i>Rojo</i>",
                                         "variantSellPrice": "4.00", "variantImage": "https://img.example.com/a.jpg"},
                                        {"vid": "bad vid", "variantSellPrice": "x"},
                                        "no soy dict"])
        c = self.conn()
        d = c.get_product_detail("d1", ship_to="co")
        self.assertEqual([v["vid"] for v in d.variants], ["d1-a"])
        self.assertEqual(d.variants[0]["name"], "Rojo")
        self.assertEqual((d.product.shipping_cost, d.product.shipping_days), (4.5, 12))
        p = c.get_product("d1")
        self.assertEqual((p.stock, p.warehouse, p.cost), (35, "CN", 4.0))
        self.assertIsNone(c.get_product("no-existe"))
        self.assertIsNone(c.get_product("../raro"))

    def test_stock_unknown_is_none_and_hostile_values_are_bounded(self):
        self.seed(1)
        c = self.conn()
        for data, want in [({"variantInventories": []}, None), ({}, None), (None, None), ("x", None),
                           ([{"countryCode": "us", "storageNum": "7"}], (7, "US")),
                           ({"variantInventories": [{"inventory": [{"countryCode": "US", "totalInventory": -4},
                                                                   {"countryCode": "CN", "totalInventory": "abc"},
                                                                   {"countryCode": "<x>", "totalInventory": 3},
                                                                   {"countryCode": "DE", "cjInventory": 2,
                                                                    "factoryInventory": 5}]}]}, (10, "DE")),   # país inválido suma al total sin almacén
                           ({"variantInventories": [{"inventory": [{"countryCode": "US", "totalInventory": 1e30}]}]},
                            (cj.MAX_STOCK, "US"))]:
            with self.fake.lock:
                self.fake.injections = [{"kind": "raw", "op": "stock", "count": 1, "data": data}]
            st = c.get_stock("p0")
            self.assertEqual(None if st is None else (st.total, st.warehouse), want, repr(data))

    def test_categories_flattened(self):
        cats = self.conn().get_categories()
        self.assertEqual([c["id"] for c in cats], ["cat-cocina", "cat-bano"])
        self.assertEqual(cats[0]["path"], "Hogar › Cocina › Utensilios")

    def test_get_price_stock_for_the_monitor(self):
        self.fake.add_product("m1", name="Monitor", price="5.00", stock={"CN": 12})
        c = self.conn()
        cur = PriceStock(cost=5.0, shipping_cost=1.25, stock=12)
        self.assertEqual(c.get_price_stock("m1", current=cur), PriceStock(5.0, 1.25, 12))
        self.fake.set_product("m1", sellPrice="6.50", stock={"CN": 0, "US": 0})
        new = c.get_price_stock("m1", current=cur)
        self.assertEqual((new.cost, new.shipping_cost, new.stock), (6.5, 1.25, 0))
        self.assertIsNone(c.get_price_stock("no-existe"))
        self.fake.set_product("m1", sellPrice="basura")
        self.assertIsNone(c.get_price_stock("m1"))                       # dato inválido: omitido, no alertas falsas
        # la respuesta de otro producto no se acepta como la de este
        with self.fake.lock:
            self.fake.injections = [{"kind": "raw", "op": "detail", "count": 1,
                                     "data": {"pid": "otro", "productNameEn": "X", "sellPrice": "1"}}]
        self.assertIsNone(c.get_price_stock("m1"))

    def test_connector_does_not_leak_secret_in_requests_log_or_errors(self):
        self.seed()
        c = self.conn()
        c.search("")
        for r in self.fake.requests:
            self.assertNotIn(API_KEY, json.dumps(r["qs"]))
            if r["op"] != "token":
                self.assertNotIn(API_KEY, json.dumps(r["body"]))
        self.assertNotIn(API_KEY, repr(c))


if __name__ == "__main__":
    unittest.main()
