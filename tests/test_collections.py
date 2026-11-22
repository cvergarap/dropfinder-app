"""Pruebas de favoritos, colecciones y exportación CSV (backend/collections_mod.py). Ejecutar:
python3 -m unittest discover tests"""
import csv
import io
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
os.environ.setdefault("DROPFINDER_DB", os.path.join(tempfile.mkdtemp(), "test.db"))
os.environ.setdefault("DROPFINDER_SYNTH_HISTORY", "0")

import auth_context  # noqa: E402
import authtest  # noqa: E402  (sesión del propietario para HTTP)
import collections_mod as cm  # noqa: E402
import currency  # noqa: E402
import db  # noqa: E402
import search  # noqa: E402
import server  # noqa: E402
from errors import ApiError  # noqa: E402

owner = lambda: auth_context.as_user(1, "owner")      # noqa: E731
member = lambda: auth_context.as_user(2, "member")    # noqa: E731

TRICKY_CSV = ("sku;titulo;costo;categoria;costo_envio;stock;pedidos\n"
              "X-1;\"=HYPERLINK(\"\"http://malo\"\";\"\"clic\"\")\";10;Varios;2;5;10\n"
              "X-2;+cmd|' /C calc'!A0;10;Varios;2;5;10\n"
              "X-3;-2+3;10;Varios;2;5;10\n"
              "X-4;\"@SUMA(1;2)\";10;Varios;2;5;10\n"
              "X-5;\"Lámpara \"\"pro\"\", 3 en 1\";10;Varios;2;5;10\n")


class _TempDb:
    """Apunta db.DB_PATH a una base temporal propia y la restaura al terminar."""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="dropfinder-coll-")
        self.path = os.path.join(self.dir, "t.db")
        self.old = db.DB_PATH
        db.DB_PATH = self.path

    def close(self):
        db.DB_PATH = self.old
        search.invalidate()
        shutil.rmtree(self.dir, ignore_errors=True)


def parse_csv(body: bytes, sep=";"):
    text = body.decode("utf-8")
    return list(csv.reader(io.StringIO(text.lstrip("﻿"), newline=""), delimiter=sep))


class CollectionsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = _TempDb()
        db.init_db(seed_size=300)
        with owner():
            server.upload_csv({"supplier": "Prov Trucos", "csv": TRICKY_CSV})
        with db.connect() as conn:
            cls.pids = [r[0] for r in conn.execute("SELECT id FROM products WHERE source = 'mock' ORDER BY id LIMIT 10")]
            cls.tricky = {r["supplier_sku"]: r["id"] for r in conn.execute(
                "SELECT id, supplier_sku FROM products WHERE supplier = 'Prov Trucos'")}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.close()

    def setUp(self):
        with db.connect() as conn:
            cm.ensure_schema(conn)
            conn.execute("DELETE FROM collections")
            conn.execute("DELETE FROM imported_products")
            currency.ensure_schema(conn)
            currency.update_settings(conn, {"rate_mode": "manual", "manual_rate": 4000, "hedge_pct": 0,
                                            "cop_rounding": "900", "display_currency": "USD"})

    def err(self, status, fn, *args):
        with self.assertRaises(ApiError) as ctx:
            fn(*args)
        self.assertEqual(ctx.exception.status, status, str(ctx.exception))
        return ctx.exception

    def route(self, method, path, qs=None, body=None):
        """Llama al handler de la ruta (igual que server.Handler._api, sin HTTP)."""
        for m, pattern, fn in server.ROUTES:
            match = re.fullmatch(pattern, path)
            if m == method and match:
                return fn(match, qs or {}, body if body is not None else {})
        raise AssertionError(f"sin ruta {method} {path}")

    # ------------------------------------------------------------ colecciones
    def test_crud_y_validacion(self):
        with owner():
            c = cm.create_collection({"name": "  Ideas   Navidad ", "description": "Para diciembre", "color": "teal"})
            self.assertEqual((c["name"], c["color"], c["count"]), ("Ideas Navidad", "teal", 0))
            self.assertEqual(cm.create_collection({"name": "Probar en marzo"})["color"], cm.DEFAULT_COLOR)
            self.err(400, cm.create_collection, {"name": ""})
            self.err(400, cm.create_collection, {"name": "   "})
            self.err(400, cm.create_collection, {"name": "x" * 61})
            self.assertEqual(len(cm.create_collection({"name": "y" * 60})["name"]), 60)
            self.err(400, cm.create_collection, {"name": 5})
            self.err(400, cm.create_collection, {"name": "Color", "color": "fucsia"})
            self.err(400, cm.create_collection, {"name": "Desc", "description": "d" * 201})
            self.err(400, cm.create_collection, ["no", "objeto"])
            self.err(409, cm.create_collection, {"name": "ideas navidad"})      # repetido (sin mayúsculas)
            self.err(409, cm.create_collection, {"name": "FAVORITOS"})          # reservado
            u = cm.update_collection(c["id"], {"name": "Navidad 2026", "color": "amber", "description": ""})
            self.assertEqual((u["name"], u["color"], u["description"]), ("Navidad 2026", "amber", ""))
            self.err(409, cm.update_collection, c["id"], {"name": "Probar en marzo"})
            self.assertEqual(cm.update_collection(c["id"], {"name": "navidad 2026"})["name"], "navidad 2026")
            names = [x["name"] for x in cm.list_collections()["items"]]
            self.assertEqual(names[0], "Favoritos")                            # siempre primero
            self.assertIn("navidad 2026", names)
            self.assertEqual(cm.delete_collection(c["id"]), {"ok": True})
            self.err(404, cm.delete_collection, c["id"])
            self.err(404, cm.update_collection, 999999, {"name": "x"})

    def test_favoritos_no_se_borra_ni_renombra(self):
        with owner():
            virtual = cm.list_collections()["items"][0]
            self.assertEqual((virtual["is_favorites"], virtual["virtual"], virtual["id"]), (True, True, None))
            cm.toggle_favorite(self.pids[0])                   # la primera escritura lo crea
            fav = cm.list_collections()["items"][0]
            self.assertEqual((fav["is_favorites"], fav["virtual"], fav["count"]), (True, False, 1))
            self.err(400, cm.delete_collection, fav["id"])
            self.err(400, cm.update_collection, fav["id"], {"name": "Otra"})
            self.assertEqual(cm.update_collection(fav["id"], {"color": "violet"})["color"], "violet")
            self.assertEqual(sum(x["is_favorites"] for x in cm.list_collections()["items"]), 1)

    def test_tope_de_colecciones(self):
        old = cm.MAX_COLLECTIONS
        cm.MAX_COLLECTIONS = 3
        try:
            with owner():
                cm.list_collections()            # Favoritos no cuenta para el tope
                for i in range(3):
                    cm.create_collection({"name": f"C{i}"})
                self.err(409, cm.create_collection, {"name": "C3"})
            with member():                       # el tope es por usuario
                cm.create_collection({"name": "C0"})
        finally:
            cm.MAX_COLLECTIONS = old

    # ------------------------------------------------------------ productos
    def test_items_idempotentes_nota_y_orden(self):
        a, b, c = self.pids[:3]
        with owner():
            col = cm.create_collection({"name": "Probar"})["id"]
            r = cm.add_items(col, {"product_ids": [a, b, 999999]})
            self.assertEqual((r["added"], r["already"], r["missing"], r["count"]), (2, 0, [999999], 2))
            r = cm.add_items(col, {"product_ids": [a, c], "note": "  pedir muestra "})
            self.assertEqual((r["added"], r["already"], r["count"]), (1, 1, 3))
            self.err(404, cm.add_items, col, {"product_ids": [999998]})
            self.err(400, cm.add_items, col, {"product_ids": []})
            self.err(400, cm.add_items, col, {"product_ids": ["x"]})
            self.err(400, cm.add_items, col, {"product_ids": [a], "note": "n" * 501})
            d = cm.list_items(col, {})
            self.assertEqual([p["id"] for p in d["items"]], [c, b, a])        # el más reciente primero
            notes = {p["id"]: p["note"] for p in d["items"]}
            self.assertEqual(notes, {a: "pedir muestra", b: "", c: "pedir muestra"})
            self.assertIn("suggested_price", d["items"][0])                    # formato de server._item
            self.assertIsInstance(d["items"][0]["tags"], list)
            self.assertEqual(cm.update_item(col, b, {"note": "Ganador"})["note"], "Ganador")
            self.err(404, cm.update_item, col, self.pids[5], {"note": "x"})
            self.err(400, cm.update_item, col, b, {})
            self.assertEqual([p["id"] for p in cm.list_items(col, {"sort": ["added_asc"]})["items"]], [a, b, c])
            self.err(400, cm.list_items, col, {"sort": ["DROP TABLE"]})
            pg = cm.list_items(col, {"page_size": ["2"], "page": ["2"]})
            self.assertEqual((pg["total"], pg["pages"], [p["id"] for p in pg["items"]]), (3, 2, [a]))
            self.assertEqual(cm.remove_item(col, b)["count"], 2)
            self.assertFalse(cm.remove_item(col, b)["removed"])
            view = [x for x in cm.list_collections()["items"] if x["id"] == col][0]
            self.assertEqual((view["count"], len(view["preview_images"]), view["preview"][0]["id"]), (2, 2, c))

    def test_tope_de_productos(self):
        old = cm.MAX_ITEMS
        cm.MAX_ITEMS = 4
        try:
            with owner():
                col = cm.create_collection({"name": "Chica"})["id"]
                cm.add_items(col, {"product_ids": self.pids[:3]})
                self.err(409, cm.add_items, col, {"product_ids": self.pids[3:5]})
                cm.add_items(col, {"product_ids": self.pids[:4]})            # los repetidos no cuentan
                self.assertEqual(cm.list_items(col, {})["total"], 4)
        finally:
            cm.MAX_ITEMS = old

    def test_borrar_producto_y_coleccion_en_cascada(self):
        with owner():
            col = cm.create_collection({"name": "Cascada"})["id"]
            # producto propio para poder borrarlo
            server.upload_csv({"supplier": "Prov Borrable", "csv": "sku;titulo;costo\nB-1;Borrable;5\n"})
            with db.connect() as conn:
                pid = conn.execute("SELECT id FROM products WHERE supplier = 'Prov Borrable'").fetchone()[0]
            cm.add_items(col, {"product_ids": [pid, self.pids[0]]})
            cm.toggle_favorite(pid)
            server.delete_supplier_catalog({"source": ["csv"], "supplier": ["Prov Borrable"]})
            self.assertEqual([p["id"] for p in cm.list_items(col, {})["items"]], [self.pids[0]])
            self.assertEqual(cm.favorite_ids({})["total"], 0)
            cm.delete_collection(col)
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM collection_items WHERE collection_id = ?",
                                          (col,)).fetchone()[0], 0)

    # ------------------------------------------------------------ favoritos
    def test_favoritos(self):
        a, b, c = self.pids[:3]
        with owner():
            self.assertEqual(cm.favorite_ids({"ids": [f"{a},{b}"]}), {"ids": [], "total": 0})
            self.assertEqual(cm.toggle_favorite(a), {"product_id": a, "favorite": True, "total": 1})
            self.assertTrue(cm.toggle_favorite(c)["favorite"])
            self.assertEqual(cm.favorite_ids({"ids": [f"{c},{b},{a}"]}), {"ids": [c, a], "total": 2})
            self.assertEqual(cm.toggle_favorite(a), {"product_id": a, "favorite": False, "total": 1})
            self.err(404, cm.toggle_favorite, 999999)
            self.err(400, cm.favorite_ids, {"ids": ["1,x"]})
            self.err(400, cm.favorite_ids, {"ids": [",".join(str(i) for i in range(1, 202))]})
            col = cm.create_collection({"name": "Con nota"})["id"]
            cm.add_items(col, {"product_ids": [c], "note": "probar"})
            pc = cm.product_collections(c)
            self.assertEqual(sorted(pc["collection_ids"]), sorted([col, pc["items"][0]["id"]]))
            mine = {x["id"]: x for x in pc["items"]}
            self.assertEqual((mine[col]["contains"], mine[col]["note"]), (True, "probar"))
            self.assertEqual(cm.product_collections(b)["collection_ids"], [])
            self.err(404, cm.product_collections, 999999)

    # ------------------------------------------------------------ aislamiento por usuario
    def test_aislamiento_entre_usuarios(self):
        a, b = self.pids[:2]
        with owner():
            col = cm.create_collection({"name": "Secreta"})["id"]
            cm.add_items(col, {"product_ids": [a], "note": "mía"})
            cm.toggle_favorite(a)
        with member():
            self.assertEqual([x["name"] for x in cm.list_collections()["items"]], ["Favoritos"])
            self.assertEqual(cm.favorite_ids({"ids": [str(a)]}), {"ids": [], "total": 0})
            self.err(404, cm.list_items, col, {})
            self.err(404, cm.update_collection, col, {"name": "Robada"})
            self.err(404, cm.delete_collection, col)
            self.err(404, cm.add_items, col, {"product_ids": [b]})
            self.err(404, cm.remove_item, col, a)
            self.err(404, cm.update_item, col, a, {"note": "x"})
            self.err(404, cm.export_csv, "collection", {"id": [str(col)]})
            self.assertEqual(cm.product_collections(a)["collection_ids"], [])
            self.assertTrue(cm.toggle_favorite(a)["favorite"])                # sus propios favoritos
            cm.create_collection({"name": "Secreta"})                          # el nombre es por usuario
        with owner():
            d = cm.list_items(col, {})
            self.assertEqual([(p["id"], p["note"]) for p in d["items"]], [(a, "mía")])
            self.assertEqual(cm.favorite_ids({"ids": [str(a)]})["total"], 1)

    def test_sin_usuario_401_sin_modo_desarrollo(self):
        prev = (getattr(auth_context._local, "user_id", None), getattr(auth_context._local, "role", None))
        self.addCleanup(auth_context.set_current, *prev)      # las demás pruebas usan el contexto de authtest
        auth_context.clear()
        e = self.err(401, self.route, "GET", "/api/collections")
        self.assertEqual(str(e), "Inicia sesión")
        self.err(401, self.route, "POST", "/api/collections", None, {"name": ""})    # 401 antes que 400
        self.err(401, self.route, "GET", "/api/export/search.csv")
        # El módulo no tiene modo de desarrollo propio: la variable de entorno no abre nada
        self.assertFalse(hasattr(cm, "_DEV_USER"))
        with mock.patch.dict(os.environ, {"DROPFINDER_DEV_USER": "1"}):
            self.err(401, self.route, "GET", "/api/collections")
            self.err(401, self.route, "POST", "/api/favorites/1")

    # ------------------------------------------------------------ capa de cuentas
    def _setup_two_users(self):
        a, b, c = self.pids[:3]
        with owner():
            cm.toggle_favorite(a)
            cm.toggle_favorite(b)
            fav1 = cm.list_collections()["items"][0]["id"]
            cm.update_item(fav1, b, {"note": "nota vieja"})
            cm.create_collection({"name": "Navidad"})
            m = cm.create_collection({"name": "Marzo"})["id"]
            cm.add_items(m, {"product_ids": [c]})
            cm.create_collection({"name": "L" * 60})
        with auth_context.as_user(5):
            cm.toggle_favorite(b)
            cm.toggle_favorite(c)
            cm.create_collection({"name": "navidad"})
            cm.create_collection({"name": "l" * 60})
        return a, b, c

    def test_reassign_user_fusiona_favoritos_y_renombra(self):
        a, b, c = self._setup_two_users()
        with db.connect() as conn:
            r = cm.reassign_user(conn, 1, 5)
        self.assertEqual(r, {"moved": 3, "merged_favorites": 1, "renamed": 2})
        with owner():
            self.assertTrue(cm.list_collections()["items"][0]["virtual"])   # al usuario 1 no le queda nada
        with auth_context.as_user(5):
            items = cm.list_collections()["items"]
            favs = [x for x in items if x["is_favorites"]]
            self.assertEqual(len(favs), 1)
            names = sorted(x["name"] for x in items if not x["is_favorites"])
            self.assertEqual(names, sorted(["navidad", "Navidad (2)", "Marzo", "l" * 60, "L" * 56 + " (2)"]))
            self.assertTrue(all(len(n) <= cm.MAX_NAME for n in names))
            fav_items = {p["id"]: p["note"] for p in cm.list_items(favs[0]["id"], {})["items"]}
            self.assertEqual(fav_items, {a: "", b: "nota vieja", c: ""})     # sin duplicados; nota conservada
            self.assertEqual(cm.favorite_ids({"ids": [f"{a},{b},{c}"]})["total"], 3)
        with db.connect() as conn:                                            # idempotente / sin datos
            self.assertEqual(cm.reassign_user(conn, 1, 5), {"moved": 0, "merged_favorites": 0, "renamed": 0})
            self.assertEqual(cm.reassign_user(conn, 5, 5)["moved"], 0)

    def test_reassign_user_sin_favoritos_en_destino(self):
        with owner():
            cm.toggle_favorite(self.pids[0])
            cm.create_collection({"name": "Solo"})
        with db.connect() as conn:
            self.assertEqual(cm.reassign_user(conn, 1, 9), {"moved": 2, "merged_favorites": 0, "renamed": 0})
        with auth_context.as_user(9):
            self.assertEqual(cm.favorite_ids({"ids": [str(self.pids[0])]})["ids"], [self.pids[0]])

    def test_delete_user_data_y_count_for_user(self):
        a, b, c = self._setup_two_users()
        with db.connect() as conn:
            self.assertEqual(cm.count_for_user(conn, 1), {"collections": 3, "favorites": 2, "items": 3})
            self.assertEqual(cm.count_for_user(conn, 5), {"collections": 2, "favorites": 2, "items": 2})
            self.assertEqual(cm.count_for_user(conn, 42), {"collections": 0, "favorites": 0, "items": 0})
            self.assertEqual(cm.delete_user_data(conn, 1), 4)
            self.assertEqual(cm.count_for_user(conn, 1), {"collections": 0, "favorites": 0, "items": 0})
            self.assertEqual(cm.count_for_user(conn, 5)["favorites"], 2)        # el otro usuario intacto
            orphan = conn.execute("SELECT COUNT(*) FROM collection_items WHERE collection_id NOT IN "
                                  "(SELECT id FROM collections)").fetchone()[0]
            self.assertEqual(orphan, 0)

    # ------------------------------------------------------------ exportación
    def test_export_formato_excel(self):
        a = self.pids[0]
        with owner():
            col = cm.create_collection({"name": "Exportar"})["id"]
            cm.add_items(col, {"product_ids": [a]})
            cm.update_item(col, a, {"note": "=1+1 nota"})
            f = cm.export_csv("collection", {"id": [str(col)]})
            p = server.get_product(a)
        self.assertEqual(f.content_type, "text/csv; charset=utf-8")
        self.assertTrue(re.fullmatch(r"dropfinder-collection-\d{8}\.csv", f.filename), f.filename)
        self.assertTrue(f.body.startswith(b"\xef\xbb\xbf"))
        text = f.body.decode("utf-8")
        self.assertEqual(text.count("\r\n"), 2)
        self.assertNotIn("\n", text.replace("\r\n", ""))
        head, row = parse_csv(f.body)
        self.assertEqual(head, ["Título", "Proveedor", "SKU", "Categoría", "Costo (USD)", "Envío (USD)",
                                "Costo total (USD)", "Precio sugerido (USD)", "Ganancia (USD)", "Margen %",
                                "Días de envío", "Almacén", "Stock", "Pedidos", "Rating", "Score", "Enlace", "Nota"])
        r = dict(zip(head, row))
        self.assertEqual(r["SKU"], p["supplier_sku"])
        self.assertEqual(r["Costo (USD)"], f"{p['cost']:.2f}".replace(".", ","))
        self.assertEqual(r["Precio sugerido (USD)"], f"{p['suggested_price']:.2f}".replace(".", ","))
        self.assertEqual(r["Margen %"], f"{p['margin']:.1f}".replace(".", ","))
        self.assertEqual(r["Rating"], f"{p['rating']:.1f}".replace(".", ","))
        self.assertEqual(r["Nota"], "'=1+1 nota")
        # Separador coma: punto decimal
        with owner():
            g = cm.export_csv("collection", {"id": [str(col)], "sep": [","]})
            self.err(400, cm.export_csv, "collection", {"id": [str(col)], "sep": ["|"]})
            self.err(400, cm.export_csv, "collection", {"id": [str(col)], "currency": ["EUR"]})
            self.err(400, cm.export_csv, "collection", {})
        head2, row2 = parse_csv(g.body, ",")
        r2 = dict(zip(head2, row2))
        self.assertEqual(r2["Costo (USD)"], f"{p['cost']:.2f}")
        self.assertEqual(r2["Rating"], f"{p['rating']:.1f}")

    def test_export_cop_con_redondeo(self):
        a = self.pids[1]
        with owner():
            col = cm.create_collection({"name": "COP"})["id"]
            cm.add_items(col, {"product_ids": [a]})
            p = server.get_product(a)
            f = cm.export_csv("collection", {"id": [str(col)], "currency": ["COP"]})
        head, row = parse_csv(f.body)
        r = dict(zip(head, row))
        self.assertIn("Costo (COP)", head)
        self.assertEqual(r["Costo (COP)"], str(round(p["cost"] * 4000)))
        self.assertEqual(r["Ganancia (COP)"], str(round(p["profit"] * 4000)))
        price = int(r["Precio sugerido (COP)"])
        self.assertEqual(price % 1000, 900)                               # terminado en 900
        self.assertTrue(p["suggested_price"] * 4000 <= price < p["suggested_price"] * 4000 + 1000)
        self.assertEqual(price, currency.round_price(p["suggested_price"] * 4000, "COP", "900"))
        with db.connect() as conn:                                          # otro modo de redondeo
            currency.update_settings(conn, {"cop_rounding": "1000"})
        with owner():
            f = cm.export_csv("collection", {"id": [str(col)], "currency": ["COP"]})
        self.assertEqual(int(dict(zip(*parse_csv(f.body)))["Precio sugerido (COP)"]) % 1000, 0)

    def test_export_neutraliza_formulas_y_comillas(self):
        with owner():
            col = cm.create_collection({"name": "Trucos"})["id"]
            cm.add_items(col, {"product_ids": list(self.tricky.values())})
            f = cm.export_csv("collection", {"id": [str(col)], "sort": ["title"]})
        rows = parse_csv(f.body)[1:]
        titles = {r[2]: r[0] for r in rows}
        self.assertEqual(titles["X-1"], "'=HYPERLINK(\"http://malo\";\"clic\")")
        self.assertEqual(titles["X-2"], "'+cmd|' /C calc'!A0")
        self.assertEqual(titles["X-3"], "'-2+3")
        self.assertEqual(titles["X-4"], "'@SUMA(1;2)")
        self.assertEqual(titles["X-5"], 'Lámpara "pro", 3 en 1')          # comillas y separador intactos
        for r in rows:
            self.assertFalse(r[0].startswith(tuple("=+-@")), r[0])
            self.assertEqual(len(r), 18)
        self.assertEqual(cm.safe_text("-3,50"), "'-3,50")                  # texto, no número
        self.assertEqual(cm.safe_text("\rx"), "'\rx")
        self.assertEqual(cm.safe_text("normal"), "normal")
        for evil in (" =1+1", "\xa0=1+1", "\u200b=1+1", "\ufeff@SUM(1)", "|cmd", "%0A", "\uff1d1+1", "\n=1"):
            self.assertEqual(cm.safe_text(evil), "'" + evil, repr(evil))
        for ok in ("Lámpara 50% off", "100% algodón", "A|B"):                 # solo cuenta el inicio
            self.assertEqual(cm.safe_text(ok), ok)

    def test_sku_que_excel_alteraria(self):
        for sku in ("0012", "007", "1-2", "3/4", "2024-01-05", "7701234567890", "1E5", "12.50", "12,5"):
            self.assertEqual(cm.safe_code(sku), "'" + sku, sku)
        for sku in ("12345", "ABC-12", "AL-100002", "X1-2", "0"):
            self.assertEqual(cm.safe_code(sku), sku, sku)
        self.assertEqual(cm.safe_code("=1"), "'=1")                            # no doble apóstrofo
        with owner():
            server.upload_csv({"supplier": "Prov Codigos", "csv": "sku;titulo;costo\n0012;Con ceros;5\n1-2;Fecha;5\n"})
            with db.connect() as conn:
                ids = [r[0] for r in conn.execute("SELECT id FROM products WHERE supplier = 'Prov Codigos'")]
            col = cm.create_collection({"name": "Códigos"})["id"]
            cm.add_items(col, {"product_ids": ids})
            rows = parse_csv(cm.export_csv("collection", {"id": [str(col)]}).body)[1:]
        self.assertEqual(sorted(r[2] for r in rows), ["'0012", "'1-2"])

    def test_export_ganancia_negativa_no_se_altera(self):
        f = cm._Formatter({"settings": {}, "rates": {"USD": 1.0}}, "USD", dec_comma=True)
        self.assertEqual(f.amount(-3.5), "-3,50")
        self.assertEqual(f.amount(-0.001), "0,00")

    def test_export_importados(self):
        a, b = self.pids[:2]
        with db.connect() as conn:
            for uid, pid, price, status in [(1, a, 49.0, "active"), (1, b, 1.0, "draft"), (2, b, 30, "draft")]:
                conn.execute("INSERT INTO imported_products(user_id, product_id, title, sell_price, last_cost, "
                             "last_stock, status, created_at) VALUES (?, ?, ?, ?, 0, 0, ?, ?)",
                             (uid, pid, f"Mi título {pid}", price, status, db.now()))
        with owner():
            f = cm.export_csv("imports", {"currency": ["COP"]})
        self.assertTrue(re.fullmatch(r"dropfinder-imports-\d{8}\.csv", f.filename))
        rows = parse_csv(f.body)
        head = rows[0]
        self.assertEqual(head[-2:], ["Precio de venta (COP)", "Estado"])
        data = {r[0]: dict(zip(head, r)) for r in rows[1:]}
        self.assertEqual(set(data), {f"Mi título {a}", f"Mi título {b}"})
        self.assertEqual(data[f"Mi título {a}"]["Estado"], "Activo")
        self.assertEqual(data[f"Mi título {a}"]["Precio de venta (COP)"], "196900")      # 49 × 4000 = 196.000 → 196.900
        self.assertTrue(data[f"Mi título {b}"]["Ganancia (COP)"].startswith("-"))      # vende a pérdida
        with member():
            rows2 = parse_csv(cm.export_csv("imports", {}).body)
        self.assertEqual([r[0] for r in rows2[1:]], [f"Mi título {b}"])

    def test_export_no_registra_busquedas(self):
        def logged():
            with db.connect() as conn:
                return conn.execute("SELECT COUNT(*) FROM search_log").fetchone()[0]
        with db.connect() as conn:
            conn.execute("DELETE FROM search_log")      # la ventana anti-repetición no debe ocultar el registro
        with owner():
            before = logged()
            cm.export_csv("search", {"q": ["lampara"]})
            cm.search_export_items({"q": ["lampara"]})
            self.assertEqual(logged(), before)
            server.search_products({"q": ["lampara"]})                           # una búsqueda real sí
            self.assertEqual(logged(), before + 1)

    def test_export_busqueda_igual_a_la_api(self):
        cases = [{"sort": ["score"]}, {"q": ["lampara"], "in_stock": ["1"]},
                 {"category": ["Mascotas"], "sort": ["price_asc"], "min_margin": ["10"]}]
        for qs in cases:
            with self.subTest(qs=qs), owner():
                api_ids, page = [], 1
                while True:
                    d = server.search_products({**qs, "page": [str(page)], "page_size": ["37"]})
                    api_ids += [p["id"] for p in d["items"]]
                    if page >= d["pages"]:
                        break
                    page += 1
                items, total = cm.search_export_items(qs)
                self.assertEqual(total, len(api_ids))
                self.assertEqual([p["id"] for p in items], api_ids)
                f = cm.export_csv("search", qs)
                self.assertEqual(len(parse_csv(f.body)) - 1, len(api_ids))
                self.assertEqual(f.headers["X-Export-Truncated"], "0")
        with owner():                                     # tope: las primeras N en el mismo orden
            items, total = cm.search_export_items({"sort": ["orders"]}, cap=150)
            d1 = server.search_products({"sort": ["orders"], "page_size": ["100"], "page": ["1"]})
            d2 = server.search_products({"sort": ["orders"], "page_size": ["100"], "page": ["2"]})
        self.assertEqual(len(items), 150)
        self.assertGreater(total, 150)
        self.assertEqual([p["id"] for p in items], [p["id"] for p in d1["items"] + d2["items"]][:150])


