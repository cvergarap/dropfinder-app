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
from test_stores_base import _TempDb  # noqa: E402

ApiError = server.ApiError


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

    def test_connector_itself_scrubs_cj_error_text(self):
        """Capa 1: el conector no deja pasar la clave ni siquiera si el llamador no limpiara nada."""
        self.fake.inject("code", op="token", count=1, code=1600001, message=f"mala {API_KEY} <b>x</b>")
        with self.assertRaises(StoreAuthError) as cm:
            self.conn().validate()
        self.assertNotIn(API_KEY, str(cm.exception))
        self.seed(1)
        c = self.conn()
        c.search("")
        self.fake.inject("code", op="list", count=1, code=1234, message=f"fallo con {API_KEY}")
        with self.assertRaises(StoreError) as cm:
            c.search("")
        self.assertNotIn(API_KEY, str(cm.exception))

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


# ====================================================================== módulo (API, importación, monitor)

class ModBase(CJBase):
    def setUp(self):
        super().setUp()
        self.tmp = _TempDb()
        db.init_db(seed_size=20)
        auth_context.set_current(1, "owner")
        cj_mod.reset_job()
        self.set_kwargs()
        self.addCleanup(self._mod_cleanup)

    def _mod_cleanup(self):
        cj_mod.reset_job()
        cj_mod.CONNECTOR_KWARGS.clear()
        auth_context.set_current(1, "owner")
        self.tmp.close()

    def set_kwargs(self, **kw):
        cj_mod.CONNECTOR_KWARGS.clear()
        cj_mod.CONNECTOR_KWARGS.update(base_url=self.fake.base_url, min_interval=0, **kw)

    def use_fake_clock(self):
        self.set_kwargs(clock=self.clock.monotonic, wall=self.clock.wall, sleep=self.clock.sleep)

    def connect(self, key=API_KEY):
        return cj_mod.connect({"api_key": key})

    def run_import(self, **body):
        body.setdefault("query", "gadget")
        body.setdefault("limit", 200)
        cj_mod.start_import(body)
        cj_mod.wait_for_import()
        return cj_mod._snapshot()

    def seed_many(self, n, price="2.00"):
        for i in range(n):
            self.fake.add_product(f"c{i:03d}", name=f"Gadget {i}", price=price, stock={"CN": 10 + i})

    def products(self):
        with db.connect() as conn:
            return {r["supplier_sku"]: dict(r) for r in conn.execute(
                "SELECT * FROM products WHERE supplier = 'CJ Dropshipping' AND source = 'api'")}

    def import_for(self, uid, pid, price=30.0):
        with auth_context.as_user(uid, "owner" if uid == 1 else "member"):
            return server.create_import({"product_id": pid, "sell_price": price})


