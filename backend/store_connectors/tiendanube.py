"""Conector de Tiendanube (Nuvemshop, API REST v1). Solo biblioteca estándar. Ver docs/adr/0003-tiendanube-woocommerce.md.

QUÉ NO SE PUDO VERIFICAR (sin red en el entorno de desarrollo; todo está escrito de memoria contra la documentación
pública de Tiendanube y probado solo contra tests/fake_tiendanube.py). Antes de usarlo en serio, conéctalo a una TIENDA
DE DESARROLLO y revisa estos supuestos:
  1. Base de la API: https://api.tiendanube.com/v1/{store_id}/… (host fijo, versión «v1», id numérico de la tienda).
  2. Cabecera de autenticación: `Authentication: bearer <token>` (sí, «Authentication», no «Authorization»), y
     `User-Agent: DropFinder (<correo>)` obligatorio (sin él la API responde 400/403).
  3. GET /store devuelve `name` (objeto por idioma: {"es": …, "pt": …, "en": …}, a veces una cadena), `main_currency`
     (3 letras) y, quizá, `id` y `original_domain`. No se sabe si informa los permisos (scopes) de la app: no se
     intenta leerlos, `StoreInfo.scopes`/`missing_scopes` quedan vacíos (el permiso de escritura solo se descubre al publicar).
  4. GET /products?per_page=1 sirve de comprobación de lectura. Se recuerda que una tienda SIN productos puede
     responder 404 («Last page is 0») en vez de una lista vacía: por eso un 404 ahí se toma como «sin productos».
  5. POST /products acepta: name (objeto por idioma), description (objeto por idioma, HTML), published (booleano),
     brand (cadena), tags (cadena separada por comas), images [{src}], variants [{price, stock, sku, cost?,
     promotional_price?}] y devuelve el producto con `id` y `variants[0].id` numéricos. NO se envía `stock_management`
     (se recuerda que el stock solo cuenta si la variante lo gestiona; si en la tienda real el stock no se refleja,
     habrá que añadir `stock_management: true`). `product_type` no tiene campo equivalente y no se envía.
  6. PUT /products/{id}/variants/{variant_id} acepta price, stock y promotional_price (cadenas decimales / entero) y
     PUT /products/{id} acepta {published}. DELETE /products/{id} borra y responde 200 (o 204).
  7. GET /products/sku/{sku} devuelve 404 si no existe y el producto (objeto, o una lista con él) si existe, con
     variants[].sku. El SKU va codificado en la ruta (%XX). Si la tienda tiene SKUs repetidos devuelve uno solo.
  8. Un 404 no distingue «producto inexistente» de «tienda o ruta inexistente»: por eso, ante un 404 de un
     producto, se consulta GET /store: si ESA responde 404 es un error de configuración (StoreValidationError),
     y solo si responde bien el producto se considera borrado (StoreNotFound).
  9. 429 con cabecera `x-rate-limit-reset` en MILISEGUNDOS hasta que el cubo se vacíe (también se acepta `Retry-After`
     en segundos, que tiene prioridad). El límite (~40 solicitudes/minuto) y que la cabecera sea en ms son recuerdos
     de la documentación: SIN VERIFICAR. Un 422 lleva `description`/`message` o un objeto campo -> errores.
 10. Nombre multilingüe de la tienda: se elige `es`, luego `pt`, luego `en` y si no el primero disponible.
 11. URL del panel del producto (ADMIN_URL_TEMPLATE) y textos de setup_help sobre crear y instalar la aplicación.
 12. Códigos 402/423 (tienda suspendida o pausada) y 5xx temporales.

Decisiones de seguridad (ADR 0002/0003): red SOLO por store_connectors/http.py (HTTPS, 443, IP pública fijada, sin
redirecciones, tiempo y tamaño máximos); host FIJO (la plantilla de URL es un atributo de CLASE que solo los tests
reemplazan, nunca sale de datos del usuario ni del entorno); el `domain` es solo el id numérico de la tienda; ninguna
credencial en excepciones, `repr` ni registros (todo texto de la tienda pasa por `scrub`).

Idempotencia: antes de crear se busca por SKU (GET /products/sku/{sku}) y, si existe, se ADOPTA (se continúa con
variante y estado sobre él) en vez de duplicar. El producto se crea SIEMPRE con published=false y solo se publica al
final. Si un paso posterior falla se intenta borrar lo creado por esta llamada (reintentos acotados, respetando
retry_after) y, si no se logra, el error original lleva `orphan_external_id`. Un producto adoptado nunca se borra.
"""
from __future__ import annotations

