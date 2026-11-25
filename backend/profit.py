"""Calculadora de rentabilidad CONTRAENTREGA (COD) para Colombia y LatAm. Fórmulas: docs/profit-calculator.md.

Módulo de funcionalidad: expone ROUTES (método, regex, handler(match, qs, body)) que server.py agrega.

POST /api/profit/calc          → calcula con las entradas del cuerpo (lo que falte sale de TUS supuestos)
GET  /api/profit/defaults      → tus supuestos para una moneda (?currency=COP|USD) y los de fábrica
PUT  /api/profit/defaults      → cambia tus supuestos ({"currency", "defaults": {clave: valor|null}}; null = fábrica)
GET  /api/products/{id}/profit → rentabilidad del producto con su precio sugerido, su costo y tus supuestos

Dos capas: `calculate(inputs, defaults)` es una función PURA (sin base de datos, sin red, sin reloj) y el resto
(supuestos por usuario, rutas) la envuelve. Todos los montos de una cuenta están en UNA moneda (la de la entrada).

Modelo (resumen). Un "pedido confirmado" sale a despacho; con probabilidad d = efectividad se entrega y cobra, y con
r = 1 − d vuelve:
  entregado: ingreso precio; costos producto + envío proveedor + empaque + flete de entrega + comisión de recaudo
             (% del precio + fijo)
  devuelto:  flete de entrega (ida) + flete de devolución + empaque + pérdida opcional del producto; sin ingreso
  utilidad por confirmado = d·utilidad_entregado − r·pérdida_devuelto − CPA

Supuestos de datos por usuario: tabla profit_defaults(user_id, currency, data JSON) (migración v8).
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

import auth_context
import currency as currency_mod
from errors import ApiError

FORMULA_VERSION = 1
CURRENCIES = ("USD", "COP")
DECIMALS = {"USD": 2, "COP": 0}
SYMBOLS = {"USD": "US$", "COP": "$"}
# Tope razonable por monto (≈ 100.000 USD): rechaza typos y entradas hostiles sin estorbar ningún producto real
MAX_MONEY = {"USD": Decimal(100_000), "COP": Decimal(400_000_000)}
INPUT_STEP = Decimal("0.0001")
MAX_ROAS = Decimal(100)
MIN_ROAS = Decimal("0.01")

# Entradas: clave → (etiqueta en español, tipo). 'money' | 'pct'
MONEY_FIELDS = {
    "price": "El precio de venta", "product_cost": "El costo del producto",
    "supplier_shipping": "El envío del proveedor", "delivery_freight": "El flete de entrega",
    "return_freight": "El flete de devolución", "packaging": "El costo de empaque",
    "cod_fee_fixed": "La comisión fija de recaudo", "cpa": "El CPA",
}
PCT_FIELDS = {
    "return_rate_pct": "El % de devoluciones", "delivery_rate_pct": "El % de efectividad de entrega",
    "cod_fee_pct": "La comisión de recaudo (%)", "return_product_loss_pct": "El % de producto devuelto que se pierde",
    "return_warn_pct": "El umbral de aviso de devoluciones",
}
INPUT_KEYS = ("currency", "target_roas", *MONEY_FIELDS, *PCT_FIELDS)
REQUIRED = ("price", "product_cost")
# Claves con supuesto editable (guardado por usuario y moneda). return_warn_pct solo es un umbral de aviso.
DEFAULT_KEYS = ("delivery_freight", "return_freight", "packaging", "cod_fee_pct", "cod_fee_fixed",
                "return_rate_pct", "return_product_loss_pct", "return_warn_pct")
DEFAULT_LABELS = {
    "delivery_freight": "flete de entrega", "return_freight": "flete de devolución", "packaging": "empaque",
    "cod_fee_pct": "comisión de recaudo (%)", "cod_fee_fixed": "comisión fija de recaudo",
    "return_rate_pct": "% de devoluciones", "return_product_loss_pct": "% de producto devuelto que se pierde",
    "return_warn_pct": "umbral de aviso de devoluciones",
}
# Supuestos de fábrica: ESTIMACIONES típicas de contraentrega en Colombia (cada tienda debe poner los suyos).
BUILTIN_DEFAULTS = {
    "COP": {"delivery_freight": 15000, "return_freight": 15000, "packaging": 1500, "cod_fee_pct": 3.0,
            "cod_fee_fixed": 0, "return_rate_pct": 25.0, "return_product_loss_pct": 0.0, "return_warn_pct": 30.0},
    "USD": {"delivery_freight": 4.0, "return_freight": 4.0, "packaging": 0.4, "cod_fee_pct": 3.0,
            "cod_fee_fixed": 0, "return_rate_pct": 25.0, "return_product_loss_pct": 0.0, "return_warn_pct": 30.0},
}
LOW_MARGIN_PCT = Decimal(10)


class ProfitError(ValueError):
    """Entrada inválida. `field` = clave de la entrada con el problema (o None); el mensaje va en español."""

    def __init__(self, message: str, field: str | None = None):
        super().__init__(message)
        self.field = field


# ---------------------------------------------------------------- validación

def _dec(value, key: str, label: str) -> Decimal:
    """Número finito exacto a partir de un int/float de JSON. Rechaza texto, bool, NaN e infinitos."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProfitError(f"{label} debe ser un número", key)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ProfitError(f"{label} debe ser un número finito", key)
        n = Decimal(repr(value))
    else:
        n = Decimal(value)         # int de cualquier tamaño: el tope lo rechaza (sin OverflowError)
    # Máximo 4 decimales (0,0001): evita valores como 1e-300 que desbordarían los cocientes (margen, ROAS)
    return n if abs(n) > MAX_MONEY["COP"] * 10 else n.quantize(INPUT_STEP, rounding=ROUND_HALF_UP)


