"""Pruebas del motor de búsqueda (FTS5, raíces, sinónimos, "¿Quisiste decir?", autocompletado),
de las métricas precalculadas y de la migración a v3. Ejecutar:  python3 -m unittest discover tests"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from urllib.parse import quote

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
os.environ.setdefault("DROPFINDER_DB", os.path.join(tempfile.mkdtemp(), "test.db"))

import db  # noqa: E402
import metrics  # noqa: E402
import search  # noqa: E402
import server  # noqa: E402


def call(base, method, path, body=None):
    req = urllib.request.Request(f"{base}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


class _TempDb:
    """Apunta db.DB_PATH a una base temporal propia y la restaura al terminar."""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="dropfinder-test-")
        self.path = os.path.join(self.dir, "t.db")
        self.old = db.DB_PATH
        db.DB_PATH = self.path

    def close(self):
        db.DB_PATH = self.old
        search.invalidate()
        shutil.rmtree(self.dir, ignore_errors=True)


class StemmerTest(unittest.TestCase):
    def test_plurales_y_genero(self):
        st = search.stem
        for a, b in [("lamparas", "lampara"), ("cargadores", "cargador"), ("auriculares", "auricular"),
                     ("luces", "luz"), ("relojes", "reloj"), ("cables", "cable"), ("bebes", "bebe"),
                     ("estaciones", "estacion"), ("rapidos", "rapida"), ("perrito", "perro"), ("gafas", "gafa")]:
            self.assertEqual(st(a), st(b), (a, b))
        self.assertEqual(st("circuito"), "circuit")          # no es diminutivo
        self.assertNotEqual(st("cargador"), st("carga"))      # -dor no se recorta (lo unen los sinónimos)
        self.assertEqual(st("4k"), "4k")

    def test_unaccent(self):
        self.assertEqual(search.unaccent("LÁMPARA Niño Crème"), "lampara nino creme")

    def test_edit_distance(self):
        self.assertEqual(search.edit_distance("cargdor", "cargador", 2), 1)
        self.assertEqual(search.edit_distance("cragador", "cargador", 2), 1)   # transposición
        self.assertEqual(search.edit_distance("abc", "xyzw", 2), 3)            # corta al pasar el límite

    def test_match_con_sinonimos_y_prefijo(self):
        m = search.build_match(search.query_tokens("audífonos de carg"))
        self.assertIn('"auricular"', m)
        self.assertIn('"carg"*', m)
        self.assertNotIn('"de"', m)                           # palabras vacías fuera


class SearchApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = _TempDb()
        db.init_db(seed_size=1500)
        cls.srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        csv = ("sku;titulo;costo;categoria;etiquetas;pedidos\n"
               "A-1;Audífonos gamer pro;10;Audio;;100\n"
               "A-2;Funda para móvil;3;Accesorios;;100\n"
               "R-1;Zumbador sonoro;5;Varios;;100\n"
               "R-2;Objeto sonoro;5;Varios;zumbador;100\n"
               "T-1;Proyectr portátil;20;Varios;;100\n")
        status, r = cls.get("POST", "/api/catalog/csv", {"supplier": "Prov Busqueda", "csv": csv})
        assert status == 200, r

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.tmp.close()

    @classmethod
    def get(cls, method, path, body=None):
        return call(cls.base, method, path, body)

    def search(self, qs):
        status, d = self.get("GET", "/api/products?" + qs)
        self.assertEqual(status, 200, d)
        return d

    def test_plural(self):
        a, b = self.search("q=lamparas"), self.search("q=lampara")
        self.assertGreater(a["total"], 0)
        self.assertEqual(a["total"], b["total"])
        self.assertTrue(all("Lámpara" in p["title"] for p in a["items"]))
        self.assertGreater(self.search("q=cargadores")["total"], 0)
        self.assertGreater(self.search("q=auricular")["total"], 0)

    def test_tildes_y_mayusculas(self):
        self.assertEqual(self.search("q=" + quote("LÁMPARAS"))["total"], self.search("q=lampara")["total"])
        self.assertEqual(self.search("q=magnetico")["total"], self.search("q=" + quote("Magnético"))["total"])

    def test_prefijo(self):
        d = self.search("q=carg")
        self.assertGreater(d["total"], 0)
        self.assertTrue(any(p["title"].startswith("Cargador") for p in d["items"]))

    def test_sinonimos(self):
        d = self.search("q=auriculares&page_size=100")
        titles = [p["title"] for p in d["items"]]
        self.assertIn("Audífonos gamer pro", self.search("q=auriculares&source=csv")["items"][0]["title"])
        self.assertTrue(any(t.startswith("Auriculares") for t in titles))
        self.assertEqual(self.search("q=celular&source=csv")["items"][0]["title"], "Funda para móvil")
        self.assertGreater(self.search("q=termo")["total"], 0)        # → "botella térmica" (frase)

    def test_quisiste_decir_autocorrige(self):
        d = self.search("q=cargdor")
        self.assertEqual(d["corrected_from"], "cargdor")
        self.assertEqual(d["query"], "cargador")
        self.assertIsNone(d["suggestion"])
        self.assertGreater(d["total"], 0)
        self.assertEqual(d["total"], self.search("q=cargador")["total"])

    def test_quisiste_decir_sugerencia_con_pocos_resultados(self):
        d = self.search("q=proyectr")                         # 1 producto escrito así, muchos "proyector"
        self.assertEqual(d["total"], 1)
        self.assertEqual(d["suggestion"], "proyector")
        self.assertIsNone(d["corrected_from"])
        self.assertIsNone(self.search("q=cargador")["suggestion"])

    def test_sin_resultados(self):
        d = self.search("q=xqzwv")
        self.assertEqual((d["total"], d["items"], d["suggestion"], d["corrected_from"]), (0, [], None, None))

    def test_relevancia_titulo_sobre_etiqueta(self):
        d = self.search("q=zumbador")
        self.assertEqual(d["sort"], "relevance")
        self.assertEqual([p["title"] for p in d["items"]], ["Zumbador sonoro", "Objeto sonoro"])
        self.assertEqual(self.search("q=zumbador&sort=orders")["sort"], "orders")
        self.assertEqual(self.search("")["sort"], "score")
        self.assertEqual(self.search("sort=relevance")["sort"], "score")   # sin texto no hay relevancia

    def test_formato_compatible(self):
        d = self.search("q=lampara&page_size=5")
        p = d["items"][0]
        for k in ("id", "title", "tags", "imported_id", "source", "product_url", "score", "margin", "profit",
                  "suggested_price", "total_cost", "break_even", "trend", "trending"):
            self.assertIn(k, p)
        self.assertIsInstance(p["tags"], list)
        self.assertIsInstance(p["trending"], bool)
        self.assertEqual(d["pages"], -(-d["total"] // 5))

    def test_filtros_sql_metricas(self):
        d = self.search("min_margin=55&min_score=50&trending=1&sort=margin&page_size=100")
        self.assertTrue(d["items"])
        margins = [p["margin"] for p in d["items"]]
        self.assertEqual(margins, sorted(margins, reverse=True))
        for p in d["items"]:
            self.assertGreaterEqual(p["margin"], 55)
            self.assertGreaterEqual(p["score"], 50)
            self.assertTrue(p["trending"])

    def test_autocompletado(self):
        self.search("q=cargador")
        self.search("q=cargador")
        status, d = self.get("GET", "/api/suggest?q=carg&limit=8")
        self.assertEqual(status, 200)
        self.assertLessEqual(len(d["items"]), 8)
        self.assertIn({"text": "cargador", "type": "busqueda"}, d["items"])
        self.assertTrue(any(i["type"] == "producto" and i["text"].startswith("Cargador") for i in d["items"]))
        _, d = self.get("GET", "/api/suggest?q=electr")
        self.assertIn({"text": "Electrónica", "type": "categoria"}, d["items"])
        _, d = self.get("GET", "/api/suggest?q=lamparas")             # también por raíz
        self.assertTrue(any(i["text"].startswith("Lámpara") for i in d["items"]))

    def test_cambio_de_reglas_recalcula(self):
        try:
            status, s = self.get("PUT", "/api/settings", {"multiplier": 3.2})
            self.assertEqual(status, 200)
            _, p = self.get("GET", "/api/products/1")
            m = metrics.compute(p, s)
            for k in ("suggested_price", "profit", "margin", "break_even", "score"):
                self.assertEqual(p[k], m[k], k)
            # El filtro por margen usa la columna recalculada
            d = self.search(f"min_margin={p['margin']}&page_size=100")
            self.assertTrue(all(x["margin"] >= p["margin"] for x in d["items"]))
        finally:
            self.get("PUT", "/api/settings", {"multiplier": metrics.DEFAULT_SETTINGS["multiplier"]})

    def test_csv_actualiza_y_borra_del_indice(self):
        sup = "Prov Indice"
        self.get("POST", "/api/catalog/csv", {"supplier": sup, "csv": "sku;titulo;costo\nX-1;Cosa rarisima;4\n"})
        d = self.search("q=rarisima&source=csv")
        self.assertEqual(d["total"], 1)
        pid = d["items"][0]["id"]
        self.assertAlmostEqual(d["items"][0]["margin"], metrics.enrich(d["items"][0] | {"tags": ""},
                                                                       metrics.DEFAULT_SETTINGS)["margin"])
        # Re-subida con otro título y costo: el índice y las métricas se actualizan
        self.get("POST", "/api/catalog/csv", {"supplier": sup, "csv": "sku;titulo;costo\nX-1;Vasija distinta;8\n"})
        self.assertNotIn(pid, [p["id"] for p in self.search("q=rarisima&source=csv")["items"]])
        d = self.search("q=vasija")
        self.assertEqual([p["id"] for p in d["items"]], [pid])
        self.assertEqual(d["items"][0]["total_cost"], 8)
        # Borrar el catálogo lo saca del índice
        self.get("DELETE", "/api/catalog?source=csv&supplier=" + quote(sup))
        self.assertNotIn(pid, [p["id"] for p in self.search("q=vasija&page_size=100")["items"]])
        with db.connect() as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM products_fts WHERE rowid = ?", (pid,)).fetchone())
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM products").fetchone()[0],
                             conn.execute("SELECT COUNT(*) FROM products_fts").fetchone()[0])

    def test_monitor_recalcula_metricas(self):
        _, imp = self.get("POST", "/api/imports", {"product_id": 5})
        self.get("POST", "/api/monitor/run")
        with db.connect() as conn:
            r = conn.execute("SELECT * FROM products WHERE id = 5").fetchone()
            m = metrics.compute(r, db.get_settings(conn))
            self.assertEqual((r["total_cost"], r["margin"], r["score"]), (m["total_cost"], m["margin"], m["score"]))
        self.get("DELETE", f"/api/imports/{imp['id']}")

    def test_facetas_en_cache_se_invalidan(self):
        _, f1 = self.get("GET", "/api/facets")
        self.get("POST", "/api/catalog/csv", {"supplier": "Prov Facetas", "csv": "sku;titulo;costo\nF-1;Algo;4\n"})
        _, f2 = self.get("GET", "/api/facets")
        self.assertEqual(f2["total"], f1["total"] + 1)
        self.assertIn("Prov Facetas", [s["value"] for s in f2["supplier"]])


V1_PRODUCTS = """CREATE TABLE products (
  id INTEGER PRIMARY KEY, supplier TEXT NOT NULL, supplier_sku TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL, category TEXT NOT NULL, tags TEXT, image TEXT,
  cost REAL NOT NULL, shipping_cost REAL NOT NULL, shipping_days INTEGER NOT NULL,
  warehouse TEXT NOT NULL, stock INTEGER NOT NULL,
  orders INTEGER NOT NULL, orders_7d INTEGER NOT NULL, orders_prev_7d INTEGER NOT NULL,
  rating REAL NOT NULL, reviews INTEGER NOT NULL, updated_at TEXT NOT NULL)"""

V2_PRODUCTS = """CREATE TABLE products (
  id INTEGER PRIMARY KEY, supplier TEXT NOT NULL, supplier_sku TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'mock', title TEXT NOT NULL, category TEXT NOT NULL, tags TEXT, image TEXT,
  product_url TEXT NOT NULL DEFAULT '',
  cost REAL NOT NULL, shipping_cost REAL NOT NULL, shipping_days INTEGER NOT NULL,
  warehouse TEXT NOT NULL, stock INTEGER NOT NULL,
  orders INTEGER NOT NULL, orders_7d INTEGER NOT NULL, orders_prev_7d INTEGER NOT NULL,
  rating REAL NOT NULL, reviews INTEGER NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE (supplier, supplier_sku))"""

OLD_REST = """
CREATE INDEX ix_products_cat ON products(category);
CREATE TABLE imported_products (id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL UNIQUE REFERENCES products(id),
  title TEXT NOT NULL, sell_price REAL NOT NULL, last_cost REAL NOT NULL, last_stock INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'draft', created_at TEXT NOT NULL);
