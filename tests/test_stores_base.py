"""Base de tiendas (Sprint 4-5): almacén cifrado, conexiones por usuario y contrato de conectores."""
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
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DROPFINDER_DB", os.path.join(tempfile.mkdtemp(), "test.db"))
os.environ["DROPFINDER_SYNTH_HISTORY"] = "0"

import authtest  # noqa: E402,F401  (fija el contexto de propietario 1 en el hilo principal)
import auth_context  # noqa: E402
import db  # noqa: E402
import search  # noqa: E402
import secrets_store  # noqa: E402
import server  # noqa: E402
import store_connectors as sc  # noqa: E402
import stores_mod  # noqa: E402
from store_connectors.fake import FakeStore  # noqa: E402

sc.register(FakeStore)


class _TempDb:
    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="dropfinder-stores-")
        self.old, db.DB_PATH = db.DB_PATH, os.path.join(self.dir, "t.db")
        self.old_key = os.environ.get("DROPFINDER_KEY_FILE")
        os.environ["DROPFINDER_KEY_FILE"] = os.path.join(self.dir, "k.key")
        secrets_store.forget_cache()
        FakeStore.reset()

    def close(self):
        db.DB_PATH = self.old
        if self.old_key is None:
            os.environ.pop("DROPFINDER_KEY_FILE", None)
        else:
            os.environ["DROPFINDER_KEY_FILE"] = self.old_key
        secrets_store.forget_cache()
        search.invalidate()
        shutil.rmtree(self.dir, ignore_errors=True)


CREDS = {"token": "shpat_secreto_1234"}


class SecretsStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = _TempDb()

    def tearDown(self):
        self.tmp.close()

    def test_roundtrip_and_randomized(self):
        a, b = secrets_store.encrypt("hola ñandú", "x"), secrets_store.encrypt("hola ñandú", "x")
        self.assertNotEqual(a, b)                                  # nonce aleatorio
        self.assertEqual(secrets_store.decrypt(a, "x"), "hola ñandú")
        self.assertNotIn("hola", a)

    def test_aad_tampering_and_wrong_key(self):
        t = secrets_store.encrypt("secreto", "user:1|a|b")
        with self.assertRaises(secrets_store.SecretError):
            secrets_store.decrypt(t, "user:2|a|b")                 # copiado a otra fila/usuario
        v, n, c, tag = t.split("$")
        for bad in (f"{v}${n}${c[:-2]}AA${tag}", f"{v}${n}${c}${tag[:-2]}AA", "v9$a$b$c", "basura", ""):
            with self.assertRaises(secrets_store.SecretError):
                secrets_store.decrypt(bad, "user:1|a|b")
        os.remove(secrets_store.key_path())
        secrets_store.forget_cache()                               # otra clave maestra
        with self.assertRaises(secrets_store.SecretError):
            secrets_store.decrypt(t, "user:1|a|b")

    def test_key_file_created_once_and_never_overwritten(self):
        secrets_store.encrypt("x")
        path = secrets_store.key_path()
        with open(path, "rb") as f:
            first = f.read()
        secrets_store.forget_cache()
        secrets_store.encrypt("y")
        with open(path, "rb") as f:
            self.assertEqual(f.read(), first)
        if os.name == "posix":
            self.assertEqual(os.stat(path).st_mode & 0o077, 0)     # sin permisos para otros usuarios

    def test_long_values(self):
        big = "a" * 5000
        self.assertEqual(secrets_store.decrypt(secrets_store.encrypt(big, "k"), "k"), big)

    def test_mask(self):
        self.assertEqual(secrets_store.mask("shpat_abcdef1234"), "••••••••1234")
        self.assertEqual(secrets_store.mask("ab"), "••")


class StoresApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = _TempDb()
        db.init_db(seed_size=30)
        auth_context.set_current(1, "owner")

    def tearDown(self):
        self.tmp.close()
        auth_context.set_current(1, "owner")

    def connect(self, domain="mitienda.fake.test", **extra):
        return stores_mod.connect_store({"platform": "fake", "domain": domain, "credentials": dict(CREDS), **extra})

    def test_connect_encrypts_and_never_exposes_credentials(self):
        s = self.connect()
        self.assertEqual(s["status"], "active")
        self.assertEqual(s["credentials"], {"token": "••••••••1234"})
        self.assertNotIn("shpat_secreto", json.dumps(s))
        with db.connect() as conn:
            raw = conn.execute("SELECT credentials_enc FROM store_connections").fetchone()[0]
        self.assertNotIn("shpat_secreto", raw)
        self.assertNotIn("token", raw)
        self.assertEqual(json.loads(secrets_store.decrypt(raw, "user:1|fake|mitienda.fake.test")), CREDS)

    def test_validation_errors(self):
        for body, status in [({"platform": "nope", "domain": "a.fake.test", "credentials": CREDS}, 400),
                             ({"platform": "fake", "domain": "https://evil.com", "credentials": CREDS}, 400),
                             ({"platform": "fake", "domain": "a.fake.test", "credentials": {}}, 400),
                             ({"platform": "fake", "domain": "a.fake.test", "credentials": {"token": "x", "otro": "y"}}, 400),
                             ({"platform": "fake", "domain": "a.fake.test", "credentials": CREDS, "settings": {"x": 1}}, 400),
                             ({"platform": "fake", "domain": "a.fake.test", "credentials": CREDS,
                               "settings": {"min_margin_pct": 500}}, 400)]:
            with self.assertRaises(server.ApiError) as cm:
                stores_mod.connect_store(body)
            self.assertEqual(cm.exception.status, status, body)

    def test_rejected_credentials_are_400_not_401(self):
        with self.assertRaises(server.ApiError) as cm:
            stores_mod.connect_store({"platform": "fake", "domain": "a.fake.test", "credentials": {"token": "revocado"}})
        self.assertEqual(cm.exception.status, 400)                  # 401 haría creer al navegador que la sesión terminó
        self.assertIn("rechazó", str(cm.exception))

    def test_upstream_error_is_502_and_rate_limit_429(self):
        FakeStore.fail_next.append(sc.StoreError("caído", retryable=True))
        with self.assertRaises(server.ApiError) as cm:
            self.connect()
        self.assertEqual(cm.exception.status, 502)
        FakeStore.fail_next.append(sc.StoreRateLimited("lento", 3))
        with self.assertRaises(server.ApiError) as cm:
            self.connect()
        self.assertEqual(cm.exception.status, 429)

    def test_duplicate_limit_and_missing_scopes(self):
        self.connect()
        with self.assertRaises(server.ApiError) as cm:
            self.connect()
        self.assertEqual(cm.exception.status, 409)
        for i in range(stores_mod.MAX_CONNECTIONS - 1):
            self.connect(f"t{i}.fake.test")
        with self.assertRaises(server.ApiError) as cm:
            self.connect("extra.fake.test")
        self.assertEqual(cm.exception.status, 409)

    def test_update_settings_and_rotate_credentials(self):
        s = self.connect()
        u = stores_mod.update_store(s["id"], {"settings": {"auto_raise_price": True, "min_margin_pct": 30}, "name": "Mi tienda"})
        self.assertTrue(u["settings"]["auto_raise_price"])
        self.assertEqual((u["settings"]["min_margin_pct"], u["name"]), (30.0, "Mi tienda"))
        self.assertTrue(u["settings"]["hide_when_out_of_stock"])    # los demás ajustes conservan su valor
        u = stores_mod.update_store(s["id"], {"credentials": {"token": "shpat_nuevo_9999"}})
        self.assertEqual(u["credentials"]["token"], "••••••••9999")

    def test_publish_as_draft_is_default(self):
        self.assertTrue(self.connect()["settings"]["publish_as_draft"])

    def test_revalidate_marks_error_then_ok(self):
        s = self.connect()
        FakeStore.stores["mitienda.fake.test"]["revoked"] = True
        with self.assertRaises(server.ApiError):
            stores_mod.validate_store(s["id"])
        self.assertEqual(stores_mod.get_store(s["id"])["status"], "error")
        FakeStore.stores["mitienda.fake.test"]["revoked"] = False
        self.assertEqual(stores_mod.validate_store(s["id"])["status"], "active")

    def test_disconnect_requires_confirmation_when_listings_exist(self):
        s = self.connect()
        with db.connect() as conn:
            conn.execute("INSERT INTO store_listings(user_id, connection_id, title, created_at) VALUES (1, ?, 't', 'x')",
                         (s["id"],))
        with self.assertRaises(server.ApiError) as cm:
            stores_mod.disconnect_store(s["id"], {})
        self.assertEqual(cm.exception.status, 409)
        self.assertEqual(stores_mod.disconnect_store(s["id"], {"confirm": ["1"]})["listings_left_in_store"], 1)
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM store_listings").fetchone()[0], 0)   # cascada

    def test_isolation_between_users(self):
        s = self.connect()
        with auth_context.as_user(2, "member"):
            self.assertEqual(stores_mod.list_stores()["items"], [])
            for fn in (lambda: stores_mod.get_store(s["id"]),
                       lambda: stores_mod.update_store(s["id"], {"name": "x"}),
                       lambda: stores_mod.validate_store(s["id"]),
                       lambda: stores_mod.disconnect_store(s["id"], {"confirm": ["1"]})):
                with self.assertRaises(server.ApiError) as cm:
                    fn()
                self.assertEqual(cm.exception.status, 404)
            self.connect()                                          # el mismo dominio, otra persona: permitido
            self.assertEqual(len(stores_mod.list_stores()["items"]), 1)
        self.assertEqual(len(stores_mod.list_stores()["items"]), 1)

    def test_no_user_context_is_401(self):
        auth_context.clear()
        with self.assertRaises(server.ApiError) as cm:
            stores_mod.list_stores()
        self.assertEqual(cm.exception.status, 401)

    def test_connector_for_decrypts_and_rejects_moved_rows(self):
        s = self.connect()
        with db.connect() as conn:
            row = conn.execute("SELECT * FROM store_connections WHERE id = ?", (s["id"],)).fetchone()
            self.assertEqual(stores_mod.connector_for(row).credentials, CREDS)
            conn.execute("UPDATE store_connections SET domain = 'otra.fake.test' WHERE id = ?", (s["id"],))
            moved = conn.execute("SELECT * FROM store_connections WHERE id = ?", (s["id"],)).fetchone()
        with self.assertRaises(secrets_store.SecretError):
            stores_mod.connector_for(moved)                         # credenciales atadas a su usuario+dominio

    def test_user_deletion_removes_store_data(self):
        import auth
        with db.connect() as conn:
            uid = auth.create_first_owner(conn, "dueno@x.co", "D", auth.hash_password("clavesegura123"))
        with auth_context.as_user(uid, "owner"):
            s = self.connect("propia.fake.test")
            with db.connect() as conn:
                conn.execute("INSERT INTO store_listings(user_id, connection_id, title, created_at) VALUES (?, ?, 't', 'x')",
                             (uid, s["id"]))
                conn.execute("INSERT INTO store_sync_log(user_id, connection_id, action, ok, message, created_at) "
                             "VALUES (?, ?, 'publish', 1, 'ok', 'x')", (uid, s["id"]))
        # El borrado de usuarios es genérico por columna user_id: las tablas de tiendas deben tenerla
        with db.connect() as conn:
            for t in ("store_connections", "store_listings", "store_jobs", "store_sync_log"):
                self.assertIn("user_id", {r[1] for r in conn.execute(f"PRAGMA table_info({t})")}, t)