def _currency(value) -> str:
    code = value.strip().upper() if isinstance(value, str) else None
    if code not in CURRENCIES:
        raise ProfitError(f"Moneda no soportada. Opciones: {', '.join(CURRENCIES)}", "currency")
    return code


def _field(key: str, value, code: str) -> Decimal:
    if key == "target_roas":
        n = _dec(value, key, "El ROAS objetivo")
        if not MIN_ROAS <= n <= MAX_ROAS:
            raise ProfitError(f"El ROAS objetivo debe estar entre {MIN_ROAS} y {MAX_ROAS}", key)
        return n
    if key in MONEY_FIELDS:
        label = MONEY_FIELDS[key]
        n = _dec(value, key, label)
        if n < 0:
            raise ProfitError(f"{label} no puede ser negativo", key)
        if n > MAX_MONEY[code]:
            raise ProfitError(f"{label} supera el máximo permitido ({MAX_MONEY[code]:,.0f} {code})".replace(",", "."), key)
        return n
    label = PCT_FIELDS[key]
    n = _dec(value, key, label)
    if not 0 <= n <= 100:
        raise ProfitError(f"{label} debe estar entre 0 y 100", key)
    return n


def validate_defaults(data, code: str, *, allow_null: bool = False) -> dict:
    """Valida un dict de supuestos (solo DEFAULT_KEYS). Con allow_null, `None` se conserva (= volver a fábrica)."""
    if not isinstance(data, dict):
        raise ProfitError("Los supuestos deben ser un objeto JSON", "defaults")
    out = {}
    for k, v in data.items():
        if k not in DEFAULT_KEYS:
            raise ProfitError(f"Supuesto desconocido: {k}", k)
        if v is None and allow_null:
            out[k] = None
        else:
            out[k] = _field(k, v, code)
    return out


def _clean(inputs) -> tuple[str, dict]:
    """(moneda, {clave: Decimal}) con solo los campos presentes (None = ausente). Lanza ProfitError."""
    if not isinstance(inputs, dict):
        raise ProfitError("Se esperaba un objeto JSON con las entradas")
    unknown = sorted(str(k) for k in inputs if k not in INPUT_KEYS)
    if unknown:
        raise ProfitError(f"Campo desconocido: {', '.join(unknown)}", unknown[0])
    code = _currency(inputs.get("currency", "COP"))
    vals = {k: _field(k, v, code) for k, v in inputs.items() if k != "currency" and v is not None}
    for k in REQUIRED:
        if k not in vals:
            raise ProfitError(f"{MONEY_FIELDS[k]} es obligatorio", k)
    if "cpa" in vals and "target_roas" in vals:
        raise ProfitError("Indica el CPA o el ROAS objetivo, no los dos", "target_roas")
    if "return_rate_pct" in vals and "delivery_rate_pct" in vals \
            and abs(vals["return_rate_pct"] + vals["delivery_rate_pct"] - 100) > Decimal("0.01"):
        raise ProfitError("Devoluciones y efectividad de entrega deben sumar 100 %: indica solo una de las dos",
                          "delivery_rate_pct")
    return code, vals


# ---------------------------------------------------------------- cálculo

