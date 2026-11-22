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

import logging
import os
import sys
import threading
import time
from datetime import date, timedelta

import auth_context
import collections_mod
import currency
import db
import history
import hooks
import saved_searches
from errors import ApiError
from metrics import DEFAULT_SETTINGS, profit_for

log = logging.getLogger("dropfinder.dashboard")

# ---------------------------------------------------------------- supuestos (se devuelven en la respuesta)

WINDOW_DAYS = 30               # ventana de gráficos, alertas y pedidos recientes
ORDER_SHARE = 0.01             # ganancia estimada: vendes el 1 % de los pedidos del producto en 30 días…
MAX_MONTHLY_ORDERS = 30        # …con un tope de 30 ventas al mes por producto (≈ 1 al día)
COST_UP_PCT = 10.0             # "requiere atención": el costo total subió más de 10 % en 30 días
ATTENTION_LIMIT = 5
RECS_LIMIT = 8
RANK_LIMIT = 100            # rankings: siempre el mismo límite (una sola clave de caché por tipo)
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

OTHER_LABEL = "Otras categorías"   # grupo del gráfico por categoría (category = None, other = True)

ONBOARDING_KEYS = ("currency", "pricing", "first_import", "saved_search", "collection", "store")
PREFS_DDL = ("CREATE TABLE IF NOT EXISTS dashboard_prefs (user_id INTEGER NOT NULL, key TEXT NOT NULL, "
             "value TEXT NOT NULL, PRIMARY KEY (user_id, key))")


# ---------------------------------------------------------------- utilidades

def _uid() -> int:
    """Usuario de la petición. Sin contexto (sin sesión) → 401 "Inicia sesión" (la interfaz no muestra nada).
    El usuario lo fija siempre el servidor (sesión) o `auth_context.as_user` (pruebas); este módulo no tiene
    ningún atajo propio."""
    try:
        return auth_context.current_user_id()
    except auth_context.NoUserContext:
        raise ApiError(401, "Inicia sesión") from None


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


def _snapshot_window(conn, ids, start: date, until: date) -> tuple[dict, dict, dict]:
    """Fotos de mis productos en la ventana [start, until], con búsquedas por producto en el índice
    (product_id, date) y solo las filas que hacen falta. Devuelve (snaps, first, orders):
    - snaps {product_id: [(fecha, costo_total), …]}: primero la última foto anterior a `start` (fecha None:
      valor inicial del costo) y luego, dentro de la ventana, la primera foto y las que cambian el costo;
    - first {product_id: fecha de su primera foto}; orders {product_id: suma de pedidos del día en la ventana}."""
    snaps: dict = {i: [] for i in ids}
    first: dict = {}
    orders: dict = {}
    if not ids:
        return snaps, first, orders
    s_iso, u_iso = start.isoformat(), until.isoformat()
    values = ",".join(f"({int(i)})" for i in ids)        # enteros propios: sin riesgo de inyección
    for pid, pre, fd, od in conn.execute(
            f"WITH ids(id) AS (VALUES {values}) SELECT id, "
            "(SELECT cost + shipping_cost FROM product_snapshots WHERE product_id = ids.id AND date < :s "
            " ORDER BY date DESC LIMIT 1), "
            "(SELECT date FROM product_snapshots WHERE product_id = ids.id ORDER BY date LIMIT 1), "
            "(SELECT SUM(orders_day) FROM product_snapshots WHERE product_id = ids.id AND date >= :s AND date <= :u) "
            "FROM ids", {"s": s_iso, "u": u_iso}):
        if pre is not None:
            snaps[pid].append((None, pre))
        if fd is not None:
            first[pid] = fd
        orders[pid] = od or 0
    for pid, d, total in conn.execute(
            f"WITH ids(id) AS (VALUES {values}) SELECT product_id, date, tot FROM ("
            " SELECT s.product_id, s.date, s.cost + s.shipping_cost AS tot, "
            "  LAG(s.cost + s.shipping_cost) OVER (PARTITION BY s.product_id ORDER BY s.date) AS prev "
            " FROM ids JOIN product_snapshots s ON s.product_id = ids.id AND s.date >= :s AND s.date <= :u) "
            "WHERE prev IS NULL OR tot <> prev ORDER BY product_id, date", {"s": s_iso, "u": u_iso}):
        snaps[pid].append((d, total))
    return snaps, first, orders


