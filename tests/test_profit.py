"""Calculadora de rentabilidad contraentrega (backend/profit.py): fórmulas con casos calculados a mano, bordes,
entradas hostiles, COP vs USD, supuestos por usuario, API (sesión, CSRF, aislamiento) y migración a v8.
Fórmulas: docs/profit-calculator.md.   Ejecutar:  python3 -m unittest discover tests"""
import json
import os
import random
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
import unittest.mock
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
os.environ.setdefault("DROPFINDER_DB", os.path.join(tempfile.mkdtemp(), "test.db"))

import auth  # noqa: E402
import auth_context  # noqa: E402
import db  # noqa: E402
import profit  # noqa: E402
import search  # noqa: E402
import server  # noqa: E402
from connectors.base import NormalizedProduct  # noqa: E402
from profit import ProfitError, calculate  # noqa: E402

PW = "una-clave-larga-1"

# Todo explícito en cero: aísla una fórmula sin supuestos de fábrica
ZERO = dict(supplier_shipping=0, delivery_freight=0, return_freight=0, packaging=0, cod_fee_pct=0, cod_fee_fixed=0,
            return_rate_pct=0, return_product_loss_pct=0, cpa=0)


def calc(**kw):
    return calculate({"currency": "COP", **kw})


class FormulaTest(unittest.TestCase):
    def test_caso_base_cop(self):
        """Precio 89.900, costo 30.000, fletes 15.000 + 15.000, empaque 1.500, recaudo 3 %, 25 % devoluciones, CPA 10.000.
        recaudo = 2.697; utilidad por entrega = 89.900 − 30.000 − 1.500 − 15.000 − 2.697 = 40.703;
        pérdida por devolución = 15.000 + 15.000 + 1.500 = 31.500;
        antes de publicidad = 0,75·40.703 − 0,25·31.500 = 22.652,25; utilidad confirmada = 12.652,25."""
        r = calc(price=89900, product_cost=30000, delivery_freight=15000, return_freight=15000, packaging=1500,
                 cod_fee_pct=3, cod_fee_fixed=0, return_rate_pct=25, return_product_loss_pct=0, cpa=10000)
        self.assertEqual(r["contribution_per_delivery"], 40703)
        self.assertEqual(r["loss_per_return"], 31500)
        self.assertEqual(r["profit_per_confirmed"], 12652)            # 12.652,25
        self.assertEqual(r["profit_per_delivered"], 16870)            # 12.652,25 / 0,75 = 16.869,67
        self.assertEqual(r["margin_pct"], 18.8)                       # 12.652,25 / (0,75·89.900) = 18,76 %
        self.assertEqual(r["max_cpa"], 22652)
        self.assertEqual(r["breakeven_roas"], 3.97)                   # 89.900 / 22.652,25 = 3,9687
        self.assertEqual(r["roas_used"], 8.99)
        self.assertEqual(r["profit_per_100"], 1265225)                # sin redondear antes de multiplicar
        self.assertEqual(r["delivered_per_100"], 75)
        self.assertEqual(r["defaults_used"], [])
        self.assertEqual(r["warnings"], [])
        b = r["breakdown"]
        self.assertEqual((b["revenue"], b["product_cost"], b["return_freight"], b["cod_fee"], b["ads"]),
                         (67425, 22500, 3750, 2023, 10000))

    def test_sin_devoluciones(self):
        """0 % devoluciones: todo se entrega. recaudo 2,5 % + 1.000 = 3.500; utilidad = 100.000 − 40.000 − 5.000 −
        2.000 − 12.000 − 3.500 = 37.500; con CPA 8.000 → 29.500."""
        r = calc(price=100000, product_cost=40000, supplier_shipping=5000, delivery_freight=12000, return_freight=12000,
                 packaging=2000, cod_fee_pct=2.5, cod_fee_fixed=1000, return_rate_pct=0, return_product_loss_pct=0,
                 cpa=8000)
        self.assertEqual(r["profit_per_confirmed"], 29500)
        self.assertEqual(r["profit_per_delivered"], 29500)            # sin devoluciones, confirmado = entregado
        self.assertEqual(r["margin_pct"], 29.5)
        self.assertEqual(r["max_cpa"], 37500)
        self.assertEqual(r["breakeven_roas"], 2.67)                   # 100.000 / 37.500
        self.assertEqual(r["profit_per_100"], 2950000)
        self.assertEqual(r["delivered_per_100"], 100)
        self.assertEqual(r["breakdown"]["return_freight"], 0)
        self.assertNotIn("high_returns", [w["code"] for w in r["warnings"]])

    def test_todo_devuelto(self):
        """100 % devoluciones: sin ingreso. Pérdida por devolución = 12.000 + 12.000 + 2.000 = 26.000; con CPA 5.000 → −31.000."""
        r = calc(price=100000, product_cost=40000, delivery_freight=12000, return_freight=12000, packaging=2000,
                 cod_fee_pct=3, cod_fee_fixed=0, return_rate_pct=100, return_product_loss_pct=0, cpa=5000)
        self.assertEqual(r["profit_per_confirmed"], -31000)
        self.assertIsNone(r["profit_per_delivered"])                  # nadie se entrega
        self.assertIsNone(r["margin_pct"])
        self.assertEqual(r["max_cpa"], 0)
        self.assertIsNone(r["breakeven_roas"])
        self.assertEqual(r["profit_per_100"], -3100000)
        self.assertEqual(r["delivered_per_100"], 0)
        self.assertEqual(r["breakdown"]["revenue"], 0)
        self.assertEqual(r["breakdown"]["cod_fee"], 0)                # sin recaudo no hay comisión
        codes = [w["code"] for w in r["warnings"]]
        self.assertIn("all_returned", codes)
        self.assertIn("negative_margin", codes)
        self.assertIn("high_returns", codes)

    def test_perdida_de_producto_devuelto(self):
        """50 % devoluciones, 20 % del producto devuelto se pierde. Entrega = 50.000 − 20.000 − 1.000 − 10.000 = 19.000;
        devolución = 10.000 + 8.000 + 1.000 + 0,2·20.000 = 23.000; utilidad = 9.500 − 11.500 = −2.000."""
        r = calc(**{**ZERO, "price": 50000, "product_cost": 20000, "delivery_freight": 10000, "return_freight": 8000,
                    "packaging": 1000, "return_rate_pct": 50, "return_product_loss_pct": 20})
        self.assertEqual(r["loss_per_return"], 23000)
        self.assertEqual(r["profit_per_confirmed"], -2000)
        self.assertEqual(r["profit_per_delivered"], -4000)
        self.assertEqual(r["margin_pct"], -8.0)                       # −2.000 / 25.000
        self.assertEqual(r["breakdown"]["returned_product_loss"], 2000)
        self.assertEqual(r["max_cpa"], 0)
        self.assertIn("negative_margin", [w["code"] for w in r["warnings"]])

    def test_roas_objetivo_y_supuestos_de_fabrica(self):
        """ROAS 4 sobre precio 90.000 → CPA 22.500. Con supuestos de fábrica: entrega = 90.000 − 30.000 − 1.500 − 15.000
        − 2.700 = 40.800; devolución 31.500; antes de publicidad = 0,75·40.800 − 0,25·31.500 = 22.725 → utilidad 225."""
        r = calc(price=90000, product_cost=30000, target_roas=4)
        self.assertEqual(r["inputs"]["cpa"], 22500)
        self.assertEqual(r["roas_used"], 4.0)
        self.assertEqual(r["profit_per_confirmed"], 225)
        self.assertEqual(r["profit_per_100"], 22500)
        self.assertEqual(r["max_cpa"], 22725)
        self.assertEqual(r["breakeven_roas"], 3.96)                   # 90.000 / 22.725 = 3,9604
        self.assertEqual(r["margin_pct"], 0.3)
        self.assertEqual(set(r["defaults_used"]), {"delivery_freight", "return_freight", "packaging", "cod_fee_pct",
                                                   "cod_fee_fixed", "return_product_loss_pct", "return_rate_pct"})
        codes = [w["code"] for w in r["warnings"]]
        self.assertIn("defaults_used", codes)
        self.assertIn("low_margin", codes)
        self.assertNotIn("no_ads", codes)

    def test_usd_dos_decimales(self):
        """USD: recaudo 3 % + 0,10 = 0,9997; entrega = 29,99 − 8,50 − 1,20 − 0,40 − 4 − 0,9997 = 14,8903; devolución = 8,40;
        antes de publicidad = 0,8·14,8903 − 0,2·8,4 = 10,23224; con CPA 5 → 5,23224."""
        r = calculate({"currency": "USD", "price": 29.99, "product_cost": 8.5, "supplier_shipping": 1.2,
                       "delivery_freight": 4, "return_freight": 4, "packaging": 0.4, "cod_fee_pct": 3,
                       "cod_fee_fixed": 0.1, "return_rate_pct": 20, "return_product_loss_pct": 0, "cpa": 5})
        self.assertEqual(r["decimals"], 2)
        self.assertEqual(r["profit_per_confirmed"], 5.23)
        self.assertEqual(r["profit_per_delivered"], 6.54)             # 5,23224 / 0,8 = 6,5403
        self.assertEqual(r["margin_pct"], 21.8)                       # 5,23224 / 23,992
        self.assertEqual(r["max_cpa"], 10.23)
        self.assertEqual(r["breakeven_roas"], 2.93)                   # 29,99 / 10,23224
        self.assertEqual(r["profit_per_100"], 523.22)

    def test_cop_sin_decimales_y_mitad_hacia_arriba(self):
        """1.000,5 → 1.001 (el redondeo bancario de round() daría 1.000); sin decimales en COP."""
        r = calc(**{**ZERO, "price": 1000.5, "product_cost": 0})
        self.assertEqual(r["profit_per_confirmed"], 1001)
        self.assertEqual(calc(**{**ZERO, "price": 1002.5, "product_cost": 0})["profit_per_confirmed"], 1003)
        u = calculate({"currency": "USD", **{**ZERO, "price": 1.005, "product_cost": 0}})
        self.assertEqual(u["profit_per_confirmed"], 1.01)             # 1,005 no es 1,00499999…
        for v in (r["profit_per_confirmed"], r["max_cpa"], r["profit_per_100"]):
            self.assertEqual(v, int(v))

    def test_precio_cero(self):
        r = calc(**{**ZERO, "price": 0, "product_cost": 10000})
        self.assertEqual(r["profit_per_confirmed"], -10000)           # sin ingreso: solo el costo
        self.assertEqual(r["profit_per_confirmed"], r["profit_per_delivered"])
        self.assertIsNone(r["margin_pct"])
        self.assertIsNone(r["breakeven_roas"])
        self.assertIsNone(r["roas_used"])
        self.assertIn("zero_price", [w["code"] for w in r["warnings"]])

    def test_efectividad_equivale_a_devoluciones(self):
        base = dict(price=89900, product_cost=30000, cpa=10000)
        a = calc(**base, return_rate_pct=30)
        b = calc(**base, delivery_rate_pct=70)
        c = calc(**base, return_rate_pct=30, delivery_rate_pct=70)
        self.assertEqual(a, b)
        self.assertEqual(a, c)
        self.assertEqual(a["inputs"]["delivery_rate_pct"], 70)
        with self.assertRaises(ProfitError):
            calc(**base, return_rate_pct=30, delivery_rate_pct=60)

    def test_avisos(self):
        base = dict(price=120000, product_cost=30000, cpa=5000)
        codes = lambda r: [w["code"] for w in r["warnings"]]
        self.assertIn("high_returns", codes(calc(**base, return_rate_pct=31)))
        self.assertNotIn("high_returns", codes(calc(**base, return_rate_pct=30)))        # umbral: estrictamente mayor
        self.assertIn("high_returns", codes(calc(**base, return_rate_pct=21, return_warn_pct=20)))
        self.assertIn("no_ads", codes(calc(price=120000, product_cost=30000)))
        neg = calc(price=20000, product_cost=30000, cpa=1000)
        self.assertIn("negative_margin", codes(neg))
        self.assertIn("$ ", [w for w in neg["warnings"] if w["code"] == "negative_margin"][0]["message"])

    def test_supuestos_del_usuario_reemplazan_los_de_fabrica(self):
        r = calc(price=89900, product_cost=30000, cpa=0, return_rate_pct=0, return_product_loss_pct=0, cod_fee_pct=0,
                 cod_fee_fixed=0, packaging=0, return_freight=0)
        self.assertEqual(r["inputs"]["delivery_freight"], 15000)
        mine = calculate({"currency": "COP", "price": 89900, "product_cost": 30000, "cpa": 0, "return_rate_pct": 0,
                          "return_product_loss_pct": 0, "cod_fee_pct": 0, "cod_fee_fixed": 0, "packaging": 0,
                          "return_freight": 0}, defaults={"delivery_freight": 20000})
        self.assertEqual(mine["inputs"]["delivery_freight"], 20000)
        self.assertEqual(mine["profit_per_confirmed"], 39900)
        self.assertEqual(mine["defaults_used"], ["delivery_freight"])

    def test_identidades_en_malla(self):
        """Propiedades que valen para cualquier entrada válida: con CPA = CPA máximo la utilidad es ≈ 0, con ROAS
        objetivo = ROAS de equilibrio también, y la utilidad por 100 es 100 × la utilidad por pedido."""
        rnd = random.Random(7)
        for _ in range(300):
            kw = dict(price=rnd.randint(20000, 300000), product_cost=rnd.randint(0, 120000),
                      supplier_shipping=rnd.randint(0, 9000), delivery_freight=rnd.randint(0, 25000),
                      return_freight=rnd.randint(0, 25000), packaging=rnd.randint(0, 4000),
                      cod_fee_pct=rnd.choice([0, 2.5, 3, 4.2]), cod_fee_fixed=rnd.choice([0, 800]),
                      return_rate_pct=rnd.choice([0, 10, 25, 33.3, 60, 100]),
                      return_product_loss_pct=rnd.choice([0, 15, 100]))
            r = calc(**kw, cpa=0)
            if r["max_cpa"] > 0:
                z = calc(**kw, cpa=r["max_cpa"])
                self.assertLessEqual(abs(z["profit_per_confirmed"]), 1, kw)
                self.assertLessEqual(abs(r["max_cpa"] - r["profit_per_confirmed"]), 1, kw)
                if 0.01 <= r["breakeven_roas"] <= 100:                    # rango admitido para el ROAS objetivo
                    be = calc(**kw, target_roas=r["breakeven_roas"])
                    tol = kw["price"] * 0.005 / r["breakeven_roas"] ** 2 * 1.05 + 1      # ROAS redondeado a 2 decimales
                    self.assertLessEqual(abs(be["profit_per_confirmed"]), tol, kw)
            else:
                self.assertLessEqual(r["profit_per_confirmed"], 0)
                self.assertIsNone(r["breakeven_roas"])
            self.assertLessEqual(abs(r["profit_per_100"] - 100 * r["profit_per_confirmed"]), 50)
            self.assertLessEqual(abs(sum(v for k, v in r["breakdown"].items() if k not in ("revenue", "profit"))
                                     * -1 + r["breakdown"]["revenue"] - r["profit_per_confirmed"]), 9)
            # Más devoluciones nunca mejoran la utilidad cuando una devolución cuesta más que una entrega rinde
            more = calc(**{**kw, "return_rate_pct": min(100, kw["return_rate_pct"] + 10)}, cpa=0)
            if r["contribution_per_delivery"] <= -r["loss_per_return"]:
                continue
            self.assertLessEqual(more["profit_per_confirmed"], r["profit_per_confirmed"] + 1)


