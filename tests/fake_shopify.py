"""Shopify Admin falso para pruebas: endpoint GraphQL + endpoint OAuth (client credentials), solo en 127.0.0.1.

Imita lo justo de la semántica real (userErrors, THROTTLED, 401, scopes, caducidad de tokens, redirecciones…)
para las consultas del conector. Estado en memoria por tienda. Cada petición se registra para poder afirmar sobre ella.
Las rutas llevan la tienda como prefijo: /{tienda}/admin/api/{version}/graphql.json y /{tienda}/admin/oauth/access_token.
"""
from __future__ import annotations

import itertools
import json
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

OPS = ("product", "products", "productCreate", "productVariantsBulkUpdate", "inventorySetQuantities", "inventoryActivate", "productUpdate",
       "productDelete")
OPERATION_NAMES = {"DropFinderValidate", "DropFinderLocations", "DropFinderFindByTag", "DropFinderProductTags", "ProductCreate", "VariantsBulkUpdate",
                   "InventorySet", "InventoryActivate", "ProductUpdate", "ProductDelete"}
IDEMPOTENT_FROM = (2026, 4)          # creencia compartida con el conector (SIN VERIFICAR)
OP_SCOPE = {"products": "read_products", "product": "read_products", "productCreate": "write_products", "productVariantsBulkUpdate": "write_products",
            "productUpdate": "write_products", "productDelete": "write_products",
            "inventorySetQuantities": "write_inventory", "inventoryActivate": "write_inventory"}
MONEY_RE = re.compile(r"^\d+\.\d{2}$")


