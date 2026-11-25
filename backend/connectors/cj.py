"""Conector real de CJ Dropshipping (API v2.0): búsqueda, detalle con variantes, stock y monitor de precio.

ESTADO: SIN VERIFICAR contra la API real. El entorno de desarrollo no tiene internet: este módulo se escribió contra la
documentación pública (https://developers.cjdropshipping.com/) y solo se probó contra el servidor simulado
`tests/fake_cj.py`, que implementa lo que aquí se SUPONE. Antes de usarlo con dinero real, comprueba con tu cuenta de
CJ cada punto de esta lista (si algo difiere, se corrige en las constantes y funciones `_parse_*` de este archivo):

SIN VERIFICAR
  Rutas y métodos
    - Base `https://developers.cjdropshipping.com/api2.0/v1` y las rutas de `PATHS` (getAccessToken,
      refreshAccessToken, product/list, product/query, product/stock/getInventoryByPid, product/getCategory,
      logistic/freightCalculate). En especial la de stock por producto (`getInventoryByPid`): si CJ solo ofrece stock
      por variante (`queryByVid`), hay que cambiar `get_stock`.
    - Nombres de los parámetros de consulta: `pageNum`, `pageSize`, `productNameEn` (palabra clave), `categoryId`, `pid`.
    - Que `pageSize` admita hasta 100 (se piden 50) y que las páginas empiecen en 1.
  Autenticación y token
    - Cuerpo `{"apiKey": ...}` y respuesta `data.accessToken / accessTokenExpiryDate / refreshToken /
      refreshTokenExpiryDate` (fechas ISO 8601 con zona horaria). Cabecera `CJ-Access-Token`.
    - Caducidad (se supone ~15 días el token de acceso y ~180 el de refresco; si la fecha no se puede leer se usa 1 día) y
      que `refreshAccessToken` acepte `{"refreshToken": ...}` y devuelva el mismo formato.
    - Que obtener un token esté limitado (se supone 1 vez cada 5 minutos): por eso el token se guarda en memoria y se
      reutiliza; tras un reinicio del servidor se pide uno nuevo.
  Respuestas y errores
    - Sobre `{"code": 200, "result": true, "message": ..., "data": ...}`; `code != 200` es error.
    - HTTP 401 (o código 1600001) = token inválido o caducado; HTTP 429 (o código 1600200) = límite de tasa; 5xx =
      error del servidor. Los códigos numéricos de `TOKEN_CODES` y `RATE_CODES` son suposiciones.
    - Cabecera `Retry-After` en 429 (si no viene se usa espera exponencial).
  Límites
    - Límite de tasa: se asume ~1 petición por segundo (`MIN_INTERVAL`); los planes de CJ pueden permitir más o menos.
  Campos de producto
    - Lista: `pid`, `productNameEn`/`productName`, `productImage`, `sellPrice` (texto, puede ser un rango «1.2 -- 3.4»),
      `categoryName`, `categoryId`; la lista NO trae stock ni envío. Detalle: los mismos más `variants[]` con `vid`,
      `variantSku`, `variantNameEn`, `variantSellPrice`, `variantImage`.
    - Stock: `data.variantInventories[].inventory[]` con `countryCode` y `totalInventory` (también se aceptan `storageNum`
      y una lista plana). Si no hay ningún dato de stock se considera «desconocido» (no «cero»).
    - Envío: `freightCalculate` devuelve una lista con `logisticPrice` y `logisticAging` («7-12» días).
    - El enlace de ficha `https://cjdropshipping.com/product/-p-<pid>.html` se construye aquí; no viene de la API.

QUÉ NORMALIZA Y QUÉ NO (no se inventan métricas que CJ no da)
    - supplier='CJ Dropshipping', supplier_sku = `pid`, source='api' (lo pone db.upsert_products), título/categoría/
      etiquetas saneados, imagen y enlace solo http(s), cost = precio de venta de CJ (en un rango, el MÁXIMO: nunca se
      promete un margen que no se pueda cumplir), stock y almacén (país con más existencias) solo cuando se consulta stock.
    - Sin dato en CJ: orders, orders_7d, orders_prev_7d, rating y reviews quedan en 0 (el score de CJ será bajo y la
      tendencia 0: es la realidad, no un fallo); shipping_cost queda en 0 salvo que se pida `ship_to` en el detalle;
      shipping_days queda en DEFAULT_SHIPPING_DAYS (valor por defecto nuestro, columna obligatoria) y warehouse en «—»
      mientras no se consulte stock.

SEGURIDAD
    - Host fijo (API_HOST) y rutas fijas; el cliente HTTPS compartido (store_connectors.http) resuelve a IP públicas,
      no sigue redirecciones y limita bytes y tiempo. Nada de la respuesta se usa como URL a visitar.
    - Todo texto de CJ se sanea (sin etiquetas, sin caracteres de control) y toda URL se valida (solo http/https, sin
      usuario/contraseña). Números hostiles (NaN, infinito, negativos, texto) nunca producen un error 500.
    - La API key y los tokens no se registran ni aparecen en mensajes de error (scrub).
"""
from __future__ import annotations

