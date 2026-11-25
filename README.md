# DropFinder

Réplica (MVP) del buscador y filtro de productos para dropshipping al estilo de AutoDS.
El análisis completo (metodología RAE: ingeniería inversa → análisis → especificación) está en
[`docs/RAE-AutoDS.md`](docs/RAE-AutoDS.md).

## Qué incluye
- **Búsqueda** por palabra clave, sin distinguir tildes ni mayúsculas.
- **Filtros**: categoría, proveedor, país del almacén, costo total, margen mínimo, pedidos mínimos,
  score mínimo, rating, días de envío, solo en tendencia, solo con stock.
- **Métricas por producto**: precio sugerido, ganancia, margen, punto de equilibrio, tendencia de 7 días
  y "score ganador" (0–100).
- **Rentabilidad contraentrega** (pestaña *Rentabilidad*): ganancia por pedido entregado y confirmado con devoluciones, fletes, recaudo y CPA/ROAS; supuestos editables por usuario ([fórmulas](docs/profit-calculator.md)).
- **Reglas de precio** configurables (multiplicador, monto fijo, comisiones).
- **Mis productos**: importar a drafts, editar precio y activar/desactivar.
- **Monitor de precio/stock** con alertas (subida o bajada de costo, sin stock, venta a pérdida).
- **Carga de catálogos por CSV** (pestaña *Proveedores*): el archivo de cualquier mayorista o un Excel exportado.
  Acepta encabezados en español o inglés, separador `,` o `;` y decimales con coma o punto.
  Hay una plantilla en `frontend/plantilla-proveedor.csv`.
- **Enlace al producto original** ("Ver en proveedor ↗") cuando el proveedor lo informa (columna `url`).
- **Imágenes**: usa la foto del proveedor (columna `imagen`); si no hay o no carga, genera una ilustración
  local que no depende de internet.
- **Conectores de proveedor** intercambiables (`backend/connectors/`): demo, CSV y un esqueleto para
  CJ Dropshipping.

## Productos de demostración
Al iniciar por primera vez se cargan 1.500 productos **ficticios** (marcados como DEMO) para probar la app.
No tienen ficha real en ningún proveedor. Se pueden eliminar desde *Proveedores → Eliminar productos de
demostración* y no vuelven a aparecer. Los productos que ya importaste se conservan.

## Requisitos
Python 3.10 o superior. No requiere dependencias externas.

## Ejecutar
```bash
cd backend && python3 server.py
```
Luego abre http://127.0.0.1:8000. La base SQLite (`backend/dropfinder.db`) se crea y se llena sola
la primera vez. Si ya tenías una base de la versión anterior, se actualiza sola al arrancar y conserva tus
productos importados y alertas. Para empezar de cero, bórrala.

### Primer arranque: cuenta del propietario
La primera vez que abres la app aparece **«Crear cuenta de propietario»**. Esa cuenta administra el catálogo,
las reglas de precio y los usuarios, y recibe los datos que ya existían (productos importados, alertas y
búsquedas guardadas). Después, el propietario crea las cuentas del equipo en *menú del usuario → Usuarios*
(con una contraseña inicial que cada persona cambia en *Mi cuenta*). No hay registro abierto.

La cuenta del propietario solo se puede crear **desde el mismo PC** (abriendo `http://127.0.0.1:8000`, sin
proxy). Desde otro equipo o a través del proxy hace falta el **código de instalación**: mientras no haya
cuentas, el servidor lo imprime en la consola al arrancar (o usa el valor de `DROPFINDER_SETUP_TOKEN`). Así
nadie que llegue antes que tú se queda con la instalación y sus datos. Si se borraran todas las cuentas (la app
no deja borrar al último propietario, pero podría pasar editando la base), el registro vuelve a abrirse con
estas mismas condiciones.

| Dato | De quién es |
|---|---|
| Productos importados, alertas, búsquedas guardadas, colecciones | De cada usuario |
| Moneda en que ve los precios, redondeo en COP, IVA mostrado | De cada usuario |
| Catálogo de proveedores, reglas de precio, historial y tendencias | De la instalación (solo el propietario cambia catálogo y reglas) |
| Tasa de cambio (TRM/manual) y cobertura cambiaria | De la instalación (solo el propietario) |
| Búsquedas recientes del autocompletado | De cada usuario (no se ven las de otros) |

