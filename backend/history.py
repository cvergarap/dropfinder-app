"""Historial diario de productos (product_snapshots), tendencias, rankings, estacionalidad y estadísticas de proveedores.

Módulo de funcionalidad: expone ROUTES (método, regex, handler(match, qs, body)) que server.py agrega
a sus rutas, y se registra en hooks.on_catalog_write para guardar la foto del día de cada producto
que cambia.

Conceptos
---------
* **Snapshot**: una fila por producto y día en `product_snapshots` (tabla del núcleo, esquema v4) con los
  pedidos acumulados (`orders`), los pedidos desde la fila anterior (`orders_day`), costo, envío y stock.
* **Pedidos de un período**: siempre se calculan como diferencia de acumulados,
  `acum(fin) − acum(inicio)`, donde `acum(d)` es el acumulado de la última fila con fecha ≤ d. Así el
  resultado no depende de que haya una fila por día (huecos, compactación semanal).
  Antes de la primera fila de un producto, `acum = orders − orders_day` de esa primera fila
  (lo que llevaba acumulado al empezar a seguirlo).
* **Fecha de referencia** (`until`): la fecha del último snapshot guardado. Si la app no se abre durante
  unos días, los rankings siguen mostrando la última semana con datos en lugar de quedar vacíos.
* **Compactación**: filas diarias para los últimos 120 días y una por semana (lunes) antes de eso. Al
  borrar filas, la siguiente fila que queda absorbe sus pedidos (`orders_day` = pedidos desde la fila
  anterior que queda), así que los acumulados y las sumas siguen cuadrando.
* **Historial sintético de la demo**: los productos de demostración (source='mock') no tienen pasado
  real; se les genera uno determinista de 730 días (ver `synth_history`).
"""
from __future__ import annotations

import bisect
import math
import random
import time
from itertools import accumulate
from datetime import date, timedelta
from functools import lru_cache

import db
import hooks
from errors import ApiError
import search

ROUTES: list = []

DAILY_DAYS = 120               # filas diarias para los últimos 120 días; antes, una por semana (lunes)
SYNTH_DAYS = 730               # días de historial sintético por producto de demostración
SYNTH_MAX_PRODUCTS = 20_000    # con catálogos demo más grandes (bench de 100k) no se genera historial
SEASON_RATIO = 1.8             # mes pico: promedio del mes ≥ 1,8 × promedio del año, en ambos años
SEASON_MIN_YEAR_ORDERS = 60    # volumen mínimo por año para hablar de estacionalidad (evita ruido)
HISTORY_DAYS = (30, 90, 365)
RANKING_DAYS = (7, 30, 90)
RANKING_TYPES = ("top", "rising", "new", "falling")

MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre",
         "octubre", "noviembre", "diciembre"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS history_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
