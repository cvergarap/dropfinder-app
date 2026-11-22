"""Panel del usuario: indicadores, gráficos, recomendaciones y primeros pasos.

Módulo de funcionalidad: expone ROUTES (método, regex, handler(match, qs, body)) que server.py agrega.

Datos por usuario: toda consulta sobre imported_products, alerts y saved_searches filtra por
auth_context.current_user_id(). El catálogo (products), las fotos diarias (product_snapshots) y las reglas
de precio son de la instalación. No se leen las tablas de cuentas (users, sessions).

GET  /api/dashboard                               → todo el panel en una sola llamada (ver dashboard())
POST /api/dashboard/onboarding/{clave}/dismiss    → marca un paso de "Primeros pasos" como hecho/oculto

Tabla propia (se crea sola con el primer descarte): dashboard_prefs(user_id, key, value).
"""
from __future__ import annotations

import json
import os
import time
from datetime import date, datetime, timedelta, timezone

import auth_context
import currency
import db
import history
import saved_searches
from errors import ApiError
from metrics import DEFAULT_SETTINGS, profit_for

# ---------------------------------------------------------------- supuestos (se devuelven en la respuesta)

WINDOW_DAYS = 30               # ventana de gráficos, alertas y pedidos recientes
ORDER_SHARE = 0.01             # ganancia estimada: vendes el 1 % de los pedidos del producto en 30 días…
MAX_MONTHLY_ORDERS = 30        # …con un tope de 30 ventas al mes por producto (≈ 1 al día)
COST_UP_PCT = 10.0             # "requiere atención": el costo total subió más de 10 % en 30 días
ATTENTION_LIMIT = 5
RECS_LIMIT = 8
TOP_CATEGORIES = 6             # gráfico "Mis productos por categoría": 6 categorías + "Otras"
PREF_CATEGORIES = 3            # recomendaciones: las 3 categorías que más importas

ASSUMPTIONS = (
    "Ganancia estimada del mes: por cada producto activo se suponen ventas mensuales = "
    f"{ORDER_SHARE * 100:g} % de los pedidos que el producto tuvo en el proveedor en los últimos {WINDOW_DAYS} "
    f"días (máximo {MAX_MONTHLY_ORDERS} al mes), multiplicadas por tu ganancia por unidad con tu precio de venta "
    "y las comisiones de las reglas de precio actuales. Si el producto tiene menos de 30 días de historial, sus "
    "pedidos de 30 días se estiman como pedidos de los últimos 7 días × 30 / 7. Es una referencia conservadora, "
    "no una proyección de ventas: depende de tu tienda, tu publicidad y tu precio."
)

ALERT_LABELS = {
    "price_up": "Subió el costo", "price_down": "Bajó el costo", "out_of_stock": "Sin stock",
    "back_in_stock": "Volvió el stock", "negative_margin": "Vendes a pérdida",
    "saved_search_new": "Nuevos en búsquedas",
}

ONBOARDING_KEYS = ("currency", "pricing", "first_import", "saved_search", "collection", "store")
PREFS_DDL = ("CREATE TABLE IF NOT EXISTS dashboard_prefs (user_id INTEGER NOT NULL, key TEXT NOT NULL, "
             "value TEXT NOT NULL, PRIMARY KEY (user_id, key))")


# ---------------------------------------------------------------- utilidades

DEV_USER_ENV = "DROPFINDER_DEV_USER"   # SOLO DESARROLLO: ver _uid()


def _uid() -> int:
    """Usuario de la petición. Sin contexto (sin sesión) → 401 "Inicia sesión"; la interfaz no muestra nada.

    SOLO DESARROLLO: con DROPFINDER_DEV_USER=<id> en el entorno, una petición sin contexto actúa como ese
    usuario. Sirve para probar el panel en esta rama antes de que exista el inicio de sesión; nunca se
    usa en producción (las peticiones autenticadas siempre traen su propio contexto, que tiene prioridad)."""
    try:
        return auth_context.current_user_id()
    except auth_context.NoUserContext:
        dev = os.environ.get(DEV_USER_ENV, "").strip()
        if dev.isdigit() and int(dev) > 0:
            return int(dev)
        raise ApiError(401, "Inicia sesión")

def _table_exists(conn, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                        (name,)).fetchone() is not None


def _prefs(conn, uid: int) -> dict:
    if not _table_exists(conn, "dashboard_prefs"):      # el GET no crea tablas
        return {}
    return {r[0]: r[1] for r in conn.execute("SELECT key, value FROM dashboard_prefs WHERE user_id = ?", (uid,))}