Solo el propietario puede: cambiar reglas de precio, cargar o eliminar catálogos, cambiar la tasa de cambio o
actualizar la TRM, y administrar usuarios. Cualquier usuario puede ejecutar el monitor (revisa sus propios
productos importados). Como el catálogo es compartido, el monitor no vuelve a consultar ni a modificar un
producto que se actualizó hace menos de **15 minutos** (lo haya ejecutado quien sea); tus alertas se calculan
igual con el último dato conocido. El propietario puede forzar la consulta (`{"force": true}`).

Seguridad: contraseñas con `scrypt` (mínimo 10 caracteres), sesión en cookie `HttpOnly`/`SameSite=Lax` que
caduca tras 14 días sin uso, token CSRF en cada cambio y cabeceras CSP/`X-Frame-Options` (HSTS detrás del proxy
HTTPS). Cambiar la contraseña cierra las demás sesiones.

**Límite de intentos de inicio de sesión** (ventana de 15 minutos, responde 429 con `Retry-After`):
- 5 fallos para un mismo correo desde una misma IP;
- 20 fallos desde una IP (con cualquier correo);
- 50 fallos para un correo sumando todas las IP. Este límite **no** se aplica a una IP desde la que ese correo
  ya inició sesión en los últimos 30 días, así un atacante no puede dejarte fuera de tu equipo habitual.

Riesgo que queda: alguien que reparta muchos intentos entre muchas IP puede hacer esperar hasta 15 minutos a un
usuario que entre desde una IP **nueva** (otro equipo o red). Para desbloquear a mano, el propietario puede
borrar los intentos fallidos: `DELETE FROM login_attempts WHERE ok = 0;` en la base (`backend/dropfinder.db`).

### Usar la app desde otros equipos (detrás de HTTPS)
Por defecto solo escucha en `127.0.0.1` y la app **no habla HTTPS**. Para usarla desde internet o desde otros
equipos, ponla detrás de un proxy con HTTPS (Caddy o nginx) en el mismo PC:

```
# Caddyfile
dropfinder.midominio.com {
    reverse_proxy 127.0.0.1:8000
}
```
```bash
DROPFINDER_ALLOWED_HOSTS=dropfinder.midominio.com DROPFINDER_TRUSTED_PROXY=127.0.0.1 python3 server.py
```
- `DROPFINDER_ALLOWED_HOSTS`: nombres con que se llega a la app (cabecera `Host`), separados por coma. Los
  demás reciben 421 (protección contra *DNS rebinding*). `127.0.0.1` y `localhost` siempre están permitidos.
- `DROPFINDER_TRUSTED_PROXY`: IP del proxy. Solo de esa IP se aceptan `X-Forwarded-Proto` y
  `X-Forwarded-For`; con `X-Forwarded-Proto: https` la cookie pasa a ser `__Host-df_session` con `Secure`, y el
  límite de intentos usa la IP real del cliente. Con nginx, envía `Host`, `X-Forwarded-Proto` y
  `X-Forwarded-For` (`proxy_set_header`).
- Sin `DROPFINDER_TRUSTED_PROXY`, la app ve a **todos** los clientes del proxy con la misma IP (`127.0.0.1`): 20
  fallos de cualquiera bloquearían el inicio de sesión de todos. Si llega `X-Forwarded-For` de un origen no
  configurado, el servidor lo avisa una vez en la consola.
- Regístrate como propietario **antes** de abrir el proxy (o usa el código de instalación, ver arriba).
- No uses `--host 0.0.0.0` para exponerla sin proxy: las contraseñas viajarían sin cifrar.
- Solo IPv4: `--host ::` o `--host ::1` no se admiten (el servidor se niega a arrancar). Para IPv6, el proxy.
- Prefiere `127.0.0.1` a `localhost` y no corras otras aplicaciones web en el mismo equipo: las cookies no
  distinguen puertos.

Solo para desarrollo: `DROPFINDER_ENV=development DROPFINDER_DEV_USER=<id>` hace que las peticiones sin sesión
actúen como ese usuario (sin contraseña ni CSRF). Solo vale para peticiones directas desde el mismo equipo (IP y
`Host` locales, sin cabeceras `X-Forwarded-*`), avisa al arrancar y el servidor **no arranca** si además hay
`DROPFINDER_TRUSTED_PROXY`, hosts permitidos que no son locales o un `--host` que no es local. Nunca lo uses con
datos reales.

## Conectar una tienda (Shopify, Tiendanube o WooCommerce)

