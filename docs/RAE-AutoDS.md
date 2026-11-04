# RAE — AutoDS → DropFinder

Metodología: **R**everse engineering (ingeniería inversa) → **A**nálisis → **E**specificación.

> Fuentes: búsquedas web y reseñas de terceros (autods.com no fue accesible desde la red de esta sesión). Las cifras comerciales (precios, número de productos) son afirmaciones del proveedor o de terceros y deben verificarse.

---

## 1. Ingeniería inversa: ¿qué hace AutoDS?

AutoDS es una plataforma SaaS "todo en uno" para dropshipping. Su ciclo de valor es:

```
Encontrar producto → Evaluar rentabilidad → Importar a la tienda → Monitorear precio/stock → Cumplir pedidos automáticamente
```

### 1.1 Módulos observados

| Módulo | Qué hace | Observaciones |
|---|---|---|
| **Marketplace** | Catálogo masivo de productos de proveedores (AutoDS afirma >20 M). | Búsqueda por palabra clave + filtros. |
| **Smart Filter System** | Filtra por categoría, ubicación del almacén, rango de precio, proveedor, entre otros. | Terceros mencionan también plataforma de fulfillment y reacciones en Facebook. |
| **Winning / Trending Products** | Productos con mejores indicadores de rendimiento (ventas, tendencia). | Filtros por indicadores de desempeño. |
| **Hand‑picked products** | Productos curados por el equipo con análisis (audiencia objetivo, competencia). | >100 productos, 3–5 nuevos por día. |
| **TikTok / Ads spy** | Analítica de anuncios y productos virales en TikTok. | Fuente de "señales de demanda". |
| **Importador (1 clic)** | Envía productos a una página de **Drafts** para editarlos antes de publicar. | Aplica reglas de precio. |
| **Price & Stock Monitoring** | Revisa 24/7 precio e inventario del proveedor y actualiza el listing. | Evita vender a pérdida o sin stock. Calculadora de punto de equilibrio. |
| **Order Fulfillment** | Coloca automáticamente el pedido en el proveedor y devuelve el tracking al cliente. | |
| **Integraciones** | Proveedores: AliExpress, Amazon, CJ Dropshipping, Walmart, almacén propio, proveedores privados (20+ US/EU/CN). Tiendas: Shopify, eBay, Wix, Facebook Marketplace, etc. | |

### 1.2 Modelo de negocio
Suscripción mensual por tramos según cantidad de productos importados/monitoreados (p. ej. "Import 200", "Starter 500"), con trial de USD 1. Los precios reportados varían entre fuentes (≈ USD 9,90–29,90/mes en el plan inicial).

### 1.3 Arquitectura inferida

```
          ┌──────────────── Proveedores (APIs / scraping) ────────────────┐
          │  AliExpress   CJ   Amazon   Walmart   Almacén propio  ...    │
          └───────┬─────────────────────────────────────────────┬────────┘
                  │ ingesta periódica                           │ precio/stock
          ┌───────▼────────┐   indexación   ┌───────────────┐   │
          │ Catálogo (DB)  ├───────────────►│ Motor búsqueda│   │
          └───────┬────────┘                │ (Elastic/OS)  │   │
                  │ métricas/tendencias     └──────┬────────┘   │
          ┌───────▼────────┐                       │            │
          │ Scoring/Trends │◄── señales TikTok/FB  │            │
          └────────────────┘                       │            │
                                          ┌────────▼─────────┐  │
                         Usuario ───────► │  Web app + API   │  │
                                          └────────┬─────────┘  │
                                       importar    │            │
                                          ┌────────▼─────────┐  │
                                          │ Productos usuario├──┘ monitor (workers/colas)
                                          └────────┬─────────┘
                                                   │ publicar / sync / pedidos
                                          ┌────────▼─────────┐
                                          │ Tiendas: Shopify,│
                                          │ eBay, Wix ...    │
                                          └──────────────────┘
```

---

## 2. Análisis