import functools
import html
import ipaddress
import json
import logging
import math
import re
import time
import urllib.parse
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from . import http as shttp, register, scrub
from .base import (ListingRef, StoreAuthError, StoreConnector, StoreError, StoreInfo, StoreNotFound,
                   StoreProduct, StoreRateLimited, StoreValidationError)

log = logging.getLogger("dropfinder.tiendanube")

# ---------------------------------------------------------------- constantes
API_HOST = "api.tiendanube.com"                 # FIJO
AUTH_HEADER = "Authentication"
APP_NAME = "DropFinder"
TIMEOUT = 20.0
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_IMAGES = 10
MAX_STOCK = 1_000_000_000
MAX_PRICE = Decimal("1000000000")
MAX_RETRY_AFTER = 120.0
DEFAULT_RETRY_AFTER = 5.0
MAX_INLINE_WAIT = 10.0                          # espera máxima dentro de la llamada ante un 429 (luego se informa al motor)
CLEANUP_TRIES = 3
CLEANUP_MAX_SLEEP = 10.0
LANG = "es"

_STORE_ID_RE = re.compile(r"[1-9][0-9]{2,11}")
_ID_IN_URL_RE = re.compile(r"/(?:stores|admin/v2|admin|v1)/([0-9]+)(?![0-9])", re.I)
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.\-]{8,500}$")
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_EMAIL_RE = re.compile(rf"^[A-Za-z0-9._%+\-]{{1,64}}@{_LABEL}(?:\.{_LABEL})+$")
_CURRENCY_RE = re.compile(r"^[A-Za-z]{3}$")
_ID_RE = re.compile(r"^[0-9]{1,20}$")


# ---------------------------------------------------------------- utilidades de módulo
def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _money(value, what: str = "precio", *, allow_zero: bool = False) -> str:
    """Decimal con 2 decimales. Se cuantiza PRIMERO y se rechaza lo <= 0 (salvo allow_zero), NaN/inf/no numérico y > 1e9."""
    if isinstance(value, bool) or value is None:
        raise StoreValidationError(f"El {what} no es un número válido")
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise StoreValidationError(f"El {what} no es un número válido") from None
    if not d.is_finite():
        raise StoreValidationError(f"El {what} no es un número válido")
    if abs(d) > MAX_PRICE:
        raise StoreValidationError(f"El {what} está fuera de rango")
    d = d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if d < 0 or (d == 0 and not allow_zero):
        raise StoreValidationError(f"El {what} debe ser mayor que cero")
    return f"{d:.2f}"


def _stock(value) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StoreValidationError("El stock debe ser un número entero")
    if isinstance(value, float) and not math.isfinite(value):
        raise StoreValidationError("El stock no es un número válido")
    n = int(value)
    if n < 0 or n > MAX_STOCK:
        raise StoreValidationError(f"El stock debe estar entre 0 y {MAX_STOCK}")
    return n


def _foreign_errors(fn):
    """El conector nunca deja escapar excepciones que no sean StoreError."""
    @functools.wraps(fn)
    def wrapper(*a, **k):
        try:
            return fn(*a, **k)
        except StoreError:
            raise
        except (ValueError, TypeError, OverflowError, ArithmeticError, AttributeError, KeyError, IndexError):
            raise StoreValidationError("Los datos del producto no son válidos") from None
    return wrapper