class RegressionReviewTest(unittest.TestCase):
    """Hallazgos de la revisión del PR #21: veredicto sin redondear, bordes exactos y detalles de salida."""

    def test_veredicto_sin_redondear(self):
        z = dict(ZERO, product_cost=0)
        r = calc(**{**z, "price": 1000, "product_cost": 900.4})       # margen 9,96 % (se muestra 10,0)
        self.assertEqual((r["margin_pct"], r["verdict"]), (10.0, "low_margin"))
        self.assertIn("low_margin", [w["code"] for w in r["warnings"]])
        self.assertEqual(calc(**{**z, "price": 1000, "product_cost": 900})["verdict"], "profitable")   # 10,0 exacto
        self.assertNotIn("low_margin", [w["code"] for w in calc(**{**z, "price": 1000, "product_cost": 900})["warnings"]])
        be = calc(**{**z, "price": 1000, "product_cost": 1000})       # utilidad exactamente 0
        self.assertEqual(be["verdict"], "break_even")
        self.assertNotIn("negative_margin", [w["code"] for w in be["warnings"]])
        loss = calc(**{**z, "price": 1000, "product_cost": 1000.3})   # pierde 0,3: se redondea a 0
        self.assertEqual(loss["verdict"], "loss")
        msg = [w["message"] for w in loss["warnings"] if w["code"] == "negative_margin"][0]
        self.assertIn("menos de", msg)
        self.assertNotIn("Pierdes $ 0 ", msg)
        self.assertEqual(calc(**{**z, "price": 1000, "product_cost": 1})["verdict"], "profitable")

    def test_menos_cero_se_normaliza(self):
        import math
        r = calc(**{**ZERO, "price": 1000, "product_cost": 1000.3})
        self.assertEqual(r["profit_per_confirmed"], 0)
        self.assertEqual(math.copysign(1, r["profit_per_confirmed"]), 1.0)
        self.assertEqual(math.copysign(1, r["breakdown"]["profit"]), 1.0)

    def test_money_text_negativo(self):
        self.assertEqual(profit.money_text(-5, "COP"), "-$ 5")
        self.assertEqual(profit.money_text(-5.5, "USD"), "-US$ 5.50")
        self.assertEqual(profit.money_text(12500, "COP"), "$ 12.500")

    def test_roas_con_precio_cero_y_cpa(self):
        r = calc(**{**ZERO, "price": 0, "product_cost": 0, "cpa": 500})
        self.assertIsNone(r["roas_used"])
        self.assertIsNone(r["breakeven_roas"])

    def test_roas_objetivo_se_redondea_en_inputs(self):
        self.assertEqual(calc(price=100000, product_cost=1, target_roas=3.456)["inputs"]["target_roas"], 3.46)

    def test_devoluciones_y_efectividad_tolerancia(self):
        with self.assertRaises(ProfitError):
            calc(price=1, product_cost=1, return_rate_pct=25, delivery_rate_pct=74.5)
        calc(price=1, product_cost=1, return_rate_pct=25, delivery_rate_pct=75.005)      # dentro de 0,01


