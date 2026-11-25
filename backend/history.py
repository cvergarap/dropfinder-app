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
* **Medición por producto**: cada producto se mide hasta su **última foto** (`last_observed`), no hasta la
  fecha global: "no tengo datos" no es "vendió 0". Un producto sin fotos dentro de la ventana pedida
  (última foto anterior al inicio de la ventana) no entra en los rankings; en el gráfico, los días
  posteriores a su última foto van sin datos (`orders: null`). `until` = fecha del último snapshot
  de toda la base (sin pasar de hoy).
* **Compactación**: filas diarias para los últimos 120 días y una por semana (lunes) antes de eso. Al
  borrar filas, la siguiente fila que queda absorbe sus pedidos (`orders_day` = pedidos desde la fila
  anterior que queda), así que los acumulados y las sumas siguen cuadrando.
* **Historial sintético de la demo**: los productos de demostración (source='mock') no tienen pasado
  real; se les genera uno determinista de 730 días (ver `synth_history`) y se **extiende día a día** hasta
  hoy (ver `extend_rows`), para que la demo siga "viva" si la app se abre semanas después.

Escrituras y concurrencia
-------------------------
* El hook del catálogo (dentro de la transacción del escritor) solo guarda la foto del día y compacta
  (rápido: 100k productos en ~0,2 s).
* Lo pesado (generar y extender la demo, la foto inicial de bases anteriores a este módulo y la
  estacionalidad) lo hace `run_maintenance` en un **hilo de fondo** (daemon), en lotes cortos con
  `busy_timeout` breve: si otra escritura tiene la base bloqueada, se reintenta más tarde.
* Las peticiones GET **no escriben nunca**: sirven lo que hay y, si la demo todavía se está generando,
  responden `"generating": true` (la interfaz lo muestra y vuelve a consultar).
* Interruptores: `SYNTH_ENABLED` (variable de entorno `DROPFINDER_SYNTH_HISTORY=0` lo apaga) y
  `BACKGROUND` (las pruebas lo apagan y llaman a `run_maintenance()` directamente).
"""
from __future__ import annotations

import bisect
import logging
import math
import os
import random
import sqlite3
import sys
import threading
import time
from datetime import date, timedelta
from functools import lru_cache
from itertools import accumulate

import db
import hooks
import search
from errors import ApiError

log = logging.getLogger("dropfinder.history")

ROUTES: list = []

DAILY_DAYS = 120               # filas diarias para los últimos 120 días; antes, una por semana (lunes)
SYNTH_DAYS = 730               # días de historial sintético por producto de demostración
SYNTH_MAX_PRODUCTS = 20_000    # con catálogos demo más grandes (bench de 100k) no se genera historial
SYNTH_ENABLED = os.environ.get("DROPFINDER_SYNTH_HISTORY", "1") != "0"
BACKGROUND = True              # hilo de mantenimiento; las pruebas lo apagan y llaman run_maintenance()
BATCH = 150                    # productos por transacción en el mantenimiento (bloqueos cortos)
YIELD_S = 0.06                 # pausa entre lotes: otro escritor (carga de CSV, monitor) puede tomar la base
MAINT_BUSY_MS = 1500           # espera máxima por el bloqueo de escritura en el mantenimiento
IDLE_POLL_S = 300.0            # el hilo revisa cada 5 min (cambio de día → extender la demo)
SEASON_RATIO = 1.8             # mes pico: promedio del mes ≥ 1,8 × promedio del año, en ambos años
SEASON_MIN_YEAR_ORDERS = 60    # volumen mínimo por año para hablar de estacionalidad (evita ruido)
SEASON_MIN_DAYS = 690          # historial mínimo (días) para buscar estacionalidad
SEASON_REFRESH_DAYS = 7        # se recalcula cuando la última foto del producto avanzó ≥ 7 días
GAP_UNOBSERVED = 14            # un hueco entre fotos de más de 14 días cuenta como "sin datos"
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
-- Hasta qué foto se calculó la estacionalidad de cada producto
CREATE TABLE IF NOT EXISTS history_season_state (
  product_id INTEGER PRIMARY KEY REFERENCES products(id) ON DELETE CASCADE,
  anchor TEXT NOT NULL
);
-- Productos demo con historial sintético y lo necesario para extenderlo día a día
CREATE TABLE IF NOT EXISTS history_synth (
  product_id INTEGER PRIMARY KEY REFERENCES products(id) ON DELETE CASCADE,
  end0 TEXT NOT NULL,                 -- último día del historial generado de una vez
  last_date TEXT NOT NULL,            -- último día ya extendido
  rate REAL NOT NULL,                 -- pedidos/día de la última semana generada
  growth REAL NOT NULL,               -- crecimiento diario (forma: en ascenso, nuevo, en caída)
  amp REAL NOT NULL                   -- amplitud del pico anual (0 = sin estacionalidad)
);
"""
_TABLES = ("history_meta", "history_seasonality", "history_season_state", "history_synth")


def today() -> date:
    """Fecha local (la app corre en el PC del usuario). Las pruebas la reemplazan."""
    return date.today()


def _d(day) -> date:
    if day is None:
        return today()
    return day if isinstance(day, date) else date.fromisoformat(str(day))


def _api_error(status: int, msg: str):
    return ApiError(status, msg)


def schema_ready(conn) -> bool:
    n = conn.execute(f"SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN "
                     f"({','.join('?' * len(_TABLES))})", _TABLES).fetchone()[0]
    return n == len(_TABLES)


