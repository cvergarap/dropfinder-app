"""Cuentas de usuario (Sprint 3): contraseñas, sesiones, CSRF, Origin/Host, límite de intentos, roles,
protección del último propietario, aislamiento de datos entre usuarios (IDOR) y cabeceras de seguridad."""
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
os.environ.setdefault("DROPFINDER_DB", os.path.join(tempfile.mkdtemp(), "test.db"))

import auth  # noqa: E402
import auth_context  # noqa: E402
import db  # noqa: E402
import saved_searches as ss  # noqa: E402
import search  # noqa: E402
import server  # noqa: E402

PW = "una-clave-larga-1"
_opener = urllib.request.build_opener()       # sin el opener global: peticiones sin sesión por defecto


class Client:
    """Navegador mínimo: guarda la cookie de sesión y el token CSRF que devuelve la API."""

    def __init__(self, base):
        self.base, self.token, self.csrf, self.cookies = base, None, None, []

    def call(self, method, path, body=None, headers=None, csrf=True):
        h = {"Content-Type": "application/json"}
        if self.token:
            h["Cookie"] = f"{auth.COOKIE_NAME}={self.token}"
        if csrf and self.csrf and method not in ("GET", "HEAD"):
            h["X-CSRF-Token"] = self.csrf
        h.update(headers or {})
        req = urllib.request.Request(self.base + path, method=method, headers=h,
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            resp = _opener.open(req)
        except urllib.error.HTTPError as e:
            resp = e
        with resp:
            raw = resp.read()
            status, hdrs = resp.status if hasattr(resp, "status") else resp.code, resp.headers
        self.cookies = hdrs.get_all("Set-Cookie") or []
        for c in self.cookies:
            name, _, rest = c.partition("=")
            if name == auth.COOKIE_NAME:
                value = rest.split(";", 1)[0]
                self.token = None if "Max-Age=0" in c else value
        try:
            data = json.loads(raw) if raw else None
        except ValueError:
            data = raw.decode("utf-8", "replace")
        if isinstance(data, dict) and data.get("csrf_token"):
            self.csrf = data["csrf_token"]
        self.headers = hdrs
        return status, data

    def login(self, email, password=PW):
        return self.call("POST", "/api/auth/login", {"email": email, "password": password})


class PasswordTest(unittest.TestCase):
    def test_hash_and_verify(self):
        h = auth.hash_password("correcta horse 1")
        algo, n, r, p, salt, dk = h.split("$")
        self.assertEqual((algo, n, r, p), ("scrypt", str(2 ** 14), "8", "1"))
        self.assertTrue(auth.verify_password("correcta horse 1", h))
        self.assertFalse(auth.verify_password("correcta horse 2", h))
        self.assertNotEqual(h, auth.hash_password("correcta horse 1"))        # sal aleatoria
        self.assertFalse(auth.verify_password("x", "basura"))
        self.assertFalse(auth.verify_password("x", "md5$1$1$1$AA==$AA=="))

    def test_policy(self):
        with self.assertRaises(server.ApiError):
            auth.check_password_policy("corta", "a@b.co")
        with self.assertRaises(server.ApiError):
            auth.check_password_policy("Persona@Ejemplo.com", "persona@ejemplo.com")
        with self.assertRaises(server.ApiError):
            auth.check_password_policy("x" * 2000)
        self.assertEqual(auth.check_password_policy("suficientemente", "a@b.co"), "suficientemente")

    def test_email_casefold(self):
        self.assertEqual(auth.clean_email("  STRASSE@Ejemplo.COM "), "strasse@ejemplo.com")
        self.assertEqual(auth.norm_email("ÑANDÚ@Ej.co"), "ñandú@ej.co")
        with self.assertRaises(server.ApiError):
            auth.clean_email("sin-arroba")


class AuthHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="dropfinder-auth-")
        cls.old_db, db.DB_PATH = db.DB_PATH, os.path.join(cls.dir, "t.db")
        cls.old_bg, ss.BACKGROUND = ss.BACKGROUND, False
        db.init_db(seed_size=60)
        cls.srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        ss.BACKGROUND = cls.old_bg
        db.DB_PATH = cls.old_db
        search.invalidate()
        shutil.rmtree(cls.dir, ignore_errors=True)

    def setUp(self):
        with db.connect() as conn:
            for t in ("alerts", "imported_products", "saved_searches", "user_currency_settings",
                      "sessions", "login_attempts", "users"):
                conn.execute(f"DELETE FROM {t}")
            conn.execute("DELETE FROM sqlite_sequence WHERE name = 'users'")
            self.pids = [r[0] for r in conn.execute("SELECT id FROM products ORDER BY id LIMIT 5")]

    # ------------------------------------------------------------ utilidades
    def client(self):
        return Client(self.base)

    def owner(self, email="duena@ejemplo.com"):
        c = self.client()
        st, d = c.call("POST", "/api/auth/register", {"email": email, "name": "Dueña", "password": PW})
        self.assertEqual(st, 201, d)
        return c

    def member(self, owner, email="miembro@ejemplo.com", role="member"):
        st, d = owner.call("POST", "/api/users", {"email": email, "name": "Miembro", "password": PW, "role": role})
        self.assertEqual(st, 201, d)
        c = self.client()
        st, d = c.login(email)
        self.assertEqual(st, 200, d)
        return c, d["user"]["id"]

    # ------------------------------------------------------------ registro, login, sesión
    def test_register_only_first_time(self):
        c = self.client()
        self.assertEqual(c.call("GET", "/api/auth/status"), (200, {"has_users": False, "authenticated": False}))
        st, d = c.call("POST", "/api/auth/register", {"email": "a@b.co", "name": "A", "password": "corta"})
        self.assertEqual(st, 400)
        st, d = c.call("POST", "/api/auth/register", {"email": "Dueña@Ejemplo.com", "name": "Dueña", "password": PW})
        self.assertEqual(st, 201, d)
        self.assertEqual(d["user"]["role"], "owner")
        self.assertEqual(d["user"]["email"], "dueña@ejemplo.com")
        cookie = c.cookies[0]
        for attr in ("HttpOnly", "SameSite=Lax", "Path=/", "Max-Age=1209600"):
            self.assertIn(attr, cookie)
        self.assertNotIn("Secure", cookie)                   # sin proxy HTTPS de confianza
        self.assertEqual(c.call("GET", "/api/auth/status")[1], {"has_users": True, "authenticated": True})
        st, me = c.call("GET", "/api/auth/me")
        self.assertEqual((st, me["user"]["name"], me["csrf_token"]), (200, "Dueña", c.csrf))
        st, d = self.client().call("POST", "/api/auth/register", {"email": "otro@b.co", "name": "O", "password": PW})
        self.assertEqual(st, 403)
        with db.connect() as conn:                           # solo se guarda el hash del token
            row = conn.execute("SELECT token_hash FROM sessions").fetchone()
        self.assertNotEqual(row[0], c.token)
        self.assertEqual(len(row[0]), 64)

    def test_first_owner_receives_legacy_data(self):
        with db.connect() as conn:
            # Un usuario creado y borrado antes: con AUTOINCREMENT el propietario no recibe el id 1
            conn.execute("INSERT INTO users(email, name, password_hash, created_at) VALUES ('x@x.co', 'x', 'x', 'x')")
            conn.execute("DELETE FROM users")
            iid = conn.execute("INSERT INTO imported_products(user_id, product_id, title, sell_price, last_cost, "
                               "last_stock, created_at) VALUES (1, ?, 'Legado', 10, 1, 1, 'x')", (self.pids[0],)).lastrowid
            conn.execute("INSERT INTO alerts(imported_id, user_id, type, message, created_at) "
                         "VALUES (?, 1, 'price_up', 'Antigua', 'x')", (iid,))
            conn.execute("INSERT INTO saved_searches(user_id, name, params, created_at) VALUES (1, 'Vieja', '{}', 'x')")
        c = self.owner()
        _, me = c.call("GET", "/api/auth/me")
        self.assertNotEqual(me["user"]["id"], 1)
        self.assertEqual([i["title"] for i in c.call("GET", "/api/imports")[1]], ["Legado"])
        self.assertEqual([a["message"] for a in c.call("GET", "/api/alerts")[1]], ["Antigua"])
        self.assertEqual([s["name"] for s in c.call("GET", "/api/saved-searches")[1]["items"]], ["Vieja"])
        with db.connect() as conn:
            for t in ("imported_products", "alerts", "saved_searches"):
                self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {t} WHERE user_id = 1").fetchone()[0], 0)

    def test_login_logout_rotation(self):
        c = self.owner()
        first = c.token
        st, d = c.login("duena@ejemplo.com", "incorrecta-123")
        self.assertEqual((st, d["error"]), (401, auth.BAD_LOGIN))
        st, d = c.login("DUENA@ejemplo.com ")
        self.assertEqual(st, 200, d)
        self.assertNotEqual(c.token, first)                  # sesión nueva al iniciar sesión
        old = Client(self.base)
        old.token = first
        self.assertEqual(old.call("GET", "/api/auth/me")[0], 401)   # la anterior ya no sirve
        self.assertEqual(c.call("GET", "/api/auth/me")[0], 200)
        token = c.token
        st, _ = c.call("POST", "/api/auth/logout")
        self.assertEqual(st, 200)
        self.assertIsNone(c.token)                           # cookie borrada (Max-Age=0)
        c.token = token
        self.assertEqual(c.call("GET", "/api/auth/me")[0], 401)

    def test_no_user_enumeration(self):
        self.owner()
        calls = []
        orig = auth.verify_password
        auth.verify_password = lambda pw, h: calls.append(h) or orig(pw, h)
        try:
            a = self.client().login("nadie@ejemplo.com", "loquesea-123")
            b = self.client().login("duena@ejemplo.com", "loquesea-123")
        finally:
            auth.verify_password = orig
        self.assertEqual(a, b)
        self.assertEqual(a[0], 401)
        self.assertEqual(len(calls), 2)                     # el correo desconocido también calcula scrypt
        self.assertTrue(calls[0].startswith("scrypt$"))

    def test_session_expiry_and_sliding(self):
        c = self.owner()
        old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
        with db.connect() as conn:
            conn.execute("UPDATE sessions SET last_seen_at = ?", (old,))
        st, _ = c.call("GET", "/api/auth/me")
        self.assertEqual(st, 200)
        self.assertTrue(any(x.startswith(auth.COOKIE_NAME + "=") for x in c.cookies))   # cookie renovada
        with db.connect() as conn:
            seen, exp = conn.execute("SELECT last_seen_at, expires_at FROM sessions").fetchone()
        self.assertGreater(seen, old)
        self.assertGreater(datetime.fromisoformat(exp), datetime.now(timezone.utc) + timedelta(days=13))
        c.call("GET", "/api/auth/me")
        self.assertEqual(c.cookies, [])                     # escrituras limitadas: no se renueva en cada petición
        with db.connect() as conn:
            conn.execute("UPDATE sessions SET expires_at = ?", (old,))
        self.assertEqual(c.call("GET", "/api/auth/me")[0], 401)
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)

    def test_password_change_invalidates_sessions(self):
        a = self.owner()
        b = self.client()
        self.assertEqual(b.login("duena@ejemplo.com")[0], 200)
        st, d = a.call("PATCH", "/api/auth/me", {"current_password": "mala-clave-123", "new_password": "nueva-clave-12"})
        self.assertEqual(st, 400)
        st, d = a.call("PATCH", "/api/auth/me", {"current_password": PW, "new_password": "corta"})
        self.assertEqual(st, 400)
        st, d = a.call("PATCH", "/api/auth/me", {"name": "Nueva", "current_password": PW, "new_password": "nueva-clave-12"})
        self.assertEqual(st, 200, d)
        self.assertEqual(d["user"]["name"], "Nueva")
        self.assertEqual(a.call("GET", "/api/auth/me")[0], 200)      # este navegador sigue con sesión nueva
        self.assertEqual(b.call("GET", "/api/auth/me")[0], 401)      # los demás quedan fuera
        self.assertEqual(self.client().login("duena@ejemplo.com")[0], 401)
        self.assertEqual(self.client().login("duena@ejemplo.com", "nueva-clave-12")[0], 200)

    # ------------------------------------------------------------ CSRF, Origin, Host, 401
    def test_csrf(self):
        c = self.owner()
        body = {"product_id": self.pids[0]}
        self.assertEqual(c.call("POST", "/api/imports", body, csrf=False)[0], 403)
        self.assertEqual(c.call("POST", "/api/imports", body, headers={"X-CSRF-Token": "falso"})[0], 403)
        self.assertEqual(c.call("POST", "/api/auth/logout", csrf=False)[0], 403)
        self.assertEqual(c.call("GET", "/api/imports")[1], [])        # nada se creó
        self.assertEqual(c.call("POST", "/api/imports", body)[0], 200)

    def test_origin_and_referer(self):
        c = self.owner()
        evil = {"Origin": "http://malo.example"}
        self.assertEqual(c.call("POST", "/api/imports", {"product_id": self.pids[0]}, headers=evil)[0], 403)
        self.assertEqual(c.call("POST", "/api/imports", {"product_id": self.pids[0]}, headers={"Origin": "null"})[0], 403)
        self.assertEqual(c.call("POST", "/api/imports", {"product_id": self.pids[0]},
                                headers={"Referer": "http://malo.example/x"})[0], 403)
        self.assertEqual(self.client().call("POST", "/api/auth/login", {"email": "duena@ejemplo.com", "password": PW},
                                            headers=evil)[0], 403)           # también el login (login CSRF)
        st, _ = c.call("POST", "/api/imports", {"product_id": self.pids[0]}, headers={"Origin": self.base})
        self.assertEqual(st, 200)

    def test_host_allowlist(self):
        c = self.owner()
        port = self.srv.server_address[1]
        self.assertEqual(c.call("GET", "/api/auth/me", headers={"Host": f"malo.example:{port}"})[0], 421)
        self.assertEqual(c.call("GET", "/", headers={"Host": "malo.example"})[0], 421)
        self.assertEqual(c.call("GET", "/api/auth/me", headers={"Host": f"localhost:{port}"})[0], 200)

    def test_unauthenticated(self):
        c = self.client()
        for path in ("/api/products", "/api/imports", "/api/alerts", "/api/saved-searches", "/api/currency",
                     "/api/rankings", "/api/users", "/api/no-existe"):
            self.assertEqual(c.call("GET", path)[0], 401, path)
        self.assertEqual(c.call("POST", "/api/imports", {"product_id": 1})[0], 401)
        st, html = c.call("GET", "/")
        self.assertEqual(st, 200)                            # los archivos estáticos son públicos (pantalla de acceso)
        self.assertEqual(c.call("GET", "/api/auth/status")[0], 200)

    def test_security_headers(self):
        c = self.client()
        for path in ("/", "/app.js", "/api/auth/status", "/api/products", "/no-existe.html"):
            c.call("GET", path)
            h = c.headers
            csp = h["Content-Security-Policy"]
            for d in ("default-src 'self'", "img-src 'self' data: https:", "script-src 'self'",
                      "frame-ancestors 'none'", "base-uri 'none'", "form-action 'self'", "connect-src 'self'"):
                self.assertIn(d, csp, path)
            self.assertNotIn("unsafe-inline", csp)
            self.assertEqual(h["X-Content-Type-Options"], "nosniff")
            self.assertEqual(h["Referrer-Policy"], "same-origin")
            self.assertEqual(h["X-Frame-Options"], "DENY")
        c.call("GET", "/api/auth/status")
        self.assertEqual(c.headers["Cache-Control"], "no-store")

    def test_trusted_proxy(self):
        https = {"X-Forwarded-Proto": "https", "X-Forwarded-For": "1.1.1.1, 203.0.113.9"}
        c = self.client()      # sin proxy de confianza configurado, X-Forwarded-* se ignoran
        st, _ = c.call("POST", "/api/auth/register", {"email": "duena@ejemplo.com", "name": "D", "password": PW},
                       headers=https)
        self.assertEqual(st, 201)
        self.assertNotIn("Secure", c.cookies[0])
        old, server.TRUSTED_PROXY = server.TRUSTED_PROXY, "127.0.0.1"
        try:
            p = self.client()
            self.assertEqual(p.call("POST", "/api/auth/login", {"email": "duena@ejemplo.com", "password": PW},
                                    headers=https)[0], 200)
            cookie = p.cookies[0]
            self.assertTrue(cookie.startswith(auth.SECURE_COOKIE_NAME + "="), cookie)
            self.assertIn("Secure", cookie)
            token = cookie.split(";", 1)[0].split("=", 1)[1]
            q = self.client()
            self.assertEqual(q.call("GET", "/api/auth/me", headers={**https, "Cookie": f"{auth.SECURE_COOKIE_NAME}={token}"})[0], 200)
            self.assertEqual(q.call("GET", "/api/auth/me", headers={"Cookie": f"{auth.SECURE_COOKIE_NAME}={token}"})[0], 401)
            self.assertEqual(q.call("GET", "/api/auth/me", headers={**https, "Cookie": f"{auth.COOKIE_NAME}={token}"})[0], 401)
            q.call("POST", "/api/auth/login", {"email": "duena@ejemplo.com", "password": "mala-clave-123"}, headers=https)
            with db.connect() as conn:
                ips = {r[0] for r in conn.execute("SELECT ip FROM login_attempts WHERE ok = 0")}
            self.assertEqual(ips, {"203.0.113.9"})          # la IP que agregó el proxy, no la que inventó el cliente
        finally:
            server.TRUSTED_PROXY = old

    # ------------------------------------------------------------ límite de intentos
    def test_throttle_by_email(self):
        self.owner()
        c = self.client()
        for _ in range(auth.MAX_FAILS_EMAIL):
            self.assertEqual(c.login("duena@ejemplo.com", "mala-clave-123")[0], 401)
        st, d = c.login("duena@ejemplo.com")                 # bloqueado aunque la clave sea correcta
        self.assertEqual(st, 429, d)
        self.assertGreater(int(c.headers["Retry-After"]), 0)
        self.assertLessEqual(int(c.headers["Retry-After"]), 15 * 60 + 1)
        with db.connect() as conn:                            # pasada la ventana se libera
            conn.execute("UPDATE login_attempts SET created_at = ?",
                         ((datetime.now(timezone.utc) - timedelta(minutes=16)).isoformat(timespec="seconds"),))
        self.assertEqual(c.login("duena@ejemplo.com")[0], 200)

    def test_throttle_by_ip(self):
        self.owner()
        c = self.client()
        for i in range(auth.MAX_FAILS_IP):
            self.assertEqual(c.login(f"x{i}@ejemplo.com", "mala-clave-123")[0], 401)
        st, _ = c.login("duena@ejemplo.com")
        self.assertEqual(st, 429)
        self.assertIn("Retry-After", c.headers)

    # ------------------------------------------------------------ roles y administración
    def test_owner_only_routes(self):
        owner = self.owner()
        m, _ = self.member(owner)
        st, me = m.call("GET", "/api/auth/me")
        self.assertEqual(me["user"]["role"], "member")
        for method, path, body in [("PUT", "/api/settings", {"multiplier": 3}),
                                   ("POST", "/api/catalog/csv", {"supplier": "X", "csv": "sku;titulo;costo\na;b;1\n"}),
                                   ("DELETE", "/api/catalog?source=mock", None),
                                   ("GET", "/api/users", None), ("POST", "/api/users", {"email": "z@z.co"}),
                                   ("PATCH", "/api/users/1", {"role": "member"}), ("DELETE", "/api/users/1", None)]:
            st, d = m.call(method, path, body)
            self.assertEqual(st, 403, path)
            self.assertTrue(d["error"].startswith("Solo el propietario puede"), d)
        self.assertEqual(m.call("POST", "/api/monitor/run")[0], 200)     # el monitor revisa sus importados
        self.assertEqual(m.call("GET", "/api/settings")[0], 200)
        # Moneda: preferencias sí; la tasa (instalación) no, salvo reenviar el mismo valor
        _, cur = m.call("GET", "/api/currency")
        self.assertEqual(m.call("PUT", "/api/currency", {"display_currency": "COP"})[0], 200)
        self.assertEqual(m.call("PUT", "/api/currency", {"hedge_pct": 7})[0], 403)
        st, d = m.call("PUT", "/api/currency", {"vat_enabled": True, "hedge_pct": cur["settings"]["hedge_pct"],
                                                "rate_mode": cur["settings"]["rate_mode"]})
        self.assertEqual(st, 200, d)
        self.assertEqual(owner.call("PUT", "/api/currency", {"hedge_pct": 7})[0], 200)
        self.assertEqual(owner.call("PUT", "/api/settings", {"multiplier": 2.5})[0], 200)

    def test_user_admin_and_last_owner(self):
        owner = self.owner()
        _, me = owner.call("GET", "/api/auth/me")
        oid = me["user"]["id"]
        self.assertEqual(owner.call("POST", "/api/users", {"email": "DUENA@ejemplo.com", "name": "D", "password": PW})[0], 409)
        self.assertEqual(owner.call("POST", "/api/users", {"email": "n@e.co", "name": "N", "password": "n@e.co"})[0], 400)
        # No puede quitarse el rol, desactivarse ni borrarse
        self.assertEqual(owner.call("PATCH", f"/api/users/{oid}", {"role": "member"})[0], 409)
        self.assertEqual(owner.call("PATCH", f"/api/users/{oid}", {"disabled": True})[0], 409)
        self.assertEqual(owner.call("DELETE", f"/api/users/{oid}")[0], 409)
        m, mid = self.member(owner)
        _, users = owner.call("GET", "/api/users")
        self.assertEqual([u["role"] for u in users["items"]], ["owner", "member"])
        self.assertNotIn("password_hash", users["items"][0])
        # Segundo propietario: puede quitarle el rol al primero; el último activo queda protegido
        self.assertEqual(owner.call("PATCH", f"/api/users/{mid}", {"role": "owner"})[0], 200)
        self.assertEqual(m.call("GET", "/api/auth/me")[0], 401)      # cambio de rol: vuelve a entrar
        m.login("miembro@ejemplo.com")
        self.assertEqual(m.call("PATCH", f"/api/users/{oid}", {"role": "member"})[0], 200)
        self.assertEqual(m.call("PATCH", f"/api/users/{oid}", {"role": "owner"})[0], 200)
        self.assertEqual(owner.call("GET", "/api/auth/me")[0], 401)
        owner.login("duena@ejemplo.com")
        self.assertEqual(owner.call("PATCH", f"/api/users/{mid}", {"disabled": True})[0], 200)
        self.assertEqual(m.call("GET", "/api/auth/me")[0], 401)      # desactivado: sesiones cerradas
        st, d = self.client().login("miembro@ejemplo.com")
        self.assertEqual(st, 403, d)
        with auth_context.as_user(oid, "owner"):               # último propietario activo (carrera simulada)
            with db.connect() as conn:
                conn.execute("UPDATE users SET role = 'owner', disabled = 0 WHERE id = ?", (mid,))
            auth.update_user(mid, {"disabled": True})
            with self.assertRaises(server.ApiError) as cm:
                auth.delete_user(oid)
            self.assertEqual(cm.exception.status, 409)
        # Restablecer contraseña y reactivar
        self.assertEqual(owner.call("PATCH", f"/api/users/{mid}", {"disabled": False, "role": "member",
                                                                  "password": "restablecida-1"})[0], 200)
        self.assertEqual(self.client().login("miembro@ejemplo.com", "restablecida-1")[0], 200)
        self.assertEqual(owner.call("PATCH", "/api/users/9999", {"disabled": True})[0], 404)

    def test_delete_user_deletes_own_data(self):
        owner = self.owner()
        m, mid = self.member(owner)
        st, imp = m.call("POST", "/api/imports", {"product_id": self.pids[1], "sell_price": 0.5})
        self.assertEqual(st, 200)
        m.call("POST", "/api/monitor/run")
        self.assertEqual(m.call("POST", "/api/saved-searches", {"params": {"min_orders": 3}})[0], 200)
        m.call("PUT", "/api/currency", {"display_currency": "COP"})
        owner.call("POST", "/api/imports", {"product_id": self.pids[1]})
        with db.connect() as conn:       # tablas de otro módulo (p. ej. colecciones) creadas en diferido
            conn.execute("CREATE TABLE IF NOT EXISTS collections (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, name TEXT)")
            conn.execute("CREATE TABLE IF NOT EXISTS collection_items (collection_id INTEGER NOT NULL "
                         "REFERENCES collections(id), user_id INTEGER NOT NULL, product_id INTEGER)")
            cid = conn.execute("INSERT INTO collections(user_id, name) VALUES (?, 'Fav')", (mid,)).lastrowid
            conn.execute("INSERT INTO collection_items VALUES (?, ?, 1)", (cid, mid))
        try:
            self.assertEqual(owner.call("DELETE", f"/api/users/{mid}")[0], 200)
            with db.connect() as conn:
                for t in ("imported_products", "alerts", "saved_searches", "user_currency_settings", "sessions",
                          "collections", "collection_items"):
                    self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {t} WHERE user_id = ?", (mid,)).fetchone()[0], 0, t)
            self.assertEqual(len(owner.call("GET", "/api/imports")[1]), 1)   # lo del propietario sigue
            self.assertEqual(m.call("GET", "/api/auth/me")[0], 401)
        finally:
            with db.connect() as conn:
                conn.execute("DROP TABLE IF EXISTS collection_items")
                conn.execute("DROP TABLE IF EXISTS collections")

    # ------------------------------------------------------------ aislamiento entre usuarios (IDOR)
    def test_idor_imports(self):
        a = self.owner()
        b, _ = self.member(a)
        pid = self.pids[2]
        st, imp = a.call("POST", "/api/imports", {"product_id": pid, "sell_price": 30})
        self.assertEqual(st, 200)
        iid = imp["id"]
        self.assertEqual(b.call("GET", "/api/imports")[1], [])
        self.assertEqual(b.call("PATCH", f"/api/imports/{iid}", {"sell_price": 1})[0], 404)
        self.assertEqual(b.call("PATCH", f"/api/imports/{iid}", {"status": "active"})[0], 404)
        self.assertEqual(b.call("DELETE", f"/api/imports/{iid}")[0], 404)
        self.assertEqual(a.call("GET", "/api/imports")[1][0]["sell_price"], 30)
        # imported_id en búsqueda, detalle, comparador y alternativas: solo el propio
        self.assertIsNone(b.call("GET", f"/api/products/{pid}")[1]["imported_id"])
        self.assertEqual(a.call("GET", f"/api/products/{pid}")[1]["imported_id"], iid)
        items = b.call("GET", "/api/products?page_size=100&in_stock=0")[1]["items"]
        self.assertTrue(all(p["imported_id"] is None for p in items))
        mine = a.call("GET", "/api/products?page_size=100&in_stock=0")[1]["items"]
        self.assertEqual([p["id"] for p in mine if p["imported_id"]], [pid] if pid in [p["id"] for p in mine] else [])
        cmp = b.call("GET", f"/api/compare?ids={pid},{self.pids[3]}")[1]
        self.assertTrue(all(p["imported_id"] is None for p in cmp["items"]))
        alts = b.call("GET", f"/api/products/{pid}/alternatives")[1]
        self.assertTrue(all(p["imported_id"] is None for p in alts["items"]))
        sups = {(s["supplier"], s["source"]): s for s in b.call("GET", "/api/suppliers")[1]}
        self.assertEqual(sum(s["imported"] for s in sups.values()), 0)
        # B puede importar el mismo producto (cada usuario tiene sus importados)
        st, imp_b = b.call("POST", "/api/imports", {"product_id": pid})
        self.assertEqual(st, 200)
        self.assertEqual(b.call("POST", "/api/imports", {"product_id": pid})[0], 409)
        total = sum(s["products"] for s in a.call("GET", "/api/suppliers")[1])
        self.assertEqual(total, sum(f["count"] for f in a.call("GET", "/api/facets")[1]["source"]))   # sin duplicar

    def test_idor_alerts_and_monitor(self):
        a = self.owner()
        b, bid = self.member(a)
        pid = self.pids[0]
        a.call("POST", "/api/imports", {"product_id": pid, "sell_price": 0.5})
        b.call("POST", "/api/imports", {"product_id": pid, "sell_price": 0.4})
        st, r = a.call("POST", "/api/monitor/run")
        self.assertEqual((st, r["checked"]), (200, 1))
        self.assertTrue(r["alerts"])
        self.assertTrue(all("user_id" not in x for x in r["alerts"]))
        # El monitor de A también avisó a B (dueño de su propio importado del mismo producto)
        b_alerts = b.call("GET", "/api/alerts")[1]
        self.assertIn("negative_margin", [x["type"] for x in b_alerts])
        a_alerts = a.call("GET", "/api/alerts")[1]
        self.assertFalse({x["id"] for x in a_alerts} & {x["id"] for x in b_alerts})
        a_unread = a.call("GET", "/api/alerts/count")[1]["unread"]
        self.assertGreater(a_unread, 0)
        self.assertEqual(b.call("POST", "/api/alerts/read")[0], 200)
        self.assertEqual(b.call("GET", "/api/alerts/count")[1]["unread"], 0)
        self.assertEqual(a.call("GET", "/api/alerts/count")[1]["unread"], a_unread)   # B no marcó las de A
        # La carga de CSV del propietario genera alertas para el dueño de cada importado
        a.call("POST", "/api/catalog/csv", {"supplier": "Aisla", "csv": "sku;titulo;costo;stock\nI-1;Vaso aislado;2;5\n"})
        _, items = b.call("GET", "/api/products?source=csv&supplier=Aisla&in_stock=0")
        cpid = items["items"][0]["id"]
        b.call("POST", "/api/imports", {"product_id": cpid, "sell_price": 20})
        a.call("POST", "/api/catalog/csv", {"supplier": "Aisla", "csv": "sku;titulo;costo;stock\nI-1;Vaso aislado;9;5\n"})
        self.assertTrue(any(x["type"] == "price_up" for x in b.call("GET", "/api/alerts")[1]))
        self.assertFalse(any(x["type"] == "price_up" for x in a.call("GET", "/api/alerts")[1]))
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM alerts WHERE user_id NOT IN (SELECT id FROM users)")
                             .fetchone()[0], 0)

    def test_idor_saved_searches(self):
        a = self.owner()
        b, bid = self.member(a)
        st, s = a.call("POST", "/api/saved-searches", {"name": "De A", "params": {"category": ["Inexistente X"]}})
        self.assertEqual(st, 200, s)
        sid = s["id"]
        self.assertEqual(b.call("GET", "/api/saved-searches")[1]["items"], [])
        for method, path, body in [("PATCH", f"/api/saved-searches/{sid}", {"name": "Robada"}),
                                   ("DELETE", f"/api/saved-searches/{sid}", None),
                                   ("GET", f"/api/saved-searches/{sid}/results", None),
                                   ("POST", f"/api/saved-searches/{sid}/seen", None)]:
            self.assertEqual(b.call(method, path, body)[0], 404, path)
        self.assertEqual(a.call("GET", "/api/saved-searches")[1]["items"][0]["name"], "De A")
        # La misma búsqueda puede guardarla B (los duplicados y el tope son por usuario)
        st, sb = b.call("POST", "/api/saved-searches", {"name": "De B", "params": {"category": ["Inexistente X"]}})
        self.assertEqual(st, 200, sb)
        # Revisión: cada alerta lleva el user_id de su búsqueda, también desde un hilo de fondo sin contexto
        a.call("POST", "/api/catalog/csv", {"supplier": "Nuevo SS", "csv":
                                            "sku;titulo;costo;stock;categoria\nN-1;Producto nuevo;2;5;Inexistente X\n"})
        out = {}
        t = threading.Thread(target=lambda: out.update(ss.check_all()))
        t.start()
        t.join()
        self.assertEqual(out["checked"], 2)
        with db.connect() as conn:
            rows = dict(conn.execute("SELECT saved_search_id, user_id FROM alerts WHERE saved_search_id IS NOT NULL"))
        self.assertEqual(rows[sid], a.call("GET", "/api/auth/me")[1]["user"]["id"])
        self.assertEqual(rows[sb["id"]], bid)
        self.assertEqual([x["saved_search_id"] for x in b.call("GET", "/api/alerts")[1]], [sb["id"]])
        st, r = b.call("POST", "/api/saved-searches/check")
        self.assertEqual((st, r["checked"]), (200, 1))       # solo informa sus búsquedas

    def test_idor_currency_settings(self):
        a = self.owner()
        b, _ = self.member(a)
        self.assertEqual(a.call("PUT", "/api/currency", {"display_currency": "COP", "vat_enabled": True,
                                                         "rate_mode": "manual", "manual_rate": 4000})[0], 200)
        sb = b.call("GET", "/api/currency")[1]["settings"]
        self.assertEqual((sb["display_currency"], sb["vat_enabled"]), ("USD", False))
        self.assertEqual((sb["rate_mode"], sb["manual_rate"]), ("manual", 4000.0))     # tasa de la instalación
        b.call("PUT", "/api/currency", {"cop_rounding": "100", "vat_pct": 5})
        sa = a.call("GET", "/api/currency")[1]["settings"]
        self.assertEqual((sa["display_currency"], sa["cop_rounding"], sa["vat_pct"]), ("COP", "900", 19.0))

    def test_dev_user_mode(self):
        import contextlib
        import io
        env = {k: os.environ.get(k) for k in ("DROPFINDER_DEV_USER", "DROPFINDER_ENV")}
        try:
            with contextlib.redirect_stdout(io.StringIO()) as out:
                os.environ["DROPFINDER_DEV_USER"] = "1"
                os.environ.pop("DROPFINDER_ENV", None)
                self.assertIsNone(server.dev_user_from_env("127.0.0.1"))       # falta DROPFINDER_ENV
                os.environ["DROPFINDER_ENV"] = "development"
                self.assertIsNone(server.dev_user_from_env("0.0.0.0"))         # no loopback
                self.assertIsNone(server.dev_user_from_env("192.168.1.5"))
                self.assertEqual(server.dev_user_from_env("127.0.0.1"), 1)
            self.assertIn("MODO DESARROLLO", out.getvalue())
        finally:
            for k, v in env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        owner = self.owner()
        m, mid = self.member(owner)
        anon = self.client()
        self.assertEqual(anon.call("GET", "/api/imports")[0], 401)
        old, server.DEV_USER = server.DEV_USER, mid
        try:
            st, me = anon.call("GET", "/api/auth/me")
            self.assertEqual((st, me["user"]["id"], me["user"]["role"]), (200, mid, "member"))
            self.assertEqual(anon.call("POST", "/api/imports", {"product_id": self.pids[0]})[0], 200)
            self.assertEqual(anon.call("PUT", "/api/settings", {"multiplier": 3})[0], 403)   # rol real de la cuenta
            self.assertEqual(anon.call("POST", "/api/imports", {"product_id": self.pids[1]},
                                       headers={"Origin": "http://malo.example"})[0], 403)
            self.assertEqual(len(m.call("GET", "/api/imports")[1]), 1)
        finally:
            server.DEV_USER = old
        self.assertEqual(anon.call("GET", "/api/imports")[0], 401)

    def test_background_without_context_fails_visibly(self):
        auth_context.clear()
        try:
            with self.assertRaises(auth_context.NoUserContext):
                server.list_imports()
        finally:
            auth_context.set_current(auth_context.LEGACY_USER_ID, "owner")


if __name__ == "__main__":
    unittest.main()