class ValidationTest(unittest.TestCase):
    BASE = dict(currency="COP", price=89900, product_cost=30000)

    def bad(self, **over):
        inputs = {**self.BASE, **over}
        with self.assertRaises(ProfitError, msg=repr(over)) as cm:
            calculate(inputs)
        self.assertTrue(str(cm.exception))
        return cm.exception

    def test_requeridos(self):
        for k in ("price", "product_cost"):
            e = self.bad(**{k: None})
            self.assertEqual(e.field, k)
        with self.assertRaises(ProfitError):
            calculate({"currency": "COP"})

    def test_tipos_no_numericos(self):
        for v in ("12", "abc", "", True, False, [], {}, [1], {"a": 1}, b"1"):
            self.assertEqual(self.bad(price=v).field, "price", v)
            self.bad(packaging=v)
            self.bad(return_rate_pct=v)

    def test_nan_e_infinitos(self):
        for v in (float("nan"), float("inf"), float("-inf")):
            for k in ("price", "delivery_freight", "return_rate_pct", "cpa", "cod_fee_pct"):
                self.bad(**{k: v})
        self.bad(cpa=None, target_roas=float("nan"))

    def test_negativos_y_porcentajes(self):
        for k in ("product_cost", "supplier_shipping", "delivery_freight", "return_freight", "packaging",
                  "cod_fee_fixed", "cpa"):
            self.bad(**{k: -1})
            self.bad(**{k: -0.01})
        for k in ("return_rate_pct", "delivery_rate_pct", "cod_fee_pct", "return_product_loss_pct", "return_warn_pct"):
            self.bad(**{k: -0.1})
            self.bad(**{k: 100.01})
            self.bad(**{k: 1e9})
        calc(price=1, product_cost=1, return_rate_pct=100)           # el borde 100 sí vale
        calc(price=1, product_cost=1, return_rate_pct=0)

    def test_topes(self):
        self.assertEqual(self.bad(price=400_000_001).field, "price")
        calc(price=400_000_000, product_cost=0)
        e = self.bad(currency="USD", price=100_001)
        self.assertEqual(e.field, "price")
        calculate({"currency": "USD", "price": 100_000, "product_cost": 0})
        self.bad(price=10 ** 400)                                    # entero gigante: sin OverflowError
        self.bad(price=1e300)
        self.bad(price=-(10 ** 400))

    def test_roas_y_cpa(self):
        for v in (0, -1, 0.001, 100.1, 1e9):
            self.assertEqual(self.bad(target_roas=v).field, "target_roas")
        self.assertEqual(self.bad(cpa=1000, target_roas=3).field, "target_roas")     # no los dos
        calc(price=100, product_cost=10, target_roas=0.01)
        calc(price=100, product_cost=10, target_roas=100)
        calc(price=100, product_cost=10, cpa=None, target_roas=2)                   # None = ausente

    def test_moneda_y_campos(self):
        for c in ("EUR", "", None, 5, [], "cop2"):
            self.assertEqual(self.bad(currency=c).field, "currency", c)
        self.assertEqual(calculate({**self.BASE, "currency": " cop "})["currency"], "COP")
        self.assertEqual(self.bad(precio=5).field, "precio")                         # campo desconocido
        for inputs in (None, [], "x", 5, [{"price": 1}]):
            with self.assertRaises(ProfitError):
                calculate(inputs)
        with self.assertRaises(ProfitError):
            calculate(self.BASE, defaults={"price": 5})                              # no es un supuesto
        with self.assertRaises(ProfitError):
            calculate(self.BASE, defaults={"packaging": -1})

    def test_fuzz_solo_lanza_profit_error(self):
        rnd = random.Random(11)
        junk = [None, True, "x", "1", -1, 0, 1, 1.5, 1e308, -1e308, 10 ** 40, float("nan"), float("inf"), [], {}, [1], 100,
                100.5, 99.99, -0.0, 5e-324]
        for _ in range(2000):
            inputs = {k: rnd.choice(junk) if rnd.random() < 0.5 else rnd.choice([0, 10, 55.5, 1000])
                      for k in rnd.sample(profit.INPUT_KEYS, rnd.randint(0, len(profit.INPUT_KEYS)))}
            try:
                r = calculate(inputs)
            except ProfitError:
                continue
            json.dumps(r, allow_nan=False)                           # resultado siempre serializable, sin NaN/inf


