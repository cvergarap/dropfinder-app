"""Métricas de rentabilidad y score "ganador"."""
from __future__ import annotations

import math

DEFAULT_SETTINGS = {
    "multiplier": 2.5,   # precio = costo_total * multiplier + fixed_add
    "fixed_add": 0.0,
    "fee_pct": 2.9,      # comisión pasarela/plataforma (%)
    "fee_fixed": 0.30,
}


def suggested_price(total_cost: float, s: dict) -> float:
    # Redondeo "psicológico" a .99
    raw = total_cost * s["multiplier"] + s["fixed_add"]
    return max(0.99, math.ceil(raw) - 0.01)


def profit_for(price: float, total_cost: float, s: dict) -> tuple[float, float]:
    fee = price * s["fee_pct"] / 100 + s["fee_fixed"]
    profit = price - total_cost - fee
    margin = profit / price * 100 if price > 0 else 0.0
    return round(profit, 2), round(margin, 1)


def break_even(total_cost: float, s: dict) -> float:
    return round((total_cost + s["fee_fixed"]) / (1 - s["fee_pct"] / 100), 2)


def trend_pct(orders_7d: int, orders_prev_7d: int) -> float:
    if orders_prev_7d == 0:
        return 100.0 if orders_7d > 0 else 0.0
    return round((orders_7d - orders_prev_7d) / orders_prev_7d * 100, 1)


def winning_score(orders: int, trend: float, rating: float, margin: float, shipping_days: int) -> int:
    """0-100. Ponderación: demanda 35, tendencia 25, rating 15, margen 15, envío 10."""
    demand = min(1.0, math.log10(orders + 1) / 5)                 # 100k pedidos ≈ 1
    tr = min(1.0, max(0.0, (trend + 50) / 150))                   # -50% → 0, +100% → 1
    rt = min(1.0, max(0.0, (rating - 3.0) / 2.0))
    mg = min(1.0, max(0.0, margin / 60))
    sh = min(1.0, max(0.0, (25 - shipping_days) / 23))
    return round(100 * (0.35 * demand + 0.25 * tr + 0.15 * rt + 0.15 * mg + 0.10 * sh))


def enrich(p: dict, s: dict) -> dict:
    total = round(p["cost"] + p["shipping_cost"], 2)
    price = suggested_price(total, s)
    profit, margin = profit_for(price, total, s)
    tr = trend_pct(p["orders_7d"], p["orders_prev_7d"])
    p = dict(p)
    p["tags"] = [t for t in (p.get("tags") or "").split(",") if t]
    p.update(
        total_cost=total,
        suggested_price=price,
        profit=profit,
        margin=margin,
        break_even=break_even(total, s),
        trend=tr,
        trending=tr >= 30 and p["orders_7d"] >= 20,
        score=winning_score(p["orders"], tr, p["rating"], margin, p["shipping_days"]),
    )
    return p
