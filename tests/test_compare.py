"""Pruebas del comparador y de la agrupación del mismo producto entre proveedores (products.match_key).
Ejecutar:  python3 -m unittest discover tests"""
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
os.environ.setdefault("DROPFINDER_DB", os.path.join(tempfile.mkdtemp(), "test.db"))

import compare  # noqa: E402
import authtest  # noqa: E402,F401  (contexto y sesión del propietario)
import db  # noqa: E402
import hooks  # noqa: E402
import search  # noqa: E402
import server  # noqa: E402

mk = compare.match_key


def call(base, method, path, body=None):
    req = urllib.request.Request(f"{base}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", **authtest.auth_headers()})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        with e:
            return e.code, json.load(e)


class _TempDb:
    """Apunta db.DB_PATH a una base temporal propia y la restaura al terminar."""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="dropfinder-cmp-")
        self.path = os.path.join(self.dir, "t.db")
        self.old = db.DB_PATH
        db.DB_PATH = self.path

    def close(self):
        db.DB_PATH = self.old
        search.invalidate()
        shutil.rmtree(self.dir, ignore_errors=True)


class MatchKeyTest(unittest.TestCase):
    C = "Electrónica"

    def same(self, a, b, cat=None):
        self.assertEqual(mk(a, cat or self.C), mk(b, cat or self.C), (a, b))

    def differ(self, a, b, cat=None):
        self.assertNotEqual(mk(a, cat or self.C), mk(b, cat or self.C), (a, b))

    def test_tildes_mayusculas_plurales_y_orden(self):
        self.same("Lámparas LED plegable", "lampara plegable led", "Hogar y cocina")
        self.same("Lámparas LED plegable", "LAMPARA  Plegables LED", "HOGAR Y COCINA")
        self.same("Cargador magnético", "cargadores magneticos")
        self.same("Luces LED", "luz led")
        self.same("Wi-Fi extensor", "wifi extensor")

    def test_palabras_vacias_y_relleno(self):
        self.same("Cepillo para mascotas", "cepillo mascotas", "Mascotas")
        self.same("Organizador de bambú", "Organizador bambú NUEVO original", "Hogar")
        self.same("Reloj minimalista", "Reloj minimalista envío gratis oferta", "Moda")

    def test_numeros_y_medidas(self):
        self.same("Memoria USB 128GB", "Memoria USB 128 GB")
        self.same("Botella 1.5 L", "Botella 1,5 L")
        self.same("Botella 1.5 L 1 unidad", "Botella 1,5 L")
        self.same("Funda 10 pulgadas", "Funda 10 in")
        for a, b in [("Memoria USB 128GB", "Memoria USB 256GB"), ("Set de 3 tazas", "Set de 5 tazas"),
                     ("Pack 2 x 3 m", "Pack 3 x 2 m"), ("Marco 20 x 30 cm", "Marco 30 x 20 cm"),
                     ("Botella 1.5 L", "Botella 5 L 1 unidad"), ("Adaptador de 2 a 1", "Adaptador 1 a 2"),
                     ("Bombilla E27 LED", "Bombilla E14 LED"), ("Hub USB 3.0 de 4 puertos", "Hub USB 4.0 de 3 puertos"),
                     ("Cargador 2.4A doble", "Cargador 4.2A doble"), ("Agenda 2025 semanal", "Agenda 2026 semanal"),
                     ("Mini proyector 4K", "Mini proyector HD")]:
            self.differ(a, b)

    def test_letras_sueltas_y_codigos(self):
        self.differ("Cable USB-C a USB-C 1m", "Cable USB-A a USB-C 1m")
        self.differ("Cable USB-C a USB-C 1m", "Cable USB-C 1m")
        self.differ("Aceite vitamina E 30ml", "Aceite vitamina 30ml")
        self.differ("Camiseta talla M", "Camiseta talla L")

    def test_genero_y_palabras_que_identifican(self):
        self.differ("Gorro de lana negro", "Gorra de lana negra")
        self.differ("Puerta magnética", "Puerto magnético")
        self.differ("Ventilador PC RGB", "Ventilador RGB")
        self.differ("Top deportivo mujer", "Deportivo mujer")

    def test_categoria(self):
        self.assertNotEqual(mk("Lámpara LED", "Hogar y cocina"), mk("Lámpara LED", "Bebés y niños"))
        # Primera palabra significativa: el mismo producto con categorías escritas distinto se agrupa
        self.assertEqual(mk("Lámpara LED", "Hogar"), mk("Lámpara LED", "Hogar y jardín"))
        self.assertNotEqual(mk("Lámpara LED", "Hogar"), mk("Lámpara LED plegable", "Hogar"))
        self.assertNotEqual(mk("Cepillo para mascotas", "Mascotas"), mk("Comedero automático", "Mascotas"))

    def test_nunca_vacia(self):
        self.assertTrue(mk("Nuevo original", "Hogar").startswith("hogar|nuevo original"))
        self.assertNotEqual(mk("", ""), "")


class CompareApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = _TempDb()
        db.init_db(seed_size=300)
        cls.srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        csv_a = ("sku;titulo;costo;costo_envio;dias_envio;categoria;rating;pedidos;stock\n"
                 "A-1;Lámparas LED plegable;10;0;12;Iluminación;4.2;100;50\n"
                 "A-2;Taza térmica;4;1;10;Cocina;4.0;10;5\n")
        csv_b = ("sku;titulo;costo;costo_envio;dias_envio;categoria;rating;pedidos;stock\n"
                 "B-1;lampara plegable led NUEVO;8;1;20;iluminacion;4.8;300;0\n"
                 "B-2;Lámpara LED;3;0;2;Iluminación;4.1;10;10\n")
        csv_c = ("sku;titulo;costo;costo_envio;dias_envio;categoria;rating;pedidos;stock\n"
                 "C-1;Lámpara plegable LED;15;0;3;Iluminación;4.5;50;80\n")
        for sup, csv in (("Prov A", csv_a), ("Prov B", csv_b), ("Prov C", csv_c)):
            status, r = cls.get("POST", "/api/catalog/csv", {"supplier": sup, "csv": csv})
            assert status == 200, r
        with db.connect() as conn:
            cls.ids = {r["supplier_sku"]: r["id"] for r in
                       conn.execute("SELECT id, supplier_sku FROM products WHERE source = 'csv'")}

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.tmp.close()

    @classmethod
    def get(cls, method, path, body=None):
        return call(cls.base, method, path, body)

    def ok(self, path):
        status, d = self.get("GET", path)
        self.assertEqual(status, 200, d)
        return d

    # ---- hook y relleno

    def test_hook_en_carga_y_demo(self):
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM products WHERE match_key = ''").fetchone()[0], 0)
            keys = {r[0] for r in conn.execute("SELECT match_key FROM products WHERE supplier_sku IN ('A-1','B-1','C-1')")}
            self.assertEqual(len(keys), 1)
            # La demo genera títulos idénticos en varios proveedores: deben agruparse
            n = conn.execute("SELECT COUNT(*) FROM (SELECT match_key FROM products WHERE source = 'mock' "
                             "GROUP BY match_key HAVING COUNT(DISTINCT supplier) > 1)").fetchone()[0]
            self.assertGreater(n, 0)

    def test_hook_recalcula_al_cambiar_titulo(self):
        csv = "sku;titulo;costo;categoria\nX-1;Botella térmica;5;Cocina\n"
        self.assertEqual(self.get("POST", "/api/catalog/csv", {"supplier": "Prov X", "csv": csv})[0], 200)
        with db.connect() as conn:
            k1 = conn.execute("SELECT match_key FROM products WHERE supplier_sku = 'X-1'").fetchone()[0]
        csv = "sku;titulo;costo;categoria\nX-1;Taza térmica;5;Cocina\n"
        self.assertEqual(self.get("POST", "/api/catalog/csv", {"supplier": "Prov X", "csv": csv})[0], 200)
        with db.connect() as conn:
            k2 = conn.execute("SELECT match_key FROM products WHERE supplier_sku = 'X-1'").fetchone()[0]
            k_taza = conn.execute("SELECT match_key FROM products WHERE supplier_sku = 'A-2'").fetchone()[0]
        self.assertNotEqual(k1, k2)
        self.assertEqual(k2, k_taza)
        self.get("DELETE", "/api/catalog?source=csv&supplier=" + "Prov%20X")

    def test_hook_del_monitor_con_ids(self):
        with db.connect() as conn:
            pid = self.ids["A-2"]
            conn.execute("UPDATE products SET match_key = 'viejo' WHERE id = ?", (pid,))
            compare.update_keys(conn, [pid])
            self.assertNotEqual(conn.execute("SELECT match_key FROM products WHERE id = ?", (pid,)).fetchone()[0],
                                "viejo")
        # El monitor real llama al hook con la lista de ids cambiados
        mock_id = self.ok("/api/products?source=mock&page_size=1")["items"][0]["id"]
        self.assertEqual(self.get("POST", "/api/imports", {"product_id": mock_id})[0], 200)
        with db.connect() as conn:
            conn.execute("UPDATE products SET match_key = 'x' WHERE id = ?", (mock_id,))
        self.assertEqual(self.get("POST", "/api/monitor/run")[0], 200)
        with db.connect() as conn:
            r = conn.execute("SELECT title, category, match_key FROM products WHERE id = ?", (mock_id,)).fetchone()
        self.assertEqual(r["match_key"], mk(r["title"], r["category"]))

    def test_hook_con_muchos_ids(self):
        with db.connect() as conn:
            ids = [r[0] for r in conn.execute("SELECT id FROM products")]
            conn.execute("UPDATE products SET match_key = 'x'")
            compare.update_keys(conn, ids + list(range(10**6, 10**6 + 1000)))     # > 900: camino json_each
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM products WHERE match_key = 'x'").fetchone()[0], 0)

    def test_version_de_la_clave(self):
        with db.connect() as conn:
            conn.execute("UPDATE products SET match_key = 'clave vieja'")
            conn.execute("UPDATE meta SET value = 'vieja' WHERE key = ?", (compare._META_KEY,))
        self.ok(f"/api/alternatives/counts?ids={self.ids['A-1']}")       # relleno perezoso
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM products WHERE match_key IN ('', 'clave vieja')")
                             .fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT value FROM meta WHERE key = ?", (compare._META_KEY,)).fetchone()[0],
                             compare.MATCH_KEY_VERSION)
            # También desde el hook (dentro de la transacción de una carga)
            conn.execute("UPDATE products SET match_key = 'clave vieja'")
            conn.execute("UPDATE meta SET value = 'vieja' WHERE key = ?", (compare._META_KEY,))
            hooks.run_catalog_write(conn, [self.ids["A-2"]])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM products WHERE match_key IN ('', 'clave vieja')")
                             .fetchone()[0], 0)
        self.assertEqual(self.ok(f"/api/alternatives/counts?ids={self.ids['A-1']}")["counts"][str(self.ids["A-1"])], 3)

    def test_grupo_grande_recortado(self):
        csv = "sku;titulo;costo;categoria\n" + "".join(f"G-{i};Taza grande;{1 + i % 97};Cocina\n" for i in range(120))
        self.assertEqual(self.get("POST", "/api/catalog/csv", {"supplier": "Prov G", "csv": csv})[0], 200)
        with db.connect() as conn:
            pid = conn.execute("SELECT id FROM products WHERE supplier_sku = 'G-96'").fetchone()[0]   # el más caro
        d = self.ok(f"/api/products/{pid}/alternatives")
        self.assertEqual(d["total"], 120)
        ids = [p["id"] for p in d["items"]]
        self.assertIn(pid, ids)                                    # el propio producto aunque sea caro
        self.assertLessEqual(len(ids), compare.MAX_ALTERNATIVES + 5)
        self.assertEqual([p["total_cost"] for p in d["items"]], sorted(p["total_cost"] for p in d["items"]))
        self.assertEqual(d["items"][0]["id"], d["best"]["cheapest"])
        c = self.ok(f"/api/alternatives/counts?ids={pid},{d['items'][0]['id']}")
        self.assertEqual(set(c["counts"].values()), {120})
        self.assertEqual(set(c["suppliers"].values()), {1})
        self.get("DELETE", "/api/catalog?source=csv&supplier=Prov%20G")

    def test_relleno_perezoso(self):
        with db.connect() as conn:
            conn.execute("UPDATE products SET match_key = '' WHERE source = 'mock'")
        d = self.ok(f"/api/alternatives/counts?ids={self.ids['A-1']}")
        self.assertEqual(d["counts"][str(self.ids["A-1"])], 3)
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM products WHERE match_key = ''").fetchone()[0], 0)

    # ---- alternativas

    def test_alternativas_orden_y_mejores(self):
        a1, b1, c1 = self.ids["A-1"], self.ids["B-1"], self.ids["C-1"]
        d = self.ok(f"/api/products/{a1}/alternatives")
        self.assertEqual(d["product_id"], a1)
        self.assertTrue(d["match_key"])
        ids = [p["id"] for p in d["items"]]
        self.assertEqual(ids, [b1, a1, c1])            # por costo total: 9, 10, 15
        self.assertEqual([p["total_cost"] for p in d["items"]], sorted(p["total_cost"] for p in d["items"]))
        self.assertEqual(d["best"], {"cheapest": b1, "fastest": c1, "best_margin": c1, "best_rating": b1})
        self.assertIn("imported_id", d["items"][0])
        self.assertIsInstance(d["items"][0]["tags"], list)
        # La lámpara sin "plegable" es otro producto
        self.assertNotIn(self.ids["B-2"], ids)
        solo = self.ok(f"/api/products/{self.ids['B-2']}/alternatives")
        self.assertEqual([p["id"] for p in solo["items"]], [self.ids["B-2"]])

    def test_alternativas_404(self):
        self.assertEqual(self.get("GET", "/api/products/99999999/alternatives")[0], 404)

    def test_conteos(self):
        a1, b2 = self.ids["A-1"], self.ids["B-2"]
        d = self.ok(f"/api/alternatives/counts?ids={a1},{b2},99999999")
        self.assertEqual(d["counts"], {str(a1): 3, str(b2): 1})
        self.assertEqual(d["suppliers"], {str(a1): 3, str(b2): 1})
        self.assertEqual(self.get("GET", "/api/alternatives/counts?ids=" + ",".join(map(str, range(1, 102))))[0], 400)
        self.assertEqual(self.get("GET", "/api/alternatives/counts?ids=1,abc")[0], 400)
        self.assertEqual(self.get("GET", "/api/alternatives/counts")[0], 400)

    # ---- comparador

    def test_comparar(self):
        a1, b1, c1 = self.ids["A-1"], self.ids["B-1"], self.ids["C-1"]
        d = self.ok(f"/api/compare?ids={a1},{b1},{c1},88888888")
        self.assertEqual([p["id"] for p in d["items"]], [a1, b1, c1])     # en el orden pedido
        self.assertEqual(d["missing"], [88888888])
        b = d["best"]
        self.assertEqual(b["total_cost"], b1)
        self.assertEqual(b["suggested_price"], b1)
        self.assertEqual(b["shipping_days"], c1)
        self.assertEqual(b["rating"], b1)
        self.assertEqual(b["orders"], b1)
        self.assertEqual(b["stock"], c1)
        self.assertEqual(b["profit"], c1)         # el más caro deja más ganancia absoluta
        for f in ("margin", "trend", "score"):
            self.assertIn(f, b)

    def test_comparar_validacion(self):
        a1 = self.ids["A-1"]
        self.assertEqual(self.get("GET", f"/api/compare?ids={a1}")[0], 400)
        self.assertEqual(self.get("GET", f"/api/compare?ids={a1},{a1}")[0], 400)      # repetidos cuentan una vez
        self.assertEqual(self.get("GET", "/api/compare?ids=1,2,3,4,5")[0], 400)
        self.assertEqual(self.get("GET", "/api/compare")[0], 400)
        self.assertEqual(self.get("GET", "/api/compare?ids=1,x")[0], 400)
        d = self.ok("/api/compare?ids=77777777,88888888")
        self.assertEqual(d["items"], [])
        self.assertEqual(d["missing"], [77777777, 88888888])

    def test_empates_sin_ganador(self):
        a1 = self.ids["A-1"]
        d = self.ok(f"/api/compare?ids={a1},99999998")
        self.assertTrue(all(v is None for v in d["best"].values()))   # un solo producto: nada que destacar

    def test_zz_borrados_desaparecen(self):
        csv = "sku;titulo;costo;categoria\nD-1;Lámpara plegable LED;1;Iluminación\n"
        self.assertEqual(self.get("POST", "/api/catalog/csv", {"supplier": "Prov D", "csv": csv})[0], 200)
        a1 = self.ids["A-1"]
        self.assertEqual(self.ok(f"/api/alternatives/counts?ids={a1}")["counts"][str(a1)], 4)
        with db.connect() as conn:
            d1 = conn.execute("SELECT id FROM products WHERE supplier_sku = 'D-1'").fetchone()[0]
        self.assertEqual(self.ok(f"/api/products/{a1}/alternatives")["items"][0]["id"], d1)
        self.assertEqual(self.get("DELETE", "/api/catalog?source=csv&supplier=Prov%20D")[1]["deleted"], 1)
        self.assertEqual(self.ok(f"/api/alternatives/counts?ids={a1}")["counts"][str(a1)], 3)
        self.assertNotIn(d1, [p["id"] for p in self.ok(f"/api/products/{a1}/alternatives")["items"]])
        self.assertEqual(self.ok(f"/api/compare?ids={a1},{d1}")["missing"], [d1])
        self.assertEqual(self.get("GET", f"/api/products/{d1}/alternatives")[0], 404)


class ServerAsScriptTest(unittest.TestCase):
    def test_usa_main_si_el_servidor_corre_como_script(self):
        """Con `python3 server.py` el módulo del servidor es __main__: compare debe usar ese (sin re-importar)."""
        main = sys.modules["__main__"]
        saved = sys.modules.pop("server")
        had = {k: getattr(main, k) for k in ("_item", "Handler") if hasattr(main, k)}
        try:
            main._item, main.Handler = saved._item, saved.Handler
            self.assertIs(compare._server(), main)
        finally:
            sys.modules["server"] = saved
            for k in ("_item", "Handler"):
                if k in had:
                    setattr(main, k, had[k])
                else:
                    delattr(main, k)
        self.assertIs(compare.ApiError, server.ApiError)


if __name__ == "__main__":
    unittest.main()
