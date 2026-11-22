"""Pruebas de las búsquedas guardadas con alertas (backend/saved_searches.py). Ejecutar:
python3 -m unittest discover tests"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
os.environ.setdefault("DROPFINDER_DB", os.path.join(tempfile.mkdtemp(), "test.db"))

import db  # noqa: E402
import saved_searches as ss  # noqa: E402
import search  # noqa: E402
import server  # noqa: E402

CAT = "Prueba Guardadas"
PARAMS = {"category": [CAT], "min_margin": 40, "max_ship_days": 6, "in_stock": "1", "sort": "score"}


def csv_rows(rows):
    """rows: [(sku, titulo, costo, categoria, dias_envio, stock)] → texto CSV."""
    lines = ["sku;titulo;costo;categoria;dias_envio;stock;pedidos"]
    lines += [f"{s};{t};{c};{cat};{d};{st};100" for s, t, c, cat, d, st in rows]
    return "\n".join(lines) + "\n"


class _TempDb:
    """Apunta db.DB_PATH a una base temporal propia y la restaura al terminar."""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="dropfinder-ss-")
        self.path = os.path.join(self.dir, "t.db")
        self.old = db.DB_PATH
        db.DB_PATH = self.path

    def close(self):
        db.DB_PATH = self.old
        search.invalidate()
        shutil.rmtree(self.dir, ignore_errors=True)


class SavedSearchesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.old_bg = ss.BACKGROUND
        ss.BACKGROUND = False          # revisiones deterministas; el hilo se prueba aparte
        cls.tmp = _TempDb()
        db.init_db(seed_size=300)
        cls.srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.seq = 0

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        ss.BACKGROUND = cls.old_bg
        cls.tmp.close()

    def setUp(self):
        with db.connect() as conn:
            conn.execute("DELETE FROM saved_searches")
            conn.execute("DELETE FROM alerts")
            conn.execute("DELETE FROM meta WHERE key = ? OR key LIKE ?", (ss.PENDING_KEY, ss.SEEN_PREFIX + "%"))

    # ------------------------------------------------------------ utilidades
    def call(self, method, path, body=None):
        req = urllib.request.Request(f"{self.base}{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.load(e)

    def upload(self, rows, supplier=None):
        type(self).seq += 1
        supplier = supplier or f"Prov SS {self.seq}"
        return server.upload_csv({"supplier": supplier, "csv": csv_rows(rows)})

    def sku(self):
        type(self).seq += 1
        return f"SS-{self.seq}"

    def alerts_for(self, sid):
        with db.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM alerts WHERE saved_search_id = ? ORDER BY id", (sid,))]

    def create(self, **kw):
        body = {"name": "Mascotas rentables", "params": PARAMS}
        body.update(kw)
        return ss.create(body)

    # ------------------------------------------------------------ creación y validación
    def test_crear_normaliza_y_resume(self):
        it = self.create(name="", params={**PARAMS, "page": "3", "page_size": "50", "exact": "1", "otra": "x",
                                          "q": "  cama   ortopédica "})
        self.assertEqual(it["params"], {"q": "cama ortopédica", "category": [CAT], "min_margin": 40,
                                        "max_ship_days": 6, "in_stock": 1, "sort": "score"})
        self.assertEqual(it["summary"], f"«cama ortopédica» · {CAT} · margen ≥ 40 % · envío ≤ 6 días · con stock")
        # nombre por defecto = resumen; si no cabe en 80 caracteres se cortan criterios completos
        self.assertEqual(it["name"], f"«cama ortopédica» · {CAT} · margen ≥ 40 % · envío ≤ 6 días …")
        self.assertEqual(ss.create({"name": None, "params": {"min_score": 70}})["name"], "score ≥ 70")
        self.assertTrue(it["notify"])
        for k in ("id", "created_at", "last_checked_at", "total", "new", "chips"):
            self.assertIn(k, it)
        _, lst = self.call("GET", "/api/saved-searches")
        self.assertIn(it["id"], [x["id"] for x in lst["items"]])
        self.assertEqual(lst["items"][0]["name"], "score ≥ 70")     # más recientes primero

    def test_validaciones(self):
        for body, status in [
            ({"name": "x" * 81, "params": PARAMS}, 400),
            ({"name": "ok", "params": {"min_margin": "abc"}}, 400),
            ({"name": "ok", "params": {"max_ship_days": "2.5"}}, 400),
            ({"name": "ok", "params": {"min_cost": -1}}, 400),
            ({"name": "ok", "params": "q=hola"}, 400),
            ({"name": "ok", "params": {}, "notify": "sí"}, 400),
        ]:
            st, r = self.call("POST", "/api/saved-searches", body)
            self.assertEqual(st, status, body)
            self.assertIn("error", r)
        st, r = self.call("POST", "/api/saved-searches", {"name": "A", "params": PARAMS})
        self.assertEqual(st, 200)
        st, r = self.call("POST", "/api/saved-searches", {"name": "B", "params": dict(PARAMS)})
        self.assertEqual(st, 409)                           # mismos criterios
        self.assertIn("«A»", r["error"])

    def test_limite_de_50(self):
        for i in range(ss.MAX_SEARCHES):
            ss.create({"name": f"B{i}", "params": {"min_orders": i}, "notify": False})
        st, r = self.call("POST", "/api/saved-searches", {"name": "otra", "params": {"min_orders": 999}})
        self.assertEqual(st, 409)
        self.assertIn("50", r["error"])

    def test_preview(self):
        st, r = self.call("GET", "/api/saved-searches/preview?q=cargador&category=Electr%C3%B3nica&min_margin=45"
                                 "&max_ship_days=5&page=2")
        self.assertEqual(st, 200)
        self.assertEqual(r["summary"], "«cargador» · Electrónica · margen ≥ 45 % · envío ≤ 5 días")
        self.assertNotIn("page", r["params"])
        self.assertIsInstance(r["total"], int)

    def test_crear_marca_como_vistas_las_coincidencias_actuales(self):
        self.upload([(self.sku(), "Cama ortopédica previa", 3, CAT, 4, 10),
                     (self.sku(), "Rascador previo", 3.5, CAT, 3, 5)])
        it = self.create()
        self.assertGreaterEqual(it["total"], 2)
        self.assertEqual(it["new"], 0)
        with db.connect() as conn:
            hits = conn.execute("SELECT COUNT(*) FROM saved_search_hits WHERE saved_search_id = ?",
                                (it["id"],)).fetchone()[0]
        self.assertEqual(hits, it["total"])
        self.assertEqual(ss.check_all()["alerts"], [])

    def test_tope_de_new_ids(self):
        it = self.create()
        self.upload([(self.sku(), f"Tope {i}", 3, CAT, 4, 10) for i in range(4)])
        old = ss.MAX_IDS
        ss.MAX_IDS = 2
        try:
            with db.connect() as conn:
                ids = ss.new_ids(conn, it["id"], it["params"])
                self.assertEqual(len(ids), 2)
                self.assertEqual(ids, sorted(ids, reverse=True))      # los más recientes
            _, lst = self.call("GET", "/api/saved-searches")
            self.assertEqual(lst["items"][0]["new"], 4)                # el contador no se recorta
        finally:
            ss.MAX_IDS = old

    # ------------------------------------------------------------ revisión y alertas
    def test_producto_nuevo_genera_una_alerta(self):
        it = self.create()
        st, r = self.call("POST", "/api/catalog/csv", {"supplier": "Prov HTTP", "csv": csv_rows(
            [(self.sku(), "Cama ortopédica silenciosa", 3, CAT, 4, 10)])})
        self.assertEqual(st, 200)
        with db.connect() as conn:          # el hook dejó la revisión pendiente (confirmada con la carga)
            self.assertIsNotNone(db._meta(conn, ss.PENDING_KEY))
        self.assertTrue(ss.run_pending())
        with db.connect() as conn:
            self.assertIsNone(db._meta(conn, ss.PENDING_KEY))
            pid = conn.execute("SELECT id FROM products WHERE title = 'Cama ortopédica silenciosa'").fetchone()[0]
        alerts = self.alerts_for(it["id"])
        self.assertEqual(len(alerts), 1)
        a = alerts[0]
        self.assertEqual((a["type"], a["product_id"], a["saved_search_id"]), ("saved_search_new", pid, it["id"]))
        self.assertIsNone(a["imported_id"])
        self.assertRegex(a["message"], r"^Nuevo producto para «Mascotas rentables»: Cama ortopédica silenciosa "
                                       r"\(margen \d+ %, envío 4 días\)$")
        self.assertEqual(ss.check_all()["alerts"], [])   # no se repite
        _, lst = self.call("GET", "/api/saved-searches")
        self.assertEqual(lst["items"][0]["new"], 1)
        self.assertIsNotNone(lst["items"][0]["last_checked_at"])

    def test_producto_que_no_cumple_no_genera_alerta(self):
        it = self.create()
        self.upload([(self.sku(), "Otra categoría", 3, "Otra cosa", 4, 10),
                     (self.sku(), "Envío lento", 3, CAT, 20, 10),
                     (self.sku(), "Sin stock", 3, CAT, 4, 0)])
        r = ss.check_all()
        self.assertEqual(r["checked"], 1)
        self.assertEqual(r["alerts"], [])
        self.assertEqual(self.alerts_for(it["id"]), [])

    def test_aviso_apagado_no_genera_alerta(self):
        it = self.create(notify=False)
        self.upload([(self.sku(), "Cama sin aviso", 3, CAT, 4, 10)])
        # La búsqueda sin aviso también registra sus coincidencias en la revisión, pero sin alertas
        self.assertEqual(ss.check_all(), {"checked": 0, "alerts": []})
        self.assertEqual(self.alerts_for(it["id"]), [])
        _, lst = self.call("GET", "/api/saved-searches")
        self.assertEqual(lst["items"][0]["new"], 1)       # igual se ve como nuevo en la pestaña
        # Al reactivar el aviso no se avisa de lo que llegó mientras estaba apagado
        st, upd = self.call("PATCH", f"/api/saved-searches/{it['id']}", {"notify": True})
        self.assertEqual(st, 200)
        self.assertTrue(upd["notify"])
        self.assertEqual(upd["new"], 1)
        self.assertEqual(ss.check_all()["alerts"], [])

    def test_maximo_5_alertas_mas_resumen(self):
        it = self.create()
        self.upload([(self.sku(), f"Producto lote {i}", 3, CAT, 4, 10) for i in range(8)])
        r = ss.check_all()
        alerts = self.alerts_for(it["id"])
        self.assertEqual(len(r["alerts"]), 6)
        self.assertEqual(len(alerts), 6)
        self.assertEqual(sum(1 for a in alerts if a["product_id"]), 5)
        summary = [a for a in alerts if a["product_id"] is None]
        self.assertEqual(len(summary), 1)
        self.assertIn("y 3 productos nuevos más para «Mascotas rentables»", summary[0]["message"])
        with db.connect() as conn:
            self.assertEqual(len(ss.new_ids(conn, it["id"], it["params"])), 8)

    def test_list_alerts_muestra_titulo(self):
        it = self.create()
        self.upload([(self.sku(), f"Título visible {i}", 3, CAT, 4, 10) for i in range(6)])
        ss.check_all()
        st, rows = self.call("GET", "/api/alerts")
        self.assertEqual(st, 200)
        mine = [a for a in rows if a["saved_search_id"] == it["id"]]
        self.assertEqual(len(mine), 6)
        titles = {a["title"] for a in mine}
        self.assertIn("Mascotas rentables", titles)       # la de resumen lleva el nombre de la búsqueda
        self.assertTrue(any(t.startswith("Título visible") for t in titles))
        self.assertTrue(all(a["saved_search_name"] == "Mascotas rentables" for a in mine))

    # ------------------------------------------------------------ resultados, vistos, edición, borrado
    def test_resultados_y_vistos(self):
        it = self.create()
        self.upload([(self.sku(), "Fuente de agua nueva", 3, CAT, 4, 10)])
        ss.check_all()
        st, r = self.call("GET", f"/api/saved-searches/{it['id']}/results?page=1")
        self.assertEqual(st, 200)
        with db.connect() as conn:
            pid = conn.execute("SELECT id FROM products WHERE title = 'Fuente de agua nueva'").fetchone()[0]
        self.assertEqual(r["new_ids"], [pid])
        self.assertEqual(r["total"], it["total"] + 1)
        self.assertIn(pid, [p["id"] for p in r["items"]])
        for k in ("page", "pages", "items", "sort"):
            self.assertIn(k, r)
        st, v = self.call("POST", f"/api/saved-searches/{it['id']}/seen")
        self.assertEqual(st, 200)
        self.assertEqual(v["new"], 0)
        self.assertTrue(all(a["read"] for a in self.alerts_for(it["id"])))
        _, r = self.call("GET", f"/api/saved-searches/{it['id']}/results")
        self.assertEqual(r["new_ids"], [])
        # Un producto que llega después de verla vuelve a ser nuevo
        self.upload([(self.sku(), "Fuente de agua posterior", 3, CAT, 4, 10)])
        ss.check_all()
        _, lst = self.call("GET", "/api/saved-searches")
        self.assertEqual(lst["items"][0]["new"], 1)

    def test_renombrar(self):
        it = self.create()
        st, r = self.call("PATCH", f"/api/saved-searches/{it['id']}", {"name": "  Nuevo   nombre "})
        self.assertEqual((st, r["name"]), (200, "Nuevo nombre"))
        st, _ = self.call("PATCH", f"/api/saved-searches/{it['id']}", {"name": ""})
        self.assertEqual(st, 400)
        st, _ = self.call("PATCH", "/api/saved-searches/99999", {"name": "x"})
        self.assertEqual(st, 404)

    def test_borrar_en_cascada(self):
        it = self.create()
        self.upload([(self.sku(), "Para borrar", 3, CAT, 4, 10)])
        ss.check_all()
        self.assertEqual(len(self.alerts_for(it["id"])), 1)
        st, _ = self.call("DELETE", f"/api/saved-searches/{it['id']}")
        self.assertEqual(st, 200)
        with db.connect() as conn:
            for sql in ("SELECT COUNT(*) FROM saved_search_hits WHERE saved_search_id = ?",
                        "SELECT COUNT(*) FROM alerts WHERE saved_search_id = ?",
                        "SELECT COUNT(*) FROM saved_searches WHERE id = ?"):
                self.assertEqual(conn.execute(sql, (it["id"],)).fetchone()[0], 0, sql)
            self.assertIsNone(db._meta(conn, f"{ss.SEEN_PREFIX}{it['id']}"))
        st, _ = self.call("DELETE", f"/api/saved-searches/{it['id']}")
        self.assertEqual(st, 404)

    def test_revisar_ahora_por_api(self):
        it = self.create()
        self.upload([(self.sku(), "Revisión manual", 3, CAT, 4, 10)])
        st, r = self.call("POST", "/api/saved-searches/check")
        self.assertEqual(st, 200)
        self.assertEqual(r["checked"], 1)
        self.assertEqual([a["saved_search_id"] for a in r["alerts"]], [it["id"]])

    # ------------------------------------------------------------ regresiones de la revisión del PR #3
    def test_reemplazo_de_csv_no_genera_falsos_nuevos(self):
        """Hallazgo 1: con la ventana deslizante de los MAX_IDS ids más altos, al desaparecer los productos más
        recientes entraban productos antiguos nunca registrados y se alertaban como nuevos."""
        old = ss.MAX_IDS
        ss.MAX_IDS = 5
        try:
            prov = "Prov Reemplazo"
            self.upload([(f"RZ-{i}", f"Antiguo {i}", 3, CAT, 4, 10) for i in range(10)], supplier=prov)
            it = self.create(name="Reemplazo")
            self.assertEqual(it["new"], 0)
            server.upload_csv({"supplier": prov, "replace": True,
                               "csv": csv_rows([("RZ-0", "Antiguo 0", 3, CAT, 4, 10)])})
            self.assertEqual(ss.check_all()["alerts"], [])
            self.assertEqual(self.alerts_for(it["id"]), [])
            _, lst = self.call("GET", "/api/saved-searches")
            self.assertEqual(lst["items"][0]["new"], 0)
            with db.connect() as conn:
                self.assertEqual(ss.new_ids(conn, it["id"], it["params"]), [])
        finally:
            ss.MAX_IDS = old

    def test_actualizar_productos_que_ya_cumplian_no_alerta_y_uno_que_empieza_a_cumplir_si(self):
        prov = "Prov Actualiza"
        self.upload([("UP-1", "Ya cumplía", 3, CAT, 4, 10), ("UP-2", "Sin stock antes", 3, CAT, 4, 0)], supplier=prov)
        it = self.create(name="Actualiza")
        # Se vuelve a subir: UP-1 sigue cumpliendo (no es nuevo); UP-2 vuelve a tener stock (sí es nuevo)
        self.upload([("UP-1", "Ya cumplía", 3, CAT, 4, 12), ("UP-2", "Sin stock antes", 3, CAT, 4, 5)], supplier=prov)
        alerts = ss.check_all()["alerts"]
        self.assertEqual([a["title"] for a in alerts], ["Sin stock antes"])

    def test_creaciones_concurrentes_respetan_tope_y_duplicados(self):
        """Hallazgo 2: el tope de 50 y los duplicados se comprobaban fuera de la transacción de escritura."""
        orig = ss._sync_fts

        def slow(conn):                     # agranda la ventana entre comprobar e insertar
            orig(conn)
            time.sleep(0.2)
        ss._sync_fts = slow
        try:
            def run(body, out):
                try:
                    out.append(ss.create(body)["id"])
                except Exception as e:  # noqa: BLE001
                    out.append(e)
            out = []
            ts = [threading.Thread(target=run, args=({"name": f"D{i}", "params": {"min_orders": 7}}, out))
                  for i in range(3)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            self.assertEqual(sum(1 for x in out if isinstance(x, int)), 1, out)
            self.assertTrue(all(getattr(x, "status", None) == 409 for x in out if not isinstance(x, int)))
            for i in range(ss.MAX_SEARCHES - 2):
                orig_create = {"name": f"C{i}", "params": {"min_orders": 100 + i}, "notify": False}
                ss._sync_fts = orig
                ss.create(orig_create)
            ss._sync_fts = slow
            out = []
            ts = [threading.Thread(target=run, args=({"name": f"E{i}", "params": {"min_score": i}}, out))
                  for i in range(3)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            with db.connect() as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM saved_searches").fetchone()[0], ss.MAX_SEARCHES)
        finally:
            ss._sync_fts = orig

    def test_borrar_durante_la_revision_no_la_rompe(self):
        """Hallazgo 3: si se borraba una búsqueda entre la lectura de la lista y su INSERT en hits, fallaba la
        FK y se perdía toda la revisión (500)."""
        a = self.create(name="A", params={**PARAMS, "q": "cargador"})
        b = self.create(name="B")
        self.upload([(self.sku(), "Cargador para mascotas", 3, CAT, 4, 10)])
        orig = ss._record_hits
        state = {"t": None, "err": []}

        def deleter():
            try:
                ss.delete(b["id"])
            except Exception as e:  # noqa: BLE001
                state["err"].append(e)

        def patched(*args, **kw):
            if state["t"] is None:          # el usuario borra B mientras se revisa
                state["t"] = threading.Thread(target=deleter)
                state["t"].start()
                state["t"].join(1.0)        # sin bloqueo de escritura el borrado terminaría aquí
            return orig(*args, **kw)
        ss._record_hits = patched
        try:
            r = ss.check_all()
        finally:
            ss._record_hits = orig
            if state["t"]:
                state["t"].join()
        self.assertEqual(r["checked"], 2)
        self.assertEqual(len(self.alerts_for(a["id"])), 1)
        self.assertEqual(state["err"], [])
        with db.connect() as conn:          # el borrado se aplicó después de la revisión
            self.assertIsNone(conn.execute("SELECT 1 FROM saved_searches WHERE id = ?", (b["id"],)).fetchone())

    def test_numeros_enormes_dan_400(self):
        """Hallazgo 4: min_orders=1e30 daba OverflowError al enlazarlo en SQLite (500)."""
        for params in ({"min_orders": 1e30}, {"min_orders": 10 ** 30}, {"max_cost": 1e300},
                       {"min_score": "9" * 40}, {"max_ship_days": 2 ** 63}):
            st, r = self.call("POST", "/api/saved-searches", {"name": "x", "params": params})
            self.assertEqual(st, 400, params)
            self.assertIn("error", r)
        st, _ = self.call("GET", "/api/saved-searches/preview?min_orders=1000000000000000000000000000000")
        self.assertEqual(st, 400)
        st, _ = self.call("POST", "/api/saved-searches", {"name": "ok", "params": {"min_orders": 2 ** 53}})
        self.assertEqual(st, 200)

    # ------------------------------------------------------------ hilo en segundo plano
    def test_hilo_revisa_tras_el_commit(self):
        it = self.create()
        ss.BACKGROUND = True
        try:
            with db.connect() as conn:      # escritura real: el hook corre dentro de esta transacción
                from connectors import CsvSupplierConnector
                parsed = CsvSupplierConnector("Prov Hilo", csv_rows(
                    [(self.sku(), "Producto detectado por el hilo", 3, CAT, 4, 10)])).result
                db.upsert_products(conn, parsed.products, "csv")
                time.sleep(0.4)             # sin commit el hilo no ve la marca ni el producto
                self.assertEqual(self.alerts_for(it["id"]), [])
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not self.alerts_for(it["id"]):
                time.sleep(0.05)
            alerts = self.alerts_for(it["id"])
            self.assertEqual(len(alerts), 1)
            self.assertIn("Producto detectado por el hilo", alerts[0]["message"])
            while time.monotonic() < deadline:      # y limpia la marca de pendiente
                with db.connect() as conn:
                    if db._meta(conn, ss.PENDING_KEY) is None:
                        break
                time.sleep(0.05)
            with db.connect() as conn:
                self.assertIsNone(db._meta(conn, ss.PENDING_KEY))
        finally:
            ss.BACKGROUND = False


if __name__ == "__main__":
    unittest.main()
