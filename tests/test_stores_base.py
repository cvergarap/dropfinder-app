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
        self.assertEqual(secrets_store.mask("ab"), "••••••••")            # un secreto corto no revela nada
        self.assertEqual(secrets_store.mask("abcde"), "••••••••")


class SecretsReviewTest(unittest.TestCase):
    """Hallazgos de la revisión del PR #10 sobre el almacén de secretos."""

    def setUp(self):
        self.tmp = _TempDb()

    def tearDown(self):
        self.tmp.close()

    def test_mac_framing_cannot_be_shifted(self):
        import base64
        t = secrets_store.encrypt("secreto-largo-1234567890", "a|b")
        v, n, c, tag = t.split("$")
        nonce, ct = secrets_store._unb64(n), secrets_store._unb64(c)
        for k in (1, 5, 15):                                       # mover bytes entre nonce y texto cifrado
            moved = f"{v}${secrets_store._b64(nonce[:-k])}${secrets_store._b64(nonce[-k:] + ct)}${tag}"
            with self.assertRaises(secrets_store.SecretError):
                secrets_store.decrypt(moved, "a|b")
        # El enmarcado no es ambiguo: con el mismo total de bytes, mover el límite entre aad, nonce y texto cifrado
        # (sin prefijos de longitud estas dos entradas serían idénticas) debe dar otra etiqueta
        self.assertNotEqual(secrets_store._mac(b"a|b", nonce, ct),
                            secrets_store._mac(b"a", b"b|" + nonce[:14], nonce[14:] + ct))
        self.assertNotEqual(secrets_store._mac(b"x", nonce, b"yz"), secrets_store._mac(b"x", nonce, b"y"))
        self.assertNotEqual(secrets_store._mac(b"ab", nonce, ct), secrets_store._mac(b"a", nonce, b"b" + ct))
        # nonce de otro tamaño y texto que no es UTF-8 → SecretError, nunca otra excepción
        with self.assertRaises(secrets_store.SecretError):
            secrets_store.decrypt(f"{v}${secrets_store._b64(nonce[:8])}${c}${tag}", "a|b")
        for bad in ("v1$AA$AA$AA", "v1$$$", "v1$a b$c$d", "v1$%%%$@@@$###"):
            with self.assertRaises(secrets_store.SecretError):
                secrets_store.decrypt(bad, "x")

    def test_lost_key_is_not_silently_regenerated_when_credentials_exist(self):
        db.init_db(seed_size=5)
        auth_context.set_current(1, "owner")
        stores_mod.connect_store({"platform": "fake", "domain": "a.fake.test", "credentials": dict(CREDS)})
        os.remove(secrets_store.key_path())
        secrets_store.forget_cache()
        with self.assertRaises(secrets_store.SecretError) as cm:
            secrets_store.encrypt("x")
        self.assertIn("vuelve a conectar", str(cm.exception).lower())
        self.assertFalse(os.path.exists(secrets_store.key_path()))          # no se creó una clave nueva

    def test_lost_key_surfaces_as_clear_state_not_500(self):
        db.init_db(seed_size=5)
        auth_context.set_current(1, "owner")
        s = stores_mod.connect_store({"platform": "fake", "domain": "a.fake.test", "credentials": dict(CREDS)})
        os.remove(secrets_store.key_path())
        secrets_store.forget_cache()
        self.assertEqual(stores_mod.get_store(s["id"])["credentials"], {})   # no revienta
        with self.assertRaises(server.ApiError) as cm:
            stores_mod.validate_store(s["id"])
        self.assertEqual((cm.exception.status, cm.exception.code), (409, "credentials_unreadable"))
        self.assertEqual(stores_mod.get_store(s["id"])["status"], "error")
        with db.connect() as conn:
            row = conn.execute("SELECT * FROM store_connections").fetchone()
        with self.assertRaises(sc.StoreAuthError):                          # el motor lo trata como auth
            stores_mod.connector_for(row)

    def test_corrupt_key_file_is_a_secret_error(self):
        with open(secrets_store.key_path(), "wb") as f:
            f.write(b"esto no es base64 !!!")
        secrets_store.forget_cache()
        with self.assertRaises(secrets_store.SecretError):
            secrets_store.encrypt("x")

    def test_concurrent_first_use_creates_one_key(self):
        keys, errs = [], []

        def go():
            try:
                secrets_store.forget_cache()
                secrets_store.encrypt("x")
                with open(secrets_store.key_path(), "rb") as f:
                    keys.append(f.read())
            except Exception as e:  # noqa: BLE001
                errs.append(e)
        os.environ["DROPFINDER_KEY_FILE"] = os.path.join(self.tmp.dir, "race.key")
        ts = [threading.Thread(target=go) for _ in range(12)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(errs, [])
        self.assertEqual(len(set(keys)), 1)

    def test_default_key_location_is_outside_repo_and_db_dir(self):
        saved = os.environ.pop("DROPFINDER_KEY_FILE")
        try:
            p = os.path.abspath(secrets_store.key_path())
        finally:
            os.environ["DROPFINDER_KEY_FILE"] = saved
        repo = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        self.assertFalse(p.startswith(repo + os.sep))
        self.assertNotEqual(os.path.dirname(p), os.path.dirname(os.path.abspath(db.DB_PATH)))
        with open(os.path.join(repo, ".gitignore")) as f:
            self.assertIn("*.key", f.read().split())


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

    def test_connection_cap_holds_under_concurrency(self):
        results = []

        def go(i):
            with auth_context.as_user(1, "owner"):      # el contexto es por hilo
                try:
                    self.connect(f"c{i}.fake.test")
                    results.append("ok")
                except server.ApiError as e:
                    results.append(e.code)
        ts = [threading.Thread(target=go, args=(i,)) for i in range(stores_mod.MAX_CONNECTIONS + 8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM store_connections").fetchone()[0],
                             stores_mod.MAX_CONNECTIONS)
        self.assertEqual(results.count("ok"), stores_mod.MAX_CONNECTIONS)

    def test_stable_error_codes_for_409(self):
        self.connect()
        with self.assertRaises(server.ApiError) as cm:
            self.connect()
        self.assertEqual(cm.exception.code, "duplicate_store")
        s = stores_mod.list_stores()["items"][0]
        with db.connect() as conn:
            conn.execute("INSERT INTO store_listings(user_id, connection_id, title, created_at) VALUES (1, ?, 't', 'x')",
                         (s["id"],))
        with self.assertRaises(server.ApiError) as cm:
            stores_mod.disconnect_store(s["id"], {})
        self.assertEqual(cm.exception.code, "has_listings")

    def test_disconnect_refused_while_remote_deletes_are_pending(self):
        s = self.connect()
        with db.connect() as conn:
            lid = conn.execute("INSERT INTO store_listings(user_id, connection_id, title, created_at, external_id) "
                               "VALUES (1, ?, 't', 'x', '77')", (s["id"],)).lastrowid
            conn.execute("INSERT INTO store_jobs(user_id, listing_id, action, run_after, created_at, updated_at) "
                         "VALUES (1, ?, 'delete', 'x', 'x', 'x')", (lid,))
        with self.assertRaises(server.ApiError) as cm:
            stores_mod.disconnect_store(s["id"], {"confirm": ["1"]})
        self.assertEqual(cm.exception.code, "pending_deletes")

    def test_store_errors_are_scrubbed_everywhere(self):
        FakeStore.fail_next.append(sc.StoreAuthError("token shpat_abcdef0123456789abcdef inválido"))
        with self.assertRaises(server.ApiError) as cm:
            self.connect()
        self.assertNotIn("shpat_abcdef", str(cm.exception))
        FakeStore.fail_next.append(sc.StoreError("falló con shpat_abcdef0123456789abcdef y secreto " + CREDS["token"]))
        with self.assertRaises(server.ApiError) as cm:
            self.connect()
        self.assertNotIn(CREDS["token"], str(cm.exception))
        s = self.connect()
        FakeStore.fail_next.append(sc.StoreAuthError("revocado " + CREDS["token"]))
        with self.assertRaises(server.ApiError):
            stores_mod.validate_store(s["id"])
        self.assertNotIn(CREDS["token"], stores_mod.get_store(s["id"])["last_error"])

    def test_unmapped_exceptions_become_502_not_500(self):
        for exc in (OSError("red"), ValueError("json"), UnicodeDecodeError("utf-8", b"", 0, 1, "x")):
            FakeStore.fail_next.append(exc)
            with self.assertRaises(server.ApiError) as cm:
                self.connect()
            self.assertEqual(cm.exception.status, 502)

    def test_transient_validate_failure_keeps_status_and_disabled_is_respected(self):
        s = self.connect()
        FakeStore.fail_next.append(sc.StoreRateLimited("lento", 2))
        with self.assertRaises(server.ApiError):
            stores_mod.validate_store(s["id"])
        self.assertEqual(stores_mod.get_store(s["id"])["status"], "active")     # un 429 pasajero no la estropea
        with db.connect() as conn:
            conn.execute("UPDATE store_connections SET status = 'disabled'")
        stores_mod.validate_store(s["id"])
        self.assertEqual(stores_mod.get_store(s["id"])["status"], "disabled")   # validar no la reactiva

    def test_hostile_store_info_is_sanitized(self):
        orig = FakeStore.validate
        FakeStore.validate = lambda self: sc.StoreInfo(name="<img onerror=x>" + "A" * 5000, domain=self.domain,
                                                       currency="USD")
        try:
            s = self.connect("info.fake.test", name="")
            self.assertLessEqual(len(s["name"]), 80)
            FakeStore.validate = lambda self: sc.StoreInfo(name="x", domain=self.domain, currency="usd<script>")
            with self.assertRaises(server.ApiError):
                self.connect("info2.fake.test")
        finally:
            FakeStore.validate = orig

    def test_strict_settings_and_credentials_and_body(self):
        s = self.connect()
        for bad in ({"min_margin_pct": True}, {"min_margin_pct": "20"}, {"min_margin_pct": float("nan")},
                    {"min_margin_pct": float("inf")}, {"publish_as_draft": 1}, {"nuevo": 1}):
            with self.assertRaises(server.ApiError):
                stores_mod.update_store(s["id"], {"settings": bad})
        for creds in ({"token": "abc\r\nX: y"}, {"token": "a\x00b"}):
            with self.assertRaises(server.ApiError):
                stores_mod.connect_store({"platform": "fake", "domain": "z.fake.test", "credentials": creds})
        for body in ([], "x", None, 5):
            with self.assertRaises(server.ApiError) as cm:
                stores_mod.connect_store(body)
            self.assertEqual(cm.exception.status, 400)

    def test_concurrent_setting_updates_do_not_clobber(self):
        s = self.connect()
        def upd(k, v):
            with auth_context.as_user(1, "owner"):
                stores_mod.update_store(s["id"], {"settings": {k: v}})
        ts = [threading.Thread(target=upd, args=(k, v))
              for k, v in (("auto_raise_price", True), ("min_margin_pct", 33.0), ("max_price_increase_pct", 12.0),
                           ("sync_stock", False))]
        [t.start() for t in ts]
        [t.join() for t in ts]
        st = stores_mod.get_store(s["id"])["settings"]
        self.assertEqual((st["auto_raise_price"], st["min_margin_pct"], st["max_price_increase_pct"], st["sync_stock"]),
                         (True, 33.0, 12.0, False))

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
        with self.assertRaises(sc.StoreAuthError):
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


class SchemaV7Test(unittest.TestCase):
    def setUp(self):
        self.tmp = _TempDb()
        db.init_db(seed_size=10)
        auth_context.set_current(1, "owner")
        self.cid = stores_mod.connect_store({"platform": "fake", "domain": "a.fake.test",
                                             "credentials": dict(CREDS)})["id"]

    def tearDown(self):
        self.tmp.close()

    def insert_listing(self, conn, ext="900", imported=None, user=1):
        return conn.execute("INSERT INTO store_listings(user_id, connection_id, imported_id, title, external_id, "
                            "created_at) VALUES (?, ?, ?, 't', ?, 'x')", (user, self.cid, imported, ext)).lastrowid

    def test_same_external_product_cannot_be_registered_twice(self):
        import sqlite3
        with db.connect() as conn:
            self.insert_listing(conn)
            with self.assertRaises(sqlite3.IntegrityError):
                self.insert_listing(conn)                           # imported_id NULL no deduplica: lo hace este índice
            self.insert_listing(conn, ext=None)                     # varios 'pending' sin id externo: permitido
            self.insert_listing(conn, ext=None)

    def test_listing_must_belong_to_same_user_as_connection(self):
        import sqlite3
        with db.connect() as conn:
            with self.assertRaises(sqlite3.DatabaseError):
                self.insert_listing(conn, ext="5", user=2)

    def test_one_active_job_per_listing_and_action(self):
        import sqlite3
        with db.connect() as conn:
            lid = self.insert_listing(conn)
            ins = ("INSERT INTO store_jobs(user_id, listing_id, action, run_after, created_at, updated_at) "
                   "VALUES (1, ?, 'publish', 'x', 'x', 'x')")
            conn.execute(ins, (lid,))
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(ins, (lid,))                           # doble clic: no encola dos 'publish'
            conn.execute("UPDATE store_jobs SET status = 'done'")
            conn.execute(ins, (lid,))                               # terminado el anterior, se puede volver a encolar

    def test_status_checks_and_demo_marker_survive_import_removal(self):
        import sqlite3
        with db.connect() as conn:
            pid = conn.execute("SELECT id FROM products LIMIT 1").fetchone()[0]
            conn.execute("INSERT INTO imported_products(user_id, product_id, title, sell_price, last_cost, last_stock, "
                         "created_at) VALUES (1, ?, 't', 9, 1, 1, 'x')", (pid,))
            iid = conn.execute("SELECT id FROM imported_products").fetchone()[0]
            lid = conn.execute("INSERT INTO store_listings(user_id, connection_id, imported_id, product_id, is_demo, "
                               "title, external_id, created_at) VALUES (1, ?, ?, ?, 1, 't', '1', 'x')",
                               (self.cid, iid, pid)).lastrowid
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE store_listings SET status = 'inventado'")
            conn.execute("DELETE FROM imported_products WHERE id = ?", (iid,))
            row = conn.execute("SELECT imported_id, product_id, is_demo FROM store_listings WHERE id = ?", (lid,)).fetchone()
        self.assertEqual((row[0], row[1], row[2]), (None, pid, 1))      # el anuncio sigue sabiendo que es de demostración

    def test_log_survives_disconnect(self):
        with db.connect() as conn:
            conn.execute("INSERT INTO store_sync_log(user_id, connection_id, action, ok, message, created_at) "
                         "VALUES (1, ?, 'publish', 1, 'ok', 'x')", (self.cid,))
        stores_mod.disconnect_store(self.cid, {})
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*), MAX(connection_id) FROM store_sync_log").fetchone()[:], (1, None))


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

    def test_idempotent_publish_capability(self):
        self.assertFalse(sc.StoreConnector.idempotent_publish)        # por defecto un conector NO se considera repetible
        self.assertFalse(FakeStore.idempotent_publish)
        self.assertTrue(sc.get_class("shopify").idempotent_publish)   # Shopify adopta lo que ya creó

    def test_unknown_platform(self):
        with self.assertRaises(sc.StoreValidationError):
            sc.get_class("inexistente")


class NotSentSignalTest(unittest.TestCase):
    """not_sent distingue lo que seguro no llegó a la tienda de lo ambiguo (el motor decide si repite una publicación)."""

    def setUp(self):
        from store_connectors import http as shttp
        self.shttp = shttp
        self.old = shttp.ALLOW_LOOPBACK
        shttp.ALLOW_LOOPBACK = True

    def tearDown(self):
        self.shttp.ALLOW_LOOPBACK = self.old

    def test_default_is_ambiguous(self):
        self.assertFalse(sc.StoreError("x", retryable=True).not_sent)
        self.assertTrue(sc.StoreError("x", retryable=True, not_sent=True).not_sent)
        self.assertFalse(sc.StoreRateLimited("x").not_sent)

    def test_dns_failure_and_refused_connection_are_not_sent(self):
        import socket
        from unittest import mock
        with mock.patch("socket.getaddrinfo", side_effect=socket.gaierror("sin dns")):
            with self.assertRaises(sc.StoreError) as cm:
                self.shttp.request("GET", "https://tienda-que-no-existe.example/")
        self.assertTrue(cm.exception.not_sent)
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()                                              # nadie escucha: conexión rechazada
        with self.assertRaises(sc.StoreError) as cm:
            self.shttp.request("GET", f"http://127.0.0.1:{port}/", timeout=2)
        self.assertTrue(cm.exception.retryable)
        self.assertTrue(cm.exception.not_sent)

    def test_oversized_response_is_refused_without_resource_warnings(self):
        # Comprueba el comportamiento (error claro y ningún ResourceWarning). Ojo: NO discrimina r.close(); la fuga
        # que reportó el autor del conector de WooCommerce no se pudo reproducir con este escenario.
        import gc
        import socket
        import threading
        import warnings
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]

        def serve():
            conn, _ = srv.accept()
            conn.recv(4096)
            conn.sendall(b"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n\r\n" + b"x" * 20000)   # >> max_bytes
            conn.close()
        th = threading.Thread(target=serve, daemon=True)
        th.start()
        try:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                msg = ""
                try:
                    self.shttp.request("GET", f"http://127.0.0.1:{port}/", max_bytes=100)
                except sc.StoreError as e:
                    msg = str(e)
                    e.__traceback__ = None        # la traza mantendría viva la respuesta y el aviso saldría tarde
                gc.collect()                      # un socket sin cerrar avisa al recolectarse
        finally:
            th.join(3)
            srv.close()
        self.assertIn("demasiado grande", msg)
        leaks = [w for w in caught if issubclass(w.category, ResourceWarning)]
        self.assertEqual(leaks, [], [str(w.message) for w in leaks])

    def test_timeout_after_the_request_was_sent_is_ambiguous(self):
        import socket
        import threading
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        got = []

        def serve():
            conn, _ = srv.accept()
            got.append(conn.recv(4096))                        # recibe la petición y NO responde nunca
            threading.Event().wait(1.5)
            conn.close()
        t = threading.Thread(target=serve, daemon=True)
        t.start()
        try:
            with self.assertRaises(sc.StoreError) as cm:
                self.shttp.request("POST", f"http://127.0.0.1:{port}/x", body=b"{}", timeout=0.5)
        finally:
            t.join(3)
            srv.close()
        self.assertTrue(got and got[0].startswith(b"POST"))      # la tienda SÍ recibió la petición
        self.assertTrue(cm.exception.retryable)
        self.assertFalse(cm.exception.not_sent)                  # → no se puede repetir una publicación a ciegas


class RouteClassificationTest(unittest.TestCase):
    def test_store_routes_are_user_routes(self):
        for method, pattern, _fn in stores_mod.ROUTES:
            self.assertEqual(server.route_access(method, pattern), "user", (method, pattern))
        self.assertIn(("POST", r"/api/stores"), server.USER_ROUTES)


if __name__ == "__main__":
    unittest.main()
