"""Conector de WooCommerce contra el servidor falso local (tests/fake_woocommerce.py). Sin red real.

Incluye, al final, MutationTest: pruebas por mutación del código del conector (se ejecutan solo con DF_MUTATION=1
porque lanzan la suite de este archivo una vez por mutante). Cada mutante debe hacer fallar alguna prueba.
"""
import base64
import concurrent.futures
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "backend"))
sys.path.insert(0, HERE)
os.environ["DROPFINDER_SYNTH_HISTORY"] = "0"
for _k in ("no_proxy", "NO_PROXY"):                 # el servidor falso es local: nada de proxies
    os.environ[_k] = "127.0.0.1,localhost"

import store_connectors as sc  # noqa: E402
from store_connectors import woocommerce  # noqa: E402
from store_connectors.base import (ListingRef, StoreAuthError, StoreError, StoreNotFound, StoreProduct,  # noqa: E402
                                   StoreRateLimited, StoreValidationError)
from store_connectors import http as shttp  # noqa: E402
from fake_woocommerce import FakeWooCommerce  # noqa: E402

logging.getLogger("dropfinder.woocommerce").addHandler(logging.NullHandler())      # sin ruido en la salida

HOST = "www.mitienda.com"
CK = "ck_" + "a1" * 20
CS = "cs_" + "b2" * 20
C = woocommerce.WooCommerceConnector
BASIC = base64.b64encode(f"{CK}:{CS}".encode()).decode()
RETRYABLE_ERR = (StoreError,)


class Base(unittest.TestCase):
    def setUp(self):
        self.fake = FakeWooCommerce().start()
        self.st = self.fake.add_store(HOST)
        p = [mock.patch.object(C, "BASE_URL_TEMPLATE", self.fake.base_template),
             mock.patch.object(woocommerce, "_sleep", side_effect=lambda s: self.sleeps.append(s))]
        self.sleeps = []
        for x in p:
            x.start()
            self.addCleanup(x.stop)
        self.addCleanup(self.fake.stop)
        old, shttp.ALLOW_LOOPBACK = shttp.ALLOW_LOOPBACK, True          # solo para el servidor falso local
        self.addCleanup(lambda: setattr(shttp, "ALLOW_LOOPBACK", old))

    def conn(self, key=CK, secret=CS, domain=HOST, settings=None):
        return C(domain, {"consumer_key": key, "consumer_secret": secret}, settings)

    def product(self, **kw):
        d = dict(title="Lámpara <LED> & más", price=19.9, sku="DF-1", stock=7, description="Línea 1\nLínea <b>2</b>\n\nPárrafo 2",
                 image_urls=["https://cdn.x.com/a.jpg", "http://x.com/b.jpg", "javascript:alert(1)", "https://u:p@x.com/c.jpg",
                             "https://10.0.0.1/x.jpg"],
                 compare_at_price=9.9, vendor="Prov", product_type="Hogar", tags=["a", "b,c", "dropfinder"], cost=7.5)
        d.update(kw)
        return StoreProduct(**d)

    def count(self):
        return len(self.fake.products(HOST))

    def ref(self, p=None):
        p = p or self.fake.put_product(HOST, sku="DF-1")
        return ListingRef(external_id=str(p["id"]))


# ================================================================ dominio
VALID_DOMAINS = [
    ("mitienda.com", "mitienda.com"), ("  MiTienda.COM ", "mitienda.com"), ("https://www.mitienda.com/", "www.mitienda.com"),
    ("HTTPS://WWW.MiTienda.com/tienda/?a=1#x", "www.mitienda.com"), ("http://mitienda.com", "mitienda.com"),
    ("mitienda.com:443", "mitienda.com"), ("https://mitienda.com:443/x", "mitienda.com"),
    ("tienda-uno.com.ar", "tienda-uno.com.ar"), ("a1.b2.c3.shop", "a1.b2.c3.shop"), ("0abc.com", "0abc.com"),
    ("café.com", "xn--caf-dma.com"), ("xn--caf-dma.com", "xn--caf-dma.com"), ("tiendañandú.com.co", "xn--tiendaand-q6a6h.com.co"),
    ("a" * 63 + ".com", "a" * 63 + ".com"), ("tienda.COM.CO", "tienda.com.co"),
]

HOSTILE_DOMAINS = [
    # vacíos, tipos
    "", " ", None, 5, [], b"x.com", "\t", "\n",
    # IP en todas sus formas
    "127.0.0.1", "10.0.0.1", "192.168.1.1", "169.254.169.254", "0.0.0.0", "8.8.8.8", "2130706433", "0x7f000001", "0x7f.1",
    "127.1", "127.0.1", "0177.0.0.1", "0x7f.0.0.1", "0x7f.0x0.0x0.0x1", "017700000001", "1.1.1.1.1", "1.2.3", "01.02.03.04",
    "http://127.0.0.1/", "https://127.0.0.1:443/", "https://2130706433/", "https://0x7f.1/", "[::1]", "::1", "[::ffff:127.0.0.1]",
    "[2001:db8::1]", "https://[::1]/", "https://[::1]:443/", "::ffff:7f00:1", "fe80::1", "0:0:0:0:0:0:0:1",
    "1.2.3.4.5", "999.999.999.999", "256.1.1.1", "1.1.1.0x1", "0x7f.0.0.0xf",
    # localhost y nombres reservados / de una sola etiqueta
    "localhost", "LOCALHOST", "localhost.", "foo.localhost", "tienda.local", "tienda.internal", "tienda.lan", "x.corp",
    "x.home", "x.test", "x.invalid", "x.example", "x.onion", "x.arpa", "1.0.0.127.in-addr.arpa", "tienda", "intranet",
    "wp", "a", "x.localdomain", "x.private",
    # puertos
    "mitienda.com:80", "mitienda.com:8443", "https://mitienda.com:8080/", "mitienda.com:0", "mitienda.com:", "mitienda.com:abc",
    "mitienda.com:443:443", "mitienda.com:-1", "mitienda.com:99999", "mitienda.com:４４３", "http://mitienda.com:80",
    # userinfo y trucos de @
    "user@mitienda.com", "user:pw@mitienda.com", "https://user:pw@mitienda.com", "https://mitienda.com@evil.com",
    "evil.com@mitienda.com", "https://a@b@mitienda.com", "https://@mitienda.com", "https://:@mitienda.com",
    "mitienda.com\\@evil.com", "https://mitienda.com\\.evil.com", "mitienda.com\\evil.com",
    # esquemas
    "ftp://mitienda.com", "file:///etc/passwd", "gopher://mitienda.com", "javascript:alert(1)", "data:text/html,x",
    "//mitienda.com", "://mitienda.com", "https:/mitienda.com", "https:mitienda.com", "https//mitienda.com", "hxxps://mitienda.com",
    "https://https://mitienda.com", "ws://mitienda.com", "mailto:a@mitienda.com", "view-source:mitienda.com",
    # punto final, puntos raros, etiquetas
    "mitienda.com.", "mitienda.com..", ".mitienda.com", "mitienda..com", "mitienda.com.:443", "https://mitienda.com./",
    "https://.mitienda.com", ".", "..", "...", "-mitienda.com", "mitienda-.com", "mi_tienda.com", "mitienda.c", "mitienda.123",
    "mitienda.c0m", "mitienda.com-", "mi tienda.com", "mitienda.com/..;@evil.com:80", "ab--cd.com", "mi!tienda.com",
    "mi*tienda.com", "mi$tienda.com", "mi'tienda.com", 'mi"tienda.com', "mi<tienda.com", "mi,tienda.com", "mi;tienda.com",
    "mi=tienda.com", "mi+tienda.com", "mi(tienda).com", "mi{tienda}.com", "mi|tienda.com", "mi^tienda.com", "mi~tienda.com",
    "mi`tienda.com", "mitienda.com`", "-.com", "x.-", "xn--.com", "xn--a.com", "xn--zzzzzzzzzzzzzzzz.com", "xn--ab-.com",
    "a" * 64 + ".com", ("a" * 63 + ".") * 4 + "com", ("a." * 130) + "com", "a" * 300 + ".com",
    # codificación porcentual
    "mitienda.com%2f", "mitienda%2ecom", "%6dtienda.com", "mitienda.com%00", "mitienda.com%0d%0a", "mitienda.com%40evil.com",
    "https://mitienda.com/%2e%2e/", "https://mitienda.com/a%20b", "%", "mitienda.com%",
    # espacios y caracteres de control
    "mi tienda.com", "mitienda.com ,evil.com", "mitienda.com\n.evil.com", "mitienda.com\r\nHost: evil.com", "mitienda.com\x00",
    "mitienda.com\x00.evil.com", "mitienda.com\t.evil.com", "mitienda.com\x7f", "\x00mitienda.com", "mitienda.com\x1b",
    "mitienda.com .evil.com", "mitienda.com\u2028.x", "mitienda.com​", "mi​tienda.com", "‮mitienda.com",
    "mitienda.com﻿", "mi­tienda.com", "mitienda.com⁠", "mitienda.com",
    # unicode engañoso (homógrafos, ancho completo, otras escrituras, mezcla)
    "ｍitienda.com", "ｍｉｔｉｅｎｄａ．ｃｏｍ", "mitienda。com", "mitienda．com", "mitienda｡com", "mitienda.ｃｏｍ",
    "mitiеnda.com", "аpple.com", "mitienda.соm", "μitienda.com", "tienda.рф", "例え.jp", "店铺.com", "mitienda.中国", "δοκιμή.gr",
    "mitiendа.com", "ⓜitienda.com", "ᴍitienda.com", "mitienda.ℂom", "ı.com", "ſ.com", "ß.com", "\u212a.com", "mi̇tienda.com",
    "café.com", "ﬁ.com", "mitienda.com̸", "①.com", "mitienda.cóm", "tienda.ᴄom", "٠١٢.com", "mitienda.com҉",
    "xn--80ak6aa92e.com", "xn--e1afmkfd.com", "xn--mitienda-9ya.com.", "xn--n3h.com", "xn--caf-dma.com.", "xn--caf-dma-.com",
    "xn--tienda.com", "xn--ls8h.la", "xn--p1ai", "xn--fiq228c.com", "xn--a.xn--b.com", "xn--mi.com",
    # rutas / consultas raras
    "https://", "https:///mitienda.com", "https://?mitienda.com", "https://#mitienda.com", "/mitienda.com", "?x=mitienda.com",
    "#mitienda.com", "https://mitienda.com\\", "https://evil.com#@mitienda.com:443", "https://evil.com?@mitienda.com",
    "https://evil.com/@mitienda.com@", "https://[mitienda.com]/",
    "mitienda.com:443@evil.com", "http://mitienda.com:443@evil.com", ":443", "https://:443", "mitienda.com:+443",
    "x" * 700 + ".com", "mitienda.com/" + "a" * 700,
]


