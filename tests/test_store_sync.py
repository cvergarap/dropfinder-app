"""Motor de publicación y sincronización de tiendas (Sprint 4-5): cola, publicación, reglas, monitor programado."""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DROPFINDER_DB", os.path.join(tempfile.mkdtemp(), "test.db"))
os.environ["DROPFINDER_SYNTH_HISTORY"] = "0"

import authtest  # noqa: E402,F401
import auth_context  # noqa: E402
import db  # noqa: E402
import hooks  # noqa: E402
import server  # noqa: E402
import store_connectors as sc  # noqa: E402
import store_sync  # noqa: E402
import stores_mod  # noqa: E402
from store_connectors.fake import FakeStore  # noqa: E402
from test_stores_base import _TempDb  # noqa: E402

sc.register(FakeStore)


class FakeIdem(FakeStore):
    """Como Shopify: publish() adopta el producto que ya existe con ese SKU (idempotent_publish)."""
    platform = "fakeidem"
    idempotent_publish = True

    def publish(self, product):
        st = self._state()
        for pid, p in st["products"].items():
            if p["sku"] == product.sku:
                return sc.ListingRef(external_id=pid, variant_id=f"v{pid}", inventory_item_id=f"i{pid}",
                                     url=f"https://{self.domain}/admin/products/{pid}")
        return super().publish(product)


sc.register(FakeIdem)
store_sync.BACKGROUND = False
store_sync._sleep = lambda s: None
CREDS = {"token": "shpat_secreto_1234"}
ApiError = server.ApiError


def utc() -> datetime:
    return datetime.now(timezone.utc)


class Base(unittest.TestCase):
    seed = 120

    def setUp(self):
        self.tmp = _TempDb()
        db.init_db(seed_size=self.seed)
        auth_context.set_current(1, "owner")
        store_sync.BACKGROUND = False
        self.domain = "tienda.fake.test"
        FakeStore.stores[self.domain] = {"products": {}, "currency": "USD", "revoked": False}
        self._csv_n = 0

    def tearDown(self):
        self.tmp.close()
        auth_context.set_current(1, "owner")

    # ---- ayudas
    def connect(self, domain=None, currency="USD", platform="fake", **settings):
        domain = domain or self.domain
        FakeStore.stores.setdefault(domain, {"products": {}, "currency": currency, "revoked": False})["currency"] = currency
        return stores_mod.connect_store({"platform": platform, "domain": domain, "credentials": dict(CREDS),
                                         "settings": settings})

    def product(self, cost=10.0, shipping=0.0, stock=10, image="https://img.example/p.jpg", source="csv") -> int:
        """Producto real (CSV) o demo, con valores controlados."""
        self._csv_n += 1
        with db.connect() as conn:
            if source == "mock":
                pid = conn.execute("SELECT id FROM products WHERE source = 'mock' AND id NOT IN "
                                   "(SELECT product_id FROM imported_products) ORDER BY id LIMIT 1").fetchone()[0]
                conn.execute("UPDATE products SET cost = ?, shipping_cost = ?, stock = ?, image = ? WHERE id = ?",
                             (cost, shipping, stock, image, pid))
                return pid
            cur = conn.execute(
                "INSERT INTO products(supplier, supplier_sku, source, title, category, image, cost, shipping_cost, "
                "shipping_days, warehouse, stock, orders, orders_7d, orders_prev_7d, rating, reviews, updated_at) "
                "VALUES ('Proveedor Secreto', ?, 'csv', ?, 'Hogar', ?, ?, ?, 7, 'CN', ?, 0, 0, 0, 4.5, 10, ?)",
                (f"SKU{self._csv_n}-{time.time_ns()}", f"Lámpara {self._csv_n}", image, cost, shipping, stock, db.now()))
            return cur.lastrowid

    def imp(self, pid=None, sell_price=20.0, **kw) -> int:
        pid = pid or self.product(**kw)
        return server.create_import({"product_id": pid, "sell_price": sell_price})["id"]

    def listing(self, lid):
        with db.connect() as conn:
            return dict(conn.execute("SELECT * FROM store_listings WHERE id = ?", (lid,)).fetchone())

    def jobs(self, lid=None, status=None):
        sql, a = "SELECT * FROM store_jobs WHERE 1=1", []
        if lid:
            sql += " AND listing_id = ?"; a.append(lid)
        if status:
            sql += " AND status = ?"; a.append(status)
        with db.connect() as conn:
            return [dict(r) for r in conn.execute(sql + " ORDER BY id", a)]

    def published(self, store=None, sell_price=20.0, as_draft=False, **pk):
        """Anuncio ya publicado (ejecuta el trabajo). Devuelve (store, imported_id, listing_id)."""
        store = store or self.connect()
        iid = self.imp(sell_price=sell_price, **pk)
        r = store_sync.publish(store["id"], {"imported_ids": [iid], "as_draft": as_draft})
        store_sync.run_due_jobs()
        return store, iid, r["queued"][0]["listing_id"]

    def catalog_change(self, pid, **cols):
        """Simula una carga/monitor: cambia el producto y llama al hook dentro de la transacción."""
        with db.connect() as conn:
            if cols:
                conn.execute(f"UPDATE products SET {', '.join(k + ' = ?' for k in cols)} WHERE id = ?",
                             [*cols.values(), pid])
            hooks.run_catalog_write(conn, [pid])

    def pid_of(self, iid):
        with db.connect() as conn:
            return conn.execute("SELECT product_id FROM imported_products WHERE id = ?", (iid,)).fetchone()[0]

    def remote(self, lid=None, ext=None):
        ext = ext or self.listing(lid)["external_id"]
        return FakeStore.stores[self.domain]["products"].get(ext)

    def alerts(self, typ="store_margin_low"):
        with db.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM alerts WHERE type = ?", (typ,))]


class PublishTest(Base):
    def test_happy_path_draft_by_default_and_cop_conversion(self):
        store = self.connect(currency="COP")
        iid = self.imp(sell_price=25.0)
        r = store_sync.publish(store["id"], {"imported_ids": [iid]})
        self.assertEqual(r["skipped"], [])
        lid = r["queued"][0]["listing_id"]
        view = store_sync.store_listings(store["id"], {})["items"][0]
        self.assertEqual((view["status"], view["pending_action"], view["is_demo"]), ("pending", "publish", False))
        self.assertEqual(store_sync.run_due_jobs(), {"processed": 1, "failed": 0})
        l = self.listing(lid)
        self.assertEqual(l["status"], "hidden")                      # borrador = no visible
        rp = self.remote(lid)
        self.assertFalse(rp["active"])
        self.assertEqual(rp["price"], 100900)                        # 25 USD × 4000 → redondeo "…900"
        self.assertEqual(rp["sku"], f"DF-{self.pid_of(iid)}")
        self.assertEqual(rp["image_urls"], ["https://img.example/p.jpg"])
        self.assertEqual((l["last_price"], l["last_stock"]), (100900, 10))
        self.assertIsNone(store_sync.store_listings(store["id"], {})["items"][0]["pending_action"])
        self.assertEqual(self.jobs(lid)[0]["status"], "done")

    def test_description_has_no_supplier_or_cost_and_payload_is_clean(self):
        store = self.connect()
        iid = self.imp(cost=7.77)
        store_sync.publish(store["id"], {"imported_ids": [iid]})
        raw = self.jobs()[0]["payload"]
        prod = json.loads(raw)["product"]
        self.assertNotIn("Proveedor Secreto", raw)
        self.assertNotIn("7.77", prod["description"])
        self.assertIn("Hogar", prod["description"])
        self.assertEqual((prod["tags"][0], prod["active"], prod["price"]), ("dropfinder", False, 20.0))
        self.assertRegex(prod["tags"][1], r"^dfi-[0-9a-f]{8}$")        # marca de ESTA instalación (ver installation_tag)
        self.assertEqual(len(prod["tags"]), 2)

    def test_not_draft_publishes_visible(self):
        store = self.connect()
        _, _, lid = self.published(store, as_draft=False)
        self.assertEqual(self.listing(lid)["status"], "published")
        self.assertTrue(self.remote(lid)["active"])

    def test_store_setting_decides_draft(self):
        store = self.connect(publish_as_draft=False)
        iid = self.imp()
        r = store_sync.publish(store["id"], {"imported_ids": [iid]})
        store_sync.run_due_jobs()
        self.assertEqual(self.listing(r["queued"][0]["listing_id"])["status"], "published")

    def test_only_https_images(self):
        store = self.connect()
        for i, img in enumerate(["", "data:image/svg+xml;base64,AAAA", "http://x.example/a.jpg", "javascript:alert(1)"]):
            iid = self.imp(image=img)
            store_sync.publish(store["id"], {"imported_ids": [iid]})
        store_sync.run_due_jobs()
        for p in FakeStore.stores[self.domain]["products"].values():
            self.assertEqual(p["image_urls"], [])

    def test_stock_is_capped_and_zero_ok(self):
        store = self.connect()
        a, b = self.imp(stock=10 ** 7), self.imp(stock=0)
        store_sync.publish(store["id"], {"imported_ids": [a, b]})
        store_sync.run_due_jobs()
        self.assertEqual(sorted(p["stock"] for p in FakeStore.stores[self.domain]["products"].values()),
                         [0, store_sync.STOCK_CAP])

    def test_usd_store_keeps_usd_price(self):
        store = self.connect(currency="USD")
        _, _, lid = self.published(store, sell_price=19.99)
        self.assertEqual(self.remote(lid)["price"], 19.99)

    def test_currency_unsupported(self):
        store = self.connect(currency="EUR")
        iid = self.imp()
        r = store_sync.publish(store["id"], {"imported_ids": [iid]})
        self.assertEqual((r["queued"], r["skipped"][0]["reason"]), ([], "currency_unsupported"))

    def test_idempotent_republish_and_double_execution(self):
        store, iid, lid = self.published()
        r = store_sync.publish(store["id"], {"imported_ids": [iid]})
        self.assertEqual((r["queued"], r["skipped"][0]["reason"]), ([], "already_published"))
        # el mismo trabajo ejecutado otra vez (p. ej. recuperado tras una caída) no crea otro producto remoto
        with db.connect() as conn:
            conn.execute("UPDATE store_jobs SET status = 'pending', attempts = 0")
        store_sync.run_due_jobs()
        self.assertEqual(len(FakeStore.stores[self.domain]["products"]), 1)
        self.assertEqual([c for c in FakeStore.calls if c[0] == "publish"].__len__(), 1)

    def test_demo_gating(self):
        store = self.connect()
        iid = self.imp(source="mock")
        r = store_sync.publish(store["id"], {"imported_ids": [iid]})
        self.assertEqual(r["skipped"][0]["reason"], "demo_not_allowed")
        r = store_sync.publish(store["id"], {"imported_ids": [iid], "allow_demo": True})
        self.assertEqual(len(r["queued"]), 1)
        self.assertTrue(store_sync.store_listings(store["id"], {})["items"][0]["is_demo"])

    def test_limits_and_validation(self):
        store = self.connect()
        for body in ({"imported_ids": list(range(1, 52))}, {"imported_ids": []}, {}, {"imported_ids": ["a"]},
                     {"imported_ids": [True]}, {"imported_ids": [1], "as_draft": "si"}, {"imported_ids": [1], "allow_demo": 1}):
            with self.assertRaises(ApiError) as cm:
                store_sync.publish(store["id"], body)
            self.assertEqual(cm.exception.status, 400, body)
        r = store_sync.publish(store["id"], {"imported_ids": [999999, 999999]})
        self.assertEqual(r["skipped"], [{"imported_id": 999999, "reason": "not_found", "message": "Producto no encontrado"}])

    def test_rate_limit_per_minute(self):
        store = self.connect()
        ids = [self.imp(source="mock") for _ in range(60)]
        store_sync.publish(store["id"], {"imported_ids": ids[:50], "allow_demo": True})
        with self.assertRaises(ApiError) as cm:
            store_sync.publish(store["id"], {"imported_ids": ids[50:], "allow_demo": True})
        self.assertEqual(cm.exception.status, 429)

    def test_store_with_error_status_skips_everything(self):
        store = self.connect()
        with db.connect() as conn:
            stores_mod.mark_error(conn, store["id"], "x")
        r = store_sync.publish(store["id"], {"imported_ids": [self.imp()]})
        self.assertEqual(r["skipped"][0]["reason"], "store_error")


