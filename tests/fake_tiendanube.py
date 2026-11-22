"""Tiendanube falso para pruebas: API REST /v1/{tienda}/… solo en 127.0.0.1 (hilo, cierre limpio).

VALIDA lo que recibe como lo haría la plataforma (cabecera de autenticación, User-Agent obligatorio, JSON, `name` como
objeto por idioma, forma y tipos de las variantes, `published` booleano, precios como cadena decimal, `images[].src`,
campos desconocidos -> 422) y mantiene estado por tienda (productos, variantes, imágenes). Registra cada petición
para poder afirmar sobre ella; `summary()` y los mensajes de fallo NUNCA repiten el token.
Las rutas llevan la tienda como prefijo: /v1/{tienda}/store, /products, /products/sku/{sku}, /products/{id},
/products/{id}/variants/{variant_id}.
"""
from __future__ import annotations

import itertools
import json
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MONEY_RE = re.compile(r"^\d+\.\d{2}$")
UA_RE = re.compile(r"^[^\s()]+ \([^\s@()]+@[^\s@()]+\.[^\s@()]+\)$")
PRODUCT_FIELDS = {"name": dict, "description": dict, "published": bool, "brand": str, "tags": str, "images": list,
                  "variants": list}
VARIANT_FIELDS = {"price": "money", "stock": "int", "sku": str, "cost": "money", "promotional_price": "money_or_null"}
PUT_PRODUCT_FIELDS = {"published": bool}
PUT_VARIANT_FIELDS = {k: v for k, v in VARIANT_FIELDS.items() if k != "sku"}
OPS = ("store", "products_list", "sku_lookup", "product_create", "product_update", "variant_update", "product_delete")
NOT_FOUND = {"code": 404, "message": "Not Found", "description": "Not found"}