class DomainTest(unittest.TestCase):
    def test_valid(self):
        for raw, exp in VALID_DOMAINS:
            self.assertEqual(C.normalize_domain(raw), exp, raw)
            self.assertEqual(C.normalize_domain(exp), exp)             # idempotente

    def test_hostile(self):
        self.assertGreaterEqual(len(HOSTILE_DOMAINS), 200)
        for raw in HOSTILE_DOMAINS:
            with self.assertRaises(StoreValidationError, msg=repr(raw)):
                C.normalize_domain(raw)

    def test_result_is_clean_ascii_hostname(self):
        for raw, _ in VALID_DOMAINS:
            out = C.normalize_domain(raw)
            self.assertTrue(re.fullmatch(r"[a-z0-9.-]+", out), out)
            self.assertLessEqual(len(out), 253)
            self.assertIn(".", out)
            self.assertFalse(out.endswith("."))

    def test_port_message_is_clear(self):
        with self.assertRaises(StoreValidationError) as cm:
            C.normalize_domain("mitienda.com:8443")
        self.assertIn("443", str(cm.exception))
        self.assertIn("HTTPS", str(cm.exception))

    def test_error_does_not_echo_input(self):
        with self.assertRaises(StoreValidationError) as cm:
            C.normalize_domain("https://user:SUPERSECRETO@mitienda.com")
        self.assertNotIn("SUPERSECRETO", str(cm.exception))

    def test_idna_roundtrip_enforced(self):
        # un punycode que no vuelve a la misma etiqueta, o que decodifica a otra escritura, se rechaza
        for bad in ("xn--80ak6aa92e.com", "xn--e1afmkfd.com", "xn--mi.com"):
            with self.assertRaises(StoreValidationError):
                C.normalize_domain(bad)

    def test_registered_and_metadata(self):
        self.assertIs(sc.get_class("woocommerce"), C)
        self.assertEqual((C.platform, C.label), ("woocommerce", "WooCommerce"))
        self.assertTrue(C.idempotent_publish)
        self.assertEqual([f["key"] for f in C.credential_fields], ["consumer_key", "consumer_secret"])
        self.assertTrue(all(f["secret"] and f.get("required", True) for f in C.credential_fields))

    def test_setup_help(self):
        for w in ("WooCommerce → Ajustes → Avanzado → API REST → Añadir clave", "Lectura/Escritura", "HTTPS",
                  "Authorization", "401", "EXACTO", "redirecc", "puede cambiar: verifica en la documentación de WooCommerce"):
            self.assertIn(w, C.setup_help)


class SharedClientDnsTest(unittest.TestCase):
    """Dominios que PASAN normalize_domain pero resuelven a redes privadas: los frena el cliente compartido."""

    def _validate_with_dns(self, answers):
        calls = []

        def gai(host, port, *a, **k):
            calls.append((host, port))
            return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in answers]

        def boom(*a, **k):
            raise AssertionError("no debe abrirse ninguna conexión")
        with mock.patch.object(shttp.socket, "getaddrinfo", gai), mock.patch.object(shttp.socket, "create_connection", boom):
            conn = C("tienda.localtest.me", {"consumer_key": CK, "consumer_secret": CS})
            with self.assertRaises(StoreError) as cm:
                conn.validate()
        self.assertTrue(calls)
        return cm.exception

    def test_private_resolutions_refused(self):
        for ip in ("127.0.0.1", "10.1.2.3", "192.168.0.9", "172.16.5.5", "169.254.169.254", "100.64.0.1", "0.0.0.0", "::1",
                   "fe80::1", "fc00::1", "::ffff:127.0.0.1", "224.0.0.1"):
            e = self._validate_with_dns([ip])
            self.assertNotIsInstance(e, (StoreAuthError, StoreNotFound), ip)
            self.assertFalse(e.retryable, ip)
            self.assertIn("no permitida", str(e))

    def test_mixed_public_and_private_refused(self):
        e = self._validate_with_dns(["93.184.216.34", "10.0.0.5"])
        self.assertIn("no permitida", str(e))
        e = self._validate_with_dns(["10.0.0.5", "93.184.216.34"])
        self.assertIn("no permitida", str(e))

    def test_localtest_me_style_name_passes_syntax_but_not_dns(self):
        self.assertEqual(C.normalize_domain("anything.localtest.me"), "anything.localtest.me")
        self.assertEqual(C.normalize_domain("127.0.0.1.nip.io"), "127.0.0.1.nip.io")
        e = self._validate_with_dns(["127.0.0.1"])
        self.assertIn("no permitida", str(e))

    def test_unresolvable_is_not_sent(self):
        def gai(*a, **k):
            raise socket.gaierror("x")
        with mock.patch.object(shttp.socket, "getaddrinfo", gai):
            with self.assertRaises(StoreError) as cm:
                C("tienda.com", {"consumer_key": CK, "consumer_secret": CS}).publish(
                    StoreProduct(title="x", price=1, sku="DF-1", stock=1))
        self.assertTrue(cm.exception.retryable and cm.exception.not_sent)


class CredentialsTest(unittest.TestCase):
    def test_valid(self):
        C.check_credentials({"consumer_key": CK, "consumer_secret": CS})
        C.check_credentials({"consumer_key": "ck_" + "A" * 16, "consumer_secret": "cs_" + "z9" * 40})
        C.check_credentials({"consumer_key": f" {CK} ", "consumer_secret": f"{CS}\n"})

    def test_invalid(self):
        good = {"consumer_key": CK, "consumer_secret": CS}
        bad = [{}, {"consumer_key": CK}, {"consumer_secret": CS}, {"consumer_key": "", "consumer_secret": CS},
               {**good, "consumer_key": CS}, {**good, "consumer_secret": CK}, {**good, "consumer_key": "ck_corta"},
               {**good, "consumer_key": "ck_" + "a" * 81}, {**good, "consumer_key": "CK_" + "a" * 40},
               {**good, "consumer_key": "ck_" + "a" * 39 + ":"}, {**good, "consumer_secret": "cs_" + "a b" * 20}, {**good, "consumer_key": "ck_" + "á" * 40},
               {**good, "consumer_key": 5}, {**good, "consumer_secret": None}, {**good, "consumer_key": "ck_" + "a" * 20 + "\x00x"},
               {**good, "consumer_secret": "cs_" + "a" * 20 + "%" + "a" * 20}, {**good, "consumer_key": "ck_" + "a" * 20 + "=" * 5}]
        for creds in bad:
            with self.assertRaises(StoreValidationError, msg=creds):
                C.check_credentials(creds)

    def test_error_does_not_echo_secrets(self):
        secret = "cs_" + "Q" * 30 + "$"
        with self.assertRaises(StoreValidationError) as cm:
            C.check_credentials({"consumer_key": CK, "consumer_secret": secret})
        self.assertNotIn(secret, str(cm.exception))
        self.assertNotIn("QQQQQ", str(cm.exception))


