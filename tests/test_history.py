"""Pruebas del historial diario, rankings, estacionalidad y estadísticas de proveedores (backend/history.py).
Ejecutar:  python3 -m unittest discover tests"""
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import unittest.mock
import urllib.error
import urllib.request
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
os.environ.setdefault("DROPFINDER_DB", os.path.join(tempfile.mkdtemp(), "test.db"))

import db  # noqa: E402
import history  # noqa: E402
import search  # noqa: E402
import server  # noqa: E402
from connectors.base import NormalizedProduct  # noqa: E402

D0 = date(2026, 11, 21)          # "hoy" fijo para las pruebas (un sábado)


class _Env:
    """Base temporal propia + fecha fija; restaura todo al terminar."""

    def __init__(self, day=D0):
        self.dir = tempfile.mkdtemp(prefix="dropfinder-hist-")
        self.old_path, self.old_today = db.DB_PATH, history.today
        self.old_flags = history.BACKGROUND, history.SYNTH_ENABLED
        history.BACKGROUND = False           # mantenimiento determinista: las pruebas llaman run_maintenance()
        history.SYNTH_ENABLED = True         # estas pruebas sí usan el historial sintético de la demo
        db.DB_PATH = os.path.join(self.dir, "t.db")
        self.set_day(day)
        search.invalidate()

    def set_day(self, day):
        history.today = lambda: day
        search.invalidate()

    def close(self):
        db.DB_PATH, history.today = self.old_path, self.old_today
        history.BACKGROUND, history.SYNTH_ENABLED = self.old_flags
        history._maint_state.update(day=None, path=None)
        search.invalidate()
        shutil.rmtree(self.dir, ignore_errors=True)


def prod(sku, supplier="Prov", category="Varios", cost=10.0, orders=100, o7=7, p7=7, stock=50, **kw):
    return NormalizedProduct(supplier=supplier, supplier_sku=sku, title=f"Producto {sku}", category=category,
                             cost=cost, shipping_cost=kw.get("ship", 1.0), shipping_days=kw.get("days", 5),
                             warehouse="CO", stock=stock, orders=orders, orders_7d=o7, orders_prev_7d=p7,
                             rating=kw.get("rating", 4.5), reviews=10)


def upsert(products, source="csv"):
    with db.connect() as conn:
        db.upsert_products(conn, products, source)
        return {r["supplier_sku"]: r["id"] for r in conn.execute("SELECT id, supplier_sku FROM products")}


def put_series(pid, daily, end=D0, cost=None, start_cum=0):
    """Reemplaza el historial de un producto por una serie diaria (lista de pedidos por día, el último = end)."""
    rows, cum = [], start_cum
    n = len(daily)
    for i, v in enumerate(daily):
        cum += v
        d = end - timedelta(days=n - 1 - i)
        c = cost[i] if cost else 10.0
        rows.append((pid, d.isoformat(), cum, v, c, 1.0, 50))
    with db.connect() as conn:
        conn.execute("DELETE FROM product_snapshots WHERE product_id = ?", (pid,))
        conn.executemany("INSERT INTO product_snapshots VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
        conn.execute("UPDATE products SET orders = ? WHERE id = ?", (cum, pid))
    search.invalidate()
    return cum


def rows_of(pid):
    with db.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM product_snapshots WHERE product_id = ? ORDER BY date",
                                              (pid,))]


