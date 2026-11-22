# Historial, tendencias y comparación de proveedores (Sprint 2)

Código: `backend/history.py`, `frontend/js/history.js`, `frontend/css/history.css`, pruebas en `tests/test_history.py`.

## Datos
- **`product_snapshots`** (tabla del núcleo, v4): una fila por producto y día. `orders` = pedidos acumulados;
  `orders_day` = pedidos desde la fila anterior (sin fila anterior: `orders_7d / 7`); costo, envío y stock.
- Se escribe desde el hook `hooks.on_catalog_write` (cargas de catálogo y monitor), en la misma transacción:
  solo la foto del día y la compactación (rápido: 100k productos en ~0,2 s).
- **Medición por producto**: cada producto se mide hasta su última foto (`last_observed`). Si no tiene fotos
  dentro de la ventana pedida, no entra en los rankings; en el gráfico, los días posteriores a su última foto
  van con `orders: null` ("Sin datos"), nunca como ventas en cero. `until` = última foto de toda la base.
- **Compactación**: diario para los últimos 120 días; antes, solo los lunes. La fila que queda absorbe los
  pedidos de las borradas, así que los acumulados siempre cuadran. Es incremental (una vez por día).
- Tablas propias: `history_meta` (estado interno), `history_seasonality` (meses pico por producto),
  `history_season_state` (hasta qué foto se calculó) e `history_synth` (estado de la demo sintética); todas
  con `ON DELETE CASCADE`.

## Escrituras en segundo plano (los GET no escriben)
- `run_maintenance()` hace lo pesado en un **hilo daemon** (`history-maintenance`): crea las tablas, foto
  inicial de bases anteriores al módulo, genera y extiende la demo, compacta y recalcula la estacionalidad
  atrasada. Trabaja en lotes de 150 productos, cada uno en su transacción con `busy_timeout` de 1,5 s y una
  pausa entre lotes; si otra escritura tiene la base bloqueada, lo deja para la siguiente vuelta.
- El hilo arranca solo al correr `python3 server.py` (1 s después de importar el módulo) y, si no, con la
  primera consulta del historial. Revisa cada 5 minutos (cambio de día) o cuando el hook lo despierta.
- Los GET usan una conexión `query_only`: responden siempre con lo que hay. Mientras la demo se genera,
  responden `"generating": true` y la interfaz muestra "Generando el historial…" y vuelve a consultar.
- Interruptores: `history.SYNTH_ENABLED` (o `DROPFINDER_SYNTH_HISTORY=0`) y `history.BACKGROUND` (las pruebas lo
  apagan y llaman `run_maintenance()`). `init_db` ya no genera nada: las semillas de las otras pruebas no
  pagan el historial sintético.

## Historial sintético de la demo
Los productos de demostración no tienen pasado real: se genera uno determinista de 730 días (semilla = id),
coherente con el producto (las dos últimas semanas suman `orders_prev_7d` y `orders_7d`; el acumulado termina
en `orders`). Formas: estable, en ascenso, en caída, nuevo y picos anuales por categoría (Bebés y niños y Hogar
en diciembre, Fitness en enero, Moda y accesorios en mayo por el Día de la Madre, Electrónica en
noviembre/diciembre). Lo que no cabe en los 730 días queda como acumulado previo al seguimiento.
No se genera si la demo tiene más de 20.000 productos (el bench de 100k solo guarda la foto del día).

**La demo sigue viva**: cada día el mantenimiento extiende de forma determinista (semilla = id + fecha) el
historial de cada producto demo hasta hoy, con el ritmo de su última semana, una tendencia suave según su
forma (acotada a ×0,3–×3) y su pico anual si lo tiene. El catálogo demo (`products`) no cambia: costo y stock
de las fotos nuevas son los actuales, así que la demo no simula cambios de precio después de generarse.

## Definiciones
Pedidos de un período = `acum(fin) − acum(inicio)`. Cada producto se mide en la ventana que termina en su
última foto y solo participan los observados dentro de la ventana global (`última foto > until − días`).

| Ranking | Regla |
|---|---|
| Más vendidos | más pedidos en la ventana |
| En ascenso | mayor % de crecimiento frente a la ventana anterior; ≥ 10 pedidos en cada una e historial que la cubra |
| Nuevos en tendencia | en la ventana anterior de **igual duración** ≤ 25 % (o ≤ 3) de los pedidos de la ventana; ≥ 10 pedidos ahora; historial que cubra la ventana anterior |
| En caída | mayor % de caída; ≥ 20 pedidos en la ventana anterior |

**Estacionalidad**: anclada en la última foto de cada producto; últimos 24 meses en dos años; solo cuentan
días observados (un hueco de más de 14 días entre fotos es "sin datos" y cada mes necesita ≥ 7 días
observados). Un mes es pico si en ambos años su promedio diario es ≥ 1,8 × el del año y ≥ 1,8 × la tendencia
del año (para no confundir crecimiento con estacionalidad), con ≥ 60 pedidos por año. Fuerza = el menor de
los dos cocientes. Se recalcula en segundo plano cuando la última foto avanzó ≥ 7 días.

**Confiabilidad del proveedor** (0–100) = 100 × (0,30·envío + 0,25·stock + 0,25·rating + 0,20·estabilidad):
envío = (25 − días)/23, stock = 1 − % sin stock, rating = (rating − 3)/2, estabilidad = 1 − % de productos
**seguidos** con cambio de costo en los 30 días previos a su última foto. Seguido = ≥ 2 fotos en esa ventana y
última foto en los últimos 30 días; para detectar el cambio se incluye la foto anterior a la ventana. Sin
productos seguidos, la estabilidad no cuenta y los demás pesos se reescalan (la falta de datos no premia ni
castiga).

## API
- `GET /api/products/{id}/history?days=30|90|365` → `product_id, days, resolution (day|week), until, first_date,
  last_observed, generating, points[{date, orders|null, stock, cost, total_cost}],
  summary{orders_period, orders_prev_period, change_pct, peak_date}, seasonality{peak_month, strength, label} | null`.
- `GET /api/rankings?type=top|rising|new|falling&days=7|30|90&category=&warehouse=&source=&supplier=&limit=20`
  → `type, days, until, generating, took_ms, items[{id, title, category, supplier, warehouse, image, source, score,
  total_cost, suggested_price, margin, orders, stock, period_orders, prev_period_orders, change_pct, last_observed,
  sparkline[14]}]`.
- `GET /api/seasonal?month=1..12&limit=12` (por defecto, el mes siguiente) → `month, month_name, label, total,
  generating, items[…, strength, peak_month, label]`.
- `GET /api/suppliers/stats` → `until, generating, formula, items[{supplier, source, products, avg_shipping_days,
  pct_out_of_stock, avg_rating, avg_margin, price_changes_30d, tracked_products, reliability}]`.