# ================================================================ validate
class ValidateTest(Base):
    def test_ok(self):
        info = self.conn().validate()
        self.assertEqual((info.domain, info.currency, info.missing_scopes), (HOST, "USD", []))
        self.assertEqual(info.name, "Tienda")
        self.assertEqual(self.fake.ops(), ["root", "list", "currency"])
        for r in self.fake.requests:
            self.assertTrue(r["has_auth"])
            self.assertNotIn("ck_", r["path"])
            self.assertNotIn("cs_", r["path"])
            self.assertEqual(r["headers"]["user-agent"], "DropFinder/1.0")
        self.assertTrue(all(r["auth_ok"] for r in self.fake.requests_for("list") + self.fake.requests_for("currency")))
        self.assertEqual(self.fake.requests_for("list")[0]["query"], {"per_page": "1"})

    def test_domain_is_normalized_before_use(self):
        self.conn(domain="HTTPS://WWW.MiTienda.com/x").validate()
        self.assertEqual(self.fake.requests[0]["host"], HOST)

    def test_hostile_domain_in_connector_never_reaches_network(self):
        with self.assertRaises(StoreValidationError):
            self.conn(domain="127.0.0.1").validate()
        self.assertEqual(self.fake.requests, [])

    def test_currency_fallback_to_system_status(self):
        self.st.update(settings_ok=False, currency="COP")
        info = self.conn().validate()
        self.assertEqual(info.currency, "COP")
        self.assertIn("system_status", self.fake.ops())

    def test_currency_unreadable(self):
        self.st.update(settings_ok=False, status_ok=False)
        with self.assertRaises(StoreValidationError) as cm:
            self.conn().validate()
        self.assertIn("moneda", str(cm.exception))

    def test_currency_garbage_rejected(self):
        for bad in ("US", "DOLAR", "", "U$D", "<s>"):
            self.st["currency"] = bad
            self.st["status_ok"] = False
            with self.assertRaises(StoreValidationError, msg=bad):
                self.conn().validate()

    def test_currency_uppercased(self):
        self.st["currency"] = "usd"
        self.assertEqual(self.conn().validate().currency, "USD")

    def test_name_is_cleaned(self):
        self.st["name"] = "<script>alert(1)</script>Mi <b>Tienda</b> " + CK
        info = self.conn().validate()
        self.assertNotIn("<", info.name)
        self.assertNotIn(CK, info.name)
        self.assertLessEqual(len(info.name), 80)
        self.st["name"] = ""
        self.assertEqual(self.conn().validate().name, HOST)

    def test_bad_key_401(self):
        with self.assertRaises(StoreAuthError) as cm:
            self.conn(key="ck_" + "0" * 40).validate()
        for w in ("Authorization", "dominio", "permisos"):
            self.assertIn(w, str(cm.exception))
        self.assertNotIn("0" * 40, str(cm.exception))

    def test_403_is_auth_error(self):
        self.fake.inject("http403", op="list")
        with self.assertRaises(StoreAuthError):
            self.conn().validate()

    def test_route_404_is_validation_not_notfound(self):
        self.fake.inject("route404", op="list")
        with self.assertRaises(StoreValidationError) as cm:
            self.conn().validate()
        self.assertNotIsInstance(cm.exception, StoreNotFound)
        self.assertIn("no parece una tienda woocommerce con la api rest activa", str(cm.exception).lower())

    def test_wc_namespace_missing(self):
        self.st["namespaces"] = ["wp/v2"]
        with self.assertRaises(StoreValidationError) as cm:
            self.conn().validate()
        self.assertIn("woocommerce", str(cm.exception).lower())

    def test_unknown_store_prefix_404(self):
        with self.assertRaises(StoreValidationError):
            self.conn(domain="otra.tienda.com").validate()

    def test_product_404_code_on_validate_is_not_notfound(self):
        self.fake.inject("product404", op="list")
        with self.assertRaises(StoreError) as cm:
            self.conn().validate()
        self.assertNotIsInstance(cm.exception, StoreNotFound)

    def test_html_login_page_200(self):
        for op in ("root", "list", "currency"):
            self.fake.injections.clear()
            self.fake.inject("html200", op=op)
            with self.assertRaises(StoreValidationError, msg=op) as cm:
                self.conn().validate()
            self.assertIn("JSON", str(cm.exception))

    def test_plain_text_200(self):
        self.fake.inject("notjson200", op="root")
        with self.assertRaises(StoreValidationError):
            self.conn().validate()

    def test_malformed_json_is_retryable_error(self):
        self.fake.inject("malformed", op="root")
        with self.assertRaises(StoreError) as cm:
            self.conn().validate()
        self.assertTrue(cm.exception.retryable)
        self.assertNotIsInstance(cm.exception, StoreValidationError)

    def test_html_error_pages(self):
        self.fake.inject("http403", op="list", code="rest_forbidden")
        for kind, cls in (("http404", StoreValidationError), ("http500", StoreError)):
            self.fake.injections.clear()
            self.fake.inject(kind, op="root", html=True)
            with self.assertRaises(cls, msg=kind) as cm:
                self.conn().validate()
            self.assertNotIn("<", str(cm.exception))
            self.assertNotIn("script", str(cm.exception).lower())

    def test_redirect_refused_with_clear_message(self):
        self.fake.inject("redirect", op="root")
        with self.assertRaises(StoreValidationError) as cm:
            self.conn().validate()
        self.assertIn("EXACTO", str(cm.exception))
        self.assertEqual(self.fake.evil_hits, 0)

    def test_redirect_on_any_call_refused(self):
        for op in ("list", "currency"):
            self.fake.injections.clear()
            self.fake.inject("redirect", op=op)
            with self.assertRaises(StoreValidationError, msg=op):
                self.conn().validate()
        self.assertEqual(self.fake.evil_hits, 0)

    def test_oversize_response(self):
        # cuerpo de exactamente MAX_BYTES + 1 (12 bytes de envoltorio + 989): http.py lo lee entero y cierra la respuesta
        # (si se lee solo una parte, http.py deja la respuesta sin cerrar: ResourceWarning ajeno a este conector)
        self.fake.inject("oversize", op="root", size=989)
        with mock.patch.object(C, "MAX_BYTES", 1000):
            with self.assertRaises(StoreError) as cm:
                self.conn().validate()
        self.assertIn("grande", str(cm.exception))

    def test_slow_response_times_out(self):
        self.fake.inject("sleep", op="root", seconds=1.0)
        with mock.patch.object(C, "TIMEOUT", 0.3):
            with self.assertRaises(StoreError) as cm:
                self.conn().validate()
        self.assertTrue(cm.exception.retryable)

    def test_unreachable(self):
        self.fake.stop()
        with self.assertRaises(StoreError) as cm:
            self.conn().validate()
        self.assertTrue(cm.exception.retryable)
        self.assertTrue(cm.exception.not_sent)

    def test_429_rate_limited(self):
        self.fake.inject("http429", op="root", retry_after=9)
        with self.assertRaises(StoreRateLimited) as cm:
            self.conn().validate()
        self.assertEqual(cm.exception.retry_after, 9.0)

    def test_root_must_be_object_and_products_list(self):
        self.fake.inject("empty200", op="root")
        with self.assertRaises(StoreValidationError):
            self.conn().validate()