class SnapshotTest(unittest.TestCase):
    def setUp(self):
        self.env = _Env()
        db.init_db(seed_size=0)

    def tearDown(self):
        self.env.close()

    def test_snapshot_en_upsert_y_orders_day(self):
        ids = upsert([prod("A", orders=100, o7=14), prod("B", orders=5, o7=70)])
        a, b = rows_of(ids["A"]), rows_of(ids["B"])
        self.assertEqual([(r["date"], r["orders"], r["orders_day"]) for r in a], [(D0.isoformat(), 100, 2)])
        self.assertEqual(b[0]["orders_day"], 5)                     # la estimación no supera el acumulado
        # Día siguiente: pedidos del día = diferencia de acumulados
        self.env.set_day(D0 + timedelta(days=1))
        upsert([prod("A", orders=130, cost=12), prod("B", orders=3)])
        a, b = rows_of(ids["A"]), rows_of(ids["B"])
        self.assertEqual((a[-1]["orders"], a[-1]["orders_day"], a[-1]["cost"]), (130, 30, 12))
        self.assertEqual(b[-1]["orders_day"], 0)                    # nunca negativo
        # Segunda carga el mismo día: se actualiza la fila del día (sin duplicar)
        upsert([prod("A", orders=140)])
        a = rows_of(ids["A"])
        self.assertEqual(len(a), 2)
        self.assertEqual(a[-1]["orders_day"], 40)

    def test_snapshot_del_monitor(self):
        ids = upsert([prod("M", orders=50)], source="mock")
        pid = ids["M"]
        status = server.create_import({"product_id": pid, "sell_price": 99})
        self.assertTrue(status["id"])
        self.env.set_day(D0 + timedelta(days=1))
        with db.connect() as conn:
            conn.execute("UPDATE products SET orders = 60 WHERE id = ?", (pid,))
        server.run_monitor()
        last = rows_of(pid)[-1]
        with db.connect() as conn:
            p = conn.execute("SELECT cost, stock FROM products WHERE id = ?", (pid,)).fetchone()
        self.assertEqual(last["date"], (D0 + timedelta(days=1)).isoformat())
        self.assertEqual((last["cost"], last["stock"], last["orders"], last["orders_day"]), (p["cost"], p["stock"], 60, 10))

    def test_borrar_productos_borra_historial(self):
        ids = upsert([prod("X"), prod("Y")])
        with db.connect() as conn:
            conn.execute("INSERT INTO history_seasonality VALUES (?, 12, 2.5)", (ids["X"],))
        put_series(ids["X"], [1] * 30)
        server.delete_supplier_catalog({"source": ["csv"], "supplier": ["Prov"]})
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM product_snapshots").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM history_seasonality").fetchone()[0], 0)

    def test_compactacion(self):
        pid = upsert([prod("C")])["C"]
        daily = [(i % 5) + 1 for i in range(300)]
        total = put_series(pid, daily)
        with db.connect() as conn:
            conn.execute("DELETE FROM history_meta WHERE key = 'compacted_until'")
            deleted = history.compact(conn)
            self.assertEqual(history.compact(conn), 0)               # incremental: nada nuevo que compactar
        rows = rows_of(pid)
        cutoff = D0 - timedelta(days=history.DAILY_DAYS - 1)
        self.assertGreater(deleted, 100)
        for r in rows[1:]:
            d = date.fromisoformat(r["date"])
            self.assertTrue(d >= cutoff or d.weekday() == 0, r["date"])
        self.assertEqual(sum(1 for r in rows if r["date"] >= cutoff.isoformat()), history.DAILY_DAYS)
        self.assertEqual(rows[-1]["orders"], total)
        self.assertEqual(rows[0]["orders"] - rows[0]["orders_day"] + sum(r["orders_day"] for r in rows), total)
        # orders_day de una fila semanal = pedidos desde la fila anterior que queda
        for a, b in zip(rows, rows[1:]):
            self.assertEqual(b["orders_day"], b["orders"] - a["orders"])


class SyntheticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = _Env()
        db.init_db(seed_size=300)
        r = history.run_maintenance()
        assert r["done"] and r["generated"] == 300, r

    @classmethod
    def tearDownClass(cls):
        cls.env.close()

    def test_coherencia_con_el_producto(self):
        with db.connect() as conn:
            prods = conn.execute("SELECT * FROM products").fetchall()
        shapes = set()
        for p in prods:
            h = history.synth_history(p, D0)
            shapes.add(h["shape"])
            self.assertEqual(len(h["daily"]), history.SYNTH_DAYS)
            self.assertEqual(h["base"] + sum(h["daily"]), p["orders"])          # acumulado = pedidos actuales
            self.assertEqual(sum(h["daily"][-7:]), p["orders_7d"])
            self.assertEqual(sum(h["daily"][-14:-7]), p["orders_prev_7d"])
            self.assertGreaterEqual(h["base"], 0)
            self.assertEqual(h["cost"][-1], p["cost"])
            self.assertEqual(h["stock"][-1], p["stock"])
            self.assertEqual(h, history.synth_history(p, D0))                    # determinista
        self.assertTrue({"steady", "rising", "falling", "new"} <= shapes, shapes)

    def test_filas_guardadas(self):
        with db.connect() as conn:
            p = conn.execute("SELECT * FROM products WHERE orders > 1000 LIMIT 1").fetchone()
        rows = rows_of(p["id"])
        cutoff = (D0 - timedelta(days=history.DAILY_DAYS - 1)).isoformat()
        self.assertEqual(rows[-1]["date"], D0.isoformat())
        self.assertEqual(rows[-1]["orders"], p["orders"])
        self.assertEqual(sum(1 for r in rows if r["date"] >= cutoff), history.DAILY_DAYS)
        self.assertTrue(all(date.fromisoformat(r["date"]).weekday() == 0 for r in rows if r["date"] < cutoff))
        last7 = rows[-1]["orders"] - rows[-8]["orders"]
        self.assertEqual(last7, p["orders_7d"])

    def test_estacionalidad_de_la_demo(self):
        with db.connect() as conn:
            got = conn.execute("SELECT p.category, h.month FROM history_seasonality h "
                               "JOIN products p ON p.id = h.product_id").fetchall()
        self.assertTrue(got)
        allowed = {"Bebés y niños": {11, 12, 1}, "Hogar y cocina": {11, 12, 1}, "Fitness": {12, 1, 2},
                   "Moda y accesorios": {4, 5, 6}, "Electrónica": {10, 11, 12, 1}}
        for cat, m in got:
            self.assertIn(m, allowed.get(cat, set()), (cat, m))

    def test_no_se_genera_con_demo_grande(self):
        old = history.SYNTH_MAX_PRODUCTS
        history.SYNTH_MAX_PRODUCTS = 10
        try:
            with db.connect() as conn:
                conn.execute("DELETE FROM product_snapshots WHERE product_id = 1")
                self.assertEqual(history.generate_demo_history(conn, [1]), 0)
                conn.rollback()
        finally:
            history.SYNTH_MAX_PRODUCTS = old


