"""Lanzador de la prueba de punta a punta: servidor real + motor real + conectores reales de Shopify, Tiendanube y
WooCommerce apuntando a servidores simulados locales. Panel de control en :8892 (solo para la prueba) para
inspeccionar/alterar las 'tiendas' (/state de Shopify; /tn/... y /wc/... de las otras dos)."""
import json, os, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path[:0] = [os.path.join(ROOT, "backend"), os.path.join(ROOT, "tests")]
SP = os.environ.get("E2E_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "work")
os.makedirs(SP + "/e2e", exist_ok=True)
os.environ["DROPFINDER_DB"] = SP + "/e2e/e2e.db"
os.environ["DROPFINDER_KEY_FILE"] = SP + "/e2e/e2e.key"
os.environ["DROPFINDER_SYNTH_HISTORY"] = "0"
import store_connectors.http as shttp
shttp.ALLOW_LOOPBACK = True                       # SOLO esta prueba: el simulador escucha en 127.0.0.1
from fake_shopify import FakeShopify
from store_connectors import shopify
fake = FakeShopify().start()
fake.add_shop("mitienda.myshopify.com", tokens=("shpat_valido_123",), currency="USD")
shopify.ShopifyConnector.ADMIN_URL_TEMPLATE = fake.admin_template
shopify.ShopifyConnector.TOKEN_URL_TEMPLATE = fake.token_template

from fake_tiendanube import FakeTiendanube
from fake_woocommerce import FakeWooCommerce
from store_connectors import tiendanube, woocommerce
TN_STORE, TN_TOKEN = "1234567", "tn_token_valido_123"
WC_HOST, WC_KEY, WC_SECRET = "www.mitienda.com", "ck_" + "a1" * 20, "cs_" + "b2" * 20
fake_tn = FakeTiendanube().start()
fake_tn.add_store(TN_STORE, tokens=(TN_TOKEN,), currency="USD")
tiendanube.TiendanubeConnector.API_URL_TEMPLATE = fake_tn.api_template
fake_wc = FakeWooCommerce().start()
fake_wc.add_store(WC_HOST, key=WC_KEY, secret=WC_SECRET, currency="USD")
woocommerce.WooCommerceConnector.BASE_URL_TEMPLATE = fake_wc.base_template

def tn_state():
    out = []
    for p in fake_tn.products(TN_STORE).values():
        v = (p.get("variants") or [{}])[0]
        out.append({"id": p["id"], "sku": v.get("sku"), "status": "ACTIVE" if p.get("published") else "DRAFT",
                    "tags": [t.strip() for t in (p.get("tags") or "").split(",") if t.strip()], "stock": v.get("stock"),
                    "price": v.get("price"), "name": p.get("name")})
    return out

def wc_state():
    return [{"id": p["id"], "sku": p.get("sku"), "status": "ACTIVE" if p.get("status") == "publish" else "DRAFT",
             "tags": [t["name"] for t in p.get("tags") or []], "stock": p.get("stock_quantity"),
             "price": p.get("regular_price"), "name": p.get("name")} for p in fake_wc.products(WC_HOST)]

class Ctl(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, obj):
        b = json.dumps(obj, default=str).encode(); self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        st = fake.shops["mitienda.myshopify.com"]
        if self.path == "/state":
            def keyed(d): return {(k if isinstance(k, str) else "|".join(map(str, k))): v for k, v in d.items()}
            self._send({"products": st["products"], "variants": st["variants"], "levels": keyed(st["levels"]),
                        "requests": len(getattr(fake, "requests", []))})
        elif self.path == "/tn/state": self._send({"products": tn_state()})
        elif self.path == "/wc/state": self._send({"products": wc_state()})
        else: self._send({})
    def do_POST(self):
        st = fake.shops["mitienda.myshopify.com"]
        if self.path.startswith("/tn/seed/"):       # producto AJENO (sin etiquetas) con un SKU dado
            return self._send({"id": fake_tn.add_product(TN_STORE, self.path.split("/")[3], "", published=True, price="7.00", stock=3, name="Producto ajeno TN")})
        if self.path.startswith("/wc/seed/"):
            return self._send({"id": fake_wc.put_product(WC_HOST, name="Producto ajeno WC", sku=self.path.split("/")[3], status="publish",
                                                        regular_price="7.00", manage_stock=True, stock_quantity=3, tags=[])["id"]})
        if self.path == "/revoke": st["tokens"].clear(); st["revoked"] = True
        if self.path == "/unrevoke": st["tokens"].add("shpat_valido_123"); st["revoked"] = False
        if self.path.startswith("/inject/"): fake.inject(self.path.split("/")[2], count=int(self.path.split("/")[3]))
        self._send({"ok": True})
threading.Thread(target=ThreadingHTTPServer(("127.0.0.1", 8892), Ctl).serve_forever, daemon=True).start()
import server
sys.argv = ["server.py", "--port", "8891"]
server.main()