# ================================================================ publish
class PublishTest(Base):
    def test_exact_request_bodies(self):
        ref = self.conn().publish(self.product(active=False))
        self.assertEqual(self.fake.ops(), ["find_sku", "create"])
        find = self.fake.requests_for("find_sku")[0]
        self.assertEqual(find["query"], {"sku": "DF-1", "status": "any"})
        body = self.fake.requests_for("create")[0]["body"]
        self.assertEqual(body, {
            "name": "Lámpara <LED> & más", "type": "simple", "status": "draft", "sku": "DF-1", "regular_price": "19.90",
            "manage_stock": True, "stock_quantity": 7,
            "description": "<p>Línea 1<br>Línea &lt;b&gt;2&lt;/b&gt;</p><p>Párrafo 2</p>",
            "images": [{"src": "https://cdn.x.com/a.jpg"}],
            "tags": [{"name": "a"}, {"name": "b c"}, {"name": "dropfinder"}],
            "sale_price": "9.90"})
        self.assertEqual(ref.external_id, str(self.fake.products(HOST)[0]["id"]))
        self.assertEqual(ref.url, f"https://{HOST}/wp-admin/post.php?post={ref.external_id}&action=edit")
        self.assertEqual((ref.variant_id, ref.inventory_item_id), ("", ""))
        rec = self.fake.requests_for("create")[0]
        self.assertTrue(rec["auth_ok"])
        self.assertTrue(rec["content_type"].startswith("application/json"))
        self.assertEqual(self.fake.products(HOST)[0]["status"], "draft")

    def test_created_as_draft_even_when_active_then_shown_last(self):
        ref = self.conn().publish(self.product(active=True))
        self.assertEqual(self.fake.ops(), ["find_sku", "create", "update"])
        self.assertEqual(self.fake.requests_for("create")[0]["body"]["status"], "draft")
        self.assertEqual(self.fake.requests_for("update")[0]["body"], {"status": "publish"})
        self.assertEqual(self.fake.products(HOST)[0]["status"], "publish")
        self.assertTrue(ref.external_id)

    def test_optional_fields_omitted(self):
        self.conn().publish(self.product(active=False, compare_at_price=None, image_urls=[], tags=[], description=""))
        body = self.fake.requests_for("create")[0]["body"]
        for k in ("sale_price", "images", "tags"):
            self.assertNotIn(k, body)
        self.assertEqual(body["description"], "")

    def test_sale_price_only_if_lower(self):
        for cmp_, expect in ((9.9, "9.90"), (19.9, None), (25, None), (19.89, "19.89"), (0.004, None)):
            self.fake.stores[HOST]["products"].clear()
            self.fake.requests.clear()
            if cmp_ == 0.004:
                with self.assertRaises(StoreValidationError):          # redondea a 0.00: inválido, no se envía nada
                    self.conn().publish(self.product(active=False, compare_at_price=cmp_))
                self.assertEqual(self.fake.requests, [])
                continue
            self.conn().publish(self.product(active=False, compare_at_price=cmp_))
            body = self.fake.requests_for("create")[0]["body"]
            self.assertEqual(body.get("sale_price"), expect, cmp_)

    def test_validation_before_any_call(self):
        bad = [dict(title=""), dict(title="   "), dict(title=None), dict(price=0), dict(price=-1), dict(price=0.004),
               dict(price="abc"), dict(price=float("nan")), dict(price=float("inf")), dict(price=True), dict(price=None),
               dict(price=10 ** 10), dict(stock=-1), dict(stock=True), dict(stock="5"), dict(stock=None),
               dict(stock=float("nan")), dict(stock=float("inf")), dict(stock=10 ** 10), dict(sku=""), dict(sku="  "),
               dict(sku=" DF-1"), dict(sku="DF\n1"), dict(sku="x" * 256), dict(sku=None), dict(sku=5),
               dict(compare_at_price="abc"), dict(compare_at_price=-3), dict(compare_at_price=float("nan"))]
        for kw in bad:
            with self.assertRaises(StoreValidationError, msg=kw):
                self.conn().publish(self.product(**kw))
        self.assertEqual(self.fake.requests, [])

    def test_price_rounding_half_up(self):
        self.conn().publish(self.product(price=10.005, compare_at_price=None, active=False))
        self.assertEqual(self.fake.requests_for("create")[0]["body"]["regular_price"], "10.01")
        self.assertIsInstance(self.fake.requests_for("create")[0]["body"]["regular_price"], str)

    def test_images_https_only_max10_and_no_ip_or_userinfo(self):
        urls = [f"https://x.com/{i}.jpg" for i in range(15)] + ["http://a.com/x.jpg"]
        self.conn().publish(self.product(image_urls=urls, active=False))
        imgs = self.fake.requests_for("create")[0]["body"]["images"]
        self.assertEqual(len(imgs), 10)
        self.assertTrue(all(i["src"].startswith("https://") for i in imgs))
        self.fake.stores[HOST]["products"].clear()
        bad = ["http://x.com/a.jpg", "ftp://x.com/a.jpg", "https://127.0.0.1/a.jpg", "https://[::1]/a.jpg", "https://u:p@x.com/a",
               "https://x.com/ñ.jpg", "https://x.com/a b.jpg", "//x.com/a.jpg", "https://localhost/a.jpg", "https://x/a.jpg",
               "https://x.com:99999/a", "javascript:alert(1)", "data:image/png;base64,AAAA", "https://x.com/" + "a" * 2100, 5, None]
        self.fake.requests.clear()
        self.conn().publish(self.product(image_urls=bad, active=False))
        self.assertNotIn("images", self.fake.requests_for("create")[0]["body"])

    def test_description_escaped_as_paragraphs(self):
        self.conn().publish(self.product(description="<script>alert(1)</script>\r\nl2\r\n\r\n\r\n  <img src=x onerror=1>  ", active=False))
        d = self.fake.requests_for("create")[0]["body"]["description"]
        self.assertEqual(d, "<p>&lt;script&gt;alert(1)&lt;/script&gt;<br>l2</p><p>&lt;img src=x onerror=1&gt;</p>")
        self.assertNotIn("<script", d)

    def test_title_truncated_to_255(self):
        self.conn().publish(self.product(title="T" * 400, active=False))
        self.assertEqual(len(self.fake.requests_for("create")[0]["body"]["name"]), 255)

    def test_tags_cleaned(self):
        self.conn().publish(self.product(tags=["  a  ", "A", "b,c", "", "  ", 5, None, "x" * 200], active=False))
        names = [t["name"] for t in self.fake.requests_for("create")[0]["body"]["tags"]]
        self.assertEqual(names[:2], ["a", "b c"])
        self.assertEqual(len(names), 3)
        self.assertEqual(len(names[2]), 100)
        self.conn().publish(self.product(sku="DF-2", tags="solo-una", active=False))
        self.assertEqual(self.fake.requests_for("create")[1]["body"]["tags"], [{"name": "solo-una"}])

    # ---------------------------------------------------------------- idempotencia
    def test_adopts_existing_sku_without_duplicating(self):
        seed = self.fake.put_product(HOST, sku="DF-1", status="draft", regular_price="5.00", stock_quantity=1)
        ref = self.conn().publish(self.product(active=False))
        self.assertEqual(ref.external_id, str(seed["id"]))
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.fake.ops(), ["find_sku", "update"])
        self.assertEqual(self.fake.requests_for("update")[0]["body"],
                         {"regular_price": "19.90", "manage_stock": True, "stock_quantity": 7, "sale_price": "9.90"})
        p = self.fake.products(HOST)[0]
        self.assertEqual((p["regular_price"], p["stock_quantity"], p["status"]), ("19.90", 7, "draft"))

    def test_adopted_published_product_not_downgraded_and_active_shown(self):
        seed = self.fake.put_product(HOST, sku="DF-1", status="publish")
        self.conn().publish(self.product(active=False))
        self.assertEqual(self.fake.products(HOST)[0]["status"], "publish")           # no se oculta lo que ya estaba visible
        self.fake.requests.clear()
        seed2 = self.fake.put_product(HOST, sku="DF-2", status="draft")
        self.conn().publish(self.product(sku="DF-2", active=True))
        self.assertEqual(self.fake.requests_for("update")[-1]["body"], {"status": "publish"})
        self.assertEqual(seed["id"] + 1, seed2["id"])

    def test_sku_filter_result_checked_exactly(self):
        self.fake.put_product(HOST, sku="df-1", status="draft")                      # el servidor compara sin mayúsculas
        with self.assertRaises(StoreValidationError) as cm:                          # el SKU existe (sin mayúsculas) pero no es el nuestro
            self.conn().publish(self.product(active=False))
        self.assertIn("SKU", str(cm.exception))
        self.assertEqual(self.count(), 1)

    def test_sku_exists_error_on_create_adopts(self):
        for code in ("woocommerce_rest_product_sku_already_exists", "product_invalid_sku"):
            self.fake.stores[HOST]["products"].clear()
            self.fake.requests.clear()
            self.fake.sku_error_code = code
            seed = self.fake.put_product(HOST, sku="DF-1", status="draft")
            self.fake.inject("empty200", op="find_sku")                              # la búsqueda previa no lo vio (carrera)
            ref = self.conn().publish(self.product(active=False))
            self.assertEqual(ref.external_id, str(seed["id"]), code)
            self.assertEqual(self.count(), 1)
            self.assertEqual(self.fake.ops(), ["find_sku", "create", "find_sku", "update"])
            self.assertEqual(self.fake.requests_for("create")[0]["body"]["status"], "draft")

    def test_sku_exists_but_cannot_find_it(self):
        self.fake.put_product(HOST, sku="DF-1", status="draft")
        self.fake.inject("empty200", op="find_sku", count=2)
        with self.assertRaises(StoreValidationError) as cm:
            self.conn().publish(self.product(active=False))
        self.assertIn("SKU", str(cm.exception))
        self.assertEqual(self.count(), 1)

    def test_other_validation_error_on_create_is_not_adopted(self):
        self.fake.inject("http400", op="create", code="rest_invalid_param", message="nombre inválido")
        with self.assertRaises(StoreValidationError) as cm:
            self.conn().publish(self.product(active=False))
        self.assertIn("nombre inválido", str(cm.exception))
        self.assertEqual(self.fake.ops(), ["find_sku", "create"])

    def test_lost_response_after_create_then_retry_one_product(self):
        self.fake.inject("execute_then_drop", op="create")
        with self.assertRaises(StoreError) as cm:
            self.conn().publish(self.product(active=False))
        self.assertTrue(cm.exception.retryable)
        self.assertEqual(self.count(), 1)                                            # se aplicó y se perdió la respuesta
        ref = self.conn().publish(self.product(active=False))
        self.assertEqual(self.count(), 1)
        self.assertEqual(ref.external_id, str(self.fake.products(HOST)[0]["id"]))

    def test_request_dropped_before_apply_then_retry_creates_one(self):
        self.fake.inject("drop", op="create")
        with self.assertRaises(StoreError):
            self.conn().publish(self.product(active=False))
        self.assertEqual(self.count(), 0)
        self.conn().publish(self.product(active=False))
        self.assertEqual(self.count(), 1)

    def test_429_and_5xx_mid_publish_then_retry_one_product(self):
        for kind, cls in (("http429", StoreRateLimited), ("http500", StoreError), ("http503", StoreError), ("http502", StoreError)):
            for op in ("find_sku", "create"):
                self.fake.stores[HOST]["products"].clear()
                self.fake.inject(kind, op=op)
                with self.assertRaises(cls, msg=(kind, op)) as cm:
                    self.conn().publish(self.product(active=False))
                self.assertTrue(cm.exception.retryable)
                self.conn().publish(self.product(active=False))
                self.assertEqual(self.count(), 1, (kind, op))

    def test_5xx_on_create_after_apply_then_retry_adopts(self):
        self.fake.inject("execute_then_drop", op="create")
        with self.assertRaises(StoreError):
            self.conn().publish(self.product(active=True))
        self.fake.inject("http500", op="update")
        with self.assertRaises(StoreError):
            self.conn().publish(self.product(active=True))
        self.assertEqual(self.count(), 1)                                            # lo adoptado NO se borra
        self.conn().publish(self.product(active=True))
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.fake.products(HOST)[0]["status"], "publish")

    def test_create_response_without_valid_id(self):
        for bad in (None, "x", 0, -1, True, 1.5):
            with mock.patch.object(C, "_call", side_effect=[[], {"id": bad, "status": "draft"}]):
                with self.assertRaises(StoreError) as cm:
                    self.conn().publish(self.product(active=False))
            self.assertTrue(cm.exception.retryable, bad)

    # ---------------------------------------------------------------- limpieza de huérfanos
    def test_partial_failure_cleans_orphan(self):
        self.fake.inject("http500", op="update")
        with self.assertRaises(StoreError) as cm:
            self.conn().publish(self.product(active=True))
        self.assertTrue(cm.exception.retryable)
        self.assertFalse(hasattr(cm.exception, "orphan_external_id"))
        self.assertEqual(self.count(), 0)
        dl = self.fake.requests_for("delete")
        self.assertEqual(len(dl), 1)
        self.assertEqual(dl[0]["query"], {"force": "true"})

    def test_cleanup_retries_bounded_then_reports_orphan(self):
        self.fake.inject("http500", op="update")
        self.fake.inject("http429", op="delete", count=10, retry_after=3)
        with self.assertRaises(StoreError) as cm:
            self.conn().publish(self.product(active=True))
        e = cm.exception
        pid = str(self.fake.products(HOST)[0]["id"])
        self.assertEqual(e.orphan_external_id, pid)
        self.assertIn(pid, str(e))
        self.assertEqual(len(self.fake.requests_for("delete")), woocommerce.CLEANUP_TRIES)
        self.assertEqual(self.sleeps, [3.0] * (woocommerce.CLEANUP_TRIES - 1))
        self.assertTrue(e.retryable)
        # el siguiente intento reutiliza el borrador huérfano
        self.fake.injections.clear()
        ref = self.conn().publish(self.product(active=True))
        self.assertEqual(ref.external_id, pid)
        self.assertEqual(self.count(), 1)

    def test_cleanup_recovers_after_transient_failure(self):
        self.fake.inject("http500", op="update")
        self.fake.inject("http503", op="delete", count=2)
        with self.assertRaises(StoreError) as cm:
            self.conn().publish(self.product(active=True))
        self.assertFalse(hasattr(cm.exception, "orphan_external_id"))
        self.assertEqual(self.count(), 0)
        self.assertEqual(len(self.fake.requests_for("delete")), 3)

    def test_cleanup_nonretryable_stops_immediately(self):
        self.fake.inject("http500", op="update")
        self.fake.inject("http401", op="delete", count=5)
        with self.assertRaises(StoreError) as cm:
            self.conn().publish(self.product(active=True))
        self.assertEqual(len(self.fake.requests_for("delete")), 1)
        self.assertTrue(hasattr(cm.exception, "orphan_external_id"))

    def test_cleanup_product_already_gone_is_fine(self):
        self.fake.inject("http500", op="update")
        self.fake.inject("product404", op="delete")
        with self.assertRaises(StoreError) as cm:
            self.conn().publish(self.product(active=True))
        self.assertFalse(hasattr(cm.exception, "orphan_external_id"))

    def test_product_vanishes_during_publish_is_retryable_error(self):
        self.fake.inject("product404", op="update")
        with self.assertRaises(StoreError) as cm:
            self.conn().publish(self.product(active=True))
        self.assertNotIsInstance(cm.exception, StoreNotFound)
        self.assertTrue(cm.exception.retryable)

    def test_cleanup_route_404_is_not_success(self):
        self.fake.inject("http500", op="update")
        self.fake.inject("route404", op="delete", count=5)
        with self.assertRaises(StoreError) as cm:
            self.conn().publish(self.product(active=True))
        self.assertTrue(hasattr(cm.exception, "orphan_external_id"))

    # ---------------------------------------------------------------- concurrencia
    def test_concurrent_publishes_of_same_sku_yield_one_product(self):
        errors, refs = [], []

        def run():
            try:
                refs.append(self.conn().publish(self.product(active=False)).external_id)
            except Exception as e:  # noqa: BLE001
                errors.append(e)
        ts = [threading.Thread(target=run) for _ in range(8)]
        [t.start() for t in ts]
        [t.join(30) for t in ts]
        self.assertEqual(errors, [])
        self.assertEqual(self.count(), 1)
        self.assertEqual(len(set(refs)), 1)

    def test_concurrent_distinct_skus(self):
        with concurrent.futures.ThreadPoolExecutor(6) as ex:
            list(ex.map(lambda i: self.conn().publish(self.product(sku=f"DF-{i}", active=False)), range(12)))
        self.assertEqual(self.count(), 12)
        self.assertEqual(len({p["sku"] for p in self.fake.products(HOST)}), 12)


