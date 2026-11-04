"""Pruebas de la API. Ejecutar:  python3 -m unittest discover tests"""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
os.environ["DROPFINDER_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")

import db  # noqa: E402
import server  # noqa: E402
from metrics import DEFAULT_SETTINGS, break_even, profit_for, suggested_price  # noqa: E402


def call(method, path, body=None):
    req = urllib.request.Request(f"{BASE}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


class MetricsTest(unittest.TestCase):
    def test_price_profit(self):
        s = DEFAULT_SETTINGS
        self.assertEqual(suggested_price(10, s), 24.99)
        profit, margin = profit_for(24.99, 10, s)
        self.assertAlmostEqual(profit, 24.99 - 10 - (24.99 * 0.029 + 0.30), places=2)
        self.assertGreater(margin, 50)
        be = break_even(10, s)
        self.assertAlmostEqual(profit_for(be, 10, s)[0], 0, delta=0.01)


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global BASE
        db.init_db(seed_size=400)
        cls.srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        BASE = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def test_search_accent_insensitive(self):
        _, a = call("GET", "/api/products?q=electronica")
        _, b = call("GET", "/api/products?q=ELECTR%C3%93NICA")
        self.assertGreater(a["total"], 0)
        self.assertEqual(a["total"], b["total"])

    def test_filters(self):
        _, d = call("GET", "/api/products?supplier=Amazon&max_ship_days=4&min_rating=4.5&in_stock=1&page_size=100")
        self.assertTrue(d["items"])
        for p in d["items"]:
            self.assertEqual(p["supplier"], "Amazon")
            self.assertLessEqual(p["shipping_days"], 4)
            self.assertGreaterEqual(p["rating"], 4.5)
            self.assertGreater(p["stock"], 0)

    def test_sort_and_pagination(self):
        _, d = call("GET", "/api/products?sort=orders&page_size=10&page=2")
        orders = [p["orders"] for p in d["items"]]
        self.assertEqual(orders, sorted(orders, reverse=True))
        self.assertEqual(d["page"], 2)

    def test_bad_param(self):
        status, _ = call("GET", "/api/products?min_cost=abc")
        self.assertEqual(status, 400)

    def test_import_and_monitor(self):
        status, imp = call("POST", "/api/imports", {"product_id": 10, "sell_price": 50})
        self.assertEqual(status, 200)
        self.assertEqual(imp["sell_price"], 50)
        status, _ = call("POST", "/api/imports", {"product_id": 10})
        self.assertEqual(status, 409)
        status, _ = call("PATCH", f"/api/imports/{imp['id']}", {"sell_price": 0})
        self.assertEqual(status, 400)
        # Precio absurdamente bajo -> el monitor debe alertar margen negativo
        call("PATCH", f"/api/imports/{imp['id']}", {"sell_price": 0.5})
        _, r = call("POST", "/api/monitor/run")
        self.assertIn("negative_margin", [a["type"] for a in r["alerts"]])
        _, alerts = call("GET", "/api/alerts")
        self.assertTrue(alerts)


    def test_csv_upload_replace_and_alerts(self):
        csv1 = ("sku;titulo;costo;categoria;stock;url;imagen\n"
                "T-1;Taza térmica;5,50;Hogar;10;https://prov.example/t1;javascript:alert(1)\n"
                "T-2;Plato hondo;3;Hogar;5;;\n"
                "T-3;;3;Hogar;5;;\n")
        status, r = call("POST", "/api/catalog/csv", {"supplier": "Mayorista Test", "csv": csv1})
        self.assertEqual(status, 200, r)
        self.assertEqual((r["new"], r["updated"]), (2, 0))
        self.assertEqual(len(r["errors"]), 1)                     # fila sin título
        _, d = call("GET", "/api/products?q=taza%20termica&source=csv&in_stock=0")
        self.assertEqual(d["total"], 1)
        p = d["items"][0]
        self.assertEqual(p["product_url"], "https://prov.example/t1")
        self.assertEqual(p["image"], "")                          # URL no http(s) descartada
        call("POST", "/api/imports", {"product_id": p["id"], "sell_price": 20})

        # Re-subida: T-1 sube de costo, T-2 desaparece (se borra), T-9 es nuevo
        csv2 = "sku,titulo,costo,stock\nT-1,Taza térmica,9.00,10\nT-9,Vaso,2,3\n"
        _, r = call("POST", "/api/catalog/csv", {"supplier": "Mayorista Test", "csv": csv2, "replace": True})
        self.assertEqual((r["new"], r["updated"], r["removed"]), (1, 1, 1))
        self.assertGreaterEqual(r["alerts"], 1)
        _, alerts = call("GET", "/api/alerts")
        self.assertTrue(any(a["type"] == "price_up" and "Taza" in a["title"] for a in alerts))

        # El monitor no simula productos CSV
        _, m = call("POST", "/api/monitor/run")
        self.assertGreaterEqual(m["skipped"], 1)

        # Borrar el catálogo conserva el producto importado
        _, r = call("DELETE", "/api/catalog?source=csv&supplier=Mayorista%20Test")
        self.assertEqual(r["deleted"], 1)
        _, sups = call("GET", "/api/suppliers")
        self.assertIn(("Mayorista Test", 1), [(s["supplier"], s["products"]) for s in sups])

    def test_csv_missing_columns(self):
        status, r = call("POST", "/api/catalog/csv", {"supplier": "X", "csv": "nombre;stock\na;1\n"})
        self.assertEqual(status, 400)
        self.assertIn("sku", r["error"])

    def test_zz_delete_demo_not_reseeded(self):
        _, r = call("DELETE", "/api/catalog?source=mock")
        self.assertGreater(r["deleted"], 0)
        db.init_db(seed_size=400)                                 # simula reiniciar el servidor
        _, d = call("GET", "/api/products?source=mock&in_stock=0&page_size=100")
        _, imports = call("GET", "/api/imports")
        mock_imported = sum(1 for i in imports if i["source"] == "mock")
        self.assertEqual(d["total"], mock_imported)               # solo quedan los importados


if __name__ == "__main__":
    unittest.main()
