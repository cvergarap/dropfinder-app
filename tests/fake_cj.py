"""CJ Dropshipping falso para pruebas: la API v2.0 que SUPONE backend/connectors/cj.py, en 127.0.0.1.

OJO: no es CJ. Implementa lo que el conector da por supuesto (rutas, nombres de campo, sobre {code, result, message,
data}, caducidad de tokens, 401/429); si la API real difiere, estas pruebas no lo detectarán (ver «SIN VERIFICAR» en
el encabezado de cj.py). Solo escucha en loopback y se usa con shttp.ALLOW_LOOPBACK activado por la prueba.

Estado: productos sembrados con `add_product`, API keys válidas, tokens emitidos (acceso y refresco) con caducidad real
(`access_ttl`, en segundos) y `expire_access()` para invalidar el token en el servidor (provoca 401).
Inyección de fallos `inject(kind, op=None, count=1, **kw)`; `op` ∈ token, refresh, list, detail, stock, categories,
freight o None (cualquiera): http401, http429 (retry_after=), http500, code429 (sobre con código 1600200), code_token
(sobre con código 1600001), notjson, truncated (cabecera Content-Length mayor que el cuerpo), oversize, redirect
(301 a otro host), code (code=, message=), sleep (seconds=), drop (corta sin responder), raw (data=<objeto JSON a enviar en `data`>).
Cada petición se registra SIN valores de cabeceras de credenciales (solo si el token era válido).
"""
from __future__ import annotations

import itertools
import json
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

API_KEY = "cjtest@api@" + "k9" * 12


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone(timedelta(hours=8))).isoformat(timespec="seconds")