# ================================================================ actualizar / mostrar / borrar
class UpdateTest(Base):
    def test_update_body(self):
        p = self.fake.put_product(HOST, sku="DF-1")
        self.conn().update_price_stock(self.ref(p), 19.9, 7)
        r = self.fake.requests_for("update")[0]
        self.assertEqual((r["method"], r["path"].split("?")[0]), ("PUT", f"/{HOST}/wp-json/wc/v3/products/{p['id']}"))
        self.assertEqual(r["body"], {"regular_price": "19.90", "manage_stock": True, "stock_quantity": 7})
        got = self.fake.products(HOST)[0]
        self.assertEqual((got["regular_price"], got["stock_quantity"], got["manage_stock"]), ("19.90", 7, True))

    def test_sale_price_rules(self):
        p = self.fake.put_product(HOST, sku="DF-1", sale_price="3.00", regular_price="10.00")
        ref = self.ref(p)
        self.conn().update_price_stock(ref, 20, 1)                                   # sin compare: no se toca la oferta manual
        self.assertNotIn("sale_price", self.fake.requests_for("update")[0]["body"])
        self.assertEqual(self.fake.products(HOST)[0]["sale_price"], "3.00")
        self.conn().update_price_stock(ref, 20, 1, compare_at_price=15)
        self.assertEqual(self.fake.requests_for("update")[1]["body"]["sale_price"], "15.00")
        self.conn().update_price_stock(ref, 20, 1, compare_at_price=20)              # no menor: no se envía
        self.assertNotIn("sale_price", self.fake.requests_for("update")[2]["body"])
        self.conn().update_price_stock(ref, 20, 1, compare_at_price=25)
        self.assertNotIn("sale_price", self.fake.requests_for("update")[3]["body"])
        for r in self.fake.requests_for("update"):
            self.assertNotEqual(r["body"].get("sale_price"), "")

    def test_draft_and_trashed_products_still_update(self):
        for st in ("draft", "trash", "private", "pending"):
            p = self.fake.put_product(HOST, sku=f"DF-{st}", status=st)
            self.conn().update_price_stock(self.ref(p), 5, 3)
        self.assertEqual({q["regular_price"] for q in self.fake.products(HOST)}, {"5.00"})

    def test_invalid_values_no_calls(self):
        ref = ListingRef(external_id="12")
        for kw in (dict(price=0, stock=1), dict(price=-5, stock=1), dict(price="x", stock=1), dict(price=1, stock=-1),
                   dict(price=1, stock=True), dict(price=float("nan"), stock=1), dict(price=1, stock=2.5e9),
                   dict(price=1, stock=1, compare_at_price="x"), dict(price=1e10, stock=1)):
            with self.assertRaises(StoreValidationError, msg=kw):
                self.conn().update_price_stock(ref, **kw)
        self.assertEqual(self.fake.requests, [])

    def test_bad_refs_no_calls(self):
        for bad in ("", "abc", "0", "01", "1/../x", "12;", "1 2", "12\n", "-1", "1.5", "1?force=true", "%31", "٣", None, 5,
                    "9" * 19, "../wp/v2/users"):
            for fn in (lambda r: self.conn().update_price_stock(r, 1, 1), lambda r: self.conn().set_active(r, True),
                       lambda r: self.conn().delete(r)):
                with self.assertRaises(StoreValidationError, msg=bad):
                    fn(ListingRef(external_id=bad))
        self.assertEqual(self.fake.requests, [])

    def test_missing_product_is_notfound_on_update(self):
        with self.assertRaises(StoreNotFound):
            self.conn().update_price_stock(ListingRef(external_id="99999"), 1, 1)
        with self.assertRaises(StoreNotFound):
            self.conn().set_active(ListingRef(external_id="99999"), True)

    def test_route_404_on_update_is_not_notfound(self):
        self.fake.inject("route404", op="update")
        with self.assertRaises(StoreValidationError) as cm:
            self.conn().update_price_stock(ListingRef(external_id="5"), 1, 1)
        self.assertNotIsInstance(cm.exception, StoreNotFound)

    def test_sale_price_conflict_is_validation(self):
        p = self.fake.put_product(HOST, sku="DF-1", regular_price="10.00")
        with self.assertRaises(StoreValidationError):                                # el fake rechaza oferta >= precio
            self.fake.inject("http400", op="update", code="woocommerce_rest_invalid_product_sale_price")
            self.conn().update_price_stock(self.ref(p), 5, 1)

    def test_set_active(self):
        p = self.fake.put_product(HOST, sku="DF-1", status="draft")
        self.conn().set_active(self.ref(p), True)
        self.assertEqual(self.fake.requests_for("update")[0]["body"], {"status": "publish"})
        self.assertEqual(self.fake.products(HOST)[0]["status"], "publish")
        self.conn().set_active(self.ref(p), False)
        self.assertEqual(self.fake.requests_for("update")[1]["body"], {"status": "draft"})
        self.assertEqual(self.fake.products(HOST)[0]["status"], "draft")