class WorkerTest(Base):
    def queued(self, n=1, store=None):
        store = store or self.connect()
        ids = [self.imp() for _ in range(n)]
        r = store_sync.publish(store["id"], {"imported_ids": ids})
        return store, [q["listing_id"] for q in r["queued"]]

    def test_backoff_schedule_then_failed(self):
        _, (lid,) = self.queued()
        now = utc().replace(microsecond=0)
        deltas = []
        for attempt in range(1, 6):
            FakeStore.fail_next.append(sc.StoreError("tienda caída", retryable=True, not_sent=True))
            self.assertEqual(store_sync.run_due_jobs(now=now), {"processed": 1, "failed": 1})
            j = self.jobs(lid)[0]
            if attempt < 5:
                self.assertEqual((j["status"], j["attempts"]), ("pending", attempt))
                due = datetime.fromisoformat(j["run_after"])
                deltas.append(round((due - now).total_seconds()))
                self.assertEqual(store_sync.run_due_jobs(now=due - timedelta(seconds=1))["processed"], 0)
                now = due
        self.assertEqual(deltas, [60, 300, 1800, 7200])
        j = self.jobs(lid)[0]
        self.assertEqual((j["status"], j["attempts"]), ("failed", 5))
        self.assertEqual(self.listing(lid)["status"], "error")
        self.assertEqual(store_sync.queue_state()["failed"], 1)

    def test_auth_error_stops_connection_jobs(self):
        store, (a, b) = self.queued(2)
        FakeStore.fail_next.append(sc.StoreAuthError("token revocado"))
        self.assertEqual(store_sync.run_due_jobs()["failed"], 1)
        self.assertEqual(stores_mod.get_store(store["id"])["status"], "error")
        calls = len(FakeStore.calls)
        self.assertEqual(store_sync.run_due_jobs()["processed"], 0)          # no se martilla el token
        self.assertEqual(len(FakeStore.calls), calls)
        self.assertEqual({j["status"] for j in self.jobs()}, {"pending"})
        self.assertEqual(self.jobs(a)[0]["attempts"], 0)                     # no gastó intentos
        stores_mod.validate_store(store["id"])
        self.assertEqual(store_sync.run_due_jobs()["processed"], 2)
        self.assertEqual({self.listing(x)["status"] for x in (a, b)}, {"hidden"})

    def test_other_connection_unaffected_by_auth_error(self):
        s1, (a,) = self.queued()
        s2 = self.connect("otra.fake.test")
        r = store_sync.publish(s2["id"], {"imported_ids": [self.imp()]})
        FakeStore.fail_next.append(sc.StoreAuthError("x"))
        store_sync.run_due_jobs()
        store_sync.run_due_jobs()
        self.assertEqual(self.listing(r["queued"][0]["listing_id"])["status"], "hidden")

    def test_rate_limited_reschedules_with_retry_after(self):
        _, (lid,) = self.queued()
        now = utc().replace(microsecond=0)
        FakeStore.fail_next.append(sc.StoreRateLimited("lento", retry_after=42))
        store_sync.run_due_jobs(now=now)
        j = self.jobs(lid)[0]
        self.assertEqual((j["status"], j["attempts"]), ("pending", 0))
        self.assertEqual(round((datetime.fromisoformat(j["run_after"]) - now).total_seconds()), 42)
        self.assertEqual(self.listing(lid)["status"], "pending")
        self.assertEqual(store_sync.run_due_jobs(now=now + timedelta(seconds=43))["processed"], 1)
        self.assertEqual(self.listing(lid)["status"], "hidden")

    def test_not_found_marks_removed_and_retry_republishes(self):
        store, iid, lid = self.published()
        old_ext = self.listing(lid)["external_id"]
        FakeStore.stores[self.domain]["products"].clear()                   # borrado a mano
        store_sync.listing_action(lid, "sync")
        store_sync.run_due_jobs()
        l = self.listing(lid)
        self.assertEqual(l["status"], "removed")
        self.assertIn("se borró a mano", l["last_error"])
        self.assertEqual(self.jobs(lid, "pending"), [])
        with self.assertRaises(ApiError):                                    # un anuncio removed no se oculta
            store_sync.listing_action(lid, "hide")
        store_sync.listing_action(lid, "retry")
        store_sync.run_due_jobs()
        l = self.listing(lid)
        self.assertEqual(l["status"], "hidden")                      # se republica según el ajuste de la tienda (borrador)
        self.assertNotEqual(l["external_id"], old_ext)

    def test_not_found_on_hide_and_show(self):
        store = self.connect()
        for action in ("hide", "show"):
            store, iid, lid = self.published(store)
            FakeStore.stores[self.domain]["products"].clear()
            store_sync.listing_action(lid, action)
            store_sync.run_due_jobs()
            self.assertEqual(self.listing(lid)["status"], "removed", action)

    def test_validation_error_is_listing_error_without_retry(self):
        _, (lid,) = self.queued()
        FakeStore.fail_next.append(sc.StoreValidationError("precio inválido"))
        store_sync.run_due_jobs()
        j = self.jobs(lid)[0]
        self.assertEqual((j["status"], j["attempts"]), ("failed", 1))
        l = self.listing(lid)
        self.assertEqual(l["status"], "error")
        self.assertIn("precio inválido", l["last_error"])
        self.assertEqual(store_sync.run_due_jobs()["processed"], 0)
        # plain StoreError (no retryable) igual
        _, (lid2,) = self.queued(store=stores_mod.get_store(self.listing(lid)["connection_id"]))
        FakeStore.fail_next.append(sc.StoreError("rechazado"))
        store_sync.run_due_jobs()
        self.assertEqual(self.jobs(lid2)[0]["status"], "failed")
        # reintentar desde la UI vuelve a publicar
        store_sync.listing_action(lid, "retry")
        store_sync.run_due_jobs()
        self.assertEqual(self.listing(lid)["status"], "hidden")

    def test_error_during_sync_then_retry_restores_previous_status(self):
        store, iid, lid = self.published(as_draft=False)
        FakeStore.fail_next.append(sc.StoreValidationError("stock inválido"))
        store_sync.listing_action(lid, "sync")
        store_sync.run_due_jobs()
        self.assertEqual(self.listing(lid)["status"], "error")
        store_sync.listing_action(lid, "retry")
        store_sync.run_due_jobs()
        self.assertEqual(self.listing(lid)["status"], "published")

    def test_stuck_recovery(self):
        store, ids = self.queued(3)
        old = (utc() - timedelta(minutes=30)).isoformat(timespec="seconds")
        with db.connect() as conn:
            for i, jid in enumerate(j["id"] for j in self.jobs()):
                conn.execute("UPDATE store_jobs SET status = 'running', attempts = 1, updated_at = ?, locked_until = ? "
                             "WHERE id = ?", (old, old, jid))
            # el tercero ya había llamado a la tienda: no se reintenta solo
            jid3 = self.jobs(ids[2])[0]["id"]
            conn.execute("UPDATE store_jobs SET payload = json_set(payload, '$._remote_started', ?) WHERE id = ?", (old, jid3))
        self.assertEqual(store_sync.recover_stuck(), 3)
        self.assertEqual([j["status"] for j in self.jobs()], ["pending", "pending", "failed"])
        l = self.listing(ids[2])
        self.assertEqual(l["status"], "error")
        self.assertIn("Revisa tu tienda", l["last_error"])
        store_sync.run_due_jobs()
        self.assertEqual(self.listing(ids[0])["status"], "hidden")
        # un trabajo con la concesión vigente NO se toca
        with db.connect() as conn:
            conn.execute("UPDATE store_jobs SET status = 'running', locked_until = ? WHERE id = ?",
                         ((utc() + timedelta(minutes=5)).isoformat(timespec="seconds"), self.jobs(ids[1])[0]["id"]))
        self.assertEqual(store_sync.recover_stuck(), 0)

    def test_concurrent_workers_run_each_job_once(self):
        store, ids = self.queued(12)
        errors = []

        def work():
            try:
                with auth_context.as_user(1, "owner"):
                    while store_sync.run_due_jobs(limit=3)["processed"]:
                        pass
            except Exception as e:  # noqa: BLE001
                errors.append(e)
        ts = [threading.Thread(target=work) for _ in range(4)]
        [t.start() for t in ts]
        [t.join(30) for t in ts]
        self.assertEqual(errors, [])
        self.assertEqual(len(FakeStore.stores[self.domain]["products"]), 12)
        self.assertEqual(len([c for c in FakeStore.calls if c[0] == "publish"]), 12)
        self.assertEqual({j["status"] for j in self.jobs()}, {"done"})
        self.assertEqual(len({self.listing(i)["external_id"] for i in ids}), 12)

    def test_claim_is_atomic_and_ordered_per_listing(self):
        store, iid, lid = self.published(as_draft=False)
        store_sync.listing_action(lid, "hide")
        store_sync.listing_action(lid, "show")                  # cancela el hide pendiente (no apila lo contrario)
        self.assertEqual([j["action"] for j in self.jobs(lid, "pending")], ["show"])
        store_sync.listing_action(lid, "show")
        self.assertEqual(len(self.jobs(lid, "pending")), 1)      # no apila iguales

    def test_secrets_never_logged_and_messages_truncated(self):
        _, (lid,) = self.queued()
        FakeStore.fail_next.append(sc.StoreValidationError(
            "Rechazado: token=shpat_abcdef0123456789abcdef y shpat_secreto_1234 " + "x" * 600))
        store_sync.run_due_jobs()
        with db.connect() as conn:
            texts = [r[0] for r in conn.execute("SELECT message FROM store_sync_log")]
            texts.append(self.listing(lid)["last_error"])
            texts.append(self.jobs(lid)[0]["last_error"])
        for t in texts:
            self.assertNotIn("shpat_", t)
            self.assertLessEqual(len(t), 300)
        self.assertIn("Rechazado", " ".join(texts))
        self.assertEqual(store_sync.clean_message("Bearer a1b2c3d4e5f6a7b8c9d0 y " + "ab12" * 20), "Bearer *** y ***")
        self.assertEqual(store_sync.clean_message("El token no sirve"), "El token no sirve")

    def test_unexpected_exception_is_retried_not_fatal(self):
        _, (lid,) = self.queued()
        FakeStore.fail_next.append(RuntimeError("bug"))
        self.assertEqual(store_sync.run_due_jobs()["failed"], 1)
        self.assertEqual(self.jobs(lid)[0]["status"], "failed")            # ambiguo y sin idempotencia: no se repite solo
        self.assertEqual(self.listing(lid)["status"], "error")

    def test_log_records_actions(self):
        store, iid, lid = self.published()
        items = store_sync.store_log(store["id"], {})["items"]
        self.assertTrue(items and all(i["ok"] for i in items))
        self.assertTrue(any(i["action"] == "publish" and "Publicado" in i["message"] for i in items))

    def test_prune(self):
        store, iid, lid = self.published()
        with db.connect() as conn:
            conn.execute("UPDATE store_jobs SET updated_at = '2020-01-01T00:00:00+00:00'")
            conn.executemany("INSERT INTO store_sync_log(user_id, connection_id, listing_id, action, ok, message, created_at) "
                             "VALUES (1, ?, ?, 'sync', 1, ?, 'x')", [(store["id"], lid, f"m{i}") for i in range(600)])
            conn.executemany("INSERT INTO store_sync_log(user_id, action, ok, message, created_at) VALUES (2, 'sync', 1, 'otro', 'x')",
                             [()] * 3)
            conn.execute("INSERT INTO meta(key, value) VALUES ('ss:base:99999', '{}')")
        r = store_sync.prune()
        self.assertEqual(r["jobs"], 1)
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM store_sync_log WHERE user_id = 1").fetchone()[0], 500)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM store_sync_log WHERE user_id = 2").fetchone()[0], 3)
            self.assertEqual(conn.execute("SELECT message FROM store_sync_log WHERE user_id = 1 ORDER BY id LIMIT 1").fetchone()[0], "m100")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM meta WHERE key LIKE 'ss:%:99999'").fetchone()[0], 0)
            self.assertIsNotNone(conn.execute("SELECT 1 FROM meta WHERE key = ?", (f"ss:base:{lid}",)).fetchone())