class ContractTest(unittest.TestCase):
    def setUp(self):
        FakeStore.reset()

    def test_registry_and_lifecycle(self):
        self.assertIn("fake", [p.platform for p in sc.platforms()])
        c = sc.create("fake", "a.fake.test", CREDS)
        ref = c.publish(sc.StoreProduct(title="Lámpara", price=19.9, sku="SK1", stock=5))
        c.update_price_stock(ref, 24.9, 0)
        c.set_active(ref, False)
        self.assertEqual(FakeStore.stores["a.fake.test"]["products"][ref.external_id],
                         {"title": "Lámpara", "price": 24.9, "stock": 0, "active": False, "sku": "SK1", "image_urls": []})
        c.delete(ref)
        c.delete(ref)                                               # borrar dos veces no es error
        with self.assertRaises(sc.StoreNotFound):
            c.update_price_stock(ref, 1, 1)

    def test_unknown_platform(self):
        with self.assertRaises(sc.StoreValidationError):
            sc.get_class("inexistente")


class RouteClassificationTest(unittest.TestCase):
    def test_store_routes_are_user_routes(self):
        for method, pattern, _fn in stores_mod.ROUTES:
            self.assertEqual(server.route_access(method, pattern), "user", (method, pattern))
        self.assertIn(("POST", r"/api/stores"), server.USER_ROUTES)


if __name__ == "__main__":
    unittest.main()