class DeleteTest(Base):
    def test_delete_force_true(self):
        p = self.fake.put_product(HOST, sku="DF-1")
        self.conn().delete(self.ref(p))
        r = self.fake.requests_for("delete")[0]
        self.assertEqual((r["method"], r["query"]), ("DELETE", {"force": "true"}))
        self.assertTrue(r["path"].endswith(f"/products/{p['id']}?force=true"))
        self.assertEqual(self.count(), 0)                                            # sin force solo iría a la papelera

    def test_delete_idempotent_product_gone(self):
        p = self.fake.put_product(HOST, sku="DF-1")
        ref = self.ref(p)
        self.conn().delete(ref)
        self.conn().delete(ref)                                                      # 404 de producto = éxito
        self.conn().delete(ListingRef(external_id="424242"))
        self.assertEqual(len(self.fake.requests_for("delete")), 3)

    def test_delete_route_404_is_not_success(self):
        self.fake.inject("route404", op="delete")
        with self.assertRaises(StoreValidationError) as cm:
            self.conn().delete(ListingRef(external_id="5"))
        self.assertNotIsInstance(cm.exception, StoreNotFound)

    def test_delete_html_404_is_not_success(self):
        self.fake.inject("http404", op="delete", html=True, code="")
        with self.assertRaises(StoreError) as cm:
            self.conn().delete(ListingRef(external_id="5"))
        self.assertNotIsInstance(cm.exception, StoreNotFound)

    def test_delete_unknown_404_code_is_not_success(self):
        self.fake.inject("http404", op="delete", code="algo_raro")
        with self.assertRaises(StoreValidationError):
            self.conn().delete(ListingRef(external_id="5"))

    def test_delete_errors(self):
        for kind, cls in (("http401", StoreAuthError), ("http403", StoreAuthError), ("http429", StoreRateLimited),
                          ("http500", StoreError)):
            self.fake.injections.clear()
            self.fake.inject(kind, op="delete")
            with self.assertRaises(cls, msg=kind):
                self.conn().delete(ListingRef(external_id="5"))


# ================================================================ tabla de errores
class ErrorMappingTest(Base):
    def run_op(self):
        p = self.fake.put_product(HOST, sku="DF-1")
        return self.conn().update_price_stock(self.ref(p), 5, 1)

    def check(self, kind, cls, retryable=None, **kw):
        self.fake.injections.clear()
        self.fake.inject(kind, op="update", **kw)
        with self.assertRaises(cls, msg=kind) as cm:
            self.run_op()
        if retryable is not None:
            self.assertEqual(cm.exception.retryable, retryable, kind)
        return cm.exception

    def test_table(self):
        self.check("http401", StoreAuthError, False)
        self.check("http403", StoreAuthError, False)
        self.check("route404", StoreValidationError, False)
        self.check("http404", StoreValidationError, False)                           # code rest_no_route
        self.check("http404", StoreValidationError, False, html=True)
        self.check("http400", StoreValidationError, False, code="rest_invalid_param")
        self.check("http422", StoreValidationError, False)
        self.check("http409", StoreValidationError, False)
        self.check("http500", StoreError, True)
        self.check("http502", StoreError, True)
        self.check("http503", StoreError, True)
        self.check("http504", StoreError, True)
        self.check("http408", StoreError, True)
        self.check("http418", StoreError, False)
        self.check("http402", StoreError, False)
        self.check("http410", StoreError, False)
        self.check("http301", StoreValidationError, False)
        self.check("http302", StoreValidationError, False)
        self.check("html200", StoreValidationError, False)
        self.check("notjson200", StoreValidationError, False)
        self.check("malformed", StoreError, True)
        e = self.check("product404", StoreNotFound)
        self.assertFalse(isinstance(e, StoreValidationError))

    def test_non_notfound_for_everything_but_product_code(self):
        for kind in ("http401", "http403", "route404", "http404", "http500", "http429", "html200", "malformed", "http400"):
            e = self.check(kind, StoreError)
            self.assertNotIsInstance(e, StoreNotFound, kind)

    def test_auth_hint_in_401(self):
        e = self.check("http401", StoreAuthError)
        for w in ("Authorization", "exactamente", "permisos"):
            self.assertIn(w, str(e))

    def test_429_retry_after_variants(self):
        self.assertEqual(self.check("http429", StoreRateLimited, retry_after=7).retry_after, 7.0)
        self.assertEqual(self.check("http429", StoreRateLimited).retry_after, woocommerce.DEFAULT_RETRY_AFTER)
        self.assertEqual(self.check("http429", StoreRateLimited, retry_after=99999).retry_after, woocommerce.MAX_RETRY_AFTER)
        self.assertEqual(self.check("http429", StoreRateLimited, retry_after=0).retry_after, 1.0)
        self.assertEqual(self.check("http429", StoreRateLimited, retry_after="abc").retry_after, woocommerce.DEFAULT_RETRY_AFTER)
        self.assertEqual(self.check("http429", StoreRateLimited, retry_after="nan").retry_after, woocommerce.DEFAULT_RETRY_AFTER)
        self.assertEqual(self.check("http429", StoreRateLimited, retry_after="-5").retry_after, 1.0)
        self.assertTrue(self.check("http429", StoreRateLimited).retryable)

    def test_not_sent_only_from_shared_client(self):
        e = self.check("http500", StoreError)
        self.assertFalse(e.not_sent)                                                 # la tienda respondió: no es "no enviado"
        self.fake.injections.clear()
        self.fake.inject("execute_then_drop", op="update")
        p = self.fake.put_product(HOST, sku="DF-9")
        with self.assertRaises(StoreError) as cm:
            self.conn().update_price_stock(self.ref(p), 5, 1)
        self.assertTrue(cm.exception.retryable)
        self.assertFalse(cm.exception.not_sent)                                      # cortada tras enviar: ambiguo
        self.assertEqual(self.fake.products(HOST)[-1]["regular_price"], "5.00")

    def test_unreachable_is_not_sent_retryable(self):
        p = self.fake.put_product(HOST, sku="DF-1")
        self.fake.stop()
        with self.assertRaises(StoreError) as cm:
            self.conn().update_price_stock(self.ref(p), 5, 1)
        self.assertTrue(cm.exception.retryable and cm.exception.not_sent)

    def test_only_store_errors_escape(self):
        weird = StoreProduct(title="t", price=object(), sku="DF-1", stock=1)
        with self.assertRaises(StoreError):
            self.conn().publish(weird)
        for bad in (StoreProduct(title="t", price=5, sku="DF-1", stock=1, tags=object(), image_urls=object(),
                                 description=object(), compare_at_price=object()),):
            with self.assertRaises(StoreError):
                self.conn().publish(bad)
        with self.assertRaises(StoreError):
            self.conn().delete(object())

    def test_oversized_error_body_capped(self):
        self.fake.inject("http400", op="update", message="x" * 5000, code="rest_invalid_param")
        with self.assertRaises(StoreValidationError) as cm:
            self.run_op()
        self.assertLess(len(str(cm.exception)), 600)

    def test_error_code_with_garbage_does_not_crash(self):
        for code in ("", "a" * 500, "<b>x</b>", None, 5):
            self.fake.injections.clear()
            self.fake.inject("http400", op="update", code=code)
            with self.assertRaises(StoreValidationError):
                self.run_op()