-- Meses pico detectados por producto (una fila por mes que cumple la regla de estacionalidad)
CREATE TABLE IF NOT EXISTS history_seasonality (
  product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
  month INTEGER NOT NULL,             -- 1..12
  strength REAL NOT NULL,             -- min(ratio año 1, ratio año 2): cuántas veces el promedio anual
  PRIMARY KEY (product_id, month)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_hseason_month ON history_seasonality(month, strength);
"""


def today() -> date:
    """Fecha local (la app corre en el PC del usuario). Las pruebas la reemplazan."""
    return date.today()


def _d(day) -> date:
    if day is None:
        return today()
    return day if isinstance(day, date) else date.fromisoformat(str(day))


def _api_error(status: int, msg: str):
    return ApiError(status, msg)


def ensure_schema(conn) -> None:
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='history_seasonality'").fetchone():
        for stmt in SCHEMA.strip().split(";"):
            if stmt.strip():
                conn.execute(stmt)


def _meta(conn, key):
    r = conn.execute("SELECT value FROM history_meta WHERE key = ?", (key,)).fetchone()
    return r[0] if r else None


def _set_meta(conn, key, value) -> None:
    conn.execute("INSERT OR REPLACE INTO history_meta(key, value) VALUES (?, ?)", (key, str(value)))


def anchor_date(conn) -> date | None:
    """Fecha del último snapshot guardado (usa el índice por fecha)."""
    r = conn.execute("SELECT MAX(date) FROM product_snapshots").fetchone()
    return date.fromisoformat(r[0]) if r and r[0] else None


# ================================================================ snapshots

def _ids_table(conn, ids) -> str:
    """Carga ids en una tabla temporal (evita el límite de parámetros de SQLite)."""
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS _hx_ids(id INTEGER PRIMARY KEY)")
    conn.execute("DELETE FROM _hx_ids")
    conn.executemany("INSERT OR IGNORE INTO _hx_ids VALUES (?)", ((int(i),) for i in ids))
    return "SELECT id FROM _hx_ids"


def take_snapshot(conn, ids=None, day=None) -> int:
    """Guarda (o actualiza) la fila del día de cada producto.

    ids=None → los productos recién escritos por db.upsert_products (todas las filas de una carga
    comparten el mismo `updated_at`, el máximo). Si no, la lista de ids dada.

    orders     = products.orders (acumulado)
    orders_day = orders − acumulado de la fila anterior (≥ 0). Normalmente la fila anterior es la de ayer;
                 si hubo días sin snapshot, son los pedidos desde entonces. Sin fila anterior se estima
                 como orders_7d / 7 (sin pasar de orders).
    Una sola sentencia INSERT … SELECT: 100.000 productos en < 1 s.
    """
    ensure_schema(conn)
    day = _d(day).isoformat()
    if ids is None:
        where = "p.updated_at = (SELECT MAX(updated_at) FROM products)"
    else:
        ids = list(ids)
        if not ids:
            return 0
        where = f"p.id IN ({_ids_table(conn, ids)})"
    cur = conn.execute(
        "INSERT INTO product_snapshots(product_id, date, orders, orders_day, cost, shipping_cost, stock) "
        "SELECT p.id, :day, p.orders, "
        "  MAX(0, p.orders - COALESCE((SELECT s.orders FROM product_snapshots s WHERE s.product_id = p.id "
        "                               AND s.date < :day ORDER BY s.date DESC LIMIT 1), "
        "                             p.orders - MIN(p.orders, CAST(ROUND(p.orders_7d / 7.0) AS INTEGER)))), "
        "  p.cost, p.shipping_cost, p.stock "
        f"FROM products p WHERE {where} "
        "ON CONFLICT(product_id, date) DO UPDATE SET orders = excluded.orders, orders_day = excluded.orders_day, "
        "  cost = excluded.cost, shipping_cost = excluded.shipping_cost, stock = excluded.stock",
        {"day": day})
    return cur.rowcount


def keep_row(d: date, cutoff: date) -> bool:
    """Política de almacenamiento: diario desde `cutoff` (= hoy − 119 días); antes, solo los lunes."""
    return d >= cutoff or d.weekday() == 0


def compact(conn, day=None) -> int:
    """Compacta el historial: borra las filas anteriores a los últimos 120 días que no caen en lunes
    (salvo la primera y la última de cada producto) y pasa sus pedidos a la siguiente fila que queda.
    Es incremental: solo revisa las fechas desde la última compactación. Devuelve las filas borradas."""
    ensure_schema(conn)
    cutoff = (_d(day) - timedelta(days=DAILY_DAYS - 1)).isoformat()
    since = _meta(conn, "compacted_until") or ""
    if since >= cutoff:
        return 0
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS _hx_del(product_id INTEGER, date TEXT, PRIMARY KEY (product_id, date))")
    conn.execute("DELETE FROM _hx_del")
    conn.execute(
        "INSERT INTO _hx_del SELECT s.product_id, s.date FROM product_snapshots s "
        "WHERE s.date >= ? AND s.date < ? AND strftime('%w', s.date) <> '1' "
        "AND EXISTS (SELECT 1 FROM product_snapshots e WHERE e.product_id = s.product_id AND e.date < s.date) "
        "AND EXISTS (SELECT 1 FROM product_snapshots e WHERE e.product_id = s.product_id AND e.date > s.date)",
        (since, cutoff))
    n = conn.execute("DELETE FROM product_snapshots WHERE (product_id, date) IN (SELECT product_id, date FROM _hx_del)"
                     ).rowcount
    if n:
        # La fila siguiente a cada borrada pasa a contar los pedidos desde la anterior que queda
        conn.execute(
            "UPDATE product_snapshots SET orders_day = MAX(0, orders - (SELECT p.orders FROM product_snapshots p "
            "  WHERE p.product_id = product_snapshots.product_id AND p.date < product_snapshots.date "
            "  ORDER BY p.date DESC LIMIT 1)) "
            "WHERE (product_id, date) IN (SELECT t.product_id, (SELECT MIN(x.date) FROM product_snapshots x "
            "  WHERE x.product_id = t.product_id AND x.date > t.date) FROM _hx_del t)")
    conn.execute("DELETE FROM _hx_del")
    _set_meta(conn, "compacted_until", cutoff)
    return n


# ================================================================ historial sintético (demo)

# Estacionalidad por categoría (Colombia): (mes, día del centro del pico). Fechas de compra, no de entrega.
SEASONS = {
    "Bebés y niños": [(12, 14)],                  # Navidad (regalos)
    "Hogar y cocina": [(12, 10)],                 # Navidad / novenas / fin de año
    "Fitness": [(1, 12)],                         # propósitos de año nuevo
    "Moda y accesorios": [(5, 6)],                # Día de la Madre (2.º domingo de mayo)
    "Electrónica": [(11, 26), (12, 12)],          # Black Friday y Navidad
}
SUPPLIER_VOLATILITY = {"AliExpress": (3, 7), "CJ Dropshipping": (2, 5), "Amazon": (1, 3), "Almacén Local": (0, 2)}


@lru_cache(maxsize=64)
def _calendar(end: date, days: int):
    """Fechas, factor de día de semana y filas que se guardan (compactación) de una ventana: se calcula una vez."""
    first = end - timedelta(days=days - 1)
    dates = tuple(first + timedelta(days=i) for i in range(days))
    weekday = tuple(0.88 if d.weekday() >= 5 else 1.04 for d in dates)
    cutoff = end - timedelta(days=DAILY_DAYS - 1)
    kept = tuple(i for i, d in enumerate(dates) if keep_row(d, cutoff) or i == days - 1)
    return dates, weekday, kept, tuple(d.isoformat() for d in dates)


@lru_cache(maxsize=64)
def _season_bump(category: str, end: date, days: int, sigma: float = 8.5) -> tuple:
    """Curva de picos (gaussianas de ancho sigma días alrededor de cada fecha de SEASONS), 0 fuera de temporada."""
    dates = _calendar(end, days)[0]
    centers = [date(y, m, dd) for m, dd in SEASONS[category]
               for y in range(dates[0].year - 1, dates[-1].year + 2)]
    out = []
    for d in dates:
        v = 0.0
        for c in centers:
            x = (d - c).days
            if -40 < x < 40:
                v += math.exp(-x * x / (2 * sigma * sigma))
        out.append(v)
    return tuple(out)


def _solve_rising_k(target: float) -> float:
    """k tal que el promedio de exp(k (t−T)/T) en [0, T] sea `target` (0 < target < 1)."""
    lo, hi = 1e-6, 12.0
    for _ in range(40):
        k = (lo + hi) / 2
        if (1 - math.exp(-k)) / k > target:
            lo = k
        else:
            hi = k
    return (lo + hi) / 2


def _round_to_total(rng, vals: list[float], total: int) -> list[int]:
    """Enteros con redondeo estocástico que suman exactamente `total`."""
    out = [int(v) + (rng.random() < v - int(v)) for v in vals]
    diff = total - sum(out)
    n = len(out)
    guard = 0
    while diff and guard < 100_000:
        i = rng.randrange(n)
        if diff > 0 and vals[i] > 0:
            out[i] += 1
            diff -= 1
        elif diff < 0 and out[i] > 0:
            out[i] -= 1
            diff += 1
        guard += 1
    if diff > 0:                    # todos los pesos eran 0: se reparte parejo
        for i in range(diff):
            out[n - 1 - (i % n)] += 1
    return out


def synth_history(p, end: date, days: int = SYNTH_DAYS) -> dict:
    """Historial diario sintético, determinista (semilla = id del producto) y coherente con el producto:
    las dos últimas semanas suman orders_prev_7d y orders_7d, y el acumulado termina en `orders`.

    Formas: "steady" (estable), "rising" (en ascenso), "falling" (en caída), "new" (≈0 hasta hace 2–4
    semanas) y, para algunos productos de categorías estacionales, picos anuales (ver SEASONS).
    La forma sale de los datos: R = (orders − últimas 2 semanas) / (ritmo actual × días restantes) dice si
    el pasado fue más bajo (en ascenso) o si el acumulado alcanza para un ritmo estable o mayor (estable / en
    caída). Lo que no cabe en los 730 días queda como acumulado previo (`base`, "antes del seguimiento");
    en los "nuevos" esa base es lo vendido antes con otra ficha.

    Devuelve {"daily": [pedidos por día], "base": int, "shape": str, "seasonal": bool,
              "cost": [costo por día], "stock": [stock por día]}.
    """
    pid, orders = int(p["id"]), int(p["orders"])
    o7, p7 = int(p["orders_7d"]), int(p["orders_prev_7d"])
    rng = random.Random(pid * 7919 + 104729)
    if o7 + p7 > orders:            # datos raros: se recorta a lo posible
        f = orders / (o7 + p7)
        o7, p7 = int(o7 * f), int(p7 * f)
    n_early = days - 14
    budget = orders - o7 - p7
    rate = p7 / 7.0                  # ritmo diario "actual" (antes de la última semana)
    r = o7 / p7 if p7 else (2.0 if o7 else 1.0)
    dates, weekday, _, _ = _calendar(end, days)
    seasonal = p["category"] in SEASONS and rng.random() < 0.4
    season = None
    if seasonal:
        amp = rng.uniform(3.5, 5.0)
        bump = _season_bump(p["category"], end, days)
        s_now = 1 + amp * bump[n_early - 1]
        season = [(1 + amp * b) / s_now for b in bump]
    m_season = (sum(season[:n_early]) / n_early) if season else 1.0

    T = n_early - 1
    if rate <= 0:
        shape = "falling" if budget > 0 else "steady"
        g = [math.exp(-1.5 * t / T) for t in range(n_early)] if budget > 0 else [0.0] * n_early
        season = None if budget == 0 else season
    else:
        R = budget / (rate * n_early * m_season)
        if r >= 1.75 and rng.random() < 0.35 and not seasonal:
            shape = "new"
            launch = days - rng.randint(17, 26)
            g = [0.0 if t < launch else ((t - launch + 1) / (T - launch + 1)) ** 1.5 for t in range(n_early)]
        elif R < 1:
            shape = "rising"
            k = _solve_rising_k(max(0.09, R))
            if seasonal:
                k = min(k, 0.7)      # tendencia suave para que se vea el pico anual
            g = [math.exp(k * (t - T) / T) for t in range(n_early)]
        elif (r <= 0.8 or rng.random() < 0.15) and R > 1.15:
            shape = "falling"
            k = min(rng.uniform(0.6, 1.6), 0.45 if seasonal else 9.0)
            if (math.exp(k) - 1) / k > R:          # que el acumulado alcance
                k = max(0.05, math.log(R) * 1.5)
            g = [math.exp(k * (T - t) / T) for t in range(n_early)]
        else:
            shape = "steady"
            period, phase = rng.uniform(80, 220), rng.uniform(0, 6.3)
            g = [1 + 0.15 * math.sin(2 * math.pi * t / period + phase) for t in range(n_early)]
    rnd = rng.random
    if season:
        early = [rate * g[t] * season[t] * weekday[t] * (0.6 + 0.8 * rnd()) for t in range(n_early)]
    else:
        early = [rate * g[t] * weekday[t] * (0.6 + 0.8 * rnd()) for t in range(n_early)]
    s = sum(early)
    if s > budget:
        early = [v * budget / s for v in early]
        s = budget
    early_i = _round_to_total(rng, early, int(round(s)) if s < budget else budget)
    base = budget - sum(early_i)
    w = [0.7 + 0.6 * rnd() for _ in range(14)]
    prev_i = _round_to_total(rng, [p7 * x / sum(w[:7]) for x in w[:7]], p7)
    last_i = _round_to_total(rng, [o7 * x / sum(w[7:]) for x in w[7:]], o7)
    daily = early_i + prev_i + last_i

    # Costo: algunos cambios de precio (más frecuentes en AliExpress), hacia atrás desde el costo actual
    lo, hi = SUPPLIER_VOLATILITY.get(p.get("supplier") if hasattr(p, "get") else p["supplier"], (1, 4))
    events = sorted(rng.randrange(1, days) for _ in range(rng.randint(lo, hi)))
    cost = [0.0] * days
    c = float(p["cost"])
    j = days
    for e in reversed(events):
        cost[e:j] = [c] * (j - e)
        c = round(c / rng.uniform(0.87, 1.15), 2)
        j = e
    cost[0:j] = [c] * j
    # Stock: cambia cada ~2 semanas; algunos episodios sin stock; termina en el stock actual
    stock_now = int(p["stock"])
    stock = [0] * days
    lvl = stock_now if stock_now > 0 else rng.randint(20, 800)
    for i in range(days - 1, -1, -14):
        v = 0 if rng.random() < 0.04 else max(1, int(lvl * (0.6 + 0.8 * rng.random())))
        stock[max(0, i - 13):i + 1] = [v] * (i + 1 - max(0, i - 13))
    oos = rng.randint(3, 15) if stock_now == 0 else 0
    stock[days - max(1, oos):] = [stock_now] * max(1, oos)
    return {"daily": daily, "base": base, "shape": shape, "seasonal": bool(season), "cost": cost, "stock": stock,
            "dates": dates}


def synth_rows(p, end: date, days: int = SYNTH_DAYS) -> list[tuple]:
    """Filas de product_snapshots del historial sintético, ya compactadas (diario 120 días, lunes antes)."""
    h = synth_history(p, end, days)
    _, _, kept, iso = _calendar(end, days)
    pid, ship = int(p["id"]), float(p["shipping_cost"])
    cum = list(accumulate(h["daily"], initial=h["base"]))      # cum[i + 1] = acumulado al cierre del día i
    rows, last = [], h["base"]
    cost, stock = h["cost"], h["stock"]
    for i in kept:
        c = cum[i + 1]
        rows.append((pid, iso[i], c, c - last, cost[i], ship, stock[i]))
        last = c
    return rows


def generate_demo_history(conn, ids=None, day=None) -> int:
    """Genera el historial sintético de los productos demo que aún no tienen snapshots.
    No hace nada si la demo tiene más de SYNTH_MAX_PRODUCTS productos (bench con 100k: sería lento y
    ocuparía cientos de MB; esos productos solo tienen el snapshot del día). Devuelve cuántos generó."""
    ensure_schema(conn)
    if conn.execute("SELECT COUNT(*) FROM products WHERE source = 'mock'").fetchone()[0] > SYNTH_MAX_PRODUCTS:
        return 0
    end = _d(day)
    where = "p.source = 'mock' AND NOT EXISTS (SELECT 1 FROM product_snapshots s WHERE s.product_id = p.id)"
    if ids is not None:
        ids = list(ids)
        if not ids:
            return 0
        where += f" AND p.id IN ({_ids_table(conn, ids)})"
    prods = conn.execute("SELECT id, supplier, category, cost, shipping_cost, stock, orders, orders_7d, orders_prev_7d "
                         f"FROM products p WHERE {where}").fetchall()
    if not prods:
        return 0
    done = [p["id"] for p in prods]
    conn.executemany("INSERT OR IGNORE INTO product_snapshots(product_id, date, orders, orders_day, cost, "
                     "shipping_cost, stock) VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (row for p in prods for row in synth_rows(p, end)))
    refresh_seasonality(conn, done)
    return len(done)


def _on_catalog_write(conn, ids) -> None:
    """Hook del núcleo: historial demo (si hace falta), snapshot del día y compactación diaria."""
    ensure_schema(conn)
    if ids is None:          # compatibilidad: "los recién escritos" (mismo updated_at, el máximo)
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM products WHERE updated_at = (SELECT MAX(updated_at) FROM products)")]
    generate_demo_history(conn, ids)     # solo los demo sin snapshots (y si la demo tiene ≤ 20.000)
    take_snapshot(conn, ids)
    compact(conn)
    search.invalidate(conn)          # rankings y estadísticas en caché


hooks.on_catalog_write(_on_catalog_write)


def ensure_backfill(conn) -> None:
    """Bases creadas antes de este módulo: la demo ya estaba cargada y nunca pasó por el hook.
    La primera vez que se usa el historial se genera el de la demo y la foto de hoy del resto."""
    ensure_schema(conn)
    if _meta(conn, "backfill_v1"):
        return
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        if not _meta(conn, "backfill_v1"):
            generate_demo_history(conn)
            missing = [r[0] for r in conn.execute(
                "SELECT id FROM products p WHERE NOT EXISTS (SELECT 1 FROM product_snapshots s WHERE s.product_id = p.id)")]
            take_snapshot(conn, missing)
            _set_meta(conn, "backfill_v1", today().isoformat())
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    search.invalidate()


# ================================================================ series por producto

class _Series:
    """Acumulados de un producto para consultar acum(d) con búsqueda binaria."""

    def __init__(self, rows):
        self.dates = [date.fromisoformat(r["date"]) for r in rows]
        self.rows = rows
        self.pre = (rows[0]["orders"] - rows[0]["orders_day"]) if rows else 0

    def idx(self, d: date) -> int:
        return bisect.bisect_right(self.dates, d) - 1

    def cum(self, d: date) -> int:
        i = self.idx(d)
        return self.rows[i]["orders"] if i >= 0 else self.pre

    def at(self, d: date):
        i = self.idx(d)
        return self.rows[i] if i >= 0 else None


def _pct(cur, prev):
    if not prev:
        return None
    return round((cur - prev) / prev * 100, 1)


def _loglinear_trend(rates: list[float]) -> list[float]:
    """Tendencia exponencial (recta en escala log) de 12 promedios mensuales; se ajusta dos veces, la segunda
    sin los meses que quedan > 1,5× por encima (los picos no deben arrastrar la tendencia)."""
    eps = max(1e-9, sum(rates) / len(rates) * 0.05)
    logs = [math.log(r + eps) for r in rates]
    use = list(range(len(rates)))
    for _ in range(2):
        n = len(use)
        mx = sum(use) / n
        my = sum(logs[i] for i in use) / n
        var = sum((i - mx) ** 2 for i in use) or 1.0
        b = sum((i - mx) * (logs[i] - my) for i in use) / var
        a = my - b * mx
        trend = [math.exp(a + b * i) for i in range(len(rates))]
        keep = [i for i in range(len(rates)) if rates[i] <= 1.5 * trend[i]]
        if len(keep) < 8:
            break
        use = keep
    return trend


def season_from_series(ser: _Series, anchor: date) -> dict[int, float]:
    """Meses pico de un producto: {mes: fuerza}.

    Se toman los 24 meses calendario más recientes (el mes en curso cuenta si lleva ≥ 7 días) y se dividen
    en dos años de 12 meses. Los pedidos se reparten por día (los de una fila semanal, parejo entre sus
    días) y se compara el promedio diario de cada mes con el promedio diario de su año. Un mes es pico si en
    AMBOS años:
      * su promedio es ≥ 1,8 × el promedio del año (regla de negocio), y
      * también es ≥ 1,8 × la tendencia del año (ajuste exponencial): así un producto que simplemente crece
        no "pica" siempre en los últimos meses de cada año.
    Cada año necesita ≥ 60 pedidos. Fuerza = el menor de los dos cocientes contra el promedio anual."""
    if not ser.rows:
        return {}
    start = anchor - timedelta(days=760)
    n = (anchor - start).days + 1
    arr = [0.0] * n
    prev_d, prev_c = start - timedelta(days=1), ser.cum(start - timedelta(days=1))
    for d, r in zip(ser.dates, ser.rows):
        if d < start:
            prev_d, prev_c = d, r["orders"]
            continue
        if d > anchor:
            break
        a, b = max(0, (prev_d - start).days + 1), (d - start).days + 1      # días (prev_d, d]
        span = b - a
        if span > 0:
            arr[a:b] = [(r["orders"] - prev_c) / span] * span
        prev_d, prev_c = d, r["orders"]
    # Meses calendario: (mes, suma, días)
    # (la primera fila cubre como mucho la semana anterior: antes de eso no hay datos, no "cero ventas")
    covered = max(0, (ser.dates[0] - timedelta(days=6) - start).days)
    months = []
    i = 0
    while i < n:
        d = start + timedelta(days=i)
        nxt = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
        j = min(n, i + (nxt - d).days)
        lo = max(i, covered)
        months.append((d.month, sum(arr[lo:j]), max(0, j - lo)))
        i = j
    if months[-1][2] < 7:
        months.pop()
    months = months[-24:]
    if len(months) < 24 or any(m[2] < 7 for m in months):
        return {}
    per_year = []
    for y in (months[:12], months[12:]):
        rates = [m[1] / m[2] for m in y]
        total = sum(m[1] for m in y)
        if total < SEASON_MIN_YEAR_ORDERS:
            return {}
        avg = sum(rates) / 12
        trend = _loglinear_trend(rates)
        per_year.append({m[0]: (rates[k] / avg, rates[k] / trend[k]) for k, m in enumerate(y)})
    out = {}
    for m in range(1, 13):
        (a1, t1), (a2, t2) = per_year[0][m], per_year[1][m]
        if min(a1, a2, t1, t2) >= SEASON_RATIO:
            out[m] = round(min(a1, a2), 2)
    return out


def _load_series(conn, pid) -> _Series:
    return _Series(conn.execute("SELECT date, orders, orders_day, cost, shipping_cost, stock FROM product_snapshots "
                                "WHERE product_id = ? ORDER BY date", (pid,)).fetchall())


def refresh_seasonality(conn, ids=None) -> int:
    """Recalcula la tabla history_seasonality (de los ids dados o de todos los productos con ≥ 700 días)."""
    ensure_schema(conn)
    anchor = anchor_date(conn)
    if anchor is None:
        return 0
    if ids is None:
        old = (anchor - timedelta(days=690)).isoformat()
        ids = [r[0] for r in conn.execute(
            "SELECT DISTINCT product_id FROM product_snapshots WHERE date <= ?", (old,))]
        conn.execute("DELETE FROM history_seasonality")
        _set_meta(conn, "seasonality_anchor", anchor.isoformat())
    else:
        ids = list(ids)
        conn.executemany("DELETE FROM history_seasonality WHERE product_id = ?", [(i,) for i in ids])
        if _meta(conn, "seasonality_anchor") is None:
            _set_meta(conn, "seasonality_anchor", anchor.isoformat())
    rows = []
    for pid in ids:
        for m, st in season_from_series(_load_series(conn, pid), anchor).items():
            rows.append((pid, m, st))
    conn.executemany("INSERT OR REPLACE INTO history_seasonality(product_id, month, strength) VALUES (?, ?, ?)", rows)
    return len(rows)


def _maybe_refresh_seasonality(conn) -> None:
    """Recalcula la estacionalidad si el último cálculo tiene más de 7 días de datos de atraso."""
    anchor = anchor_date(conn)
    if anchor is None:
        return
    done = _meta(conn, "seasonality_anchor")
    if done and (anchor - date.fromisoformat(done)).days < 7:
        return
    refresh_seasonality(conn)
    conn.commit()


def _season_label(m: int) -> str:
    return f"Sube cada año en {MESES[m - 1]}"


# ================================================================ API: historial de un producto

def _int_param(qs, key, default, allowed=None, lo=None, hi=None):
    v = qs.get(key, [""])[0].strip()
    if v == "":
        return default
    try:
        n = int(v)
    except ValueError:
        raise _api_error(400, f"Parámetro inválido: {key}")
    if allowed and n not in allowed:
        raise _api_error(400, f"{key} debe ser uno de: {', '.join(map(str, allowed))}")
    if lo is not None and hi is not None and not lo <= n <= hi:
        raise _api_error(400, f"{key} debe estar entre {lo} y {hi}")
    return n


def product_history(pid: int, qs: dict) -> dict:
    """GET /api/products/{id}/history?days=30|90|365

    points: un bucket por día (30 y 90 días) o por semana (365 días: 52 semanas que terminan en `until`).
      date = último día del bucket; orders = pedidos en el bucket; stock/cost/total_cost = valores al cierre
      del bucket (null si aún no había historial).
    summary: pedidos del período, del período anterior de igual duración, variación % (null si el anterior
      fue 0) y fecha del bucket con más pedidos.
    seasonality: mes pico más fuerte (ver season_from_series) o null.
    """
    days = _int_param(qs, "days", 30, HISTORY_DAYS)
    with db.connect() as conn:
        ensure_backfill(conn)
        if not conn.execute("SELECT 1 FROM products WHERE id = ?", (pid,)).fetchone():
            raise _api_error(404, "Producto no encontrado")
        _maybe_refresh_seasonality(conn)
        anchor = anchor_date(conn) or today()
        ser = _load_series(conn, pid)
        season = conn.execute("SELECT month, strength FROM history_seasonality WHERE product_id = ? "
                              "ORDER BY strength DESC LIMIT 1", (pid,)).fetchone()
    step = 1 if days <= 90 else 7
    n = days // step
    points = []
    if ser.rows:
        for k in range(n):
            end = anchor - timedelta(days=(n - 1 - k) * step)
            r = ser.at(end)
            points.append({
                "date": end.isoformat(),
                "orders": max(0, ser.cum(end) - ser.cum(end - timedelta(days=step))),
                "stock": r["stock"] if r else None,
                "cost": r["cost"] if r else None,
                "total_cost": round(r["cost"] + r["shipping_cost"], 2) if r else None,
            })
    span = timedelta(days=n * step)
    cur = max(0, ser.cum(anchor) - ser.cum(anchor - span)) if ser.rows else 0
    prev = max(0, ser.cum(anchor - span) - ser.cum(anchor - 2 * span)) if ser.rows else 0
    peak = max(points, key=lambda x: x["orders"]) if points else None
    return {
        "product_id": pid, "days": days, "resolution": "day" if step == 1 else "week",
        "until": anchor.isoformat(),
        "first_date": ser.dates[0].isoformat() if ser.rows else None,
        "points": points,
        "summary": {"orders_period": cur, "orders_prev_period": prev, "change_pct": _pct(cur, prev),
                    "peak_date": peak["date"] if peak and peak["orders"] > 0 else None},
        "seasonality": ({"peak_month": season["month"], "strength": season["strength"],
                         "label": _season_label(season["month"])} if season else None),
    }


# ================================================================ API: rankings

ITEM_COLS = ("id, title, category, supplier, warehouse, image, source, score, total_cost, suggested_price, "
             "margin, orders, stock")
MIN_RISING_PREV = 10          # "en ascenso": el período anterior tuvo al menos 10 pedidos
MIN_FALLING_PREV = 20         # "en caída": el período anterior tuvo al menos 20 pedidos
MIN_PERIOD = 10               # "en ascenso" y "nuevos": al menos 10 pedidos en el período
NEW_PRE_SHARE = 0.25          # "nuevos": pedidos de los 30 días previos ≤ 25 % de los de la ventana (o ≤ 3)


def _product_filters(qs) -> tuple[str, list]:
    where, args = [], []
    for key in ("category", "warehouse", "source", "supplier"):
        vals = [v for v in qs.get(key, []) if v]
        if vals:
            where.append(f"p.{key} IN ({','.join('?' * len(vals))})")
            args += vals
    return (" AND " + " AND ".join(where)) if where else "", args


def _cum_sub(param: str) -> str:
    # Búsqueda por clave primaria (product_id, date): una lectura de índice por producto
    return (f"(SELECT s.orders FROM product_snapshots s WHERE s.product_id = p.id AND s.date <= :{param} "
            "ORDER BY s.date DESC LIMIT 1)")


def compute_ranking(conn, rtype: str, days: int, qs: dict, limit: int) -> dict:
    """Rankings sobre la ventana W = (until − days, until] y la anterior P = (until − 2·days, until − days].

    top      más pedidos en W.
    rising   mayor crecimiento % de W frente a P; exige ≥ 10 pedidos en P y ≥ 10 en W, e historial que
             cubra P (un producto recién descubierto no "crece", solo aparece).
    new      casi sin ventas en los 30 días previos a W (≤ 25 % de los pedidos de W, o ≤ 3) y ≥ 10 pedidos en W;
             exige historial desde antes de esos 30 días. Orden: pedidos en W.
    falling  mayor caída % de W frente a P; exige ≥ 20 pedidos en P e historial que cubra P.
    Desempate: pedidos en W y luego id.
    """
    anchor = anchor_date(conn)
    if anchor is None:
        return {"type": rtype, "days": days, "until": None, "items": []}
    s1 = anchor - timedelta(days=days)
    params = {"a": anchor.isoformat(), "s1": s1.isoformat(), "s2": (s1 - timedelta(days=days)).isoformat(),
              "s3": (s1 - timedelta(days=30)).isoformat()}
    extra = {"top": [], "rising": ["s2"], "falling": ["s2"], "new": ["s3"]}[rtype]
    cond, fargs = _product_filters(qs)
    fparams = {f"f{i}": v for i, v in enumerate(fargs)}
    for i in range(len(fargs)):
        cond = cond.replace("?", f":f{i}", 1)
    cols = ", ".join([_cum_sub("a"), _cum_sub("s1")] + [_cum_sub(x) for x in extra])
    sql = (f"SELECT p.id, f.date AS first_date, f.orders - f.orders_day AS pre, {cols} "
           "FROM products p JOIN product_snapshots f ON f.product_id = p.id "
           "AND f.date = (SELECT MIN(date) FROM product_snapshots m WHERE m.product_id = p.id) "
           f"WHERE 1 = 1{cond}")
    scored = []
    for r in conn.execute(sql, {**params, **fparams}):
        pre = r[2]
        c = [pre if v is None else v for v in r[3:]]
        cur = c[0] - c[1]
        first = r[1]
        if rtype == "top":
            if cur > 0:
                scored.append((cur, cur, r[0], cur, None))
            continue
        if rtype == "new":
            before = c[1] - c[2]
            if first <= params["s3"] and cur >= MIN_PERIOD and before <= max(3, NEW_PRE_SHARE * cur):
                scored.append((cur, cur, r[0], cur, before))
            continue
        prev = c[1] - c[2]
        if first > params["s2"]:
            continue
        if rtype == "rising" and prev >= MIN_RISING_PREV and cur >= MIN_PERIOD and cur > prev:
            scored.append(((cur - prev) / prev, cur, r[0], cur, prev))
        elif rtype == "falling" and prev >= MIN_FALLING_PREV and cur < prev:
            scored.append(((prev - cur) / prev, -cur, r[0], cur, prev))
    scored.sort(key=lambda x: (-x[0], -x[1], -x[2]))
    top = scored[:limit]
    items = []
    if top:
        ids = [t[2] for t in top]
        prods = {r["id"]: dict(r) for r in conn.execute(
            f"SELECT {ITEM_COLS} FROM products p WHERE id IN ({','.join('?' * len(ids))})", ids)}
        sparks = _sparklines(conn, ids, anchor)
        prevs = _prev_periods(conn, [t for t in top if t[4] is None or rtype == "new"], anchor, days)
        for _, _, pid, cur, prev in top:
            d = prods[pid]
            if rtype in ("top", "new"):
                prev = prevs.get(pid, 0)
            d.update(period_orders=cur, prev_period_orders=prev, change_pct=_pct(cur, prev), sparkline=sparks[pid])
            items.append(d)
    return {"type": rtype, "days": days, "until": anchor.isoformat(), "items": items}


def _prev_periods(conn, top, anchor, days) -> dict:
    out = {}
    s1, s2 = anchor - timedelta(days=days), anchor - timedelta(days=2 * days)
    for t in top:
        ser = _load_series(conn, t[2])
        out[t[2]] = max(0, ser.cum(s1) - ser.cum(s2))
    return out


def _sparklines(conn, ids, anchor, n=14) -> dict:
    """Pedidos diarios de los últimos n días (acum(d) − acum(d−1))."""
    out = {}
    for pid in ids:
        ser = _load_series(conn, pid)
        cums = [ser.cum(anchor - timedelta(days=n - k)) for k in range(n + 1)]
        out[pid] = [max(0, cums[k + 1] - cums[k]) for k in range(n)]
    return out


def rankings(qs: dict) -> dict:
    rtype = qs.get("type", ["top"])[0] or "top"
    if rtype not in RANKING_TYPES:
        raise _api_error(400, "type debe ser top, rising, new o falling")
    days = _int_param(qs, "days", 7, RANKING_DAYS)
    limit = _int_param(qs, "limit", 20, lo=1, hi=100)
    started = time.perf_counter()
    key = repr(("history:rank", db.DB_PATH, rtype, days, limit,
                sorted((k, tuple(v)) for k, v in qs.items() if k in ("category", "warehouse", "source", "supplier"))))

    def build():
        with db.connect() as conn:
            ensure_backfill(conn)
            return compute_ranking(conn, rtype, days, qs, limit)
    out = dict(search.cached(key, build))
    out["took_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return out


# ================================================================ API: estacionalidad

def seasonal(qs: dict) -> dict:
    """GET /api/seasonal?month=N (por defecto, el mes siguiente) → productos con pico recurrente ese mes."""
    nxt = today().month % 12 + 1
    month = _int_param(qs, "month", nxt, lo=1, hi=12)
    limit = _int_param(qs, "limit", 12, lo=1, hi=100)
    cond, args = _product_filters(qs)
    with db.connect() as conn:
        ensure_backfill(conn)
        _maybe_refresh_seasonality(conn)
        rows = conn.execute(
            f"SELECT {ITEM_COLS}, h.strength FROM history_seasonality h JOIN products p ON p.id = h.product_id "
            f"WHERE h.month = ?{cond} ORDER BY h.strength DESC, p.orders DESC LIMIT ?", [month, *args, limit]).fetchall()
        total = conn.execute(f"SELECT COUNT(*) FROM history_seasonality h JOIN products p ON p.id = h.product_id "
                             f"WHERE h.month = ?{cond}", [month, *args]).fetchone()[0]
    items = []
    for r in rows:
        d = dict(r)
        d.update(peak_month=month, label=_season_label(month))
        items.append(d)
    return {"month": month, "month_name": MESES[month - 1], "label": _season_label(month), "total": total,
            "items": items}


# ================================================================ API: estadísticas de proveedores

def reliability(avg_ship, pct_oos, avg_rating, price_changes) -> int:
    """Confiabilidad 0–100 = 100 × (0,30·envío + 0,25·stock + 0,25·rating + 0,20·estabilidad de precio)
      envío       = (25 − días promedio) / 23, acotado a [0, 1]  (2 días → 1; 25 días → 0)
      stock       = 1 − % de productos sin stock / 100
      rating      = (rating promedio − 3) / 2, acotado a [0, 1]
      estabilidad = 1 − % de productos con cambio de costo en 30 días / 100 (sin historial: 0,5)"""
    def clamp(x):
        return max(0.0, min(1.0, x))
    ship = clamp((25 - (avg_ship or 25)) / 23)
    stock = clamp(1 - (pct_oos or 0) / 100)
    rating = clamp(((avg_rating or 3) - 3) / 2)
    stab = 0.5 if price_changes is None else clamp(1 - price_changes / 100)
    return round(100 * (0.30 * ship + 0.25 * stock + 0.25 * rating + 0.20 * stab))


def supplier_stats(qs: dict | None = None) -> dict:
    """GET /api/suppliers/stats → una fila por proveedor y origen (demo / csv / api)."""
    def build():
        with db.connect() as conn:
            ensure_backfill(conn)
            anchor = anchor_date(conn) or today()
            since = (anchor - timedelta(days=30)).isoformat()
            rows = conn.execute(
                "WITH ch AS (SELECT product_id, MAX(cost) <> MIN(cost) AS changed FROM product_snapshots "
                "            WHERE date >= ? GROUP BY product_id) "
                "SELECT p.supplier, p.source, COUNT(*) AS products, AVG(p.shipping_days) AS ship, "
                "  AVG(p.stock = 0) * 100 AS oos, AVG(p.rating) AS rating, AVG(p.margin) AS margin, "
                "  AVG(ch.changed) * 100 AS changes, COUNT(ch.product_id) AS tracked "
                "FROM products p LEFT JOIN ch ON ch.product_id = p.id GROUP BY p.supplier, p.source", (since,)
            ).fetchall()
        items = []
        for r in rows:
            changes = round(r["changes"], 1) if r["tracked"] else None
            items.append({
                "supplier": r["supplier"], "source": r["source"], "products": r["products"],
                "avg_shipping_days": round(r["ship"], 1), "pct_out_of_stock": round(r["oos"], 1),
                "avg_rating": round(r["rating"], 2), "avg_margin": round(r["margin"], 1),
                "price_changes_30d": changes, "tracked_products": r["tracked"],
                "reliability": reliability(r["ship"], r["oos"], r["rating"], changes),
            })
        items.sort(key=lambda x: (-x["reliability"], x["supplier"]))
        return {"until": anchor.isoformat(), "items": items,
                "formula": "100 × (0,30·envío + 0,25·stock + 0,25·rating + 0,20·estabilidad de precio)"}
    return search.cached(repr(("history:suppliers", db.DB_PATH)), build)


ROUTES.extend([
    ("GET", r"/api/products/(\d+)/history", lambda m, qs, b: product_history(int(m[1]), qs)),
    ("GET", r"/api/rankings", lambda m, qs, b: rankings(qs)),
    ("GET", r"/api/seasonal", lambda m, qs, b: seasonal(qs)),
    ("GET", r"/api/suppliers/stats", lambda m, qs, b: supplier_stats(qs)),
])
