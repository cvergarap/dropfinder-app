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

## Conectar una tienda (Shopify)

Desde la pestaña **Tiendas** conectas tu tienda y desde **Mis productos** publicas con «Publicar en tienda». DropFinder
mantiene después el stock al día y, si lo activas, sube el precio cuando sube el costo. **Todo producto se publica como
borrador**: lo revisas en tu tienda y solo lo haces visible tú, con confirmación.

> ⚠️ **Antes de usarlo con una tienda de verdad: pruébalo primero en una tienda de desarrollo de Shopify.**
> El conector se escribió contra la documentación pública de Shopify y se probó contra un servidor simulado, porque
> el entorno de desarrollo no tiene acceso a internet. No se pudo verificar contra la Shopify real:
> la versión de API que se usa (`API_VERSION` en `backend/store_connectors/shopify.py`), la forma exacta de algunas
> operaciones, el flujo de credenciales de cliente (ID y secreto), si las operaciones de inventario exigen una clave de
> idempotencia desde la versión `2026-04`, y el texto de ayuda para crear la aplicación. Si Shopify devuelve un error
> de directiva o de versión en la primera conexión, cambia `API_VERSION` a `2026-01` (variable de entorno
> `DROPFINDER_SHOPIFY_API_VERSION`). La lista completa está en el encabezado de ese archivo.

**Cómo conectar** (la pantalla de conexión repite estos pasos y puede quedar desactualizada: verifica en la
documentación de Shopify):
1. En tu panel de Shopify crea una aplicación con estos permisos de la API de administración: `write_products`,
   `write_inventory` y `read_locations` (se piden también lectura de productos).
2. Usa la forma que tu tienda permita: el **token de acceso** de una aplicación personalizada, o el **ID y el secreto**
   de una aplicación del Dev Dashboard instalada en tu tienda.
3. En **Tiendas → Conectar una tienda** pega el dominio (`tutienda.myshopify.com`; puedes pegar el enlace completo) y
   las credenciales. DropFinder las valida contra tu tienda antes de guardarlas.

**Reglas de seguridad y de dinero que conviene conocer**
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

Diseño, seguridad y límites conocidos: [`docs/adr/0002-integracion-tiendas.md`](docs/adr/0002-integracion-tiendas.md) y
contrato de la API en [`docs/stores-api.md`](docs/stores-api.md).

## Pruebas
```bash
python3 -W error::ResourceWarning -m unittest discover tests
```

## Estructura
```
backend/
  server.py        API REST + servidor de archivos estáticos (autenticación, CSRF, cabeceras de seguridad)
  auth.py          Cuentas, contraseñas, sesiones, límite de intentos y administración de usuarios
  db.py            Esquema SQLite, semilla y conexión
  metrics.py       Precio sugerido, ganancia, margen y score
  connectors/      Interfaz SupplierConnector: demo (mock.py), CSV (csv_connector.py) y esqueleto de CJ
frontend/          SPA en HTML, CSS y JS vanilla (+ plantilla-proveedor.csv)
docs/RAE-AutoDS.md Ingeniería inversa, análisis y especificación
tests/             Pruebas de la API
```