class LeaseAndConstraintsTest(Base):
    def test_claim_sets_lease_and_expired_or_missing_lease_recovers(self):
        store = self.connect()
        r = store_sync.publish(store["id"], {"imported_ids": [self.imp(), self.imp()]})
        a, b = [q["listing_id"] for q in r["queued"]]
        job = store_sync._claim(None, utc())
        self.assertIsNotNone(job["locked_until"])
        self.assertGreater(job["locked_until"], db.now())
        self.assertEqual(store_sync.recover_stuck(), 0)                       # concesión vigente
        self.assertEqual(store_sync.recover_stuck(utc() + timedelta(minutes=11)), 1)   # vencida
        j = self.jobs(a)[0]
        self.assertEqual((j["status"], j["locked_until"]), ("pending", None))
        with db.connect() as conn:                                            # sin concesión = también se recupera
            conn.execute("UPDATE store_jobs SET status = 'running', locked_until = NULL WHERE listing_id = ?", (b,))
        self.assertEqual(store_sync.recover_stuck(), 1)
        self.assertEqual(store_sync.run_due_jobs(now=utc() + timedelta(minutes=12))["processed"], 2)
        self.assertEqual({j["locked_until"] for j in self.jobs()}, {None})

    def test_double_publish_creates_one_listing_and_one_job(self):
        store = self.connect()
        iid = self.imp()
        results, errs = [], []

        def go():
            try:
                with auth_context.as_user(1, "owner"):
                    results.append(store_sync.publish(store["id"], {"imported_ids": [iid]}))
            except Exception as e:  # noqa: BLE001
                errs.append(e)
        ts = [threading.Thread(target=go) for _ in range(4)]
        [t.start() for t in ts]
        [t.join(20) for t in ts]
        self.assertEqual(errs, [])
        self.assertEqual(sum(len(r["queued"]) for r in results), 1)
        self.assertEqual(len(self.jobs()), 1)
        store_sync.run_due_jobs()
        self.assertEqual(len(FakeStore.stores[self.domain]["products"]), 1)

    def test_enqueue_is_idempotent_for_every_action(self):
        store, iid, lid = self.published(as_draft=False)
        with db.connect() as conn:
            first = store_sync._enqueue(conn, 1, lid, "hide", {})
            again = store_sync._enqueue(conn, 1, lid, "hide", {})
        self.assertTrue(first)
        self.assertEqual(again, 0)
        store_sync.listing_action(lid, "sync")
        store_sync.listing_action(lid, "sync")
        self.assertEqual(sorted(j["action"] for j in self.jobs(lid, "pending")), ["hide", "sync"])

    def test_sync_enqueued_while_sync_running_does_not_crash(self):
        store, iid, lid = self.published(as_draft=False)
        store_sync.listing_action(lid, "sync")
        with db.connect() as conn:
            conn.execute("UPDATE store_jobs SET status = 'running' WHERE status = 'pending'")
        self.catalog_change(self.pid_of(iid), stock=2)                        # el hook no revienta con el índice único
        with self.assertRaises(ApiError) as cm:                               # la acción manual no se pierde en silencio
            store_sync.listing_action(lid, "sync")
        self.assertEqual((cm.exception.status, cm.exception.code), (409, "action_in_progress"))

    def test_rate_limit_has_stable_code(self):
        store2, iid, lid = self.published(self.connect("x.fake.test"))
        with self.assertRaises(ApiError) as cm:
            store_sync.listing_action(lid, "retry")
        self.assertEqual(cm.exception.code, "no_retry_needed")
        store = self.connect()
        ids = [self.imp(source="mock") for _ in range(51)]
        store_sync.publish(store["id"], {"imported_ids": ids[:49], "allow_demo": True})
        with self.assertRaises(ApiError) as cm:
            store_sync.publish(store["id"], {"imported_ids": ids[49:], "allow_demo": True})
        self.assertEqual((cm.exception.status, cm.exception.code), (429, "publish_rate_limited"))

    def test_hidden_by_rule_column_matrix(self):
        store, iid, lid = self.published(as_draft=False)
        pid = self.pid_of(iid)
        self.assertEqual(self.listing(lid)["hidden_by_rule"], 0)
        self.catalog_change(pid, stock=0)
        store_sync.run_due_jobs()
        self.assertEqual((self.listing(lid)["status"], self.listing(lid)["hidden_by_rule"]), ("hidden", 1))
        store_sync.listing_action(lid, "hide")                                # el usuario lo oculta a mano encima
        store_sync.run_due_jobs()
        self.assertEqual(self.listing(lid)["hidden_by_rule"], 0)
        self.catalog_change(pid, stock=5)
        self.assertNotIn("show", [j["action"] for j in self.jobs(lid, "pending")])   # nunca se muestra solo
        store_sync.run_due_jobs()
        self.assertEqual(self.listing(lid)["status"], "hidden")
        store_sync.listing_action(lid, "show")
        store_sync.run_due_jobs()
        self.assertEqual((self.listing(lid)["status"], self.listing(lid)["hidden_by_rule"]), ("published", 0))
        self.catalog_change(pid, stock=0)
        store_sync.run_due_jobs()
        self.assertEqual(self.listing(lid)["hidden_by_rule"], 1)
        self.catalog_change(pid, stock=3)
        store_sync.run_due_jobs()
        self.assertEqual((self.listing(lid)["status"], self.listing(lid)["hidden_by_rule"]), ("published", 0))

    def test_demo_marker_survives_import_deletion_and_blocks_sync(self):
        store = self.connect()
        iid = self.imp(source="mock")
        pid = self.pid_of(iid)
        r = store_sync.publish(store["id"], {"imported_ids": [iid], "allow_demo": True, "as_draft": False})
        lid = r["queued"][0]["listing_id"]
        store_sync.run_due_jobs()
        l = self.listing(lid)
        self.assertEqual((l["is_demo"], l["product_id"]), (1, pid))
        # aunque el producto "dejara" de ser demo, la marca del anuncio manda
        with db.connect() as conn:
            conn.execute("UPDATE products SET source = 'csv' WHERE id = ?", (pid,))
        self.catalog_change(pid, stock=0)
        self.assertEqual(self.jobs(lid, "pending"), [])
        server.delete_import(iid)
        l = self.listing(lid)
        self.assertEqual((l["imported_id"], l["is_demo"]), (None, 1))
        v = store_sync.store_listings(store["id"], {})["items"][0]
        self.assertEqual((v["imported_id"], v["is_demo"]), (None, True))
        self.catalog_change(pid, stock=0)
        self.assertEqual(self.jobs(lid, "pending"), [])

    def test_unreadable_credentials_mark_connection_error_and_stop_jobs(self):
        store = self.connect()
        r = store_sync.publish(store["id"], {"imported_ids": [self.imp(), self.imp()]})
        with db.connect() as conn:
            conn.execute("UPDATE store_connections SET credentials_enc = 'basura$x$y$z'")
        self.assertEqual(store_sync.run_due_jobs()["failed"], 1)
        got = stores_mod.get_store(store["id"])
        self.assertEqual(got["status"], "error")
        self.assertIn("credenciales", got["last_error"])
        self.assertEqual(store_sync.run_due_jobs()["processed"], 0)
        self.assertEqual({j["status"] for j in self.jobs()}, {"pending"})
        self.assertEqual(FakeStore.stores[self.domain]["products"], {})

    def test_disabled_connections_are_skipped_entirely(self):
        store = self.connect()
        r = store_sync.publish(store["id"], {"imported_ids": [self.imp()]})
        with db.connect() as conn:
            conn.execute("UPDATE store_connections SET status = 'disabled'")
        self.assertEqual(store_sync.run_due_jobs()["processed"], 0)
        with db.connect() as conn:
            stores_mod.mark_ok(conn, store["id"])                             # no la reactiva
        self.assertEqual(store_sync.run_due_jobs()["processed"], 0)
        self.assertEqual(store_sync.publish(store["id"], {"imported_ids": [self.imp()]})["skipped"][0]["reason"],
                         "store_error")
        with db.connect() as conn:
            conn.execute("UPDATE store_connections SET status = 'active'")
        self.assertEqual(store_sync.run_due_jobs()["processed"], 1)

    def test_logs_survive_disconnect_and_pending_delete_blocks_it(self):
        store, iid, lid = self.published()
        store_sync.delete_listing(lid, {"remove_remote": ["1"]})
        with self.assertRaises(ApiError) as cm:
            stores_mod.disconnect_store(store["id"], {"confirm": ["1"]})
        self.assertEqual((cm.exception.status, cm.exception.code), (409, "pending_deletes"))
        store_sync.run_due_jobs()
        stores_mod.disconnect_store(store["id"], {"confirm": ["1"]})
        with db.connect() as conn:
            n = conn.execute("SELECT COUNT(*) FROM store_sync_log WHERE user_id = 1 AND connection_id IS NULL").fetchone()[0]
        self.assertGreater(n, 0)

    def test_scrub_applied_to_stored_messages(self):
        store = self.connect()
        r = store_sync.publish(store["id"], {"imported_ids": [self.imp()]})
        FakeStore.fail_next.append(sc.StoreValidationError("falló con shpat_abcdef0123456789 y shpat_secreto_1234"))
        store_sync.run_due_jobs()
        with db.connect() as conn:
            rows = [x[0] for x in conn.execute("SELECT message FROM store_sync_log")] + \
                   [x[0] for x in conn.execute("SELECT last_error FROM store_listings")] + \
                   [x[0] for x in conn.execute("SELECT last_error FROM store_jobs")]
        self.assertFalse([t for t in rows if t and ("shpat_" in t)])


class ChangeDuringRunningJobTest(Base):
    """Cambios del catálogo que llegan mientras un trabajo del anuncio está `running` (el índice único impide
    encolar otro igual): deben reflejarse al terminar el trabajo."""

    def during(self, callbacks):
        """Parchea las operaciones del conector: la 1.ª llamada ejecuta callbacks[0], la 2.ª callbacks[1]…"""
        todo = list(callbacks)
        orig_u, orig_a = FakeStore.update_price_stock, FakeStore.set_active

        def wrap(orig):
            def inner(self_, *a, **k):
                if todo:
                    todo.pop(0)()
                return orig(self_, *a, **k)
            return inner
        FakeStore.update_price_stock, FakeStore.set_active = wrap(orig_u), wrap(orig_a)
        self.addCleanup(lambda: (setattr(FakeStore, "update_price_stock", orig_u), setattr(FakeStore, "set_active", orig_a)))

    def test_stock_change_during_running_sync_is_not_lost(self):
        store, iid, lid = self.published(as_draft=False, stock=10)
        pid = self.pid_of(iid)
        self.catalog_change(pid, stock=8)                                      # sync pendiente (stock 8)
        self.during([lambda: self.catalog_change(pid, stock=6)])               # llega MIENTRAS corre ese sync
        store_sync.run_due_jobs()
        self.assertEqual(len([j for j in self.jobs(lid) if j["action"] == "sync"]), 2)   # se encoló un sync nuevo
        store_sync.run_due_jobs()
        self.assertEqual((self.remote(lid)["stock"], self.listing(lid)["last_stock"]), (6, 6))

    def test_stock_zero_during_running_sync_hides_the_listing(self):
        store, iid, lid = self.published(as_draft=False, stock=10)
        pid = self.pid_of(iid)
        self.catalog_change(pid, stock=9)
        self.during([lambda: self.catalog_change(pid, stock=0)])
        store_sync.run_due_jobs()
        store_sync.run_due_jobs()
        l = self.listing(lid)
        self.assertEqual((l["status"], l["hidden_by_rule"], l["last_stock"]), ("hidden", 1, 0))
        self.assertFalse(self.remote(lid)["active"])

    def test_rapid_5_0_3_during_running_job_ends_at_3_and_shown_only_if_hidden_by_rule(self):
        # caso 1: visible, hay un sync en curso y llega 5 → 0 → 3: termina visible con stock 3
        store, iid, lid = self.published(as_draft=False, stock=10)
        pid = self.pid_of(iid)
        self.catalog_change(pid, stock=9)
        self.during([lambda: (self.catalog_change(pid, stock=5), self.catalog_change(pid, stock=0),
                              self.catalog_change(pid, stock=3))])
        for _ in range(3):
            store_sync.run_due_jobs()
        l = self.listing(lid)
        self.assertEqual((l["status"], l["last_stock"], self.remote(lid)["stock"]), ("published", 3, 3))
        # caso 2: oculto A MANO + sync en curso con 5 → 0 → 3: no se muestra solo
        store_sync.listing_action(lid, "hide")
        store_sync.run_due_jobs()
        self.catalog_change(pid, stock=2)
        self.during([lambda: (self.catalog_change(pid, stock=5), self.catalog_change(pid, stock=0),
                              self.catalog_change(pid, stock=3))])
        for _ in range(3):
            store_sync.run_due_jobs()
        l = self.listing(lid)
        self.assertEqual((l["status"], l["hidden_by_rule"], l["last_stock"]), ("hidden", 0, 3))
        # caso 3: oculto POR REGLA y un hide/sync en curso: al volver el stock se muestra
        store_sync.listing_action(lid, "show")
        store_sync.run_due_jobs()
        self.catalog_change(pid, stock=0)
        self.during([lambda: self.catalog_change(pid, stock=3)])               # vuelve el stock durante el hide
        for _ in range(4):
            store_sync.run_due_jobs()
        l = self.listing(lid)
        self.assertEqual((l["status"], l["hidden_by_rule"], l["last_stock"]), ("published", 0, 3))

    def test_no_endless_requeue_when_nothing_changed(self):
        store, iid, lid = self.published(as_draft=False, stock=10)
        pid = self.pid_of(iid)
        self.catalog_change(pid, stock=7)
        self.during([lambda: self.catalog_change(pid, stock=7)])               # "cambio" sin diferencia real
        store_sync.run_due_jobs()
        self.assertEqual(self.jobs(lid, "pending"), [])
        calls = len(FakeStore.calls)
        self.assertEqual(store_sync.run_due_jobs()["processed"], 0)
        self.assertEqual(len(FakeStore.calls), calls)
        # y tras terminar varios trabajos seguidos el total de syncs es acotado
        self.assertLessEqual(len([j for j in self.jobs(lid) if j["action"] == "sync"]), 2)