# ================================================================ secretos
class SecretsTest(Base):
    def leak_check(self, text):
        for s in (CK, CS, BASIC, f"Basic {BASIC}", CK[3:], CS[3:]):
            self.assertNotIn(s, text)

    def test_secrets_scrubbed_from_store_messages(self):
        evil = f"fallo {CK} y {CS} y Authorization: Basic {BASIC} y {BASIC}"
        for kind in ("http400", "http401", "http403", "http500"):
            self.fake.injections.clear()
            self.fake.inject(kind, op="list", message=evil)
            with self.assertRaises(StoreError) as cm:
                self.conn().validate()
            self.leak_check(str(cm.exception))
            self.leak_check(repr(cm.exception))
        self.fake.injections.clear()
        self.fake.inject("http500", op="root", html=True)
        self.fake.inject("http400", op="root", message=f"<p>{CK}</p><script>{CS}</script> {BASIC}")
        with self.assertRaises(StoreError) as cm:
            self.conn().validate()
        self.leak_check(str(cm.exception))

    def test_own_scrub_layer_without_generic_patterns(self):
        """Defensa en profundidad: aunque el scrub genérico no reconociera la forma, el conector quita sus secretos."""
        from store_connectors import base
        with mock.patch.object(base, "_TOKEN_RE", re.compile(r"(?!x)x")):
            out = self.conn()._text(f"a {CK} b {CS} c Basic {BASIC} d {BASIC} e")
            self.leak_check(out)
            self.assertIn("a", out)
            self.fake.inject("http400", op="list", message=f"x {CK} {CS} {BASIC} Basic {BASIC}")
            with self.assertRaises(StoreError) as cm:
                self.conn().validate()
            self.leak_check(str(cm.exception))

    def test_secrets_scrubbed_from_html_body_errors(self):
        with mock.patch.object(woocommerce.shttp, "request_json") as rj:
            rj.return_value = shttp.Response(502, {}, f"<html><body>Bad gateway {CK} {CS} {BASIC}<script>x</script></body></html>".encode())
            with self.assertRaises(StoreError) as cm:
                self.conn().validate()
        self.leak_check(str(cm.exception))
        self.assertNotIn("<", str(cm.exception))
        self.assertLessEqual(len(str(cm.exception)), 500)

    def test_secrets_only_in_authorization_header(self):
        c = self.conn()
        c.validate()
        c.publish(self.product(active=True, compare_at_price=None))
        ref = ListingRef(external_id=str(self.fake.products(HOST)[0]["id"]))
        c.update_price_stock(ref, 5, 2)
        c.set_active(ref, False)
        c.delete(ref)
        self.assertGreaterEqual(len(self.fake.requests), 8)
        for r in self.fake.requests:
            self.leak_check(r["path"])
            self.leak_check(repr(r["body"]))
            self.leak_check(repr(r["headers"]))                                      # sin Authorization registrado
            self.assertTrue(r["has_auth"])
        self.assertTrue(all(r["auth_ok"] for r in self.fake.requests if r["op"] not in ("root",)))

    def test_header_is_basic_with_exact_credentials(self):
        seen = {}

        class Cap(shttp.Response):
            pass
        real = shttp.request_json

        def spy(method, url, payload=None, **kw):
            seen["h"] = dict(kw.get("headers") or {})
            seen["url"] = url
            return real(method, url, payload, **kw)
        with mock.patch.object(woocommerce.shttp, "request_json", spy):
            self.conn().validate()
        self.assertEqual(seen["h"], {"Authorization": f"Basic {BASIC}"})
        self.assertNotIn("@", seen["url"].split("//", 1)[1].split("/", 1)[0])
        self.assertNotIn("ck_", seen["url"])

    def test_no_secrets_in_repr_dict_logs_or_exceptions(self):
        c = self.conn()
        for text in (repr(c), str(c), repr(c.__dict__), str(vars(c)), repr(c.credentials), str(c.credentials), f"{c.credentials}",
                     repr([c]), repr({"c": c})):
            self.leak_check(text)
        self.assertEqual(c.credentials["consumer_key"], CK)                          # el motor sí puede leerlas
        self.assertEqual(sorted(c.credentials.values()), sorted([CK, CS]))
        with self.assertNoLogs(level=logging.DEBUG):
            c.validate()
        with self.assertLogs("dropfinder.woocommerce", level="WARNING") as lg:       # huérfano: aviso sin secretos
            self.fake.inject("http500", op="update")
            self.fake.inject("http500", op="delete", count=5)
            with self.assertRaises(StoreError) as cm:
                c.publish(self.product(active=True))
        self.leak_check("\n".join(lg.output))
        self.leak_check(str(cm.exception))

    def test_secrets_not_in_unreachable_or_validation_errors(self):
        self.fake.stop()
        for fn in (lambda c: c.validate(), lambda c: c.publish(self.product()), lambda c: c.delete(ListingRef(external_id="4"))):
            with self.assertRaises(StoreError) as cm:
                fn(self.conn())
            self.leak_check(str(cm.exception))

    def test_secret_in_title_does_not_matter_but_bad_sku_error_has_no_secret(self):
        with self.assertRaises(StoreValidationError) as cm:
            self.conn().publish(self.product(sku=CS + "\n"))
        self.leak_check(str(cm.exception))


# ================================================================ cliente compartido
class SharedClientTest(Base):
    def test_plain_http_refused_outside_allow_loopback(self):
        shttp.ALLOW_LOOPBACK = False
        with self.assertRaises(StoreError) as cm:
            self.conn().validate()
        self.assertIn("HTTPS", str(cm.exception))
        self.assertEqual(self.fake.requests, [])

    def test_non_443_port_refused(self):
        shttp.ALLOW_LOOPBACK = False
        with mock.patch.object(C, "BASE_URL_TEMPLATE", "https://{host}:8443"):
            with self.assertRaises(StoreError) as cm:
                self.conn().validate()
        self.assertIn("Puerto", str(cm.exception))

    def test_redirect_refused_and_never_followed(self):
        self.fake.inject("redirect", count=10)
        for fn in (lambda c: c.validate(), lambda c: c.publish(self.product()), lambda c: c.delete(ListingRef(external_id="4")),
                   lambda c: c.set_active(ListingRef(external_id="4"), True),
                   lambda c: c.update_price_stock(ListingRef(external_id="4"), 1, 1)):
            with self.assertRaises(StoreValidationError):
                fn(self.conn())
        self.assertEqual(self.fake.evil_hits, 0)

    def test_connector_uses_only_shared_client(self):
        with open(woocommerce.__file__, encoding="utf-8") as f:
            src = f.read()
        for forbidden in ("urllib.request", "import http.client", "import socket", "from socket", "import requests", "urlopen",
                          "HTTPConnection", "HTTPSConnection", "verify=False", "CERT_NONE", "_create_unverified"):
            self.assertNotIn(forbidden, src)
        self.assertIn("shttp.request_json", src)

    def test_url_template_not_overridable_from_settings_or_credentials(self):
        c = C(HOST, {"consumer_key": CK, "consumer_secret": CS, "BASE_URL_TEMPLATE": "http://evil.example"},
              {"BASE_URL_TEMPLATE": "http://evil.example", "base_url": "http://evil.example"})
        c.validate()
        self.assertEqual({r["host"] for r in self.fake.requests}, {HOST})

    def test_uses_pinned_public_ip_for_real_hosts(self):
        shttp.ALLOW_LOOPBACK = False
        seen = []

        def gai(host, port, *a, **k):
            seen.append((host, port))
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

        def cc(addr, timeout=None, *a, **k):
            seen.append(addr)
            raise OSError("sin red en pruebas")
        with mock.patch.object(C, "BASE_URL_TEMPLATE", "https://{host}"), \
                mock.patch.object(shttp.socket, "getaddrinfo", gai), mock.patch.object(shttp.socket, "create_connection", cc):
            with self.assertRaises(StoreError) as cm:
                self.conn().validate()
        self.assertEqual(seen, [(HOST, 443), ("93.184.216.34", 443)])
        self.assertTrue(cm.exception.not_sent)


