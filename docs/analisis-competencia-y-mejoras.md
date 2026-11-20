# DropFinder — Análisis de la app, competencia y propuesta de mejoras

> Continuación del documento RAE (`RAE-AutoDS.md`). Fecha: noviembre 2026.
> Los precios y funcionalidades de la competencia vienen de sitios de comparación y páginas de los propios
> proveedores (muchas con enlaces de afiliados). Son orientativos: verificar en cada sitio antes de decidir.

---

## 1. Diagnóstico de la app actual

### 1.1 Mediciones (motor de búsqueda actual)

| Catálogo | Consulta | Tiempo | Observación |
|---|---|---|---|
| 1.500 productos | sin filtro | 56 ms | OK |
| 50.000 productos | sin filtro | **2.265 ms** | Inaceptable (objetivo < 200 ms) |
| 50.000 productos | `q=cargador` | 299 ms | Lento |
| 50.000 productos | margen ≥ 40 % | 742 ms | Lento: el margen se calcula en Python fila por fila |
| — | `lamparas` (plural) | **0 resultados** | `lampara` da 1.986. No entiende plurales |
| — | `cargdor` (error de tipeo) | **0 resultados** | No tolera errores |

**Causas:**
1. Cada búsqueda lee **todo** el catálogo y calcula precio, margen y score en Python antes de filtrar y ordenar.
2. Busca con `LIKE '%texto%'`, que no usa índices, no ordena por relevancia y solo encuentra coincidencias exactas.
3. No hay autocompletado, sinónimos ("audífonos" = "auriculares"), plurales ni tolerancia a errores.

### 1.2 Otras brechas

| Área | Situación actual | Impacto |
|---|---|---|
| Usuarios | No hay cuentas: todos comparten productos, reglas y alertas | Impide el panel de usuario, los planes y el multi‑tienda |
| Tendencias | Solo dos números (pedidos de esta semana y de la anterior), sin historial | No se puede graficar ni rankear "lo más vendido del mes" |
| Datos de ventas | Vienen del proveedor (demo o CSV) | "Lo más vendido" depende de la calidad de esa fuente (ver §4.4) |
| Moneda | Solo USD | El vendedor colombiano no ve su precio real en COP |
| Tiendas | No publica en ninguna tienda | Falta el núcleo de valor de AutoDS: importar → publicar → sincronizar |
| Comparación | No existe | No se puede decidir entre productos parecidos ni entre proveedores |
| Seguridad | Sin autenticación (aceptable solo en local) | Obligatoria antes de exponer la app en internet |

---

## 2. Competencia

### 2.1 Panorama global

| Herramienta | Enfoque | Fuerte en | Precio de entrada aprox. (USD/mes) |
|---|---|---|---|
| **AutoDS** | Todo en uno: buscar, importar, monitorear, despachar pedidos | Automatización, muchos proveedores y tiendas | ~19,90 |
| **Zendrop** | Proveedores curados, marca propia, almacenes en EE. UU. | Fulfillment, empaques con marca | ~49 |
| **DSers** | Socio oficial de AliExpress (sucesor de Oberlo) | Pedidos masivos a AliExpress, plan gratis | 0 – 39,90 |
| **Spocket** | Proveedores de EE. UU. y Europa | Envíos rápidos, calidad | ~40 |
| **Sell The Trend** | Investigación + automatización de tienda | Descubrimiento con IA, análisis de tiendas | ~30 – 50 |
| **Minea** | Espionaje de anuncios (Facebook, TikTok, Pinterest) | Señales de demanda reales (anuncios) | ~49 |
| **Kalodata / FastMoss** | Analítica de TikTok Shop | Ventas reales por producto y creador | ~45 – 49 |

### 2.2 Competencia en Latinoamérica: **Dropi** (Colombia, Chile, México)

Es el competidor más relevante para el mercado colombiano:
- Integraciones con **Shopify** (la mayoría de sus tiendas en Colombia) y **Tiendanube**. Hay un plugin para **WooCommerce**, pero con reseñas negativas sobre su estabilidad.
- Importa nombre, precio, **precio sugerido**, descripción, imágenes, variantes y **sincroniza stock**.
- **Logística local**: transportadoras colombianas (Coordinadora, Inter Rapidísimo, TCC, Envía, 99 Minutos…), número de guía sincronizado con el pedido y venta **contraentrega**.
- También ofrece AliExpress y CJ "by Dropi".

### 2.3 Lecciones para DropFinder