def _pct(cur, base):
    return round((cur / base - 1) * 100, 1) if base else None


def _ids_sql(ids) -> str:
    return ",".join(str(int(i)) for i in ids) or "NULL"     # enteros propios: sin riesgo de inyección


def _my_imports(conn, uid: int, s: dict) -> list[dict]:
    """Mis productos importados con costo/stock actuales del catálogo y ganancia a mi precio de venta."""
    rows = conn.execute(
        "SELECT i.id, i.product_id, i.title, i.sell_price, i.status, i.created_at, p.cost, p.shipping_cost, "
        "p.stock, p.category, p.supplier, p.image, p.source, p.orders_7d "
        "FROM imported_products i JOIN products p ON p.id = i.product_id WHERE i.user_id = ? "
        "ORDER BY i.created_at DESC, i.id DESC", (uid,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["total_cost"] = round(d["cost"] + d["shipping_cost"], 2)
        d["profit"], d["margin"] = profit_for(d["sell_price"], d["total_cost"], s)
        out.append(d)
    return out


def _snapshot_window(conn, ids, start: date, until: date) -> dict:
    """{product_id: [(fecha, costo_total, pedidos_del_día), …]}: primero la última foto anterior a `start`
    (fecha None: valor inicial del costo), luego las fotos de [start, until] por fecha."""
    out: dict = {i: [] for i in ids}
    if not ids:
        return out
    lst = _ids_sql(ids)
    for pid, total in conn.execute(
            f"SELECT s.product_id, s.cost + s.shipping_cost FROM product_snapshots s "
            f"WHERE s.product_id IN ({lst}) AND s.date = (SELECT MAX(t.date) FROM product_snapshots t "
            f"WHERE t.product_id = s.product_id AND t.date < ?)", (start.isoformat(),)):
        out[pid].append((None, total, 0))
    for pid, d, total, od in conn.execute(
            f"SELECT product_id, date, cost + shipping_cost, orders_day FROM product_snapshots "
            f"WHERE product_id IN ({lst}) AND date >= ? AND date <= ? ORDER BY product_id, date",
            (start.isoformat(), until.isoformat())):
        out[pid].append((d, total, od))
    return out


def _first_snapshot(conn, ids) -> dict:
    if not ids:
        return {}
    return dict(conn.execute(f"SELECT product_id, MIN(date) FROM product_snapshots "
                             f"WHERE product_id IN ({_ids_sql(ids)}) GROUP BY product_id").fetchall())


# ---------------------------------------------------------------- partes del panel

def orders_30d(imp: dict, snaps: list, first: str | None, start: date) -> tuple[float, str]:
    """Pedidos del producto (en el proveedor) en los últimos 30 días: suma de los pedidos diarios de las
    fotos si el historial cubre la ventana; si no, pedidos de 7 días del catálogo × 30 / 7."""
    if first and first <= start.isoformat():
        return float(sum(od for d, _, od in snaps if d is not None)), "historial"
    return round(imp["orders_7d"] * WINDOW_DAYS / 7, 1), "estimado_7d"


def estimated_profit(o30: float, profit: float) -> tuple[float, float]:
    """(ventas esperadas al mes, ganancia esperada al mes) = min(tope, pedidos_30d × cuota) × ganancia."""
    expected = min(MAX_MONTHLY_ORDERS, o30 * ORDER_SHARE)
    return expected, expected * profit


def _cost_index(imports: list, snaps: dict, start: date, until: date) -> dict:
    """Índice del costo total promedio de mis productos (canasta fija: mis importados de hoy).
    Cada día, cada producto aporta el costo de su última foto a esa fecha; antes de su primera foto, el de
    esa primera foto (no aporta cambio); sin fotos, su costo actual. Índice = promedio(día) / promedio(día 1) × 100."""
    days = [start + timedelta(days=k) for k in range((until - start).days + 1)]
    if not imports:
        return {"points": [], "products": 0, "change_pct": None}
    series = []
    for imp in imports:
        rows = snaps.get(imp["product_id"]) or []
        dated = [(d, t) for d, t, _ in rows if d is not None]
        cur = next((t for d, t, _ in rows if d is None), dated[0][1] if dated else imp["total_cost"])
        values, k = [], 0
        for day in days:
            iso = day.isoformat()
            while k < len(dated) and dated[k][0] <= iso:
                cur = dated[k][1]
                k += 1
            values.append(cur)
        series.append(values)
    n = len(series)
    avgs = [sum(s[i] for s in series) / n for i in range(len(days))]
    base = avgs[0] or 1
    points = [{"date": d.isoformat(), "index": round(a / base * 100, 1), "avg_total_cost": round(a, 2)}
              for d, a in zip(days, avgs)]
    return {"points": points, "products": n, "change_pct": round(points[-1]["index"] - 100, 1)}


def _attention(imports: list, snaps: dict) -> tuple[list, int]:
    """Productos que requieren acción, uno por producto con su motivo más grave:
    1) vendes a pérdida (de mayor a menor pérdida), 2) sin stock, 3) costo total +10 % en 30 días (de mayor
    a menor alza; base = última foto anterior a la ventana o la primera dentro de ella)."""
    items = []
    for imp in imports:
        rows = snaps.get(imp["product_id"]) or []
        change = _pct(imp["total_cost"], rows[0][1]) if rows else None
        common = {"import_id": imp["id"], "product_id": imp["product_id"], "title": imp["title"],
                  "image": imp["image"], "category": imp["category"], "profit": imp["profit"],
                  "margin": imp["margin"], "total_cost": imp["total_cost"], "sell_price": imp["sell_price"],
                  "change_pct": change}
        if imp["profit"] < 0:
            items.append((0, imp["profit"], {**common, "reason": "negative_margin", "action": "imports",
                                             "message": "Vendes a pérdida: sube el precio de venta"}))
        elif imp["stock"] == 0:
            items.append((1, 0, {**common, "reason": "out_of_stock", "action": "detail",
                                 "message": "El proveedor no tiene stock: pausa la publicación o busca otro"}))
        elif change is not None and change > COST_UP_PCT:
            items.append((2, -change, {**common, "reason": "cost_up", "action": "detail",
                                       "message": f"El costo subió {change:.0f} % en {WINDOW_DAYS} días: revisa tu margen"}))
    items.sort(key=lambda x: (x[0], x[1], x[2]["import_id"]))
    return [x[2] for x in items[:ATTENTION_LIMIT]], len(items)


REC_FIELDS = ("id", "title", "category", "supplier", "image", "source", "total_cost", "suggested_price",
              "margin", "score", "change_pct")


def _recommendations(conn, imports: list) -> dict:
    """"Para ti": productos en tendencia a 30 días (primero "en ascenso", luego "más vendidos", de
    history.rankings) de las 3 categorías que más importo; si no alcanzan 8 (o no importé nada), los
    globales. Excluye lo que ya importé. Sin historial todavía (sin fotos): mejor score del catálogo."""
    mine = {imp["product_id"] for imp in imports}
    counts: dict = {}
    for imp in imports:
        counts[imp["category"]] = counts.get(imp["category"], 0) + 1
    cats = [c for c, _ in sorted(counts.items(), key=lambda x: (-x[1], x[0]))[:PREF_CATEGORIES]]
    out, seen = [], set(mine)
    titles = {str(imp["title"]).strip().lower() for imp in imports}

    def add(items, reason):
        for p in items:
            if len(out) >= RECS_LIMIT:
                return
            title = str(p.get("title") or "").strip().lower()
            if p["id"] in seen or title in titles:      # mismo producto en otro proveedor: una sola tarjeta
                continue
            seen.add(p["id"])
            titles.add(title)
            d = {k: p.get(k) for k in REC_FIELDS}
            d["reason"] = reason
            d["in_my_categories"] = p.get("category") in cats
            out.append(d)

    want = min(100, RECS_LIMIT + len(mine))
    plans = ([("rising", cats), ("top", cats)] if cats else []) + [("rising", None), ("top", None)]
    for rtype, cs in plans:
        if len(out) >= RECS_LIMIT:
            break
        qs = {"type": [rtype], "days": [str(WINDOW_DAYS)], "limit": [str(want)]}
        if cs:
            qs["category"] = list(cs)
        data = history.rankings(qs)       # en caché hasta que cambie el catálogo
        add(data.get("items") or [], "En ascenso" if rtype == "rising" else "Más vendido")
    if len(out) < RECS_LIMIT:
        for cs in ([cats] if cats else []) + [None]:
            cond, args = "", []
            if cs:
                cond = f" WHERE category IN ({','.join('?' * len(cs))})"
                args = list(cs)
            rows = conn.execute(f"SELECT id, title, category, supplier, image, source, total_cost, suggested_price, "
                                f"margin, score, trend AS change_pct FROM products{cond} "
                                f"ORDER BY score DESC, id DESC LIMIT ?", args + [want]).fetchall()
            add([dict(r) for r in rows], "Mejor score")
    return {"categories": cats, "items": out}


def _onboarding(conn, uid: int, prefs: dict, n_imports: int, n_saved: int) -> dict:
    """Primeros pasos.
    - Moneda: hecho si la moneda mostrada ya no es la de fábrica (USD) o si el usuario confirmó que se queda
      en USD (botón "Me quedo en USD" = descarte).
    - Reglas de precio: hecho si difieren de las de fábrica o si confirmó que las revisó (descarte).
    - Colección: solo aparece si existe la tabla `collections` (otro módulo).
    - Tienda: deshabilitado ("Próximamente — Sprint 4"); no cuenta para el progreso.
    Un paso descartado cuenta como hecho."""
    cur = currency.get_settings(conn)
    s = db.get_settings(conn)
    items = [
        {"key": "currency", "label": "Elige la moneda en que ves los precios",
         "hint": "Dólares (USD) o pesos colombianos (COP) con la TRM del día.",
         "done": cur.get("display_currency") != currency.DEFAULTS["display_currency"],
         "action": {"view": "settings", "label": "Elegir moneda"}, "dismiss_label": "Me quedo en USD"},
        {"key": "pricing", "label": "Revisa tus reglas de precio",
         "hint": "Multiplicador y comisiones con que se calculan el precio sugerido y el margen.",
         "done": any(abs(float(s[k]) - float(v)) > 1e-9 for k, v in DEFAULT_SETTINGS.items()),
         "action": {"view": "settings", "label": "Ver reglas"}, "dismiss_label": "Ya las revisé"},
        {"key": "first_import", "label": "Importa tu primer producto",
         "hint": "Busca un producto ganador y pulsa Importar.",
         "done": n_imports > 0, "action": {"view": "search", "label": "Buscar productos"}},
        {"key": "saved_search", "label": "Guarda una búsqueda",
         "hint": "Te avisamos cuando aparezcan productos nuevos que la cumplan.",
         "done": n_saved > 0, "action": {"view": "search", "label": "Ir a buscar"}},
    ]
    if _table_exists(conn, "collections"):
        cols = {r[1] for r in conn.execute("PRAGMA table_info(collections)")}
        sql = "SELECT COUNT(*) FROM collections" + (" WHERE user_id = ?" if "user_id" in cols else "")
        n = conn.execute(sql, (uid,) if "user_id" in cols else ()).fetchone()[0]
        items.append({"key": "collection", "label": "Crea una colección",
                      "hint": "Agrupa productos candidatos para decidir después.",
                      "done": n > 0, "action": {"view": "collections", "label": "Ver colecciones"}})
    items.append({"key": "store", "label": "Conecta tu tienda", "hint": "Próximamente — Sprint 4",
                  "done": False, "disabled": True, "action": None})
    for it in items:
        it.setdefault("disabled", False)
        it.setdefault("dismiss_label", None if it["disabled"] else "Omitir")
        it["dismissed"] = prefs.get(f"dismiss:{it['key']}") == "1"
        if it["dismissed"] and not it["disabled"]:
            it["done"] = True
    countable = [it for it in items if not it["disabled"]]
    done = sum(1 for it in countable if it["done"])
    return {"items": items, "done": done, "total": len(countable),
            "complete": done == len(countable), "hidden": prefs.get("dismiss:checklist") == "1"}


# ---------------------------------------------------------------- API

def dashboard(qs=None) -> dict:
    """GET /api/dashboard → kpis, estimate, charts, attention, recommendations, onboarding, assumptions,
    generated_at, took_ms. Montos en USD (el frontend convierte con money())."""
    started = time.perf_counter()
    uid = _uid()
    with db.connect() as conn:
        s = db.get_settings(conn)
        imports = _my_imports(conn, uid, s)
        until = history.anchor_date(conn) or history.today()
        start = until - timedelta(days=WINDOW_DAYS - 1)
        ids = [imp["product_id"] for imp in imports]
        snaps = _snapshot_window(conn, ids, start, until)
        first = _first_snapshot(conn, ids)

        active = [imp for imp in imports if imp["status"] == "active"]
        monthly, per_product = 0.0, []
        for imp in active:
            o30, src = orders_30d(imp, snaps.get(imp["product_id"]) or [], first.get(imp["product_id"]), start)
            expected, est = estimated_profit(o30, imp["profit"])
            monthly += est
            per_product.append({"import_id": imp["id"], "product_id": imp["product_id"], "orders_30d": o30,
                                "orders_source": src, "expected_orders": round(expected, 2),
                                "profit": imp["profit"], "estimated_profit": round(est, 2)})

        unread = conn.execute("SELECT COUNT(*) FROM alerts WHERE user_id = ? AND read = 0", (uid,)).fetchone()[0]
        saved = conn.execute("SELECT id, params FROM saved_searches WHERE user_id = ?", (uid,)).fetchall()
        new_in_saved = 0
        if saved:
            saved_searches._ensure_schema(conn)       # tabla de candidatos del módulo (idempotente)
            for r in saved:
                try:
                    new_in_saved += saved_searches._count_new(conn, r["id"], json.loads(r["params"]))
                except (ValueError, TypeError):
                    pass

        cutoff = (datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)).isoformat(timespec="seconds")
        by_type = [{"type": t, "label": ALERT_LABELS.get(t, t), "count": n} for t, n in conn.execute(
            "SELECT type, COUNT(*) FROM alerts WHERE user_id = ? AND created_at >= ? GROUP BY type "
            "ORDER BY COUNT(*) DESC, type", (uid, cutoff))]

        cat_counts: dict = {}
        for imp in imports:
            cat_counts[imp["category"]] = cat_counts.get(imp["category"], 0) + 1
        ranked = sorted(cat_counts.items(), key=lambda x: (-x[1], x[0]))
        by_cat = [{"category": c, "count": n} for c, n in ranked[:TOP_CATEGORIES]]
        rest = sum(n for _, n in ranked[TOP_CATEGORIES:])
        if rest:
            by_cat.append({"category": "Otras", "count": rest})

        attention, attention_total = _attention(imports, snaps)
        recs = _recommendations(conn, imports)
        onboarding = _onboarding(conn, uid, _prefs(conn, uid), len(imports), len(saved))

    margins = [imp["margin"] for imp in imports]
    kpis = {
        "imported": len(imports),
        "active": len(active),
        "drafts": sum(1 for imp in imports if imp["status"] == "draft"),
        "avg_margin": round(sum(margins) / len(margins), 1) if margins else None,
        "potential_profit_per_sale": round(sum(imp["profit"] for imp in active), 2),
        "estimated_monthly_profit": round(monthly, 2),
        "unread_alerts": unread,
        "out_of_stock": sum(1 for imp in imports if imp["stock"] == 0),
        "negative_margin": sum(1 for imp in imports if imp["profit"] < 0),
        "saved_searches": len(saved),
        "new_in_saved_searches": new_in_saved,
    }
    return {
        "kpis": kpis,
        "estimate": {"order_share": ORDER_SHARE, "max_monthly_orders": MAX_MONTHLY_ORDERS,
                     "window_days": WINDOW_DAYS, "products": per_product},
        "charts": {
            "cost_index": {"from": start.isoformat(), "until": until.isoformat(),
                           **_cost_index(imports, snaps, start, until)},
            "alerts_by_type": {"days": WINDOW_DAYS, "items": by_type},
            "imports_by_category": {"items": by_cat},
        },
        "attention": {"items": attention, "total": attention_total},
        "recommendations": recs,
        "onboarding": onboarding,
        "assumptions": ASSUMPTIONS,
        "generated_at": db.now(),
        "took_ms": round((time.perf_counter() - started) * 1000, 1),
    }


def dismiss(key: str) -> dict:
    """POST /api/dashboard/onboarding/{clave}/dismiss: el paso cuenta como hecho para este usuario.
    La clave "checklist" oculta la lista completa."""
    if key not in ONBOARDING_KEYS and key != "checklist":
        raise ApiError(404, "Paso de primeros pasos desconocido")
    uid = _uid()
    with db.connect() as conn:
        conn.execute(PREFS_DDL)
        conn.execute("INSERT OR REPLACE INTO dashboard_prefs(user_id, key, value) VALUES (?, ?, '1')",
                     (uid, f"dismiss:{key}"))
    return {"ok": True, "key": key}


ROUTES: list = [
    ("GET", r"/api/dashboard", lambda m, qs, b: dashboard(qs)),
    ("POST", r"/api/dashboard/onboarding/([a-z_]+)/dismiss", lambda m, qs, b: dismiss(m[1])),
]