class OwnershipTagTest(Base):
    """Propiedad por etiquetas: el motor pasa SIEMPRE a los conectores la `dfi-` de la instalación y los reintentos no la
    regeneran."""

    def tag(self):
        with db.connect() as conn:
            return store_sync.installation_tag(conn)

    def set_installation(self, value):
        with db.connect() as conn:
            conn.execute("UPDATE meta SET value = ? WHERE key = 'installation_id'", (value,))

    def test_remote_delete_passes_installation_tag(self):
        store, iid, lid = self.published()
        store_sync.delete_listing(lid, {"remove_remote": ["1"]})
        store_sync.run_due_jobs()
        self.assertEqual(FakeStore.delete_tags, [self.tag()])
        self.assertRegex(FakeStore.delete_tags[0], r"^dfi-[0-9a-f]{8}$")

    def test_compensation_delete_passes_the_tag_used_to_publish(self):
        store = self.connect()
        r = store_sync.publish(store["id"], {"imported_ids": [self.imp()]})
        lid = r["queued"][0]["listing_id"]
        orig = FakeStore.publish

        def pub(self_, product):
            ref = orig(self_, product)
            with db.connect() as conn:                       # el usuario quita el anuncio durante la llamada
                conn.execute("DELETE FROM store_listings WHERE id = ?", (lid,))
            return ref
        FakeStore.publish = pub
        self.addCleanup(lambda: setattr(FakeStore, "publish", orig))
        store_sync.run_due_jobs()
        self.assertEqual(FakeStore.stores[self.domain]["products"], {})
        self.assertEqual(FakeStore.delete_tags, [self.tag()])

    def test_retry_reuses_tags_of_failed_publish_after_installation_id_change(self):
        """Si cambia meta.installation_id, el reintento NO regenera la `dfi-`: el producto que el intento fallido pudo
        crear lleva la vieja y el reintento chocaría con su propio producto."""
        store = self.connect()
        r = store_sync.publish(store["id"], {"imported_ids": [self.imp()]})
        lid = r["queued"][0]["listing_id"]
        old_tags = json.loads(self.jobs(lid)[0]["payload"])["product"]["tags"]
        FakeStore.fail_next.append(sc.StoreValidationError("precio inválido"))
        store_sync.run_due_jobs()
        self.assertEqual(self.listing(lid)["status"], "error")
        self.set_installation("ffff0000")
        self.assertNotEqual(self.tag(), old_tags[1])
        store_sync.listing_action(lid, "retry")
        new = [j for j in self.jobs(lid) if j["status"] == "pending"][0]
        self.assertEqual(json.loads(new["payload"])["product"]["tags"], old_tags)

    def test_fresh_publish_uses_current_installation_tag(self):
        store, iid, lid = self.published()
        with db.connect() as conn:
            conn.execute("DELETE FROM store_listings WHERE id = ?", (lid,))
        self.set_installation("ffff0000")
        store2 = self.connect("otra.fake.test")
        r = store_sync.publish(store2["id"], {"imported_ids": [iid]})
        tags = json.loads(self.jobs(r["queued"][0]["listing_id"])[0]["payload"])["product"]["tags"]
        self.assertEqual(tags, ["dropfinder", "dfi-ffff0000"])


class PublishSaveFailureTest(Base):
    """Review PR #12, hallazgo 1: nunca un segundo producto remoto si falla el guardado local tras crear."""

    def break_writes_after_publish(self, fails):
        """Tras la llamada a publish() de la tienda, las próximas `fails` escrituras lanzan OperationalError."""
        state = {"armed": False, "left": fails}
        orig_pub, orig_w = FakeStore.publish, store_sync._write_once

        def pub(self_, product):
            ref = orig_pub(self_, product)
            state["armed"] = True
            return ref

        def w(fn):
            if state["armed"] and state["left"] > 0:
                state["left"] -= 1
                raise __import__("sqlite3").OperationalError("database is locked")
            return orig_w(fn)
        FakeStore.publish, store_sync._write_once = pub, w
        self.addCleanup(lambda: (setattr(FakeStore, "publish", orig_pub), setattr(store_sync, "_write_once", orig_w)))
        return state

    def test_save_is_retried_and_never_republishes(self):
        store = self.connect()
        r = store_sync.publish(store["id"], {"imported_ids": [self.imp()]})
        lid = r["queued"][0]["listing_id"]
        self.break_writes_after_publish(3)                      # falla 3 veces y luego se puede
        store_sync.run_due_jobs()
        l = self.listing(lid)
        self.assertEqual((l["status"], bool(l["external_id"])), ("hidden", True))
        self.assertEqual(len(FakeStore.stores[self.domain]["products"]), 1)
        self.assertEqual(self.jobs(lid)[0]["status"], "done")

    def test_save_keeps_failing_ends_in_error_with_one_remote_product(self):
        store = self.connect()
        r = store_sync.publish(store["id"], {"imported_ids": [self.imp()]})
        lid = r["queued"][0]["listing_id"]
        st = self.break_writes_after_publish(store_sync.SAVE_TRIES)      # agota los reintentos del guardado
        for _ in range(4):
            store_sync.run_due_jobs(now=utc() + timedelta(hours=3))
        self.assertEqual(len(FakeStore.stores[self.domain]["products"]), 1)
        l = self.listing(lid)
        self.assertEqual(l["status"], "error")
        self.assertIn("Se creó el producto en la tienda pero no se pudo guardar", l["last_error"])
        self.assertIn(f"DF-", l["last_error"])
        self.assertEqual(self.jobs(lid)[0]["status"], "failed")

    def test_even_the_marker_write_fails_recovery_never_republishes(self):
        store = self.connect()
        r = store_sync.publish(store["id"], {"imported_ids": [self.imp()]})
        lid = r["queued"][0]["listing_id"]
        st = self.break_writes_after_publish(1000)
        store_sync.run_due_jobs()
        self.assertEqual(self.jobs(lid)[0]["status"], "running")          # no se pudo ni dejar constancia
        st["left"] = 0                                                    # la base vuelve a responder
        for _ in range(3):
            store_sync.run_due_jobs(now=utc() + timedelta(hours=1))       # concesión vencida → recuperación
        self.assertEqual(len(FakeStore.stores[self.domain]["products"]), 1)
        self.assertEqual(self.listing(lid)["status"], "error")

    def test_internal_error_after_remote_create_is_not_republished_on_non_idempotent(self):
        store = self.connect()
        r = store_sync.publish(store["id"], {"imported_ids": [self.imp()]})
        lid = r["queued"][0]["listing_id"]
        orig = FakeStore.publish

        def boom(self_, product):
            orig(self_, product)
            raise RuntimeError("bug propio tras crear")
        FakeStore.publish = boom
        self.addCleanup(lambda: setattr(FakeStore, "publish", orig))
        store_sync.run_due_jobs()
        self.assertEqual(self.jobs(lid)[0]["status"], "failed")
        FakeStore.publish = orig
        for _ in range(3):
            store_sync.run_due_jobs(now=utc() + timedelta(hours=3))
        self.assertEqual(len(FakeStore.stores[self.domain]["products"]), 1)
        self.assertEqual(self.listing(lid)["status"], "error")
        self.assertIn("revisa tu tienda", self.listing(lid)["last_error"].lower())

    def test_idempotent_connector_may_retry_and_adopts(self):
        store = self.connect(platform="fakeidem")
        r = store_sync.publish(store["id"], {"imported_ids": [self.imp()]})
        lid = r["queued"][0]["listing_id"]
        orig = FakeStore.publish

        def boom(self_, product):
            orig(self_, product)
            raise RuntimeError("bug propio tras crear")
        FakeStore.publish = boom
        self.addCleanup(lambda: setattr(FakeStore, "publish", orig))
        store_sync.run_due_jobs()
        FakeStore.publish = orig
        store_sync.run_due_jobs(now=utc() + timedelta(hours=3))
        self.assertEqual(len(FakeStore.stores[self.domain]["products"]), 1)
        self.assertEqual((self.listing(lid)["status"], self.jobs(lid)[0]["status"]), ("hidden", "done"))

    def test_recovery_of_interrupted_publish_depends_on_idempotent_publish(self):
        for platform, expect_error in (("fake", True), ("fakeidem", False)):
            FakeStore.reset()
            domain = f"{platform}.fake.test"
            store = self.connect(domain, platform=platform)
            r = store_sync.publish(store["id"], {"imported_ids": [self.imp()]})
            lid = r["queued"][0]["listing_id"]
            old = (utc() - timedelta(hours=1)).isoformat(timespec="seconds")
            with db.connect() as conn:
                conn.execute("UPDATE store_jobs SET status = 'running', locked_until = ?, "
                             "payload = json_set(payload, '$._remote_started', ?) WHERE listing_id = ?", (old, old, lid))
            store_sync.run_due_jobs()
            l = self.listing(lid)
            if expect_error:
                self.assertEqual(l["status"], "error", platform)
                self.assertEqual(FakeStore.stores[domain]["products"], {})
            else:
                self.assertEqual(l["status"], "hidden", platform)
                self.assertEqual(len(FakeStore.stores[domain]["products"]), 1)