def _q(x: Decimal, places: int) -> float:
    """Redondeo único de salida: mitad hacia arriba (no el bancario de round()). -0 se normaliza a 0."""
    r = x.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    return float(r) if r != 0 else 0.0


def money_text(amount: float, code: str) -> str:
    """'$ 12.500' (COP, punto de miles) o 'US$ 12.50'."""
    d = DECIMALS[code]
    s = f"{abs(amount):,.{d}f}"
    if code == "COP":
        s = s.replace(",", ".")
    return f"{'-' if amount < 0 else ''}{SYMBOLS[code]} {s}"


def builtin_defaults(code: str) -> dict:
    return dict(BUILTIN_DEFAULTS[_currency(code)])


def calculate(inputs: dict, defaults: dict | None = None) -> dict:
    """Rentabilidad contraentrega. Función pura; lanza ProfitError si las entradas no son válidas.

    inputs: price y product_cost (obligatorios) y, opcionales: currency ('COP' por defecto), supplier_shipping,
    delivery_freight, return_freight, packaging, cod_fee_pct, cod_fee_fixed, return_rate_pct | delivery_rate_pct,
    return_product_loss_pct, cpa | target_roas. Los opcionales ausentes (o None) salen de `defaults` (misma
    moneda que las entradas; por defecto los de fábrica) y se listan en `defaults_used`.
    """
    try:
        return _calculate(inputs, defaults)
    except ArithmeticError:        # InvalidOperation/Overflow de Decimal: combinación de valores extrema
        raise ProfitError("Los valores indicados dan un resultado fuera de rango") from None


def _calculate(inputs, defaults) -> dict:
    code, v = _clean(inputs)
    dflt = builtin_defaults(code)
    if defaults:
        dflt.update({k: x for k, x in validate_defaults(defaults, code, allow_null=True).items() if x is not None})
        dflt = {k: (x if isinstance(x, Decimal) else Decimal(repr(x))) for k, x in dflt.items()}
    else:
        dflt = {k: Decimal(repr(x)) for k, x in dflt.items()}

    used: list[str] = []
    def pick(key):
        if key in v:
            return v[key]
        used.append(key)
        return dflt[key]

    price, cost = v["price"], v["product_cost"]
    ship_sup = v.get("supplier_shipping", Decimal(0))
    freight, ret_freight, pack = pick("delivery_freight"), pick("return_freight"), pick("packaging")
    fee_pct, fee_fix, loss_pct = pick("cod_fee_pct"), pick("cod_fee_fixed"), pick("return_product_loss_pct")
    if "return_rate_pct" in v:
        ret_pct = v["return_rate_pct"]
    elif "delivery_rate_pct" in v:
        ret_pct = 100 - v["delivery_rate_pct"]
    else:
        used.append("return_rate_pct")
        ret_pct = dflt["return_rate_pct"]
    warn_pct = v.get("return_warn_pct", dflt["return_warn_pct"])
    d, r = (100 - ret_pct) / 100, ret_pct / 100               # entregados / devueltos por pedido confirmado

    if "cpa" in v:
        cpa, roas_in = v["cpa"], None
    elif "target_roas" in v:
        cpa, roas_in = price / v["target_roas"], v["target_roas"]
    else:
        cpa, roas_in = Decimal(0), None

    fee = price * fee_pct / 100 + fee_fix                      # recaudo: solo si se entrega y se cobra
    profit_delivered_gross = price - cost - ship_sup - pack - freight - fee   # una entrega, antes de devoluciones y CPA
    loss_returned = freight + ret_freight + pack + cost * loss_pct / 100      # una devolución
    before_ads = d * profit_delivered_gross - r * loss_returned               # por pedido confirmado, sin CPA
    profit_confirmed = before_ads - cpa
    revenue = d * price
    max_cpa = max(before_ads, Decimal(0))

    places = DECIMALS[code]
    q = lambda x: _q(x, places)
    result = {
        "formula_version": FORMULA_VERSION, "currency": code, "decimals": places,
        "inputs": {
            "price": q(price), "product_cost": q(cost), "supplier_shipping": q(ship_sup),
            "delivery_freight": q(freight), "return_freight": q(ret_freight), "packaging": q(pack),
            "cod_fee_pct": _q(fee_pct, 2), "cod_fee_fixed": q(fee_fix), "return_rate_pct": _q(ret_pct, 2),
            "delivery_rate_pct": _q(100 - ret_pct, 2), "return_product_loss_pct": _q(loss_pct, 2),
            "cpa": q(cpa), "target_roas": _q(roas_in, 2) if roas_in is not None else None,
        },
        "defaults_used": used,
        "profit_per_confirmed": q(profit_confirmed),
        "profit_per_delivered": q(profit_confirmed / d) if d > 0 else None,
        "margin_pct": _q(profit_confirmed / revenue * 100, 1) if revenue > 0 else None,
        "max_cpa": q(max_cpa),
        "breakeven_roas": _q(price / max_cpa, 2) if max_cpa > 0 and price > 0 else None,
        "profit_per_100": q(profit_confirmed * 100),
        "delivered_per_100": _q(d * 100, 2),
        "contribution_per_delivery": q(profit_delivered_gross),
        "loss_per_return": q(loss_returned),
        "roas_used": _q(price / cpa, 2) if cpa > 0 and price > 0 else None,
        # Dinero esperado por pedido confirmado (ingreso y cada costo); cada línea se redondea sola,
        # así que la suma de líneas puede diferir de la utilidad en una unidad de la moneda.
        "breakdown": {
            "revenue": q(revenue), "product_cost": q(d * cost), "supplier_shipping": q(d * ship_sup),
            "packaging": q(pack), "delivery_freight": q(freight), "return_freight": q(r * ret_freight),
            "returned_product_loss": q(r * cost * loss_pct / 100), "cod_fee": q(d * fee), "ads": q(cpa),
            "profit": q(profit_confirmed),
        },
    }
    result["warnings"] = _warnings(result, code, profit_confirmed, revenue, ret_pct, warn_pct, price, "cpa" in v or "target_roas" in v)
    return result


