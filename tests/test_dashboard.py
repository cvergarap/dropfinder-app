"""Pruebas del panel del usuario (backend/dashboard.py): indicadores, ganancia estimada, atención,
recomendaciones, primeros pasos, separación por usuario y rendimiento.
Ejecutar:  python3 -m unittest discover tests"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
os.environ.setdefault("DROPFINDER_DB", os.path.join(tempfile.mkdtemp(), "test.db"))

import auth_context  # noqa: E402
import authtest  # noqa: E402  (sesión del propietario para HTTP)
import collections_mod  # noqa: E402
import dashboard  # noqa: E402
import db  # noqa: E402
import history  # noqa: E402
import saved_searches  # noqa: E402
import search  # noqa: E402
import server  # noqa: E402
from connectors.base import NormalizedProduct  # noqa: E402
from errors import ApiError  # noqa: E402
from metrics import DEFAULT_SETTINGS, profit_for  # noqa: E402

D0 = date(2026, 11, 21)          # "hoy" fijo
OWNER, MEMBER = 1, 2


class _Env:
    """Base temporal propia, fecha fija y sin historial sintético ni hilo de fondo; restaura todo al cerrar."""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="dropfinder-dash-")
        self.old = (db.DB_PATH, history.today, history.BACKGROUND, history.SYNTH_ENABLED)
        db.DB_PATH = os.path.join(self.dir, "t.db")
        history.today = lambda: D0
        history.BACKGROUND = False
        history.SYNTH_ENABLED = False
        search.invalidate()

    def close(self):
        db.DB_PATH, history.today, history.BACKGROUND, history.SYNTH_ENABLED = self.old
        history._maint_state.update(day=None, path=None)
        search.invalidate()
        shutil.rmtree(self.dir, ignore_errors=True)


def prod(sku, category, cost, ship=1.0, stock=50, o7=7, orders=1000):
    return NormalizedProduct(supplier="Prov", supplier_sku=sku, title=f"Producto {sku}", category=category,
                             cost=cost, shipping_cost=ship, shipping_days=5, warehouse="CO", stock=stock,
                             orders=orders, orders_7d=o7, orders_prev_7d=7, rating=4.5, reviews=10)


def as_owner():
    return auth_context.as_user(OWNER, "owner")


def as_member():
    return auth_context.as_user(MEMBER, "member")


def ids_by_sku(conn):
    return {r[0]: r[1] for r in conn.execute("SELECT supplier_sku, id FROM products")}


def add_history(conn, pid, prev_rate, cur_rate, costs=None, ship=1.0):
    """61 fotos diarias (D0−60 … D0): `prev_rate` pedidos/día en los 30 días anteriores y `cur_rate` en los
    últimos 30. costs: f(fecha) → costo del producto ese día."""
    cum = 1000
    for k in range(60, -1, -1):
        d = D0 - timedelta(days=k)
        od = 0 if k == 60 else (prev_rate if k >= 30 else cur_rate)
        cum += od
        c = costs(d) if costs else 10.0
        conn.execute("INSERT OR REPLACE INTO product_snapshots(product_id, date, orders, orders_day, cost, "
                     "shipping_cost, stock) VALUES (?, ?, ?, ?, ?, ?, 50)", (pid, d.isoformat(), cum, od, c, ship))


def add_import(conn, uid, pid, price, status="draft", created="2026-11-01T00:00:00+00:00"):
    title = conn.execute("SELECT title FROM products WHERE id = ?", (pid,)).fetchone()[0]
    cost, stock = conn.execute("SELECT cost, stock FROM products WHERE id = ?", (pid,)).fetchone()
    return conn.execute("INSERT INTO imported_products(user_id, product_id, title, sell_price, last_cost, last_stock, "
                        "status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (uid, pid, title, price, cost, stock, status, created)).lastrowid


class DashboardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = _Env()
        db.init_db(seed_size=0)
        products = [
            prod("C1", "Cocina", 12.0),                 # importado (activo); costo 11 → 13 (+18 %)
            prod("C2", "Cocina", 10.0),                 # en ascenso (+100 %)
            prod("C3", "Cocina", 10.0),                 # en ascenso (+50 %)
            prod("C5", "Cocina", 5.0, stock=0),         # importado (borrador) sin stock
            prod("M1", "Fitness", 10.0),                # el que más crece del catálogo (+400 %), fuera de mis categorías
            prod("M2", "Mascotas", 20.0, ship=2.0, o7=70),     # importado (activo) a pérdida, sin historial
            prod("M3", "Mascotas", 10.0, ship=0.0, o7=70000),  # importado (activo), pedidos enormes: tope
        ]
        with db.connect() as conn:
            db.upsert_products(conn, products, "csv")
            conn.execute("DELETE FROM product_snapshots")       # historial controlado por la prueba
            p = cls.p = ids_by_sku(conn)
            add_history(conn, p["C1"], 1, 3, costs=lambda d: 10.0 if d < D0 - timedelta(days=10) else 12.0)
            add_history(conn, p["C2"], 1, 2)
            add_history(conn, p["C3"], 2, 3)
            add_history(conn, p["M1"], 1, 5)
            add_history(conn, p["C5"], 1, 1, costs=lambda d: 5.0)
            add_history(conn, p["M3"], 1, 1, costs=lambda d: 10.0, ship=0.0)
            cls.i1 = add_import(conn, OWNER, p["C1"], 30.0, "active")
            cls.i2 = add_import(conn, OWNER, p["C5"], 20.0, "draft")
            cls.i3 = add_import(conn, OWNER, p["M2"], 15.0, "active")
            cls.i4 = add_import(conn, OWNER, p["M3"], 100.0, "active")
            now = datetime(D0.year, D0.month, D0.day, 12, tzinfo=timezone.utc)   # mismo reloj que el panel
            old = (now - timedelta(days=40)).isoformat(timespec="seconds")
            recent = now.isoformat(timespec="seconds")
            for t, created, read, uid in [("price_up", recent, 0, OWNER), ("price_up", recent, 1, OWNER),
                                          ("out_of_stock", recent, 0, OWNER), ("price_down", old, 0, OWNER),
                                          ("price_up", recent, 0, MEMBER)]:
                conn.execute("INSERT INTO alerts(imported_id, type, message, created_at, read, user_id) "
                             "VALUES (NULL, ?, 'x', ?, ?, ?)", (t, created, read, uid))
            sid = conn.execute("INSERT INTO saved_searches(name, params, created_at, user_id) VALUES "
                               "('Cocina', ?, ?, ?)", (json.dumps({"category": ["Cocina"]}), recent, OWNER)).lastrowid
            conn.execute("INSERT INTO saved_search_hits(saved_search_id, product_id, first_seen_at) VALUES (?, ?, ?)",
                         (sid, p["C2"], "2099-01-01T00:00:00"))
        search.invalidate()
        cls.s = dict(DEFAULT_SETTINGS)

    @classmethod
    def tearDownClass(cls):
        cls.env.close()

    def get(self, uid=OWNER):
        with auth_context.as_user(uid, "owner" if uid == OWNER else "member"):
            return dashboard.dashboard()

    def profit(self, price, total):
        return profit_for(price, total, self.s)

    # ---------------------------------------------------------------- indicadores

    def test_kpis(self):
        k = self.get()["kpis"]
        self.assertEqual(k["imported"], 4)
        self.assertEqual(k["active"], 3)
        self.assertEqual(k["drafts"], 1)
        margins = [self.profit(30, 13)[1], self.profit(20, 6)[1], self.profit(15, 22)[1], self.profit(100, 10)[1]]
        self.assertAlmostEqual(k["avg_margin"], round(sum(margins) / 4, 1))
        active_profit = self.profit(30, 13)[0] + self.profit(15, 22)[0] + self.profit(100, 10)[0]
        self.assertAlmostEqual(k["potential_profit_per_sale"], round(active_profit, 2))
        self.assertEqual(k["out_of_stock"], 1)
        self.assertEqual(k["negative_margin"], 1)
        self.assertEqual(k["unread_alerts"], 3)          # 2 recientes + 1 vieja sin leer (todas las no leídas)
        self.assertEqual(k["saved_searches"], 1)
        self.assertEqual(k["new_in_saved_searches"], 1)

    def test_estimated_monthly_profit(self):
        d = self.get()
        est = {e["import_id"]: e for e in d["estimate"]["products"]}
        self.assertNotIn(self.i2, est)                    # solo productos activos
        # C1: historial completo → 30 días × 3 pedidos = 90 → 0,9 ventas al mes
        self.assertEqual(est[self.i1]["orders_source"], "historial")
        self.assertEqual(est[self.i1]["orders_30d"], 90)
        self.assertAlmostEqual(est[self.i1]["expected_orders"], 0.9)
        # M2: sin historial → 70 pedidos/7 días × 30 / 7 = 300 → 3 ventas al mes (y vende a pérdida)
        self.assertEqual(est[self.i3]["orders_source"], "estimado_7d")
        self.assertAlmostEqual(est[self.i3]["expected_orders"], 3.0)
        # M3: historial de 30 días con 1 pedido/día = 30 → 0,3 ventas
        self.assertAlmostEqual(est[self.i4]["expected_orders"], 0.3)
        expected = 0.9 * self.profit(30, 13)[0] + 3 * self.profit(15, 22)[0] + 0.3 * self.profit(100, 10)[0]
        self.assertAlmostEqual(d["kpis"]["estimated_monthly_profit"], round(expected, 2), places=2)
        self.assertIn("1 %", d["assumptions"])
        self.assertIn("30 al mes", d["assumptions"])

    def test_estimate_cap(self):
        self.assertEqual(dashboard.estimated_profit(300_000, 2.0), (30, 60.0))
        self.assertEqual(dashboard.estimated_profit(100, 2.0), (1.0, 2.0))
        o30, src = dashboard.orders_30d({"orders_7d": 14}, 0, None, (D0 - timedelta(days=29)).isoformat())
        self.assertEqual((o30, src), (60.0, "estimado_7d"))

    # ---------------------------------------------------------------- gráficos

    def test_charts(self):
        c = self.get()["charts"]
        ci = c["cost_index"]
        self.assertEqual(len(ci["points"]), 30)
        self.assertEqual(ci["until"], D0.isoformat())
        self.assertEqual(ci["points"][0]["index"], 100.0)
        # día 1: C1 11, C5 6, M2 sin fotos → actual 22, M3 10 → 49/4; al final C1 13 → 51/4
        self.assertAlmostEqual(ci["points"][0]["avg_total_cost"], 12.25)
        self.assertAlmostEqual(ci["points"][-1]["avg_total_cost"], 12.75)
        self.assertAlmostEqual(ci["change_pct"], round(51 / 49 * 100 - 100, 1))
        types = {x["type"]: x["count"] for x in c["alerts_by_type"]["items"]}
        self.assertEqual(types, {"price_up": 2, "out_of_stock": 1})       # la de hace 40 días no cuenta
        cats = {x["category"]: x["count"] for x in c["imports_by_category"]["items"]}
        self.assertEqual(cats, {"Cocina": 2, "Mascotas": 2})

    def test_category_chart_groups_others(self):
        imports = [{"category": f"Cat{i}"} for i in range(8)] + [{"category": "Cat0"}]
        # mismo cálculo que dashboard(): top 6 + "Otras"
        with as_owner(), db.connect() as conn:
            conn.execute("SAVEPOINT t")
            for i, imp in enumerate(imports):
                pid = conn.execute("INSERT INTO products(supplier, supplier_sku, title, category, cost, shipping_cost, "
                                   "shipping_days, warehouse, stock, orders, orders_7d, orders_prev_7d, rating, reviews, "
                                   "updated_at) VALUES ('X', ?, 'x', ?, 1, 1, 1, 'CO', 1, 1, 1, 1, 4, 1, '')",
                                   (f"K{i}", imp["category"])).lastrowid
                add_import(conn, 7, pid, 10)
            conn.execute("RELEASE t")
        with auth_context.as_user(7):
            items = dashboard.dashboard()["charts"]["imports_by_category"]["items"]
        self.assertEqual(len(items), 7)
        self.assertEqual(items[0], {"category": "Cat0", "label": "Cat0", "count": 2, "other": False})
        self.assertEqual(items[-1], {"category": None, "label": dashboard.OTHER_LABEL, "count": 2, "other": True})

    # ---------------------------------------------------------------- atención

    def test_attention_order(self):
        att = self.get()["attention"]
        self.assertEqual([a["reason"] for a in att["items"]], ["negative_margin", "out_of_stock", "cost_up"])
        self.assertEqual([a["import_id"] for a in att["items"]], [self.i3, self.i2, self.i1])
        self.assertEqual(att["total"], 3)
        self.assertEqual(att["items"][0]["product_id"], self.p["M2"])
        self.assertEqual(att["items"][0]["action"], "imports")
        self.assertAlmostEqual(att["items"][2]["change_pct"], round((13 / 11 - 1) * 100, 1))

    # ---------------------------------------------------------------- recomendaciones

    def test_recommendations(self):
        recs = self.get()["recommendations"]
        ids = [r["id"] for r in recs["items"]]
        mine = {self.p["C1"], self.p["C5"], self.p["M2"], self.p["M3"]}
        self.assertFalse(mine & set(ids))                       # nunca lo que ya importé
        self.assertEqual(recs["categories"][0], "Cocina")       # empate 2–2: orden alfabético
        # Mis categorías primero (en ascenso: C2 +100 %, C3 +50 %), aunque M1 crezca más; M1 después (global)
        self.assertEqual(ids[:2], [self.p["C2"], self.p["C3"]])
        self.assertIn(self.p["M1"], ids)
        for f in dashboard.REC_FIELDS:
            self.assertIn(f, recs["items"][0])
        self.assertLessEqual(len(ids), 8)

    def test_recommendations_new_user_global(self):
        recs = self.get(MEMBER)["recommendations"]
        self.assertEqual(recs["categories"], [])
        self.assertEqual(recs["items"][0]["id"], self.p["M1"])  # lo que más crece del catálogo
        self.assertIn(self.p["C1"], [r["id"] for r in recs["items"]])   # lo importó otro usuario, no yo

    # ---------------------------------------------------------------- primeros pasos

    def test_onboarding_and_dismiss(self):
        uid = 11
        with auth_context.as_user(uid):
            ob = dashboard.dashboard()["onboarding"]
            keys = [i["key"] for i in ob["items"]]
            self.assertEqual(keys, ["currency", "pricing", "first_import", "saved_search", "collection", "store"])
            self.assertEqual((ob["done"], ob["total"], ob["complete"]), (0, 5, False))
            store = ob["items"][-1]
            self.assertTrue(store["disabled"])
            self.assertFalse(store["done"])
            self.assertIn("Sprint 4", store["hint"])
            self.assertEqual(dashboard.dismiss("currency"), {"ok": True, "key": "currency"})
            cur = next(i for i in dashboard.dashboard()["onboarding"]["items"] if i["key"] == "currency")
            self.assertTrue(cur["done"] and cur["dismissed"])
            with self.assertRaises(ApiError) as cm:
                dashboard.dismiss("nada")
            self.assertEqual(cm.exception.status, 404)
            dashboard.dismiss("checklist")
            self.assertTrue(dashboard.dashboard()["onboarding"]["hidden"])
        with auth_context.as_user(12):                        # el descarte es por usuario
            ob = dashboard.dashboard()["onboarding"]
            self.assertFalse(ob["hidden"])
            self.assertFalse(ob["items"][0]["done"])

    def test_onboarding_real_state(self):
        with db.connect() as conn:
            collections_mod.ensure_schema(conn)
            conn.execute("INSERT INTO collections(user_id, name, created_at, updated_at) VALUES (?, 'Ideas', 'x', 'x')",
                         (OWNER,))
            conn.execute("UPDATE settings SET value = '3.0' WHERE key = 'multiplier'")
            conn.execute("UPDATE currency_settings SET value = '\"COP\"' WHERE key = 'display_currency'")
        try:
            items = {i["key"]: i for i in self.get()["onboarding"]["items"]}
            for k in ("currency", "pricing", "first_import", "saved_search", "collection"):
                self.assertTrue(items[k]["done"], k)
                self.assertFalse(items[k]["dismissed"], k)
            m = {i["key"]: i for i in self.get(MEMBER)["onboarding"]["items"]}
            self.assertFalse(m["collection"]["done"])           # la colección es del otro usuario
            self.assertFalse(m["first_import"]["done"])
        finally:
            with db.connect() as conn:
                conn.execute("DELETE FROM collections")
                conn.execute("UPDATE settings SET value = '2.5' WHERE key = 'multiplier'")
                conn.execute("UPDATE currency_settings SET value = '\"USD\"' WHERE key = 'display_currency'")

    # ---------------------------------------------------------------- usuarios

    def test_isolation(self):
        d = self.get(MEMBER)
        k = d["kpis"]
        self.assertEqual((k["imported"], k["active"], k["out_of_stock"], k["negative_margin"]), (0, 0, 0, 0))
        self.assertIsNone(k["avg_margin"])
        self.assertEqual(k["estimated_monthly_profit"], 0)
        self.assertEqual(k["unread_alerts"], 1)                 # solo su propia alerta
        self.assertEqual((k["saved_searches"], k["new_in_saved_searches"]), (0, 0))
        self.assertEqual(d["attention"]["items"], [])
        self.assertEqual(d["charts"]["cost_index"]["points"], [])
        self.assertEqual(d["charts"]["imports_by_category"]["items"], [])
        self.assertEqual(d["charts"]["alerts_by_type"]["items"], [{"type": "price_up", "label": "Subió el costo", "count": 1}])

    def test_no_user_context_is_401(self):
        """Sin contexto de usuario no hay atajos: 401, también con variables de entorno de desarrollo."""
        self.assertFalse(hasattr(dashboard, "DEV_USER_ENV"))
        prev = (getattr(auth_context._local, "user_id", None), getattr(auth_context._local, "role", None))
        self.addCleanup(auth_context.set_current, *prev)      # las demás pruebas usan el contexto de authtest
        auth_context.clear()
        with unittest.mock.patch.dict(os.environ, {"DROPFINDER_DEV_USER": "1"}):
            for call in (dashboard.dashboard, lambda: dashboard.dismiss("currency")):
                with self.assertRaises(ApiError) as cm:
                    call()
                self.assertEqual(cm.exception.status, 401)

    def test_http(self):
        srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        try:
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(base + "/api/dashboard", timeout=10).close()
            self.assertEqual(cm.exception.code, 401)
            cm.exception.close()
            # Con la sesión del propietario (usuario 1, tests/authtest.py)
            req = urllib.request.Request(base + "/api/dashboard", headers=authtest.auth_headers())
            with urllib.request.urlopen(req, timeout=10) as r:
                body = json.loads(r.read())
            self.assertEqual(set(body), {"kpis", "estimate", "charts", "attention", "recommendations", "onboarding",
                                         "assumptions", "generated_at", "took_ms"})
            self.assertEqual(body["kpis"]["imported"], 4)
        finally:
            srv.shutdown()
            srv.server_close()

    # ---------------------------------------------------------------- regresiones de la revisión

    def test_alert_window_uses_history_clock(self):
        """La ventana de alertas usa history.today() (como el resto del panel), no el reloj del sistema."""
        old_today = history.today
        try:
            history.today = lambda: date(2025, 1, 31)
            with db.connect() as conn:
                conn.execute("INSERT INTO alerts(type, message, created_at, user_id) VALUES "
                             "('price_up', 'x', '2025-01-15T10:00:00+00:00', 21), "
                             "('price_down', 'x', '2024-12-01T10:00:00+00:00', 21)")
            with auth_context.as_user(21):
                items = dashboard.dashboard()["charts"]["alerts_by_type"]["items"]
            self.assertEqual([(x["type"], x["count"]) for x in items], [("price_up", 1)])
        finally:
            history.today = old_today

    def test_other_bucket_does_not_merge_real_category(self):
        with db.connect() as conn:
            for i, cat in enumerate(["Otras", "A", "B", "C", "D", "E", "F", "G"]):
                pid = conn.execute("INSERT INTO products(supplier, supplier_sku, title, category, cost, shipping_cost, "
                                   "shipping_days, warehouse, stock, orders, orders_7d, orders_prev_7d, rating, reviews, "
                                   "updated_at) VALUES ('Y', ?, ?, ?, 1, 1, 1, 'CO', 1, 1, 1, 1, 4, 1, '')",
                                   (f"O{i}", f"Otro {i}", cat)).lastrowid
                add_import(conn, 22, pid, 10)
                if cat == "Otras":
                    pid2 = conn.execute("INSERT INTO products(supplier, supplier_sku, title, category, cost, shipping_cost, "
                                        "shipping_days, warehouse, stock, orders, orders_7d, orders_prev_7d, rating, "
                                        "reviews, updated_at) VALUES ('Y', 'O-bis', 'Otro bis', 'Otras', 1, 1, 1, 'CO', "
                                        "1, 1, 1, 1, 4, 1, '')").lastrowid
                    add_import(conn, 22, pid2, 10)
        with auth_context.as_user(22):
            items = dashboard.dashboard()["charts"]["imports_by_category"]["items"]
        self.assertEqual(items[0], {"category": "Otras", "label": "Otras", "count": 2, "other": False})
        self.assertEqual(items[-1], {"category": None, "label": dashboard.OTHER_LABEL, "count": 2, "other": True})

    def test_onboarding_member_not_done_by_owner_settings(self):
        with db.connect() as conn:
            conn.execute("UPDATE settings SET value = '3.0' WHERE key = 'multiplier'")
            conn.execute("UPDATE currency_settings SET value = '\"COP\"' WHERE key = 'display_currency'")
        try:
            with auth_context.as_user(23, "member"):
                m = {i["key"]: i for i in dashboard.dashboard()["onboarding"]["items"]}
                self.assertFalse(m["currency"]["done"])
                self.assertFalse(m["pricing"]["done"])
                dashboard.dismiss("pricing")                    # el miembro solo lo completa confirmando
                m = {i["key"]: i for i in dashboard.dashboard()["onboarding"]["items"]}
                self.assertTrue(m["pricing"]["done"] and m["pricing"]["dismissed"])
            with auth_context.as_user(24, "owner"):
                o = {i["key"]: i for i in dashboard.dashboard()["onboarding"]["items"]}
                self.assertTrue(o["currency"]["done"] and o["pricing"]["done"])
                self.assertFalse(o["currency"]["dismissed"])
        finally:
            with db.connect() as conn:
                conn.execute("UPDATE settings SET value = '2.5' WHERE key = 'multiplier'")
                conn.execute("UPDATE currency_settings SET value = '\"USD\"' WHERE key = 'display_currency'")

    def test_collections_only_mine_without_favorites(self):
        """Paso "Crea una colección": API pública de colecciones; ni Favoritos ni las de otro usuario cuentan."""
        with db.connect() as conn:
            collections_mod.ensure_schema(conn)
            conn.execute("INSERT INTO collections(user_id, name, is_favorites, created_at, updated_at) "
                         "VALUES (?, 'Favoritos', 1, 'x', 'x')", (OWNER,))
            conn.execute("INSERT INTO collections(user_id, name, created_at, updated_at) VALUES (?, 'Ideas', 'x', 'x')",
                         (MEMBER,))
        try:
            self.assertFalse({i["key"]: i for i in self.get()["onboarding"]["items"]}["collection"]["done"])
            self.assertTrue({i["key"]: i for i in self.get(MEMBER)["onboarding"]["items"]}["collection"]["done"])
        finally:
            with db.connect() as conn:
                conn.execute("DELETE FROM collections")

    def test_saved_searches_public_api_no_ddl(self):
        """new_counts es pública, coincide con el contador de la pestaña y el GET del panel no crea tablas."""
        with db.connect() as conn:
            conn.execute("DROP TABLE IF EXISTS saved_search_pending")
        with as_owner(), db.connect() as conn:
            counts = saved_searches.new_counts(conn, OWNER)
        self.assertEqual(list(counts.values()), [1])
        self.assertEqual(self.get()["kpis"]["new_in_saved_searches"], 1)
        with db.connect() as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'saved_search_pending'").fetchone())
            self.assertEqual(saved_searches.new_counts(conn, MEMBER), {})
        with open(dashboard.__file__, encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn("_count_new", src)
        self.assertNotIn("_ensure_schema", src)

    def test_rankings_cache_shared(self):
        """Importar otro producto o ser otro usuario no cambia la clave de caché de los rankings."""
        self.get()
        with unittest.mock.patch.object(history, "compute_ranking", wraps=history.compute_ranking) as spy:
            self.get()
            self.get(MEMBER)
            with db.connect() as conn:
                iid = add_import(conn, OWNER, self.p["C3"], 30.0)
            try:
                self.get()
            finally:
                with db.connect() as conn:
                    conn.execute("DELETE FROM imported_products WHERE id = ?", (iid,))
        self.assertEqual(spy.call_count, 0)


class FrontendStartupTest(unittest.TestCase):
    """Arranque sin carreras: el Panel es la vista activa desde el HTML (no se cambia de pestaña cuando llega
    la respuesta) y la búsqueda inicial se difiere hasta abrir Buscar."""

    @staticmethod
    def read(*parts):
        with open(os.path.join(os.path.dirname(__file__), "..", "frontend", *parts), encoding="utf-8") as f:
            return f.read()

    def test_panel_is_initial_view_in_html(self):
        html = self.read("index.html")
        self.assertIn('<button class="tab active" data-view="dashboard">', html)
        self.assertIn('<section id="view-dashboard" class="view active">', html)
        self.assertIn('<section id="view-search" class="view">', html)
        self.assertEqual(html.count('class="tab active"'), 1)

    def test_no_deferred_tab_switch_and_search_deferred(self):
        js = self.read("js", "dashboard.js")
        self.assertNotIn('show("dashboard");', js.split("DF.dashboard =")[0])   # solo DF.dashboard.home() cambia de pestaña
        self.assertIn('home: () => show("dashboard")', js)
        app = self.read("app.js")
        self.assertIn("DF.deferInitialSearch", app)
        self.assertNotIn(".then(search);", app)


class DashboardPerformanceTest(unittest.TestCase):
    """Rendimiento con 20.000 productos, 1.000 importados, 5 búsquedas guardadas y 61 días de fotos de TODOS los
    productos (1,2 M filas; el 5 % cambia de costo). Qué se mide (dashboard() completo + json.dumps, la
    mediana de 3 llamadas):
    - frío: justo después de invalidar la caché (como tras una carga de catálogo), con los rankings en
      segundo plano (ASYNC_RANKINGS, el modo del servidor): < 150 ms y "pending" → luego, ya con la caché
      llena, la siguiente llamada trae las 8 recomendaciones de los rankings, también < 150 ms;
    - caliente (rankings en caché): < 150 ms.
    No se mide la primera generación del historial sintético de la demo (la hace el hilo de history)."""

    @classmethod
    def setUpClass(cls):
        cls.env = _Env()
        db.init_db(seed_size=20_000)
        with db.connect() as conn:
            conn.execute("DELETE FROM product_snapshots")
            conn.execute(
                "WITH RECURSIVE k(n) AS (SELECT 0 UNION ALL SELECT n + 1 FROM k WHERE n < 60) "
                "INSERT INTO product_snapshots(product_id, date, orders, orders_day, cost, shipping_cost, stock) "
                "SELECT p.id, date(:d0, '-' || k.n || ' days'), p.orders - k.n * (p.id % 7 + 1), p.id % 7 + 1, "
                "  p.cost * (CASE WHEN p.id % 20 = 0 AND k.n > 10 THEN 0.92 ELSE 1.0 END), p.shipping_cost, p.stock "
                "FROM products p, k", {"d0": D0.isoformat()})
            ids = [r[0] for r in conn.execute("SELECT id FROM products ORDER BY (id * 7919) % 20011 LIMIT 1000")]
            for n, pid in enumerate(ids):
                add_import(conn, OWNER, pid, 25.0, "active" if n % 2 else "draft")
            for k in range(5):
                conn.execute("INSERT INTO saved_searches(name, params, created_at, user_id) VALUES ('s', ?, ?, ?)",
                             (json.dumps({"q": "led", "max_cost": 20 + k}), "2026-11-01", OWNER))
        search.invalidate()

    @classmethod
    def tearDownClass(cls):
        dashboard.wait_rankings()
        cls.env.close()

    def timed(self):
        t = time.perf_counter()
        d = dashboard.dashboard()
        json.dumps(d, ensure_ascii=False)
        return d, (time.perf_counter() - t) * 1000

    def test_cold_and_warm(self):
        cold, after, warm = [], [], []
        with as_owner(), unittest.mock.patch.object(dashboard, "ASYNC_RANKINGS", True):
            dashboard.dashboard()                    # primera conexión / páginas de SQLite en memoria
            dashboard.wait_rankings()
            for _ in range(3):
                search.invalidate()                  # caché vacía: como tras una carga del catálogo
                d, ms = self.timed()
                cold.append(ms)
                self.assertTrue(d["recommendations"]["pending"])
                # respaldo "Mejor score" (la demo repite títulos y 1.000 importados cubren casi todos)
                self.assertTrue(d["recommendations"]["items"])
                dashboard.wait_rankings()
                d, ms = self.timed()
                after.append(ms)
                self.assertFalse(d["recommendations"]["pending"])
                self.assertIn(d["recommendations"]["items"][0]["reason"], ("En ascenso", "Más vendido"))
        with as_owner():
            for _ in range(3):
                d, ms = self.timed()
                warm.append(ms)
        self.assertEqual(d["kpis"]["imported"], 1000)
        med = lambda xs: sorted(xs)[1]  # noqa: E731
        self.assertLess(med(cold), 150, cold)
        self.assertLess(med(after), 150, after)
        self.assertLess(med(warm), 150, warm)


if __name__ == "__main__":
    unittest.main()