class _Client:
    def __init__(self, base):
        self.base, self.token, self.csrf = base, None, None

    def call(self, method, path, body=None, *, csrf=True, raw=None, session=True):
        h = {"Content-Type": "application/json"}
        if self.token and session:
            h["Cookie"] = f"{auth.COOKIE_NAME}={self.token}"
        if csrf and self.csrf and method not in ("GET", "HEAD"):
            h["X-CSRF-Token"] = self.csrf
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.base + path, method=method, headers=h, data=data)
        try:
            resp = urllib.request.build_opener().open(req)
        except urllib.error.HTTPError as e:
            resp = e
        with resp:
            payload = resp.read()
            status = getattr(resp, "status", None) or resp.code
            for c in resp.headers.get_all("Set-Cookie") or []:
                if c.startswith(auth.COOKIE_NAME + "="):
                    self.token = c.split("=", 1)[1].split(";", 1)[0]
        try:
            out = json.loads(payload) if payload else None
        except ValueError:
            out = payload.decode("utf-8", "replace")
        if isinstance(out, dict) and out.get("csrf_token"):
            self.csrf = out["csrf_token"]
        return status, out


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="dropfinder-profit-")
        cls.old_db, db.DB_PATH = db.DB_PATH, os.path.join(cls.dir, "t.db")
        db.init_db(seed_size=0)
        with db.connect() as conn:
            db.upsert_products(conn, [NormalizedProduct(
                supplier="Prov", supplier_sku="P1", title="Producto uno", category="Hogar", cost=10.0, shipping_cost=2.0,
                shipping_days=5, warehouse="CO", stock=50, orders=100, orders_7d=5, orders_prev_7d=5, rating=4.5,
                reviews=3)], "csv")
            cls.pid = conn.execute("SELECT id FROM products").fetchone()[0]
        cls.srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.owner = _Client(cls.base)
        st, d = cls.owner.call("POST", "/api/auth/register", {"email": "duena@ejemplo.com", "name": "Dueña", "password": PW})
        assert st == 201, d
        st, d = cls.owner.call("POST", "/api/users", {"email": "m@ejemplo.com", "name": "M", "password": PW, "role": "member"})
        assert st == 201, d
        cls.member = _Client(cls.base)
        st, d = cls.member.call("POST", "/api/auth/login", {"email": "m@ejemplo.com", "password": PW})
        assert st == 200, d
        st, d = cls.owner.call("PUT", "/api/currency", {"display_currency": "COP", "rate_mode": "manual",
                                                       "manual_rate": 4000, "hedge_pct": 0})
        assert st == 200, d
        assert cls.member.call("PUT", "/api/currency", {"display_currency": "COP"})[0] == 200

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        db.DB_PATH = cls.old_db
        search.invalidate()
        shutil.rmtree(cls.dir, ignore_errors=True)

    def setUp(self):
        with db.connect() as conn:
            conn.execute("DELETE FROM profit_defaults")

    BODY = {"price": 89900, "product_cost": 30000, "delivery_freight": 15000, "return_freight": 15000, "packaging": 1500,
            "cod_fee_pct": 3, "cod_fee_fixed": 0, "return_rate_pct": 25, "return_product_loss_pct": 0, "cpa": 10000}

    def test_rutas_clasificadas_como_de_usuario(self):
        for key in (("POST", r"/api/profit/calc"), ("GET", r"/api/profit/defaults"), ("PUT", r"/api/profit/defaults"),
                    ("GET", r"/api/products/(\d+)/profit")):
            self.assertEqual(server.route_access(*key), "user", key)
            self.assertIn(key, {(m, p) for m, p, _ in server.ROUTES})
            self.assertIn(key, server.USER_ROUTES)
            self.assertNotIn(key, server.OWNER_ONLY)

    def test_requiere_sesion(self):
        anon = _Client(self.base)
        for method, path, body in (("POST", "/api/profit/calc", self.BODY), ("GET", "/api/profit/defaults", None),
                                   ("PUT", "/api/profit/defaults", {"defaults": {}}),
                                   ("GET", f"/api/products/{self.pid}/profit", None)):
            self.assertEqual(anon.call(method, path, body)[0], 401, path)

    def test_csrf_en_escrituras(self):
        for method, path, body in (("POST", "/api/profit/calc", self.BODY),
                                   ("PUT", "/api/profit/defaults", {"currency": "COP", "defaults": {"packaging": 1}})):
            st, d = self.member.call(method, path, body, csrf=False)
            self.assertEqual(st, 403, d)
            self.assertIn("CSRF", d["error"])
        self.assertEqual(self.member.call("GET", "/api/profit/defaults")[0], 200)     # las lecturas no lo necesitan
        with db.connect() as conn:                                                    # y la escritura rechazada no guardó nada
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM profit_defaults").fetchone()[0], 0)

    def test_calc_ok_y_miembro_no_propietario(self):
        st, d = self.member.call("POST", "/api/profit/calc", self.BODY)
        self.assertEqual(st, 200, d)
        self.assertEqual((d["currency"], d["profit_per_confirmed"], d["margin_pct"]), ("COP", 12652, 18.8))
        self.assertEqual(self.owner.call("POST", "/api/profit/calc", self.BODY)[1], d)

    def test_calc_usa_la_moneda_del_usuario_y_la_pedida(self):
        usd = {"price": 29.99, "product_cost": 8.5}
        self.member.call("PUT", "/api/currency", {"display_currency": "USD"})
        try:
            st, d = self.member.call("POST", "/api/profit/calc", usd)
            self.assertEqual((st, d["currency"], d["decimals"]), (200, "USD", 2))
            self.assertEqual(d["inputs"]["delivery_freight"], 4.0)            # fábrica en USD, no en COP
            st, d = self.member.call("POST", "/api/profit/calc", {**usd, "currency": "COP"})
            self.assertEqual((st, d["currency"], d["inputs"]["delivery_freight"]), (200, "COP", 15000))
        finally:
            self.member.call("PUT", "/api/currency", {"display_currency": "COP"})
        st, d = self.owner.call("POST", "/api/profit/calc", usd)                  # el propietario sigue en COP
        self.assertEqual(d["currency"], "COP")

    def test_entradas_invalidas_dan_400_sin_500(self):
        cases = [
            ({"price": "abc", "product_cost": 1}, "invalid_price"), ({"price": -5, "product_cost": 1}, "invalid_price"),
            ({"price": 5}, "invalid_product_cost"), ({"price": 5, "product_cost": 1, "return_rate_pct": 101}, "invalid_return_rate_pct"),
            ({"price": 5, "product_cost": 1, "cpa": 1, "target_roas": 2}, "invalid_target_roas"),
            ({"price": 5, "product_cost": 1, "currency": "EUR"}, "invalid_currency"),
            ({"price": 5, "product_cost": 1, "x": 1}, "invalid_x"), ({}, "invalid_price"),
            ({"price": 10 ** 400, "product_cost": 1}, "invalid_price"),
        ]
        for body, code in cases:
            st, d = self.member.call("POST", "/api/profit/calc", body)
            self.assertEqual((st, d.get("code")), (400, code), body)
            self.assertTrue(d["error"])
        for raw in (b'{"price": NaN, "product_cost": 1}', b'{"price": Infinity, "product_cost": 1}',
                    b'{"price": -Infinity, "product_cost": 1}', b'{"price": 1e999, "product_cost": 1}',
                    b'[1, 2]', b'"hola"', b'null', b'5', b'{"price": ', b'\xff\xfe', b'{"price": [[[[[1]]]]], "product_cost": 1}'):
            st, d = self.member.call("POST", "/api/profit/calc", raw=raw)
            self.assertEqual(st, 400, raw)
            self.assertIn("error", d)

    def test_enteros_gigantes_en_json_dan_400(self):
        """json.loads lanza ValueError (no JSONDecodeError) con enteros de más de 4300 dígitos: antes era un 500."""
        big = b"9" * 5000
        cases = [("POST", "/api/profit/calc", b'{"price": ' + big + b', "product_cost": 1}'),
                 ("PUT", "/api/profit/defaults", b'{"currency": "COP", "defaults": {"packaging": ' + big + b"}}"),
                 ("POST", "/api/imports", b'{"product_id": 1, "sell_price": ' + big + b"}")]
        for method, path, raw in cases:
            st, d = self.member.call(method, path, raw=raw)
            self.assertEqual((st, d.get("error")), (400, "JSON inválido"), path)

    def test_calc_devuelve_veredicto(self):
        st, d = self.member.call("POST", "/api/profit/calc", self.BODY)
        self.assertEqual(d["verdict"], "profitable")

    def test_supuestos_por_usuario_aislados(self):
        st, d = self.member.call("GET", "/api/profit/defaults?currency=COP")
        self.assertEqual((st, d["custom_keys"], d["defaults"], d["defaults"]), (200, [], d["builtin"], d["builtin"]))
        self.assertEqual(d["defaults"]["delivery_freight"], 15000)
        st, d = self.member.call("PUT", "/api/profit/defaults", {"currency": "COP", "defaults": {"delivery_freight": 9000,
                                                                                             "return_rate_pct": 40}})
        self.assertEqual(st, 200, d)
        self.assertEqual((d["defaults"]["delivery_freight"], d["defaults"]["return_rate_pct"], d["custom_keys"]),
                         (9000, 40, ["delivery_freight", "return_rate_pct"]))
        # La calculadora del miembro los usa; la del propietario sigue con los de fábrica
        mine = self.member.call("POST", "/api/profit/calc", {"price": 80000, "product_cost": 20000, "cpa": 0})[1]
        theirs = self.owner.call("POST", "/api/profit/calc", {"price": 80000, "product_cost": 20000, "cpa": 0})[1]
        self.assertEqual((mine["inputs"]["delivery_freight"], mine["inputs"]["return_rate_pct"]), (9000, 40))
        self.assertEqual((theirs["inputs"]["delivery_freight"], theirs["inputs"]["return_rate_pct"]), (15000, 25))
        self.assertEqual(self.owner.call("GET", "/api/profit/defaults?currency=COP")[1]["custom_keys"], [])
        # Cada moneda tiene los suyos
        self.assertEqual(self.member.call("GET", "/api/profit/defaults?currency=USD")[1]["custom_keys"], [])
        # null = volver al de fábrica; el resto se conserva
        st, d = self.member.call("PUT", "/api/profit/defaults", {"currency": "COP", "defaults": {"delivery_freight": None}})
        self.assertEqual((st, d["custom_keys"], d["defaults"]["delivery_freight"]), (200, ["return_rate_pct"], 15000))
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM profit_defaults").fetchone()[0], 1)

    def test_put_supuestos_invalido_no_guarda_nada(self):
        for body in ({"currency": "COP", "defaults": {"packaging": 2000, "return_rate_pct": 150}},
                     {"currency": "COP", "defaults": {"packaging": "x"}}, {"currency": "COP", "defaults": {"price": 5}},
                     {"currency": "COP", "defaults": {"cpa": 5}}, {"currency": "COP", "defaults": []},
                     {"currency": "COP"}, {"currency": "EUR", "defaults": {}}, [], {"defaults": {"packaging": -1}}):
            st, d = self.member.call("PUT", "/api/profit/defaults", body)
            self.assertEqual(st, 400, body)
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM profit_defaults").fetchone()[0], 0)
        self.assertEqual(self.member.call("GET", "/api/profit/defaults?currency=EUR")[0], 400)

    def test_supuestos_corruptos_en_la_base_se_ignoran(self):
        uid = self.member.call("GET", "/api/auth/me")[1]["user"]["id"]
        with db.connect() as conn:
            conn.execute("INSERT INTO profit_defaults VALUES (?, 'COP', ?, 'x')",
                         (uid, json.dumps({"packaging": 2500, "delivery_freight": "mucho", "return_rate_pct": 999, "zzz": 1})))
        st, d = self.member.call("GET", "/api/profit/defaults?currency=COP")
        self.assertEqual((st, d["defaults"]["packaging"], d["defaults"]["delivery_freight"], d["defaults"]["return_rate_pct"]),
                         (200, 2500, 15000, 25.0))
        with db.connect() as conn:
            conn.execute("UPDATE profit_defaults SET data = '{no es json'")
        self.assertEqual(self.member.call("GET", "/api/profit/defaults?currency=COP")[1]["custom_keys"], [])
        self.assertEqual(self.member.call("POST", "/api/profit/calc", self.BODY)[0], 200)

    def test_rentabilidad_de_un_producto(self):
        # costo 10 + envío 2 = 12 USD → precio sugerido 29,99 USD. Tasa manual 4.000 → costo 40.000, envío 8.000,
        # precio 119.960 con redondeo comercial a ...900 → 120.900
        st, d = self.owner.call("GET", f"/api/products/{self.pid}/profit")
        self.assertEqual(st, 200, d)
        self.assertEqual(d["inputs"], {"currency": "COP", "price": 120900.0, "product_cost": 40000.0,
                                       "supplier_shipping": 8000.0})
        self.assertEqual(d["product"]["title"], "Producto uno")
        r = d["result"]
        self.assertEqual(r["inputs"]["price"], 120900)
        self.assertEqual(set(r["defaults_used"]) >= {"delivery_freight", "return_rate_pct"}, True)
        # Los supuestos del usuario entran; los de otro usuario no
        self.member.call("PUT", "/api/profit/defaults", {"currency": "COP", "defaults": {"delivery_freight": 1000}})
        self.assertEqual(self.member.call("GET", f"/api/products/{self.pid}/profit")[1]["result"]["inputs"]["delivery_freight"], 1000)
        self.assertEqual(self.owner.call("GET", f"/api/products/{self.pid}/profit")[1]["result"]["inputs"]["delivery_freight"], 15000)
        # Misma cuenta pedida en USD: sin tasa ni redondeo comercial
        st, d = self.owner.call("GET", f"/api/products/{self.pid}/profit?currency=USD")
        self.assertEqual((st, d["inputs"]), (200, {"currency": "USD", "price": 29.99, "product_cost": 10.0, "supplier_shipping": 2.0}))
        self.assertEqual(self.owner.call("GET", f"/api/products/{self.pid}/profit?currency=EUR")[0], 400)
        self.assertEqual(self.owner.call("GET", "/api/products/999999/profit")[0], 404)

    def test_clasificada_la_ruta_de_producto_no_choca_con_el_detalle(self):
        st, d = self.owner.call("GET", f"/api/products/{self.pid}")
        self.assertEqual((st, d["id"]), (200, self.pid))