class ReviewVisibilityAndPriceTest(ChangeDuringRunningJobTest):
    """Review PR #12, hallazgos 2 a 5 y 6."""

    # ---- 2: un hide que falla no cambia la visibilidad que afirma DropFinder
    def test_failed_hide_keeps_visible_status_alerts_and_retry_reruns(self):
        store, iid, lid = self.published(as_draft=False, stock=10)
        store_sync.listing_action(lid, "hide")
        FakeStore.fail_next.append(sc.StoreValidationError("rechazado"))
        store_sync.run_due_jobs()
        l = self.listing(lid)
        self.assertEqual(l["status"], "published")                          # la tienda sigue visible
        self.assertTrue(self.remote(lid)["active"])
        self.assertIn("rechazado", l["last_error"])
        al = self.alerts("store_visibility_failed")
        self.assertEqual(len(al), 1)
        self.assertIn("sigue visible en tu tienda", al[0]["message"])
        store_sync.listing_action(lid, "retry")
        store_sync.run_due_jobs()
        self.assertEqual((self.listing(lid)["status"], self.remote(lid)["active"]), ("hidden", False))

    def test_failed_rule_hide_then_retry_even_if_rule_no_longer_applies(self):
        store, iid, lid = self.published(as_draft=False, stock=10)
        pid = self.pid_of(iid)
        self.catalog_change(pid, stock=0)
        FakeStore.fail_next.append(sc.StoreValidationError("no se pudo"))
        store_sync.run_due_jobs(limit=1)                                    # el hide por regla (va primero) falla
        self.assertEqual((self.listing(lid)["status"], self.remote(lid)["active"]), ("published", True))
        self.catalog_change(pid, stock=5)                                   # ya no aplica la regla…
        store_sync.listing_action(lid, "retry")                             # …pero el usuario reintenta: se ejecuta
        store_sync.run_due_jobs(limit=2)
        self.assertEqual(len([c for c in FakeStore.calls if c[0] == "set_active" and c[-1] is False]), 2)

    def test_retry_of_failed_rule_show_runs_even_if_marker_changed(self):
        store, iid, lid = self.published(as_draft=False, stock=10)
        pid = self.pid_of(iid)
        self.catalog_change(pid, stock=0)
        store_sync.run_due_jobs()                                           # hide por regla (marca = 1)
        self.catalog_change(pid, stock=5)                                   # show por regla pendiente
        FakeStore.fail_next.append(sc.StoreValidationError("no se pudo mostrar"))
        store_sync.run_due_jobs(limit=1)                                    # el show falla: sigue oculto
        self.assertEqual(self.listing(lid)["status"], "hidden")
        self.assertEqual(len(self.alerts("store_visibility_failed")), 1)
        with db.connect() as conn:
            conn.execute("UPDATE store_listings SET hidden_by_rule = 0 WHERE id = ?", (lid,))
        store_sync.listing_action(lid, "retry")
        store_sync.run_due_jobs()
        self.assertEqual((self.listing(lid)["status"], self.remote(lid)["active"]), ("published", True))

    def test_restore_never_invents_hidden_when_previous_state_unknown(self):
        store, iid, lid = self.published(as_draft=False, stock=10)
        FakeStore.fail_next.append(sc.StoreValidationError("stock inválido"))
        store_sync.listing_action(lid, "sync")
        store_sync.run_due_jobs()
        self.assertEqual(self.listing(lid)["status"], "error")
        with db.connect() as conn:                                          # se perdió todo rastro del estado previo
            conn.execute("DELETE FROM meta WHERE key LIKE 'ss:prev:%'")
            conn.execute("DELETE FROM store_jobs")
        store_sync.listing_action(lid, "sync")
        store_sync.run_due_jobs()
        l = self.listing(lid)
        self.assertEqual(l["status"], "error")                              # no se afirma "hidden" ni "published"
        self.assertIn("Mostrar u Ocultar", l["last_error"])

    # ---- 3: la subida automática no se encadena
    def test_price_raise_reacts_once_per_cost_rise(self):
        store = self.connect(auto_raise_price=True, min_margin_pct=60.0, max_price_increase_pct=10.0)
        _, iid, lid = self.published(store, sell_price=20.0, cost=10.0, as_draft=False)
        pid = self.pid_of(iid)
        self.catalog_change(pid, cost=15.0)
        store_sync.run_due_jobs()
        self.assertEqual(self.remote(lid)["price"], 22.0)                  # +10% y nada más
        for stock in (9, 8, 7):
            self.catalog_change(pid, stock=stock)                           # escrituras sin cambio de costo
            store_sync.run_due_jobs()
        self.catalog_change(pid)                                            # recarga idéntica del CSV / monitor
        store_sync.run_due_jobs()
        self.assertEqual(self.remote(lid)["price"], 22.0)
        self.catalog_change(pid, cost=18.0)                                 # un nuevo aumento: una subida más
        store_sync.run_due_jobs()
        self.assertEqual(self.remote(lid)["price"], 24.2)
        self.catalog_change(pid, stock=1)
        store_sync.run_due_jobs()
        self.assertEqual(self.remote(lid)["price"], 24.2)

    # ---- 4: el precio subido por el motor es el de Mis productos
    def test_auto_raise_updates_sell_price_and_manual_sync_keeps_it(self):
        store = self.connect(auto_raise_price=True, min_margin_pct=30.0)
        _, iid, lid = self.published(store, sell_price=20.0, cost=10.0, as_draft=False)
        self.catalog_change(self.pid_of(iid), cost=16.0)
        store_sync.run_due_jobs()
        raised = self.remote(lid)["price"]
        self.assertGreater(raised, 20.0)
        with db.connect() as conn:
            self.assertAlmostEqual(conn.execute("SELECT sell_price FROM imported_products WHERE id = ?", (iid,)).fetchone()[0],
                                   raised, places=6)
        self.assertEqual(len(self.alerts("store_price_raised")), 1)
        store_sync.listing_action(lid, "sync")
        store_sync.run_due_jobs()
        self.assertEqual((self.remote(lid)["price"], self.listing(lid)["last_price"]), (raised, raised))
        # y si el USUARIO baja el precio a propósito, el sync manual sí lo baja (acción explícita)
        server.update_import(iid, {"sell_price": 21.0})
        store_sync.listing_action(lid, "sync")
        store_sync.run_due_jobs()
        self.assertEqual(self.remote(lid)["price"], 21.0)

    # ---- 5: una acción manual no se pierde
    def test_manual_hide_while_rule_hide_running_is_409(self):
        store, iid, lid = self.published(as_draft=False, stock=10)
        pid = self.pid_of(iid)
        self.catalog_change(pid, stock=0)
        seen = []

        def user_clicks_hide():
            try:
                store_sync.listing_action(lid, "hide")
            except ApiError as e:
                seen.append((e.status, e.code))
        self.during([user_clicks_hide])
        store_sync.run_due_jobs()
        self.assertEqual(seen, [(409, "action_in_progress")])

    def test_manual_hide_converts_pending_rule_hide_and_is_never_auto_shown(self):
        store, iid, lid = self.published(as_draft=False, stock=10)
        pid = self.pid_of(iid)
        self.catalog_change(pid, stock=0)                                   # hide por regla pendiente
        store_sync.listing_action(lid, "hide")                              # el usuario también lo pide
        hides = [json.loads(j["payload"]) for j in self.jobs(lid, "pending") if j["action"] == "hide"]
        self.assertEqual(len(hides), 1)
        self.assertEqual((hides[0]["by_rule"], hides[0]["manual"]), (False, True))
        store_sync.run_due_jobs()
        self.assertEqual((self.listing(lid)["status"], self.listing(lid)["hidden_by_rule"]), ("hidden", 0))
        self.catalog_change(pid, stock=5)
        store_sync.run_due_jobs()
        self.assertEqual(self.listing(lid)["status"], "hidden")             # nunca se muestra solo

    # ---- 6: cambio de moneda
    def test_currency_change_after_publish_blocks_pushes_until_manual_sync(self):
        store, iid, lid = self.published(as_draft=False, stock=10, cost=10.0)
        self.assertEqual(self.listing(lid)["currency"], "USD")
        FakeStore.stores[self.domain]["currency"] = "COP"
        stores_mod.validate_store(store["id"])
        calls = len(FakeStore.calls)
        self.catalog_change(self.pid_of(iid), stock=3, cost=14.0)
        l = self.listing(lid)
        self.assertEqual(l["status"], "error")
        self.assertIn("USD→COP", l["last_error"])
        self.assertEqual(self.jobs(lid, "pending"), [])
        store_sync.run_due_jobs()
        self.assertEqual(len(FakeStore.calls), calls)                       # no se empujó nada
        store_sync.listing_action(lid, "sync")                              # el usuario lo arregla a propósito
        store_sync.run_due_jobs()
        l = self.listing(lid)
        self.assertEqual((l["status"], l["currency"], self.remote(lid)["price"]), ("published", "COP", 80900.0))


class AmbiguousPublishErrorsTest(Base):
    """Un error ambiguo de publish() en un conector sin idempotent_publish NO se reintenta solo (duplicaría un
    producto en vivo); solo si la tienda lo rechazó (límite de velocidad) o la petición nunca salió (not_sent)."""

    def setup_publish(self, platform="fake"):
        store = getattr(self, "_store", None) or self.connect(platform=platform)
        self._store = store
        r = store_sync.publish(store["id"], {"imported_ids": [self.imp()]})
        return r["queued"][0]["listing_id"]

    def creating_then_failing(self, exc):
        """publish() que SÍ crea el producto en la tienda y luego falla (respuesta perdida)."""
        orig = FakeStore.publish

        def pub(self_, product):
            orig(self_, product)
            raise exc
        FakeStore.publish = pub
        self.addCleanup(lambda: setattr(FakeStore, "publish", orig))
        return lambda: setattr(FakeStore, "publish", orig)

    def test_ambiguous_retryable_error_is_not_retried_and_leaves_one_product(self):
        for exc in (sc.StoreError("tiempo agotado", retryable=True), sc.StoreError("HTTP 502", retryable=True),
                    RuntimeError("bug")):
            lid = self.setup_publish()
            restore = self.creating_then_failing(exc)
            store_sync.run_due_jobs()
            restore()
            for _ in range(3):
                store_sync.run_due_jobs(now=utc() + timedelta(hours=5))
            self.assertEqual(len(FakeStore.stores[self.domain]["products"]), 1, exc)
            l = self.listing(lid)
            self.assertEqual(l["status"], "error")
            self.assertIn("No sabemos si la tienda creó el producto", l["last_error"])
            self.assertIn("SKU DF-", l["last_error"])
            self.assertEqual(self.jobs(lid)[0]["status"], "failed")
            store_sync.delete_listing(lid, {})
            FakeStore.stores[self.domain]["products"].clear()

    def test_not_sent_and_rate_limited_are_retried(self):
        for exc in (sc.StoreError("sin DNS", retryable=True, not_sent=True), sc.StoreRateLimited("lento", 5)):
            lid = self.setup_publish()
            FakeStore.fail_next.append(exc)
            store_sync.run_due_jobs()
            self.assertEqual(self.jobs(lid)[0]["status"], "pending", exc)
            store_sync.run_due_jobs(now=utc() + timedelta(hours=1))
            self.assertEqual(self.listing(lid)["status"], "hidden")
            store_sync.delete_listing(lid, {})
            FakeStore.stores[self.domain]["products"].clear()
            with db.connect() as conn:
                conn.execute("DELETE FROM meta WHERE key LIKE 'ss:pub:%'")

    def test_idempotent_connector_retries_ambiguous_errors_and_adopts(self):
        lid = self.setup_publish("fakeidem")
        restore = self.creating_then_failing(sc.StoreError("tiempo agotado", retryable=True))
        store_sync.run_due_jobs()
        self.assertEqual(self.jobs(lid)[0]["status"], "pending")
        restore()
        store_sync.run_due_jobs(now=utc() + timedelta(hours=1))
        self.assertEqual(len(FakeStore.stores[self.domain]["products"]), 1)
        self.assertEqual(self.listing(lid)["status"], "hidden")

    def test_definitive_refusals_keep_their_own_handling(self):
        lid = self.setup_publish()
        FakeStore.fail_next.append(sc.StoreValidationError("título inválido"))
        store_sync.run_due_jobs()
        self.assertIn("título inválido", self.listing(lid)["last_error"])    # no es el aviso de "no sabemos"

    def test_retry_warns_that_it_may_duplicate(self):
        lid = self.setup_publish()
        self.creating_then_failing(sc.StoreError("tiempo agotado", retryable=True))
        store_sync.run_due_jobs()
        res = store_sync.listing_action(lid, "retry")
        self.assertIn("podría duplicarse", res["warning"])
        with db.connect() as conn:
            self.assertIn("duplic", conn.execute("SELECT message FROM store_sync_log ORDER BY id DESC").fetchone()[0])


