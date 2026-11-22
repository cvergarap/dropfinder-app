# Historial, tendencias y comparación de proveedores (Sprint 2)

Código: `backend/history.py`, `frontend/js/history.js`, `frontend/css/history.css`, pruebas en `tests/test_history.py`.

## Datos
- **`product_snapshots`** (tabla del núcleo, v4): una fila por producto y día. `orders` = pedidos acumulados;
  `orders_day` = pedidos desde la fila anterior (sin fila anterior: `orders_7d / 7`); costo, envío y stock.
- Se escribe desde el hook `hooks.on_catalog_write` (cargas de catálogo y monitor), en la misma transacción.
- **Compactación**: diario para los últimos 120 días; antes, solo los lunes. La fila que queda absorbe los
  pedidos de las borradas, así que los acumulados siempre cuadran. Es incremental (una vez por día).
- Tablas propias: `history_meta` (estado interno) y `history_seasonality` (meses pico por producto,
  `ON DELETE CASCADE`).
- Bases anteriores a este módulo: la primera consulta del historial genera el de la demo y la foto de hoy del
  resto (`ensure_backfill`).

## Historial sintético de la demo
Los productos de demostración no tienen pasado real: se genera uno determinista de 730 días (semilla = id),
coherente con el producto (las dos últimas semanas suman `orders_prev_7d` y `orders_7d`; el acumulado termina
en `orders`). Formas: estable, en ascenso, en caída, nuevo y picos anuales por categoría (Bebés y niños y Hogar
en diciembre, Fitness en enero, Moda y accesorios en mayo por el Día de la Madre, Electrónica en
noviembre/diciembre). Lo que no cabe en los 730 días queda como acumulado previo al seguimiento.
No se genera si la demo tiene más de 20.000 productos (el bench de 100k solo guarda la foto del día).

## Definiciones
Pedidos de un período = `acum(fin) − acum(inicio)`; la fecha de referencia es la del último snapshot.

| Ranking | Regla |
|---|---|
| Más vendidos | más pedidos en la ventana |
| En ascenso | mayor % de crecimiento frente a la ventana anterior; ≥ 10 pedidos en cada una e historial que la cubra |
| Nuevos en tendencia | ≤ 25 % (o ≤ 3) de los pedidos de la ventana en los 30 días previos; ≥ 10 pedidos ahora |
| En caída | mayor % de caída; ≥ 20 pedidos en la ventana anterior |

**Estacionalidad**: últimos 24 meses en dos años; un mes es pico si en ambos años su promedio diario es
≥ 1,8 × el del año y ≥ 1,8 × la tendencia del año (para no confundir crecimiento con estacionalidad), con
≥ 60 pedidos por año. Fuerza = el menor de los dos cocientes.

**Confiabilidad del proveedor** (0–100) = 100 × (0,30·envío + 0,25·stock + 0,25·rating + 0,20·estabilidad):
envío = (25 − días)/23, stock = 1 − % sin stock, rating = (rating − 3)/2, estabilidad = 1 − % de productos con
cambio de costo en 30 días (sin historial: 0,5).

## API
- `GET /api/products/{id}/history?days=30|90|365` → `product_id, days, resolution (day|week), until, first_date,
  points[{date, orders, stock, cost, total_cost}], summary{orders_period, orders_prev_period, change_pct, peak_date},
  seasonality{peak_month, strength, label} | null`.
- `GET /api/rankings?type=top|rising|new|falling&days=7|30|90&category=&warehouse=&source=&supplier=&limit=20`
  → `type, days, until, took_ms, items[{id, title, category, supplier, warehouse, image, source, score, total_cost,
  suggested_price, margin, orders, stock, period_orders, prev_period_orders, change_pct, sparkline[14]}]`.
- `GET /api/seasonal?month=1..12&limit=12` (por defecto, el mes siguiente) → `month, month_name, label, total, items[…, strength, peak_month, label]`.
- `GET /api/suppliers/stats` → `until, formula, items[{supplier, source, products, avg_shipping_days,
  pct_out_of_stock, avg_rating, avg_margin, price_changes_30d, tracked_products, reliability}]`.
