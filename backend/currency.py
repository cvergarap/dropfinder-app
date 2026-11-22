"""Moneda local (USD / COP) para DropFinder.

El catálogo se guarda SIEMPRE en USD; la conversión ocurre sólo al mostrar. La tasa COP/USD sale de la
TRM oficial (datos.gov.co, dataset 32sa-8pi3), que se guarda como historial en `exchange_rates`, o de
una tasa manual. Sobre la tasa base se aplica un margen de cobertura cambiaria (`hedge_pct`), porque los
proveedores cobran en USD con la tasa de la tarjeta, que suele ser peor que la TRM.

Diseño por tabla (`CURRENCIES`): sumar MXN, CLP, PEN o ARS es agregar una entrada con símbolo, decimales,
locale, modos de redondeo y la clave de ajustes que guarda su redondeo, más su fuente de tasa.

Formato en el frontend (equivalente JS):

    const fmt = (amount, cur) => {        // cur = state.currencies.find(c => c.code === code)
      const n = new Intl.NumberFormat(cur.locale, {
        minimumFractionDigits: cur.decimals, maximumFractionDigits: cur.decimals,
      }).format(amount);
      return `${cur.symbol} ${n}`;        // COP → "$ 89.900"   ·   USD → "US$ 12.99"
    };

    // Precio de VENTA: usar `price_view` del backend (ya trae el redondeo comercial, p. ej. ...900).
    // Montos que NO son precio de venta (costo, envío, ganancia): usd * state.rates[code] con redondeo
    // simple a `decimals` (Math.round para COP, 2 decimales para USD); nunca redondeo comercial.

Ajustes por usuario e instalación (Sprint 3, cuentas):
- De cada usuario (cómo VE los precios): display_currency, cop_rounding, vat_enabled, vat_pct. Se guardan en
  `user_currency_settings`; mientras un usuario no los cambie, valen los de `currency_settings` (valores de
  la instalación, que hacen de valores por defecto para cuentas nuevas).
- De la instalación (cuánto CUESTA el dólar para el negocio; solo el propietario): rate_mode, manual_rate,
  hedge_pct, en `currency_settings`. El historial de la TRM (`exchange_rates`) también es de la instalación.

IVA: si está activado se SUMA encima del precio de venta sólo para mostrar el "precio al consumidor con
IVA"; no altera los cálculos de ganancia en USD. El tratamiento tributario depende del régimen de cada
vendedor: debe validarlo con su contador.
"""
from __future__ import annotations

import json
import math
import sqlite3
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

import auth_context

# ── Configuración ───────────────────────────────────────────────────────────────────────────────────

DEFAULTS = {"display_currency": "USD", "rate_mode": "trm", "manual_rate": 4000.0, "hedge_pct": 0.0,
            "cop_rounding": "900", "vat_enabled": False, "vat_pct": 19.0}

# Redondeo comercial: (paso, terminación). "900" → sube a la siguiente cifra terminada en 900.
ROUNDING_STEPS = {"900": (1000, 900), "1000": (1000, 0), "100": (100, 0)}

CURRENCIES = {
    "USD": {"symbol": "US$", "decimals": 2, "locale": "en-US",
            "rounding_modes": ["none"], "rounding_setting": None},
    "COP": {"symbol": "$", "decimals": 0, "locale": "es-CO",
            "rounding_modes": ["900", "1000", "100", "none"], "rounding_setting": "cop_rounding"},
}
SUPPORTED = list(CURRENCIES)  # ["USD", "COP"]

USER_KEYS = ("display_currency", "cop_rounding", "vat_enabled", "vat_pct")   # preferencias de cada usuario
INSTALLATION_KEYS = tuple(k for k in DEFAULTS if k not in USER_KEYS)          # solo el propietario

RATE_MODES = ("trm", "manual")
MAX_HEDGE_PCT = 50.0
STALE_DAYS = 3

TRM_DATASET_URL = "https://www.datos.gov.co/resource/32sa-8pi3.json"
USER_AGENT = "DropFinder/1.0 (consulta TRM)"

# Colombia no tiene horario de verano: UTC-5 fijo. Las fechas de la TRM son fechas colombianas.
CO_TZ = timezone(timedelta(hours=-5), "America/Bogota")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _today_co() -> date:
    return _now().astimezone(CO_TZ).date()


# ── Esquema ─────────────────────────────────────────────────────────────────────────────────────────