# ================================================================ mutaciones
# (archivo, texto original, reemplazo): cada mutante debe hacer fallar alguna prueba de este archivo.
CONNECTOR = "backend/store_connectors/woocommerce.py"
MUTANTS = [
    ("draft_always", '"type": "simple", "status": "draft"', '"type": "simple", "status": "publish"'),
    ("draft_removed", '"type": "simple", "status": "draft", "sku": sku', '"type": "simple", "sku": sku'),
    ("no_sku_lookup", "existing = self._find_by_sku(sku)\n        adopted = existing is not None",
     "existing = None\n        adopted = False"),
    ("sku_exists_not_adopted", 'if getattr(e, "wc_code", "") not in SKU_EXISTS_CODES:', "if True:"),
    ("sku_exists_other_code", 'SKU_EXISTS_CODES = ("woocommerce_rest_product_sku_already_exists", "product_invalid_sku")',
     'SKU_EXISTS_CODES = ("product_invalid_sku",)'),
    ("price_zero_allowed", "if d < 0 or (d == 0 and not allow_zero):", "if d < 0:"),
    ("price_checked_before_rounding", "    d = d.quantize(Decimal(\"0.01\"), rounding=ROUND_HALF_UP)\n    if d < 0 or (d == 0 and not allow_zero):\n        raise StoreValidationError(f\"El {what} debe ser mayor que cero\")",
     "    if d < 0 or (d == 0 and not allow_zero):\n        raise StoreValidationError(f\"El {what} debe ser mayor que cero\")\n    d = d.quantize(Decimal(\"0.01\"), rounding=ROUND_HALF_UP)"),
    ("price_half_up", "rounding=ROUND_HALF_UP)", "rounding=ROUND_HALF_EVEN)"),
    ("route404_as_notfound", "if code in PRODUCT_NOT_FOUND_CODES and ref_op:", "if st == 404 and ref_op:"),
    ("product404_any_notfound", "if code in PRODUCT_NOT_FOUND_CODES and ref_op:", "if (code in PRODUCT_NOT_FOUND_CODES or not code) and ref_op:"),
    ("route_missing_is_success_on_delete", "        except StoreNotFound:\n            return\n",
     "        except (StoreNotFound, StoreValidationError):\n            return\n"),
    ("no_header_secret_scrub", "out += [header, header[len(\"Basic \"):]]", "pass"),
    ("no_secret_scrub", "return _plain(text, self._secrets())", "return _plain(text, ())"),
    ("secrets_in_url", 'headers={"Authorization": self._auth_header()}', 'headers={"Authorization": self._auth_header(), "X-K": self.credentials["consumer_key"]}'),
    ("creds_in_query", "url = self.BASE_URL_TEMPLATE.format(host=self._host()) + prefix + path\n        if query:",
     "url = self.BASE_URL_TEMPLATE.format(host=self._host()) + prefix + path\n        query = {**(query or {}), 'consumer_key': self.credentials['consumer_key']}\n        if query:"),
    ("force_missing", 'query={"force": "true"}, ref_op=True)\n                return', 'ref_op=True)\n                return'),
    ("delete_force_missing", 'self._call("DELETE", f"/products/{pid}", query={"force": "true"}, ref_op=True)\n        except StoreNotFound:\n            return',
     'self._call("DELETE", f"/products/{pid}", ref_op=True)\n        except StoreNotFound:\n            return'),
    ("images_allow_http", 'if p.scheme != "https" or "." not in host', 'if p.scheme not in ("https", "http") or "." not in host'),
    ("images_allow_ip", "            ipaddress.ip_address(host)\n            continue                                         # IP literal",
     "            ipaddress.ip_address(host)\n            pass"),
    ("images_max", "if len(out) >= MAX_IMAGES:", "if len(out) >= MAX_IMAGES + 5:"),
    ("domain_ip_decimal", "if not _TLD_RE.fullmatch(tld) or tld in _RESERVED_TLDS:", "if tld in _RESERVED_TLDS:"),
    ("domain_port_allowed", "if int(port) != 443:", "if False:"),
    ("domain_userinfo", 'if not authority or "@" in s or', "if not authority or"),
    ("domain_userinfo_authority_only", 'if not authority or "@" in s or', 'if not authority or "@" in authority or'),
    ("domain_trailing_dot", [('or host.startswith(".") or host.endswith(".") or ".." in host', 'or host.startswith(".") or ".." in host'),
                             ("labels = [_label_to_ascii(lb, bad) for lb in host.split(\".\")]", "labels = [_label_to_ascii(lb, bad) for lb in host.rstrip(\".\").split(\".\")]")]),
    ("domain_label_regex", "if len(ascii_label) > 63 or not _LABEL_RE.fullmatch(ascii_label):", "if len(ascii_label) > 63:"),
    ("domain_percent", 'c in "\\\\%"', 'c in "\\\\"'),
    ("domain_localhost_tld", '_RESERVED_TLDS = {"localhost", "local",', '_RESERVED_TLDS = {"local",'),
    ("domain_idna_latin_only", 'if not _latin_accented(ch):\n                raise bad\n        try:', 'pass\n        try:'),
    ("domain_idna_dotless", 'return len(d) > 1 and "a" <= d[0] <= "z"', 'return "a" <= d[0] <= "z" or ch in "ıſß"'),
    ("domain_no_dot", "if len(labels) < 2 or len(ascii_host) > 253:", "if len(ascii_host) > 253:"),
    ("retry_5xx_not_retryable", 'return StoreError(f"La tienda tuvo un error temporal (HTTP {st}).{extra}", retryable=True)',
     'return StoreError(f"La tienda tuvo un error temporal (HTTP {st}).{extra}", retryable=False)'),
    ("429_as_plain_error", 'return StoreRateLimited("La tienda pidió esperar (demasiadas solicitudes)", self._retry_after(resp.headers))',
     'return StoreError("La tienda pidió esperar (demasiadas solicitudes)", retryable=True)'),
    ("not_sent_everywhere", "raise StoreError(self._text(msg), retryable=e.retryable, not_sent=e.not_sent) from None",
     "raise StoreError(self._text(msg), retryable=e.retryable, not_sent=True) from None"),
    ("retryable_lost", "raise StoreError(self._text(msg), retryable=e.retryable, not_sent=e.not_sent) from None",
     "raise StoreError(self._text(msg), retryable=False, not_sent=e.not_sent) from None"),
    ("html_200_accepted", 'raise StoreValidationError(\n                "La tienda no respondió con JSON:', 'return {}\n            raise StoreValidationError(\n                "La tienda no respondió con JSON:'),
    ("html_not_stripped", 's = _TAG_RE.sub(" ", str(text or "")[:20000])', 's = str(text or "")[:20000]'),
    ("redirect_message", 'if "redirecci" in msg.lower():', 'if False:'),
    ("401_not_auth", "if st in (401, 403):", "if st in (403,):"),
    ("403_not_auth", "if st in (401, 403):", "if st in (401,):"),
    ("cleanup_unbounded_none", "for attempt in range(CLEANUP_TRIES):", "for attempt in range(1):"),
    ("cleanup_no_orphan_id", "original.orphan_external_id = pid", "pass"),
    ("cleanup_adopted", "if not adopted:\n                self._cleanup_orphan(pid, e)", "if True:\n                self._cleanup_orphan(pid, e)"),
    ("sale_always", "return c if Decimal(c) < Decimal(price) else None", "return c"),
    ("sale_empty_string", "        if compare is None:\n            return None", "        if compare is None:\n            return ''"),
    ("update_no_manage_stock", 'body = {"regular_price": p, "manage_stock": True, "stock_quantity": n}', 'body = {"regular_price": p, "stock_quantity": n}'),
    ("price_as_float", 'body = {"name": title, "type": "simple", "status": "draft", "sku": sku, "regular_price": price,',
     'body = {"name": title, "type": "simple", "status": "draft", "sku": sku, "regular_price": float(price),'),
    ("description_unescaped", 'html.escape(p, quote=True).replace("\\n", "<br>")', 'p.replace("\\n", "<br>")'),
    ("set_active_inverted", '"publish" if active else "draft"', '"draft" if active else "publish"'),
    ("ref_not_validated", "if not _ID_RE.fullmatch(pid):", "if False:"),
    ("adopt_deletes_nothing_but_skips_update", "            if adopted:                              # lo adoptado", "            if False:                              # lo adoptado"),
    ("find_exact_sku_filter", 'd.get("sku") == sku and d.get("type")', 'd.get("sku") is not None and d.get("type")'),
    ("repr_leaks", 'return f"<WooCommerceConnector {self.domain}>"', 'return f"<WooCommerceConnector {self.domain} {dict(self.credentials)}>"'),
    ("creds_repr_leaks", 'return "<credenciales ocultas>"', "return dict.__repr__(self)"),
    ("basic_wrong_order", 'f"{k}:{s}".encode("utf-8")', 'f"{s}:{k}".encode("utf-8")'),
    ("validate_skips_products", 'prods = self._call("GET", "/products", query={"per_page": 1})', "prods = []"),
    ("currency_unvalidated", 'if not (isinstance(cur, str) and re.fullmatch(r"[A-Za-z]{3}", cur.strip())):\n            raise',
     "if False:\n            raise"),
    ("status_any_dropped", 'query={"sku": sku, "status": "any"}', 'query={"sku": sku}'),
    ("credentials_shape", "if not _KEY_RE.fullmatch(k):", "if False:"),
    ("check_secret_shape", "if not _SECRET_RE.fullmatch(s):", "if False:"),
    ("empty_sku_allowed", "or not sku.strip() or sku != sku.strip()", "or sku != sku.strip()"),
    ("validation_after_call", "        existing = self._find_by_sku(sku)\n        adopted", "        existing = self._find_by_sku(sku)\n        adopted"),
    ("stock_negative", "if n < 0 or n > MAX_STOCK:", "if n > MAX_STOCK:"),
    ("title_empty", 'if not title:\n            raise StoreValidationError("El producto necesita un título")', "pass"),
    ("shown_when_inactive", "if product.active and status != \"publish\":", "if status != \"publish\":"),
]
MUTANTS = [m for m in MUTANTS if m[0] != "validation_after_call"]       # (original == reemplazo: no es un mutante)


def _run_mutant(root: str, name: str, old, new=None) -> tuple[str, str]:
    """Devuelve (nombre, 'killed'|'SURVIVED'|'STALE')."""
    work = tempfile.mkdtemp(prefix="wcmut_")
    try:
        shutil.copytree(os.path.join(root, "backend"), os.path.join(work, "backend"),
                        ignore=shutil.ignore_patterns("__pycache__", "*.db"))
        shutil.copytree(os.path.join(root, "tests"), os.path.join(work, "tests"),
                        ignore=shutil.ignore_patterns("__pycache__", "e2e", "js"))
        path = os.path.join(work, CONNECTOR)
        with open(path, encoding="utf-8") as f:
            src = f.read()
        pairs = old if isinstance(old, list) else [(old, new)]
        for o, _ in pairs:
            if src.count(o) != 1:
                return name, f"STALE({src.count(o)})"
        for o, n in pairs:
            src = src.replace(o, n)
        with open(path, "w", encoding="utf-8") as f:
            f.write(src)
        env = {**os.environ, "DF_MUTATION": "", "PYTHONDONTWRITEBYTECODE": "1"}
        r = subprocess.run([sys.executable, "-m", "unittest", "test_woocommerce"], cwd=os.path.join(work, "tests"), env=env,
                           capture_output=True, text=True, timeout=600)
        return name, ("killed" if r.returncode != 0 else "SURVIVED")
    finally:
        shutil.rmtree(work, ignore_errors=True)


@unittest.skipUnless(os.environ.get("DF_MUTATION") == "1", "pruebas por mutación: ejecutar con DF_MUTATION=1")
class MutationTest(unittest.TestCase):
    def test_every_mutant_is_killed(self):
        root = os.path.abspath(os.path.join(HERE, ".."))
        with concurrent.futures.ThreadPoolExecutor(int(os.environ.get("DF_MUTATION_JOBS", "4"))) as ex:
            results = list(ex.map(lambda m: _run_mutant(root, *m), MUTANTS))
        report = ", ".join(f"{n}={s}" for n, s in results)
        print(f"\nmutantes: {len(results)}; muertos: {sum(1 for _, s in results if s == 'killed')}\n{report}")
        bad = [(n, s) for n, s in results if s != "killed"]
        self.assertEqual(bad, [], f"mutantes vivos o desactualizados: {bad}")


if __name__ == "__main__":
    unittest.main()