import hashlib
import html
import json
import math
import re
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from urllib.parse import urlencode, urlsplit

from store_connectors import http as shttp
from store_connectors.base import StoreAuthError, StoreError, StoreNotFound, StoreRateLimited, scrub

from .base import NormalizedProduct, PriceStock, SupplierConnector

SUPPLIER = "CJ Dropshipping"
API_HOST = "developers.cjdropshipping.com"
BASE_URL = f"https://{API_HOST}/api2.0/v1"
PATHS = {
    "token": "/authentication/getAccessToken",
    "refresh": "/authentication/refreshAccessToken",
    "list": "/product/list",
    "detail": "/product/query",
    "stock": "/product/stock/getInventoryByPid",
    "categories": "/product/getCategory",
    "freight": "/logistic/freightCalculate",
}
TOKEN_CODES = {401, 1600001}            # SIN VERIFICAR: «token inválido o caducado» dentro del sobre JSON
RATE_CODES = {429, 1600200}             # SIN VERIFICAR: «demasiadas peticiones»
NOT_FOUND_CODES = {404}

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
OP_TIMEOUT = 15.0                       # por operación de red
CALL_DEADLINE = 60.0                    # plazo TOTAL de una llamada lógica (token + reintentos + esperas)
MAX_RETRIES = 3                         # reintentos de lecturas (429, 5xx, red, JSON inválido)
BACKOFF_BASE = 1.0
BACKOFF_CAP = 20.0                      # una espera nunca supera esto (ni el Retry-After que mande CJ)
MIN_INTERVAL = 1.0                      # separación mínima entre peticiones con la misma API key
TOKEN_SAFETY_MARGIN = 300               # se renueva 5 min antes de que caduque
DEFAULT_TOKEN_TTL = 86400               # si CJ no da una fecha legible
MAX_TOKEN_TTL = 200 * 86400
DEFAULT_SHIPPING_DAYS = 15
PAGE_SIZE = 50
MAX_PAGE_SIZE = 100
MAX_PRICE = Decimal("1000000")
MAX_STOCK = 10 ** 9
UNKNOWN_WAREHOUSE = "—"
_PID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_KEY_RE = re.compile(r"^[\x21-\x7e]{1,200}$")
_TAG_RE = re.compile(r"<[^>]*>")
_SCRIPT_RE = re.compile(r"<\s*(script|style)\b[^>]*>.*?<\s*/\s*\1\s*>", re.I | re.S)
_NUM_RE = re.compile(r"\d+(?:\.\d+)?")


# ---------------------------------------------------------------- errores (mismo árbol que las tiendas)

class CJError(StoreError):
    """Fallo al hablar con CJ."""


class CJAuthError(StoreAuthError):
    """La API key o el token fueron rechazados."""


class CJRateLimited(StoreRateLimited):
    """CJ pidió esperar y se agotaron los reintentos o el plazo."""


# ---------------------------------------------------------------- saneado y conversión segura de tipos