def ensure_schema(conn) -> None:
    """Crea las tablas de moneda (idempotente) e inserta los ajustes por defecto que falten."""
    conn.execute("""CREATE TABLE IF NOT EXISTS exchange_rates (
        date TEXT NOT NULL, currency TEXT NOT NULL, rate REAL NOT NULL, source TEXT NOT NULL,
        fetched_at TEXT NOT NULL, PRIMARY KEY (date, currency, source))""")
    conn.execute("CREATE TABLE IF NOT EXISTS currency_settings (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS user_currency_settings (user_id INTEGER NOT NULL, key TEXT NOT NULL, "
                 "value TEXT, PRIMARY KEY (user_id, key))")
    conn.executemany("INSERT OR IGNORE INTO currency_settings(key, value) VALUES (?, ?)",
                     [(k, json.dumps(v)) for k, v in DEFAULTS.items()])


# ── Ajustes ─────────────────────────────────────────────────────────────────────────────────────────

def _uid(user_id: int | None) -> int | None:
    """Usuario dueño de las preferencias: el indicado o el de la petición en curso. None fuera de una
    petición (p. ej. el hilo de la TRM): entonces solo cuentan los valores de la instalación."""
    if user_id is not None:
        return int(user_id)
    try:
        return auth_context.current_user_id()
    except auth_context.NoUserContext:
        return None


def get_settings(conn, user_id: int | None = None) -> dict:
    """Ajustes del usuario (por defecto el de la petición en curso): los de la instalación más sus
    preferencias. Cualquier valor ausente o corrupto vuelve al de la instalación o al valor por defecto."""
    out = dict(DEFAULTS)
    rows = conn.execute("SELECT key, value FROM currency_settings").fetchall()
    uid = _uid(user_id)
    if uid is not None:
        try:
            rows += conn.execute("SELECT key, value FROM user_currency_settings WHERE user_id = ?",
                                 (uid,)).fetchall()
        except sqlite3.OperationalError:     # base sin la tabla aún (antes de ensure_schema)
            pass
    for key, raw in rows:                # las filas del usuario van después: pisan a las de la instalación
        if key not in DEFAULTS:
            continue
        try:
            out[key] = _validate(key, json.loads(raw))
        except (ValueError, TypeError):
            pass
    return out


def _number(key: str, value, label: str) -> float:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{label} debe ser un número")
    if isinstance(value, str):
        value = value.strip().replace(",", ".")
    try:
        n = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} debe ser un número") from None
    if not math.isfinite(n):
        raise ValueError(f"{label} debe ser un número")
    return n


def _validate(key: str, value):
    """Valida y normaliza un ajuste. Lanza ValueError con un mensaje en español."""
    if key == "display_currency":
        code = str(value).strip().upper() if isinstance(value, str) else None
        if code not in CURRENCIES:
            raise ValueError(f"Moneda no soportada. Opciones: {', '.join(SUPPORTED)}")
        return code
    if key == "rate_mode":
        if value not in RATE_MODES:
            raise ValueError("El modo de tasa debe ser 'trm' (oficial) o 'manual'")
        return value
    if key == "manual_rate":
        n = _number(key, value, "La tasa manual")
        if n <= 0:
            raise ValueError("La tasa manual debe ser mayor que 0")
        return n
    if key == "hedge_pct":
        n = _number(key, value, "El margen de cobertura")
        if not 0 <= n <= MAX_HEDGE_PCT:
            raise ValueError(f"El margen de cobertura debe estar entre 0 y {MAX_HEDGE_PCT:g} %")
        return n
    if key == "cop_rounding":
        mode = str(value) if isinstance(value, (str, int)) and not isinstance(value, bool) else None
        if mode not in CURRENCIES["COP"]["rounding_modes"]:
            raise ValueError("Redondeo en COP inválido. Opciones: 900, 1000, 100 o none")
        return mode
    if key == "vat_enabled":
        if not isinstance(value, bool):
            raise ValueError("vat_enabled debe ser verdadero o falso")
        return value
    if key == "vat_pct":
        n = _number(key, value, "El porcentaje de IVA")
        if not 0 <= n <= 100:
            raise ValueError("El porcentaje de IVA debe estar entre 0 y 100")
        return n
    raise ValueError(f"Opción desconocida: {key}")