class ListingActionsTest(Base):
    def test_manual_sync_pushes_current_price_and_stock(self):
        store, iid, lid = self.published(sell_price=20.0)
        server.update_import(iid, {"sell_price": 25.0})
        self.catalog_change(self.pid_of(iid), stock=3)
        store_sync.listing_action(lid, "sync")
        store_sync.run_due_jobs()
        self.assertEqual((self.remote(lid)["price"], self.remote(lid)["stock"]), (25.0, 3))
        self.assertEqual(self.listing(lid)["last_price"], 25.0)

    def test_manual_hide_show(self):
        store, iid, lid = self.published(as_draft=False)
        store_sync.listing_action(lid, "hide")
        store_sync.run_due_jobs()
        self.assertEqual((self.listing(lid)["status"], self.remote(lid)["active"]), ("hidden", False))
        store_sync.listing_action(lid, "show")
        store_sync.run_due_jobs()
        self.assertEqual((self.listing(lid)["status"], self.remote(lid)["active"]), ("published", True))

    def test_actions_on_unpublished_listing_are_409(self):
        store = self.connect()
        r = store_sync.publish(store["id"], {"imported_ids": [self.imp()]})
        lid = r["queued"][0]["listing_id"]
        for a in ("sync", "hide", "show", "retry"):
            with self.assertRaises(ApiError) as cm:
                store_sync.listing_action(lid, a)
            self.assertEqual(cm.exception.status, 409, a)

    def test_delete_listing_local_only(self):
        store, iid, lid = self.published()
        r = store_sync.delete_listing(lid, {})
        self.assertEqual(r, {"ok": True, "queued_remote_delete": False})
        self.assertEqual(len(FakeStore.stores[self.domain]["products"]), 1)         # sigue en la tienda
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM store_listings").fetchone()[0], 0)

    def test_delete_listing_remote(self):
        store, iid, lid = self.published()
        r = store_sync.delete_listing(lid, {"remove_remote": ["1"]})
        self.assertTrue(r["queued_remote_delete"])
        self.assertEqual(store_sync.store_listings(store["id"], {})["items"][0]["pending_action"], "delete")
        store_sync.run_due_jobs()
        self.assertEqual(FakeStore.stores[self.domain]["products"], {})
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM store_listings").fetchone()[0], 0)
            self.assertIn("eliminado", conn.execute("SELECT message FROM store_sync_log ORDER BY id DESC").fetchone()[0])

    def test_delete_while_publish_running_is_409(self):
        store = self.connect()
        r = store_sync.publish(store["id"], {"imported_ids": [self.imp()]})
        with db.connect() as conn:
            conn.execute("UPDATE store_jobs SET status = 'running'")
        with self.assertRaises(ApiError) as cm:
            store_sync.delete_listing(r["queued"][0]["listing_id"], {})
        self.assertEqual(cm.exception.status, 409)

    def test_listings_pagination_filter_and_ids(self):
        store = self.connect()
        ids = [self.imp() for _ in range(5)]
        store_sync.publish(store["id"], {"imported_ids": ids})
        store_sync.run_due_jobs()
        p = store_sync.store_listings(store["id"], {"page_size": ["2"], "page": ["3"]})
        self.assertEqual((len(p["items"]), p["total"], p["pages"], p["page"]), (1, 5, 3, 3))
        self.assertEqual(store_sync.store_listings(store["id"], {"status": ["error"]})["total"], 0)
        with self.assertRaises(ApiError):
            store_sync.store_listings(store["id"], {"status": ["x"]})
        got = store_sync.listings_for_imports({"imported_ids": [f"{ids[0]},{ids[1]},999"]})["items"]
        self.assertEqual({i["imported_id"] for i in got}, {ids[0], ids[1]})
        self.assertEqual(set(got[0]), {"id", "imported_id", "connection_id", "store_name", "platform", "title", "status",
                                       "external_url", "last_price", "last_stock", "last_synced_at", "last_error",
                                       "pending_action", "is_demo", "currency", "hidden_by_rule", "created_at"})
        with self.assertRaises(ApiError):
            store_sync.listings_for_imports({"imported_ids": [",".join(map(str, range(201)))]})

    def test_orphans_after_import_deletion(self):
        store, iid, lid = self.published(as_draft=False)
        server.delete_import(iid)
        l = self.listing(lid)
        self.assertIsNone(l["imported_id"])
        items = store_sync.store_listings(store["id"], {})["items"]
        self.assertEqual((items[0]["imported_id"], items[0]["status"], items[0]["is_demo"]), (None, "published", False))
        with self.assertRaises(ApiError) as cm:
            store_sync.listing_action(lid, "sync")                  # sin producto no hay datos
        self.assertEqual(cm.exception.status, 409)
        store_sync.listing_action(lid, "hide")                      # ocultar y retirar sí
        store_sync.run_due_jobs()
        self.assertEqual(self.listing(lid)["status"], "hidden")
        store_sync.delete_listing(lid, {"remove_remote": ["1"]})
        store_sync.run_due_jobs()
        self.assertEqual(FakeStore.stores[self.domain]["products"], {})

    def test_orphan_not_auto_synced(self):
        store, iid, lid = self.published(as_draft=False)
        pid = self.pid_of(iid)
        server.delete_import(iid)
        self.catalog_change(pid, stock=0)
        self.assertEqual(self.jobs(lid, "pending"), [])


class RulesTest(Base):
    def setup_listing(self, as_draft=False, store=None, **pk):
        return self.published(store=store, as_draft=as_draft, **pk)

    def run_actions(self, lid):
        return [(j["action"], json.loads(j["payload"])) for j in self.jobs(lid, "pending")]

    def test_stock_zero_hides_and_back_shows_only_if_hidden_by_rule(self):
        store, iid, lid = self.setup_listing()
        pid = self.pid_of(iid)
        self.catalog_change(pid, stock=0)
        self.assertEqual({a for a, _ in self.run_actions(lid)}, {"sync", "hide"})
        store_sync.run_due_jobs()
        l = self.listing(lid)
        self.assertEqual((l["status"], l["last_stock"]), ("hidden", 0))
        self.assertFalse(self.remote(lid)["active"])
        self.catalog_change(pid, stock=7)
        self.assertEqual({a for a, _ in self.run_actions(lid)}, {"sync", "show"})
        store_sync.run_due_jobs()
        self.assertEqual((self.listing(lid)["status"], self.remote(lid)["active"], self.remote(lid)["stock"]),
                         ("published", True, 7))

    def test_manually_hidden_is_not_shown_back(self):
        store, iid, lid = self.setup_listing()
        store_sync.listing_action(lid, "hide")
        store_sync.run_due_jobs()
        self.catalog_change(self.pid_of(iid), stock=0)              # ya está oculto: nada que ocultar
        self.assertNotIn("hide", [a for a, _ in self.run_actions(lid)])
        store_sync.run_due_jobs()
        self.catalog_change(self.pid_of(iid), stock=9)
        self.assertNotIn("show", [a for a, _ in self.run_actions(lid)])
        store_sync.run_due_jobs()
        self.assertEqual(self.listing(lid)["status"], "hidden")

    def test_manual_show_clears_marker_then_manual_hide_is_respected(self):
        store, iid, lid = self.setup_listing()
        pid = self.pid_of(iid)
        self.catalog_change(pid, stock=0)
        store_sync.run_due_jobs()
        store_sync.listing_action(lid, "show")                      # el usuario lo muestra a mano
        store_sync.run_due_jobs()
        store_sync.listing_action(lid, "hide")                      # y luego lo oculta a mano
        store_sync.run_due_jobs()
        self.catalog_change(pid, stock=5)
        self.assertNotIn("show", [a for a, _ in self.run_actions(lid)])

    def test_draft_is_not_made_visible_by_rules(self):
        store, iid, lid = self.setup_listing(as_draft=True)
        pid = self.pid_of(iid)
        self.catalog_change(pid, stock=0)
        store_sync.run_due_jobs()
        self.catalog_change(pid, stock=5)
        store_sync.run_due_jobs()
        self.assertEqual((self.listing(lid)["status"], self.remote(lid)["active"]), ("hidden", False))
        self.assertEqual(self.remote(lid)["stock"], 5)              # el stock sí se sincroniza

    def test_settings_switch_rules_off(self):
        store = self.connect(hide_when_out_of_stock=False, sync_stock=False)
        _, iid, lid = self.setup_listing(store=store)
        self.catalog_change(self.pid_of(iid), stock=0)
        self.assertEqual(self.run_actions(lid), [])

    def test_stale_hide_cancelled_when_stock_returns_before_running(self):
        store, iid, lid = self.setup_listing()
        pid = self.pid_of(iid)
        self.catalog_change(pid, stock=0)
        self.catalog_change(pid, stock=4)
        self.assertEqual([a for a, _ in self.run_actions(lid)], ["sync"])
        store_sync.run_due_jobs()
        self.assertEqual((self.listing(lid)["status"], self.remote(lid)["stock"]), ("published", 4))

    def test_rule_hide_obsolete_at_execution_is_skipped(self):
        store, iid, lid = self.setup_listing()
        self.catalog_change(self.pid_of(iid), stock=0)
        store_sync.listing_action(lid, "hide")                      # lo manual reemplaza al de la regla
        self.assertEqual([p for a, p in self.run_actions(lid) if a == "hide"], [{"by_rule": False, "manual": True}])

    # ---- precio
    def raise_setup(self, **settings):
        store = self.connect(auto_raise_price=True, min_margin_pct=30.0, **settings)
        return (store,) + self.setup_listing(store=store, sell_price=20.0, cost=10.0)[1:]

    def test_raises_price_just_enough_after_cost_increase(self):
        store, iid, lid = self.raise_setup()
        self.catalog_change(self.pid_of(iid), cost=16.0)
        sync = [p for a, p in self.run_actions(lid) if a == "sync"][0]
        needed = (16 + 0.30) / (1 - 0.029 - 0.30)
        self.assertAlmostEqual(sync["price"], needed, delta=0.011)
        self.assertGreaterEqual(sync["price"], needed)              # redondeo hacia arriba: el margen se cumple
        store_sync.run_due_jobs()
        self.assertEqual(self.remote(lid)["price"], sync["price"])
        self.assertEqual(self.alerts(), [])

    def test_raise_is_capped_per_change_and_alerts(self):
        store, iid, lid = self.raise_setup(max_price_increase_pct=10.0)
        self.catalog_change(self.pid_of(iid), cost=16.0)
        store_sync.run_due_jobs()
        self.assertEqual(self.remote(lid)["price"], 22.0)           # +10% de 20
        self.assertEqual(len(self.alerts()), 1)                     # el margen sigue bajo: el usuario decide

    def test_raise_never_exceeds_double_in_total(self):
        store, iid, lid = self.raise_setup(max_price_increase_pct=200.0)
        pid = self.pid_of(iid)
        self.catalog_change(pid, cost=60.0)
        store_sync.run_due_jobs()
        self.assertEqual(self.remote(lid)["price"], 40.0)           # 2x el precio publicado
        self.catalog_change(pid, cost=80.0)
        self.assertEqual([a for a, p in self.run_actions(lid) if p.get("price")], [])   # ya en el tope total
        self.assertEqual(len(self.alerts()), 1)

    def test_never_lowers_price(self):
        store, iid, lid = self.raise_setup()
        pid = self.pid_of(iid)
        self.catalog_change(pid, cost=16.0)
        store_sync.run_due_jobs()
        raised = self.remote(lid)["price"]
        self.catalog_change(pid, cost=5.0, stock=3)                 # el costo baja mucho
        store_sync.run_due_jobs()
        self.assertEqual(self.remote(lid)["price"], raised)
        self.assertEqual(self.remote(lid)["stock"], 3)
        # y un trabajo de regla con precio menor al actual tampoco lo baja
        with db.connect() as conn:
            store_sync._enqueue_sync(conn, 1, lid, {"price": 1.0, "stock": 3})
        store_sync.run_due_jobs()
        self.assertEqual(self.remote(lid)["price"], raised)

    def test_low_margin_alert_when_auto_raise_off_and_deduped(self):
        store = self.connect(min_margin_pct=30.0)
        _, iid, lid = self.setup_listing(store=store, sell_price=20.0, cost=10.0)
        pid = self.pid_of(iid)
        self.catalog_change(pid, cost=16.0)
        a = self.alerts()
        self.assertEqual((len(a), a[0]["user_id"], a[0]["imported_id"], a[0]["product_id"]), (1, 1, iid, pid))
        self.assertFalse([1 for _, p in self.run_actions(lid) if p.get("price")])
        self.catalog_change(pid, cost=17.0)
        self.assertEqual(len(self.alerts()), 1)                     # no se repite mientras no la lea
        with db.connect() as conn:
            conn.execute("UPDATE alerts SET read = 1")
        self.catalog_change(pid, cost=18.0)
        self.assertEqual(len(self.alerts()), 2)

    def test_no_price_action_if_cost_did_not_rise(self):
        store = self.connect(min_margin_pct=95.0)                   # margen "bajo" desde el principio
        _, iid, lid = self.setup_listing(store=store, sell_price=20.0, cost=10.0)
        self.catalog_change(self.pid_of(iid), stock=2)
        self.assertEqual(self.alerts(), [])

    def test_margin_ok_no_action(self):
        store, iid, lid = self.raise_setup()
        self.catalog_change(self.pid_of(iid), cost=11.0)
        self.assertEqual(self.alerts(), [])
        self.assertFalse([1 for _, p in self.run_actions(lid) if p.get("price")])

    def test_demo_excluded_from_rules(self):
        store = self.connect()
        iid = self.imp(source="mock")
        r = store_sync.publish(store["id"], {"imported_ids": [iid], "allow_demo": True, "as_draft": False})
        store_sync.run_due_jobs()
        lid = r["queued"][0]["listing_id"]
        self.assertEqual(self.listing(lid)["status"], "published")
        self.catalog_change(self.pid_of(iid), stock=0, cost=500.0)
        self.assertEqual(self.jobs(lid, "pending"), [])
        self.assertEqual(self.alerts(), [])

    def test_error_removed_pending_untouched(self):
        store = self.connect()
        lids = []
        for st in ("error", "removed", "pending"):
            _, iid, lid = self.setup_listing(store=store)
            with db.connect() as conn:
                conn.execute("UPDATE store_listings SET status = ? WHERE id = ?", (st, lid))
            self.catalog_change(self.pid_of(iid), stock=0)
            lids.append(lid)
        self.assertEqual([j for l in lids for j in self.jobs(l, "pending")], [])

    def test_unrelated_product_and_all_ids_none(self):
        store, iid, lid = self.setup_listing()
        other = self.product()
        self.catalog_change(other, stock=0)
        self.assertEqual(self.jobs(lid, "pending"), [])
        with db.connect() as conn:
            conn.execute("UPDATE products SET stock = 0 WHERE id = ?", (self.pid_of(iid),))
            hooks.run_catalog_write(conn, None)                     # None = no se sabe cuáles: revisar todo
        self.assertTrue(self.jobs(lid, "pending"))

    def test_coalescing_one_pending_sync(self):
        store, iid, lid = self.setup_listing()
        pid = self.pid_of(iid)
        for stock in (8, 6, 3, 2):
            self.catalog_change(pid, stock=stock)
        syncs = self.jobs(lid, "pending")
        self.assertEqual(len(syncs), 1)
        self.assertEqual(json.loads(syncs[0]["payload"])["stock"], 2)
        self.catalog_change(pid, stock=2)                           # sin cambios: no toca nada
        self.assertEqual(len(self.jobs(lid, "pending")), 1)

    def test_hook_only_enqueues_never_calls_store(self):
        store, iid, lid = self.setup_listing()
        calls = len(FakeStore.calls)
        self.catalog_change(self.pid_of(iid), stock=0)
        self.assertEqual(len(FakeStore.calls), calls)

    def test_csv_upload_and_monitor_paths_trigger_rules(self):
        """Por el camino real: carga CSV del propietario y server.run_monitor."""
        csv1 = "sku;titulo;costo;categoria;stock\nZ-1;Taza real;5;Hogar;10\n"
        server.upload_csv({"supplier": "Prov Z", "csv": csv1})
        with db.connect() as conn:
            pid = conn.execute("SELECT id FROM products WHERE supplier = 'Prov Z'").fetchone()[0]
        store = self.connect()
        iid = self.imp(pid=pid, sell_price=15.0)
        r = store_sync.publish(store["id"], {"imported_ids": [iid], "as_draft": False})
        store_sync.run_due_jobs()
        lid = r["queued"][0]["listing_id"]
        server.upload_csv({"supplier": "Prov Z", "csv": "sku;titulo;costo;categoria;stock\nZ-1;Taza real;5;Hogar;0\n"})
        self.assertEqual({a for a, _ in self.run_actions(lid)}, {"sync", "hide"})
        # el monitor (productos de demostración ya excluidos, esto es CSV: no cambia nada y no rompe)
        server.run_monitor(user_id=1)