1. **Las herramientas de investigación ganan con señales de demanda reales** (anuncios, ventas en TikTok Shop), no con el catálogo del proveedor. Es la diferencia entre Minea o Kalodata y un simple buscador.
2. **El valor que retiene al cliente es la sincronización con la tienda** (precio, stock y pedidos), no la búsqueda. Sin eso, el usuario busca una vez y se va.
3. **En Colombia el modelo es local**: precios en COP, contraentrega, devoluciones y transportadoras. Ninguna herramienta global calcula bien la rentabilidad de un pedido contraentrega. **Ese es el espacio para diferenciarse** frente a AutoDS y a Dropi.

---

## 3. Propuesta de mejoras

Prioridad: 🔴 alta · 🟠 media · 🟢 baja. Esfuerzo estimado para una persona: S = 1–3 días · M = 1–2 semanas · L = 3+ semanas.

### 3.1 🔴 Búsqueda más rápida y efectiva — Esfuerzo M

**Etapa 1 (sin cambiar de tecnología):**
- **Precalcular** precio sugerido, margen, ganancia, tendencia y score como columnas indexadas. Se recalculan al cambiar las reglas de precio o al llegar datos nuevos. Así filtrar y ordenar ocurre en SQL, con índice.
- **Índice de texto completo (SQLite FTS5**, ya disponible): ranking por relevancia (BM25), prefijos (`carg*` encuentra "cargador") y sin tildes.
- **Plurales en español**: normalizar las palabras a su raíz ("lámparas" → "lampar"), tanto en el índice como en la búsqueda.
- **Sinónimos editables**: audífonos ↔ auriculares, celular ↔ móvil ↔ teléfono, perro ↔ mascota…
- **Tolerancia a errores**: si no hay resultados, sugerir "¿Quisiste decir *cargador*?" usando el vocabulario del catálogo.
- **Autocompletado** mientras se escribe (títulos, categorías y búsquedas populares).
- **Paginación por cursor** y caché de facetas.
- Meta: **< 150 ms con 100.000 productos**.

**Etapa 2 (con millones de productos o varios usuarios):** migrar a **PostgreSQL** + **Meilisearch** o **OpenSearch**, que traen tolerancia a errores, análisis en español y facetas nativas.

**Búsqueda por imagen (🟢, L):** subir una foto y encontrar productos parecidos. Sirve para detectar el mismo producto en varios proveedores.

### 3.2 🔴 Moneda local (USD / COP) — Esfuerzo S

- Selector de moneda en la barra superior. El catálogo se guarda en USD y se convierte al mostrar.
- **Tasa**: la **TRM oficial** del portal de datos abiertos de Colombia (`datos.gov.co`, dataset `32sa-8pi3`), actualizada a diario y guardada en una tabla `exchange_rates(fecha, moneda, tasa)` como historial. El usuario puede **fijar una tasa manual** o un **margen de cobertura cambiaria** (por ejemplo TRM + 3 %), porque los proveedores cobran en USD con la tasa de su tarjeta.
- **Redondeo comercial en COP**: precios terminados en 900 (por ejemplo $89.900), configurable.
- **Reglas de precio por moneda**: el multiplicador sirve en USD, pero en COP el vendedor suele pensar en "precio de venta objetivo".
- Opcional: **IVA** en el precio al consumidor (casilla con el porcentaje). Validar con un contador según el régimen del vendedor.
- Diseño preparado para sumar MXN, CLP, PEN y ARS sin cambiar el modelo.

### 3.3 🔴 Rentabilidad real para Colombia (diferenciador) — Esfuerzo M

Calculadora por producto que incluya:
- Costo del producto + flete nacional según la ciudad de destino.
- Comisión de la pasarela de pago (Wompi, Mercado Pago, PayU…) o el **recaudo contraentrega**.
- **% de devolución contraentrega** (pedidos que el cliente no recibe y que igual pagan flete de ida y vuelta).
- **Costo por adquisición publicitaria (CPA)** estimado.
- Resultado: **ganancia neta por pedido entregado**, **ROAS de equilibrio** (cuánto vender por cada peso de anuncios) y **CPA máximo** tolerable.

Ninguna herramienta global lo hace bien, y es lo que define si un producto sirve en Colombia.

### 3.4 🔴 Panel de usuario — Esfuerzo M (requiere cuentas)

**Base:** registro e inicio de sesión (correo + contraseña o Google), contraseñas con hash, sesiones seguras y datos separados por usuario (productos, reglas, alertas, tiendas, moneda).

**Panel principal (dashboard):**
- Indicadores: productos importados / publicados, margen promedio, **ganancia estimada del mes**, alertas sin resolver y productos sin stock.
- Gráficos: evolución de costos de mis productos, alertas por tipo y ventas por tienda (cuando haya integración).
- "Para ti": productos en tendencia de las categorías que el usuario más importa.
- Lista de primeros pasos: elegir moneda → definir reglas de precio → conectar tienda → importar el primer producto.