Desde la pestaña **Tiendas** conectas tu tienda (eliges la plataforma en el asistente) y desde **Mis productos**
publicas con «Publicar en tienda». DropFinder mantiene después el stock al día y, si lo activas, sube el precio cuando
sube el costo. **Todo producto se publica como borrador**: lo revisas en tu tienda y solo lo haces visible tú, con
confirmación.

> ⚠️ **Las tres APIs están SIN VERIFICAR contra tiendas reales. Pruébalo primero en una tienda de desarrollo o de
> staging, nunca en tu tienda de verdad.**
> Los tres conectores se escribieron contra la documentación pública de cada plataforma y solo se probaron contra
> servidores simulados (`tests/fake_*.py`), porque el entorno de desarrollo no tiene acceso a internet. Rutas, nombres
> de campo, códigos de error, límites y cabeceras pueden diferir de la API real. La lista exacta de supuestos sin
> comprobar está en el encabezado de cada conector (`backend/store_connectors/shopify.py`, `tiendanube.py` y
> `woocommerce.py`) y en [`docs/adr/0003-tiendanube-woocommerce.md`](docs/adr/0003-tiendanube-woocommerce.md).
> En Shopify, por ejemplo, no se pudo verificar la versión de API (`API_VERSION`; si devuelve un error de directiva o de
> versión en la primera conexión, cambia a `2026-01` con la variable `DROPFINDER_SHOPIFY_API_VERSION`), el flujo de
> credenciales de cliente (ID y secreto) ni si las operaciones de inventario exigen clave de idempotencia desde `2026-04`.

**Cómo conectar** (la pantalla de conexión repite estos pasos y puede quedar desactualizada: verifica en la
documentación de cada plataforma):

*Shopify*
1. En tu panel de Shopify crea una aplicación con estos permisos de la API de administración: `write_products`,
   `write_inventory` y `read_locations` (se piden también lectura de productos).
2. Usa la forma que tu tienda permita: el **token de acceso** de una aplicación personalizada, o el **ID y el secreto**
   de una aplicación del Dev Dashboard instalada en tu tienda.
3. En **Tiendas → Conectar una tienda** elige Shopify, pega el dominio (`tutienda.myshopify.com`; puedes pegar el enlace
   completo) y las credenciales. DropFinder las valida contra tu tienda antes de guardarlas.

*Tiendanube*
1. Crea una aplicación en el portal de socios de Tiendanube con permisos de productos (lectura y escritura:
   `read_products`, `write_products`) e instálala en tu tienda (mejor, una tienda de desarrollo). Al terminar el flujo
   OAuth, Tiendanube entrega el **token de acceso** (`access_token`) y el ID de la tienda.
2. En **Tiendas → Conectar una tienda** elige Tiendanube y escribe el **ID numérico de la tienda** (se ve en la URL de
   tu panel; también puedes pegar esa URL y DropFinder extrae el número; no sirve el nombre ni el dominio).
3. Pega el **token de la app** y un **correo de contacto** (Tiendanube exige identificar la aplicación con un correo).

*WooCommerce*
1. Escribe la **URL exacta de tu tienda**, con el dominio tal como carga en el navegador (con o sin `www`). Debe usar
   **HTTPS** estándar (sin puerto) y DropFinder **no sigue redirecciones**: si tu sitio redirige de un dominio a otro,
   la conexión falla y debes poner el dominio de destino. Se requieren enlaces permanentes «bonitos» (no «Simple»).
2. En WordPress: WooCommerce → Ajustes → Avanzado → API REST → Añadir clave, con permisos de **Lectura/Escritura**.
3. Pega la **clave de cliente** (`ck_…`) y el **secreto de cliente** (`cs_…`); el secreto se muestra una sola vez.
4. **Si al conectar da un error 401** aunque las claves sean correctas, lo más probable es que tu hosting elimine la
   cabecera `Authorization`: pide a tu proveedor que la deje pasar (en Apache suele resolverse con una regla en
   `.htaccess`; en Cloudflare o un cortafuegos de WordPress, revisa que no bloqueen `/wp-json`).

**Qué productos toca DropFinder (regla de propiedad)**
- Al publicar, DropFinder marca el producto con las etiquetas **`dropfinder`** y **`dfi-<id>`** (identifican esta
  instalación) y le pone el SKU `DF-<id>`. **Solo adopta y solo borra productos que lleven esas etiquetas.**