class FakeShopify:
    def __init__(self):
        self.lock = threading.Lock()
        self.shops: dict[str, dict] = {}
        self.requests: list[dict] = []
        self.injections: list[dict] = []
        self.cost_available = 1000
        self.token_ttl = 86399
        self._ids = itertools.count(1000)
        self._tok = itertools.count(1)
        self.oauth_hits = 0
        self.evil_hits = 0
        self.httpd: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.port = 0

    # ------------------------------------------------------------ ciclo de vida
    def start(self) -> "FakeShopify":
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path.startswith("/evil"):
                    with fake.lock:
                        fake.evil_hits += 1
                self.send_response(200); self.send_header("Content-Length", "2"); self.end_headers(); self.wfile.write(b"{}")

            def do_POST(self):
                try:
                    fake._handle(self)
                except (BrokenPipeError, ConnectionResetError):
                    pass

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
    def admin_template(self) -> str:
        return f"http://127.0.0.1:{self.port}/{{shop}}/admin/api/{{version}}/graphql.json"

    @property
    def token_template(self) -> str:
        return f"http://127.0.0.1:{self.port}/{{shop}}/admin/oauth/access_token"

    # ------------------------------------------------------------ configuración
    def add_shop(self, shop: str, *, tokens=("shpat_valido_123",), apps=None, scopes=None, currency="USD",
                 locations=None) -> dict:
        st = {"tokens": set(tokens), "apps": dict(apps or {}), "issued": {}, "scopes": set(scopes if scopes is not None else
              ("write_products", "write_inventory", "read_locations")),
              "currency": currency, "name": f"Tienda {shop.split('.')[0]}", "products": {}, "variants": {}, "items": {},
              "levels": {}, "unstocked_on_create": False, "revoked": False,
              "locations": locations if locations is not None else [
                  {"id": "gid://shopify/Location/1", "name": "Principal", "isActive": True}]}
        self.shops[shop] = st
        return st

    def inject(self, kind: str, *, op: str | None = None, count: int = 1, **kw) -> None:
        """kind: throttled, http429, http500, http401, http404, malformed, redirect, oversize, sleep, notjson200."""
        with self.lock:
            self.injections.append({"kind": kind, "op": op, "count": count, **kw})

    def expire_tokens(self, shop: str) -> None:
        self.shops[shop]["issued"].clear()

    def requests_for(self, op: str) -> list[dict]:
        return [r for r in self.requests if r["op"] == op]

    def ops(self) -> list[str]:
        return [r["op"] for r in self.requests if r["op"] not in ("oauth",)]

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

    def _take_injection(self, op: str):
        with self.lock:
            for inj in self.injections:
                if inj["count"] > 0 and (inj["op"] is None or inj["op"] == op):
                    inj["count"] -= 1
                    return dict(inj)
        return None

    def _apply_injection(self, h, inj) -> bool:
        k = inj["kind"]
        if k == "throttled":
            self._send(h, 200, {"errors": [{"message": "Throttled", "extensions": {"code": "THROTTLED"}}],
                                "extensions": {"cost": {"requestedQueryCost": 10, "actualQueryCost": None,
                                                        "throttleStatus": {"maximumAvailable": 1000.0, "currentlyAvailable": 4,
                                                                           "restoreRate": 50.0}}}})
        elif k == "http429":
            self._send(h, 429, {"errors": "Throttled"}, {"Retry-After": str(inj.get("retry_after", 7))})
        elif k.startswith("http"):
            self._send(h, int(k[4:]), {"errors": f"error {k[4:]}"})
        elif k == "malformed":
            self._send(h, 200, b"{esto no es json")
        elif k == "notjson200":
            self._send(h, 200, b"<html>proxy</html>")
        elif k == "redirect":
            self._send(h, 302, b"", {"Location": f"http://127.0.0.1:{self.port}/evil"})
        elif k == "oversize":
            self._send(h, 200, b'{"data":{"pad":"' + b"a" * inj.get("size", 5000) + b'"}}')
        elif k == "sleep":
            threading.Event().wait(inj.get("seconds", 0.6))
            self._send(h, 200, {"data": {}})
        else:
            return False
        return True

    def _handle(self, h) -> None:
        n = int(h.headers.get("Content-Length") or 0)
        raw = h.rfile.read(n)
        try:
            body = json.loads(raw.decode())
        except ValueError:
            body = None
        m = re.match(r"^/([^/]+)/admin/(oauth/access_token|api/[^/]+/graphql\.json)$", h.path)
        if not m or m.group(1) not in self.shops:
            self._send(h, 404, {"errors": "Not Found"})
            return
        shop, st = m.group(1), self.shops[m.group(1)]
        rec = {"path": h.path, "shop": shop, "headers": {k.lower(): v for k, v in h.headers.items()}, "body": body, "op": "oauth"}
        if m.group(2) == "oauth/access_token":
            with self.lock:
                self.oauth_hits += 1
                self.requests.append(rec)
            inj = self._take_injection("oauth")
            if inj and self._apply_injection(h, inj):
                return
            ctype = rec["headers"].get("content-type", "")
            if ctype.startswith("application/x-www-form-urlencoded"):
                body = {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode(), keep_blank_values=True).items()}
                rec["body"] = body
            else:
                body = None                      # el token se pide con form-encoded; JSON no se acepta
                rec["body"] = None
            ok = (isinstance(body, dict) and body.get("grant_type") == "client_credentials"
                  and st["apps"].get(body.get("client_id")) == body.get("client_secret") and not st["revoked"])
            if not ok:
                self._send(h, 400, {"error": "invalid_client"})
                return
            tok = f"shpat_tok{next(self._tok):04d}"
            with self.lock:
                st["issued"][tok] = True
            self._send(h, 200, {"access_token": tok, "scope": ",".join(sorted(st["scopes"])), "expires_in": self.token_ttl})
            return

        query = (body or {}).get("query", "") if isinstance(body, dict) else ""
        variables = (body or {}).get("variables") or {} if isinstance(body, dict) else {}
        op = next((o for o in OPS if re.search(rf"\b{o}\(", query)), None) or (
            "validate" if "currentAppInstallation" in query else "locations")
        rec["op"] = op
        with self.lock:
            self.requests.append(rec)
        inj = self._take_injection(op)
        drop_after = bool(inj and inj["kind"] == "execute_then_drop")
        if inj and not drop_after and self._apply_injection(h, inj):
            return
        token = rec["headers"].get("x-shopify-access-token", "")
        if st["revoked"] or not (token in st["tokens"] or st["issued"].get(token)):
            self._send(h, 401, {"errors": "[API] Invalid API key or access token (unrecognized login or wrong password)"})
            return
        version = re.search(r"/api/([^/]+)/", h.path).group(1)
        bad = self._validate_request(op, query, variables, version)
        if bad:
            self._send(h, 200, {"errors": [{"message": bad, "extensions": {"code": "INVALID_FIELD_ARGUMENTS"}}]})
            return
        with self.lock:
            resp = self._execute(st, op, variables)
        if drop_after:                           # la mutación se aplicó pero la respuesta se pierde
            h.close_connection = True
            h.connection.shutdown(2)
            return
        resp.setdefault("extensions", {"cost": {"requestedQueryCost": 10, "actualQueryCost": 10, "throttleStatus": {
            "maximumAvailable": 1000.0, "currentlyAvailable": self.cost_available, "restoreRate": 50.0}}})
        self._send(h, 200, resp)

    # ------------------------------------------------------------ validación de peticiones (esquema simulado)
    def _validate_request(self, op: str, query: str, v: dict, version: str):
        """Devuelve un mensaje de error GraphQL realista si la petición no cumple el esquema simulado."""
        m = re.match(r"\s*(?:query|mutation)\s+(\w+)", query)
        if not m or m.group(1) not in OPERATION_NAMES:
            return f"Operación desconocida o sin nombre en la lista permitida: {m.group(1) if m else None}"
        key = tuple(int(x) for x in version.split("-"))
        if op in ("inventorySetQuantities", "inventoryActivate"):
            has = "@idempotent(key: $key)" in query
            if key >= IDEMPOTENT_FROM and not has:
                return f"The @idempotent directive is required for {op}"
            if key < IDEMPOTENT_FROM and has:
                return "Unknown directive @idempotent"
            if has and not (isinstance(v.get("key"), str) and re.fullmatch(r"[0-9a-f-]{36}", v["key"])):
                return "Variable $key of type String! was provided invalid value"

        def need(d, name, typ, path):
            if not isinstance(d, dict) or name not in d or d[name] is None or not isinstance(d[name], typ) \
                    or isinstance(d[name], bool) and typ is int:
                return f"Variable ${path} of type {typ.__name__} is required (invalid value for {name})"
            return None
        if op == "products":
            return need(v, "q", str, "q")
        if op == "product":
            return need(v, "id", str, "id")
        if op == "productCreate":
            return need(v, "product", dict, "product") or need(v["product"], "title", str, "product.title") or (
                "Variable $media of type [CreateMediaInput!] was provided invalid value" if v.get("media") is not None and not (
                    isinstance(v["media"], list) and all(isinstance(x, dict) and isinstance(x.get("originalSource"), str)
                                                          and x.get("mediaContentType") == "IMAGE" for x in v["media"])) else None)
        if op == "productVariantsBulkUpdate":
            r = need(v, "productId", str, "productId") or need(v, "variants", list, "variants")
            if r:
                return r
            for x in v["variants"]:
                r = need(x, "id", str, "variants.id")
                if r:
                    return r
                if "price" in x and not isinstance(x["price"], str):
                    return "Variable $variants.price must be a Money string"
                if "compareAtPrice" in x and x["compareAtPrice"] is not None and not isinstance(x["compareAtPrice"], str):
                    return "Variable $variants.compareAtPrice must be a Money string"
            return None
        if op == "inventorySetQuantities":
            r = need(v, "input", dict, "input")
            if r:
                return r
            i = v["input"]
            r = need(i, "name", str, "input.name") or need(i, "reason", str, "input.reason") or need(i, "quantities", list, "input.quantities")
            if r or not isinstance(i.get("ignoreCompareQuantity"), bool):
                return r or "Variable $input.ignoreCompareQuantity is required"
            for q in i["quantities"]:
                r = (need(q, "inventoryItemId", str, "quantities.inventoryItemId") or need(q, "locationId", str, "quantities.locationId")
                     or need(q, "quantity", int, "quantities.quantity"))
                if r:
                    return r
            return None
        if op == "inventoryActivate":
            return need(v, "inventoryItemId", str, "inventoryItemId") or need(v, "locationId", str, "locationId")
        if op == "productUpdate":
            return need(v, "product", dict, "product") or need(v["product"], "id", str, "product.id")
        if op == "productDelete":
            return need(v, "input", dict, "input") or need(v["input"], "id", str, "input.id")
        return None

    # ------------------------------------------------------------ semántica
    @staticmethod
    def _denied(field: str, scope: str, path=None) -> dict:
        e = {"message": f"Access denied for {field} field. Required access: `{scope}` access scope.",
             "extensions": {"code": "ACCESS_DENIED", "requiredAccess": f"`{scope}` access scope."}}
        if path:
            e["path"] = path
        return e

    def _execute(self, st: dict, op: str, v: dict) -> dict:
        if op == "validate":
            errs, data = [], {"shop": {"name": st["name"], "myshopifyDomain": "x.myshopify.com", "currencyCode": st["currency"]},
                              "currentAppInstallation": {"accessScopes": [{"handle": s} for s in sorted(st["scopes"])]}}
            if "read_locations" in st["scopes"]:
                data["locations"] = {"nodes": [dict(x) for x in st["locations"]]}
            else:
                data["locations"] = None
                errs.append(self._denied("locations", "read_locations", ["locations"]))
            return {"data": data, **({"errors": errs} if errs else {})}
        if op == "locations":
            if "read_locations" not in st["scopes"]:
                return {"data": None, "errors": [self._denied("locations", "read_locations")]}
            return {"data": {"locations": {"nodes": [dict(x) for x in st["locations"]]}}}
        scope = OP_SCOPE[op]
        if scope not in st["scopes"] and not (scope.startswith("read_") and "write_" + scope[5:] in st["scopes"]):
            return {"data": None, "errors": [self._denied(op, scope)]}
        return {"data": {op: getattr(self, "_" + op)(st, v)}}

    def _ue(self, field, message, code=None):
        e = {"field": field, "message": message}
        if code is not None:
            e["code"] = code
        return e

    def _product(self, st, v):
        rec = st["products"].get(v.get("id"))
        return {"id": rec["id"], "tags": rec["tags"]} if rec else None

    def seed_product(self, shop: str, tags: list, title: str = "Producto ajeno", sku: str = "") -> str:
        """Producto ya existente en la tienda (de otra instalación o del cliente) con las etiquetas dadas."""
        st = self.shops[shop]
        r = self._productCreate(st, {"product": {"title": title, "tags": tags, "status": "ACTIVE"}})
        if sku:                                  # SKU de la variante por defecto (producto del cliente)
            st["items"][st["variants"][st["products"][r["product"]["id"]]["variant"]]["item"]]["sku"] = sku
        return r["product"]["id"]

    def _products(self, st, v):
        q = v.get("q", "")
        m = re.fullmatch(r"tag:'([^']*)'", q)
        ms = re.fullmatch(r"sku:'((?:[^'\\]|\\.)*)'", q)
        want_sku = re.sub(r"\\(.)", r"\1", ms.group(1)).lower() if ms else None
        nodes = []
        for pid, rec in st["products"].items():
            var = st["variants"][rec["variant"]]
            sku = str(st["items"][var["item"]].get("sku", "")).lower()
            # Como Shopify: las etiquetas se comparan sin distinguir mayúsculas; el SKU, igual
            tags = rec["tags"] if isinstance(rec["tags"], list) else []          # el fake admite siembras hostiles
            hit = (m and m.group(1).lower() in [t.lower() for t in tags if isinstance(t, str)]) \
                or (ms and sku == want_sku)
            if hit:
                nodes.append({"id": pid, "status": rec["status"], "tags": rec["tags"],
                              "variants": {"nodes": [{"id": var["id"], "sku": st["items"][var["item"]].get("sku", ""),
                                                      "inventoryItem": {"id": var["item"]}}]}})
        return {"nodes": nodes[:5]}

    def _productCreate(self, st, v):
        p = v.get("product") or {}
        if not str(p.get("title", "")).strip():
            return {"product": None, "userErrors": [self._ue(["title"], "Title can't be blank")]}
        n = next(self._ids)
        pid, vid, iid = (f"gid://shopify/Product/{n}", f"gid://shopify/ProductVariant/{n}", f"gid://shopify/InventoryItem/{n}")
        st["products"][pid] = {"id": pid, "title": p["title"], "descriptionHtml": p.get("descriptionHtml", ""),
                               "vendor": p.get("vendor"), "productType": p.get("productType"), "tags": p.get("tags"),
                               "status": p.get("status", "DRAFT"), "media": v.get("media") or [], "variant": vid}
        st["variants"][vid] = {"id": vid, "product": pid, "item": iid, "price": "0.00", "compareAtPrice": None}
        st["items"][iid] = {"id": iid, "sku": "", "cost": None, "tracked": False}
        if not st["unstocked_on_create"]:
            st["levels"][(iid, st["locations"][0]["id"])] = 0
        return {"product": {"id": pid, "variants": {"nodes": [{"id": vid, "inventoryItem": {"id": iid}}]}}, "userErrors": []}

    def _productVariantsBulkUpdate(self, st, v):
        pid = v.get("productId")
        if pid not in st["products"]:
            return {"productVariants": None, "userErrors": [self._ue(["productId"], "Product does not exist",
                                                                    "PRODUCT_DOES_NOT_EXIST")]}
        for i, var in enumerate(v.get("variants") or []):
            rec = st["variants"].get(var.get("id"))
            if rec is None or rec["product"] != pid:
                return {"productVariants": None, "userErrors": [self._ue(["variants", str(i), "id"],
                                                                        "Variant does not exist", "PRODUCT_VARIANT_DOES_NOT_EXIST")]}
            for key in ("price", "compareAtPrice"):
                val = var.get(key)
                if val is not None and not (isinstance(val, str) and MONEY_RE.match(val)):
                    return {"productVariants": None, "userErrors": [self._ue(["variants", str(i), key],
                                                                            "Price must be a valid amount", "INVALID")]}
            if "price" in var and var["price"] is not None:
                rec["price"] = var["price"]
            if "compareAtPrice" in var:
                rec["compareAtPrice"] = var["compareAtPrice"]
            ii = var.get("inventoryItem")
            if ii:
                st["items"][rec["item"]].update(ii)
        return {"productVariants": [{"id": x["id"]} for x in st["variants"].values() if x["product"] == pid], "userErrors": []}

    def _inventorySetQuantities(self, st, v):
        i = v.get("input") or {}
        if i.get("name") != "available" or i.get("reason") != "correction" or i.get("ignoreCompareQuantity") is not True:
            return {"inventoryAdjustmentGroup": None, "userErrors": [self._ue(["input"], "Invalid input", "INVALID")]}
        for k, q in enumerate(i.get("quantities") or []):
            if q.get("inventoryItemId") not in st["items"]:
                return {"inventoryAdjustmentGroup": None, "userErrors": [self._ue(["input", "quantities", str(k)],
                        "The specified inventory item could not be found.", "ITEM_NOT_FOUND")]}
            if not isinstance(q.get("quantity"), int) or q["quantity"] < 0:
                return {"inventoryAdjustmentGroup": None, "userErrors": [self._ue(["input", "quantities", str(k), "quantity"],
                        "Quantity must be non-negative", "INVALID_QUANTITY_NEGATIVE")]}
            if q.get("locationId") not in {x["id"] for x in st["locations"]}:
                return {"inventoryAdjustmentGroup": None, "userErrors": [self._ue(["input", "quantities", str(k), "locationId"],
                        "The specified location could not be found.", "LOCATION_NOT_FOUND")]}
            key = (q["inventoryItemId"], q.get("locationId"))
            if key not in st["levels"]:
                return {"inventoryAdjustmentGroup": None, "userErrors": [self._ue(["input", "quantities", str(k)],
                        "The specified inventory item is not stocked at the location.", "ITEM_NOT_STOCKED_AT_LOCATION")]}
            st["levels"][key] = q["quantity"]
        return {"inventoryAdjustmentGroup": {"id": "gid://shopify/InventoryAdjustmentGroup/1"}, "userErrors": []}

    def _inventoryActivate(self, st, v):
        key = (v.get("inventoryItemId"), v.get("locationId"))
        if key[0] not in st["items"]:
            return {"inventoryLevel": None, "userErrors": [self._ue(["inventoryItemId"], "Inventory item not found")]}
        st["levels"].setdefault(key, 0)
        return {"inventoryLevel": {"id": "gid://shopify/InventoryLevel/1"}, "userErrors": []}

    def _productUpdate(self, st, v):
        p = v.get("product") or {}
        rec = st["products"].get(p.get("id"))
        if rec is None:
            return {"product": None, "userErrors": [self._ue(["id"], "Product does not exist")]}
        if p.get("status") not in ("ACTIVE", "DRAFT", "ARCHIVED", None):
            return {"product": None, "userErrors": [self._ue(["status"], "Invalid status")]}
        if p.get("status"):
            rec["status"] = p["status"]
        return {"product": {"id": rec["id"], "status": rec["status"]}, "userErrors": []}

    def _productDelete(self, st, v):
        pid = (v.get("input") or {}).get("id")
        rec = st["products"].pop(pid, None)
        if rec is None:
            return {"deletedProductId": None, "userErrors": [self._ue(["id"], "Product does not exist")]}
        st["variants"].pop(rec["variant"], None)
        return {"deletedProductId": pid, "userErrors": []}