class ConnectTest(ModBase):
    def test_connect_validates_stores_encrypted_and_never_returns_key(self):
        st = self.connect()
        self.assertTrue(st["connected"] and st["unverified"])
        self.assertNotIn(API_KEY, json.dumps(st))
        with db.connect() as conn:
            row = conn.execute("SELECT * FROM supplier_connections").fetchone()
        self.assertNotIn(API_KEY, json.dumps(dict(row)))
        self.assertEqual(secrets_store.decrypt(row["credentials_enc"], cj_mod.AAD), API_KEY)
        with self.assertRaises(secrets_store.SecretError):
            secrets_store.decrypt(row["credentials_enc"], "supplier:otro")        # atada a su fila
        # ni la base (ni su WAL) contienen la clave en claro
        for suffix in ("", "-wal"):
            path = db.DB_PATH + suffix
            if os.path.exists(path):
                with open(path, "rb") as f:
                    self.assertNotIn(API_KEY.encode(), f.read())
        self.assertEqual(self.fake.ops()[:2], ["token", "list"])                  # validó contra CJ antes de guardar

    def test_invalid_key_is_not_saved_and_message_is_clean(self):
        bad = "clave-mala-" + "q" * 25
        with self.assertRaises(ApiError) as cm:
            self.connect(bad)
        self.assertEqual((cm.exception.status, cm.exception.code), (400, "cj_auth"))
        self.assertNotIn(bad, str(cm.exception))
        self.assertFalse(cj_mod.status()["connected"])

    def test_error_text_from_cj_is_scrubbed_of_the_key(self):
        self.fake.inject("code", op="token", count=1, code=1600001,
                         message=f"Invalid apiKey {API_KEY} <script>alert(1)</script> Bearer abcdefghijklmnop")
        with self.assertRaises(ApiError) as cm:
            self.connect()
        msg = str(cm.exception)
        self.assertNotIn(API_KEY, msg)
        self.assertNotIn("abcdefghijklmnop", msg)
        self.assertNotIn("<script>", msg)

    def test_module_layer_scrubs_errors_even_if_connector_leaked(self):
        """Capa 2: aunque un conector dejara la clave en un error, la API no la devuelve."""
        from unittest import mock
        for exc in (StoreAuthError(f"rechazada {API_KEY}"), StoreRateLimited(f"espera {API_KEY}"),
                    StoreError(f"caída {API_KEY}")):
            with mock.patch.object(cj.CJConnector, "validate", side_effect=exc):
                with self.assertRaises(ApiError) as cm:
                    self.connect()
            self.assertNotIn(API_KEY, str(cm.exception), type(exc).__name__)
        self.connect()
        with mock.patch.object(cj.CJConnector, "get_categories", side_effect=StoreError(f"x {API_KEY}")):
            with self.assertRaises(ApiError) as cm:
                cj_mod.categories()
        self.assertNotIn(API_KEY, str(cm.exception))

    def test_bad_input(self):
        for body in (None, [], {}, {"api_key": ""}, {"api_key": 5}, {"api_key": "con espacio"},
                     {"api_key": "x" * 300}, {"api_key": "a\nb"}):
            with self.assertRaises(ApiError) as cm:
                cj_mod.connect(body)
            self.assertEqual(cm.exception.status, 400, body)
        self.assertEqual(self.fake.requests, [])                                  # ni siquiera molestó a CJ

    def test_rate_limited_and_unreachable_are_mapped(self):
        self.fake.token_rate_limited = True
        with self.assertRaises(ApiError) as cm:
            self.connect()
        self.assertEqual((cm.exception.status, cm.exception.code), (429, "cj_rate_limited"))
        self.fake.token_rate_limited = False
        self.fake.stop()
        with self.assertRaises(ApiError) as cm:
            self.connect()
        self.assertEqual((cm.exception.status, cm.exception.code), (502, "cj_unreachable"))
        self.assertFalse(cj_mod.status()["connected"])

    def test_reconnect_replaces_the_key_and_keeps_one_row(self):
        self.connect()
        key2 = "otra@api@" + "m3" * 12
        self.fake.api_keys.add(key2)
        self.connect(key2)
        with db.connect() as conn:
            rows = conn.execute("SELECT credentials_enc FROM supplier_connections").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(secrets_store.decrypt(rows[0][0], cj_mod.AAD), key2)

    def test_validate_marks_error_when_key_is_revoked_and_recovers(self):
        self.connect()
        self.fake.api_keys.clear()
        cj.forget_tokens()
        with self.assertRaises(ApiError) as cm:
            cj_mod.validate_connection()
        self.assertEqual(cm.exception.code, "cj_auth")
        st = cj_mod.status()
        self.assertEqual(st["status"], "error")
        self.assertNotIn(API_KEY, st["last_error"] or "")
        self.fake.api_keys.add(API_KEY)
        self.assertEqual(cj_mod.validate_connection()["status"], "active")

    def test_lost_encryption_key_is_reported_not_silently_replaced(self):
        self.connect()
        os.remove(secrets_store.key_path())
        secrets_store.forget_cache()
        with self.assertRaises(ApiError) as cm:
            cj_mod.validate_connection()
        self.assertEqual((cm.exception.status, cm.exception.code), (409, "credentials_unreadable"))
        self.assertFalse(os.path.exists(secrets_store.key_path()))               # no se generó una clave nueva
        self.assertEqual(cj_mod.status()["status"], "error")

    def test_disconnect_keeps_products_and_forgets_tokens(self):
        self.seed_many(3)
        self.connect()
        self.run_import()
        self.assertEqual(len(self.products()), 3)
        out = cj_mod.disconnect()
        self.assertEqual(out, {"ok": True, "products_kept": 3})
        self.assertFalse(cj_mod.status()["connected"])
        self.assertEqual(len(self.products()), 3)
        self.assertEqual(cj._TOKENS, {})
        with self.assertRaises(ApiError) as cm:
            cj_mod.start_import({"query": "g"})
        self.assertEqual(cm.exception.code, "not_connected")

    def test_disconnect_refused_while_importing(self):
        self.connect()
        with cj_mod._job_lock:
            cj_mod._job = {"state": "running", "warnings": []}
        with self.assertRaises(ApiError) as cm:
            cj_mod.disconnect()
        self.assertEqual(cm.exception.code, "import_running")

    def test_schema_is_lazy_atomic_idempotent_and_needs_no_new_version(self):
        with db.connect() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
            self.assertIsNone(conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'supplier_connections'").fetchone())
        errors = []

        def go():
            try:
                for _ in range(5):
                    with db.connect() as conn:
                        cj_mod.ensure_schema(conn)
            except Exception as e:  # noqa: BLE001
                errors.append(e)
        ts = [threading.Thread(target=go) for _ in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(errors, [])
        with db.connect() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'supplier_connections'")
                             .fetchone()[0], 1)

    def test_encrypted_supplier_data_counts_as_encrypted_data(self):
        self.connect()
        os.remove(secrets_store.key_path())
        secrets_store.forget_cache()
        with self.assertRaises(secrets_store.SecretError):
            secrets_store.encrypt("x")                                            # no inventa otra clave en silencio


class ImportTest(ModBase):
    def test_import_with_pagination_cap_and_fields(self):
        self.seed_many(130)
        self.connect()
        self.fake.requests.clear()
        job = self.run_import(query="gadget", limit=120)
        self.assertEqual((job["state"], job["new"], job["updated"], job["total"]), ("done", 120, 0, 120))
        self.assertEqual(self.fake.ops().count("list"), 3)                        # 50 + 50 + 20
        self.assertEqual(self.fake.ops().count("stock"), 120)
        prods = self.products()
        self.assertEqual(len(prods), 120)
        p = prods["c005"]
        self.assertEqual((p["cost"], p["stock"], p["warehouse"], p["source"], p["orders"], p["rating"], p["shipping_cost"]),
                         (2.0, 15, "CN", "api", 0, 0, 0))
        self.assertEqual(p["category"], "Utensilios")
        self.assertTrue(p["product_url"].startswith("https://cjdropshipping.com/"))
        self.assertGreater(p["suggested_price"], 0)                               # métricas calculadas como en el CSV

    def test_import_cap_is_enforced(self):
        self.connect()
        for bad in (0, 201, -1, True, "5", None, 1.5):
            with self.assertRaises(ApiError, msg=bad) as cm:
                cj_mod.start_import({"query": "x", "limit": bad})
            self.assertEqual(cm.exception.status, 400)
        with self.assertRaises(ApiError):
            cj_mod.start_import({"limit": 5})                                     # sin palabra clave ni categoría
        with self.assertRaises(ApiError):
            cj_mod.start_import({"category_id": "../../x", "limit": 5})
        self.seed_many(250)
        job = self.run_import(query="gadget", limit=200)
        self.assertEqual(job["new"], 200)
        self.assertEqual(len(self.products()), 200)

    def test_import_is_idempotent_and_updates_changes(self):
        self.seed_many(10)
        self.connect()
        touched = []
        hooks.on_catalog_write(lambda conn, ids: touched.append(list(ids or [])))
        self.addCleanup(lambda: hooks._catalog_write.pop(
            [k for k in hooks._catalog_write if k.endswith("<lambda>") and "test_cj" in k][-1], None))
        self.run_import(query="gadget", limit=50)
        first = self.products()
        job = self.run_import(query="gadget", limit=50)
        self.assertEqual((job["new"], job["updated"]), (0, 10))
        self.assertEqual(len(self.products()), 10)
        self.assertEqual({k: v["id"] for k, v in first.items()}, {k: v["id"] for k, v in self.products().items()})
        self.assertGreaterEqual(len(touched), 2)                                  # los hooks del catálogo corrieron
        self.assertEqual(sorted(touched[-1]), sorted(v["id"] for v in first.values()))
        self.fake.set_product("c003", sellPrice="9.00", stock={"US": 0})
        self.run_import(query="gadget", limit=50)
        self.assertEqual((self.products()["c003"]["cost"], self.products()["c003"]["stock"]), (9.0, 0))
        with db.connect() as conn:                                                # UNIQUE(supplier, sku): sin duplicados
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM products WHERE supplier_sku = 'c003'").fetchone()[0], 1)

    def test_reimport_raises_alerts_for_users_who_imported_the_product(self):
        self.seed_many(2)
        self.connect()
        self.run_import(query="gadget", limit=10)
        pid = self.products()["c000"]["id"]
        self.import_for(1, pid)
        self.fake.set_product("c000", sellPrice="4.00", stock={"CN": 0})
        job = self.run_import(query="gadget", limit=10)
        self.assertEqual(job["alerts"], 2)
        with db.connect() as conn:
            types = sorted(r[0] for r in conn.execute("SELECT type FROM alerts WHERE user_id = 1"))
        self.assertEqual(types, ["out_of_stock", "price_up"])

    def test_import_does_not_overwrite_user_loaded_rows(self):
        self.seed_many(3)
        self.connect()
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO products(supplier, supplier_sku, source, title, category, image, cost, shipping_cost, "
                "shipping_days, warehouse, stock, orders, orders_7d, orders_prev_7d, rating, reviews, updated_at) "
                "VALUES ('CJ Dropshipping', 'c001', 'csv', 'Mío', 'Hogar', '', 99, 1, 7, 'MX', 5, 0, 0, 0, 4, 1, ?)",
                (db.now(),))
        job = self.run_import(query="gadget", limit=10)
        self.assertEqual((job["new"], job["conflicts"]), (2, 1))
        with db.connect() as conn:
            r = conn.execute("SELECT title, cost, source FROM products WHERE supplier_sku = 'c001'").fetchone()
        self.assertEqual((r[0], r[1], r[2]), ("Mío", 99, "csv"))

    def test_hostile_catalog_is_sanitized_and_never_breaks_the_import(self):
        self.fake.add_product("h1", name="<img src=x onerror=alert(1)>Taza\u202e x", price="1.2 -- 3.4",
                              category="<b>Cat</b>", image="javascript:alert(1)")
        self.fake.add_product("h2", name="Sin precio x", price="NaN")
        self.fake.add_product("h3", name="Neg x", price="-4")
        self.fake.add_product("h4", name="Ok x", price="2", stock={"US": "muchos", "CN": -3})
        self.connect()
        cj_mod.start_import({"query": "x", "limit": 50})
        cj_mod.wait_for_import()
        job = cj_mod._snapshot()
        self.assertEqual((job["state"], job["new"], job["invalid"]), ("done", 2, 2))
        prods = self.products()
        self.assertEqual(sorted(prods), ["h1", "h4"])
        self.assertEqual((prods["h1"]["title"], prods["h1"]["category"], prods["h1"]["image"], prods["h1"]["cost"]),
                         ("Taza x", "Cat", "", 3.4))
        self.assertEqual(prods["h4"]["stock"], 0)

    def test_unknown_stock_does_not_import_new_and_keeps_previous_stock(self):
        self.seed_many(3)
        self.connect()
        self.run_import(query="gadget", limit=10)
        before = self.products()["c001"]["stock"]
        self.assertGreater(before, 0)
        pid = self.products()["c001"]["id"]
        self.import_for(1, pid)
        self.fake.add_product("c999", name="Gadget nuevo", price="2.00")
        for sku in ("c001", "c999"):
            self.fake.set_product(sku, stock={"US": "N/A"})                       # sin dato real de stock
        job = self.run_import(query="gadget", limit=10)
        self.assertEqual((job["new"], job["stock_unknown"], job["unknown_skipped"], job["stock_failed"]), (0, 2, 1, 0))
        self.assertNotIn("c999", self.products())                                 # nuevo sin stock real: no se importa
        self.assertEqual(self.products()["c001"]["stock"], before)                # el existente conserva su stock
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM alerts WHERE type = 'out_of_stock'").fetchone()[0], 0)

    def test_rate_limit_during_stock_imports_what_it_has_and_reports(self):
        self.seed_many(6)
        self.connect()
        self.use_fake_clock()
        # las 2 primeras consultas de stock pasan; las demás reciben 429 hasta agotar los reintentos
        orig = self.fake._take_injection
        calls = {"n": 0}

        def take(op):
            if op == "stock":
                calls["n"] += 1
                if calls["n"] > 2:
                    return {"kind": "http429", "op": op, "count": 1}
            return orig(op)
        self.fake._take_injection = take
        job = self.run_import(query="gadget", limit=10)
        self.assertEqual((job["state"], job["new"], job["rate_limited"], job["stock_failed"]), ("done", 2, True, 4))
        self.assertTrue(job["warnings"])
        self.assertEqual(len(self.products()), 2)                                 # nada a medias: sin stock real no se importa
        self.assertLessEqual(sum(self.clock.sleeps), 3 * cj.BACKOFF_CAP)

    def test_rate_limit_on_first_page_is_an_error_with_clear_message(self):
        self.seed_many(2)
        self.connect()
        self.use_fake_clock()
        self.fake.inject("http429", op="list", count=99)
        job = self.run_import(query="gadget", limit=10)
        self.assertEqual((job["state"], job["rate_limited"]), ("error", True))
        self.assertIn("esperar", job["error"])
        self.assertEqual(len(self.products()), 0)

    def test_revoked_key_during_import_marks_connection(self):
        self.seed_many(2)
        self.connect()
        self.fake.api_keys.clear()
        self.fake.expire_access()
        cj.forget_tokens()
        job = self.run_import(query="gadget", limit=10)
        self.assertEqual(job["state"], "error")
        self.assertNotIn(API_KEY, json.dumps(job))
        self.assertEqual(cj_mod.status()["status"], "error")

    def test_only_one_import_at_a_time(self):
        self.seed_many(3)
        self.connect()
        self.fake.inject("sleep", op="list", count=1, seconds=0.6)
        cj_mod.start_import({"query": "gadget", "limit": 5})
        with self.assertRaises(ApiError) as cm:
            cj_mod.start_import({"query": "gadget", "limit": 5})
        self.assertEqual((cm.exception.status, cm.exception.code), (409, "import_running"))
        cj_mod.wait_for_import()
        self.assertEqual(cj_mod._snapshot()["state"], "done")

    def test_category_import_and_categories_endpoint(self):
        self.fake.add_product("k1", name="Una", price="2", categoryId="cat-cocina")
        self.fake.add_product("k2", name="Otra", price="2", categoryId="cat-bano")
        self.connect()
        self.assertEqual([c["id"] for c in cj_mod.categories()["items"]], ["cat-cocina", "cat-bano"])
        job = self.run_import(query="", category_id="cat-bano", limit=10)
        self.assertEqual(sorted(self.products()), ["k2"])
        self.assertEqual(job["category_id"], "cat-bano")

    def test_secrets_never_reach_logs_or_job(self):
        import logging
        import io
        buf = io.StringIO()
        h = logging.StreamHandler(buf)
        h.setLevel(logging.DEBUG)
        root = logging.getLogger()
        old = root.level
        root.setLevel(logging.DEBUG)
        root.addHandler(h)
        self.addCleanup(lambda: (root.removeHandler(h), root.setLevel(old)))
        self.seed_many(3)
        self.connect()
        self.fake.inject("http500", op="stock", count=1)
        job = self.run_import(query="gadget", limit=10)
        self.assertNotIn(API_KEY, buf.getvalue())
        self.assertNotIn(API_KEY, json.dumps(job))
        self.assertNotIn(API_KEY, json.dumps(cj_mod.status()))
        with self.fake.lock:
            tokens = list(self.fake.access_tokens) + list(self.fake.refresh_tokens)
        for t in tokens:
            self.assertNotIn(t, buf.getvalue())
            self.assertNotIn(t, json.dumps(cj_mod.status()))


class MonitorTest(ModBase):
    def setUp(self):
        super().setUp()
        self.seed_many(3, price="5.00")
        self.connect()
        self.run_import(query="gadget", limit=10)
        self.pids = {k: v["id"] for k, v in self.products().items()}
        self.iids = {k: self.import_for(1, pid, 20.0)["id"] for k, pid in self.pids.items()}
        self.fake.requests.clear()

    def test_price_up_and_out_of_stock_alerts(self):
        self.fake.set_product("c000", sellPrice="8.00")
        self.fake.set_product("c001", stock={"CN": 0, "US": 0})
        res = server.run_monitor(user_id=1)
        self.assertEqual((res["checked"], res["skipped"], res["cooldown"]), (3, 0, 0))
        kinds = sorted((a["title"], a["type"]) for a in res["alerts"])
        self.assertEqual(kinds, [("Gadget 0", "price_up"), ("Gadget 1", "out_of_stock")])
        p = self.products()
        self.assertEqual((p["c000"]["cost"], p["c001"]["stock"]), (8.0, 0))
        self.assertEqual(self.fake.ops().count("detail"), 3)
        self.assertEqual(self.fake.ops().count("stock"), 3)
        # el costo de envío conocido se conserva (CJ no se consulta por envío en el monitor)
        self.assertEqual(p["c000"]["shipping_cost"], 0)

    def test_cooldown_prevents_new_requests_and_force_overrides(self):
        server.run_monitor(user_id=1)
        n = len(self.fake.requests)
        res = server.run_monitor(user_id=1)
        self.assertEqual((res["checked"], res["cooldown"]), (0, 3))
        self.assertEqual(len(self.fake.requests), n)
        res = server.run_monitor(user_id=1, force=True)
        self.assertEqual(res["checked"], 3)
        self.assertGreater(len(self.fake.requests), n)

    def test_back_in_stock_and_price_down(self):
        self.fake.set_product("c002", stock={"CN": 0})
        server.run_monitor(user_id=1)
        self.fake.set_product("c002", stock={"CN": 9}, sellPrice="3.00")
        res = server.run_monitor(user_id=1, force=True)
        self.assertEqual(sorted(a["type"] for a in res["alerts"] if a["title"] == "Gadget 2"),
                         ["back_in_stock", "price_down"])

    def test_rate_limit_stops_without_false_alerts_and_reports(self):
        self.use_fake_clock()
        self.fake.inject("http429", op="detail", count=99)
        res = server.run_monitor(user_id=1)
        self.assertEqual((res["checked"], res["alerts"]), (0, []))
        self.assertEqual(res["skipped"], 3)
        self.assertIn("esperar", res["notice"])
        self.assertEqual(self.fake.ops().count("detail"), cj.MAX_RETRIES + 1)      # se detuvo: no insiste con los demás
        self.assertEqual(self.products()["c000"]["cost"], 5.0)

    def test_hostile_or_invalid_data_is_skipped_not_alerted(self):
        self.fake.set_product("c000", sellPrice="basura")
        self.fake.inject("notjson", op="detail", count=99)
        self.set_kwargs(max_retries=0)
        res = server.run_monitor(user_id=1)
        self.assertEqual((res["checked"], res["alerts"]), (0, []))

    def test_revoked_key_marks_connection_and_stops(self):
        self.fake.api_keys.clear()
        self.fake.expire_access()
        cj.forget_tokens()
        res = server.run_monitor(user_id=1)
        self.assertEqual(res["checked"], 0)
        self.assertIn("rechazó", res["notice"])
        self.assertEqual(cj_mod.status()["status"], "error")
        self.assertNotIn(API_KEY, json.dumps(res))

    def test_not_connected_is_skipped_with_notice(self):
        cj_mod.disconnect()
        res = server.run_monitor(user_id=1)
        self.assertEqual((res["checked"], res["skipped"]), (0, 3))
        self.assertIn("no está conectado", res["notice"])

    def test_per_run_cap(self):
        n = cj_mod.MONITOR_MAX_PRODUCTS + 5
        self.seed_many(n)
        self.run_import(query="gadget", limit=200)
        with db.connect() as conn:
            ids = [r[0] for r in conn.execute("SELECT id FROM products WHERE source = 'api' AND id NOT IN "
                                              "(SELECT product_id FROM imported_products)")]
        for pid in ids:
            self.import_for(1, pid)
        self.fake.requests.clear()
        res = server.run_monitor(user_id=1)
        self.assertEqual(res["checked"], cj_mod.MONITOR_MAX_PRODUCTS)
        self.assertIn("Solo se revisaron", res["notice"])

    def test_network_calls_do_not_hold_the_write_lock(self):
        self.fake.inject("sleep", op="detail", count=1, seconds=1.0)
        out = {}
        t = threading.Thread(target=lambda: out.setdefault("r", self._run_as_owner()))
        t.start()
        time.sleep(0.3)                                                           # el monitor está esperando a CJ
        t0 = time.monotonic()
        conn = db.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('prueba', '1')")
            conn.commit()
        finally:
            conn.close()
        self.assertLess(time.monotonic() - t0, 0.5)
        t.join(10)
        self.assertEqual(out["r"]["checked"], 3)

    def _run_as_owner(self):
        with auth_context.as_user(1, "owner"):
            return server.run_monitor(user_id=1)

    def test_users_only_see_and_get_their_own_alerts(self):
        with db.connect() as conn:
            conn.execute("INSERT INTO users(email, name, password_hash, role, created_at) "
                         "VALUES ('m@x.co', 'Miembro', 'x', 'member', ?)", (db.now(),))
        self.import_for(2, self.pids["c000"], 25.0)
        self.fake.set_product("c000", sellPrice="9.00")
        with auth_context.as_user(2, "member"):
            res = server.run_monitor()
        self.assertEqual([a["type"] for a in res["alerts"]], ["price_up"])
        with db.connect() as conn:
            owners = sorted(r[0] for r in conn.execute("SELECT user_id FROM alerts WHERE type = 'price_up'"))
        self.assertEqual(owners, [1, 2])                                          # cada alerta es de su dueño
        with auth_context.as_user(2, "member"):                                   # el miembro no puede forzar
            self.fake.set_product("c000", sellPrice="12.00")
            res = server.run_monitor(force=True)
        self.assertEqual((res["checked"], res["cooldown"]), (0, 1))


class PermissionsTest(ModBase):
    def member(self):
        with db.connect() as conn:
            conn.execute("INSERT OR IGNORE INTO users(id, email, name, password_hash, role, created_at) "
                         "VALUES (2, 'm@x.co', 'Miembro', 'x', 'member', ?)", (db.now(),))
        return auth_context.as_user(2, "member")

    def test_routes_are_classified(self):
        want = {("GET", r"/api/suppliers/cj"): "user", ("POST", r"/api/suppliers/cj"): "owner",
                ("DELETE", r"/api/suppliers/cj"): "owner", ("POST", r"/api/suppliers/cj/validate"): "owner",
                ("GET", r"/api/suppliers/cj/categories"): "owner", ("POST", r"/api/suppliers/cj/import"): "owner"}
        got = {(m, p): server.route_access(m, p) for m, p, _ in cj_mod.ROUTES}
        self.assertEqual(got, want)
        for key in want:
            self.assertTrue(key in server.USER_ROUTES or key in server.OWNER_ONLY, key)
            self.assertNotIn(key, server.USER_ROUTES if want[key] == "owner" else server.OWNER_ONLY)
        for route in cj_mod.ROUTES:
            self.assertIn(route, server.ROUTES)

    def test_member_cannot_manage_even_calling_handlers_directly(self):
        self.connect()
        with self.member():
            for fn, args in ((cj_mod.connect, ({"api_key": API_KEY},)), (cj_mod.disconnect, ()),
                             (cj_mod.validate_connection, ()), (cj_mod.categories, ()),
                             (cj_mod.start_import, ({"query": "x", "limit": 1},))):
                with self.assertRaises(ApiError, msg=fn.__name__) as cm:
                    fn(*args)
                self.assertEqual(cm.exception.status, 403)
            st = cj_mod.status()
        self.assertFalse(st["can_manage"])
        self.assertTrue(st["connected"])
        self.assertEqual(set(st), {"provider", "connected", "status", "last_error", "connected_at", "last_validated_at",
                                   "last_import_at", "products", "can_manage", "max_import", "unverified", "import"})
        self.assertEqual(self.fake.ops().count("token"), 1)                       # el miembro no gastó la API de CJ


class HttpTest(ModBase):
    def setUp(self):
        super().setUp()
        import auth
        self.srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (self.srv.shutdown(), self.srv.server_close()))
        self.owner = authtest.auth_headers()
        with db.connect() as conn:
            conn.execute("INSERT INTO users(id, email, name, password_hash, role, created_at) "
                         "VALUES (2, 'm@x.co', 'Miembro', 'x', 'member', ?)", (db.now(),))
            tok, csrf = auth.create_session(conn, 2)
        self.member = authtest.session_headers(tok, csrf)

    def call(self, method, path, body=None, headers=None, raw=False):
        import urllib.error
        import urllib.request
        h = {"Content-Type": "application/json", **(headers if headers is not None else self.owner)}
        req = urllib.request.Request(self.base + path, method=method, headers=h,
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req) as r:
                text = r.read().decode()
                return r.status, text if raw else json.loads(text)
        except urllib.error.HTTPError as e:
            text = e.read().decode()
            return e.code, text if raw else json.loads(text)

    def test_full_flow_over_http_and_key_never_in_any_response(self):
        self.seed_many(4)
        texts = []
        st, body = self.call("POST", "/api/suppliers/cj", {"api_key": API_KEY}, raw=True)
        texts.append(body)
        self.assertEqual(st, 200)
        st, body = self.call("GET", "/api/suppliers/cj", raw=True)
        texts.append(body)
        st, body = self.call("POST", "/api/suppliers/cj/validate", raw=True)
        texts.append(body)
        st, body = self.call("POST", "/api/suppliers/cj/import", {"query": "gadget", "limit": 10}, raw=True)
        texts.append(body)
        self.assertEqual(st, 200)
        cj_mod.wait_for_import()
        st, body = self.call("GET", "/api/suppliers/cj", raw=True)
        texts.append(body)
        self.assertEqual(json.loads(body)["import"]["new"], 4)
        st, body = self.call("POST", "/api/suppliers/cj", {"api_key": "mala-" + "x" * 20}, raw=True)
        texts.append(body)
        self.assertEqual(st, 400)
        st, body = self.call("GET", "/api/suppliers", raw=True)
        texts.append(body)
        for t in texts:
            self.assertNotIn(API_KEY, t)
            self.assertNotIn("credentials_enc", t)

    def test_member_gets_403_owner_actions_but_can_read_status(self):
        self.connect()
        for method, path, body in (("POST", "/api/suppliers/cj", {"api_key": API_KEY}),
                                   ("DELETE", "/api/suppliers/cj", None),
                                   ("POST", "/api/suppliers/cj/validate", None),
                                   ("GET", "/api/suppliers/cj/categories", None),
                                   ("POST", "/api/suppliers/cj/import", {"query": "x", "limit": 1})):
            st, data = self.call(method, path, body, headers=self.member)
            self.assertEqual(st, 403, (method, path))
            self.assertIn("propietario", data["error"])
        st, data = self.call("GET", "/api/suppliers/cj", headers=self.member)
        self.assertEqual((st, data["can_manage"]), (200, False))
        self.assertTrue(cj_mod.status()["connected"])

    def test_csrf_and_auth_are_required(self):
        no_csrf = {k: v for k, v in self.owner.items() if k != "X-CSRF-Token"}
        st, _ = self.call("POST", "/api/suppliers/cj", {"api_key": API_KEY}, headers=no_csrf)
        self.assertEqual(st, 403)
        st, _ = self.call("POST", "/api/suppliers/cj/import", {"query": "x", "limit": 1}, headers=no_csrf)
        self.assertEqual(st, 403)
        st, _ = self.call("GET", "/api/suppliers/cj", headers={})
        self.assertEqual(st, 401)
        st, _ = self.call("DELETE", "/api/suppliers/cj", headers={})
        self.assertEqual(st, 401)
        self.assertFalse(cj_mod.status()["connected"])

    def test_oversized_body_is_refused(self):
        st, data = self.call("POST", "/api/suppliers/cj", {"api_key": "x" * 20000})
        self.assertEqual(st, 413)


