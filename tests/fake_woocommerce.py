"""WooCommerce falso para pruebas: API REST v3 mínima en 127.0.0.1 (hilo propio, cierre limpio).

VALIDA lo que recibe (no solo lo registra): autenticación básica válida, Content-Type JSON, tipos de campos, valores de
`status`, unicidad de SKU con el código de error de aspecto real, filtro ?sku=, parámetro force, 404 con
`rest_no_route` (ruta inexistente) frente a `woocommerce_rest_product_invalid_id` (producto inexistente). Estado en
memoria por tienda; las rutas llevan la tienda como prefijo: /{tienda}/wp-json/wc/v3/….
Cada petición se registra SIN el valor de Authorization (solo si venía y si era válida).

Inyección de fallos (`inject(kind, op=None, count=1)`; `op` ∈ root, currency, system_status, list, find_sku, create,
update, delete, o None = cualquiera): http401/http403/http404/http429/http500/http502/http503, route404, product404,
html200 (página de login/WAF con 200), notjson200 (texto plano), malformed (JSON roto), redirect (301 a otro host: NO
debe seguirse), oversize, sleep, drop (corta sin aplicar), execute_then_drop (APLICA la mutación y pierde la respuesta).
"""
from __future__ import annotations

import base64
import itertools
import json
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PRODUCT_STATUSES = {"draft", "pending", "private", "publish"}       # "trash" no se acepta como valor de entrada
LIST_STATUSES = PRODUCT_STATUSES | {"any", "trash"}
CREATE_FIELDS = {"name", "type", "status", "sku", "regular_price", "sale_price", "manage_stock", "stock_quantity",
                 "description", "images", "tags"}
UPDATE_FIELDS = CREATE_FIELDS - {"type"}
PRICE_RE = re.compile(r"^\d+(\.\d+)?$")
HTML_LOGIN = (b"<!DOCTYPE html><html><head><title>Acceder</title></head><body><form action='wp-login.php'>"
              b"<input name='log'></form></body></html>")