class ApiHistoryTest(unittest.TestCase):
    """Datos armados a mano para comprobar rankings, estacionalidad, historial y proveedores."""

    @classmethod
    def setUpClass(cls):
        cls.env = _Env()
        db.init_db(seed_size=0)
        ids = upsert([prod("STEADY"), prod("RISE"), prod("NEW"), prod("FALL"), prod("SEASON", category="Hogar"),
                      prod("OTHER", supplier="Prov2", cost=5, stock=0, days=20, rating=3.5)])
        cls.ids = ids
        n = 730
        put_series(ids["STEADY"], [10] * n)
        put_series(ids["RISE"], [6] * (n - 7) + [20] * 7)        # 42 → 140: sube, pero no es "nuevo"
        put_series(ids["NEW"], [0] * (n - 5) + [30] * 5, start_cum=0)
        put_series(ids["FALL"], [30] * (n - 7) + [5] * 7)
        season = []
        for i in range(n):
            d = D0 - timedelta(days=n - 1 - i)
            season.append(20 if d.month == 12 else 2)
        put_series(ids["SEASON"], season)
        costs = [10.0] * (n - 10) + [12.0] * 10                      # cambio de costo hace 10 días
        put_series(ids["OTHER"], [1] * n, cost=costs)
        with db.connect() as conn:
            history.refresh_seasonality(conn)
        cls.srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.env.close()

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.load(e)

    def ranking(self, t, days=7, extra=""):
        status, d = self.get(f"/api/rankings?type={t}&days={days}{extra}")
        self.assertEqual(status, 200, d)
        self.assertEqual((d["type"], d["days"]), (t, days))
        return d["items"]

    def test_top(self):
        items = self.ranking("top")
        order = [i["id"] for i in items]
        # 7 días: NEW 150, RISE 140, STEADY 70, FALL 35, SEASON 14, OTHER 7
        self.assertEqual(order[:4], [self.ids["NEW"], self.ids["RISE"], self.ids["STEADY"], self.ids["FALL"]])
        it = items[2]
        self.assertEqual((it["period_orders"], it["prev_period_orders"], it["change_pct"]), (70, 70, 0.0))
        self.assertEqual(it["sparkline"], [10] * 14)
        for k in ("title", "category", "supplier", "warehouse", "image", "source", "score", "total_cost",
                  "suggested_price", "margin", "orders"):
            self.assertIn(k, it)

    def test_rising(self):
        items = self.ranking("rising")
        self.assertEqual(items[0]["id"], self.ids["RISE"])
        self.assertEqual(items[0]["change_pct"], 233.3)
        self.assertNotIn(self.ids["NEW"], [i["id"] for i in items])   # sin volumen previo no "crece"
        self.assertNotIn(self.ids["STEADY"], [i["id"] for i in items])

    def test_new(self):
        items = self.ranking("new")
        self.assertEqual([i["id"] for i in items], [self.ids["NEW"]])
        self.assertEqual(items[0]["period_orders"], 150)
        self.assertEqual(self.ranking("new", 30)[0]["id"], self.ids["NEW"])

    def test_falling(self):
        items = self.ranking("falling")
        self.assertEqual(items[0]["id"], self.ids["FALL"])
        self.assertLess(items[0]["change_pct"], -80)
        self.assertNotIn(self.ids["RISE"], [i["id"] for i in items])

    def test_filtros_y_errores(self):
        items = self.ranking("top", 30, "&category=Hogar")
        self.assertEqual([i["id"] for i in items], [self.ids["SEASON"]])
        self.assertEqual(len(self.ranking("top", 90, "&limit=2")), 2)
        self.assertEqual(self.get("/api/rankings?type=xx")[0], 400)
        self.assertEqual(self.get("/api/rankings?days=8")[0], 400)

    def test_estacionalidad(self):
        status, d = self.get("/api/seasonal?month=12")
        self.assertEqual(status, 200)
        self.assertEqual([i["id"] for i in d["items"]], [self.ids["SEASON"]])
        self.assertGreater(d["items"][0]["strength"], 1.8)
        self.assertEqual(d["items"][0]["label"], "Sube cada año en diciembre")
        self.assertEqual(self.get("/api/seasonal?month=6")[1]["items"], [])
        status, d = self.get("/api/seasonal")                    # por defecto: el mes siguiente (diciembre)
        self.assertEqual(d["month"], 12)
        self.assertEqual(self.get("/api/seasonal?month=13")[0], 400)

    def test_deteccion_requiere_ambos_anios(self):
        # Pico solo el último diciembre: no es estacional
        ser = []
        for i in range(730):
            d = D0 - timedelta(days=729 - i)
            ser.append(20 if (d.month == 12 and d.year == 2025) else 2)
        rows, cum = [], 0
        for i, v in enumerate(ser):
            cum += v
            rows.append({"date": (D0 - timedelta(days=729 - i)).isoformat(), "orders": cum, "orders_day": v})
        self.assertEqual(history.season_from_series(history._Series(rows), D0), {})

    def test_historial_producto(self):
        pid = self.ids["RISE"]
        status, d = self.get(f"/api/products/{pid}/history?days=30")
        self.assertEqual(status, 200, d)
        self.assertEqual((d["product_id"], d["days"], d["resolution"], len(d["points"])), (pid, 30, "day", 30))
        self.assertEqual(d["points"][-1], {"date": D0.isoformat(), "orders": 20, "stock": 50, "cost": 10.0,
                                           "total_cost": 11.0})
        self.assertEqual(d["summary"]["orders_period"], 23 * 6 + 7 * 20)
        self.assertEqual(d["summary"]["orders_prev_period"], 180)
        self.assertEqual(d["summary"]["peak_date"], (D0 - timedelta(days=6)).isoformat())
        self.assertIsNone(d["seasonality"])
        status, d = self.get(f"/api/products/{pid}/history?days=365")
        self.assertEqual((d["resolution"], len(d["points"])), ("week", 52))
        self.assertEqual(d["points"][-1]["orders"], 140)
        self.assertEqual(len(self.get(f"/api/products/{pid}/history?days=90")[1]["points"]), 90)
        s = self.get(f"/api/products/{self.ids['SEASON']}/history?days=365")[1]["seasonality"]
        self.assertEqual((s["peak_month"], s["label"]), (12, "Sube cada año en diciembre"))
        self.assertEqual(self.get("/api/products/999999/history")[0], 404)
        self.assertEqual(self.get(f"/api/products/{pid}/history?days=7")[0], 400)

    def test_proveedores(self):
        status, d = self.get("/api/suppliers/stats")
        self.assertEqual(status, 200)
        by = {i["supplier"]: i for i in d["items"]}
        self.assertEqual(by["Prov"]["products"], 5)
        self.assertEqual(by["Prov"]["price_changes_30d"], 0.0)
        self.assertEqual(by["Prov2"]["price_changes_30d"], 100.0)
        self.assertEqual(by["Prov2"]["pct_out_of_stock"], 100.0)
        self.assertEqual(by["Prov2"]["avg_shipping_days"], 20.0)
        self.assertGreater(by["Prov"]["reliability"], by["Prov2"]["reliability"])
        self.assertEqual(d["items"][0]["supplier"], "Prov")
        self.assertEqual(history.reliability(2, 0, 5, 0), 100)
        self.assertEqual(history.reliability(25, 100, 3, 100), 0)


