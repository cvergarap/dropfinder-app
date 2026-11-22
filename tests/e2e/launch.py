"""Lanzador de la prueba de punta a punta: servidor real + motor real + conector Shopify real
apuntando a un servidor Shopify simulado local. Panel de control en :8892 (solo para la prueba) para inspeccionar/alterar la 'tienda'."""
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
        else: self._send({})
    def do_POST(self):
        st = fake.shops["mitienda.myshopify.com"]
        if self.path == "/revoke": st["tokens"].clear(); st["revoked"] = True
        if self.path == "/unrevoke": st["tokens"].add("shpat_valido_123"); st["revoked"] = False
        if self.path.startswith("/inject/"): fake.inject(self.path.split("/")[2], count=int(self.path.split("/")[3]))
        self._send({"ok": True})
threading.Thread(target=ThreadingHTTPServer(("127.0.0.1", 8892), Ctl).serve_forever, daemon=True).start()
import server
sys.argv = ["server.py", "--port", "8891"]
server.main()
