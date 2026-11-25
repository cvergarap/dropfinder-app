# Calculadora de rentabilidad contraentrega

Pestaña **Rentabilidad** (y botón «Calcular contraentrega» en el detalle de un producto). Implementación:
`backend/profit.py` (función pura `calculate`), rutas `/api/profit/*` y `frontend/js/profit.js`.

> Todo es una **estimación** hecha con supuestos que cada usuario puede editar. No reemplaza la contabilidad real.

## Modelo

Se razona por **pedido confirmado** (el que sale a despacho). Con probabilidad `d` (efectividad de entrega) se entrega y
se cobra; con `r = 1 − d` (devoluciones / no recibidos) vuelve. Todos los montos están en **una** moneda (USD o COP).

| Símbolo | Significado |
|---|---|
| `P` | precio de venta · `C` costo del producto · `S` envío del proveedor |
| `F` | flete de entrega (ida, se paga en **todo** pedido despachado) · `R` flete de devolución · `E` empaque |
| `f%`, `f₀` | comisión de recaudo: % del precio + fija, **solo** en pedidos entregados |
| `L%` | % de producto devuelto que se pierde (no se puede revender) |
| `CPA` | publicidad por pedido confirmado; si se da `ROAS`, `CPA = P / ROAS` |

```
comisión            = P·f%/100 + f₀
utilidad_entrega    = P − C − S − E − F − comisión            (una entrega, antes de devoluciones y publicidad)
pérdida_devolución  = F + R + E + C·L%/100                    (una devolución: sin ingreso)
antes_de_publicidad = d·utilidad_entrega − r·pérdida_devolución
ganancia_confirmado = antes_de_publicidad − CPA
ganancia_entregado  = ganancia_confirmado / d                 (cada entrega absorbe devoluciones y publicidad; d = 0 → sin dato)
margen %            = ganancia_confirmado / (d·P) · 100       (sobre lo realmente cobrado; sin ingreso → sin dato)
CPA máximo          = max(antes_de_publicidad, 0)
ROAS de equilibrio  = P / CPA máximo                          (sin dato si CPA máximo = 0)
utilidad por 100    = 100 · ganancia_confirmado               (sin redondear antes de multiplicar)
```

Supuestos de modelado (documentados porque cambian el resultado):
- Costo del producto y envío del proveedor se incurren **solo en entregas**: el producto devuelto vuelve a tu inventario
  (salvo la parte `L%`, que sí se pierde).
- Flete de ida y empaque se pagan siempre; el de devolución solo en devoluciones.
- El ROAS se mide sobre el valor del pedido confirmado (`P`), como lo reportan las plataformas de anuncios.
- No se modelan cancelaciones antes del despacho ni impuestos.

## Redondeo y validación
- Cálculo con `Decimal`; se redondea **una sola vez**, al final, con «mitad hacia arriba»: COP sin decimales, USD 2,
  margen 1 decimal, ROAS 2. Las líneas del desglose se redondean cada una, así que pueden diferir de la suma en una unidad.
- Entradas: números JSON (no texto, no booleanos); se rechazan NaN, infinitos, negativos, porcentajes fuera de 0–100,
  ROAS fuera de 0,01–100 y montos sobre el tope (USD 100.000 / COP 400.000.000). Los valores se redondean a 4 decimales al validarlos.
  Campos desconocidos, CPA y ROAS a la vez, o devoluciones y efectividad que no sumen 100 → 400 con `code: "invalid_<campo>"`.
- Opcionales ausentes → supuestos del usuario (luego los de fábrica); quedan listados en `defaults_used` y en un aviso.

## Supuestos de fábrica (estimaciones típicas en Colombia)
| | COP | USD |
|---|---|---|
| Flete de entrega / devolución | 15.000 / 15.000 | 4,00 / 4,00 |
| Empaque | 1.500 | 0,40 |
| Comisión de recaudo | 3 % + 0 | 3 % + 0 |
| Devoluciones | 25 % | 25 % |
| Producto devuelto perdido | 0 % | 0 % |
| Umbral de aviso de devoluciones | 30 % | 30 % |

Cada usuario guarda los suyos **por moneda** (tabla `profit_defaults`, esquema v8); nadie ve ni usa los de otro.

## Avisos
El resultado trae `verdict` (`profitable`, `low_margin`, `loss`, `break_even`), calculado sin redondear.
`negative_margin`, `low_margin` (< 10 %), `high_returns` (devoluciones > umbral), `all_returned`, `zero_price`,
`no_ads` (sin CPA ni ROAS), `defaults_used`.

## API (todas requieren sesión; las escrituras, token CSRF)
| Ruta | Qué hace |
|---|---|
| `POST /api/profit/calc` | Calcula con el cuerpo `{price, product_cost, …, currency?}`; `currency` por defecto = la que el usuario ve |
| `GET /api/profit/defaults?currency=` | Supuestos efectivos, de fábrica y cuáles cambió el usuario |
| `PUT /api/profit/defaults` | `{"currency", "defaults": {clave: valor \| null}}`; `null` vuelve al de fábrica |
| `GET /api/products/{id}/profit?currency=` | Precio sugerido (redondeo comercial del usuario), costo y envío del producto convertidos, con sus supuestos |

## Ejemplo numérico (COP)
Precio 89.900, costo 30.000, fletes 15.000 + 15.000, empaque 1.500, recaudo 3 %, 25 % devoluciones, CPA 10.000.

- comisión = 2.697 → utilidad_entrega = 89.900 − 30.000 − 1.500 − 15.000 − 2.697 = **40.703**
- pérdida_devolución = 15.000 + 15.000 + 1.500 = **31.500**
- antes_de_publicidad = 0,75·40.703 − 0,25·31.500 = **22.652,25** (= CPA máximo)
- ganancia por pedido confirmado = 22.652,25 − 10.000 = **12.652** · por pedido entregado = 12.652,25 / 0,75 = **16.870**
- margen = 12.652,25 / (0,75·89.900) = **18,8 %** · ROAS de equilibrio = 89.900 / 22.652,25 = **3,97**
- utilidad por 100 pedidos = **1.265.225** (75 entregas)

## Pruebas
`tests/test_profit.py` (fórmulas a mano, bordes, entradas hostiles, API, migración v8), `tests/js/profit_helpers.test.js`
y `python3 tests/mutation_profit.py` (introduce 17 errores de fórmula en una copia y exige que las pruebas los detecten).