class FakeCJ:
    def __init__(self, api_keys=(API_KEY,)):
        self.lock = threading.Lock()
        self.api_keys = set(api_keys)
        self.products: dict[str, dict] = {}
        self.categories = [{"categoryFirstName": "Hogar", "categoryFirstList": [
            {"categorySecondName": "Cocina", "categorySecondList": [
                {"categoryId": "cat-cocina", "categoryName": "Utensilios"},
                {"categoryId": "cat-bano", "categoryName": "Baño"}]}]}]
        self.requests: list[dict] = []
        self.injections: list[dict] = []
        self.access_ttl = 15 * 86400
        self.refresh_ttl = 180 * 86400
        self.reject_refresh = False
        self.token_rate_limited = False
        self._seq = itertools.count(1)
        self.access_tokens: dict[str, float] = {}       # token -> caducidad (época)
        self.refresh_tokens: dict[str, float] = {}
        self.max_page_size = 100
        self.httpd = None
        self.thread = None
        self.port = 0

    # ------------------------------------------------------------ ciclo de vida
    def start(self) -> "FakeCJ":
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _go(self):
                try:
                    fake._handle(self)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                except Exception:  # noqa: BLE001 — el fake nunca deja colgado al cliente
                    self.close_connection = True
                    raise

            do_GET = do_POST = _go

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
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/api2.0/v1"

    # ------------------------------------------------------------ configuración
    def add_product(self, pid: str, name="Producto", price="3.50", category="Utensilios", stock=None, image=None,
                    variants=None, **extra) -> dict:
        p = {"pid": pid, "productNameEn": name, "sellPrice": price, "categoryName": category,
             "productImage": image if image is not None else f"https://img.example.com/{pid}.jpg",
             "stock": stock if stock is not None else {"CN": 40, "US": 10},
             "variants": variants if variants is not None else [
                 {"vid": f"{pid}-v1", "variantSku": f"SKU-{pid}-1", "variantNameEn": "Rojo", "variantSellPrice": price}]}
        p.update(extra)
        with self.lock:
            self.products[pid] = p
        return p

    def set_product(self, pid: str, **fields) -> None:
        with self.lock:
            self.products[pid].update(fields)

    def inject(self, kind: str, *, op: str | None = None, count: int = 1, **kw) -> None:
        with self.lock:
            self.injections.append({"kind": kind, "op": op, "count": count, **kw})

    def expire_access(self) -> None:
        """El servidor deja de aceptar los tokens de acceso emitidos (el cliente aún cree que valen)."""
        with self.lock:
            self.access_tokens.clear()

    def ops(self) -> list[str]:
        return [r["op"] for r in self.requests]

    def requests_for(self, op: str) -> list[dict]:
        return [r for r in self.requests if r["op"] == op]

    # ------------------------------------------------------------ servidor
    @staticmethod
    def _op(method: str, path: str) -> str | None:
        table = {("POST", "/api2.0/v1/authentication/getAccessToken"): "token",
                 ("POST", "/api2.0/v1/authentication/refreshAccessToken"): "refresh",
                 ("GET", "/api2.0/v1/product/list"): "list", ("GET", "/api2.0/v1/product/query"): "detail",
                 ("GET", "/api2.0/v1/product/stock/getInventoryByPid"): "stock",
                 ("GET", "/api2.0/v1/product/getCategory"): "categories",
                 ("POST", "/api2.0/v1/logistic/freightCalculate"): "freight"}
        return table.get((method, path))

    def _take_injection(self, op):
        with self.lock:
            for inj in self.injections:
                if inj["count"] > 0 and inj["op"] in (None, op):
                    inj["count"] -= 1
                    return dict(inj)
        return None

    @staticmethod
    def _send(h, status, body: bytes, headers=None, content_length=None):
        h.send_response(status)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(body) if content_length is None else content_length))
        for k, v in (headers or {}).items():
            h.send_header(k, str(v))
        h.end_headers()
        h.wfile.write(body)

    def _envelope(self, h, data, code=200, message="Success", status=200, headers=None):
        body = json.dumps({"code": code, "result": code == 200, "message": message, "data": data,
                           "requestId": "fake"}).encode()
        self._send(h, status, body, headers)

    def _handle(self, h):
        url = urllib.parse.urlsplit(h.path)
        qs = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        length = int(h.headers.get("Content-Length") or 0)
        raw = h.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            body = None
        op = self._op(h.command, url.path)
        tok = h.headers.get("CJ-Access-Token") or ""
        now = time.time()
        with self.lock:
            tok_ok = bool(tok) and self.access_tokens.get(tok, 0) > now
            self.requests.append({"op": op, "qs": qs, "body": body, "token_valid": tok_ok, "has_token": bool(tok),
                                  "t": time.monotonic()})
        if op is None:
            return self._envelope(h, None, 404, "no route", 404)
        inj = self._take_injection(op)
        if inj:
            kind = inj["kind"]
            if kind == "http401":
                return self._send(h, 401, b'{"code":1600001,"result":false,"message":"Invalid token"}')
            if kind == "http429":
                hd = {"Retry-After": inj["retry_after"]} if "retry_after" in inj else {}
                return self._send(h, 429, b'{"code":1600200,"result":false,"message":"Too Many Requests"}', hd)
            if kind == "http500":
                return self._send(h, 500, b"boom")
            if kind == "code429":
                return self._envelope(h, None, 1600200, "Too Many Requests")
            if kind == "code_token":
                return self._envelope(h, None, 1600001, "Invalid token")
            if kind == "code":                     # error con código y mensaje a elección (code=, message=)
                return self._envelope(h, None, inj["code"], inj.get("message", "error"))
            if kind == "notjson":
                return self._send(h, 200, b"<html>no soy json</html>")
            if kind == "truncated":
                return self._send(h, 200, b'{"code":200,"data":{"list":[{"pid":"x"', content_length=500)
            if kind == "oversize":
                return self._send(h, 200, b" " * (3 * 1024 * 1024))
            if kind == "redirect":
                return self._send(h, 301, b"", {"Location": "http://169.254.169.254/latest/meta-data"})
            if kind == "sleep":
                time.sleep(inj.get("seconds", 1))
            if kind == "drop":
                h.close_connection = True
                return
            if kind == "raw":
                return self._envelope(h, inj["data"])
        try:
            self._route(h, op, qs, body, tok_ok)
        except KeyError as e:                       # robustez del fake: nunca deja colgado al cliente
            self._envelope(h, None, 400, f"bad request {e}", 400)

    def _issue(self):
        now = time.time()
        n = next(self._seq)
        a, r = f"acc-token-{n:04d}-" + "a" * 20, f"ref-token-{n:04d}-" + "r" * 20
        with self.lock:
            self.access_tokens[a] = now + self.access_ttl
            self.refresh_tokens[r] = now + self.refresh_ttl
        return {"accessToken": a, "accessTokenExpiryDate": _iso(now + self.access_ttl), "refreshToken": r,
                "refreshTokenExpiryDate": _iso(now + self.refresh_ttl)}

    def _route(self, h, op, qs, body, tok_ok):
        if op == "token":
            if self.token_rate_limited:
                return self._send(h, 429, b'{"code":1600200,"result":false,"message":"Too Many Requests"}')
            if not isinstance(body, dict) or body.get("apiKey") not in self.api_keys:
                return self._envelope(h, None, 1600001, "API key invalid", 200)
            return self._envelope(h, self._issue())
        if op == "refresh":
            r = (body or {}).get("refreshToken")
            with self.lock:
                valid = (not self.reject_refresh) and self.refresh_tokens.get(r, 0) > time.time()
            if not valid:
                return self._envelope(h, None, 1600001, "refresh token invalid", 200)
            return self._envelope(h, self._issue())
        if not tok_ok:
            return self._send(h, 401, b'{"code":1600001,"result":false,"message":"Invalid token"}')
        if op == "categories":
            return self._envelope(h, self.categories)
        if op == "list":
            page, size = int(qs.get("pageNum", 1)), min(int(qs.get("pageSize", 20)), self.max_page_size)
            with self.lock:
                items = [p for p in self.products.values()
                         if qs.get("productNameEn", "").lower() in str(p.get("productNameEn", "")).lower()
                         and (not qs.get("categoryId") or p.get("categoryId") == qs["categoryId"])]
            chunk = items[(page - 1) * size: page * size]
            listing = [{k: v for k, v in p.items() if k not in ("stock", "variants")} for p in chunk]
            return self._envelope(h, {"pageNum": page, "pageSize": size, "total": len(items), "list": listing})
        if op == "freight":
            return self._envelope(h, [{"logisticName": "CJPacket", "logisticPrice": 4.5, "logisticAging": "7-12"},
                                      {"logisticName": "Express", "logisticPrice": 12, "logisticAging": "3-5"}])
        pid = qs.get("pid", "")
        with self.lock:
            p = self.products.get(pid)
        if p is None:
            return self._envelope(h, None, 404, "Product not found")
        if op == "detail":
            return self._envelope(h, {k: v for k, v in p.items() if k != "stock"})
        if op == "stock":
            return self._envelope(h, {"variantInventories": [{"vid": f"{pid}-v1", "inventory": [
                {"countryCode": c, "totalInventory": n} for c, n in p["stock"].items()]}]})