class GetSinEscriturasTest(unittest.TestCase):
    """Los GET no escriben: responden aunque otra conexión tenga la base bloqueada (BEGIN IMMEDIATE) y no
    crean tablas ni Favoritos. El esquema se crea una sola vez, en la primera escritura."""

    def setUp(self):
        self.tmp = _TempDb()                 # base nueva: las tablas del módulo aún no existen
        db.init_db(seed_size=60)
        with db.connect() as conn:
            self.pid = conn.execute("SELECT id FROM products LIMIT 1").fetchone()[0]

    def tearDown(self):
        self.tmp.close()

    def _tables(self):
        with db.connect() as conn:
            return cm._has_tables(conn)

    def _gets(self, uid):
        with auth_context.as_user(uid):
            t = time.perf_counter()
            items = cm.list_collections()["items"]
            pc = cm.product_collections(self.pid)
            fi = cm.favorite_ids({"ids": [str(self.pid)]})
            cm.export_csv("imports", {})
            for fn, args in [(cm.list_items, (1, {})), (cm.export_csv, ("collection", {"id": ["1"]}))]:
                with self.assertRaises(ApiError) as ctx:
                    fn(*args)
                self.assertEqual(ctx.exception.status, 404)
            return time.perf_counter() - t, items, pc, fi

    def _locked(self):
        w = sqlite3.connect(db.DB_PATH, isolation_level=None)
        w.execute("BEGIN IMMEDIATE")
        w.execute("UPDATE settings SET value = value")
        return w

    def test_get_sin_tablas_con_base_bloqueada(self):
        w = self._locked()
        try:
            took, items, pc, fi = self._gets(77)
        finally:
            w.execute("ROLLBACK")
            w.close()
        self.assertLess(took, 1.0)
        self.assertEqual([(x["name"], x["virtual"], x["id"]) for x in items], [("Favoritos", True, None)])
        self.assertEqual((pc["collection_ids"], pc["items"][0]["virtual"]), ([], True))
        self.assertEqual(fi, {"ids": [], "total": 0})
        self.assertFalse(self._tables())                       # ningún GET creó las tablas

    def test_get_de_usuario_sin_favoritos_con_base_bloqueada(self):
        with auth_context.as_user(1):
            cm.create_collection({"name": "Existe"})            # tablas creadas por una escritura
        w = self._locked()
        try:
            took, items, pc, _ = self._gets(78)                 # usuario sin Favoritos: antes hacía INSERT
        finally:
            w.execute("ROLLBACK")
            w.close()
        self.assertLess(took, 1.0)
        self.assertTrue(items[0]["virtual"])
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM collections WHERE user_id = 78").fetchone()[0], 0)

    def test_esquema_una_vez_por_proceso(self):
        calls = []
        real = cm.ensure_schema
        with mock.patch.object(cm, "ensure_schema", side_effect=lambda conn: (calls.append(1), real(conn))):
            with auth_context.as_user(1):
                cm.list_collections()
                self.assertEqual(calls, [])                     # un GET no crea el esquema
                cm.create_collection({"name": "Uno"})
                cm.toggle_favorite(self.pid)
                cm.create_collection({"name": "Dos"})
        self.assertEqual(len(calls), 1)
        self.assertTrue(self._tables())

    def test_helpers_sin_tablas(self):
        with db.connect() as conn:
            self.assertEqual(cm.count_for_user(conn, 1), {"collections": 0, "favorites": 0, "items": 0})
            self.assertEqual(cm.delete_user_data(conn, 1), 0)
            self.assertEqual(cm.reassign_user(conn, 1, 2), {"moved": 0, "merged_favorites": 0, "renamed": 0})
        self.assertFalse(self._tables())