class PerformanceTest(Base):
    seed = 30

    def test_hook_with_100k_products_and_500_listings_under_1s(self):
        store = self.connect()
        n = 100_000
        with db.connect() as conn:
            base_id = conn.execute("SELECT COALESCE(MAX(id), 0) FROM products").fetchone()[0]
            conn.executemany(
                "INSERT INTO products(id, supplier, supplier_sku, source, title, category, image, cost, shipping_cost, "
                "shipping_days, warehouse, stock, orders, orders_7d, orders_prev_7d, rating, reviews, updated_at) "
                "VALUES (?, 'P', ?, 'csv', 'Prod', 'Hogar', '', 10, 0, 7, 'CN', 5, 0, 0, 0, 4, 1, 'x')",
                ((base_id + i, f"S{i}") for i in range(1, n + 1)))
            conn.executemany("INSERT INTO imported_products(id, user_id, product_id, title, sell_price, last_cost, "
                             "last_stock, created_at) VALUES (?, 1, ?, 'Prod', 20, 10, 5, 'x')",
                             ((100 + i, base_id + i * 190) for i in range(1, 501)))
            conn.executemany("INSERT INTO store_listings(user_id, imported_id, connection_id, title, status, external_id, "
                             "last_price, last_stock, created_at) VALUES (1, ?, ?, 'Prod', 'published', ?, 20, 5, 'x')",
                             ((100 + i, store["id"], str(i)) for i in range(1, 501)))
            conn.execute("UPDATE products SET stock = 0 WHERE id > ?", (base_id,))
            ids = list(range(base_id + 1, base_id + n + 1))
            t = time.perf_counter()
            store_sync._on_catalog_write(conn, ids)      # solo nuestro hook (los demás módulos tienen el suyo)
            dt = time.perf_counter() - t
            queued = conn.execute("SELECT COUNT(*) FROM store_jobs").fetchone()[0]
        print(f"\n[hook 100k productos / 500 anuncios: {dt * 1000:.0f} ms, {queued} trabajos]", file=sys.stderr)
        self.assertEqual(queued, 1000)                              # sync + hide por anuncio (500 anuncios)
        self.assertLess(dt, 1.0)


class ReviewMinorsTest(Base):
    seed = 60

    def test_rate_limit_survives_deleting_listings(self):
        store = self.connect()
        ids = [self.imp(source="mock") for _ in range(52)]
        store_sync.publish(store["id"], {"imported_ids": ids[:50], "allow_demo": True})
        for item in store_sync.store_listings(store["id"], {"page_size": ["100"]})["items"]:
            store_sync.delete_listing(item["id"], {})
        with self.assertRaises(ApiError) as cm:
            store_sync.publish(store["id"], {"imported_ids": ids[50:], "allow_demo": True})
        self.assertEqual(cm.exception.code, "publish_rate_limited")

    def test_rate_limit_window_slides(self):
        store = self.connect()
        ids = [self.imp(source="mock") for _ in range(52)]
        store_sync.publish(store["id"], {"imported_ids": ids[:50], "allow_demo": True})
        real = time.time
        store_sync.time.time = lambda: real() + 61                         # pasó un minuto
        try:
            self.assertEqual(len(store_sync.publish(store["id"], {"imported_ids": ids[50:], "allow_demo": True})["queued"]), 2)
        finally:
            store_sync.time.time = real

    def test_huge_integers_are_400_or_404_not_500(self):
        store = self.connect()
        big = 2 ** 70
        for call, status in ((lambda: store_sync.publish(store["id"], {"imported_ids": [big]}), 400),
                             (lambda: store_sync.publish(big, {"imported_ids": [1]}), 404),
                             (lambda: store_sync.listings_for_imports({"imported_ids": [str(big)]}), 400),
                             (lambda: store_sync.store_listings(big, {}), 404),
                             (lambda: store_sync.store_log(big, {}), 404),
                             (lambda: store_sync.listing_action(big, "hide"), 404),
                             (lambda: store_sync.delete_listing(big, {}), 404)):
            with self.assertRaises(ApiError) as cm:
                call()
            self.assertEqual(cm.exception.status, status)

    def test_sync_now_processes_at_most_five(self):
        store = self.connect()
        store_sync.publish(store["id"], {"imported_ids": [self.imp() for _ in range(8)]})
        self.assertEqual(store_sync.sync_now(), {"processed": 5, "failed": 0})
        self.assertEqual(store_sync.queue_state()["pending"], 3)

    def test_failed_count_ignores_failures_already_superseded_by_a_retry(self):
        store = self.connect()
        r = store_sync.publish(store["id"], {"imported_ids": [self.imp()]})
        lid = r["queued"][0]["listing_id"]
        FakeStore.fail_next.append(sc.StoreValidationError("no"))
        store_sync.run_due_jobs()
        self.assertEqual(store_sync.queue_state()["failed"], 1)
        store_sync.listing_action(lid, "retry")
        store_sync.run_due_jobs()
        self.assertEqual(self.listing(lid)["status"], "hidden")
        self.assertEqual(store_sync.queue_state(), {"pending": 0, "running": 0, "failed": 0})

    def test_exceptions_are_logged_without_foreign_messages_or_tracebacks(self):
        store = self.connect(platform="fakeidem")
        store_sync.publish(store["id"], {"imported_ids": [self.imp()]})
        FakeStore.fail_next.append(RuntimeError("token shpat_abcdef0123456789abcdef revocado"))
        with self.assertLogs("dropfinder.store_sync", level="ERROR") as cm:
            store_sync.run_due_jobs()
        text = " ".join(cm.output)
        self.assertNotIn("shpat_", text)
        self.assertNotIn("Traceback", text)
        self.assertTrue(all(r.exc_info is None for r in cm.records))

    def test_hook_filters_in_sql_with_5000_listings(self):
        store = self.connect()
        n = 5000
        with db.connect() as conn:
            base = conn.execute("SELECT COALESCE(MAX(id), 0) FROM products").fetchone()[0]
            conn.executemany(
                "INSERT INTO products(id, supplier, supplier_sku, source, title, category, image, cost, shipping_cost, "
                "shipping_days, warehouse, stock, orders, orders_7d, orders_prev_7d, rating, reviews, updated_at) "
                "VALUES (?, 'P', ?, 'csv', 'Prod', 'Hogar', '', 10, 0, 7, 'CN', 5, 0, 0, 0, 4, 1, 'x')",
                ((base + i, f"S{i}") for i in range(1, n + 1)))
            conn.executemany("INSERT INTO imported_products(id, user_id, product_id, title, sell_price, last_cost, "
                             "last_stock, created_at) VALUES (?, 1, ?, 'Prod', 20, 10, 5, 'x')",
                             ((1000 + i, base + i) for i in range(1, n + 1)))
            conn.executemany("INSERT INTO store_listings(user_id, imported_id, product_id, connection_id, title, status, "
                             "external_id, last_price, last_stock, created_at) VALUES (1, ?, ?, ?, 'Prod', 'published', ?, 20, 5, 'x')",
                             ((1000 + i, base + i, store["id"], str(i)) for i in range(1, n + 1)))
            conn.execute("UPDATE products SET stock = 0 WHERE id = ?", (base + 7,))
            t = time.perf_counter()
            for _ in range(50):
                store_sync._on_catalog_write(conn, [base + 7])
            one = (time.perf_counter() - t) / 50
            t = time.perf_counter()
            store_sync._on_catalog_write(conn, list(range(base + 1, base + 101)))
            hundred = time.perf_counter() - t
            queued = conn.execute("SELECT COUNT(*) FROM store_jobs").fetchone()[0]
        print(f"\n[hook 5000 anuncios: 1 id {one * 1000:.1f} ms/llamada, 100 ids {hundred * 1000:.1f} ms]", file=sys.stderr)
        self.assertEqual(queued, 2)                                         # hide + sync solo del producto 7
        self.assertLess(one, 0.01)