**Otras secciones:** mis tiendas conectadas, búsquedas guardadas, colecciones, uso del plan y configuración.

### 3.5 🟠 Tendencias y "lo más vendido" — Esfuerzo M

- **Historial diario** (`product_snapshots`: producto, fecha, pedidos, precio, stock). Es la base de todo lo demás.
- **Gráfico de pedidos y precio** en el detalle de cada producto (30, 90 y 365 días).
- **Rankings**: más vendidos (7, 30 y 90 días), **en ascenso** (mayor aceleración), **nuevos en tendencia** (sin ventas hace 30 días y con fuerte subida ahora) y **en caída** (para evitarlos).
- Rankings por categoría y por país del almacén ("lo más vendido con envío desde Colombia").
- **Estacionalidad**: marcar productos que suben cada año en la misma fecha (Navidad, Día de la Madre, regreso a clases).
- **Búsquedas guardadas con alerta**: "Avísame cuando aparezca en Mascotas un producto con margen > 45 % y envío ≤ 5 días".

### 3.6 🟠 Comparativas — Esfuerzo S/M

- **Comparador** de 2 a 4 productos lado a lado: costo, precio, margen, envío, rating, tendencia y gráfico superpuesto. El ganador de cada fila queda resaltado.
- **El mismo producto en varios proveedores**: agrupar productos equivalentes (por título y, más adelante, por imagen) y mostrar "más barato" y "más rápido". AutoDS y Dropi no lo muestran de forma clara.
- **Comparar proveedores**: tiempos reales de envío, % de productos sin stock y cambios de precio frecuentes (sale del historial del monitor).

### 3.7 🔴 Integración con tiendas — Esfuerzo L (la mayor pieza)

Arquitectura: un `StoreConnector`, igual que los `SupplierConnector` de proveedores.

```
StoreConnector
  connect(credenciales)        → valida y guarda las credenciales cifradas
  publish(producto)            → crea el producto en la tienda y devuelve su id externo
  update_price_stock(...)      → sincroniza cuando el monitor detecta cambios
  unpublish(id_externo)
  fetch_orders() / webhook     → pedidos nuevos, para despacharlos al proveedor (fase siguiente)

store_connections(id, user_id, plataforma, url_tienda, credenciales_cifradas, estado)
store_listings(imported_id, connection_id, external_id, estado, ultimo_sync, error)
```

| Plataforma | Autenticación | Crear producto | Notas |
|---|---|---|---|
| **Shopify** | App personalizada (token del admin) para empezar; OAuth para la tienda pública de apps | Admin **GraphQL** API (`productCreate`, `productVariantsBulkUpdate`, `inventorySetQuantities`) | La más usada por dropshippers colombianos. **Hacerla primero.** |
| **Tiendanube** | OAuth 2 (solo *authorization code*), app creada en el panel de partners, permiso `write_products` | `POST https://api.tiendanube.com/v1/{store_id}/products` con token Bearer y **`User-Agent` obligatorio** | Fuerte en Argentina, México y Colombia |
| **WooCommerce** | Consumer Key + Secret (WooCommerce → Ajustes → Avanzado → REST API), Basic Auth sobre **HTTPS** | `POST /wp-json/wc/v3/products` | Tiendas en hostings muy distintos: validar HTTPS y versión |
| **Mercado Libre** | OAuth 2 | API de publicaciones (`/items`) | Marketplace dominante en Latinoamérica; reglas de categorías estrictas |
| **VTEX / Jumpseller / Wix** | API keys / OAuth | APIs REST | Fase posterior, según la demanda |

**Flujo para el usuario:**
1. "Conectar tienda".
2. Elegir el producto en *Mis productos* y presionar "Publicar en…".
3. Desde ahí, cada cambio de costo o stock que detecta el monitor **actualiza la tienda sola** según reglas: subir el precio si baja el margen y **ocultar el producto si se agota**.

Este punto convierte DropFinder de "buscador" en "herramienta diaria".

### 3.8 🟢 Otras mejoras

| Mejora | Valor | Esfuerzo |
|---|---|---|
| Favoritos y **colecciones** ("Ideas Navidad", "Probar en marzo") | Organización | S |
| Exportar resultados o colecciones a CSV / Excel | Trabajo en equipo | S |
| Descripciones generadas con IA (en español, con tono de marca) al publicar | Ahorra tiempo | M |
| Conector **CJ Dropshipping** real (pendiente de la clave) | Datos reales | M |
| Conector con **proveedores colombianos** (CSV o API de mayoristas locales) | Envíos de 1 a 5 días, contraentrega | M |
| Señales externas: TikTok Creative Center, Google Trends | Demanda real | L |
| Monitor programado (cada X horas), sin botón | Automatización | S |
| Planes y límites (productos importados, tiendas, búsquedas guardadas) | Monetización | M |