def ensure_schema(conn) -> None:
    """Crea las tablas propias (solo desde escritores: hook o mantenimiento; nunca desde un GET)."""
    if not schema_ready(conn):
        for stmt in SCHEMA.strip().split(";"):
            if stmt.strip():
                conn.execute(stmt)


def _meta(conn, key):
    r = conn.execute("SELECT value FROM history_meta WHERE key = ?", (key,)).fetchone()
    return r[0] if r else None


def _set_meta(conn, key, value) -> None:
    conn.execute("INSERT OR REPLACE INTO history_meta(key, value) VALUES (?, ?)", (key, str(value)))


def anchor_date(conn) -> date | None:
    """`until`: fecha del último snapshot de la base (índice por fecha), sin pasar de hoy."""
    r = conn.execute("SELECT MAX(date) FROM product_snapshots WHERE date <= ?", (today().isoformat(),)).fetchone()
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

    ids=None → los productos recién escritos (todas las filas de una carga comparten el `updated_at`
    máximo). Si no, la lista de ids dada.

    orders     = products.orders (acumulado)
    orders_day = orders − acumulado de la fila anterior (≥ 0). Normalmente la fila anterior es la de ayer;
                 si hubo días sin snapshot, son los pedidos desde entonces. Sin fila anterior se estima
                 como orders_7d / 7 (sin pasar de orders).
    Productos demo con historial sintético: sus pedidos los lleva la extensión diaria (`extend_rows`), así
    que aquí se guarda costo y stock y el acumulado de la fila anterior (la extensión lo completa).
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
    prev = ("(SELECT s.orders FROM product_snapshots s WHERE s.product_id = p.id AND s.date < :day "
            "ORDER BY s.date DESC LIMIT 1)")
    synth = "EXISTS (SELECT 1 FROM history_synth h WHERE h.product_id = p.id)"
    cur = conn.execute(
        "INSERT INTO product_snapshots(product_id, date, orders, orders_day, cost, shipping_cost, stock) "
        f"SELECT p.id, :day, CASE WHEN {synth} THEN COALESCE({prev}, p.orders) ELSE p.orders END, "
        f"  CASE WHEN {synth} THEN 0 ELSE "
        f"  MAX(0, p.orders - COALESCE({prev}, p.orders - MIN(p.orders, CAST(ROUND(p.orders_7d / 7.0) AS INTEGER)))) END, "
        "  p.cost, p.shipping_cost, p.stock "
        f"FROM products p WHERE {where} "
        "ON CONFLICT(product_id, date) DO UPDATE SET "
        "  orders = CASE WHEN EXISTS (SELECT 1 FROM history_synth h WHERE h.product_id = excluded.product_id) "
        "               THEN product_snapshots.orders ELSE excluded.orders END, "
        "  orders_day = CASE WHEN EXISTS (SELECT 1 FROM history_synth h WHERE h.product_id = excluded.product_id) "
        "               THEN product_snapshots.orders_day ELSE excluded.orders_day END, "
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



# Estacionalidad por categoría (Colombia): (mes, día del centro del pico). Fechas de compra, no de entrega.
SEASONS = {
    "Bebés y niños": [(12, 14)],                  # Navidad (regalos)
    "Hogar y cocina": [(12, 10)],                 # Navidad / novenas / fin de año
    "Fitness": [(1, 12)],                         # propósitos de año nuevo
    "Moda y accesorios": [(5, 6)],                # Día de la Madre (2.º domingo de mayo)
    "Electrónica": [(11, 26), (12, 12)],          # Black Friday y Navidad
}
# Crecimiento diario al extender la demo después de generarla (acotado a ×0,3 … ×3 del ritmo inicial)
SHAPE_GROWTH = {"steady": 0.0, "rising": 0.004, "new": 0.008, "falling": -0.004}
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
              "cost": [costo por día], "stock": [stock por día], "dates", y para extenderlo después:
              "rate" (pedidos/día de la última semana), "growth" y "amp"}.
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
    amp = 0.0
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
        if budget == 0:
            season, amp = None, 0.0
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
            "dates": dates, "amp": amp if season else 0.0, "rate": o7 / 7.0, "growth": SHAPE_GROWTH[shape]}


def _bump_at(category: str, d: date, sigma: float = 8.5) -> float:
    v = 0.0
    for m, dd in SEASONS.get(category, ()):
        for y in (d.year - 1, d.year, d.year + 1):
            x = (d - date(y, m, dd)).days
            if -40 < x < 40:
                v += math.exp(-x * x / (2 * sigma * sigma))
    return v


def extend_rows(p, st, last_cum: int, until: date) -> list[tuple]:
    """Días sintéticos siguientes al último extendido (st["last_date"]) hasta `until`, deterministas
    (semilla = id del producto y fecha). Ritmo = el de la última semana generada × crecimiento de su forma
    (acotado a ×0,3–×3) × pico anual si lo tiene. Costo y stock: los actuales del producto."""
    pid = int(p["id"])
    end0 = date.fromisoformat(st["end0"])
    d = date.fromisoformat(st["last_date"]) + timedelta(days=1)
    amp, cat = float(st["amp"]), p["category"]
    s0 = 1 + amp * _bump_at(cat, end0) if amp else 1.0
    rows, cum = [], int(last_cum)
    while d <= until:
        rng = random.Random(pid * 7919 + d.toordinal())
        f = min(3.0, max(0.3, math.exp(float(st["growth"]) * (d - end0).days)))
        s = (1 + amp * _bump_at(cat, d)) / s0 if amp else 1.0
        v = float(st["rate"]) * f * s * (0.88 if d.weekday() >= 5 else 1.04) * (0.6 + 0.8 * rng.random())
        n = int(v) + (rng.random() < v - int(v))
        cum += n
        rows.append((pid, d.isoformat(), cum, n, float(p["cost"]), float(p["shipping_cost"]), int(p["stock"])))
        d += timedelta(days=1)
    return rows