class MigrationV8Test(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="dropfinder-mig8-")
        self.old, db.DB_PATH = db.DB_PATH, os.path.join(self.dir, "t.db")

    def tearDown(self):
        db.DB_PATH = self.old
        search.invalidate()
        shutil.rmtree(self.dir, ignore_errors=True)

    def _v7(self):
        """Base como la dejaba la versión 7: con datos y sin la tabla de supuestos."""
        db.init_db(seed_size=20)
        conn = sqlite3.connect(db.DB_PATH)
        conn.execute("DROP TABLE profit_defaults")
        conn.execute("PRAGMA user_version = 7")
        conn.commit()
        conn.close()

    def _tables(self):
        with db.connect() as conn:
            return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}, \
                conn.execute("PRAGMA user_version").fetchone()[0], conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]

    def test_version_actual_es_8(self):
        self.assertEqual(db.SCHEMA_VERSION, 8)

    def test_migra_de_v7_a_v8_conservando_datos(self):
        self._v7()
        tables, version, n = self._tables()
        self.assertEqual((version, "profit_defaults" in tables), (7, False))
        db.init_db(seed_size=20)
        db.init_db(seed_size=20)                                   # idempotente
        tables2, version2, n2 = self._tables()
        self.assertEqual((version2, "profit_defaults" in tables2, n2), (8, True, n))
        with db.connect() as conn:                                 # la tabla funciona y exige moneda válida
            conn.execute("INSERT INTO profit_defaults VALUES (1, 'COP', '{}', 'x')")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO profit_defaults VALUES (1, 'EUR', '{}', 'x')")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO profit_defaults VALUES (1, 'COP', '{}', 'x')")    # una fila por usuario y moneda

    def test_migracion_atomica_si_falla(self):
        """Si algo falla a mitad, la tabla no queda creada, la versión sigue en 7 y el siguiente arranque la completa."""
        self._v7()
        real = profit.ensure_schema

        def boom(conn):
            real(conn)
            raise RuntimeError("fallo simulado")
        with unittest.mock.patch.object(profit, "ensure_schema", boom):
            with self.assertRaises(RuntimeError):
                db.init_db(seed_size=20)
        tables, version, _ = self._tables()
        self.assertEqual((version, "profit_defaults" in tables), (7, False))
        db.init_db(seed_size=20)
        tables, version, _ = self._tables()
        self.assertEqual((version, "profit_defaults" in tables), (8, True))


if __name__ == "__main__":
    unittest.main()