def _clean_text(value, limit: int) -> str:
    s = value if isinstance(value, str) else ("" if value is None else str(value))
    s = "".join(" " if (c < " " or c == "\x7f") else c for c in s)
    return s.strip()[:limit]


def _description_html(text) -> str:
    """Las descripciones de DropFinder son texto plano: se escapa todo y los saltos de línea pasan a <br>."""
    t = (text if isinstance(text, str) else "").replace("\r\n", "\n").replace("\r", "\n")[:100000]
    return html.escape(t, quote=True).replace("\n", "<br>")


def _safe_images(urls) -> list[dict]:
    """Solo https, ASCII, sin IP literal ni usuario, <= 2048 caracteres, máximo 10. Las malas se descartan."""
    out = []
    if isinstance(urls, str):
        urls = [urls]
    for u in urls if isinstance(urls, (list, tuple)) else []:
        if not isinstance(u, str) or len(u) > 2048 or not u.isascii() or any(c.isspace() or ord(c) < 32 for c in u):
            continue
        try:
            p = urllib.parse.urlsplit(u)
            host = p.hostname or ""
            p.port
        except ValueError:
            continue
        if p.scheme != "https" or "." not in host or p.username or p.password:
            continue
        try:
            ipaddress.ip_address(host)
            continue                                         # IP literal
        except ValueError:
            pass
        out.append({"src": u})
        if len(out) >= MAX_IMAGES:
            break
    return out


def _clean_tags(tags) -> str:
    """Tiendanube recibe las etiquetas como UNA cadena separada por comas."""
    if isinstance(tags, str):
        tags = [tags]
    out = []
    for t in tags if isinstance(tags, (list, tuple)) else []:
        if isinstance(t, str):
            t = _clean_text(t.replace(",", " "), 60)
            if t:
                out.append(t)
    return ",".join(out[:50])[:1000]


def _pick_name(name, default: str = "") -> str:
    """Nombre multilingüe -> es, pt, en o el primero disponible; saneado."""
    if isinstance(name, dict):
        cand = [name.get(k) for k in ("es", "pt", "en")] + list(name.values())
        for c in cand:
            if isinstance(c, str) and c.strip():
                return _clean_text(c, 100)
        return default
    if isinstance(name, str) and name.strip():
        return _clean_text(name, 100)
    return default


def _as_id(value) -> str:
    """Id numérico de la API (entero o cadena de dígitos) -> cadena; si no es válido, ''."""
    if isinstance(value, bool):
        return ""
    if isinstance(value, int) and value > 0:
        value = str(value)
    return value if isinstance(value, str) and _ID_RE.fullmatch(value) else ""