def _warnings(res, code, profit, revenue, ret_pct, warn_pct, price, has_ads) -> list[dict]:
    w = []
    def add(c, msg):
        w.append({"code": c, "message": msg})
    if price == 0:
        add("zero_price", "El precio de venta es 0: no hay ingreso, así que no se puede calcular el margen.")
    if ret_pct >= 100:
        add("all_returned", "Con 100 % de devoluciones ningún pedido se entrega: pierdes los fletes y el empaque de cada pedido.")
    if profit < 0:
        add("negative_margin", f"Pierdes {money_text(-res['profit_per_confirmed'], code)} por cada pedido confirmado "
                               f"({money_text(-res['profit_per_100'], code)} por cada 100). Sube el precio o baja costos, fletes o devoluciones.")
    elif revenue > 0 and profit / revenue * 100 < LOW_MARGIN_PCT:
        add("low_margin", "El margen es menor a 10 %: una subida de devoluciones o de CPA te dejaría en pérdida.")
    if ret_pct > warn_pct:
        add("high_returns", f"Devoluciones de {ret_pct.normalize():f} %, por encima de tu umbral de {warn_pct.normalize():f} %: "
                           "cada devolución paga flete de ida y de vuelta sin ingreso.")
    if not has_ads:
        add("no_ads", "No indicaste CPA ni ROAS objetivo: el resultado NO incluye publicidad.")
    if res["defaults_used"]:
        names = ", ".join(DEFAULT_LABELS[k] for k in res["defaults_used"])
        add("defaults_used", f"Se usaron tus supuestos por defecto para: {names}. Son estimaciones: reemplázalos por tus datos reales.")
    return w


# ---------------------------------------------------------------- supuestos por usuario (SQLite)

def ensure_schema(conn) -> None:
    """Tabla de supuestos por usuario y moneda (v8). Idempotente; db.init_db la llama dentro de su transacción."""
    conn.execute("CREATE TABLE IF NOT EXISTS profit_defaults ("
                 "user_id INTEGER NOT NULL, currency TEXT NOT NULL CHECK (currency IN ('USD', 'COP')), "
                 "data TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY (user_id, currency))")


def _uid() -> int:
    return auth_context.current_user_id()


def stored_defaults(conn, user_id: int, code: str) -> dict:
    """Solo lo que el usuario cambió (valores válidos). Datos corruptos se ignoran clave por clave."""
    row = conn.execute("SELECT data FROM profit_defaults WHERE user_id = ? AND currency = ?", (user_id, code)).fetchone()
    if not row:
        return {}
    try:
        raw = json.loads(row[0])
    except ValueError:
        return {}
    out = {}
    for k, v in (raw.items() if isinstance(raw, dict) else ()):
        try:
            out[k] = _field(k, v, code) if k in DEFAULT_KEYS else None
        except ProfitError:
            pass
    return {k: float(x) for k, x in out.items() if x is not None}