class FakeWooCommerce:
    def __init__(self):
        self.lock = threading.Lock()
        self.stores: dict[str, dict] = {}
        self.requests: list[dict] = []
        self.injections: list[dict] = []
        self._ids = itertools.count(100)
        self.evil_hits = 0
        self.sku_error_code = "woocommerce_rest_product_sku_already_exists"
        self.httpd: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.port = 0

    # ------------------------------------------------------------ ciclo de vida
    def start(self) -> "FakeWooCommerce":
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _go(self):
                try:
                    fake._handle(self)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            do_GET = do_POST = do_PUT = do_DELETE = _go

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = False
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05})
        self.thread.start()
        return self

    def stop(self) -> None:
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.thread.join(5)
            self.httpd = None

    @property
    def base_template(self) -> str:
        return f"http://127.0.0.1:{self.port}/{{host}}"

    # ------------------------------------------------------------ configuración
    def add_store(self, host: str, *, key="ck_" + "a1" * 20, secret="cs_" + "b2" * 20, currency="USD",
                  name=None, namespaces=("wp/v2", "wc/v3"), settings_ok=True, status_ok=True) -> dict:
        st = {"key": key, "secret": secret, "currency": currency, "name": name or f"Tienda <{host.split('.')[0]}>",
              "namespaces": list(namespaces), "products": {}, "settings_ok": settings_ok, "status_ok": status_ok}
        self.stores[host] = st
        return st

    def inject(self, kind: str, *, op: str | None = None, count: int = 1, **kw) -> None:
        with self.lock:
            self.injections.append({"kind": kind, "op": op, "count": count, **kw})

    def requests_for(self, op: str) -> list[dict]:
        return [r for r in self.requests if r["op"] == op]

    def ops(self) -> list[str]:
        return [r["op"] for r in self.requests]

    def products(self, host: str) -> list[dict]:
        with self.lock:
            return [dict(p) for p in self.stores[host]["products"].values()]

    def put_product(self, host: str, **fields) -> dict:
        """Siembra un producto (p. ej. creado por un intento anterior o por el usuario a mano)."""
        with self.lock:
            pid = next(self._ids)
            p = {"id": pid, "name": "Existente", "type": "simple", "status": "draft", "sku": "", "regular_price": "1.00",
                 "sale_price": "", "manage_stock": False, "stock_quantity": None, "description": "", "images": [], "tags": []}
            p.update(fields)
            self.stores[host]["products"][pid] = p
            return dict(p)

    # ------------------------------------------------------------ HTTP
    @staticmethod
    def _send(h, status: int, body, extra=None, ctype="application/json"):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        h.send_response(status)
        h.send_header("Content-Type", ctype)
        h.send_header("Content-Length", str(len(raw)))
        for k, v in (extra or {}).items():
            h.send_header(k, v)
        h.end_headers()
        h.wfile.write(raw)

    @staticmethod
    def _err(status: int, code: str, message: str):
        return status, {"code": code, "message": message, "data": {"status": status}}, {}

    def _take_injection(self, op: str):
        with self.lock:
            for inj in self.injections:
                if inj["count"] > 0 and (inj["op"] is None or inj["op"] == op):
                    inj["count"] -= 1
                    return dict(inj)
        return None

    def _apply_injection(self, h, inj) -> bool:
        """True si ya se respondió (o se cortó) y no hay que procesar la petición."""
        k = inj["kind"]
        if re.fullmatch(r"http\d{3}", k):
            st = int(k[4:])
            hdrs = {"Retry-After": str(inj["retry_after"])} if "retry_after" in inj else {}
            if st >= 500 and inj.get("html", True):
                self._send(h, st, b"<html><body><h1>Error " + str(st).encode() + b"</h1><script>x</script></body></html>",
                           hdrs, "text/html")
            else:
                code = {401: "woocommerce_rest_cannot_view", 403: "woocommerce_rest_cannot_view",
                        404: "rest_no_route", 429: "too_many_requests"}.get(st, "error")
                self._send(h, st, {"code": inj.get("code", code), "message": inj.get("message", f"error {st}"),
                                   "data": {"status": st}}, hdrs)
        elif k == "route404":
            self._send(h, 404, {"code": "rest_no_route", "message": "No route was found matching the URL and request method.",
                                "data": {"status": 404}})
        elif k == "product404":
            self._send(h, 404, {"code": "woocommerce_rest_product_invalid_id", "message": "ID no válido.",
                                "data": {"status": 404}})
        elif k == "empty200":
            self._send(h, 200, [])
        elif k == "html200":
            self._send(h, 200, HTML_LOGIN, ctype="text/html; charset=UTF-8")
        elif k == "notjson200":
            self._send(h, 200, b"OK", ctype="text/plain")
        elif k == "malformed":
            self._send(h, 200, b'{"id": 12, "name": ')
        elif k == "redirect":
            self._send(h, 301, b"", {"Location": f"http://127.0.0.1:{self.port}/evil"})
        elif k == "oversize":
            self._send(h, 200, b'[{"pad":"' + b"a" * inj.get("size", 5000) + b'"}]')
        elif k == "sleep":
            threading.Event().wait(inj.get("seconds", 0.8))
            self._send(h, 200, [])
        elif k == "drop":
            h.close_connection = True
        else:
            return False
        return True

    def _handle(self, h) -> None:
        n = int(h.headers.get("Content-Length") or 0)
        raw = h.rfile.read(n) if n else b""
        parts = urllib.parse.urlsplit(h.path)
        query = {k: v[-1] for k, v in urllib.parse.parse_qs(parts.query, keep_blank_values=True).items()}
        if parts.path.startswith("/evil"):
            with self.lock:
                self.evil_hits += 1
            self._send(h, 200, {})
            return
        m = re.match(r"^/([^/]+)(/wp-json(?:/wc/v3(/[^?]*)?)?)/?$", parts.path)
        host = parts.path.split("/")[1] if parts.path.count("/") >= 1 else ""
        st = self.stores.get(host)
        auth = h.headers.get("Authorization")
        rec = {"method": h.command, "path": h.path, "query": query, "host": host, "op": "unknown", "body": None,
               "has_auth": auth is not None, "auth_ok": False, "content_type": h.headers.get("Content-Type", ""),
               "headers": {k.lower(): v for k, v in h.headers.items() if k.lower() != "authorization"}}
        if st and auth and auth.startswith("Basic "):
            try:
                rec["auth_ok"] = (base64.b64decode(auth[6:], validate=True).decode() == f"{st['key']}:{st['secret']}")
            except (ValueError, UnicodeDecodeError):
                pass
        try:
            rec["body"] = json.loads(raw.decode()) if raw else None
        except ValueError:
            rec["body"] = {"__invalid_json__": True}
        op, route = self._classify(h.command, m, st)
        rec["op"] = op
        with self.lock:
            self.requests.append(rec)
        inj = self._take_injection(op)
        drop_after = False
        if inj:
            if inj["kind"] == "execute_then_drop":
                drop_after = True
            elif self._apply_injection(h, inj):
                return
        if not st or not m:
            status, body, extra = self._err(404, "rest_no_route", "No route was found matching the URL and request method.")
        else:
            with self.lock:
                status, body, extra = self._dispatch(op, route, rec, st, raw)
        if drop_after:
            h.close_connection = True               # la mutación ya se aplicó; la respuesta se pierde
            return
        self._send(h, status, body, extra)

    @staticmethod
    def _classify(method: str, m, st) -> tuple[str, str]:
        if not m or not st:
            return "unknown", ""
        if m.group(2) == "/wp-json":
            return ("root", "") if method == "GET" else ("unknown", "")
        route = m.group(3) or ""
        if route == "/products":
            if method == "GET":
                return "list", route
            if method == "POST":
                return "create", route
        if re.fullmatch(r"/products/\d+", route):
            return {"PUT": "update", "DELETE": "delete", "GET": "get"}.get(method, "unknown"), route
        if route == "/settings/general/woocommerce_currency" and method == "GET":
            return "currency", route
        if route == "/system_status" and method == "GET":
            return "system_status", route
        return "unknown", route

    # ------------------------------------------------------------ lógica de la API
    def _dispatch(self, op: str, route: str, rec: dict, st: dict, raw: bytes):
        if op == "root":
            return 200, {"name": st["name"], "description": "x", "namespaces": st["namespaces"]}, {}
        if op == "unknown":
            return self._err(404, "rest_no_route", "No route was found matching the URL and request method.")
        if "wc/v3" not in st["namespaces"]:
            return self._err(404, "rest_no_route", "No route was found matching the URL and request method.")
        if not rec["auth_ok"]:
            return self._err(401, "woocommerce_rest_cannot_view", "Lo siento, no puedes listar recursos.")
        if op in ("create", "update") and not rec["content_type"].startswith("application/json"):
            return self._err(400, "rest_invalid_json", "Content-Type debe ser application/json")
        if op == "currency":
            if not st["settings_ok"]:
                return self._err(403, "woocommerce_rest_cannot_view", "Sin permiso para ver ajustes.")
            return 200, {"id": "woocommerce_currency", "value": st["currency"], "type": "select"}, {}
        if op == "system_status":
            if not st["status_ok"]:
                return self._err(403, "woocommerce_rest_cannot_view", "Sin permiso.")
            return 200, {"settings": {"currency": st["currency"]}}, {}
        products = st["products"]
        if op == "list":
            return self._list(rec["query"], products)
        pid = int(route.rsplit("/", 1)[1]) if route.startswith("/products/") else None
        if op == "create":
            return self._create(rec["body"], products)
        p = products.get(pid)
        if p is None:
            return self._err(404, "woocommerce_rest_product_invalid_id", "ID no válido.")
        if op == "get":
            return 200, dict(p), {}
        if op == "update":
            return self._update(p, rec["body"], products)
        if op == "delete":
            force = rec["query"].get("force")
            if force not in (None, "true", "false", "1", "0"):
                return self._err(400, "rest_invalid_param", "force inválido")
            if force in ("true", "1"):
                del products[pid]
                return 200, {**p, "deleted_by_force": True}, {}
            p["status"] = "trash"                      # sin force solo va a la papelera
            return 200, dict(p), {}
        return self._err(404, "rest_no_route", "x")

    def _list(self, q: dict, products: dict):
        for k in q:
            if k not in ("sku", "status", "per_page", "page"):
                return self._err(400, "rest_invalid_param", f"Parámetro desconocido: {k}")
        if "status" in q and q["status"] not in LIST_STATUSES:
            return self._err(400, "rest_invalid_param", "status inválido")
        try:
            per = int(q.get("per_page", 10))
        except ValueError:
            return self._err(400, "rest_invalid_param", "per_page inválido")
        out = []
        for p in sorted(products.values(), key=lambda x: x["id"]):
            if "sku" in q and (p["sku"] or "").lower() != q["sku"].lower():
                continue
            want = q.get("status", "any")
            if want == "any" and p["status"] == "trash":
                continue
            if want not in ("any", p["status"]):
                continue
            out.append(dict(p))
        return 200, out[:per], {}

    def _check_fields(self, body, allowed: set, creating: bool):
        if not isinstance(body, dict) or body.get("__invalid_json__"):
            return self._err(400, "rest_invalid_json", "JSON inválido")
        for k in body:
            if k not in allowed:
                return self._err(400, "rest_invalid_param", f"Campo desconocido: {k}")
        if creating:
            for req in ("name", "regular_price"):
                if req not in body:
                    return self._err(400, "rest_missing_callback_param", f"Falta {req}")
        if "name" in body and (not isinstance(body["name"], str) or not body["name"].strip()):
            return self._err(400, "rest_invalid_param", "name inválido")
        if "type" in body and body["type"] != "simple":
            return self._err(400, "rest_invalid_param", "type inválido")
        if "status" in body and body["status"] not in PRODUCT_STATUSES:
            return self._err(400, "rest_invalid_param", "status inválido")
        for k in ("regular_price", "sale_price"):
            if k in body and not (isinstance(body[k], str) and (PRICE_RE.fullmatch(body[k]) or (k == "sale_price" and body[k] == ""))):
                return self._err(400, "rest_invalid_param", f"{k} debe ser una cadena decimal")
        if "manage_stock" in body and not isinstance(body["manage_stock"], bool):
            return self._err(400, "rest_invalid_param", "manage_stock debe ser booleano")
        if "stock_quantity" in body and (isinstance(body["stock_quantity"], bool) or not isinstance(body["stock_quantity"], int)
                                         or body["stock_quantity"] < 0):
            return self._err(400, "rest_invalid_param", "stock_quantity debe ser un entero >= 0")
        if "sku" in body and not isinstance(body["sku"], str):
            return self._err(400, "rest_invalid_param", "sku inválido")
        if "description" in body and not isinstance(body["description"], str):
            return self._err(400, "rest_invalid_param", "description inválido")
        if "images" in body:
            im = body["images"]
            if not isinstance(im, list) or len(im) > 10 or not all(
                    isinstance(i, dict) and set(i) <= {"src", "alt", "name", "id"} and isinstance(i.get("src"), str)
                    and i["src"].startswith("https://") for i in im):
                return self._err(400, "rest_invalid_param", "images inválido")
        if "tags" in body:
            tg = body["tags"]
            if not isinstance(tg, list) or not all(isinstance(t, dict) and isinstance(t.get("name"), str) and t["name"]
                                                   for t in tg):
                return self._err(400, "rest_invalid_param", "tags inválido")
        rp, sp = body.get("regular_price"), body.get("sale_price")
        if rp and sp and float(sp) >= float(rp):
            return self._err(400, "woocommerce_rest_invalid_product_sale_price", "La oferta debe ser menor que el precio")
        return None

    def _sku_taken(self, sku: str, products: dict, except_id=None) -> bool:
        return bool(sku) and any(p["sku"].lower() == sku.lower() and p["status"] != "trash" and p["id"] != except_id
                                 for p in products.values())

    def _create(self, body, products):
        bad = self._check_fields(body, CREATE_FIELDS, True)
        if bad:
            return bad
        if self._sku_taken(body.get("sku", ""), products):
            return self._err(400, self.sku_error_code, "Número de referencia no válido o duplicado.")
        pid = next(self._ids)
        p = {"id": pid, "type": "simple", "status": "publish", "sku": "", "sale_price": "", "manage_stock": False,
             "stock_quantity": None, "description": "", "images": [], "tags": []}
        p.update(body)
        products[pid] = p
        return 201, dict(p), {}

    def _update(self, p, body, products):
        bad = self._check_fields(body, UPDATE_FIELDS, False)
        if bad:
            return bad
        if "sku" in body and self._sku_taken(body["sku"], products, p["id"]):
            return self._err(400, self.sku_error_code, "Número de referencia no válido o duplicado.")
        rp = body.get("regular_price", p["regular_price"])
        sp = body.get("sale_price", p["sale_price"])
        if rp and sp and float(sp) >= float(rp):
            return self._err(400, "woocommerce_rest_invalid_product_sale_price", "La oferta debe ser menor que el precio")
        p.update(body)
        return 200, dict(p), {}