### 2.1 Actores
- **Vendedor (dropshipper)**: busca, evalúa, importa y vende.
- **Proveedor**: origen de producto, precio, stock y envío.
- **Tienda**: canal donde se publica (Shopify, etc.).
- **Sistema de monitoreo**: proceso automático.

### 2.2 Problema central que resuelve el buscador
El vendedor necesita responder rápido: *"¿qué producto puedo vender con margen, que tenga demanda, envío aceptable y un proveedor confiable?"*. Por eso el valor está en:
1. **Datos normalizados** de muchos proveedores en un solo esquema.
2. **Métricas derivadas** (precio de venta sugerido, ganancia, margen, score "ganador").
3. **Filtros combinables** sobre esas métricas.

### 2.3 Métricas clave (definidas para el clon)
| Métrica | Fórmula |
|---|---|
| Costo total | `costo_producto + costo_envío` |
| Precio sugerido | `costo_total × multiplicador + fijo` (regla de precio configurable; por defecto ×2,5) |
| Comisión plataforma | `precio × % comisión` (por defecto 2,9 % + 0,30) |
| Ganancia | `precio − costo_total − comisión` |
| Margen % | `ganancia / precio × 100` |
| Punto de equilibrio | `(costo_total + 0,30) / (1 − 0,029)` |
| Score ganador (0–100) | Ponderación de pedidos (log), tendencia 7 días, rating, margen y velocidad de envío |

### 2.4 Acceso a datos de proveedores

| Fuente | Costo de la API | Condición |
|---|---|---|
| CJ Dropshipping | Gratis | Registro y API key; se paga solo al comprar productos |
| AliExpress Open Platform | Gratis | Requiere aprobación como desarrollador |
| Amazon PA‑API | Gratis | Requiere ser afiliado con ventas calificadas |
| APIs de terceros (RapidAPI, Apify…) | De pago | Datos ya extraídos de AliExpress/Amazon/TikTok |
| CSV / Excel de un mayorista | Gratis | El proveedor envía su catálogo |

**Scraping**: técnicamente posible, pero AliExpress y Amazon lo prohíben en sus términos de uso, tienen
protección anti‑bots (captchas, bloqueo de IPs) y los scrapers se rompen con cada cambio de diseño.
No se adopta como fuente principal. Casos aceptables: sitios que lo permiten (robots.txt y términos)
o catálogos que se publican a propósito, como `/products.json` de tiendas Shopify. La legalidad
depende del país y del uso; validar con el área legal.

### 2.5 Riesgos / decisiones
- **Acceso a datos**: AliExpress/Amazon restringen scraping; usar APIs oficiales (AliExpress Open Platform, CJ Dropshipping API, Amazon PA‑API) o feeds de proveedores privados. **Este es el principal riesgo legal y técnico.**
- **Escala de búsqueda**: SQLite basta para un MVP; para millones de productos se requiere Elasticsearch/OpenSearch o Postgres + `pg_trgm`.
- **Frescura**: el monitoreo requiere colas (Celery/BullMQ) y límites de tasa por proveedor.

---

## 3. Especificación

### 3.1 Alcance del MVP (implementado en este repo)
| ID | Requisito | Estado |
|---|---|---|
| RF‑01 | Buscar productos por palabra clave (título, etiquetas). | ✅ |
| RF‑02 | Filtrar por categoría, proveedor, país del almacén, rango de costo, margen mínimo, pedidos mínimos, rating mínimo, días máx. de envío, solo en tendencia, solo con stock. | ✅ |
| RF‑03 | Ordenar por score, pedidos, tendencia, margen, ganancia, precio, envío. | ✅ |
| RF‑04 | Calcular precio sugerido, ganancia, margen, punto de equilibrio y score por producto. | ✅ |
| RF‑05 | Regla de precio configurable (multiplicador y monto fijo) que recalcula métricas. | ✅ |
| RF‑06 | Ver detalle de producto. | ✅ |
| RF‑07 | Importar producto a "Mis productos" (drafts) con precio de venta editable. | ✅ |
| RF‑08 | Monitor de precio/stock: job que re‑consulta al proveedor y genera alertas (subida de costo, sin stock, margen negativo). | ✅ (proveedor simulado) |
| RF‑09 | Capa de conectores de proveedor intercambiable (`SupplierConnector`). | ✅ (mock + esqueleto CJ) |
| RF‑13 | Cargar catálogo de un proveedor desde CSV (alta, actualización, reemplazo) con alertas sobre productos importados. | ✅ |
| RF‑14 | Enlace a la ficha original del producto en el proveedor e imagen real (o ilustración local). | ✅ |
| RF‑15 | Identificar y eliminar productos de demostración; filtro por origen (demo / CSV / API). | ✅ |
| RF‑10 | Publicación en tiendas (Shopify) y fulfillment automático de pedidos. | ⏳ Fase 2 |
| RF‑11 | Señales de demanda externas (TikTok/Facebook ads). | ⏳ Fase 3 |
| RF‑12 | Autenticación multiusuario y planes. | ⏳ Fase 2 |