def clean_text(value, limit: int = 200) -> str:
    """Texto de CJ → texto plano: sin etiquetas HTML, sin caracteres de control ni de dirección, espacios colapsados.
    Un valor que no es texto o número (lista, objeto, None) da ''. Nunca lanza excepciones."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return ""
    s = str(value)[:limit * 4 + 200]
    s = html.unescape(s)
    s = _SCRIPT_RE.sub(" ", s)                                         # el contenido de script/style también se descarta
    s = _TAG_RE.sub(" ", s).replace("<", " ").replace(">", " ")
    s = s.encode("utf-8", "ignore").decode("utf-8", "ignore")          # sin sustitutos sueltos
    s = "".join(" " if unicodedata.category(c) in ("Cc", "Zl", "Zp") else c
                for c in s if unicodedata.category(c) not in ("Cf", "Cs", "Co", "Cn"))
    return re.sub(r"\s+", " ", s).strip()[:limit]


def _json_first_text(value):
    """CJ a veces manda un texto que en realidad es una lista JSON (`["a","b"]`): se toma el primer elemento."""
    if isinstance(value, str) and value.lstrip().startswith("["):
        try:
            data = json.loads(value)
        except ValueError:
            return value
        if isinstance(data, list):
            return next((x for x in data if isinstance(x, str) and x.strip()), "")
    return value


def safe_url(value, limit: int = 500) -> str:
    """Solo http(s) con host, sin usuario/contraseña, sin espacios ni caracteres de control; si no, ''."""
    value = _json_first_text(value)
    if not isinstance(value, str):
        return ""
    u = value.strip()
    # sin espacios, control, no ASCII ni caracteres que rompen un atributo HTML (" ' < > ` \): se rechaza, no se arregla
    if not u or len(u) > limit or any(ord(c) <= 32 or ord(c) >= 127 or c in "\"'<>`\\" for c in u):
        return ""
    try:
        parts = urlsplit(u)
        host = parts.hostname
        parts.port                       # noqa: B018 — fuerza la validación del puerto
    except ValueError:
        return ""
    if parts.scheme.lower() not in ("http", "https") or not host or parts.username or parts.password:
        return ""
    return u


def parse_money(value) -> Decimal | None:
    """Precio de CJ → Decimal con 2 decimales, o None si no es utilizable. Acepta números, texto («3.5», «USD 3.5»)
    y rangos («1.2 -- 3.4», «1.2-3.4», «1.2~3.4»): de un rango se toma el MÁXIMO. Rechaza NaN, infinitos, negativos,
    cero y valores absurdos (> 1.000.000)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        nums = [repr(value)] if value > 0 else []
    elif isinstance(value, str):
        s = value.strip()[:100]
        if not s or s.startswith("-") or re.search(r"\d[eE][+-]?\d", s):      # sin negativos ni notación científica
            return None
        s = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", s)                       # separador de miles: 1,234.50
        nums = _NUM_RE.findall(s)
    else:
        return None
    if not nums or len(nums) > 4:
        return None
    try:
        best = max(Decimal(n) for n in nums)
    except InvalidOperation:
        return None
    if not best.is_finite() or best <= 0 or best > MAX_PRICE:
        return None
    best = best.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return best if best > 0 else None


def parse_int(value, default: int = 0, hi: int = MAX_STOCK) -> int:
    """Entero ≥ 0 acotado; basura → default. Nunca lanza (ni con 10**400)."""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return min(max(value, 0), hi)
    try:
        if isinstance(value, str):
            f = float(value.strip()[:30])
        elif isinstance(value, float):
            f = value
        else:
            return default
    except (ValueError, OverflowError):
        return default
    if not math.isfinite(f):
        return default
    return int(min(max(f, 0), hi))


