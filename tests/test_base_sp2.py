"""Base del Sprint 2: hooks de catálogo, migración v4 e índices (hallazgos de la revisión del PR #2)."""
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
os.environ.setdefault("DROPFINDER_DB", os.path.join(tempfile.mkdtemp(), "test.db"))

import db  # noqa: E402
import hooks  # noqa: E402
import search  # noqa: E402
import server  # noqa: E402


class _TempDb:
    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="dropfinder-sp2-")
        self.old, db.DB_PATH = db.DB_PATH, os.path.join(self.dir, "t.db")

    def close(self):
        db.DB_PATH = self.old
        search.invalidate()
        shutil.rmtree(self.dir, ignore_errors=True)


class HooksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = _TempDb()
        db.init_db(seed_size=50)
        self.calls = []

        def recorder(conn, ids):
            stocks = dict(conn.execute("SELECT supplier_sku, stock FROM products WHERE supplier = 'H'").fetchall())
            self.calls.append((sorted(ids) if ids is not None else None, stocks))
        recorder.__qualname__ = "recorder_test"
        self.recorder = hooks.on_catalog_write(recorder)

    def tearDown(self):
        hooks._catalog_write.pop(f"{self.recorder.__module__}.recorder_test", None)
        hooks._catalog_write.pop("tests.test_base_sp2.boom", None)
        self.tmp.close()

    def test_hook_sees_final_state_after_csv_replace(self):
        server.upload_csv({"supplier": "H", "csv": "sku;titulo;costo;stock\nA;Uno;1;10\nB;Dos;1;20\nC;Tres;1;30\n"})
        with db.connect() as conn:
            b_id = conn.execute("SELECT id FROM products WHERE supplier_sku='B'").fetchone()[0]
        server.create_import({"product_id": b_id})
        self.calls.clear()
        server.upload_csv({"supplier": "H", "csv": "sku;titulo;costo;stock\nA;Uno;1;10\n", "replace": True})
        self.assertEqual(len(self.calls), 1)                     # una sola vez, al final
        ids, stocks = self.calls[0]
        self.assertEqual(stocks, {"A": 10, "B": 0})              # C borrado, B (importado) sin stock
        self.assertIn(b_id, ids)

    def test_failing_hook_does_not_break_writes(self):
        def boom(conn, ids):
            conn.execute("UPDATE products SET title = 'roto'")   # se deshace con el savepoint
            raise RuntimeError("módulo con errores")
        boom.__module__ = "tests.test_base_sp2"
        hooks.on_catalog_write(boom)
        r = server.upload_csv({"supplier": "H2", "csv": "sku;titulo;costo\nZ;Producto zeta;2\n"})
        self.assertEqual(r["new"], 1)
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM products WHERE title = 'roto'").fetchone()[0], 0)

    def test_hook_ids_are_exact_within_same_second(self):
        server.upload_csv({"supplier": "H", "csv": "sku;titulo;costo\nA;Uno;1\nB;Dos;1\n"})
        self.calls.clear()
        server.upload_csv({"supplier": "H", "csv": "sku;titulo;costo\nA;Uno;2\n", "replace": True})
        with db.connect() as conn:
            a_id = conn.execute("SELECT id FROM products WHERE supplier='H' AND supplier_sku='A'").fetchone()[0]
            self.assertIsNone(conn.execute("SELECT 1 FROM products WHERE supplier_sku='B' AND supplier='H'").fetchone())
        self.assertEqual(self.calls[-1][0], [a_id])        # B fue borrado: no debe llegar al hook

    def test_reregistering_same_function_is_idempotent(self):
        n = len(hooks._catalog_write)
        hooks.on_catalog_write(self.recorder)
        self.assertEqual(len(hooks._catalog_write), n)

    def test_alerts_count(self):
        self.assertIn("unread", server.alerts_count())


class MigrationV4Test(unittest.TestCase):
    def test_orphan_alert_does_not_block_migration(self):
        tmp = _TempDb()
        try:
            db.init_db(seed_size=20)
            conn = sqlite3.connect(db.DB_PATH)
            # Simula una base v3: alerta huérfana y tabla alerts antigua
            conn.executescript("""
                PRAGMA foreign_keys = OFF;
                DROP TABLE alerts;
                CREATE TABLE alerts (id INTEGER PRIMARY KEY,
                  imported_id INTEGER NOT NULL REFERENCES imported_products(id) ON DELETE CASCADE,
                  type TEXT NOT NULL, message TEXT NOT NULL, created_at TEXT NOT NULL, read INTEGER NOT NULL DEFAULT 0);
                INSERT INTO alerts(imported_id, type, message, created_at) VALUES (999, 'x', 'huérfana', '2026-01-01');
                PRAGMA user_version = 3;
            """)
            conn.close()
            db.init_db()
            with db.connect() as c:
                self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
                self.assertEqual(c.execute("PRAGMA foreign_key_check").fetchall(), [])
        finally:
            tmp.close()

    def test_cascade_delete_is_fast_with_indexes(self):
        tmp = _TempDb()
        try:
            db.init_db(seed_size=3000)
            with db.connect() as conn:
                for i in range(10):
                    conn.execute("INSERT INTO saved_searches(name, params, created_at) VALUES (?, '{}', 'x')", (f"s{i}",))
                conn.execute("INSERT INTO saved_search_hits SELECT s.id, p.id, 'x' FROM saved_searches s, products p")
            t0 = time.perf_counter()
            server.delete_supplier_catalog({"source": ["mock"]})
            self.assertLess(time.perf_counter() - t0, 2.0)
        finally:
            tmp.close()


if __name__ == "__main__":
    unittest.main()