@register
class TiendanubeConnector(StoreConnector):
    platform = "tiendanube"
    label = "Tiendanube"
    idempotent_publish = True               # publish() adopta el producto con el mismo SKU si ya existe
    credential_fields = [
        {"key": "access_token", "label": "Token de acceso de la aplicación (access_token)", "secret": True,
         "help": "Token que devuelve Tiendanube al instalar tu aplicación en la tienda (OAuth)."},
        {"key": "contact_email", "label": "Correo de contacto", "secret": False,
         "help": "Tiendanube exige identificar la aplicación con un correo en la cabecera User-Agent. "
                 "Usa uno al que puedan escribirte si hay un problema."},
    ]
    setup_help = (
        "Conectar Tiendanube (puede cambiar: verifica en la documentación de Tiendanube).\n"
        "1. Dominio: aquí va el ID numérico de tu tienda (3 a 12 dígitos). Lo ves en la URL del panel de administración "
        "(…/stores/123456/… o …/admin/v2/123456/…) o en la respuesta de instalación de la aplicación (user_id). "
        "También puedes pegar esa URL y DropFinder extrae el número. No sirve el nombre ni el dominio de la tienda.\n"
        "2. Crea una aplicación en el portal de socios de Tiendanube (partners.tiendanube.com > Mis aplicaciones > "
        "Crear aplicación) y activa los permisos «Productos» (lectura y escritura: read_products, write_products). "
        "No pidas permisos de clientes ni pedidos.\n"
        "3. Instala la aplicación en tu tienda (en modo de pruebas puedes usar una tienda de desarrollo) y completa el "
        "flujo OAuth: al terminar, Tiendanube entrega el access_token (y el user_id, que es el ID de la tienda). "
        "Copia el access_token aquí.\n"
        "4. Correo de contacto: Tiendanube obliga a que la aplicación se identifique con un correo (se envía en la "
        "cabecera User-Agent). Pon el tuyo.\n"
        "5. Prueba primero en una tienda de desarrollo. DropFinder crea los productos siempre ocultos (no publicados); "
        "mostrarlos es un paso aparte."
    )

    # Plantillas de URL: atributos de CLASE (solo los tests las reemplazan; no vienen de datos del usuario).
    API_URL_TEMPLATE = f"https://{API_HOST}/v1/{{store}}{{path}}"
    ADMIN_URL_TEMPLATE = "https://www.tiendanube.com/admin/v2/{store}/products/{product}"      # SIN VERIFICAR
    TIMEOUT = TIMEOUT
    MAX_BYTES = MAX_RESPONSE_BYTES

    # ------------------------------------------------------------ dominio y credenciales
    @classmethod
    def normalize_domain(cls, raw: str) -> str:
        bad = StoreValidationError("ID de tienda inválido: usa solo el número de tu tienda de Tiendanube (3 a 12 dígitos), "
                                   "o pega una URL del panel que lo contenga (…/stores/123456/…)")
        if not isinstance(raw, str):
            raise bad
        s = raw.strip()
        if not s or len(s) > 300 or not s.isascii() or any(c.isspace() and c != " " or ord(c) < 32 or c in "\\%@" for c in s):
            raise bad
        if _STORE_ID_RE.fullmatch(s):
            return s
        if s.isdigit() or re.fullmatch(r"[-+]?[0-9]+", s):
            raise bad                                            # negativo, con ceros a la izquierda, corto o enorme
        found = {m.group(1) for m in _ID_IN_URL_RE.finditer(s)}
        if len(found) == 1:
            if re.match(r"^[a-z][a-z0-9+.-]*://", s, re.I) and not re.match(r"^https?://", s, re.I):
                raise bad
            sid = found.pop()
            if _STORE_ID_RE.fullmatch(sid):
                return sid
            raise bad
        if found:
            raise bad                                            # varios ids distintos en rutas de panel
        # Texto libre: todos los números que aparezcan deben ser el MISMO id (sin signo negativo)
        if re.search(r"-\s*[0-9]", s):
            raise bad
        runs = set(re.findall(r"[0-9]+", s))
        if len(runs) != 1:
            raise bad
        sid = runs.pop()
        if not _STORE_ID_RE.fullmatch(sid) or re.search(r"[0-9]+[.,][0-9]+", s):
            raise bad
        return sid

    @classmethod
    def _creds(cls, credentials: dict) -> tuple[str, str]:
        c = {k: (v.strip() if isinstance(v, str) else "") for k, v in (credentials or {}).items()}
        tok, mail = c.get("access_token", ""), c.get("contact_email", "")
        if not tok:
            raise StoreValidationError("Falta el token de acceso (access_token) de la aplicación de Tiendanube")
        if not _TOKEN_RE.fullmatch(tok):
            raise StoreValidationError("El token de acceso tiene un formato inválido (sin espacios ni caracteres raros)")
        if not mail:
            raise StoreValidationError("Falta el correo de contacto (Tiendanube lo exige en la cabecera User-Agent)")
        if len(mail) > 254 or not _EMAIL_RE.fullmatch(mail):
            raise StoreValidationError("El correo de contacto no parece válido")
        return tok, mail

    @classmethod
    def check_credentials(cls, credentials: dict[str, str]) -> None:
        cls._creds(credentials)

    def __repr__(self) -> str:
        return f"<TiendanubeConnector {self.domain}>"           # nunca muestra credenciales

    # ------------------------------------------------------------ HTTP
    def _store(self) -> str:
        return self.normalize_domain(self.domain)

    def _secrets(self) -> list[str]:
        return [v for k in ("access_token",) if isinstance(v := self.credentials.get(k), str) and v]

    def _redact(self, text) -> str:
        return scrub(text, self._secrets())

    @staticmethod
    def _retry_after(hdrs: dict) -> float:
        """Segundos a esperar: Retry-After (s) o x-rate-limit-reset (ms), acotado; por defecto 5 s."""
        for key, div in (("retry-after", 1.0), ("x-rate-limit-reset", 1000.0)):
            if key in hdrs:
                try:
                    v = float(hdrs[key]) / div
                except (TypeError, ValueError):
                    continue
                if math.isfinite(v):
                    return max(1.0, min(v, MAX_RETRY_AFTER))
        return DEFAULT_RETRY_AFTER

    @staticmethod
    def _parse(resp):
        parsed = None
        if resp.body:
            try:
                parsed = json.loads(resp.body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                if resp.status < 400:
                    raise StoreError("Tiendanube devolvió una respuesta que no es JSON válido", retryable=True) from None
        return parsed

    def _send(self, method: str, path: str, payload=None):
        tok, mail = self._creds(self.credentials)
        url = self.API_URL_TEMPLATE.format(store=self._store(), path=path)
        headers = {AUTH_HEADER: f"bearer {tok}", "User-Agent": f"{APP_NAME} ({mail})"}
        return shttp.request_json(method, url, payload, headers=headers, timeout=self.TIMEOUT, max_bytes=self.MAX_BYTES)

    def _error_text(self, body) -> str:
        parts = []
        if isinstance(body, dict):
            for k in ("description", "message", "error"):
                v = body.get(k)
                if isinstance(v, (str, int)) and str(v).strip():
                    parts.append(str(v))
            for k, v in body.items():
                if k in ("description", "message", "error", "code"):
                    continue
                if isinstance(v, list):
                    parts.append(f"{k}: " + ", ".join(str(x) for x in v[:3]))
                elif isinstance(v, str):
                    parts.append(f"{k}: {v}")
        elif isinstance(body, str):
            parts.append(body)
        return self._redact("; ".join(parts[:4]))

    def _call(self, method: str, path: str, payload=None, *, missing: str = "store"):
        """Hace una llamada y devuelve el JSON (o None si no hay cuerpo). Mapea errores (ADR 0003 §4).
        missing: qué significa un 404 -> 'store' (tienda/ruta: StoreValidationError), 'product' (comprueba la tienda y
        luego StoreNotFound) o 'ok' (404 = no existe; lo devuelve como None)."""
        resp = None
        for attempt in (0, 1):
            resp = self._send(method, path, payload)
            if resp.status == 429 and attempt == 0:
                wait = self._retry_after(resp.headers)
                if wait <= MAX_INLINE_WAIT:                      # 429: no se procesó; una espera corta y se repite
                    _sleep(wait)
                    continue
            break
        status = resp.status
        body = self._parse(resp)
        if status < 400:
            return body
        if status in (401, 403):
            raise StoreAuthError("Tiendanube rechazó el token o la aplicación no tiene permisos suficientes")
        if status == 404:
            if missing == "ok":
                return None
            if missing == "product":
                self._check_store_exists()
                raise StoreNotFound("El producto ya no existe en la tienda")
            raise StoreValidationError("No se encontró la tienda o el recurso: revisa el ID de la tienda")
        if status == 429:
            raise StoreRateLimited("Tiendanube pidió esperar (demasiadas solicitudes)", self._retry_after(resp.headers))
        if status in (400, 422):
            raise StoreValidationError("Tiendanube rechazó los datos: " + (self._error_text(body) or f"HTTP {status}"))
        if status in (402, 423):
            raise StoreError(f"La tienda de Tiendanube está suspendida o pausada (HTTP {status})", retryable=False)
        if status == 408 or status >= 500:
            raise StoreError(f"Tiendanube tuvo un error temporal (HTTP {status})", retryable=True)
        raise StoreError(f"Tiendanube rechazó la solicitud (HTTP {status})", retryable=False)

    def _check_store_exists(self) -> None:
        """Ante un 404 de un producto: si la TIENDA tampoco existe es un error de configuración, no un producto borrado
        (y un delete() jamás debe tomar eso por éxito). Los errores de esta comprobación se propagan tal cual."""
        body = self._call("GET", "/store", missing="store")
        if not isinstance(body, dict):
            raise StoreError("Respuesta inesperada de Tiendanube", retryable=True)

    # ------------------------------------------------------------ contrato
    @_foreign_errors
    def validate(self) -> StoreInfo:
        store = self._store()
        data = self._call("GET", "/store", missing="store")
        if not isinstance(data, dict):
            raise StoreError("Respuesta inesperada de Tiendanube", retryable=True)
        cur = data.get("main_currency")
        if not isinstance(cur, str) or not _CURRENCY_RE.fullmatch(cur):
            raise StoreError("Tiendanube no informó una moneda válida para la tienda", retryable=False)
        # Lectura de productos (una tienda sin productos puede responder 404: se toma como lista vacía)
        self._call("GET", "/products?per_page=1&fields=id", missing="ok")
        return StoreInfo(name=_pick_name(data.get("name"), store), domain=store, currency=cur.upper(),
                         scopes=[], missing_scopes=[])

    def _admin_url(self, pid: str) -> str:
        return self.ADMIN_URL_TEMPLATE.format(store=self._store(), product=pid) if _ID_RE.fullmatch(pid or "") else ""

    @staticmethod
    def _variant_for(prod: dict, sku: str | None = None) -> str:
        """Id de la variante (la del SKU si se da; si no la primera)."""
        for v in prod.get("variants") or []:
            if isinstance(v, dict) and (sku is None or v.get("sku") == sku) and _as_id(v.get("id")):
                return _as_id(v["id"])
        return ""

    def _find_existing(self, sku: str) -> dict | None:
        """Producto ya creado con ese SKU (publicación idempotente). 404 = no existe."""
        data = self._call("GET", "/products/sku/" + urllib.parse.quote(sku, safe=""), missing="ok")
        cands = data if isinstance(data, list) else [data]
        for p in cands:
            if isinstance(p, dict) and _as_id(p.get("id")) and self._variant_for(p, sku):
                return p
        return None

    @_foreign_errors
    def publish(self, product: StoreProduct) -> ListingRef:
        # 1. Todo se valida ANTES de tocar la tienda
        title = _clean_text(product.title, 255)
        if not title:
            raise StoreValidationError("El producto necesita un título")
        sku = _clean_text(product.sku, 255)
        if not sku:
            raise StoreValidationError("El producto necesita un SKU (se usa para no duplicarlo)")
        price = _money(product.price)
        promo = None
        if product.compare_at_price is not None:
            c = _money(product.compare_at_price, "precio de oferta")
            promo = c if Decimal(c) < Decimal(price) else None   # solo si es menor que el precio
        cost = _money(product.cost, "costo", allow_zero=True) if product.cost is not None else None
        stock = _stock(product.stock)
        images = _safe_images(product.image_urls)
        self._creds(self.credentials)
        self._store()

        # 2. Adoptar un producto ya creado por un intento anterior, o crearlo
        existing = self._find_existing(sku)
        adopted = existing is not None
        published = bool(existing.get("published")) if adopted else False
        if adopted:
            pid, vid = _as_id(existing["id"]), self._variant_for(existing, sku)
        else:
            variant = {"price": price, "stock": stock, "sku": sku}
            if cost is not None:
                variant["cost"] = cost
            if promo is not None:
                variant["promotional_price"] = promo
            body = {"name": {LANG: title}, "description": {LANG: _description_html(product.description)},
                    "published": False, "variants": [variant]}
            brand = _clean_text(product.vendor, 255)
            if brand:
                body["brand"] = brand
            tags = _clean_tags(product.tags)
            if tags:
                body["tags"] = tags
            if images:
                body["images"] = images
            created = self._call("POST", "/products", body, missing="store")
            pid = _as_id(created.get("id")) if isinstance(created, dict) else ""
            if not pid:
                raise StoreError("Tiendanube no devolvió el id del producto creado", retryable=True)
            vid = self._variant_for(created)
        try:
            if not vid:
                raise StoreError("Tiendanube creó el producto sin variante", retryable=True)
            if adopted:
                self._put_variant(pid, vid, price, stock, promo)
            if product.active and not published:
                self._call("PUT", f"/products/{pid}", {"published": True}, missing="product")
        except StoreError as e:
            if not adopted:
                self._cleanup_orphan(pid, e)                     # solo se borra lo que creó esta llamada
            if isinstance(e, StoreNotFound):
                raise StoreError("El producto desapareció de la tienda durante la publicación", retryable=True) from None
            raise
        return ListingRef(external_id=pid, variant_id=vid, url=self._admin_url(pid))

    def _cleanup_orphan(self, pid: str, original: StoreError) -> None:
        """Borra el producto a medio crear; ante 429/5xx espera y reintenta (hasta CLEANUP_TRIES)."""
        for attempt in range(CLEANUP_TRIES):
            try:
                self._call("DELETE", f"/products/{pid}", missing="product")
                return
            except StoreNotFound:
                return
            except StoreError as e:
                if not e.retryable or attempt == CLEANUP_TRIES - 1:
                    break
                _sleep(min(float(getattr(e, "retry_after", 1.0)), CLEANUP_MAX_SLEEP))
            except Exception:  # noqa: BLE001
                break
        original.orphan_external_id = pid
        original.args = (f"{original.args[0] if original.args else original}. "
                         f"Además no se pudo borrar el producto incompleto ({pid}); el próximo intento lo reutiliza "
                         "(o bórralo a mano en Tiendanube)",)
        log.warning("tiendanube: producto huérfano %s en %s", pid, self._store())

    def _put_variant(self, pid: str, vid: str, price: str, stock: int, promo: str | None) -> None:
        body = {"price": price, "stock": stock}
        if promo is not None:                                    # nunca se envía null: no se borra lo que el usuario puso
            body["promotional_price"] = promo
        self._call("PUT", f"/products/{pid}/variants/{vid}", body, missing="product")

    @staticmethod
    def _check_ref(ref: ListingRef, need_variant: bool = False) -> None:
        if not _ID_RE.fullmatch(ref.external_id or ""):
            raise StoreValidationError("Identificador de producto de Tiendanube inválido")
        if need_variant and not _ID_RE.fullmatch(ref.variant_id or ""):
            raise StoreValidationError("Al anuncio le falta el identificador de la variante")

    @_foreign_errors
    def update_price_stock(self, ref: ListingRef, price: float, stock: int,
                           compare_at_price: float | None = None) -> None:
        self._check_ref(ref, need_variant=True)
        p = _money(price)
        n = _stock(stock)
        promo = None
        if compare_at_price is not None:
            c = _money(compare_at_price, "precio de oferta")
            promo = c if Decimal(c) < Decimal(p) else None
        self._put_variant(ref.external_id, ref.variant_id, p, n, promo)

    @_foreign_errors
    def set_active(self, ref: ListingRef, active: bool) -> None:
        self._check_ref(ref)
        self._call("PUT", f"/products/{ref.external_id}", {"published": bool(active)}, missing="product")

    @_foreign_errors
    def delete(self, ref: ListingRef) -> None:
        self._check_ref(ref)
        try:
            self._call("DELETE", f"/products/{ref.external_id}", missing="product")
        except StoreNotFound:
            return