---

## 4. Decisiones y riesgos

1. **Tecnología:** las mejoras de búsqueda de la etapa 1, la moneda, el comparador y las tendencias funcionan con la tecnología actual (Python + SQLite). **Las cuentas de usuario y las integraciones con tiendas** justifican migrar a un framework web (FastAPI o Django) con PostgreSQL y una cola de tareas para la sincronización. Conviene decidirlo **antes** del panel de usuario.
2. **Credenciales de tiendas:** cifradas en reposo y nunca enviadas al navegador. Usar los permisos mínimos (`write_products`, sin acceso a clientes, salvo para despachar pedidos).
3. **Tasas de cambio:** guardar la tasa usada en cada cálculo, para que un precio publicado sea explicable después.
4. **Calidad de "lo más vendido":** depende de la fuente. Hay tres opciones, en orden de confiabilidad:
   - (a) ventas reales de las tiendas conectadas de los usuarios, agregadas y anónimas: es un efecto de red y requiere consentimiento;
   - (b) los pedidos que informa el proveedor (CJ, CSV);
   - (c) señales externas (anuncios, TikTok).

   Al principio será (b); (a) es la ventaja de largo plazo.
5. **Legal:** el scraping queda descartado como fuente principal (ver RAE §2.4). Revisar términos de uso y protección de datos (Ley 1581 de 2012 en Colombia) antes de agregar datos de usuarios o de sus tiendas.

---

## 5. Hoja de ruta propuesta

| Sprint (2 semanas) | Entregable | Por qué en este orden |
|---|---|---|
| **1** | Búsqueda rápida (precálculo + FTS5 + plurales + sinónimos + "¿quisiste decir?") y **moneda USD/COP con TRM** | Lo que más se nota al usar la app; no depende de nadie |
| **2** | Historial diario + **tendencias y rankings** + **comparador** | Necesita la base de datos del sprint 1 |
| **3** | Decisión de stack + **cuentas de usuario + panel** | Requisito de las integraciones con tiendas |
| **4–5** | **Shopify** (publicar + sincronizar precio y stock) | La plataforma más usada |
| **6** | **Tiendanube** y **WooCommerce** | Reutilizan el `StoreConnector` de Shopify |
| **7** | **Calculadora de rentabilidad contraentrega** + conector de proveedor real (CJ o mayorista colombiano) | Diferenciación frente a AutoDS y Dropi |

---

## Fuentes
- Comparaciones de competencia: [ecommerce-platforms.com](https://ecommerce-platforms.com/?p=111716), [branvas.com](https://branvas.com/blogs/news/autods-alternatives), [teemdrop.com](https://teemdrop.com/blogs/autods-alternatives-in-2026), [spotsaas.com](https://www.spotsaas.com/compare/autods-vs-zendrop-vs-dsers), [Software Advice](https://www.softwareadvice.com/scm/autods-profile/alternatives/), [productlair.com](https://productlair.com/dropshipping-product-research-tool-pricing), [Sell The Trend vs Minea](https://www.sellthetrend.com/compare/sell-the-trend-vs-minea), [Capterra – Kalodata](https://www.capterra.com/p/10013207/Kalodata/)
- Dropi: [Tiendanube – app Dropi](https://www.tiendanube.com/co/tienda-aplicaciones-nube/dropi), [Ayuda Tiendanube – Dropi](https://ayuda.tiendanube.com/es_CO/dropshipping/como-integrar-dropi-mx-con-mi-tiendanube), [Plugin WooCommerce](https://ka.wordpress.org/plugins/wc-dropi-integration/), [StoreLeads – Dropi en Colombia](https://storeleads.app/reports/technology/Dropi/country/CO)
- APIs de tiendas: [Tiendanube – autenticación](https://tiendanube.github.io/api-documentation/authentication), [Tiendanube – API docs](https://github.com/TiendaNube/api-docs), [WooCommerce – autenticación REST](https://developer.woocommerce.com/docs/apis/rest-api/authentication/)
- TRM: [Superfinanciera – TRM](https://www.superfinanciera.gov.co/publicaciones/60819), [Banco de la República – consulta TRM](https://banrep.gov.co/es/como-consultar-datos-historicos-tasa-representativa-mercado-trm-nuevo-portal-estadisticas), [trm-api (dataset datos.gov.co)](https://app.unpkg.com/trm-api@1.5.10/files/README.md)