### 3.2 Modelo de datos
```
products(id, supplier, supplier_sku, source[mock|csv|api], title, category, tags, image,
         product_url, cost, shipping_cost, shipping_days, warehouse, stock,
         orders, orders_7d, orders_prev_7d, rating, reviews, updated_at)
         UNIQUE(supplier, supplier_sku)

imported_products(id, product_id → products.id, title, sell_price,
                  last_cost, last_stock, status[draft|active], created_at)

alerts(id, imported_id → imported_products.id, type, message, created_at, read)

settings(key, value)   -- multiplier, fixed_add, fee_pct, fee_fixed
```

### 3.3 API REST
| Método | Ruta | Descripción |
|---|---|---|
| GET | `/api/products?q=&category=&supplier=&warehouse=&min_cost=&max_cost=&min_margin=&min_orders=&min_rating=&max_ship_days=&trending=1&in_stock=1&sort=&page=&page_size=` | Búsqueda + filtros + métricas |
| GET | `/api/products/{id}` | Detalle |
| GET | `/api/facets` | Valores de filtros (categorías, proveedores, almacenes) |
| GET/PUT | `/api/settings` | Regla de precio |
| GET | `/api/imports` | Mis productos |
| POST | `/api/imports` `{product_id, sell_price?}` | Importar |
| PATCH | `/api/imports/{id}` `{sell_price?, status?, title?}` | Editar draft |
| DELETE | `/api/imports/{id}` | Quitar |
| POST | `/api/monitor/run` | Ejecuta el monitor de precio/stock |
| GET | `/api/alerts` | Alertas |
| GET | `/api/suppliers` | Proveedores cargados (origen, cantidad, importados) |
| POST | `/api/catalog/csv` `{supplier, csv, replace?}` | Cargar o actualizar catálogo CSV |
| DELETE | `/api/catalog?source=mock` · `?source=csv&supplier=` | Eliminar demo o catálogo de un proveedor (conserva importados) |

### 3.4 Arquitectura del MVP
- **Backend**: Python 3 (solo librería estándar) — `http.server` + `sqlite3`. Cero dependencias para que corra en cualquier lado.
- **Frontend**: HTML + JS vanilla (SPA ligera) servida por el mismo backend.
- **Datos**: generador de catálogo semilla (~1.500 productos sintéticos de 4 proveedores) mediante `MockSupplierConnector`.
- **Conectores**: interfaz `SupplierConnector` con `search()`, `get_product()`, `get_price_stock()`. Para producción se implementa por proveedor (CJ, AliExpress…).

### 3.5 Hoja de ruta
1. **Fase 1 (MVP)** — este repo.
2. **Fase 2** — Postgres + OpenSearch, autenticación, conector real CJ Dropshipping / AliExpress, publicación a Shopify (Admin API), scheduler de monitoreo (cron/Celery).
3. **Fase 3** — Fulfillment automático de pedidos (webhook `orders/create` de Shopify → pedido al proveedor → tracking), señales TikTok/Facebook, productos curados, planes de suscripción.