_PROD_COLS = "p.id, p.supplier, p.category, p.cost, p.shipping_cost, p.stock, p.orders, p.orders_7d, p.orders_prev_7d"
_UPSERT_SYNTH = ("INSERT INTO product_snapshots(product_id, date, orders, orders_day, cost, shipping_cost, stock) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(product_id, date) DO UPDATE SET "
                 "orders = excluded.orders, orders_day = excluded.orders_day")


def _demo_allowed(conn) -> bool:
    return SYNTH_ENABLED and conn.execute(
        "SELECT COUNT(*) FROM products WHERE source = 'mock'").fetchone()[0] <= SYNTH_MAX_PRODUCTS


def generate_demo_history(conn, ids=None, day=None, limit=None) -> int:
    """Genera el historial sintético (730 días hasta `day`) de productos demo que aún no lo tienen.
    No hace nada si está desactivado o si la demo tiene más de SYNTH_MAX_PRODUCTS productos (bench con 100k:
    sería lento y ocuparía cientos de MB; esos productos solo tienen las fotos reales del día).
    Si el producto ya tenía alguna foto real (del hook), se reemplaza por la sintética del mismo día.
    Devuelve cuántos generó."""
    ensure_schema(conn)
    if not _demo_allowed(conn):
        return 0
    end = _d(day)
    where = "p.source = 'mock' AND NOT EXISTS (SELECT 1 FROM history_synth h WHERE h.product_id = p.id)"
    if ids is not None:
        ids = list(ids)
        if not ids:
            return 0
        where += f" AND p.id IN ({_ids_table(conn, ids)})"
    sql = f"SELECT {_PROD_COLS} FROM products p WHERE {where} ORDER BY p.id"
    if limit:
        sql += f" LIMIT {int(limit)}"
    prods = conn.execute(sql).fetchall()
    if not prods:
        return 0
    rows, state = [], []
    for p in prods:
        h = synth_history(p, end)
        rows.append(_rows_from(p, h, end))
        state.append((p["id"], end.isoformat(), end.isoformat(), h["rate"], h["growth"], h["amp"]))
    done = [p["id"] for p in prods]
    conn.executemany("DELETE FROM product_snapshots WHERE product_id = ?", [(i,) for i in done])
    conn.executemany("INSERT INTO product_snapshots(product_id, date, orders, orders_day, cost, shipping_cost, stock) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?)", (r for rs in rows for r in rs))
    conn.executemany("INSERT OR REPLACE INTO history_synth VALUES (?, ?, ?, ?, ?, ?)", state)
    refresh_seasonality(conn, done)
    return len(done)


def _rows_from(p, h, end) -> list[tuple]:
    _, _, kept, iso = _calendar(end, SYNTH_DAYS)
    pid, ship = int(p["id"]), float(p["shipping_cost"])
    cum = list(accumulate(h["daily"], initial=h["base"]))
    rows, last = [], h["base"]
    for i in kept:
        c = cum[i + 1]
        rows.append((pid, iso[i], c, c - last, h["cost"][i], ship, h["stock"][i]))
        last = c
    return rows


def extend_demo_history(conn, day=None, limit=None) -> int:
    """Extiende hasta `day` el historial sintético de los productos demo (la demo sigue viva)."""
    ensure_schema(conn)
    if not _demo_allowed(conn):
        return 0
    until = _d(day)
    sql = (f"SELECT {_PROD_COLS}, h.end0, h.last_date, h.rate, h.growth, h.amp, "
           "(SELECT s.orders FROM product_snapshots s WHERE s.product_id = p.id AND s.date <= h.last_date "
           " ORDER BY s.date DESC LIMIT 1) AS last_cum "
           "FROM history_synth h JOIN products p ON p.id = h.product_id WHERE h.last_date < ? ORDER BY p.id")
    if limit:
        sql += f" LIMIT {int(limit)}"
    prods = conn.execute(sql, (until.isoformat(),)).fetchall()
    rows = []
    for p in prods:
        rows += extend_rows(p, p, p["last_cum"] or 0, until)
    conn.executemany(_UPSERT_SYNTH, rows)
    conn.executemany("UPDATE history_synth SET last_date = ? WHERE product_id = ?",
                     [(until.isoformat(), p["id"]) for p in prods])
    if prods:
        refresh_seasonality(conn, [p["id"] for p in prods], only_stale=True)
    return len(prods)


def _on_catalog_write(conn, ids) -> None:
    """Hook del núcleo (dentro de la transacción del escritor): foto del día y compactación diaria.
    Lo pesado (demo, estacionalidad) lo hace el hilo de mantenimiento."""
    ensure_schema(conn)
    take_snapshot(conn, ids)
    compact(conn)
    search.invalidate(conn)          # rankings y estadísticas en caché
    _maint_state["day"] = None       # hay datos nuevos: el mantenimiento debe revisar
    if _worker is not None and _worker.is_alive():
        _wake.set()


hooks.on_catalog_write(_on_catalog_write)


# ================================================================ mantenimiento en segundo plano

_wake = threading.Event()
_worker_lock = threading.Lock()
_run_lock = threading.Lock()
_worker: threading.Thread | None = None
_maint_state = {"day": None, "path": None}      # último día (y base) con el mantenimiento al día