# ---------------------------------------------------------------- partes del panel

def orders_30d(imp: dict, window_orders: float, first: str | None, start_iso: str) -> tuple[float, str]:
    """Pedidos del producto (en el proveedor) en los últimos 30 días: suma de los pedidos diarios de las
    fotos si el historial cubre la ventana; si no, pedidos de 7 días del catálogo × 30 / 7.
    Limitación compartida con history.rankings: si un hueco entre fotos cruza el inicio de la ventana, la
    primera foto de la ventana trae también los pedidos del hueco."""
    if first and first <= start_iso:
        return float(window_orders), "historial"
    return round(imp["orders_7d"] * WINDOW_DAYS / 7, 1), "estimado_7d"


def estimated_profit(o30: float, profit: float) -> tuple[float, float]:
    """(ventas esperadas al mes, ganancia esperada al mes) = min(tope, pedidos_30d × cuota) × ganancia."""
    expected = min(MAX_MONTHLY_ORDERS, o30 * ORDER_SHARE)
    return expected, expected * profit


def _cost_index(imports: list, snaps: dict, start: date, until: date) -> dict:
    """Índice del costo total promedio de mis productos (canasta fija: mis importados de hoy).
    Cada día, cada producto aporta el costo de su última foto a esa fecha; antes de su primera foto, el de
    esa primera foto (no aporta cambio); sin fotos, su costo actual. Índice = promedio(día) / promedio(día 1) × 100."""
    days = [(start + timedelta(days=k)).isoformat() for k in range((until - start).days + 1)]   # una sola vez
    if not imports:
        return {"points": [], "products": 0, "change_pct": None}
    totals = [0.0] * len(days)
    for imp in imports:
        rows = snaps.get(imp["product_id"]) or []
        dated = [(d, t) for d, t in rows if d is not None]
        cur = next((t for d, t in rows if d is None), dated[0][1] if dated else imp["total_cost"])
        k, nd = 0, len(dated)
        for i, iso in enumerate(days):
            while k < nd and dated[k][0] <= iso:
                cur = dated[k][1]
                k += 1
            totals[i] += cur
    n = len(imports)
    avgs = [t / n for t in totals]
    base = avgs[0] or 1
    points = [{"date": d, "index": round(a / base * 100, 1), "avg_total_cost": round(a, 2)}
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


def _ranking(rtype: str, cats=None) -> list:
    """history.rankings a 30 días con límite fijo y categorías ordenadas: la misma clave de caché para todos
    los usuarios y para cualquier cantidad de importados (se invalida solo cuando cambia el catálogo)."""
    qs = {"type": [rtype], "days": [str(WINDOW_DAYS)], "limit": [str(RANK_LIMIT)]}
    if cats:
        qs["category"] = sorted(cats)
    return history.rankings(qs).get("items") or []


class _Picker:
    """Arma la lista "Para ti" sin repetir productos ni títulos y sin lo que ya importé."""

    def __init__(self, imports: list, cats: list):
        self.cats = cats
        self.out: list = []
        self.seen = {imp["product_id"] for imp in imports}
        self.titles = {str(imp["title"]).strip().lower() for imp in imports}

    def full(self) -> bool:
        return len(self.out) >= RECS_LIMIT

    def add(self, items, reason, only_mine=False) -> None:
        for p in items:
            if self.full():
                return
            if only_mine and p.get("category") not in self.cats:
                continue
            title = str(p.get("title") or "").strip().lower()
            if p["id"] in self.seen or title in self.titles:   # mismo producto en otro proveedor: una tarjeta
                continue
            self.seen.add(p["id"])
            self.titles.add(title)
            d = {k: p.get(k) for k in REC_FIELDS}
            d["reason"] = reason if reason is not None else p.get("reason")   # None: conserva el motivo
            d["in_my_categories"] = p.get("category") in self.cats
            self.out.append(d)


def _from_rankings(imports: list, cats: list) -> list:
    """Parte de "Para ti" que sale de history.rankings (no usa la conexión de la petición: puede correr en
    otro hilo). Solo se piden los dos rankings globales a 30 días ("en ascenso" y "más vendidos", 100
    productos cada uno: una clave de caché compartida por todos los usuarios) y se filtran por mis
    categorías; luego se completa con los globales. Con 8 categorías, los 100 globales traen de sobra de
    las mías; si una categoría muy pequeña no aparece, la completan los globales o "Mejor score"."""
    pk = _Picker(imports, cats)
    pools = [(_ranking("rising"), "En ascenso")]
    if cats:
        pk.add(pools[0][0], "En ascenso", only_mine=True)
    if cats and not pk.full():
        pools.append((_ranking("top"), "Más vendido"))
        pk.add(pools[1][0], "Más vendido", only_mine=True)
    for items, reason in pools:
        pk.add(items, reason)
    if not pk.full() and len(pools) == 1:
        pk.add(_ranking("top"), "Más vendido")
    return pk.out


# Presupuesto de tiempo para los rankings dentro de la petición. history.rankings queda en caché hasta que
# cambia el catálogo, pero calcularlo en frío cuesta ~300 ms con 20.000 productos. Con el servidor corriendo
# (ASYNC_RANKINGS, ver _autostart) se calcula en un hilo: si no termina en RANK_BUDGET_S, el panel responde
# con "Mejor score" y `pending: true`, el hilo deja la caché llena y la interfaz vuelve a pedir el panel.
# En las pruebas se calcula en la misma petición (determinista), salvo que se active a propósito.
ASYNC_RANKINGS = False
RANK_BUDGET_S = 0.02
_rank_job: dict = {"thread": None}


def _rankings_within_budget(imports: list, cats: list) -> tuple[list, bool]:
    """(productos de los rankings, pendiente) respetando el presupuesto de tiempo."""
    if not ASYNC_RANKINGS:
        return _from_rankings(imports, cats), False
    box: dict = {}

    def run():
        try:
            box["items"] = _from_rankings(imports, cats)
        except Exception:  # noqa: BLE001
            log.exception("No se pudieron calcular los rankings del panel")
            box["items"] = []
    t = threading.Thread(target=run, name="dashboard-rankings", daemon=True)
    _rank_job["thread"] = t
    t.start()
    t.join(RANK_BUDGET_S)
    if t.is_alive():
        return [], True
    return box.get("items", []), False


def wait_rankings(timeout: float = 30.0) -> None:
    """Espera al último cálculo de rankings en segundo plano (pruebas y bench)."""
    t = _rank_job["thread"]
    if t is not None:
        t.join(timeout)


def _recommendations(conn, uid: int, imports: list) -> dict:
    """"Para ti": productos en tendencia a 30 días (history.rankings) de las 3 categorías que más importo,
    primero "en ascenso" y luego "más vendidos"; si no alcanzan 8 (o no importé nada), los globales. Excluye
    lo que ya importé (y el mismo título en otro proveedor). Sin historial todavía (sin fotos) o mientras los
    rankings se calculan en segundo plano (pending): mejor score del catálogo."""
    counts: dict = {}
    for imp in imports:
        counts[imp["category"]] = counts.get(imp["category"], 0) + 1
    cats = [c for c, _ in sorted(counts.items(), key=lambda x: (-x[1], str(x[0])))[:PREF_CATEGORIES]]
    # Respaldo ("Mejor score") antes de los rankings: consulta rápida que no compite con su hilo
    fallback = []
    for cs in ([cats] if cats else []) + [None]:
        cond = "id NOT IN (SELECT product_id FROM imported_products WHERE user_id = ?)"
        args = [uid]
        if cs:
            cond += f" AND category IN ({','.join('?' * len(cs))})"
            args += list(cs)
        fallback.append([dict(r) for r in conn.execute(
            f"SELECT id, title, category, supplier, image, source, total_cost, suggested_price, margin, score, "
            f"trend AS change_pct FROM products WHERE {cond} ORDER BY score DESC, id DESC LIMIT ?",
            args + [RECS_LIMIT * 25])])         # holgura: el mismo título se repite entre proveedores
    ranked, pending = _rankings_within_budget(imports, cats)
    pk = _Picker(imports, cats)
    pk.add(ranked, None)
    for rows in fallback:
        pk.add(rows, "Mejor score")
    return {"categories": cats, "items": pk.out, "pending": pending}


def _onboarding(conn, uid: int, owner: bool, prefs: dict, n_imports: int, n_saved: int) -> dict:
    """Primeros pasos.
    - Moneda y reglas de precio son ajustes de la instalación (los administra el propietario):
      · propietario: hecho si difieren de los de fábrica (moneda distinta de USD; alguna regla distinta) o si
        lo confirmó ("Me quedo en USD" / "Ya las revisé" = descarte);
      · miembro: solo cuenta su propio descarte (que el propietario cambie un ajuste no completa su paso).
    - Colección: collections_mod.count_for_user (API pública): solo las colecciones del usuario, sin la
      lista automática "Favoritos".
    - Tienda: deshabilitado ("Próximamente — Sprint 4"); no cuenta para el progreso.
    Un paso descartado cuenta como hecho."""
    cur = currency.get_settings(conn)
    s = db.get_settings(conn)
    currency_done = owner and cur.get("display_currency") != currency.DEFAULTS["display_currency"]
    pricing_done = owner and any(abs(float(s[k]) - float(v)) > 1e-9 for k, v in DEFAULT_SETTINGS.items())
    items = [
        {"key": "currency", "label": "Elige la moneda en que ves los precios",
         "hint": "Dólares (USD) o pesos colombianos (COP) con la TRM del día.",
         "done": currency_done,
         "action": {"view": "settings", "label": "Elegir moneda"}, "dismiss_label": "Me quedo en USD"},
        {"key": "pricing", "label": "Revisa tus reglas de precio" if owner else "Conoce las reglas de precio",
         "hint": ("Multiplicador y comisiones con que se calculan el precio sugerido y el margen." if owner
                  else "Las define el propietario; con ellas se calculan el precio sugerido y el margen."),
         "done": pricing_done,
         "action": {"view": "settings", "label": "Ver reglas"},
         "dismiss_label": "Ya las revisé" if owner else "Entendido"},
        {"key": "first_import", "label": "Importa tu primer producto",
         "hint": "Busca un producto ganador y pulsa Importar.",
         "done": n_imports > 0, "action": {"view": "search", "label": "Buscar productos"}},
        {"key": "saved_search", "label": "Guarda una búsqueda",
         "hint": "Te avisamos cuando aparezcan productos nuevos que la cumplan.",
         "done": n_saved > 0, "action": {"view": "search", "label": "Ir a buscar"}},
    ]
    # API pública del módulo de colecciones: solo las del usuario y sin la lista automática "Favoritos"
    n_collections = collections_mod.count_for_user(conn, uid)["collections"]
    items.append({"key": "collection", "label": "Crea una colección",
                  "hint": "Agrupa productos candidatos para decidir después.",
                  "done": n_collections > 0, "action": {"view": "collections", "label": "Ver colecciones"}})
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
        snaps, first, window_orders = _snapshot_window(conn, ids, start, until)
        start_iso = start.isoformat()

        active = [imp for imp in imports if imp["status"] == "active"]
        monthly, per_product = 0.0, []
        for imp in active:
            o30, src = orders_30d(imp, window_orders.get(imp["product_id"], 0), first.get(imp["product_id"]), start_iso)
            expected, est = estimated_profit(o30, imp["profit"])
            monthly += est
            per_product.append({"import_id": imp["id"], "product_id": imp["product_id"], "orders_30d": o30,
                                "orders_source": src, "expected_orders": round(expected, 2),
                                "profit": imp["profit"], "estimated_profit": round(est, 2)})

        unread = conn.execute("SELECT COUNT(*) FROM alerts WHERE user_id = ? AND read = 0", (uid,)).fetchone()[0]
        new_counts = saved_searches.new_counts(conn, uid)      # API pública del módulo (solo lee)

        # Alertas de los últimos 30 días según el mismo reloj que el resto del panel (history.today())
        cutoff = (history.today() - timedelta(days=WINDOW_DAYS - 1)).isoformat()
        by_type = [{"type": t, "label": ALERT_LABELS.get(t, t), "count": n} for t, n in conn.execute(
            "SELECT type, COUNT(*) FROM alerts WHERE user_id = ? AND created_at >= ? GROUP BY type "
            "ORDER BY COUNT(*) DESC, type", (uid, cutoff))]

        cat_counts: dict = {}
        for imp in imports:
            cat_counts[imp["category"]] = cat_counts.get(imp["category"], 0) + 1
        ranked = sorted(cat_counts.items(), key=lambda x: (-x[1], str(x[0])))
        by_cat = [{"category": c, "label": c, "count": n, "other": False} for c, n in ranked[:TOP_CATEGORIES]]
        rest = sum(n for _, n in ranked[TOP_CATEGORIES:])
        if rest:          # category None: no se confunde con una categoría real llamada "Otras"
            by_cat.append({"category": None, "label": OTHER_LABEL, "count": rest, "other": True})

        attention, attention_total = _attention(imports, snaps)
        onboarding = _onboarding(conn, uid, auth_context.is_owner(), _prefs(conn, uid), len(imports), len(new_counts))
        recs = _recommendations(conn, uid, imports)      # al final: su hilo de rankings no compite con lo demás

    margins = [imp["margin"] for imp in imports]
    kpis = {
        "imported": len(imports),
        "active": len(active),
        "drafts": sum(1 for imp in imports if imp["status"] == "draft"),
        # Margen promedio de TODOS mis importados (activos y borradores) a mi precio de venta: es la
        # calidad de mi selección. Ganancia por venta y ganancia estimada usan solo los activos.
        "avg_margin": round(sum(margins) / len(margins), 1) if margins else None,
        "avg_margin_scope": "all_imports",
        "potential_profit_per_sale": round(sum(imp["profit"] for imp in active), 2),
        "estimated_monthly_profit": round(monthly, 2),
        "unread_alerts": unread,
        "out_of_stock": sum(1 for imp in imports if imp["stock"] == 0),
        "negative_margin": sum(1 for imp in imports if imp["profit"] < 0),
        "saved_searches": len(new_counts),
        "new_in_saved_searches": sum(new_counts.values()),
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


# ---------------------------------------------------------------- precálculo de rankings en segundo plano

WARM_DELAY_S = 1.0           # espera tras un cambio del catálogo (deja terminar al escritor y al historial)
_warm = {"enabled": False, "thread": None}
_warm_event = threading.Event()


def warm_rankings() -> None:
    """Calcula y deja en caché los rankings globales que usa "Para ti" (las mismas claves que _ranking)."""
    for rtype in ("rising", "top"):
        _ranking(rtype)


def _warm_loop() -> None:
    while True:
        _warm_event.wait()
        _warm_event.clear()
        time.sleep(WARM_DELAY_S)
        try:
            warm_rankings()
        except Exception:  # noqa: BLE001 — nunca debe tumbar el servidor
            log.exception("No se pudieron precalcular los rankings del panel")


def request_warmup() -> None:
    """Despierta el hilo (daemon) que vuelve a llenar la caché de rankings. Solo actúa con el servidor
    corriendo (ver _autostart): en las pruebas no se crean hilos."""
    if not _warm["enabled"]:
        return
    t = _warm["thread"]
    if t is None or not t.is_alive():
        t = _warm["thread"] = threading.Thread(target=_warm_loop, name="dashboard-warmup", daemon=True)
        t.start()
    _warm_event.set()


@hooks.on_catalog_write
def _on_catalog_write(conn, ids) -> None:
    """Tras cargas de catálogo y el monitor la caché de rankings se invalida: se recalcula en segundo plano
    para que el primer panel después no pague ese cálculo (solo despierta al hilo; no usa `conn`)."""
    request_warmup()


def _autostart() -> None:
    main = sys.modules.get("__main__")
    if os.path.basename(getattr(main, "__file__", "") or "") == "server.py":
        global ASYNC_RANKINGS
        ASYNC_RANKINGS = True
        _warm["enabled"] = True
        request_warmup()


ROUTES: list = [
    ("GET", r"/api/dashboard", lambda m, qs, b: dashboard(qs)),
    ("POST", r"/api/dashboard/onboarding/([a-z_]+)/dismiss", lambda m, qs, b: dismiss(m[1])),
]

_autostart()