def put_rows(pid, rows):
    """Historial a mano: rows = [(fecha, acumulado, pedidos_desde_la_anterior, costo)]."""
    with db.connect() as conn:
        conn.execute("DELETE FROM product_snapshots WHERE product_id = ?", (pid,))
        conn.executemany("INSERT INTO product_snapshots VALUES (?, ?, ?, ?, ?, 1.0, 50)",
                         [(pid, d.isoformat(), c, od, cost) for d, c, od, cost in rows])
    search.invalidate()


class ClockAdvanceTest(unittest.TestCase):
    """Regresión 🔴: con el paso de los días solo algunos productos reciben fotos nuevas. Cada producto se
    mide hasta su última foto; los no observados no "caen a cero" ni distorsionan nada."""

    def setUp(self):
        self.env = _Env()
        db.init_db(seed_size=0)
        self.ids = upsert([prod("STEADY"), prod("SEASON", category="Hogar"), prod("FRESH")])
        n = 730
        put_series(self.ids["STEADY"], [10] * n)                  # última foto: D0
        season = [20 if (D0 - timedelta(days=n - 1 - i)).month == 12 else 2 for i in range(n)]
        put_series(self.ids["SEASON"], season)                     # última foto: D0

    def tearDown(self):
        self.env.close()

    def advance(self, days):
        """Pasan `days` días y solo FRESH se actualiza (un CSV de un producto)."""
        self.env.set_day(D0 + timedelta(days=days))
        upsert([prod("FRESH", orders=100 + days)])

    def test_rankings_miden_cada_producto_hasta_su_ultima_foto(self):
        self.advance(3)
        top = history.rankings({"type": ["top"], "days": ["7"]})
        self.assertEqual(top["until"], (D0 + timedelta(days=3)).isoformat())
        by = {i["id"]: i for i in top["items"]}
        steady = by[self.ids["STEADY"]]
        self.assertEqual((steady["period_orders"], steady["prev_period_orders"]), (70, 70))
        self.assertEqual(steady["last_observed"], D0.isoformat())
        self.assertEqual(steady["sparkline"], [10] * 14)
        falling = history.rankings({"type": ["falling"], "days": ["7"]})["items"]
        self.assertNotIn(self.ids["STEADY"], [i["id"] for i in falling])          # no "cayó a 0"
        # Sin fotos dentro de la ventana: fuera de los rankings
        self.advance(10)
        for t in ("top", "rising", "falling", "new"):
            items = history.rankings({"type": [t], "days": ["7"]})["items"]
            self.assertNotIn(self.ids["STEADY"], [i["id"] for i in items], t)

    def test_historial_con_hueco_sin_datos(self):
        self.advance(3)
        h = history.product_history(self.ids["STEADY"], {"days": ["30"]})
        self.assertEqual(h["last_observed"], D0.isoformat())
        self.assertEqual(h["until"], (D0 + timedelta(days=3)).isoformat())
        self.assertEqual([p["orders"] for p in h["points"][-3:]], [None, None, None])
        self.assertEqual(h["points"][-4]["orders"], 10)
        self.assertEqual(h["summary"]["orders_period"], 300)                    # 30 días hasta su última foto
        self.assertEqual(h["summary"]["change_pct"], 0.0)

    def test_estacionalidad_anclada_en_el_producto(self):
        self.advance(30)
        with db.connect() as conn:
            history.refresh_seasonality(conn, [self.ids["SEASON"]])
        d = history.seasonal({"month": ["12"]})
        self.assertEqual([i["id"] for i in d["items"]], [self.ids["SEASON"]])
        self.assertEqual(history.season_from_series(
            history._Series(rows_of(self.ids["SEASON"])), D0 + timedelta(days=120)), {12: unittest.mock.ANY})

    def test_proveedores_seguidos_y_cambio_en_el_borde(self):
        ids = upsert([prod("A", supplier="Prov3"), prod("B", supplier="Prov3")])
        # A: el costo cambió entre la foto anterior a la ventana (D0−40) y la primera dentro (D0−5)
        put_rows(ids["A"], [(D0 - timedelta(days=40), 10, 10, 10.0), (D0 - timedelta(days=5), 20, 10, 12.0),
                            (D0, 30, 10, 12.0)])
        # B: una sola foto (recién cargado): no cuenta como "seguido"
        put_rows(ids["B"], [(D0, 5, 5, 3.0)])
        st = {i["supplier"]: i for i in history.supplier_stats()["items"]}
        self.assertEqual((st["Prov3"]["tracked_products"], st["Prov3"]["price_changes_30d"]), (1, 100.0))
        # La falta de datos de precio no premia ni castiga: los demás pesos se reescalan
        self.assertEqual(history.reliability(2, 0, 5, None), 100)
        self.assertEqual(history.reliability(25, 100, 3, None), 0)
        self.assertEqual(history.reliability(13.5, 50, 4, None), 50)