class FakeTiendanube:
    def __init__(self):
        self.lock = threading.Lock()
        self.stores: dict[str, dict] = {}
        self.requests: list[dict] = []
        self.injections: list[dict] = []
        self._ids = itertools.count(5000)
        self.evil_hits = 0
        self.httpd: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.port = 0

    # ------------------------------------------------------------ ciclo de vida
    def start(self) -> "FakeTiendanube":
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
    def api_template(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1/{{store}}{{path}}"

    # ------------------------------------------------------------ configuración
    def add_store(self, store: str, *, tokens=("tn_token_valido_123",), currency="USD", name=None, scopes=None,
                  empty_list_404=True) -> dict:
        st = {"tokens": set(tokens), "currency": currency, "name": name if name is not None else {"es": f"Tienda {store}"},
              "scopes": set(scopes if scopes is not None else ("read_products", "write_products")),
              "products": {}, "empty_list_404": empty_list_404}
        self.stores[store] = st
        return st

    def inject(self, kind: str, *, op: str | None = None, method: str | None = None, count: int = 1, **kw) -> None:
        """kind: http401/403/404/422/500/502…, http429 (reset_ms, retry_after), malformed, notjson200, redirect,
        oversize, sleep, execute_then_drop (aplica la mutación y pierde la respuesta)."""
        with self.lock:
            self.injections.append({"kind": kind, "op": op, "method": method, "count": count, **kw})

    def requests_for(self, op: str) -> list[dict]:
        return [r for r in self.requests if r["op"] == op]

    def ops(self) -> list[str]:
        return [r["op"] for r in self.requests]

    def summary(self) -> list[str]:
        """Resumen SIN secretos de lo recibido (para mensajes de fallo de las pruebas)."""
        return [f"{r['method']} {r['path']} -> {r['op']}" for r in self.requests]

    def products(self, store: str) -> dict:
        return self.stores[store]["products"]

    # ------------------------------------------------------------ HTTP
    def _send(self, h, status: int, body, extra_headers=None):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        h.send_response(status)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(raw)))
        for k, v in (extra_headers or {}).items():
            h.send_header(k, v)
        h.end_headers()
        h.wfile.write(raw)

    def _take_injection(self, op: str, method: str):
        with self.lock:
            for inj in self.injections:
                if inj["count"] > 0 and inj["op"] in (None, op) and inj["method"] in (None, method):
                    inj["count"] -= 1
                    return dict(inj)
        return None

    def _apply_injection(self, h, inj) -> bool:
        k = inj["kind"]
        if k == "http429":
            hdrs = {}
            if "reset_ms" in inj:
                hdrs["x-rate-limit-reset"] = str(inj["reset_ms"])
            if "retry_after" in inj:
                hdrs["Retry-After"] = str(inj["retry_after"])
            self._send(h, 429, {"code": 429, "message": "Too Many Requests", "description": "Rate limit exceeded"}, hdrs)
        elif re.fullmatch(r"http\d{3}", k):
            self._send(h, int(k[4:]), inj.get("body") or {"code": int(k[4:]), "message": f"error {k[4:]}",
                                                         "description": f"error {k[4:]}"})
        elif k == "malformed":
            self._send(h, 200, b"{esto no es json")
        elif k == "notjson200":
            self._send(h, 200, b"<html>proxy</html>")
        elif k == "redirect":
            self._send(h, 302, b"", {"Location": f"http://127.0.0.1:{self.port}/evil"})
        elif k == "oversize":
            self._send(h, 200, b'{"pad":"' + b"a" * inj.get("size", 5000) + b'"}')
        elif k == "sleep":
            threading.Event().wait(inj.get("seconds", 0.6))
            self._send(h, 200, {})
        else:
            return False
        return True

    @staticmethod
    def _classify(method: str, rest: str) -> tuple[str | None, dict]:
        """(operación, parámetros de ruta) para la ruta relativa a la tienda."""
        if rest == "/store" and method == "GET":
            return "store", {}
        path = rest.split("?", 1)[0]
        if path == "/products":
            return ("products_list" if method == "GET" else "product_create" if method == "POST" else None), {}
        m = re.fullmatch(r"/products/sku/(.+)", path)
        if m and method == "GET":
            return "sku_lookup", {"sku": urllib.parse.unquote(m.group(1))}
        m = re.fullmatch(r"/products/(\d+)", path)
        if m:
            return {"PUT": "product_update", "DELETE": "product_delete"}.get(method), {"pid": int(m.group(1))}
        m = re.fullmatch(r"/products/(\d+)/variants/(\d+)", path)
        if m and method == "PUT":
            return "variant_update", {"pid": int(m.group(1)), "vid": int(m.group(2))}
        return None, {}

    def _handle(self, h) -> None:
        n = int(h.headers.get("Content-Length") or 0)
        raw = h.rfile.read(n) if n else b""
        method = h.command
        if h.path.startswith("/evil"):
            with self.lock:
                self.evil_hits += 1
            self._send(h, 200, {})
            return
        m = re.match(r"^/v1/([^/]+)(/.*)$", h.path)
        headers = {k.lower(): v for k, v in h.headers.items()}
        try:
            body = json.loads(raw.decode()) if raw else None
        except ValueError:
            body = None
        rec = {"method": method, "path": h.path, "headers": headers, "body": body, "raw": raw, "op": "?", "status": None}
        if not m:
            with self.lock:
                self.requests.append(rec)
            self._send(h, 404, NOT_FOUND)
            return
        store, rest = m.group(1), m.group(2)
        op, params = self._classify(method, rest)
        rec["op"] = op or "?"
        rec["store"] = store
        with self.lock:
            self.requests.append(rec)
        inj = self._take_injection(rec["op"], method)
        drop_after = bool(inj and inj["kind"] == "execute_then_drop")
        if inj and not drop_after and self._apply_injection(h, inj):
            return
        st = self.stores.get(store)
        if st is None or op is None:
            self._send(h, 404, NOT_FOUND)
            return
        auth = headers.get("authentication", "")
        tm = re.fullmatch(r"bearer (\S+)", auth)
        if not tm or tm.group(1) not in st["tokens"]:
            self._send(h, 401, {"code": 401, "message": "Unauthorized", "description": "Invalid access token"})
            return
        ua = headers.get("user-agent", "")
        if not UA_RE.match(ua) or ua.startswith("Python-urllib") or ua == "DropFinder/1.0":
            self._send(h, 400, {"code": 400, "message": "Bad Request",
                                "description": "User-Agent header is required: AppName (contact email)"})
            return
        if method in ("POST", "PUT"):
            if not headers.get("content-type", "").startswith("application/json"):
                self._send(h, 415, {"code": 415, "message": "Unsupported Media Type", "description": "Use application/json"})
                return
            if not isinstance(body, dict):
                self._send(h, 400, {"code": 400, "message": "Bad Request", "description": "Invalid JSON body"})
                return
        need = "write_products" if method in ("POST", "PUT", "DELETE") else "read_products"
        if need not in st["scopes"]:
            self._send(h, 403, {"code": 403, "message": "Forbidden", "description": f"Missing scope {need}"})
            return
        with self.lock:
            status, resp = getattr(self, "_" + op)(st, rest, body, **params)
        rec["status"] = status
        if drop_after:                           # la mutación se aplicó pero la respuesta se pierde
            h.close_connection = True
            h.connection.shutdown(2)
            return
        self._send(h, status, resp)

    # ------------------------------------------------------------ validación de tipos
    @staticmethod
    def _unprocessable(desc, errors=None):
        return 422, {"code": 422, "message": "Unprocessable Entity", "description": desc, **(errors or {})}

    def _check_value(self, key, typ, val, path):
        if typ == "money":
            ok = isinstance(val, str) and MONEY_RE.match(val)
        elif typ == "money_or_null":
            ok = val is None or (isinstance(val, str) and MONEY_RE.match(val))
        elif typ == "int":
            ok = isinstance(val, int) and not isinstance(val, bool) and 0 <= val <= 1_000_000_000
        else:
            ok = isinstance(val, typ) and not (typ is int and isinstance(val, bool))
        return None if ok else f"{path}{key}: valor inválido"

    def _check_variant(self, v, allowed, path="variants.0."):
        if not isinstance(v, dict):
            return f"{path}: debe ser un objeto"
        for k, val in v.items():
            if k not in allowed:
                return f"{path}{k}: campo desconocido"
            e = self._check_value(k, allowed[k], val, path)
            if e:
                return e
        return None

    def _check_product(self, body):
        for k, val in body.items():
            if k not in PRODUCT_FIELDS:
                return f"{k}: campo desconocido"
            e = self._check_value(k, PRODUCT_FIELDS[k], val, "")
            if e:
                return e
        name = body.get("name")
        if not isinstance(name, dict) or not name or not all(isinstance(k, str) and isinstance(v, str) for k, v in name.items()):
            return "name: debe ser un objeto por idioma"
        if not any(v.strip() for v in name.values()):
            return "name: no puede estar vacío"
        if "description" in body and not all(isinstance(v, str) for v in body["description"].values()):
            return "description: debe ser un objeto por idioma"
        if "published" not in body or not isinstance(body["published"], bool):
            return "published: debe ser booleano"
        vs = body.get("variants")
        if not isinstance(vs, list) or len(vs) < 1:
            return "variants: se necesita al menos una variante"
        for i, v in enumerate(vs):
            e = self._check_variant(v, VARIANT_FIELDS, f"variants.{i}.")
            if e:
                return e
            if "price" not in v:
                return f"variants.{i}.price: obligatorio"
            if v["price"] == "0.00":
                return f"variants.{i}.price: debe ser mayor que cero"
            if v.get("promotional_price") not in (None,) and float(v["promotional_price"]) >= float(v["price"]):
                return f"variants.{i}.promotional_price: debe ser menor que price"
        for i, im in enumerate(body.get("images", [])):
            if not (isinstance(im, dict) and set(im) == {"src"} and isinstance(im["src"], str)
                    and im["src"].startswith("https://")):
                return f"images.{i}.src: URL https obligatoria"
        if len(body.get("images", [])) > 10:
            return "images: máximo 10"
        return None

    # ------------------------------------------------------------ semántica
    @staticmethod
    def _variant_json(v):
        return dict(v)

    def _product_json(self, p):
        return {"id": p["id"], "name": p["name"], "description": p["description"], "published": p["published"],
                "brand": p["brand"], "tags": p["tags"], "images": [dict(i) for i in p["images"]],
                "variants": [self._variant_json(v) for v in p["variants"]]}

    def _store(self, st, rest, body):
        return 200, {"id": 1, "name": st["name"], "main_currency": st["currency"]}

    def _products_list(self, st, rest, body):
        if not st["products"] and st["empty_list_404"]:
            return 404, {"code": 404, "message": "Not Found", "description": "Last page is 0"}
        return 200, [self._product_json(p) for p in list(st["products"].values())[:1]]

    def _sku_lookup(self, st, rest, body, sku):
        for p in st["products"].values():
            if any(v["sku"] == sku for v in p["variants"]):
                return 200, self._product_json(p)
        return 404, NOT_FOUND

    def _product_create(self, st, rest, body):
        bad = self._check_product(body)
        if bad:
            return self._unprocessable(bad)
        pid = next(self._ids)
        variants = []
        for v in body["variants"]:
            variants.append({"id": next(self._ids), "product_id": pid, "price": v["price"], "stock": v.get("stock"),
                             "sku": v.get("sku"), "cost": v.get("cost"), "promotional_price": v.get("promotional_price")})
        st["products"][pid] = {"id": pid, "name": body["name"], "description": body.get("description", {}),
                               "published": body["published"], "brand": body.get("brand", ""), "tags": body.get("tags", ""),
                               "images": [{"id": next(self._ids), "src": i["src"], "product_id": pid}
                                          for i in body.get("images", [])], "variants": variants}
        return 201, self._product_json(st["products"][pid])

    def _product_update(self, st, rest, body, pid):
        p = st["products"].get(pid)
        if p is None:
            return 404, NOT_FOUND
        for k, val in body.items():
            if k not in PUT_PRODUCT_FIELDS:
                return self._unprocessable(f"{k}: campo desconocido")
            e = self._check_value(k, PUT_PRODUCT_FIELDS[k], val, "")
            if e:
                return self._unprocessable(e)
        p.update(body)
        return 200, self._product_json(p)

    def _variant_update(self, st, rest, body, pid, vid):
        p = st["products"].get(pid)
        v = next((x for x in (p or {"variants": []})["variants"] if x["id"] == vid), None)
        if p is None or v is None:
            return 404, NOT_FOUND
        e = self._check_variant(body, PUT_VARIANT_FIELDS, "")
        if e:
            return self._unprocessable(e)
        price = body.get("price", v["price"])
        if price == "0.00":
            return self._unprocessable("price: debe ser mayor que cero")
        if body.get("promotional_price") is not None and float(body["promotional_price"]) >= float(price):
            return self._unprocessable("promotional_price: debe ser menor que price")
        v.update(body)
        return 200, self._variant_json(v)

    def _product_delete(self, st, rest, body, pid):
        if st["products"].pop(pid, None) is None:
            return 404, NOT_FOUND
        return 200, {}
