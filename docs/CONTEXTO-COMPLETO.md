# DropFinder — Contexto completo para respaldo y replicación en otro entorno de IA

> Documento de respaldo y traspaso. Estado: tras el **Sprint 7** (rama `main`, noviembre 2026). Idioma del proyecto:
> **español** (interfaz, comentarios, commits y documentos).
> Repositorio: https://github.com/cvergarap/dropfinder-app · Copia local del usuario: `C:\Proyectos\Develops\Claude-IA\CV\dropfinder-app`.
>
> **Cómo usar este documento con otra IA:** pásale este archivo completo como contexto inicial, más `README.md`,
> `docs/BACKLOG.md` y los ADR de `docs/adr/`. Las secciones 14 y 15 traen un *prompt* de arranque y un plan de reconstrucción.
> Todo lo marcado **SIN VERIFICAR** se escribió de memoria contra documentación pública y no se probó contra servicios reales.

---

## 1. Qué es el proyecto y cómo nació

**DropFinder** es una réplica funcional (MVP ampliado) de **AutoDS** (https://www.autods.com/), una plataforma web que busca y
filtra productos para *dropshipping*. Se construyó con la metodología **RAE = Ingeniería inversa → Análisis → Especificación**:

1. **Ingeniería inversa** del ciclo de valor de AutoDS: *encontrar producto → evaluar rentabilidad → importar a la tienda →
   monitorear precio/stock → cumplir pedidos*. (`docs/RAE-AutoDS.md`; las cifras comerciales de AutoDS son afirmaciones de
   terceros y no se verificaron: autods.com no era accesible desde la red de la sesión.)
2. **Análisis** de la competencia y de brechas (`docs/analisis-competencia-y-mejoras.md`).
3. **Especificación** del MVP y de un plan de 7 sprints.

Entregable elegido por el usuario: **análisis + MVP funcional**. Mercado objetivo: **Colombia / LatAm** (precios en USD y COP,
contraentrega, TRM oficial).

### Cronología (commits en `main`)
| Commit | Contenido |
|---|---|
| `e7c3caf` | MVP: buscador de productos, métricas, importar, monitor, conectores |
| `cc6d5df` | Catálogos por CSV, enlace al proveedor, imágenes locales |
| `187d216` | Análisis de competencia y propuesta de mejoras |
| `c40395b` | **Sprint 1**: búsqueda rápida (FTS5) y moneda USD/COP |
| `9f1e8f8` | **Sprint 2**: tendencias, rankings, comparador, búsquedas guardadas |
| `6ce8ee8` | **Sprint 3**: ADR de stack, cuentas de usuario, panel, colecciones, exportación |
| `425a058` | **Sprint 4-5**: publicar en Shopify y sincronizar precio y stock |
| `6d46dcd` | **Sprint 6**: Tiendanube y WooCommerce, cliente HTTP endurecido, propiedad por etiquetas |
| `b17dd6b` | **Sprint 7**: calculadora de rentabilidad contraentrega y conector CJ Dropshipping |
| `1bb2af7`, `af96239`, `eda86ff` | Backlog (B1 servicio público, BUG-001, BUG-002) |

---

## 2. Metodología de trabajo con la IA (importante para replicar)

Instrucciones del usuario (textuales en esencia):
- Todo en **LOOP con revisiones** al terminar cada sprint y antes de iniciar el siguiente.
- **Siempre subagentes y multihilo**, lo que mejor funcione para cada caso.
- Validaciones periódicas y una al final de cada sprint; si pasa, se sigue con el siguiente.
- **Un commit por sprint** con el resumen de las tareas, **las decisiones tomadas y el porqué**.
- **No pedir autorización** para lo ya planeado. Crear ramas por funcionalidad y **revisar los MR** (PR).
- Registrar hallazgos nuevos **solo en el backlog** (sin corregir) cuando el usuario lo pide así.

Flujo aplicado en cada sprint:
1. Rama base del sprint (`feature/spN-base`) + *worktrees* por funcionalidad.
2. **Subagentes en paralelo**, uno por funcionalidad, cada uno con pruebas y documentación.
3. **Revisor independiente** (agente distinto, solo lectura, que no recibe la conclusión del autor) por cada PR; cada hallazgo
   debe estar **reproducido** con una prueba real o se descarta.
4. Ronda de correcciones con **pruebas de regresión verificadas por mutación**: se rompe el código a propósito y la prueba nueva
   debe fallar. Si el mutante sobrevive, la prueba no discrimina y se reescribe.
5. **Verificación propia** del coordinador (suite completa + mutaciones propias) antes de fusionar (*squash*) a la rama base.
6. Validación final del sprint (suite, pruebas JS, E2E con navegador real) y *squash-merge* a `main` con commit-resumen
   (secciones: TAREAS / REVISIONES / VALIDACIÓN / DECISIONES Y POR QUÉ / PENDIENTE).
7. Limpieza de *worktrees*.

Lecciones prácticas:
- Los agentes se cortan por **límite de uso**; se dejan commits WIP y se reanudan con el mismo agente.
- Usar siempre un directorio nuevo (`mktemp -d`) para la E2E; no borrar con `rm -rf` datos de trabajo.
- Una mutación que "sobrevive" puede ser un patrón de texto que no coincidió: comprobar con `git diff --stat` que se aplicó.
- `.claude/settings.json` del repo contiene una *allowlist* de comandos (python3, node, git, gh api, curl local, etc.) para
  evitar pedir permisos en cada paso.

---

## 3. Arquitectura general

```
Navegador (SPA vanilla JS, CSP estricta)
        │  HTTP/JSON  (cookie de sesión + token CSRF)
        ▼
backend/server.py  (http.server de la librería estándar, hilos)
   ├─ tabla ROUTES  → módulos: search, compare, history, saved_searches, dashboard,
   │                  collections_mod, currency, auth, stores_mod, store_sync, cj_mod, profit
   ├─ clasificación de rutas: PUBLIC_ROUTES / USER_ROUTES / OWNER_ONLY
   ├─ cabeceras de seguridad, Host/Origin allowlist, límite de cuerpo por ruta
   └─ hooks.py (ganchos de escritura de catálogo dentro de SAVEPOINT)
        │
        ▼
SQLite (WAL, FTS5)  backend/dropfinder.db     ← esquema v8 con migraciones atómicas
        │
        ├─ connectors/        proveedores  (SupplierConnector): demo, CSV, CJ Dropshipping (API)
        └─ store_connectors/  tiendas      (StoreConnector): Shopify, Tiendanube, WooCommerce
                 └─ http.py   cliente HTTPS anti-SSRF compartido
        Hilos de fondo: historial, búsquedas guardadas, TRM, motor de sincronización con tiendas
```

**Decisión de stack (ADR 0001):** se mantiene **Python 3 stdlib + SQLite + JS vanilla** (cero dependencias) porque cumple las
metas medidas (búsqueda de 100.000 productos en 3–11 ms) y el usuario instala solo Python en Windows. Se migra a
FastAPI/Django + PostgreSQL + cola **si**: >5 usuarios simultáneos o servicio público, catálogos >1 M, colas persistentes
necesarias o requisitos de cumplimiento. (Ver backlog B1.)

Dimensiones actuales: ~14.000 líneas de Python en `backend/`, ~7.400 líneas de JS/CSS/HTML en `frontend/`, 924 funciones de
prueba (928 pruebas ejecutadas por `unittest`), 37 pruebas JS, 93 comprobaciones E2E.

---

## 4. Estructura del repositorio

```
dropfinder-app/
├─ README.md                     guía de uso, seguridad, despliegue tras proxy, conexión de tiendas
├─ .gitignore                    __pycache__, *.db, *.zip, *.key, *.db-wal/-shm/-journal, tests/e2e/work/
├─ .claude/settings.json         allowlist de comandos para el agente
├─ backend/
│  ├─ server.py                  servidor HTTP, rutas, seguridad, monitor, arranque (--port, --host)
│  ├─ db.py                      esquema (SCHEMA_VERSION=8), semilla, conexión, migraciones
│  ├─ auth.py / auth_context.py  cuentas, scrypt, sesiones, CSRF, límite de intentos, usuario actual (thread-local)
│  ├─ metrics.py                 precio sugerido, ganancia, margen, punto de equilibrio, tendencia, score
│  ├─ search.py                  FTS5, autocompletado, tolerancia a errores/plurales, facetas
│  ├─ currency.py                USD/COP, TRM (datos.gov.co), tasa manual, cobertura, redondeo, IVA
│  ├─ history.py                 historial de precios/pedidos, tendencias, rankings, estacionalidad (sintético)
│  ├─ compare.py                 comparador y alternativas entre proveedores
│  ├─ saved_searches.py          búsquedas guardadas con detección de productos nuevos
│  ├─ dashboard.py               panel de inicio, onboarding
│  ├─ collections_mod.py         colecciones, favoritos y exportación CSV
│  ├─ profit.py                  calculadora de rentabilidad contraentrega (función pura, Decimal)
│  ├─ stores_mod.py              API de tiendas (conexión, publicación, listados)
│  ├─ store_sync.py              motor de sincronización: cola persistente, reglas, reintentos
│  ├─ secrets_store.py           cifrado de credenciales (clave fuera del repo)
│  ├─ cj_mod.py                  API de CJ (conectar, importar en hilo, apoyo al monitor)
│  ├─ hooks.py, errors.py, bench.py
│  ├─ connectors/                base.py (SupplierConnector), mock.py (demo), csv_connector.py, cj.py
│  ├─ store_connectors/          base.py, http.py, shopify.py, tiendanube.py, woocommerce.py, fake.py, __init__.py
│  └─ data/                      sinonimos.json (sinónimos editables del autocompletado)
├─ frontend/
│  ├─ index.html                 SPA: barra superior con pestañas y vistas
│  ├─ app.js                     núcleo (estado, búsqueda, tarjetas, importados, alertas, ajustes, objeto DF de extensión)
│  ├─ styles.css                 tokens, layout base, tarjetas, tablas
│  ├─ plantilla-proveedor.csv    plantilla de catálogo
│  ├─ css/   auth, collections, compare, dashboard, history, profit, saved, stores, cj
│  └─ js/    auth, collections, compare, currency, dashboard, history, profit, saved, stores, suggest, cj
├─ docs/
│  ├─ RAE-AutoDS.md, analisis-competencia-y-mejoras.md, historial-tendencias.md
│  ├─ profit-calculator.md, stores-api.md, BACKLOG.md, CONTEXTO-COMPLETO.md (este archivo)
│  └─ adr/ 0001-stack, 0002-integracion-tiendas, 0003-tiendanube-woocommerce, 0004-proveedor-cj
└─ tests/
   ├─ test_*.py (23 archivos), fake_shopify.py / fake_tiendanube.py / fake_woocommerce.py / fake_cj.py
   ├─ js/   stores_helpers, profit_helpers, cj_helpers (.test.js, Node)
   └─ e2e/  launch.py, e2e.js, README.md  (Playwright)
```

---

## 5. Modelo de datos (SQLite, esquema v8)

Versionado con `PRAGMA user_version` y migraciones **atómicas** (si falla a mitad no queda a medias). Tablas principales:

| Grupo | Tablas |
|---|---|
| Catálogo | `products` (única `supplier + supplier_sku`; `source` = mock / csv / api; métricas precalculadas `total_cost, suggested_price, profit, margin, break_even, trend, trending, score`), `products_fts` (FTS5) + `products_fts_vocab`, `fts_dirty`, `product_snapshots` |
| Usuarios | `users`, `sessions` (token guardado solo como hash), `login_attempts`, `meta`, `settings` |
| Por usuario | `imported_products` (borradores/activos), `alerts`, `monitor_checks`, `saved_searches`, `saved_search_hits`, `saved_search_pending`, `search_log`, `collections` (+ favoritos), `collection_items`, `dashboard_prefs`, `user_currency_settings`, `profit_defaults` (por usuario **y moneda**) |
| Moneda | `exchange_rates`, `currency_settings` |
| Historial | `history_meta`, `history_season_state`, `history_seasonality`, `history_synth` |
| Tiendas | `store_connections` (credenciales cifradas), `store_listings` (producto publicado: `is_demo`, `hidden_by_rule`, `currency`), `store_jobs` (cola persistente con *leases*, índice único de trabajos activos), `store_sync_log` |
| Proveedores API | `supplier_connections` (CJ; creada de forma perezosa con `CREATE TABLE IF NOT EXISTS`, **sin** subir `user_version`) |

Propiedad de los datos: catálogo, reglas de precio, TRM, historial y la conexión de CJ son **de la instalación** (solo el
propietario los cambia); productos importados, alertas, búsquedas, colecciones, moneda de visualización y supuestos de rentabilidad
son **de cada usuario**.

---

## 6. Funcionalidades y fórmulas

### 6.1 Métricas de producto (`metrics.py`, única fuente de verdad)
- `costo_total = costo + envío`
- `precio_sugerido = max(0.99, ceil(costo_total × multiplicador + monto_fijo) − 0.01)` (redondeo a .99). Por defecto multiplicador 2,5, monto fijo 0.
- `comisión = precio × % + fijo` (por defecto 2,9 % + 0,30 USD)
- `ganancia = precio − costo_total − comisión`; `margen = ganancia / precio × 100`
- `punto de equilibrio = (costo_total + fijo) / (1 − %/100)`
- `tendencia % = (pedidos_7d − pedidos_7d_previos) / pedidos_7d_previos × 100`; **en tendencia** si tendencia ≥ 30 % y pedidos_7d ≥ 20.
- **Score ganador 0–100**: demanda 35 % (log10 de pedidos, 100 k ≈ 1) + tendencia 25 % (−50 % → 0, +100 % → 1) + rating 15 % (3→0, 5→1) +
  margen 15 % (60 % → 1) + envío 10 % (≤2 días → 1, ≥25 → 0). Cambiar la fórmula exige subir `FORMULA_VERSION` (recalcula al arrancar).

### 6.2 Búsqueda (Sprint 1)
FTS5 con relevancia, sin distinguir tildes ni mayúsculas, plurales y tolerancia a errores de tipeo, autocompletado con sinónimos
(`/api/suggest`), filtros (categoría, proveedor, almacén, costo, margen, pedidos, score, rating, días de envío, en tendencia, con stock),
ordenamientos y paginación. Medido antes: 50.000 productos tardaba 2.265 ms; objetivo <200 ms; ahora 3–11 ms con 100.000.

### 6.3 Moneda (Sprint 1)
Catálogo y reglas en **USD**; visualización en **USD o COP** por usuario. TRM oficial (datos.gov.co) o tasa manual, **cobertura cambiaria**
(TRM + %), redondeo de precios en COP (p. ej. «terminado en 900»), **IVA opcional** mostrado (19 % por defecto, a validar con contador).

### 6.4 Tendencias, rankings, comparador, búsquedas guardadas (Sprint 2)
Historial de precio/pedidos por producto (**datos sintéticos** mientras no haya reales; se desactivan con `DROPFINDER_SYNTH_HISTORY=0`),
rankings y estacionalidad, comparador de hasta N productos y de alternativas entre proveedores (`/api/products/{id}/alternatives`),
búsquedas guardadas con aviso de productos nuevos.

### 6.5 Cuentas, panel, colecciones (Sprint 3)
Roles **propietario** y **miembro**; primer propietario creado solo desde el mismo PC (o con código de instalación); panel con *onboarding*;
colecciones y favoritos; exportación CSV (`/api/export/{search|collection|imports}.csv`).

### 6.5b Calculadora de rentabilidad contraentrega (Sprint 7, `docs/profit-calculator.md`)
Razona por **pedido confirmado**, con `d` = efectividad de entrega y `r` = devoluciones (complementarias):
- `utilidad_entrega = P − C − S − E − F − (P·f%/100 + f₀)` (precio − costo − envío proveedor − empaque − flete de entrega − comisión de recaudo)
- `pérdida_devolución = F + R + E + C·L%/100` (flete de ida + flete de devolución + empaque + parte no revendible del producto)
- `antes_de_publicidad = d·utilidad_entrega − r·pérdida_devolución`; **CPA máximo** = `max(antes_de_publicidad, 0)`
- `ganancia_confirmado = antes_de_publicidad − CPA` (con ROAS objetivo, `CPA = P/ROAS`); `ganancia_entregado = ganancia_confirmado / d`
- `margen = ganancia_confirmado / (d·P)`; **ROAS de equilibrio** = `P / CPA máximo`; utilidad por 100 pedidos = `100 × ganancia_confirmado`
- Decimal con un solo redondeo final (COP sin decimales, USD 2). Entradas validadas (sin NaN/inf/negativos/% fuera de 0–100). Devuelve `verdict`
  (`profitable`, `low_margin`, `loss`, `break_even`) calculado **sin redondear** y avisos (`negative_margin`, `low_margin`, `high_returns`, …).
- Supuestos de fábrica (editables por usuario y moneda): flete 15.000/15.000 COP, empaque 1.500, recaudo 3 %, 25 % de devoluciones (aviso sobre 30 %).
- No modela impuestos/IVA, cancelaciones previas al despacho ni fletes por zona o peso.

---

## 7. Integraciones

### 7.1 Proveedores (`backend/connectors/`)
Interfaz `SupplierConnector` (`search`, `get_product`, `get_price_stock`).
- **Demo** (`mock.py`): 1.500 productos ficticios de 4 proveedores (AliExpress, Almacén Local, Amazon, CJ Dropshipping) marcados DEMO.
- **CSV** (`csv_connector.py`): encabezados en español o inglés, separador `,` o `;`, decimales con coma o punto; obligatorias `sku, titulo, costo`;
  opcionales `categoria, costo_envio, dias_envio, almacen, stock, pedidos, rating, imagen, url, etiquetas`; alta, actualización o reemplazo,
  con alertas sobre productos ya importados.
- **CJ Dropshipping (API v2.0)** (`cj.py`, `cj_mod.py`, ADR 0004): clave API **cifrada**, token con caché y refresco único ante 401,
  límite de tasa con *backoff* acotado y plazo total, importación de hasta **200** productos con progreso (hilo, una a la vez), monitor de
  precio/stock que consulta **antes** de tomar el bloqueo de escritura (tope 40 productos y 120 s por ejecución, orden por más antiguo),
  **stock desconocido ≠ cero**, envío no informado excluido de los «mejores» del comparador. Conexión **de la instalación**, solo propietario.
  **SIN VERIFICAR**: rutas (`/api2.0/v1/...`), campos, vigencias de token, límites, ruta de stock por producto, enlace de ficha.
- Acceso a datos de proveedores (análisis): CJ gratis con API key; AliExpress Open Platform y Amazon PA-API exigen aprobación; scraping de
  AliExpress/Amazon prohibido por términos y frágil → no es fuente principal.

### 7.2 Tiendas (`backend/store_connectors/`, ADR 0002 y 0003, `docs/stores-api.md`)
Contrato `StoreConnector`: `StoreProduct`, `ListingRef` (con `owner_tag`), `StoreInfo`, errores (`retryable`, `not_sent`), `idempotent_publish`, `scrub`.
- **Shopify** (GraphQL Admin; token o ID+secreto; `API_VERSION` configurable con `DROPFINDER_SHOPIFY_API_VERSION`, alternativa `2026-01`).
- **Tiendanube / Nuvemshop** (REST v1; ID numérico de la tienda + token + correo de contacto; cabecera `Authentication: bearer`; `stock_management: true` siempre).
- **WooCommerce** (REST v3; URL exacta HTTPS sin redirecciones, claves `ck_…`/`cs_…`; ayuda para error 401 por cabecera `Authorization` eliminada).
- Todo **SIN VERIFICAR** contra las APIs reales; probado solo con servidores simulados. Primera conexión: **tienda de desarrollo**.

**Reglas del motor (`store_sync.py`):**
- Se publica **siempre como borrador**; «Mostrar» exige confirmación explícita.
- Cola persistente con *leases*, enqueue idempotente, reintentos con *backoff*; un publicado ambiguo en conectores no idempotentes **no se reintenta** a ciegas.
- Sin stock en el proveedor → se **oculta** (marca `hidden_by_rule`); al volver el stock solo se re-muestra lo que ocultó la regla.
- Subir precio automáticamente es opcional, con tope por cambio y máximo 2× el precio publicado; **nunca baja precios solo**.
- Productos demo nunca se sincronizan solos con una tienda real.
- **Regla de propiedad:** todo producto publicado lleva las etiquetas `dropfinder` + `dfi-<id de instalación>`. Solo se **adopta o borra** un producto que lleve
  **ambas, con la `dfi-` exacta de esta instalación** (`tags_match`, `is_owned_by`). Un SKU `DF-<id>` igual **sin** esas etiquetas es **conflicto**
  (`StoreValidationError`): ni se adopta, ni se modifica, ni se borra, ni se crea otro. Los reintentos reutilizan las etiquetas del intento fallido.
  Límite conocido: si cambia `installation_id` tras publicar, esos listados no se pueden borrar desde DropFinder (falla segura).

**Cliente HTTP compartido anti-SSRF (`http.py`):** solo HTTPS/443, DNS resuelto una vez e IP fijada, redes bloqueadas explícitas + `is_global`,
se prueban todas las IP validadas, **sin redirecciones**, tope de tamaño (2 MB), `timeout` por operación y `total_timeout` con *watchdog*, mensaje
uniforme de «inalcanzable» (sin filtrar si el host existe).

**Secretos (`secrets_store.py`):** HMAC-SHA256 en modo CTR con MAC enmarcado por longitud y AAD por contexto; clave en
`%LOCALAPPDATA%\DropFinder\dropfinder.key` (Linux/macOS `~/.local/share/dropfinder/`), **fuera del repo y de la BD**, `*.key` ignorado por Git;
si hay credenciales cifradas y falta la clave, **no se regenera en silencio** (se informa). **Respaldar la clave aparte.**

### 7.3 MCP
No hay integración MCP en la aplicación. Está en el backlog (BUG-002) como tipo de integración de proveedor a diseñar.

---

## 8. Seguridad (resumen de lo implementado)
- Contraseñas `scrypt` (n=2¹⁴, r=8, p=1, sal 16 B, mínimo 10 caracteres, comparación en tiempo constante); correos en minúsculas (`casefold`).
- Sesiones: token aleatorio, guardado solo como hash, cookie `HttpOnly` + `SameSite=Lax` (`__Host-df_session` + `Secure` tras proxy HTTPS), caducidad 14 días sin uso; cambiar contraseña cierra las demás sesiones.
- **CSRF** en toda petición que modifica datos; validación de `Host` (lista permitida, 421 si no) y `Origin`/`Referer`.
- **Límite de intentos de login** (ventana 15 min): 5 por correo+IP, 20 por IP, 50 por correo (no aplica a IP conocida de los últimos 30 días); responde 429 con `Retry-After`.
- Cabeceras: `Content-Security-Policy` (`default-src 'self'; img-src 'self' data: https:; style-src 'self'; script-src 'self'; connect-src 'self'; font-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'`), `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`, `X-Frame-Options: DENY`, `Cross-Origin-Opener-Policy: same-origin`, HSTS tras proxy HTTPS. **Sin estilos ni manejadores inline** en el frontend.
- Clasificación explícita de rutas (`PUBLIC_ROUTES`, `USER_ROUTES`, `OWNER_ONLY`) con prueba `RouteClassificationTest`: una ruta nueva sin clasificar falla las pruebas.
- Límites de cuerpo por ruta; JSON con enteros gigantes → 400 (no 500).
- Modo desarrollo: `DROPFINDER_ENV=development DROPFINDER_DEV_USER=<id>` (solo local; el servidor se niega a arrancar si se combina con proxy, hosts no locales o `--host` no local). **Nunca con datos reales.**

---

## 9. Estructura, UX/UI y diseño de la web

### 9.1 Principios
SPA de una sola página, sin frameworks ni *build*: `index.html` + `app.js` + módulos `js/*.js` que se registran en un objeto de extensión `DF`.
Texto en español, sin dependencias externas (fuentes del sistema), accesible (etiquetas, `aria-live` en resultados, foco visible),
**responsive** (probado a 390 px sin desplazamiento horizontal) y **modo oscuro automático** con `prefers-color-scheme`.

### 9.2 Tokens de diseño (`frontend/styles.css`)
| Token | Claro | Oscuro |
|---|---|---|
| `--bg` | `#f6f7fb` | `#0b1020` |
| `--surface` | `#fff` | `#131a2e` |
| `--text` | `#0f172a` | `#e2e8f0` |
| `--muted` | `#64748b` | `#94a3b8` |
| `--border` | `#e2e8f0` | `#26304a` |
| `--primary` | `#4f46e5` (índigo) | `#818cf8` |
| `--primary-ink` | `#fff` | `#0b1020` |
| `--good` / `--bad` / `--warn` | `#15803d` / `#b91c1c` / `#b45309` | `#4ade80` / `#f87171` / `#fbbf24` |
| `--chip` | `#eef2ff` | `#1e2643` |
| `--radius` | 12 px | 12 px |

Tipografía: `system-ui, -apple-system, "Segoe UI", Roboto, sans-serif`, 14 px / 1,45. Marca: «Drop» en negrita 800 y «Finder» en color primario.

### 9.3 Componentes recurrentes
- **Barra superior fija** (`.topbar`): marca + pestañas (`.tab`, activa con fondo `--chip` y texto `--primary`) con *pills* de conteo (`.pill`, `.pill.warn` para alertas),
  a la derecha: tasa «1 USD = $ 3.592», selector de moneda (USD/COP) y menú de usuario (avatar con inicial).
- **Búsqueda** (`.searchbar`) con autocompletado; **filtros** laterales de 250 px (acordeones `details`, casillas con conteos, rangos, interruptores) pegajosos.
- **Cuadrícula de tarjetas** (`.grid`, columnas `minmax(220px, 1fr)`): imagen cuadrada, insignia de score (izquierda) y «en tendencia» (derecha, naranja),
  título, meta en chips, precio sugerido, ganancia y margen, acciones (importar, favorito, comparar).
- **Tablas** (`.table`) para importados, alertas, proveedores, listados de tiendas; **modales** con `dialog`; **avisos** (*toasts*) con `aria-live`.
- **Botones:** `primary` (índigo), `ghost` (borde), `link`; deshabilitado con opacidad .5.
- **Imágenes:** foto del proveedor (`imagen`) o ilustración local generada si falta o falla (sin depender de internet).
- Etiquetas de origen: «Demo (ficticio)», CSV, API; avisos ámbar «Sin verificar» en integraciones.

### 9.4 Mapa de pestañas (barra superior)
| Pestaña | Contenido |
|---|---|
| **Panel** | Resumen (productos, alertas, tendencias, atajos) y *onboarding* descartable |
| **Buscar productos** | Búsqueda + filtros + ordenamiento + tarjetas; detalle del producto (enlace «Ver en proveedor ↗», botón «Calcular contraentrega») |
| **Mis productos** (contador) | Importados (borradores/activos), precio de venta editable, «Publicar en tienda», exportación CSV |
| **Alertas** (contador ámbar) | Subida/bajada de costo, sin stock, venta a pérdida; «Ejecutar monitor» |
| **Tendencias** | Rankings, estacionalidad, gráficos de historial |
| **Comparar** (contador) | Comparador y alternativas entre proveedores |
| **Búsquedas guardadas** | Búsquedas con aviso de productos nuevos |
| **Colecciones** (contador) | Colecciones y favoritos, exportación |
| **Tiendas** | Conectar Shopify/Tiendanube/WooCommerce (asistente con selector de plataforma), estado, publicados, cola y registro |
| **Rentabilidad** | Calculadora contraentrega en vivo, supuestos editables, guardar/restablecer |
| **Proveedores** | Catálogos cargados, panel «CJ Dropshipping (API)», carga de CSV, eliminar demostración |
| **Reglas de precio** | Multiplicador, monto fijo, comisión %, comisión fija (USD) + panel «Moneda» (visualización, TRM/manual, cobertura, redondeo COP, IVA) |

Menú del usuario: Mi cuenta, Usuarios (propietario), cerrar sesión. Primer arranque: pantalla «Crear cuenta de propietario».

### 9.5 Problemas de UX conocidos
BUG-001 (Reglas de precio sin paso siguiente tras guardar) y BUG-002 (Proveedores sin alta/edición genérica) — ver `docs/BACKLOG.md`.

---

## 10. Scripts, comandos y entorno

### 10.1 Ejecutar
```bash
cd backend && python3 server.py          # http://127.0.0.1:8000 ; opciones: --port 8000 (o env PORT), --host 127.0.0.1
```
Requisitos: **Python 3.10+**, sin dependencias. La base `backend/dropfinder.db` se crea y se siembra sola (1.500 productos demo); para empezar de cero se borra.

### 10.2 Variables de entorno
| Variable | Uso |
|---|---|
| `DROPFINDER_DB` | Ruta del archivo SQLite (pruebas y E2E) |
| `DROPFINDER_KEY_FILE` | Ruta de la clave de cifrado de credenciales |
| `DROPFINDER_ALLOWED_HOSTS` | Hosts permitidos en la cabecera `Host`, separados por coma |
| `DROPFINDER_TRUSTED_PROXY` | IP del proxy de confianza (acepta `X-Forwarded-Proto/For`) |
| `DROPFINDER_SETUP_TOKEN` | Código de instalación para crear el primer propietario fuera del PC |
| `DROPFINDER_ENV` + `DROPFINDER_DEV_USER` | Modo desarrollo sin sesión (solo local) |
| `DROPFINDER_SYNTH_HISTORY` | `0` desactiva el historial sintético (pruebas) |
| `DROPFINDER_SHOPIFY_API_VERSION` | Versión de la API de Shopify |
| `PORT` | Puerto por defecto |

### 10.3 Pruebas
```bash
DROPFINDER_SYNTH_HISTORY=0 python3 -W error::ResourceWarning -m unittest discover tests     # 928 pruebas (~95 s; 1 omitida)
for t in tests/js/*.test.js; do node $t; done                                                # 37 pruebas JS
```
- `tests/test_*.py` cubren API, auth, búsqueda, moneda, historial, comparador, búsquedas guardadas, colecciones, panel, tiendas (base, Shopify, Tiendanube,
  WooCommerce, motor de sincronización), rentabilidad y CJ; los conectores se prueban contra **servidores simulados** (`fake_*.py`, con `ALLOW_LOOPBACK`
  solo en pruebas) incluyendo respuestas hostiles, límites de tasa, TLS real con `openssl`, mutación y fugas de secretos.
- **Suites de mutación:** `tests/mutation_profit.py` (27 mutantes); la de WooCommerce se activa con `DF_MUTATION=1` (omitida por defecto).
- **E2E con navegador real** (Playwright + Chromium): `tests/e2e/launch.py` levanta el servidor real y los simuladores (puertos 8891 y 8892); `tests/e2e/e2e.js` recorre la interfaz.
```bash
cd tests/e2e && export E2E_DIR=$(mktemp -d) && mkdir -p "$E2E_DIR/e2e"
python3 launch.py > "$E2E_DIR/launch.log" 2>&1 &
node e2e.js        # Shopify 29/29, Tiendanube 32/32, WooCommerce 32/32 = 93
```
- `backend/bench.py`: medición de rendimiento de la búsqueda.

### 10.4 Git / GitHub
Ramas por sprint y por funcionalidad, PR hacia la rama base del sprint (squash), y PR del sprint hacia `main` (squash con commit-resumen).
`gh api` para crear y fusionar PR. Los commits terminan con los *trailers* de coautoría de la sesión. Los PR se revisan con un agente revisor independiente.

---

## 11. Despliegue (hoy y opciones, incluido Cloudflare)

### 11.1 Hoy (probado)
La app corre **local** en el PC Windows del usuario (`127.0.0.1:8000`). Para acceso remoto el README documenta un **proxy inverso con HTTPS** (Caddy o nginx) en el mismo equipo:
```
# Caddyfile
dropfinder.midominio.com { reverse_proxy 127.0.0.1:8000 }
```
```bash
DROPFINDER_ALLOWED_HOSTS=dropfinder.midominio.com DROPFINDER_TRUSTED_PROXY=127.0.0.1 python3 server.py
```
Reglas: no usar `--host 0.0.0.0` sin proxy; registrar al propietario **antes** de abrir el proxy; solo IPv4; usar `127.0.0.1` (no `localhost`) y no compartir el equipo con otras apps web (las cookies no distinguen puertos).

### 11.2 Cloudflare — **NO implementado ni probado**; análisis y recomendación
Restricción clave: la aplicación es un **proceso Python de larga duración con SQLite en disco, hilos de fondo y un archivo de clave local**. **No puede ejecutarse tal cual en Cloudflare Workers/Pages** (entorno de V8/JS o WASM, sin disco persistente ni hilos *daemon* ni `http.server`). Opciones:

| Opción | Qué es | Esfuerzo | Notas |
|---|---|---|---|
| **A. Cloudflare Tunnel (`cloudflared`) delante de la app que sigue corriendo en tu PC o un VPS** *(recomendada a corto plazo)* | El túnel publica `https://dropfinder.tudominio.com` hacia `127.0.0.1:8000` sin abrir puertos; Cloudflare aporta DNS, TLS, CDN y WAF | Bajo | Configurar `DROPFINDER_ALLOWED_HOSTS=dropfinder.tudominio.com` y `DROPFINDER_TRUSTED_PROXY=127.0.0.1` (el túnel conecta desde localhost). **A verificar:** qué cabeceras envía el túnel (`X-Forwarded-For`, `X-Forwarded-Proto`, `CF-Connecting-IP`); hoy la app solo lee `X-Forwarded-*`, y si falta alguna habría que adaptar `server.py` (si no, el límite de intentos vería una sola IP y la cookie no sería `Secure`) |
| **B. Cloudflare Access (Zero Trust) delante del túnel** | Capa de autenticación previa (correo/Google/OTP) antes de llegar a la app | Bajo | Útil para equipos pequeños: reemplaza la necesidad inmediata de verificación de correo propia; la app mantiene su login |
| **C. VPS (o contenedor) con la app + Caddy/nginx, y Cloudflare solo como DNS/WAF/CDN (proxy naranja)** | Despliegue clásico con disco persistente, copia de seguridad de `.db` y `.key` | Medio | Modo SSL «Full (strict)» en Cloudflare; restringir el origen a las IP de Cloudflare |
| **D. Cloudflare Containers** | Ejecutar el contenedor de la app en la red de Cloudflare | Medio–alto | **SIN VERIFICAR** (producto reciente; revisar disponibilidad, persistencia de disco y precios). SQLite local requiere volumen persistente |
| **E. Reescritura para Workers + D1** | Frontend estático en Cloudflare Pages; API en Workers (JS/TS o Python Workers) con **D1** (SQLite administrado), **Queues/Cron Triggers** para sincronización, **KV/Secrets** para credenciales | Alto | Sustituye `http.server`, hilos, FTS5 propio y cifrado local. **SIN VERIFICAR:** soporte de FTS5 y de transacciones/`PRAGMA user_version` en D1, límites de CPU y de tamaño de BD. Es el camino de la migración de backlog B1 |

Recomendación por etapa: **uso interno de un equipo → A (+B)**; **servicio público con registro abierto → C o E**, junto con PostgreSQL/D1, correo transaccional,
cola persistente y aislamiento por cliente (ver `docs/BACKLOG.md` B1 y la condición de revisión del ADR 0001).

Checklist de despliegue (cualquier opción): respaldar **`dropfinder.db` (+ `-wal`/`-shm` con la app detenida o con `sqlite3 .backup`) y `dropfinder.key` por separado**;
registrar al propietario antes de exponer el servicio; HTTPS siempre; revisar `DROPFINDER_ALLOWED_HOSTS`; no activar `DROPFINDER_DEV_USER`;
probar las tiendas primero con tiendas de desarrollo; supervisar el monitor programado (apagado por defecto).

---

## 12. Decisiones finales y por qué (consolidado)

| Decisión | Motivo |
|---|---|
| Stack stdlib + SQLite + JS vanilla (ADR 0001) | Cero instalación en Windows, rendimiento medido suficiente, 1–5 usuarios; migración documentada con condiciones de revisión |
| Catálogo y reglas compartidos por la instalación; datos de usuario separados | Equipo pequeño con un propietario que administra |
| Precios en USD, visualización en USD/COP con TRM, cobertura, redondeo e IVA | Proveedores cobran en USD; el vendedor opera en COP |
| Publicar siempre como **borrador**; mostrar con confirmación | Evitar publicar a clientes por error o productos demo |
| Nunca bajar precios automáticamente; subida opcional con tope y máximo 2× | Evitar ventas a pérdida o errores de precio |
| Ocultar por falta de stock y re-mostrar solo lo ocultado por la regla | Evitar sobreventa sin pisar decisiones del usuario |
| Propiedad por etiquetas `dropfinder` + `dfi-<id>`; SKU solo no prueba propiedad | Un SKU local `DF-<id>` puede coincidir con productos ajenos; nunca adoptar/borrar lo que no se creó |
| Borrado exige la `dfi-` **exacta** de esta instalación; reintentos reutilizan etiquetas | Hallazgo de revisión: evitar borrar productos de otra instalación y conflictos eternos |
| WooCommerce cuenta solo el **nombre** de la etiqueta, no el slug | Evitar que etiquetas del cliente con ese slug pasen como propias |
| Credenciales cifradas con clave fuera del repo y de la BD; no regenerar en silencio | Evitar fugas y pérdida silenciosa de acceso |
| Cliente HTTP compartido anti-SSRF, sin redirecciones, con plazo total | Los conectores hablan con hosts externos y datos hostiles |
| Reintentar solo lecturas / operaciones idempotentes; publicaciones ambiguas no se repiten a ciegas | Evitar productos duplicados |
| Calculadora COD: costo y comisión de recaudo solo en entregas; flete de ida y empaque siempre | Modelo económico de contraentrega con devoluciones |
| CJ como conexión de la instalación, solo propietario; sin subir `user_version` (tabla perezosa) | Catálogo compartido; evitar choque de migraciones entre ramas paralelas |
| Stock desconocido **nunca** es cero; envío no informado se excluye de los «mejores» | Evitar alertas falsas y rankings distorsionados |
| El monitor consulta a CJ **antes** de bloquear escrituras | El límite de tasa de CJ no debe paralizar la app |
| No se hace *scraping* de AliExpress/Amazon como fuente principal | Términos de uso, bloqueos y fragilidad |
| Todo conector externo **SIN VERIFICAR** hasta probar con credenciales reales | El entorno de desarrollo no tiene red |

### Errores notables corregidos en revisión (para no repetirlos)
Clave de cifrado dentro del repo; MAC ambiguo; publicación duplicada con BD ocupada; cadenas de subida de precio; borrador mostrado como «Oculto» con un clic;
Tiendanube sin `stock_management` (sobreventa); lentitud *slow-drip* en el cliente HTTP; adopción por SKU sin prueba de propiedad; `validate()` de CJ que destruía
un token útil; stock desconocido guardado como 0; *starvation* del monitor con >40 productos; paginación truncada sin aviso; entero JSON >4300 dígitos → 500
en todo el servidor; `parseNumber("0,500")` = 500 (error 1000×); veredicto de rentabilidad calculado con valores redondeados.

---

## 13. Pendientes y backlog (resumen de `docs/BACKLOG.md`)
- **BUG-001** Reglas de precio: sin confirmación clara, «Volver/Cancelar» ni paso siguiente tras guardar; dos formularios independientes; mezcla USD/COP.
- **BUG-002** Proveedores: no hay alta/edición genérica de proveedores (API, MCP, archivo); AliExpress/Amazon son solo demo.
- **B1** Servicio público: registro abierto, **correo transaccional** (verificación y recuperación de contraseña), avisos por correo, protección contra abuso, planes y facturación,
  PostgreSQL y servidor de producción, aislamiento por cliente, aspectos legales (Ley 1581 de 2012).
- **B2** Verificar conectores (Shopify, Tiendanube, WooCommerce, CJ) con credenciales reales; calcular el flete de CJ al importar.
- **B3** Deuda técnica: prueba intermitente `MigrationV8Test` («database is locked» una vez); E2E de Rentabilidad y CJ; persistir `orphan_external_id`; limpiar ramas viejas.
- **B4** Producto: cumplimiento automático de pedidos, señales de TikTok/Facebook, IVA/cancelaciones/fletes por zona, más proveedores y tiendas.
- Hoy **no hay** servicio de correo, verificación de correo ni «olvidé mi contraseña» (correcto para uso interno).

---

## 14. Prompt de arranque para otra IA

> Eres un ingeniero senior. Vas a continuar/replicar **DropFinder**, una réplica de AutoDS para dropshipping orientada a Colombia/LatAm, siguiendo la metodología
> RAE (ingeniería inversa → análisis → especificación). Lee íntegramente `docs/CONTEXTO-COMPLETO.md`, `README.md`, `docs/BACKLOG.md` y `docs/adr/*`.
> Reglas: (1) idioma español en interfaz, comentarios y commits; (2) Python 3.10+ solo con la librería estándar, SQLite con FTS5 y JS vanilla con CSP estricta
> (sin estilos ni manejadores inline); (3) cada ruta nueva se clasifica en PUBLIC/USER/OWNER_ONLY; (4) migraciones de esquema atómicas con prueba;
> (5) trabajo por sprints con ramas, subagentes en paralelo, revisor independiente por PR, pruebas de regresión verificadas por mutación y un commit-resumen por sprint
> (TAREAS / REVISIONES / VALIDACIÓN / DECISIONES Y POR QUÉ / PENDIENTE); (6) lo no verificable contra servicios reales se marca **SIN VERIFICAR**;
> (7) nunca publicar a tiendas sin borrador y confirmación, nunca bajar precios solos, nunca adoptar ni borrar productos sin las etiquetas de propiedad de esta instalación;
> (8) no corrijas defectos que el usuario solo pidió registrar: anótalos en el backlog.

## 15. Plan de reconstrucción desde cero (orden recomendado)
1. Esquema y semilla demo (1.500 productos de 4 proveedores), `metrics.py`, API de productos y facetas, UI de búsqueda y tarjetas.
2. Importar a «Mis productos», reglas de precio, monitor de precio/stock y alertas; conectores `SupplierConnector` (demo, CSV).
3. Búsqueda FTS5 con tolerancia a errores y autocompletado; moneda USD/COP con TRM, cobertura, redondeo e IVA.
4. Historial/tendencias/rankings, comparador y búsquedas guardadas.
5. Seguridad y cuentas (scrypt, sesiones, CSRF, límite de intentos, Host/Origin), roles, panel, colecciones y exportación; ADR de stack.
6. Contrato `StoreConnector`, cliente HTTP anti-SSRF, almacén de secretos cifrados, motor de sincronización (cola, reglas, reintentos), Shopify; luego Tiendanube y WooCommerce con propiedad por etiquetas.
7. Calculadora de rentabilidad contraentrega y conector CJ Dropshipping.
8. E2E con navegador real y suites de mutación; documentación (README, ADR, `docs/`).
9. Resolver BUG-001 y BUG-002; después backlog B1 (servicio público) y despliegue (sección 11).

---

## 16. Cómo respaldar este proyecto
- **Código:** todo está en GitHub (`main`). Respaldo adicional: `git clone --mirror https://github.com/cvergarap/dropfinder-app.git` o un ZIP del repositorio.
- **Datos de la instalación:** `backend/dropfinder.db` (detener la app o usar `sqlite3 dropfinder.db ".backup copia.db"`) y, **por separado y en lugar seguro**,
  `%LOCALAPPDATA%\DropFinder\dropfinder.key` (sin ella no se pueden descifrar las credenciales de tiendas ni de CJ). Ninguno de los dos está en Git.
- **Conversación y decisiones:** este documento, `docs/adr/`, `docs/BACKLOG.md` y los mensajes de commit de cada sprint (secciones DECISIONES Y POR QUÉ).