class DemoAliveTest(unittest.TestCase):
    """Regresión 🔴/🟠: la demo se genera fuera de init_db (hilo de mantenimiento) y sigue viva día a día."""

    def setUp(self):
        self.env = _Env()

    def tearDown(self):
        self.env.close()

    def test_init_db_no_genera_y_el_mantenimiento_si(self):
        db.init_db(seed_size=120)
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM product_snapshots").fetchone()[0], 120)
        self.assertTrue(history.rankings({"type": ["top"], "days": ["7"]})["generating"])
        r = history.run_maintenance()
        self.assertTrue(r["done"])
        self.assertEqual(r["generated"], 120)
        search.invalidate()
        self.assertFalse(history.rankings({"type": ["top"], "days": ["7"]})["generating"])
        self.assertEqual(history.run_maintenance()["generated"], 0)                # idempotente

    def test_interruptor_desactiva_la_demo(self):
        history.SYNTH_ENABLED = False
        db.init_db(seed_size=50)
        self.assertEqual(history.run_maintenance()["generated"], 0)
        self.assertFalse(history.rankings({"type": ["top"], "days": ["7"]})["generating"])

    def test_la_demo_sigue_viva(self):
        db.init_db(seed_size=150)
        history.run_maintenance()
        with db.connect() as conn:
            pid, before = conn.execute("SELECT product_id, orders FROM product_snapshots WHERE date = ? "
                                       "ORDER BY orders DESC LIMIT 1", (D0.isoformat(),)).fetchone()
        self.env.set_day(D0 + timedelta(days=7))
        upsert([prod("NUEVO")])                    # solo un producto real recibe foto nueva
        r = history.run_maintenance()
        self.assertEqual(r["extended"], 150)
        rows = rows_of(pid)
        self.assertEqual(rows[-1]["date"], (D0 + timedelta(days=7)).isoformat())
        self.assertGreaterEqual(rows[-1]["orders"], before)
        self.assertEqual(sum(r["orders_day"] for r in rows[-7:]), rows[-1]["orders"] - before)
        top = history.rankings({"type": ["top"], "days": ["7"], "limit": ["50"]})["items"]
        self.assertGreater(sum(1 for i in top if i["source"] == "mock"), 20)
        falling = history.rankings({"type": ["falling"], "days": ["7"], "limit": ["100"]})["items"]
        self.assertFalse([i for i in falling if i["source"] == "mock" and i["change_pct"] == -100.0])
        # Determinista: la misma extensión en otra base da los mismos pedidos
        with db.connect() as conn:
            p = conn.execute("SELECT p.*, h.end0, h.last_date, h.rate, h.growth, h.amp FROM products p "
                             "JOIN history_synth h ON h.product_id = p.id WHERE p.id = ?", (pid,)).fetchone()
        st = dict(p)
        st["last_date"] = D0.isoformat()
        again = history.extend_rows(p, st, before, D0 + timedelta(days=7))
        self.assertEqual([x[2] for x in again], [r["orders"] for r in rows[-7:]])