def update_settings(conn, body: dict, user_id: int | None = None) -> dict:
    """Actualiza (parcialmente) los ajustes. Valida todo antes de escribir: o se guarda todo o nada.
    Las preferencias (USER_KEYS) se guardan para el usuario; el resto, para la instalación (quien llama
    comprueba que solo el propietario cambie estas últimas)."""
    if not isinstance(body, dict):
        raise ValueError("Se esperaba un objeto JSON con los ajustes")
    clean = {k: _validate(k, v) for k, v in body.items()}
    uid = _uid(user_id)
    if uid is None and any(k in USER_KEYS for k in clean):
        raise auth_context.NoUserContext("Las preferencias de moneda son de un usuario: falta el contexto")
    conn.executemany("INSERT INTO currency_settings(key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                     [(k, json.dumps(v)) for k, v in clean.items() if k not in USER_KEYS])
    conn.executemany("INSERT INTO user_currency_settings(user_id, key, value) VALUES (?, ?, ?) "
                     "ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value",
                     [(uid, k, json.dumps(v)) for k, v in clean.items() if k in USER_KEYS])
    return get_settings(conn, uid)


# ── TRM oficial ─────────────────────────────────────────────────────────────────────────────────────

def _trm_url() -> str:
    query = urllib.parse.urlencode({"$order": "vigenciadesde DESC", "$limit": 1},
                                   safe="$", quote_via=urllib.parse.quote)
    return f"{TRM_DATASET_URL}?{query}"


def _iso_date(value) -> str | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        return None


def fetch_trm(timeout: float = 6.0, opener=None) -> dict | None:
    """Consulta la TRM vigente más reciente en datos.gov.co.

    `opener(request, timeout=...)` permite inyectar una respuesta falsa en pruebas (por defecto
    `urllib.request.urlopen`, con verificación TLS normal). Devuelve
    {"rate", "date", "valid_until", "source": "trm"} o None ante cualquier error de red o de formato.
    """
    opener = opener or urllib.request.urlopen
    req = urllib.request.Request(_trm_url(), headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with opener(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):  # URLError/HTTPError/timeout son OSError
        return None
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        return None
    row = data[0]
    if str(row.get("unidad", "COP")).upper() != "COP":
        return None
    try:
        rate = float(str(row.get("valor", "")).replace(",", ""))
    except ValueError:
        return None
    day = _iso_date(row.get("vigenciadesde"))
    if not math.isfinite(rate) or rate <= 0 or day is None:
        return None
    return {"rate": rate, "date": day, "valid_until": _iso_date(row.get("vigenciahasta")) or day,
            "source": "trm"}


def _store_rate(conn, day: str, currency: str, rate: float, source: str) -> None:
    conn.execute("INSERT INTO exchange_rates(date, currency, rate, source, fetched_at) VALUES (?, ?, ?, ?, ?) "
                 "ON CONFLICT(date, currency, source) DO UPDATE SET rate = excluded.rate, "
                 "fetched_at = excluded.fetched_at",
                 (day, currency, rate, source, _now().isoformat(timespec="seconds")))


def _latest_rate(conn, currency: str = "COP", source: str = "trm"):
    return conn.execute("SELECT date, rate, fetched_at FROM exchange_rates WHERE currency = ? AND source = ? "
                        "ORDER BY date DESC, fetched_at DESC LIMIT 1", (currency, source)).fetchone()


def rate_history(conn, currency: str = "COP", limit: int = 30) -> list[dict]:
    """Historial de tasas guardadas (más reciente primero); sirve para explicar precios pasados."""
    rows = conn.execute("SELECT date, currency, rate, source, fetched_at FROM exchange_rates "
                        "WHERE currency = ? ORDER BY date DESC, fetched_at DESC LIMIT ?",
                        (currency, limit)).fetchall()
    return [dict(zip(("date", "currency", "rate", "source", "fetched_at"), r)) for r in rows]


def refresh_trm(conn, opener=None) -> dict:
    """Descarga la TRM y la guarda en el historial. Si falla, conserva la última guardada y el estado
    devuelto trae `rate_info.error`."""
    trm = fetch_trm(opener=opener)
    if trm is None:
        return get_state(conn, error="No se pudo consultar la TRM oficial en datos.gov.co; "
                                     "se usa la última tasa disponible.")
    _store_rate(conn, trm["date"], "COP", trm["rate"], "trm")
    return get_state(conn)


def maybe_auto_refresh(conn, opener=None, max_age_hours: float = 12) -> None:
    """Al iniciar el servidor: si el modo es TRM y la última consulta tiene más de `max_age_hours`
    (o no hay ninguna), intenta actualizarla. Nunca lanza excepciones."""
    try:
        if get_settings(conn)["rate_mode"] != "trm":
            return
        row = conn.execute("SELECT MAX(fetched_at) FROM exchange_rates "
                           "WHERE currency = 'COP' AND source = 'trm'").fetchone()
        if row and row[0]:
            last = datetime.fromisoformat(row[0])
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if _now() - last < timedelta(hours=max_age_hours):
                return
        refresh_trm(conn, opener=opener)
    except Exception:  # noqa: BLE001 — arranque silencioso: sin TRM se usa la tasa manual
        pass


# ── Estado y conversión ─────────────────────────────────────────────────────────────────────────────

def get_state(conn, error: str | None = None, user_id: int | None = None) -> dict:
    """Estado completo para el frontend: ajustes, tasas efectivas y metadatos de la tasa.

    `error` (opcional) permite a `refresh_trm` reportar un fallo de consulta. `user_id`: de quién son las
    preferencias (por defecto, el usuario de la petición en curso).
    """
    s = get_settings(conn, user_id)
    hedge = s["hedge_pct"]
    errors = [error] if error else []
    base, source, day, stale = None, "none", None, False

    if s["rate_mode"] == "trm":
        row = _latest_rate(conn)
        if row:
            day, base, source = row[0], float(row[1]), "trm"
            try:
                stale = (_today_co() - date.fromisoformat(day)).days > STALE_DAYS
            except ValueError:
                stale = True
        else:
            errors.append("Aún no hay TRM guardada; se usa la tasa manual.")
    if base is None and s["manual_rate"] > 0:
        base, source = float(s["manual_rate"]), "manual"

    effective = round(base * (1 + hedge / 100), 4) if base is not None else None
    return {
        "settings": s,
        "rates": {"USD": 1.0, "COP": effective},
        "rate_info": {"base_rate": base, "effective_rate": effective, "source": source, "date": day,
                      "stale": stale, "hedge_pct": hedge, "error": " ".join(errors) or None},
        "user_keys": list(USER_KEYS), "installation_keys": list(INSTALLATION_KEYS),
        "currencies": [{"code": c, "symbol": m["symbol"], "decimals": m["decimals"], "locale": m["locale"]}
                       for c, m in CURRENCIES.items()],
    }


def _currency(state: dict, currency: str | None) -> str:
    code = (currency or state["settings"]["display_currency"]).upper()
    if code not in CURRENCIES:
        raise ValueError(f"Moneda no soportada: {code}")
    if state["rates"].get(code) is None:
        raise ValueError(f"No hay tasa disponible para {code}")
    return code


def _plain(value: float, decimals: int) -> float:
    """Redondeo simple 'mitad hacia arriba' (evita el redondeo bancario de round())."""
    # repr() da el decimal más corto que representa el float (1.005 → "1.005", no 1.00499999…)
    q = Decimal(1).scaleb(-decimals)
    return float(Decimal(repr(float(value))).quantize(q, rounding=ROUND_HALF_UP))


def convert(usd: float, state: dict, currency: str | None = None) -> float:
    """Convierte un monto en USD (costo, envío, ganancia…) con redondeo simple a los decimales de la
    moneda. Para precios de VENTA usar `price_view` (redondeo comercial)."""
    code = _currency(state, currency)
    return _plain(float(usd) * state["rates"][code], CURRENCIES[code]["decimals"])


def round_price(value: float, currency: str, mode: str) -> float:
    """Redondeo comercial de un precio de VENTA ya convertido.

    COP: "900" → sube a la siguiente cifra terminada en 900 (87.350 → 87.900; 87.900 → 87.900;
    87.901 → 88.900); "1000" → sube al siguiente múltiplo de 1.000; "100" → al siguiente de 100;
    "none" → peso entero. USD: 2 decimales (el .99 se maneja en metrics). Un modo que la moneda no
    admite se trata como "none".
    """
    meta = CURRENCIES.get(currency.upper())
    if meta is None:
        raise ValueError(f"Moneda no soportada: {currency}")
    if mode not in meta["rounding_modes"] or mode not in ROUNDING_STEPS or value <= 0:
        return _plain(value, meta["decimals"])
    step, ending = ROUNDING_STEPS[mode]
    units = math.ceil(round(value, 2))  # tolera ruido de coma flotante (21.99 * 4000 = 87960.00000000001)
    return float(math.ceil((units - ending) / step) * step + ending)


def price_view(usd_price: float, state: dict) -> dict:
    """Precio de venta en la moneda de visualización, con redondeo comercial y, si está activo, el
    precio al consumidor con IVA (IVA sumado encima, con redondeo simple para no alterar el impuesto)."""
    s = state["settings"]
    code = _currency(state, None)
    meta = CURRENCIES[code]
    mode = s.get(meta["rounding_setting"], "none") if meta["rounding_setting"] else "none"
    amount = round_price(float(usd_price) * state["rates"][code], code, mode)
    with_vat = _plain(amount * (1 + s["vat_pct"] / 100), meta["decimals"]) if s["vat_enabled"] else None
    return {"currency": code, "amount": amount, "amount_with_vat": with_vat,
            "vat_pct": s["vat_pct"] if s["vat_enabled"] else None, "usd": float(usd_price)}