- Si en tu tienda ya existe un producto con el mismo SKU `DF-<id>` pero **sin** esas etiquetas (p. ej. uno tuyo o de otra
  instalación), es un **conflicto**: DropFinder no lo adopta, no lo modifica ni lo borra, y la publicación falla con un
  mensaje claro. **Lo resuelves tú**: cambia el SKU del producto ajeno (o publica otro) y reintenta.
- **No quites esas etiquetas a mano** de los productos publicados: sin ellas DropFinder deja de reconocerlos como suyos
  (no podrá adoptarlos ni borrarlos).

**Reglas de seguridad y de dinero que conviene conocer** (iguales en las tres plataformas)
- Las credenciales se guardan **cifradas** y nunca vuelven al navegador. La clave de cifrado vive en
  `%LOCALAPPDATA%\DropFinder\dropfinder.key` (Linux/macOS: `~/.local/share/dropfinder/`), **fuera del proyecto y de la base
  de datos**. **Respáldala aparte**: si se pierde, hay que volver a conectar las tiendas (la app te lo dice con claridad).
- Los **productos de demostración** son ficticios: solo se publican si lo confirmas expresamente y **nunca** se
  sincronizan solos con una tienda real.
- DropFinder **nunca baja un precio por su cuenta**. Subirlo automáticamente es opcional (por tienda), con un tope por
  cambio y un máximo total de 2 veces el precio publicado.
- Si el proveedor se queda sin stock, el producto se **oculta** en tu tienda; si el stock vuelve, solo se vuelve a mostrar
  si lo había ocultado esa regla (nunca un borrador ni algo que ocultaste tú).
- Si una publicación termina en un resultado dudoso (por ejemplo, la tienda tardó en responder), DropFinder **no la
  repite a ciegas**: marca el producto con un aviso para que revises tu tienda (SKU `DF-<id>`) antes de reintentar.
- Desconectar una tienda **no borra** los productos ya publicados: seguirán en tu tienda, pero DropFinder dejará de
  sincronizarlos.
- Un monitor programado (solo el propietario, apagado por defecto) puede revisar precios y stock cada cierto tiempo.

Diseño, seguridad y límites conocidos: [`docs/adr/0002-integracion-tiendas.md`](docs/adr/0002-integracion-tiendas.md),
[`docs/adr/0003-tiendanube-woocommerce.md`](docs/adr/0003-tiendanube-woocommerce.md) y contrato de la API en
[`docs/stores-api.md`](docs/stores-api.md).

## Pruebas
```bash
DROPFINDER_SYNTH_HISTORY=0 python3 -W error::ResourceWarning -m unittest discover tests   # 766 pruebas, ~80 s
node tests/js/stores_helpers.test.js                                                      # ayudas de la interfaz
```
Además hay una **prueba de punta a punta con navegador real** (Playwright) contra el servidor real y tiendas simuladas de
Shopify, Tiendanube y WooCommerce: ver [`tests/e2e/README.md`](tests/e2e/README.md). No usa internet ni tiendas reales.

## Estructura
```
backend/
  server.py            API REST + servidor de archivos estáticos (autenticación, CSRF, cabeceras de seguridad)
  auth.py              Cuentas, contraseñas, sesiones, límite de intentos y administración de usuarios
  db.py                Esquema SQLite, semilla y conexión
  metrics.py           Precio sugerido, ganancia, margen y score
  connectors/          Interfaz SupplierConnector: demo (mock.py), CSV (csv_connector.py) y esqueleto de CJ
  store_connectors/    Conectores de tienda: base.py (contrato), http.py (cliente compartido anti-SSRF),
                       shopify.py, tiendanube.py, woocommerce.py y fake.py (tienda de pruebas en memoria)
  stores_mod.py        API de tiendas (conexiones, publicación, productos publicados, registro)
  store_sync.py        Motor de sincronización de stock/precio y monitor programado
  secrets_store.py     Cifrado de las credenciales de las tiendas
frontend/              SPA en HTML, CSS y JS vanilla (+ plantilla-proveedor.csv)
docs/RAE-AutoDS.md     Ingeniería inversa, análisis y especificación
docs/adr/              Decisiones de diseño (0002 integración de tiendas, 0003 Tiendanube y WooCommerce)
docs/stores-api.md     Contrato de la API de tiendas
tests/                 Pruebas de la API y de los conectores, con servidores simulados (fake_shopify.py,
                       fake_tiendanube.py, fake_woocommerce.py)
tests/js/              Pruebas de las ayudas de la interfaz (Node)
tests/e2e/             Prueba de punta a punta con Playwright
```