def parse_stock(value) -> int | None:
    """Existencias de un dato de CJ: entero ≥ 0 acotado SOLO si es un número real (int/float no bool, o texto numérico).
    Cualquier otra cosa («N/A», «», {}, [1], True, «unlimited») es None = desconocido, NUNCA cero."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return min(max(value, 0), MAX_STOCK)
    if isinstance(value, float):
        return int(min(max(value, 0), MAX_STOCK)) if math.isfinite(value) else None
    if isinstance(value, str) and re.fullmatch(r"\s*-?\d{1,30}(\.\d{1,10})?\s*", value):
        return int(min(max(float(value), 0), MAX_STOCK))
    return None


def parse_country(value) -> str:
    v = clean_text(value, 8).upper()
    return v if re.fullmatch(r"[A-Z]{2}", v) else ""


def parse_pid(value) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return ""
    s = str(value).strip()
    return s if _PID_RE.match(s) else ""


def product_url_for(pid: str) -> str:
    return f"https://cjdropshipping.com/product/-p-{pid}.html"       # SIN VERIFICAR (ver docstring)


def _title_of(d: dict) -> str:
    for k in ("productNameEn", "productName", "nameEn", "name"):
        t = clean_text(_json_first_text(d.get(k)), 200)
        if t:
            return t
    return ""


def normalize_item(d, *, supplier: str = SUPPLIER) -> NormalizedProduct | None:
    """Un producto de la lista o del detalle → NormalizedProduct, o None si no sirve (sin id, título o precio válidos)."""
    if not isinstance(d, dict):
        return None
    pid = parse_pid(d.get("pid") if d.get("pid") is not None else d.get("id"))
    title = _title_of(d)
    cost = parse_money(d.get("sellPrice"))
    if not pid or not title or cost is None:
        return None
    category = clean_text(d.get("categoryName") or d.get("threeCategoryName"), 80) or "Sin categoría"
    return NormalizedProduct(
        supplier=supplier, supplier_sku=pid, title=title, category=category,
        cost=float(cost), shipping_cost=0.0, shipping_days=DEFAULT_SHIPPING_DAYS, warehouse=UNKNOWN_WAREHOUSE,
        stock=0, orders=0, orders_7d=0, orders_prev_7d=0, rating=0.0, reviews=0,
        image=safe_url(d.get("productImage") or d.get("image")), product_url=product_url_for(pid),
        tags=[category.lower()])


@dataclass
class SearchPage:
    items: list = field(default_factory=list)       # NormalizedProduct (sin stock ni envío)
    total: int = 0                                  # total que informa CJ (0 si no lo informa)
    page: int = 1
    raw_count: int = 0                              # elementos recibidos (incluye los descartados por inválidos)
    skipped: int = 0                                # descartados por no tener id, título o precio válidos
    has_more: bool = False
    total_known: bool = True                        # False si CJ no informó un total (no se puede saber si hay más)


@dataclass
class StockInfo:
    total: int
    warehouse: str                                  # país con más existencias ('' si no se sabe)


@dataclass
class CJDetail:
    product: NormalizedProduct
    variants: list = field(default_factory=list)    # [{vid, sku, name, price (str), image}]


# ---------------------------------------------------------------- estado compartido (token y ritmo por API key)

@dataclass
class _Token:
    access: str
    access_exp: float                               # época (s)
    refresh: str = ""
    refresh_exp: float = 0.0


_LOCK = threading.RLock()                # solo secciones CORTAS (nunca durante la E/S de red)
_KEY_LOCKS: dict[str, threading.Lock] = {}   # un candado por clave: la E/S del token solo frena a quien usa esa clave
_TOKENS: dict[str, _Token] = {}
_NEXT_OK: dict[str, float] = {}                     # clave -> instante (reloj monótono) antes del cual no se pide
_MAX_ENTRIES = 20


def forget_tokens() -> None:
    """Olvida tokens y ritmos (desconectar CJ, cambiar la clave, pruebas)."""
    with _LOCK:
        _TOKENS.clear()
        _NEXT_OK.clear()


def _key_lock(key: str) -> threading.Lock:
    with _LOCK:
        lk = _KEY_LOCKS.get(key)
        if lk is None:
            lk = _KEY_LOCKS[key] = threading.Lock()
            if len(_KEY_LOCKS) > _MAX_ENTRIES:
                for old in list(_KEY_LOCKS):
                    if old != key and not _KEY_LOCKS[old].locked():
                        del _KEY_LOCKS[old]
                        break
        return lk


def _cache_key(base_url: str, api_key: str) -> str:
    return hashlib.sha256(f"{base_url}|{api_key}".encode()).hexdigest()


def _parse_expiry(value, now: float) -> float:
    """Fecha ISO 8601 de CJ → época. Ilegible → ahora + 1 día. Siempre acotada: ni ya vencida ni a años vista."""
    exp = None
    if isinstance(value, str) and value.strip():
        s = value.strip()[:40]
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            exp = dt.timestamp()
        except ValueError:
            exp = None
    if exp is None or not math.isfinite(exp):
        exp = now + DEFAULT_TOKEN_TTL
    return min(max(exp, now + 60), now + MAX_TOKEN_TTL)


def _token_text(v) -> str:
    return v if isinstance(v, str) and 8 <= len(v) <= 4000 and re.fullmatch(r"[\x21-\x7e]+", v) else ""


def _rewrap(e: StoreError) -> StoreError:
    """Los mensajes del cliente compartido hablan de «la tienda»: aquí el interlocutor es CJ."""
    msg = str(e).replace("de la tienda", "de CJ Dropshipping").replace("la tienda", "CJ Dropshipping") \
                .replace("con la tienda", "con CJ Dropshipping")
    cls = CJAuthError if isinstance(e, StoreAuthError) else CJError
    return cls(msg, retryable=e.retryable, not_sent=e.not_sent)


class CJConnector(SupplierConnector):
    name = SUPPLIER

    def __init__(self, api_key: str, *, base_url: str = BASE_URL, min_interval: float = MIN_INTERVAL,
                 call_deadline: float = CALL_DEADLINE, max_retries: int = MAX_RETRIES,
                 clock=time.monotonic, wall=time.time, sleep=time.sleep):
        if not isinstance(api_key, str) or not _KEY_RE.match(api_key):
            raise CJAuthError("La API key de CJ no es válida (debe ser texto sin espacios, hasta 200 caracteres)")
        self._api_key = api_key
        self.base_url = self._check_base(base_url)
        self.min_interval, self.call_deadline, self.max_retries = min_interval, call_deadline, max_retries
        self._clock, self._wall, self._sleep = clock, wall, sleep
        self._key = _cache_key(self.base_url, api_key)

    def __repr__(self):                       # nunca imprime la clave
        return f"<CJConnector {self.base_url}>"

    @staticmethod
    def _check_base(base_url: str) -> str:
        """Host fijo. Solo una prueba con ALLOW_LOOPBACK puede apuntar a http://127.0.0.1:<puerto>."""
        parts = urlsplit(base_url)
        official = urlsplit(BASE_URL)
        if (parts.scheme, parts.hostname, parts.port) == (official.scheme, official.hostname, None):
            return BASE_URL
        if shttp.ALLOW_LOOPBACK and parts.scheme == "http" and parts.hostname == "127.0.0.1" and not parts.username:
            return base_url.rstrip("/")
        raise CJError("Host de CJ no permitido")

    # ------------------------------------------------------------ transporte
    def _pace(self, deadline: float) -> None:
        if self.min_interval <= 0:
            return
        with _LOCK:
            now = self._clock()
            start = max(now, _NEXT_OK.get(self._key, 0.0))
            wait = start - now
            if now + wait > deadline:             # no cabe en el plazo: NO se reserva el hueco
                raise CJRateLimited("Se alcanzó el plazo mientras se respetaba el límite de peticiones de CJ",
                                    retry_after=wait)
            _NEXT_OK[self._key] = start + self.min_interval
            while len(_NEXT_OK) > _MAX_ENTRIES:
                _NEXT_OK.pop(next(iter(_NEXT_OK)))
        if wait > 0:
            self._sleep(wait)

    def _wait_after_limit(self, wait: float) -> None:
        with _LOCK:       # que otras peticiones con la misma clave también esperen
            _NEXT_OK[self._key] = max(_NEXT_OK.get(self._key, 0.0), self._clock() + wait)
        self._sleep(wait)

    @staticmethod
    def _retry_after(resp) -> float | None:
        raw = (resp.headers or {}).get("retry-after")
        try:
            v = float(str(raw).strip())
        except (TypeError, ValueError):
            return None
        return v if math.isfinite(v) and v >= 0 else None

    def _backoff_wait(self, attempt: int, hinted: float | None) -> float:
        w = hinted if hinted is not None else BACKOFF_BASE * (2 ** (attempt - 1))
        return min(max(w, 0.0), BACKOFF_CAP)

    def _raw(self, method: str, path: str, *, params=None, payload=None, token: str | None, remaining: float):
        url = self.base_url + path + (("?" + urlencode(params)) if params else "")
        headers = {"CJ-Access-Token": token} if token else {}
        try:
            return shttp.request_json(method, url, payload, headers=headers, timeout=min(OP_TIMEOUT, remaining),
                                      total_timeout=remaining, max_bytes=MAX_RESPONSE_BYTES)
        except StoreError as e:
            raise _rewrap(e) from None

    @staticmethod
    def _envelope(resp):
        """(código, mensaje, datos) del sobre de CJ; CJError(retryable) si el cuerpo no es un sobre JSON."""
        try:
            body = json.loads(resp.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise CJError("CJ respondió algo que no es JSON válido", retryable=True) from None
        if not isinstance(body, dict):
            raise CJError("CJ respondió con un formato inesperado", retryable=True)
        code = body.get("code")
        if isinstance(code, str) and code.strip().isdigit():
            code = int(code.strip())
        if isinstance(code, bool) or not isinstance(code, int):
            raise CJError("CJ respondió con un formato inesperado (sin código)", retryable=True)
        return code, clean_text(body.get("message"), 200), body.get("data")

    def _get_token(self, deadline: float) -> str:
        with _key_lock(self._key):                    # una sola petición de token por clave a la vez
            with _LOCK:
                now = self._wall()
                t = _TOKENS.get(self._key)
                if t and t.access_exp - TOKEN_SAFETY_MARGIN > now:
                    return t.access
                refresh = t.refresh if t and t.refresh and t.refresh_exp - TOKEN_SAFETY_MARGIN > now else ""
            if refresh:
                try:
                    return self._auth_call("refresh", {"refreshToken": refresh}, deadline).access
                except CJAuthError:
                    with _LOCK:
                        _TOKENS.pop(self._key, None)         # refresco rechazado: se cae a la API key
            return self._auth_call("token", {"apiKey": self._api_key}, deadline).access

    def _auth_call(self, which: str, payload: dict, deadline: float) -> _Token:
        """Obtiene (o refresca) el token. Una sola petición, sin reintentos: CJ limita esta ruta y repetirla
        solo empeoraría el límite. Guarda el resultado en la caché compartida."""
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise CJError("CJ no respondió a tiempo", retryable=True)
        resp = self._raw("POST", PATHS[which], payload=payload, token=None, remaining=remaining)
        if resp.status == 429:
            raise CJRateLimited("CJ limita cuántas veces se puede pedir un token de acceso (aprox. 1 cada 5 minutos). "
                                "Espera unos minutos y reintenta", retry_after=300.0)
        if resp.status in (401, 403):
            raise CJAuthError("CJ rechazó la API key o el token (comprueba que esté bien copiada y vigente)")
        if resp.status >= 500:
            raise CJError(f"CJ tuvo un error interno (HTTP {resp.status})", retryable=True)
        if resp.status != 200:
            raise CJError(f"CJ respondió HTTP {resp.status} al pedir el token")
        code, message, data = self._envelope(resp)
        if code in RATE_CODES:
            raise CJRateLimited("CJ limita cuántas veces se puede pedir un token de acceso. Espera unos minutos y "
                                "reintenta", retry_after=300.0)
        if code != 200:
            raise CJAuthError(f"CJ rechazó las credenciales: {scrub(message or 'sin detalle', [self._api_key])} "
                              f"(código {code})")
        if not isinstance(data, dict):
            raise CJError("CJ no devolvió el token de acceso (formato inesperado)", retryable=True)
        access = _token_text(data.get("accessToken"))
        if not access:
            raise CJError("CJ devolvió un token de acceso inválido", retryable=True)
        now = self._wall()
        with _LOCK:
            old = _TOKENS.get(self._key)
            refresh = _token_text(data.get("refreshToken")) or (old.refresh if old else "")
            t = _Token(access, _parse_expiry(data.get("accessTokenExpiryDate"), now), refresh,
                       _parse_expiry(data.get("refreshTokenExpiryDate"), now) if data.get("refreshToken") else
                       (old.refresh_exp if old else 0.0))
            _TOKENS[self._key] = t
            while len(_TOKENS) > _MAX_ENTRIES:
                _TOKENS.pop(next(iter(_TOKENS)))
        return t

    def _invalidate_access(self, bad: str) -> None:
        with _LOCK:
            t = _TOKENS.get(self._key)
            if t and t.access == bad:           # otro hilo pudo haberlo renovado ya
                t.access_exp = 0.0

    def _call(self, method: str, path: str, *, params=None, payload=None, safe: bool = True):
        """Llamada autenticada. Devuelve `data`. Token: se cachea; ante 401 se refresca UNA vez y se repite.
        429/5xx/red/JSON inválido: solo si `safe` (lecturas), con espera exponencial acotada y dentro del plazo total."""
        deadline = self._clock() + self.call_deadline
        refreshed = False
        attempt = 0
        while True:
            if deadline - self._clock() <= 0:
                raise CJError("CJ no respondió dentro del plazo", retryable=True)
            token = self._get_token(deadline)
            self._pace(deadline)
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise CJError("CJ no respondió dentro del plazo", retryable=True)
            try:
                resp = self._raw(method, path, params=params, payload=payload, token=token, remaining=remaining)
                kind, code, message, data = self._classify(resp)
            except CJError as e:
                if safe and e.retryable and attempt < self.max_retries:
                    attempt += 1
                    self._retry_wait(attempt, None, deadline, e)
                    continue
                raise
            if kind == "ok":
                return data
            if kind == "token":
                if refreshed:
                    raise CJAuthError("CJ rechazó el token de acceso incluso después de renovarlo. Vuelve a conectar "
                                      "CJ con tu API key") from None
                refreshed = True
                self._invalidate_access(token)
                continue
            if kind == "rate":
                if not safe or attempt >= self.max_retries:
                    raise CJRateLimited("CJ pidió esperar (límite de peticiones). Reintenta en unos segundos",
                                        retry_after=self._backoff_wait(attempt + 1, self._retry_after(resp))) from None
                attempt += 1
                self._retry_wait(attempt, self._retry_after(resp), deadline,
                                 CJRateLimited("CJ pidió esperar (límite de peticiones)"))
                continue
            if kind == "server":
                err = CJError(f"CJ tuvo un error interno ({scrub(message or code, [self._api_key])})", retryable=True)
                if safe and attempt < self.max_retries:
                    attempt += 1
                    self._retry_wait(attempt, None, deadline, err)
                    continue
                raise err
            if kind == "notfound":
                raise StoreNotFound("CJ no encontró el recurso pedido")
            raise CJError(f"CJ rechazó la petición: {scrub(message or 'sin detalle', [self._api_key])} (código {code})")

    def _retry_wait(self, attempt: int, hinted: float | None, deadline: float, err: StoreError) -> None:
        wait = self._backoff_wait(attempt, hinted)
        if self._clock() + wait >= deadline:        # esperar dejaría sin plazo a la repetición: se abandona ya
            if isinstance(err, StoreRateLimited):
                raise CJRateLimited("CJ pidió esperar más de lo que permite el plazo de la operación",
                                    retry_after=wait) from None
            raise err
        self._wait_after_limit(wait)

    def _classify(self, resp):
        """(tipo, código, mensaje, datos): ok | token | rate | server | notfound | error."""
        st = resp.status
        if st == 401:
            return "token", st, "", None
        if st == 429:
            return "rate", st, "", None
        if st >= 500:
            return "server", st, "", None
        if st == 404:
            return "notfound", st, "", None
        if st != 200:
            return "error", st, "", None
        code, message, data = self._envelope(resp)
        if code == 200:
            return "ok", code, message, data
        if code in TOKEN_CODES:
            return "token", code, message, None
        if code in RATE_CODES:
            return "rate", code, message, None
        if code in NOT_FOUND_CODES:
            return "notfound", code, message, None
        if 500 <= code < 600:                      # los códigos propios de CJ (1600xxx) NO son errores del servidor
            return "server", code, message, None
        return "error", code, message, None

    # ------------------------------------------------------------ operaciones
    def validate(self) -> dict:
        """Comprueba la API key con una lectura mínima real. NO borra el token en caché (CJ limita getAccessToken: si la
        petición nueva diera 429 la caché quedaría vacía y se bloquearía todo ~5 min). Con una clave nueva no hay token
        en caché y se pide uno; con la misma clave se usa el que hay (si CJ ya lo rechaza, el 401 lo renueva solo)."""
        page = self.search("", page=1, page_size=1)
        return {"ok": True, "total": page.total}

    def search(self, query: str = "", *, category_id: str = "", page: int = 1, page_size: int = PAGE_SIZE) -> SearchPage:
        page = max(1, min(int(page), 10_000))
        size = max(1, min(int(page_size), MAX_PAGE_SIZE))
        params = {"pageNum": page, "pageSize": size}
        q = clean_text(query, 100)
        if q:
            params["productNameEn"] = q
        cat = parse_pid(category_id)
        if cat:
            params["categoryId"] = cat
        data = self._call("GET", PATHS["list"], params=params)
        if not isinstance(data, dict):
            raise CJError("CJ devolvió una lista de productos con formato inesperado", retryable=True)
        raw = data.get("list")
        raw = raw if isinstance(raw, list) else []
        raw = raw[:MAX_PAGE_SIZE * 2]
        items, seen = [], set()
        for d in raw:
            p = normalize_item(d)
            if p and p.supplier_sku not in seen:
                seen.add(p.supplier_sku)
                items.append(p)
        total = parse_int(data.get("total"), 0)
        eff = min(parse_int(data.get("pageSize"), size) or size, size)       # tamaño que CJ aplicó de verdad
        # Sigue si la página vino llena, si CJ no informó total, o si el total dice que faltan. Una página corta con total
        # alcanzado es la última. (Si CJ recorta el tamaño sin decirlo, el llamador lo detecta: ver cj_mod._gather.)
        has_more = bool(raw) and (len(raw) >= eff or total == 0 or page * eff < total)
        return SearchPage(items=items, total=total, page=page, raw_count=len(raw), skipped=len(raw) - len(items),
                          has_more=has_more, total_known=total > 0)

    def iter_catalog(self, query: str = "", *, category_id: str = "", limit: int = 200):
        """Productos (sin stock) de una búsqueda, hasta `limit`. CJ no ofrece «todo el catálogo»: hace falta consulta."""
        got, page = 0, 1
        while got < limit and page <= 100:
            res = self.search(query, category_id=category_id, page=page)
            for p in res.items:
                if got >= limit:
                    return
                got += 1
                yield p
            if not res.has_more:
                return
            page += 1

    def get_categories(self) -> list[dict]:
        """Categorías aplanadas: [{id, name, path}] (máx. 2000)."""
        data = self._call("GET", PATHS["categories"])
        out: list[dict] = []
        if not isinstance(data, list):
            return out
        for first in data[:200]:
            if not isinstance(first, dict):
                continue
            n1 = clean_text(first.get("categoryFirstName"), 60)
            for second in (first.get("categoryFirstList") if isinstance(first.get("categoryFirstList"), list) else [])[:200]:
                if not isinstance(second, dict):
                    continue
                n2 = clean_text(second.get("categorySecondName"), 60)
                for third in (second.get("categorySecondList") if isinstance(second.get("categorySecondList"), list)
                              else [])[:300]:
                    if not isinstance(third, dict):
                        continue
                    cid, n3 = parse_pid(third.get("categoryId")), clean_text(third.get("categoryName"), 60)
                    if cid and n3 and len(out) < 2000:
                        out.append({"id": cid, "name": n3, "path": " › ".join(x for x in (n1, n2, n3) if x)})
        return out

    def get_product_detail(self, supplier_sku: str, *, ship_to: str = "") -> CJDetail | None:
        pid = parse_pid(supplier_sku)
        if not pid:
            return None
        try:
            data = self._call("GET", PATHS["detail"], params={"pid": pid})
        except StoreNotFound:
            return None
        p = normalize_item(data)
        if p is None or p.supplier_sku != pid:           # CJ contestó por otro producto o sin datos válidos
            return None
        variants = []
        raw = data.get("variants") if isinstance(data.get("variants"), list) else []
        for v in raw[:200]:
            if not isinstance(v, dict):
                continue
            vid = parse_pid(v.get("vid"))
            price = parse_money(v.get("variantSellPrice"))
            if vid:
                variants.append({"vid": vid, "sku": clean_text(v.get("variantSku"), 60),
                                 "name": clean_text(_json_first_text(v.get("variantNameEn")), 120),
                                 "price": str(price) if price is not None else "",
                                 "image": safe_url(v.get("variantImage"))})
        dest = parse_country(ship_to)
        if dest and variants:
            try:
                cost, days = self.get_shipping(variants[0]["vid"], dest)
            except (StoreAuthError, StoreRateLimited):
                raise
            except StoreError:
                cost = days = None
            if cost is not None:
                p.shipping_cost = float(cost)
                if days:
                    p.shipping_days = days
        return CJDetail(p, variants)

    def get_product(self, supplier_sku: str, *, ship_to: str = "") -> NormalizedProduct | None:
        """Detalle normalizado con stock y almacén (2 llamadas, 3 con `ship_to`)."""
        d = self.get_product_detail(supplier_sku, ship_to=ship_to)
        if d is None:
            return None
        st = self.get_stock(d.product.supplier_sku)
        if st is not None:
            d.product.stock, d.product.warehouse = st.total, st.warehouse or UNKNOWN_WAREHOUSE
        return d.product

    def get_stock(self, supplier_sku: str) -> StockInfo | None:
        """Existencias totales y país con más stock. None = CJ no dio ningún dato de stock (desconocido, no cero)."""
        pid = parse_pid(supplier_sku)
        if not pid:
            return None
        try:
            data = self._call("GET", PATHS["stock"], params={"pid": pid})
        except StoreNotFound:
            return None
        variants = data.get("variantInventories") if isinstance(data, dict) else data
        if not isinstance(variants, list):
            return None
        by_country: dict[str, int] = {}
        seen = False
        for v in variants[:500]:
            if not isinstance(v, dict):
                continue
            entries = v.get("inventory") if isinstance(v.get("inventory"), list) else [v]
            for e in entries[:50]:
                if not isinstance(e, dict):
                    continue
                n = parse_stock(e.get("totalInventory"))
                if n is None:
                    n = parse_stock(e.get("storageNum"))
                if n is None:
                    parts = [x for x in (parse_stock(e.get("cjInventory")), parse_stock(e.get("factoryInventory")))
                             if x is not None]
                    n = sum(parts) if parts else None
                if n is None:
                    continue
                seen = True
                c = parse_country(e.get("countryCode")) or UNKNOWN_WAREHOUSE
                by_country[c] = min(MAX_STOCK, by_country.get(c, 0) + n)
        if not seen:
            return None
        known = {c: n for c, n in by_country.items() if c != UNKNOWN_WAREHOUSE}
        best = max(known, key=known.get) if known and max(known.values()) > 0 else (next(iter(known), ""))
        return StockInfo(total=min(MAX_STOCK, sum(by_country.values())), warehouse=best)

    def get_shipping(self, vid: str, country: str, *, from_country: str = "CN"):
        """(costo Decimal, días int|None) del envío más barato de una variante, o (None, None)."""
        vid, dest = parse_pid(vid), parse_country(country)
        if not vid or not dest:
            return None, None
        data = self._call("POST", PATHS["freight"], safe=True, payload={
            "startCountryCode": parse_country(from_country) or "CN", "endCountryCode": dest,
            "products": [{"quantity": 1, "vid": vid}]})
        best = None
        for o in (data if isinstance(data, list) else [])[:50]:
            if not isinstance(o, dict):
                continue
            price = parse_money(o.get("logisticPrice"))
            if price is None:
                continue
            nums = [int(n) for n in re.findall(r"\d+", clean_text(o.get("logisticAging"), 20))[:2]]
            days = min(max(nums), 365) if nums else None
            if best is None or price < best[0]:
                best = (price, days)
        return best or (None, None)

    def get_price_stock(self, supplier_sku: str, current: PriceStock | None = None) -> PriceStock | None:
        """Precio y stock actuales para el monitor (2 llamadas). None si CJ no conoce el producto o no dio datos
        utilizables (el monitor lo cuenta como «omitido» y no genera alertas). El envío se conserva del último
        conocido: CJ lo calcula por destino y no se consulta en cada revisión."""
        d = self.get_product_detail(supplier_sku)
        if d is None:
            return None
        st = self.get_stock(supplier_sku)
        if st is None:
            return None
        return PriceStock(cost=d.product.cost, shipping_cost=current.shipping_cost if current else 0.0,
                          stock=st.total)