class ReadOnlyGetTest(unittest.TestCase):
    """Regresión 🟠: los GET no escriben. Con la base bloqueada por otra escritura larga responden enseguida."""

    def setUp(self):
        self.env = _Env()
        db.init_db(seed_size=60)
        with db.connect() as conn:                 # base "anterior al módulo": sin tablas propias ni fotos
            for t in ("history_meta", "history_seasonality", "history_season_state", "history_synth"):
                conn.execute(f"DROP TABLE IF EXISTS {t}")
            conn.execute("DELETE FROM product_snapshots")
        search.invalidate()

    def tearDown(self):
        self.env.close()

    def test_get_con_la_base_bloqueada(self):
        import sqlite3
        import time
        lock = sqlite3.connect(db.DB_PATH)
        try:
            lock.execute("BEGIN IMMEDIATE")
            lock.execute("UPDATE products SET stock = stock WHERE id = 1")
            t0 = time.perf_counter()
            r = history.rankings({"type": ["top"], "days": ["30"]})
            self.assertTrue(r["generating"])
            self.assertEqual(r["items"], [])
            self.assertEqual(history.seasonal({"month": ["12"]})["items"], [])
            self.assertEqual(history.product_history(1, {"days": ["30"]})["points"], [])
            history.supplier_stats()
            self.assertLess(time.perf_counter() - t0, 1.5)
            # El mantenimiento tampoco espera indefinidamente: lo deja para después
            old = history.MAINT_BUSY_MS
            history.MAINT_BUSY_MS = 100
            try:
                self.assertFalse(history.run_maintenance()["done"])
            finally:
                history.MAINT_BUSY_MS = old
        finally:
            lock.rollback()
            lock.close()
        with db.connect() as conn:
            self.assertFalse(history.schema_ready(conn))       # ningún GET creó tablas
        r = history.run_maintenance()
        self.assertTrue(r["done"])
        self.assertEqual(r["generated"], 60)


class NewRankingTest(unittest.TestCase):
    """Regresión 🟠: "nuevos" compara con un período anterior de igual duración que la ventana."""

    def setUp(self):
        self.env = _Env()
        db.init_db(seed_size=0)
        self.ids = upsert([prod("NEW90"), prod("OLDRISE")])
        n = 730
        put_series(self.ids["NEW90"], [0] * (n - 60) + [15] * 60)                 # lanzado hace 60 días
        put_series(self.ids["OLDRISE"], [6] * (n - 90) + [18] * 90)               # vende hace años y sube

    def tearDown(self):
        self.env.close()

    def test_nuevos_90_dias(self):
        items = history.rankings({"type": ["new"], "days": ["90"]})["items"]
        self.assertEqual([i["id"] for i in items], [self.ids["NEW90"]])
        self.assertEqual(items[0]["prev_period_orders"], 0)
        rising = history.rankings({"type": ["rising"], "days": ["90"]})["items"]
        self.assertEqual([i["id"] for i in rising], [self.ids["OLDRISE"]])


if __name__ == "__main__":
    unittest.main()