class CollectionsHttpTest(unittest.TestCase):
    """Cabeceras de la descarga sobre HTTP real, con la sesión del propietario (tests/authtest.py)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = _TempDb()
        db.init_db(seed_size=120)
        cls.srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.tmp.close()

    def call(self, method, path, body=None):
        req = urllib.request.Request(self.base + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json", **authtest.auth_headers()})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, dict(e.headers), e.read()

    def test_descarga_csv(self):
        st, _, body = self.call("POST", "/api/collections", {"name": "HTTP"})
        self.assertEqual(st, 200, body)
        cid = json.loads(body)["id"]
        st, _, body = self.call("POST", f"/api/collections/{cid}/items", {"product_ids": [1, 2]})
        self.assertEqual(json.loads(body)["added"], 2)
        st, h, body = self.call("POST", "/api/favorites/1")
        self.assertEqual(json.loads(body)["favorite"], True)
        for path, kind in [(f"/api/export/collection.csv?id={cid}", "collection"),
                           ("/api/export/search.csv?q=lampara&sep=%2C&currency=COP", "search"),
                           ("/api/export/imports.csv", "imports")]:
            st, h, body = self.call("GET", path)
            self.assertEqual(st, 200, body)
            self.assertEqual(h["Content-Type"], "text/csv; charset=utf-8")
            self.assertRegex(h["Content-Disposition"], rf'^attachment; filename="dropfinder-{kind}-\d{{8}}\.csv"$')
            self.assertEqual(int(h["Content-Length"]), len(body))
            self.assertTrue(body.startswith(b"\xef\xbb\xbf"))
            self.assertTrue(body.endswith(b"\r\n"))
            self.assertIn("default-src 'self'", h["Content-Security-Policy"])       # cabeceras de seguridad
            self.assertEqual(h["X-Content-Type-Options"], "nosniff")
        st, h, body = self.call("GET", "/api/export/collection.csv?id=999999")
        self.assertEqual((st, json.loads(body)["error"]), (404, "Colección no encontrada"))
        st, _, _ = self.call("GET", "/api/export/otra.csv")
        self.assertEqual(st, 404)


if __name__ == "__main__":
    unittest.main()