def effective_defaults(conn, user_id: int, code: str) -> dict:
    out = builtin_defaults(code)
    out.update(stored_defaults(conn, user_id, code))
    return out


def _defaults_view(conn, code: str) -> dict:
    custom = stored_defaults(conn, _uid(), code)
    return {"currency": code, "defaults": {**builtin_defaults(code), **custom}, "builtin": builtin_defaults(code),
            "custom_keys": sorted(custom), "labels": DEFAULT_LABELS}


# ---------------------------------------------------------------- API

def _api_error(e: ProfitError) -> ApiError:
    return ApiError(400, str(e), code=f"invalid_{e.field}" if e.field else "invalid_input")


def _user_currency(conn, requested) -> str:
    if requested is None or requested == "":
        return currency_mod.get_settings(conn)["display_currency"]
    try:
        return _currency(requested)
    except ProfitError as e:
        raise _api_error(e) from None


def calc(body) -> dict:
    import db
    try:
        if not isinstance(body, dict):
            raise ProfitError("Se esperaba un objeto JSON con las entradas")
        with db.connect() as conn:
            code = _user_currency(conn, body.get("currency"))
            return calculate({**body, "currency": code}, effective_defaults(conn, _uid(), code))
    except ProfitError as e:
        raise _api_error(e) from None


def get_defaults(qs: dict) -> dict:
    import db
    with db.connect() as conn:
        return _defaults_view(conn, _user_currency(conn, (qs.get("currency") or [None])[0]))


def put_defaults(body) -> dict:
    import db
    if not isinstance(body, dict) or not isinstance(body.get("defaults"), dict):
        raise ApiError(400, "Se esperaba {\"currency\": ..., \"defaults\": {...}}", code="invalid_defaults")
    try:
        with db.connect() as conn:
            code = _user_currency(conn, body.get("currency"))
            changes = validate_defaults(body["defaults"], code, allow_null=True)
            uid = _uid()
            cur = stored_defaults(conn, uid, code)
            for k, x in changes.items():
                if x is None:
                    cur.pop(k, None)
                else:
                    cur[k] = float(x)
            conn.execute("INSERT INTO profit_defaults(user_id, currency, data, updated_at) VALUES (?, ?, ?, ?) "
                         "ON CONFLICT(user_id, currency) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at",
                         (uid, code, json.dumps(cur), datetime.now(timezone.utc).isoformat(timespec="seconds")))
            return _defaults_view(conn, code)
    except ProfitError as e:
        raise _api_error(e) from None


def product_profit(pid: int, qs: dict) -> dict:
    """Precio sugerido (con el redondeo comercial del usuario), costo y envío del producto, convertidos a la
    moneda pedida, más tus supuestos. `inputs` sirve para precargar la calculadora."""
    import db
    with db.connect() as conn:
        p = conn.execute("SELECT id, title, category, supplier, cost, shipping_cost, suggested_price FROM products "
                         "WHERE id = ?", (pid,)).fetchone()
        if not p:
            raise ApiError(404, "Producto no encontrado")
        code = _user_currency(conn, (qs.get("currency") or [None])[0])
        state = currency_mod.get_state(conn)
        rate = state["rates"].get(code)
        if rate is None:
            raise ApiError(400, f"No hay tasa de cambio disponible para {code}", code="no_rate")
        s = state["settings"]
        mode = s.get(currency_mod.CURRENCIES[code]["rounding_setting"], "none") \
            if currency_mod.CURRENCIES[code]["rounding_setting"] else "none"
        inputs = {
            "currency": code,
            "price": currency_mod.round_price(float(p["suggested_price"]) * rate, code, mode),
            "product_cost": currency_mod.convert(p["cost"], state, code),
            "supplier_shipping": currency_mod.convert(p["shipping_cost"], state, code),
        }
        try:
            result = calculate(inputs, effective_defaults(conn, _uid(), code))
        except ProfitError as e:               # precio/costo fuera de tope (datos del proveedor absurdos)
            raise _api_error(e) from None
        return {"product": {"id": p["id"], "title": p["title"], "category": p["category"], "supplier": p["supplier"]},
                "inputs": inputs, "result": result}


ROUTES: list = [
    ("POST", r"/api/profit/calc", lambda m, qs, b: calc(b)),
    ("GET", r"/api/profit/defaults", lambda m, qs, b: get_defaults(qs)),
    ("PUT", r"/api/profit/defaults", lambda m, qs, b: put_defaults(b)),
    ("GET", r"/api/products/(\d+)/profit", lambda m, qs, b: product_profit(int(m[1]), qs)),
]