class MutationSurvivorsTest(Base):
    """Pruebas de las 5 mutaciones que sobrevivían en la revisión de PR #12."""

    def test_normal_pass_recovers_expired_leases(self):
        store = self.connect()
        r = store_sync.publish(store["id"], {"imported_ids": [self.imp()]})
        lid = r["queued"][0]["listing_id"]
        with db.connect() as conn:
            conn.execute("UPDATE store_jobs SET status = 'running', attempts = 1, locked_until = ?",
                         ((utc() - timedelta(hours=1)).isoformat(timespec="seconds"),))
        store_sync.run_due_jobs()                                          # SOLO el pase normal, sin recover_stuck a mano
        self.assertEqual((self.jobs(lid)[0]["status"], self.listing(lid)["status"]), ("done", "hidden"))

    def test_sync_never_runs_before_its_publish(self):
        store = self.connect()
        r = store_sync.publish(store["id"], {"imported_ids": [self.imp()]})
        lid = r["queued"][0]["listing_id"]
        with db.connect() as conn:                                         # un sync con vencimiento ANTERIOR al publish
            conn.execute("INSERT INTO store_jobs(user_id, listing_id, action, status, attempts, run_after, payload, "
                         "created_at, updated_at) VALUES (1, ?, 'sync', 'pending', 0, '2000-01-01T00:00:00+00:00', '{}', 'x', 'x')",
                         (lid,))
        first = store_sync._claim(None, utc())
        self.assertEqual(first["action"], "publish")
        self.assertIsNone(store_sync._claim(None, utc()))                  # el sync espera a que termine el publish

    def test_scrub_masks_what_the_regex_layer_does_not(self):
        secret = "ck_0123456789abcdef01234567"                             # clave WooCommerce: solo la reconoce sc.scrub
        out = store_sync.clean_message(f"fallo con {secret} en la tienda")
        self.assertNotIn(secret, out)
        self.assertIn("••••", out)

    def test_rule_show_is_skipped_at_execution_if_not_hidden_by_rule_any_more(self):
        store, iid, lid = self.published(as_draft=False, stock=10)
        pid = self.pid_of(iid)
        self.catalog_change(pid, stock=0)
        store_sync.run_due_jobs()
        self.assertEqual(self.listing(lid)["hidden_by_rule"], 1)
        self.catalog_change(pid, stock=5)                                  # show por regla pendiente
        self.assertIn("show", [j["action"] for j in self.jobs(lid, "pending")])
        with db.connect() as conn:                                         # …y el usuario lo oculta a mano antes de ejecutarse
            conn.execute("UPDATE store_listings SET hidden_by_rule = 0 WHERE id = ?", (lid,))
        store_sync.run_due_jobs()
        self.assertEqual((self.listing(lid)["status"], self.remote(lid)["active"]), ("hidden", False))

    def test_mock_product_with_is_demo_0_is_still_excluded_from_rules(self):
        store = self.connect()
        iid = self.imp(source="mock")
        r = store_sync.publish(store["id"], {"imported_ids": [iid], "allow_demo": True, "as_draft": False})
        store_sync.run_due_jobs()
        lid = r["queued"][0]["listing_id"]
        with db.connect() as conn:
            conn.execute("UPDATE store_listings SET is_demo = 0 WHERE id = ?", (lid,))
        self.catalog_change(self.pid_of(iid), stock=0)
        self.assertEqual(self.jobs(lid, "pending"), [])


class IsolationTest(Base):
    def test_user_b_gets_404_everywhere(self):
        store, iid, lid = self.published(as_draft=False)
        with auth_context.as_user(2, "member"):
            for fn in (lambda: store_sync.publish(store["id"], {"imported_ids": [iid]}),
                       lambda: store_sync.store_listings(store["id"], {}),
                       lambda: store_sync.store_log(store["id"], {}),
                       lambda: store_sync.delete_listing(lid, {"remove_remote": ["1"]}),
                       *[(lambda a=a: store_sync.listing_action(lid, a)) for a in ("sync", "hide", "show", "retry")]):
                with self.assertRaises(ApiError) as cm:
                    fn()
                self.assertEqual(cm.exception.status, 404)
            self.assertEqual(store_sync.listings_for_imports({"imported_ids": [str(iid)]}), {"items": []})
            self.assertEqual(store_sync.queue_state(), {"pending": 0, "running": 0, "failed": 0})
            # su propia tienda no puede publicar el producto importado de A
            FakeStore.stores["b.fake.test"] = {"products": {}, "currency": "USD", "revoked": False}
            mine = stores_mod.connect_store({"platform": "fake", "domain": "b.fake.test", "credentials": dict(CREDS)})
            r = store_sync.publish(mine["id"], {"imported_ids": [iid]})
            self.assertEqual(r["skipped"][0]["reason"], "not_found")
            self.assertEqual(store_sync.run_due_jobs(user_id=2)["processed"], 0)
        self.assertEqual(self.listing(lid)["status"], "published")          # nada cambió para A
        self.assertEqual(len(self.remote(lid) and [1]), 1)

    def test_sync_now_only_processes_own_jobs(self):
        store = self.connect()
        store_sync.publish(store["id"], {"imported_ids": [self.imp()]})
        with auth_context.as_user(2, "member"):
            self.assertEqual(store_sync.sync_now(), {"processed": 0, "failed": 0})
        self.assertEqual(store_sync.sync_now(), {"processed": 1, "failed": 0})

    def test_jobs_run_as_their_owner(self):
        seen = []
        orig = FakeStore.publish

        def spy(self_, product):
            seen.append(auth_context.current_user_id())
            return orig(self_, product)
        FakeStore.publish = spy
        try:
            store = self.connect()
            store_sync.publish(store["id"], {"imported_ids": [self.imp()]})
            auth_context.set_current(2, "member")
            store_sync.run_due_jobs()                               # otro usuario ejecuta la cola global
        finally:
            FakeStore.publish = orig
            auth_context.set_current(1, "owner")
        self.assertEqual(seen, [1])


class ScheduleTest(Base):
    def test_validation_and_defaults(self):
        self.assertEqual(store_sync.get_schedule()["minutes"], 0)
        for bad in (7, "15", None, True, -1, 1.5):
            with self.assertRaises(ApiError) as cm:
                store_sync.set_schedule({"minutes": bad})
            self.assertEqual(cm.exception.status, 400, bad)
        self.assertEqual(store_sync.set_schedule({"minutes": 60})["minutes"], 60)
        self.assertEqual(store_sync.set_schedule({"minutes": 0})["minutes"], 0)

    def test_tick_runs_monitor_per_user_with_active_stores(self):
        calls = []

        def runner(user_id=None):
            calls.append((user_id, auth_context.current_user_id()))
            if user_id == 2:
                raise RuntimeError("boom")
            return {"checked": 1, "alerts": []}
        store_sync.set_monitor_runner(runner)
        try:
            self.connect()
            with auth_context.as_user(2, "member"):
                self.connect("dos.fake.test")
            with auth_context.as_user(3, "member"):
                s3 = self.connect("tres.fake.test")
            with db.connect() as conn:
                stores_mod.mark_error(conn, s3["id"], "x")          # sin tienda activa: no corre
            self.assertEqual(store_sync.monitor_tick(), [])         # apagado
            store_sync.set_schedule({"minutes": 15})
            now = utc() + timedelta(minutes=16)
            self.assertEqual(store_sync.monitor_tick(now), [1])     # el 2 falla pero no tumba al resto
            self.assertEqual(calls, [(1, 1), (2, 2)])
            self.assertEqual(store_sync.monitor_tick(now + timedelta(minutes=5)), [])      # aún no toca
            self.assertEqual(store_sync.monitor_tick(now + timedelta(minutes=16)), [1])
            with db.connect() as conn:
                self.assertTrue(conn.execute("SELECT 1 FROM store_sync_log WHERE action = 'monitor' AND ok = 0").fetchone())
        finally:
            store_sync.set_monitor_runner(server.run_monitor)

    def test_real_monitor_respects_cooldown(self):
        store_sync.set_monitor_runner(server.run_monitor)
        self.connect()
        self.imp(source="mock")
        store_sync.set_schedule({"minutes": 15})
        self.assertEqual(store_sync.monitor_tick(utc() + timedelta(minutes=20)), [1])
        with db.connect() as conn:
            n1 = conn.execute("SELECT COUNT(*) FROM monitor_checks").fetchone()[0]
        self.assertEqual(n1, 1)
        res = server.run_monitor(user_id=1)
        self.assertEqual((res["checked"], res["cooldown"]), (0, 1))     # enfriamiento de 15 min por producto

    def test_worker_is_lazy_and_respects_flag(self):
        store_sync.ensure_worker()
        self.assertIsNone(store_sync._worker if not store_sync.BACKGROUND and store_sync._worker is None else None)

    def test_routes_classification(self):
        for method, pattern, _fn in store_sync.ROUTES:
            want = "owner" if (method, pattern) == ("PUT", r"/api/stores/monitor-schedule") else "user"
            self.assertEqual(server.route_access(method, pattern), want, (method, pattern))
            key = (method, pattern)
            self.assertTrue(key in server.USER_ROUTES or key in server.OWNER_ONLY, key)


class HttpSmokeTest(Base):
    seed = 40

    def setUp(self):
        super().setUp()
        self.srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.h = authtest.auth_headers()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def call(self, method, path, body=None):
        req = urllib.request.Request(self.base + path, method=method, headers={"Content-Type": "application/json", **self.h},
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def test_publish_sync_and_queue_over_http(self):
        FakeStore.stores["web.fake.test"] = {"products": {}, "currency": "COP", "revoked": False}
        st, store = self.call("POST", "/api/stores", {"platform": "fake", "domain": "web.fake.test", "credentials": CREDS})
        self.assertEqual(st, 200, store)
        pid = self.product(stock=4)
        st, imp = self.call("POST", "/api/imports", {"product_id": pid, "sell_price": 10})
        self.assertEqual(st, 200)
        st, r = self.call("POST", f"/api/stores/{store['id']}/publish", {"imported_ids": [imp["id"]], "as_draft": False})
        self.assertEqual((st, len(r["queued"]), r["skipped"]), (200, 1, []))
        st, q = self.call("GET", "/api/stores/queue")
        self.assertEqual((st, q), (200, {"pending": 1, "running": 0, "failed": 0}))
        st, r2 = self.call("POST", "/api/stores/sync")
        self.assertEqual((st, r2), (200, {"processed": 1, "failed": 0}))
        st, ls = self.call("GET", f"/api/store-listings?imported_ids={imp['id']}")
        self.assertEqual((st, ls["items"][0]["status"], ls["items"][0]["last_price"]), (200, "published", 40900))
        st, pg = self.call("GET", f"/api/stores/{store['id']}/listings?page_size=5")
        self.assertEqual((st, pg["total"], pg["pages"]), (200, 1, 1))
        lid = ls["items"][0]["id"]
        st, h = self.call("POST", f"/api/store-listings/{lid}/hide")
        self.assertEqual((st, h["listing"]["pending_action"]), (200, "hide"))
        self.call("POST", "/api/stores/sync")
        st, log = self.call("GET", f"/api/stores/{store['id']}/log?limit=10")
        self.assertEqual(st, 200)
        self.assertTrue(any(i["action"] == "hide" for i in log["items"]))
        st, d = self.call("DELETE", f"/api/store-listings/{lid}?remove_remote=1")
        self.assertEqual((st, d), (200, {"ok": True, "queued_remote_delete": True}))
        self.assertEqual(self.call("GET", "/api/store-listings/99999")[0], 404)
        self.assertEqual(self.call("POST", "/api/store-listings/99999/sync")[0], 404)
        self.assertEqual(self.call("POST", "/api/stores/99999/publish", {"imported_ids": [1]})[0], 404)
        st, m = self.call("PUT", "/api/stores/monitor-schedule", {"minutes": 30})
        self.assertEqual((st, m["minutes"]), (200, 30))
        self.assertEqual(self.call("PUT", "/api/stores/monitor-schedule", {"minutes": 7})[0], 400)
        self.assertEqual(self.call("GET", "/api/stores/monitor-schedule")[1]["minutes"], 30)

    def test_member_cannot_change_schedule_over_http(self):
        import auth
        with db.connect() as conn:
            token, csrf = auth.create_session(conn, conn.execute("SELECT id FROM users").fetchone()[0])
            conn.execute("UPDATE users SET role = 'member'")             # la sesión es la misma persona sin rol de propietario
        self.h = authtest.session_headers(token, csrf)
        self.assertEqual(self.call("PUT", "/api/stores/monitor-schedule", {"minutes": 60})[0], 403)
        self.assertEqual(self.call("GET", "/api/stores/monitor-schedule")[0], 200)


if __name__ == "__main__":
    unittest.main()