def _step(conn, fn, *args) -> int:
    """Un lote en su propia transacción corta. Si la base está bloqueada, se deja para la próxima vuelta."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        n = fn(conn, *args)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    if n:
        search.invalidate()
    return n


def _missing_snapshots(conn) -> int:
    """Bases anteriores a este módulo: foto de hoy de los productos que no tienen ninguna (salvo demo
    pendiente de generar, que recibe la sintética). Una vez por base."""
    if _meta(conn, "backfill_v1"):
        return 0
    skip_demo = "p.source = 'mock' AND " + ("1" if _demo_allowed(conn) else "0")
    missing = [r[0] for r in conn.execute(
        f"SELECT id FROM products p WHERE NOT ({skip_demo}) AND "
        "NOT EXISTS (SELECT 1 FROM product_snapshots s WHERE s.product_id = p.id)")]
    take_snapshot(conn, missing)
    _set_meta(conn, "backfill_v1", today().isoformat())
    return len(missing)


def _season_batch(conn) -> int:
    return refresh_seasonality(conn, _stale_season_ids(conn, BATCH))


def run_maintenance(max_seconds: float | None = None) -> dict:
    """Pone al día el historial: crea las tablas, foto inicial de bases anteriores, genera y extiende la
    demo, compacta y recalcula la estacionalidad atrasada. En lotes de BATCH productos, cada uno en su
    transacción con busy_timeout corto. Nunca lanza: si la base está ocupada, deja el resto para después.
    Devuelve lo hecho y si quedó al día ("done")."""
    out = {"generated": 0, "extended": 0, "backfilled": 0, "compacted": 0, "seasonality": 0, "done": False}
    path = db.DB_PATH
    if not os.path.exists(path):          # no crear bases vacías (pruebas que ya borraron la suya)
        return out
    started = time.monotonic()
    with _run_lock:
        conn = db.connect()
        try:
            conn.execute(f"PRAGMA busy_timeout = {MAINT_BUSY_MS}")
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='products'").fetchone():
                return out
            day = today()
            if not schema_ready(conn):
                _step(conn, lambda c: ensure_schema(c) or 1)
            out["backfilled"] = _step(conn, _missing_snapshots)
            for key, fn in (("generated", lambda c: generate_demo_history(c, day=day, limit=BATCH)),
                            ("extended", lambda c: extend_demo_history(c, day=day, limit=BATCH)),
                            ("seasonality", _season_batch)):
                while True:
                    n = _step(conn, fn)
                    out[key] += n
                    if not n:
                        break
                    time.sleep(YIELD_S)      # sin esto, un escritor en espera podría no entrar nunca
                    if max_seconds is not None and time.monotonic() - started > max_seconds:
                        return out
            out["compacted"] = _step(conn, lambda c: compact(c, day))
            out["done"] = True
            _maint_state.update(day=day, path=path)
        except sqlite3.OperationalError as e:     # base bloqueada por otra escritura larga: se reintenta
            log.info("Mantenimiento del historial pospuesto: %s", e)
        except Exception:  # noqa: BLE001 — nunca debe tumbar el hilo ni el servidor
            log.exception("Error en el mantenimiento del historial")
        finally:
            conn.close()
    return out


def _needs_maintenance() -> bool:
    return _maint_state["day"] != today() or _maint_state["path"] != db.DB_PATH


def _loop() -> None:
    while True:
        try:
            if BACKGROUND and _needs_maintenance():
                r = run_maintenance()
                if not r["done"]:
                    time.sleep(2.0)            # base ocupada: reintenta pronto
                    continue
            _wake.wait(IDLE_POLL_S)
            _wake.clear()
        except Exception:  # noqa: BLE001 — el hilo nunca debe morir
            log.exception("Error en el hilo del historial")
            time.sleep(5.0)


def ensure_worker() -> None:
    """Arranca el hilo de mantenimiento (daemon: no impide que el proceso o las pruebas terminen)."""
    global _worker
    if not BACKGROUND:
        return
    if _worker is None or not _worker.is_alive():
        with _worker_lock:
            if _worker is None or not _worker.is_alive():
                _worker = threading.Thread(target=_loop, name="history-maintenance", daemon=True)
                _worker.start()
    if _needs_maintenance():
        _wake.set()


def _kick() -> None:
    """Desde las peticiones GET: despierta el mantenimiento si hace falta (sin esperar ni escribir)."""
    try:
        ensure_worker()
    except Exception:  # noqa: BLE001
        log.exception("No se pudo iniciar el mantenimiento del historial")


def _autostart() -> None:
    """Al correr el servidor (python3 server.py), el mantenimiento arranca solo poco después de init_db."""
    main = sys.modules.get("__main__")
    if os.path.basename(getattr(main, "__file__", "") or "") == "server.py":
        t = threading.Timer(1.0, _kick)
        t.daemon = True
        t.start()


def generating(conn) -> bool:
    """True si la demo aún no tiene su historial (lo está generando el hilo de fondo). Solo lee."""
    if not SYNTH_ENABLED:
        return False
    if not schema_ready(conn):
        return conn.execute("SELECT 1 FROM products LIMIT 1").fetchone() is not None
    if not _demo_allowed(conn):
        return False
    return conn.execute("SELECT 1 FROM products p WHERE p.source = 'mock' AND NOT EXISTS "
                        "(SELECT 1 FROM history_synth h WHERE h.product_id = p.id) LIMIT 1").fetchone() is not None


# ================================================================ series por producto

class _Series:
    """Acumulados de un producto para consultar acum(d) con búsqueda binaria."""

    def __init__(self, rows):
        self.dates = [date.fromisoformat(r["date"]) for r in rows]
        self.rows = rows
        self.pre = (rows[0]["orders"] - rows[0]["orders_day"]) if rows else 0

    @property
    def last(self) -> date | None:
        return self.dates[-1] if self.rows else None

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
    trend = rates
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


def season_from_series(ser: _Series, anchor: date | None = None) -> dict[int, float]:
    """Meses pico de un producto: {mes: fuerza}.

    Ancla = la última foto del producto (no la fecha global). Se toman los 24 meses calendario más recientes
    hasta el ancla (el mes en curso cuenta si lleva ≥ 7 días) y se dividen en dos años de 12 meses. Los
    pedidos se reparten por día entre fotos (los de una fila semanal, parejo entre sus días); un hueco de más
    de 14 días entre fotos cuenta como **sin datos** (no como cero ventas) y cada mes necesita ≥ 7 días
    observados. Un mes es pico si en AMBOS años:
      * su promedio diario es ≥ 1,8 × el promedio del año (regla de negocio), y
      * también es ≥ 1,8 × la tendencia del año (ajuste exponencial): así un producto que simplemente crece
        no "pica" siempre en los últimos meses de cada año.
    Cada año necesita ≥ 60 pedidos. Fuerza = el menor de los dos cocientes contra el promedio anual."""
    if not ser.rows:
        return {}
    anchor = min(anchor, ser.last) if anchor else ser.last
    start = anchor - timedelta(days=760)
    n = (anchor - start).days + 1
    arr = [0.0] * n
    obs = [False] * n
    # La primera foto cubre como mucho la semana anterior: antes de eso no hay datos
    prev_d = ser.dates[0] - timedelta(days=7)
    prev_c = ser.pre
    for d, r in zip(ser.dates, ser.rows):
        if d > anchor:
            break
        span_all = (d - prev_d).days
        if d >= start and span_all > 0:
            a, b = max(0, (prev_d - start).days + 1), (d - start).days + 1      # días (prev_d, d]
            if span_all <= GAP_UNOBSERVED:
                v = (r["orders"] - prev_c) / span_all
                arr[a:b] = [v] * (b - a)
                obs[a:b] = [True] * (b - a)
        prev_d, prev_c = d, r["orders"]
    months = []
    i = 0
    while i < n:
        d = start + timedelta(days=i)
        nxt = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
        j = min(n, i + (nxt - d).days)
        days = sum(obs[i:j])
        months.append((d.month, sum(arr[i:j]), days))
        i = j
    if months[-1][2] < 7:
        months.pop()
    months = months[-24:]
    if len(months) < 24 or any(m[2] < 7 for m in months):
        return {}
    per_year = []
    for y in (months[:12], months[12:]):
        rates = [m[1] / m[2] for m in y]
        if sum(r * 365 / 12 for r in rates) < SEASON_MIN_YEAR_ORDERS:
            return {}
        avg = sum(rates) / 12
        if avg <= 0:
            return {}
        trend = _loglinear_trend(rates)
        per_year.append({m[0]: (rates[k] / avg, rates[k] / trend[k] if trend[k] > 0 else 0) for k, m in enumerate(y)})
    out = {}
    for m in range(1, 13):
        (a1, t1), (a2, t2) = per_year[0][m], per_year[1][m]
        if min(a1, a2, t1, t2) >= SEASON_RATIO:
            out[m] = round(min(a1, a2), 2)
    return out


def _load_series(conn, pid) -> _Series:
    return _Series(conn.execute("SELECT date, orders, orders_day, cost, shipping_cost, stock FROM product_snapshots "
                                "WHERE product_id = ? ORDER BY date", (pid,)).fetchall())


def _stale_season_ids(conn, limit: int) -> list[int]:
    """Productos con historial suficiente cuya estacionalidad no se calculó o quedó ≥ 7 días atrás."""
    old = (today() - timedelta(days=SEASON_MIN_DAYS)).isoformat()
    return [r[0] for r in conn.execute(
        "SELECT c.product_id FROM (SELECT DISTINCT product_id FROM product_snapshots WHERE date <= ?) c "
        "LEFT JOIN history_season_state st ON st.product_id = c.product_id "
        "WHERE st.anchor IS NULL OR st.anchor <= date((SELECT MAX(s.date) FROM product_snapshots s "
        "  WHERE s.product_id = c.product_id), ?) ORDER BY c.product_id LIMIT ?",
        (old, f"-{SEASON_REFRESH_DAYS} days", limit))]


def refresh_seasonality(conn, ids=None, only_stale=False) -> int:
    """Recalcula la estacionalidad de los ids dados (o de todos los atrasados), cada uno anclado en su
    última foto. only_stale: solo los que avanzaron ≥ 7 días desde el último cálculo. Devuelve cuántos."""
    ensure_schema(conn)
    if ids is None:
        ids = _stale_season_ids(conn, 10**9)
    ids = [int(i) for i in ids]
    if only_stale and ids:
        st = dict(conn.execute(f"SELECT product_id, anchor FROM history_season_state WHERE product_id IN "
                               f"({_ids_table(conn, ids)})").fetchall())
    done, rows = [], []
    for pid in ids:
        ser = _load_series(conn, pid)
        if not ser.rows:
            continue
        last = ser.last.isoformat()
        if only_stale and pid in st and st[pid] > (ser.last - timedelta(days=SEASON_REFRESH_DAYS)).isoformat():
            continue
        done.append((pid, last))
        rows += [(pid, m, s) for m, s in season_from_series(ser).items()]
    conn.executemany("DELETE FROM history_seasonality WHERE product_id = ?", [(p,) for p, _ in done])
    conn.executemany("INSERT OR REPLACE INTO history_seasonality(product_id, month, strength) VALUES (?, ?, ?)", rows)
    conn.executemany("INSERT OR REPLACE INTO history_season_state(product_id, anchor) VALUES (?, ?)", done)
    return len(done)


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


def _read_conn():
    """Conexión de solo lectura para los GET (nunca escriben: sin bloqueos ni 500 si hay una carga en curso)."""
    conn = db.connect()
    conn.execute("PRAGMA query_only = ON")
    return conn


def product_history(pid: int, qs: dict) -> dict:
    """GET /api/products/{id}/history?days=30|90|365

    points: un bucket por día (30 y 90 días) o por semana (365 días: 52 semanas que terminan en `until`).
      date = último día del bucket; orders = pedidos en el bucket; stock/cost/total_cost = valores al cierre
      del bucket. Los buckets posteriores a la última foto del producto (`last_observed`) o anteriores a su
      primera foto van con null (sin datos).
    summary: pedidos en la ventana de `days` días que termina en `last_observed`, en la anterior de igual
      duración, variación % (null si la anterior fue 0) y fecha del bucket con más pedidos.
    seasonality: mes pico más fuerte (ver season_from_series) o null.
    generating: true mientras el hilo de fondo genera el historial de la demo.
    """
    days = _int_param(qs, "days", 30, HISTORY_DAYS)
    _kick()
    conn = _read_conn()
    try:
        if not conn.execute("SELECT 1 FROM products WHERE id = ?", (pid,)).fetchone():
            raise _api_error(404, "Producto no encontrado")
        until = anchor_date(conn) or today()
        ser = _load_series(conn, pid)
        season = None
        if schema_ready(conn):
            season = conn.execute("SELECT month, strength FROM history_seasonality WHERE product_id = ? "
                                  "ORDER BY strength DESC LIMIT 1", (pid,)).fetchone()
        gen = generating(conn)
    finally:
        conn.close()
    step = 1 if days <= 90 else 7
    n = days // step
    points = []
    last = ser.last
    if ser.rows:
        first = ser.dates[0] - timedelta(days=step)
        for k in range(n):
            end = until - timedelta(days=(n - 1 - k) * step)
            r = ser.at(end)
            known = first <= end <= last
            points.append({
                "date": end.isoformat(),
                "orders": max(0, ser.cum(end) - ser.cum(end - timedelta(days=step))) if known else None,
                "stock": r["stock"] if r and known else None,
                "cost": r["cost"] if r and known else None,
                "total_cost": round(r["cost"] + r["shipping_cost"], 2) if r and known else None,
            })
    span = timedelta(days=n * step)
    cur = max(0, ser.cum(last) - ser.cum(last - span)) if ser.rows else 0
    prev = max(0, ser.cum(last - span) - ser.cum(last - 2 * span)) if ser.rows else 0
    valid = [x for x in points if x["orders"]]
    peak = max(valid, key=lambda x: x["orders"]) if valid else None
    return {
        "product_id": pid, "days": days, "resolution": "day" if step == 1 else "week",
        "until": until.isoformat(),
        "first_date": ser.dates[0].isoformat() if ser.rows else None,
        "last_observed": last.isoformat() if last else None,
        "generating": gen,
        "points": points,
        "summary": {"orders_period": cur, "orders_prev_period": prev, "change_pct": _pct(cur, prev),
                    "peak_date": peak["date"] if peak else None},
        "seasonality": ({"peak_month": season["month"], "strength": season["strength"],
                         "label": _season_label(season["month"])} if season else None),
    }


# ================================================================ API: rankings

ITEM_COLS = ("id, title, category, supplier, warehouse, image, source, score, total_cost, suggested_price, "
             "margin, orders, stock")
MIN_RISING_PREV = 10          # "en ascenso": el período anterior tuvo al menos 10 pedidos
MIN_FALLING_PREV = 20         # "en caída": el período anterior tuvo al menos 20 pedidos
MIN_PERIOD = 10               # "en ascenso" y "nuevos": al menos 10 pedidos en el período
NEW_PREV_SHARE = 0.25         # "nuevos": período anterior (de igual duración) ≤ 25 % del actual (o ≤ 3)


def _product_filters(qs) -> tuple[str, dict]:
    where, args = [], {}
    for key in ("category", "warehouse", "source", "supplier"):
        vals = [v for v in qs.get(key, []) if v]
        if vals:
            names = []
            for i, v in enumerate(vals):
                args[f"{key}{i}"] = v
                names.append(f":{key}{i}")
            where.append(f"p.{key} IN ({','.join(names)})")
    return (" AND " + " AND ".join(where)) if where else "", args


def compute_ranking(conn, rtype: str, days: int, qs: dict, limit: int) -> dict:
    """Rankings medidos **por producto**: la ventana W de `days` días termina en la última foto del producto
    (lp) y P es la anterior de igual duración. Solo entran productos observados dentro de la ventana
    global (lp > until − days): sin fotos recientes no se sabe si vendió.

    top      más pedidos en W.
    rising   mayor crecimiento % de W frente a P; exige ≥ 10 pedidos en P y ≥ 10 en W, e historial que
             cubra P (un producto recién descubierto no "crece", solo aparece).
    new      casi sin ventas en P (≤ 25 % de los pedidos de W, o ≤ 3) y ≥ 10 pedidos en W; exige historial que
             cubra P (si no, solo sería "recién descubierto"). Orden: pedidos en W.
    falling  mayor caída % de W frente a P; exige ≥ 20 pedidos en P e historial que cubra P.
    Desempate: pedidos en W y luego id.
    """
    until = anchor_date(conn)
    if until is None:
        return {"type": rtype, "days": days, "until": None, "items": []}
    cond, fargs = _product_filters(qs)
    cum = ("(SELECT s.orders FROM product_snapshots s WHERE s.product_id = x.id AND s.date <= date(x.lp, '{off}') "
           "ORDER BY s.date DESC LIMIT 1)")
    sql = ("WITH x AS (SELECT p.id AS id, (SELECT MAX(s.date) FROM product_snapshots s WHERE s.product_id = p.id) AS lp "
           f"  FROM products p WHERE 1 = 1{cond}) "
           "SELECT x.id, x.lp, "
           "  (SELECT s.date FROM product_snapshots s WHERE s.product_id = x.id ORDER BY s.date LIMIT 1) AS fd, "
           "  (SELECT s.orders - s.orders_day FROM product_snapshots s WHERE s.product_id = x.id ORDER BY s.date LIMIT 1), "
           "  (SELECT s.orders FROM product_snapshots s WHERE s.product_id = x.id AND s.date = x.lp), "
           f"  {cum.format(off=f'-{days} days')}, {cum.format(off=f'-{2 * days} days')} "
           "FROM x WHERE x.lp > :cut AND x.lp <= :until")
    params = {"cut": (until - timedelta(days=days)).isoformat(), "until": until.isoformat(), **fargs}
    scored = []
    for pid, lp, fd, pre, c0, c1, c2 in conn.execute(sql, params):
        c1 = pre if c1 is None else c1
        c2 = pre if c2 is None else c2
        cur, prev = max(0, c0 - c1), max(0, c1 - c2)
        covers_prev = fd <= (date.fromisoformat(lp) - timedelta(days=2 * days)).isoformat()
        if rtype == "top":
            if cur > 0:
                scored.append((cur, cur, pid, cur, prev, lp))
        elif rtype == "new":
            if covers_prev and cur >= MIN_PERIOD and prev <= max(3, NEW_PREV_SHARE * cur):
                scored.append((cur, cur, pid, cur, prev, lp))
        elif not covers_prev:
            continue
        elif rtype == "rising" and prev >= MIN_RISING_PREV and cur >= MIN_PERIOD and cur > prev:
            scored.append(((cur - prev) / prev, cur, pid, cur, prev, lp))
        elif rtype == "falling" and prev >= MIN_FALLING_PREV and cur < prev:
            scored.append(((prev - cur) / prev, -cur, pid, cur, prev, lp))
    scored.sort(key=lambda x: (-x[0], -x[1], -x[2]))
    top = scored[:limit]
    items = []
    if top:
        ids = [t[2] for t in top]
        prods = {r["id"]: dict(r) for r in conn.execute(
            f"SELECT {ITEM_COLS} FROM products p WHERE id IN ({','.join('?' * len(ids))})", ids)}
        for _, _, pid, cur, prev, lp in top:
            d = prods[pid]
            d.update(period_orders=cur, prev_period_orders=prev, change_pct=_pct(cur, prev), last_observed=lp,
                     sparkline=_sparkline(_load_series(conn, pid), date.fromisoformat(lp)))
            items.append(d)
    return {"type": rtype, "days": days, "until": until.isoformat(), "items": items}


def _sparkline(ser: _Series, last: date, n=14) -> list[int]:
    """Pedidos diarios de los últimos n días hasta la última foto del producto (acum(d) − acum(d−1))."""
    cums = [ser.cum(last - timedelta(days=n - k)) for k in range(n + 1)]
    return [max(0, cums[k + 1] - cums[k]) for k in range(n)]


def rankings(qs: dict) -> dict:
    rtype = qs.get("type", ["top"])[0] or "top"
    if rtype not in RANKING_TYPES:
        raise _api_error(400, "type debe ser top, rising, new o falling")
    days = _int_param(qs, "days", 7, RANKING_DAYS)
    limit = _int_param(qs, "limit", 20, lo=1, hi=100)
    _kick()
    started = time.perf_counter()
    key = repr(("history:rank", db.DB_PATH, today(), rtype, days, limit,
                sorted((k, tuple(v)) for k, v in qs.items() if k in ("category", "warehouse", "source", "supplier"))))

    def build():
        conn = _read_conn()
        try:
            out = compute_ranking(conn, rtype, days, qs, limit)
            out["generating"] = generating(conn)
            return out
        finally:
            conn.close()
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
    _kick()
    conn = _read_conn()
    try:
        gen = generating(conn)
        rows, total = [], 0
        if schema_ready(conn):
            args.update(month=month, limit=limit)
            rows = conn.execute(
                f"SELECT {ITEM_COLS}, h.strength FROM history_seasonality h JOIN products p ON p.id = h.product_id "
                f"WHERE h.month = :month{cond} ORDER BY h.strength DESC, p.orders DESC LIMIT :limit", args).fetchall()
            total = conn.execute(f"SELECT COUNT(*) FROM history_seasonality h JOIN products p ON p.id = h.product_id "
                                 f"WHERE h.month = :month{cond}", args).fetchone()[0]
    finally:
        conn.close()
    items = []
    for r in rows:
        d = dict(r)
        d.update(peak_month=month, label=_season_label(month))
        items.append(d)
    return {"month": month, "month_name": MESES[month - 1], "label": _season_label(month), "total": total,
            "generating": gen, "items": items}


# ================================================================ API: estadísticas de proveedores

RELIABILITY_WEIGHTS = {"ship": 0.30, "stock": 0.25, "rating": 0.25, "stability": 0.20}


def reliability(avg_ship, pct_oos, avg_rating, price_changes) -> int:
    """Confiabilidad 0–100 = 100 × (0,30·envío + 0,25·stock + 0,25·rating + 0,20·estabilidad de precio)
      envío       = (25 − días promedio) / 23, acotado a [0, 1]  (2 días → 1; 25 días → 0)
      stock       = 1 − % de productos sin stock / 100
      rating      = (rating promedio − 3) / 2, acotado a [0, 1]
      estabilidad = 1 − % de productos con cambio de costo en 30 días / 100
    Si no hay datos de precio (ningún producto seguido), la estabilidad no cuenta y los demás pesos se
    reescalan: la falta de datos no premia ni castiga."""
    def clamp(x):
        return max(0.0, min(1.0, x))
    parts = {
        "ship": clamp((25 - (avg_ship or 25)) / 23),
        "stock": clamp(1 - (pct_oos or 0) / 100),
        "rating": clamp(((avg_rating or 3) - 3) / 2),
    }
    if price_changes is not None:
        parts["stability"] = clamp(1 - price_changes / 100)
    w = sum(RELIABILITY_WEIGHTS[k] for k in parts)
    return round(100 * sum(RELIABILITY_WEIGHTS[k] * v for k, v in parts.items()) / w)


SUPPLIER_WINDOW = 30


def supplier_stats(qs: dict | None = None) -> dict:
    """GET /api/suppliers/stats → una fila por proveedor y origen (demo / csv / api).

    price_changes_30d: % de los productos **seguidos** cuyo costo cambió en los 30 días previos a su última
    foto. Seguido = ≥ 2 fotos en esa ventana y última foto dentro de los 30 días previos a `until`. Para
    detectar un cambio se incluye la última foto anterior a la ventana (un cambio justo en el borde
    también cuenta). Sin productos seguidos → null (y la confiabilidad no lo usa)."""
    _kick()

    def build():
        conn = _read_conn()
        try:
            until = anchor_date(conn) or today()
            w = f"-{SUPPLIER_WINDOW} days"
            win = "s.product_id = x.id AND s.date >= date(x.lp, :w)"
            rows = conn.execute(
                "WITH x AS (SELECT p.id, p.supplier, p.source, p.stock, "
                "  CASE WHEN p.source = 'api' AND p.supplier = 'CJ Dropshipping' AND p.shipping_cost = 0 "
                "  AND p.shipping_days = 15 THEN NULL ELSE p.shipping_days END AS shipping_days, p.rating, p.margin, "
                "  (SELECT MAX(s.date) FROM product_snapshots s WHERE s.product_id = p.id) AS lp FROM products p), "
                "y AS (SELECT x.*, "
                f"  (SELECT COUNT(*) FROM product_snapshots s WHERE {win}) AS n, "
                f"  (SELECT MIN(cost) FROM product_snapshots s WHERE {win}) AS cmin, "
                f"  (SELECT MAX(cost) FROM product_snapshots s WHERE {win}) AS cmax, "
                "  (SELECT s.cost FROM product_snapshots s WHERE s.product_id = x.id AND s.date < date(x.lp, :w) "
                "   ORDER BY s.date DESC LIMIT 1) AS cprev FROM x) "
                "SELECT supplier, source, COUNT(*) AS products, AVG(shipping_days) AS ship, "
                "  AVG(stock = 0) * 100 AS oos, AVG(rating) AS rating, AVG(margin) AS margin, "
                "  SUM(n >= 2 AND lp >= :cut) AS tracked, "
                "  SUM(n >= 2 AND lp >= :cut AND (cmax <> cmin OR COALESCE(cprev <> cmin OR cprev <> cmax, 0))) AS changed "
                "FROM y GROUP BY supplier, source",
                {"w": w, "cut": (until - timedelta(days=SUPPLIER_WINDOW)).isoformat()}).fetchall()
            gen = generating(conn)
        finally:
            conn.close()
        items = []
        for r in rows:
            tracked = r["tracked"] or 0
            changes = round(100 * (r["changed"] or 0) / tracked, 1) if tracked else None
            items.append({
                "supplier": r["supplier"], "source": r["source"], "products": r["products"],
                "avg_shipping_days": round(r["ship"], 1) if r["ship"] is not None else None, "pct_out_of_stock": round(r["oos"], 1),
                "avg_rating": round(r["rating"], 2), "avg_margin": round(r["margin"], 1),
                "price_changes_30d": changes, "tracked_products": tracked,
                "reliability": reliability(r["ship"], r["oos"], r["rating"], changes),
            })
        items.sort(key=lambda x: (-x["reliability"], x["supplier"]))
        return {"until": until.isoformat(), "generating": gen, "items": items,
                "formula": "100 × (0,30·envío + 0,25·stock + 0,25·rating + 0,20·estabilidad de precio)"}
    return search.cached(repr(("history:suppliers", db.DB_PATH, today())), build)


ROUTES.extend([
    ("GET", r"/api/products/(\d+)/history", lambda m, qs, b: product_history(int(m[1]), qs)),
    ("GET", r"/api/rankings", lambda m, qs, b: rankings(qs)),
    ("GET", r"/api/seasonal", lambda m, qs, b: seasonal(qs)),
    ("GET", r"/api/suppliers/stats", lambda m, qs, b: supplier_stats(qs)),
])

_autostart()