CREATE TABLE alerts (id INTEGER PRIMARY KEY,
  imported_id INTEGER NOT NULL REFERENCES imported_products(id) ON DELETE CASCADE,
  type TEXT NOT NULL, message TEXT NOT NULL, created_at TEXT NOT NULL, read INTEGER NOT NULL DEFAULT 0);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO meta VALUES ('demo_seeded', '1');
INSERT INTO settings VALUES ('multiplier', '3.0');
"""


class MigrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = _TempDb()

    def tearDown(self):
        self.tmp.close()

    def _build(self, version):
        conn = sqlite3.connect(self.tmp.path)
        conn.executescript((V1_PRODUCTS if version == 1 else V2_PRODUCTS) + ";" + OLD_REST)
        cols = "id, supplier, supplier_sku, title, category, tags, image, cost, shipping_cost, shipping_days, " \
               "warehouse, stock, orders, orders_7d, orders_prev_7d, rating, reviews, updated_at"
        rows = [(7, "AliExpress", "AL-1", "Lámparas de escritorio", "Hogar", "hogar,lampara", "", 4.0, 1.0, 10,
                 "CN", 5, 900, 60, 30, 4.6, 10, "2024-01-01"),
                (42, "Amazon", "AM-9", "Cargador rápido USB", "Electrónica", "electrónica", "", 6.0, 0.0, 3,
                 "US", 50, 50, 5, 5, 4.1, 3, "2024-01-01")]
        conn.executemany(f"INSERT INTO products({cols}) VALUES ({','.join('?' * 18)})", rows)
        conn.execute("INSERT INTO imported_products VALUES (3, 42, 'Mi cargador', 19.99, 6.0, 50, 'active', 'x')")
        conn.execute("INSERT INTO alerts VALUES (1, 3, 'price_up', 'subió', 'x', 0)")
        conn.execute(f"PRAGMA user_version = {version}")
        conn.commit()
        conn.close()

    def _check(self):
        db.init_db(seed_size=50)
        db.init_db(seed_size=50)                                  # idempotente
        with db.connect() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM products").fetchone()[0], 2)   # sin re-seed
            self.assertEqual(conn.execute("SELECT product_id FROM imported_products WHERE id = 3").fetchone()[0], 42)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0], 1)
            s = db.get_settings(conn)
            self.assertEqual(s["multiplier"], 3.0)                # se conservan las reglas del usuario
            for r in conn.execute("SELECT * FROM products"):
                m = metrics.compute(r, s)
                for k in metrics.METRIC_COLS:
                    self.assertEqual(r[k], m[k], k)
                self.assertGreater(r["score"], 0)
        d = server.search_products({"q": ["lampara"]})
        self.assertEqual([p["id"] for p in d["items"]], [7])
        d = server.search_products({"q": ["cargadores"]})
        self.assertEqual(d["items"][0]["imported_id"], 3)

    def test_migracion_desde_v2(self):
        self._build(2)
        self._check()

    def test_migracion_desde_v1(self):
        self._build(1)
        self._check()
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT source FROM products WHERE id = 7").fetchone()[0], "mock")


if __name__ == "__main__":
    unittest.main()