# ====================================================================== revisión independiente (PR #22)

class ReviewConnectorTest(CJBase):
    def test_validate_never_empties_the_token_cache_even_if_get_token_is_rate_limited(self):
        self.seed()
        c = self.conn()
        c.search("")
        self.fake.token_rate_limited = True
        self.assertTrue(c.validate()["ok"])                         # usa el token en caché
        self.assertEqual(len(c.search("").items), 3)                # y todo lo demás sigue funcionando
        self.assertEqual(self.fake.ops().count("token"), 1)
        self.assertIn(c._key, cj._TOKENS)

    def test_stock_garbage_is_unknown_never_zero(self):
        self.seed(1)
        c = self.conn()
        for bad in ("N/A", "", {}, [1], True, "unlimited", None, [], "1e5", "0x10", float("nan")):
            data = {"variantInventories": [{"inventory": [{"countryCode": "US", "totalInventory": bad}]}]}
            with self.fake.lock:
                self.fake.injections = [{"kind": "raw", "op": "stock", "count": 1, "data": data}]
            self.assertIsNone(c.get_stock("p0"), repr(bad))
        for good, want in (("0", 0), (0, 0), ("12", 12), (7.9, 7), (-3, 0), ("-3", 0)):
            data = {"variantInventories": [{"inventory": [{"countryCode": "US", "totalInventory": good}]}]}
            with self.fake.lock:
                self.fake.injections = [{"kind": "raw", "op": "stock", "count": 1, "data": data}]
            self.assertEqual(c.get_stock("p0").total, want, repr(good))

    def test_huge_numbers_do_not_raise(self):
        self.seed(1)
        c = self.conn()
        self.assertEqual(cj.parse_int(10 ** 400), cj.MAX_STOCK)
        self.assertEqual(cj.parse_int("9" * 29), cj.MAX_STOCK)
        self.assertEqual(cj.parse_stock(10 ** 400), cj.MAX_STOCK)
        self.assertIsNone(cj.parse_money(10 ** 400))
        data = {"variantInventories": [{"inventory": [{"countryCode": "US", "totalInventory": 10 ** 400}]}]}
        with self.fake.lock:
            self.fake.injections = [{"kind": "raw", "op": "stock", "count": 1, "data": data}]
        self.assertEqual(c.get_stock("p0").total, cj.MAX_STOCK)
        with self.fake.lock:
            self.fake.injections = [{"kind": "raw", "op": "list", "count": 1, "data": {"total": 10 ** 400, "list": []}}]
        self.assertEqual(c.search("").total, cj.MAX_STOCK)

    def test_has_more_without_total_and_with_clipped_page_size(self):
        for i in range(120):
            self.fake.add_product(f"p{i:03d}", name=f"Lámpara {i}", price="2.00")
        c = self.conn()
        self.fake.omit_total = True
        res = c.search("lámpara", page=1, page_size=50)
        self.assertEqual((res.total, res.total_known, res.has_more), (0, False, True))
        self.assertEqual(len(list(c.iter_catalog("lámpara", limit=200))), 120)      # sigue hasta una página vacía/corta
        self.fake.omit_total = False
        self.fake.clip_page_size = 20                                               # CJ recorta pageSize y lo informa
        self.assertEqual(len(list(c.iter_catalog("lámpara", limit=200))), 120)
        self.assertEqual(len(list(c.iter_catalog("lámpara", limit=70))), 70)

    def test_concurrent_token_requests_collapse_to_one(self):
        self.seed()
        self.fake.inject("sleep", op="token", count=1, seconds=0.4)
        errors = []

        def go():
            try:
                self.conn().search("")
            except Exception as e:  # noqa: BLE001
                errors.append(e)
        ts = [threading.Thread(target=go) for _ in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(errors, [])
        self.assertEqual(self.fake.ops().count("token"), 1)

    def test_token_io_does_not_hold_the_global_lock(self):
        self.seed()
        self.fake.inject("sleep", op="token", count=1, seconds=0.8)
        t = threading.Thread(target=lambda: self.conn().search(""))
        t.start()
        time.sleep(0.3)
        got = cj._LOCK.acquire(timeout=0.2)                                         # otra clave/estado no queda bloqueado
        if got:
            cj._LOCK.release()
        t.join()
        self.assertTrue(got)

    def test_invalidate_only_affects_the_given_token(self):
        self.seed()
        c = self.conn()
        c.search("")
        good = cj._TOKENS[c._key].access
        c._invalidate_access("otro-token-viejo")                                    # otro hilo ya lo renovó
        self.assertEqual(cj._TOKENS[c._key].access_exp > 0, True)
        c._invalidate_access(good)
        self.assertEqual(cj._TOKENS[c._key].access_exp, 0.0)

    def test_pace_does_not_reserve_a_slot_when_it_gives_up(self):
        c = self.conn(min_interval=5.0)
        with cj._LOCK:
            cj._NEXT_OK[c._key] = self.clock.t + 100
        with self.assertRaises(StoreRateLimited):
            c._pace(self.clock.t + 10)
        self.assertEqual(cj._NEXT_OK[c._key], self.clock.t + 100)

    def test_token_cache_is_bounded(self):
        self.seed(1)
        for i in range(cj._MAX_ENTRIES + 6):
            key = f"clave{i}@api@" + "x" * 12
            self.fake.api_keys.add(key)
            self.conn(key).search("")
        self.assertLessEqual(len(cj._TOKENS), cj._MAX_ENTRIES)
        self.assertLessEqual(len(cj._NEXT_OK), cj._MAX_ENTRIES)

    def test_safe_url_rejects_attribute_breaking_characters(self):
        for bad in ('https://img.example.com/b.jpg"onerror="alert(1)', "https://a.com/x'y", "https://a.com/<x>",
                    "https://a.com/ñ.jpg", "https://a.com/`x", "https://a.com/x\\y", "https://a.com/a b"):
            self.assertEqual(cj.safe_url(bad), "", bad)
        self.assertEqual(cj.safe_url("https://a.com/p%20q.jpg?x=1&y=2"), "https://a.com/p%20q.jpg?x=1&y=2")

    def test_server_error_message_is_scrubbed_of_the_key(self):
        self.seed(1)
        c = self.conn(max_retries=0)
        c.search("")
        self.fake.inject("code", op="list", count=1, code=503, message=f"boom {API_KEY}")
        with self.assertRaises(StoreError) as cm:
            c.search("")
        self.assertNotIn(API_KEY, str(cm.exception))


class ReviewModuleTest(ModBase):
    def test_monitor_never_starves_products_beyond_the_cap(self):
        n = cj_mod.MONITOR_MAX_PRODUCTS + 10
        self.seed_many(n)
        self.connect()
        self.run_import(query="gadget", limit=200)
        with db.connect() as conn:
            ids = [r[0] for r in conn.execute("SELECT id FROM products WHERE source = 'api' ORDER BY id")]
        for pid in ids:
            self.import_for(1, pid)
        checked = set()

        def checked_ids():
            with db.connect() as conn:
                return {r[0] for r in conn.execute("SELECT product_id FROM monitor_checks")}
        r1 = server.run_monitor(user_id=1)
        self.assertEqual(r1["checked"], cj_mod.MONITOR_MAX_PRODUCTS)
        r2 = server.run_monitor(user_id=1)                                          # los 40 primeros están en enfriamiento
        self.assertEqual(r2["checked"], 10)
        self.assertEqual(checked_ids(), set(ids))                                   # todos revisados al menos una vez
        old = "2000-01-01T00:00:00+00:00"                                           # pasa el enfriamiento
        with db.connect() as conn:
            conn.execute("UPDATE monitor_checks SET checked_at = ?", (old,))
            conn.execute("UPDATE monitor_checks SET checked_at = ? WHERE product_id IN (%s)" % ",".join(map(str, ids[:5])),
                         ("2001-01-01T00:00:00+00:00",))
        self.fake.requests.clear()
        server.run_monitor(user_id=1)
        asked = [r["qs"]["pid"] for r in self.fake.requests_for("detail")]
        self.assertEqual(len(asked), cj_mod.MONITOR_MAX_PRODUCTS)
        self.assertEqual(asked[:1] and len(set(asked)), cj_mod.MONITOR_MAX_PRODUCTS)

    def test_monitor_survives_hostile_values_and_unexpected_exceptions(self):
        from unittest import mock
        self.seed_many(2)
        self.connect()
        self.run_import(query="gadget", limit=10)
        for pid in [v["id"] for v in self.products().values()]:
            self.import_for(1, pid)
        data = {"variantInventories": [{"inventory": [{"countryCode": "US", "totalInventory": 10 ** 400}]}]}
        self.fake.inject("raw", op="stock", count=1, data=data)
        res = server.run_monitor(user_id=1)
        self.assertEqual(res["checked"], 2)
        with mock.patch.object(cj.CJConnector, "get_price_stock", side_effect=ValueError("dato hostil")), \
                self.assertLogs("dropfinder.cj", "ERROR"):
            res = server.run_monitor(user_id=1, force=True)
        self.assertEqual((res["checked"], res["skipped"]), (0, 2))
        self.assertIn("datos inesperados", res["notice"])

    def test_pagination_cut_warns(self):
        self.seed_many(130)
        self.connect()
        old = cj_mod.MAX_PAGES
        cj_mod.MAX_PAGES = 2
        self.addCleanup(lambda: setattr(cj_mod, "MAX_PAGES", old))
        job = self.run_import(query="gadget", limit=200)
        self.assertEqual(job["new"], 100)
        self.assertTrue(any("se cortó" in w for w in job["warnings"]), job["warnings"])

    def test_import_without_total_still_gets_everything(self):
        self.seed_many(120)
        self.connect()
        self.fake.omit_total = True
        job = self.run_import(query="gadget", limit=200)
        self.assertEqual(job["new"], 120)

    def test_monitor_only_treats_cj_supplier_as_cj(self):
        with db.connect() as conn:
            cur = conn.execute(
                "INSERT INTO products(supplier, supplier_sku, source, title, category, image, cost, shipping_cost, "
                "shipping_days, warehouse, stock, orders, orders_7d, orders_prev_7d, rating, reviews, updated_at) "
                "VALUES ('Otra API', 'x1', 'api', 'Otro', 'Hogar', '', 5, 1, 7, 'US', 5, 0, 0, 0, 4, 1, ?)", (db.now(),))
            pid = cur.lastrowid
            conn.execute("INSERT INTO monitor_checks(product_id, checked_at) VALUES (?, ?)", (pid, db.now()))
        self.import_for(1, pid)
        res = server.run_monitor(user_id=1)
        self.assertEqual((res["checked"], res["skipped"], res["cooldown"]), (0, 1, 0))
        self.assertEqual(self.fake.requests, [])

    def test_unknown_shipping_is_flagged_and_excluded_from_best(self):
        import compare
        self.seed_many(1)
        self.connect()
        self.run_import(query="gadget", limit=5)
        cjp = self.products()["c000"]
        with db.connect() as conn:
            conn.execute("UPDATE products SET title = 'Lámpara mesa roja', category = 'Hogar', match_key = ? WHERE id = ?",
                         (compare.match_key("Lámpara mesa roja", "Hogar"), cjp["id"]))
            ids = []
            for sup, cost, ship, days in (("Tienda A", 10.0, 2.0, 8), ("Tienda B", 12.0, 1.0, 5)):
                ids.append(conn.execute(
                    "INSERT INTO products(supplier, supplier_sku, source, title, category, image, cost, shipping_cost, "
                    "shipping_days, warehouse, stock, orders, orders_7d, orders_prev_7d, rating, reviews, updated_at) "
                    "VALUES (?, 'k1', 'csv', 'Lámpara mesa roja', 'Hogar', '', ?, ?, ?, 'US', 5, 0, 0, 0, 4, 1, ?)",
                    (sup, cost, ship, days, db.now())).lastrowid)
            db.recompute_metrics(conn)
        item = server.get_product(cjp["id"])
        self.assertFalse(item["shipping_known"])
        self.assertTrue(server.get_product(ids[0])["shipping_known"])
        alts = compare.alternatives(ids[0])
        self.assertEqual(alts["total"], 3)
        self.assertNotEqual(alts["best"]["cheapest"], cjp["id"])                    # CJ es el más barato en el papel
        self.assertNotEqual(alts["best"]["fastest"], cjp["id"])
        cmp = compare.compare({"ids": [f"{cjp['id']},{ids[0]},{ids[1]}"]})
        for field in ("total_cost", "shipping_days", "margin", "profit", "suggested_price"):
            self.assertNotEqual(cmp["best"][field], cjp["id"], field)
        self.assertEqual(cmp["best"]["total_cost"], ids[0])
        self.assertEqual(cmp["best"]["shipping_days"], ids[1])

    def test_supplier_stats_ignore_unknown_shipping(self):
        import history
        self.seed_many(2)
        self.connect()
        self.run_import(query="gadget", limit=5)
        out = history.supplier_stats({})
        row = next(i for i in out["items"] if i["source"] == "api")
        self.assertIsNone(row["avg_shipping_days"])


if __name__ == "__main__":
    unittest.main()
