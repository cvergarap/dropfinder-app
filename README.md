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

| Dato | De quién es |
|---|---|
| Productos importados, alertas, búsquedas guardadas, colecciones | De cada usuario |
| Moneda en que ve los precios, redondeo en COP, IVA mostrado | De cada usuario |
| Catálogo de proveedores, reglas de precio, historial y tendencias | De la instalación (solo el propietario cambia catálogo y reglas) |
| Tasa de cambio (TRM/manual) y cobertura cambiaria | De la instalación (solo el propietario) |
| Búsquedas populares del autocompletado | De la instalación |

Solo el propietario puede: cambiar reglas de precio, cargar o eliminar catálogos, cambiar la tasa de cambio y
administrar usuarios. Cualquier usuario puede ejecutar el monitor (revisa sus propios productos importados).

Seguridad: contraseñas con `scrypt` (mínimo 10 caracteres), sesión en cookie `HttpOnly`/`SameSite=Lax` que
caduca tras 14 días sin uso, token CSRF en cada cambio, límite de intentos (5 por correo o 20 por IP en 15
minutos) y cabeceras CSP/`X-Frame-Options`. Cambiar la contraseña cierra las demás sesiones.

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
- No uses `--host 0.0.0.0` para exponerla sin proxy: las contraseñas viajarían sin cifrar.

Solo para desarrollo: `DROPFINDER_ENV=development DROPFINDER_DEV_USER=<id>` hace que las peticiones sin sesión
actúen como ese usuario (sin contraseña ni CSRF). Solo funciona si el servidor escucha en `127.0.0.1` y avisa al
arrancar; nunca lo uses con datos reales.

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
