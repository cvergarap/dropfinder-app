# ADR 0004 — Proveedor CJ Dropshipping (API v2.0)

- **Estado:** aceptada (Sprint 7, noviembre 2026). **Depende de** ADR 0002 (cliente HTTP compartido anti-SSRF, `scrub`,
  `secrets_store`) y del contrato `SupplierConnector` (`backend/connectors/base.py`).
- Todo lo que sigue sobre la API de CJ está escrito **de memoria de su documentación pública y NO se pudo verificar** (el
  entorno de desarrollo no tiene red). El conector se probó solo contra `tests/fake_cj.py`, que implementa lo que el conector
  *supone*. La lista completa de lo no verificado vive en el encabezado de `backend/connectors/cj.py` y en el README
  («SIN VERIFICAR»); este ADR la resume al final.

## Decisiones

### 1. Conexión de la instalación, no de cada usuario
El catálogo de proveedores es compartido y solo el propietario lo cambia (CSV incluido). Una importación de CJ escribe en ese
catálogo, así que **conectar, validar, desconectar, listar categorías e importar son solo-propietario** (`OWNER_ONLY` en
`server.py`; los handlers vuelven a comprobarlo: defensa en profundidad). El estado (`GET /api/suppliers/cj`) es de usuario y no
contiene ningún dato de la clave. El monitor de cada usuario consulta CJ con la clave de la instalación para los productos de CJ
que ese usuario importó; está acotado (ver 5), así que un miembro no puede agotar la cuota de CJ.

### 2. Sin versión nueva de esquema
Otro sprint puede añadir la v8 en paralelo. La conexión vive en una tabla propia, `supplier_connections` (una fila por proveedor,
clave primaria `supplier = 'cj'`), creada de forma **perezosa e idempotente** (`CREATE TABLE IF NOT EXISTS`, una sola sentencia:
atómica) la primera vez que se usa. `PRAGMA user_version` no cambia. Si más adelante se prefiere en el esquema oficial, basta con
mover el `CREATE` a `db.SCHEMA` (es el mismo texto). `secrets_store._has_encrypted_data` ahora también mira esta tabla: perder
`dropfinder.key` con una conexión de CJ guardada es una pérdida que se informa, no un primer arranque que genera otra clave.

### 3. API key cifrada y nunca de vuelta
`secrets_store.encrypt(key, "supplier:cj")` (mismo almacén y clave maestra que las tiendas). La clave se valida **contra CJ antes de
guardar** (token + una lectura mínima), sin transacción abierta. Nunca aparece en respuestas, registros, `repr` ni mensajes de
error: todo texto de CJ pasa por `clean_text` y por `scrub(texto, [clave])`; los errores del conector no incluyen cuerpos ni
cabeceras. Los tokens de acceso/refresco solo viven en memoria (caché del proceso por hash de base+clave) y no se escriben en la
base. Desconectar borra la fila y olvida los tokens, **no** borra los productos importados.

### 4. Conector (`connectors/cj.py`)
- **Red**: solo por `store_connectors/http.py` (HTTPS, IP pública fijada, sin redirecciones, tope de bytes y de tiempo). Host y
  prefijo fijos; `base_url` distinto solo se acepta con `ALLOW_LOOPBACK` (pruebas) y solo hacia `http://127.0.0.1`. 2 MB de tope.
- **Token**: se obtiene con la API key y se cachea hasta 5 min antes de caducar; al caducar se refresca con el *refresh token* y,
  si este se rechaza o caducó, con la API key. Ante **401** (o el código de «token inválido» del sobre) se invalida, se refresca
  **una vez** y se repite la petición; un segundo 401 es `CJAuthError` (se avisa de reconectar; sin bucles). Pedir/refrescar token
  no se reintenta (CJ limita esa ruta; reintentar solo empeoraría el límite).
- **Límite de tasa**: separación mínima entre peticiones por clave (`MIN_INTERVAL`, compartida por todos los hilos) y, ante **429**,
  espera exponencial con tope (`BACKOFF_CAP`, también si CJ manda un `Retry-After` enorme), hasta `MAX_RETRIES` y dentro de un
  **plazo total** por llamada (`CALL_DEADLINE`): si esperar ya no cabe en el plazo se abandona con `CJRateLimited(retry_after)`.
- **Reintentos solo seguros**: lecturas (GET y el cálculo de envío, que es una consulta) reintentan 429, 5xx, errores de red y
  JSON inválido; una llamada marcada `safe=False` no se repite nunca (hoy no hay escrituras contra CJ).
- **Normalización** (`normalize_item`): ver el encabezado de `cj.py`. Precios con `Decimal` (`ROUND_HALF_UP`, 2 decimales,
  rechazo de NaN/infinito/negativos/cero/notación científica/>1e6); un rango toma el **máximo** (no prometer un margen que no se
  puede cumplir). Texto sin etiquetas (incluido el contenido de `<script>`/`<style>`), sin caracteres de control ni de dirección;
  URL solo http(s) sin usuario/contraseña; identificadores `[A-Za-z0-9._-]{1,64}`. Un producto sin id, título o precio válidos se
  omite y se cuenta (`invalid`). **No se inventan métricas**: pedidos, valoraciones y reseñas quedan en 0, el envío en 0 y los
  días de envío en 15 (columna obligatoria, valor por defecto nuestro).
- **Stock desconocido ≠ cero**: si CJ no da ningún dato de stock, `get_stock`/`get_price_stock` devuelven `None` (el monitor lo
  cuenta como omitido, sin alertas falsas); solo un cero explícito dispara «sin stock».

### 5. Importación y monitor
- **Importar** (`POST /api/suppliers/cj/import`, máx. **200** por importación, palabra clave y/o categoría): corre en un hilo
  porque con ~1 petición/s consultar el stock de 200 productos tarda minutos; el navegador sondea `GET /api/suppliers/cj`
  (progreso: buscando → stock → guardando). Una importación a la vez (409 si hay otra). Páginas de tamaño constante (la
  numeración de CJ depende de él). Guarda con **los mismos caminos que el CSV**: `db.upsert_products(..., "api")` (UNIQUE
  proveedor+sku, idempotente), `hooks.run_catalog_write` y alertas para quienes ya importaron el producto (`server._alerts_for_products`,
  registrada con `cj_mod.set_after_import`). **No pisa** filas del mismo proveedor y código con `source != 'api'` (CSV o demo): se
  cuentan como `conflicts`. Si CJ limita la tasa a mitad, se guarda lo que ya tiene stock y se avisa (nada a medias con stock falso).
- **Monitor** (`server.run_monitor`): los productos de CJ del usuario que no están en enfriamiento se consultan **antes** de tomar
  el bloqueo de escritura (`BEGIN IMMEDIATE`): antes todo el monitor corría dentro de él, y con el límite de tasa de CJ eso
  dejaría la base bloqueada para el resto de la app (hay una prueba que lo comprueba). Máximo `MONITOR_MAX_PRODUCTS` (40) y
  `MONITOR_BUDGET` (120 s) por ejecución; si CJ pide esperar o rechaza la clave, se deja de consultar, se avisa en `notice` y se
  marca la conexión (rechazo de clave). El resto del monitor (cooldown de 15 min, alertas, métricas, hooks) no cambia.
  Nota: el monitor original pasaba el *nombre* del proveedor a `get_price_stock`; para CJ se usa el `supplier_sku` real.

## Consecuencias y límites conocidos
- Una importación cuesta ~1 + N peticiones (N = productos): por eso el tope de 200 y el hilo. Un plan de CJ con más cuota se
  aprovecha cambiando `MIN_INTERVAL`.
- El costo de envío de CJ depende del destino y no viene en el listado: queda en 0 (`get_product(ship_to=…)` sabe consultarlo, pero
  la importación no lo usa para no multiplicar las peticiones). El margen mostrado de un producto de CJ es por tanto optimista
  hasta que el usuario ajusta el envío.
- El score de los productos de CJ es bajo por diseño (no hay pedidos ni valoraciones reales).
- Los tokens no sobreviven a un reinicio del servidor: se pide uno nuevo (CJ limita cuántos; si se pide más de lo permitido se
  informa con un mensaje claro, no se reintenta).
- «Eliminar catálogo» de la interfaz solo cubre demo y CSV; los productos de CJ se conservan al desconectar y no tienen botón de
  borrado masivo todavía.
- La interfaz de importación no se probó en un navegador real (no hay Playwright en este entorno): solo funciones puras (Node) y la
  API de punta a punta con el servidor real.

## Cambios tras la revisión independiente (PR #22)
- `validate()` ya no borra el token en caché (si `getAccessToken` daba 429 se bloqueaba todo ~5 min): valida con una lectura; se pide
  token nuevo solo con una clave nueva.
- **Stock desconocido ≠ 0** también en la importación: solo un número real cuenta como existencias (`parse_stock`); un producto
  nuevo sin dato de stock **no se importa** (se cuenta `stock_unknown`/`unknown_skipped` y se avisa) y uno ya importado conserva
  su stock y almacén (sin alerta falsa de «sin stock»).
- Monitor: orden `nunca revisados, luego más viejos primero` (sin starvation con el tope de 40); un error inesperado en un
  producto no tumba el monitor; el plazo por producto se acota a la mitad del presupuesto restante.
- Paginación: sigue mientras la página venga llena, no haya total o el total diga que faltan; avisa en `warnings` si se corta
  antes del tope pedido.
- Envío no informado: sin columna nueva, se **deriva** (`source='api'`, proveedor CJ, envío 0 y 15 días = valores por defecto):
  `shipping_known=false` en la API de productos; no compite por «más barato/más rápido» ni por los mejores del comparador, la UI
  dice «envío no informado» y el promedio de envío del proveedor lo ignora. Limitación: un producto de CJ con envío real de
  exactamente 0 y 15 días se vería como no informado. El nombre mostrado sigue siendo «CJ Dropshipping» (la clave `supplier` es
  la misma que la de la demo; se distingue por Origen): no se cambió la presentación.
- Candados: uno por clave para la E/S del token (el global solo en secciones cortas); `_pace` no reserva hueco si desiste por plazo.

## Lo que NO se verificó contra la API real (resumen; detalle en `cj.py` y el README)
Rutas y métodos; nombres de parámetros (`pageNum`, `pageSize`, `productNameEn`, `categoryId`, `pid`); formato del sobre
`{code, result, message, data}`; cuerpo y respuesta de `getAccessToken`/`refreshAccessToken` y formato de las fechas de caducidad;
vigencias (15/180 días) y que `refreshAccessToken` exista con ese cuerpo; códigos `1600001` (token) y `1600200` (tasa) y `Retry-After`;
límite de ~1 petición/s y de 1 token cada 5 min; que el listado traiga `sellPrice` como texto/rango; forma del stock
(`variantInventories[].inventory[].totalInventory`) y la ruta `getInventoryByPid`; forma de `freightCalculate`; el enlace de ficha
construido.

## Pruebas
`tests/test_cj.py` (82 pruebas: conector, conexión, importación, monitor, permisos, HTTP real con CSRF, fugas de secretos),
`tests/fake_cj.py` y `tests/js/cj_helpers.test.js` (7). **Mutaciones propias** (se aplica el cambio a mano, se corre
`tests.test_cj` y debe fallar): 31 de 31 detectadas —
refresco de token (5: sin refresco con *refresh token*, 401 que no invalida, sin tope de un refresco por 401, caché de token
ignorada, caducidad ilegible sin acotar), límite de tasa (9: backoff sin tope, sin límite de reintentos, escrituras
reintentadas, sin plazo total, 429 no reconocido, `Retry-After` ignorado, sin separación mínima, monitor sin tope por
ejecución, monitor que sigue tras un 429), saneado (9: etiquetas HTML, `javascript:`, usuario:clave en URL, precio
negativo/NaN, imagen sin validar, título sin sanear, stock sin acotar, rango con el mínimo, id sin validar) y no-fuga/permisos
(8: `scrub` quitado en la API, en el token y en el trabajo, `repr` con la clave, clave guardada en claro, estado que devuelve
la credencial, miembro que puede conectar, ruta de importar sin clasificar). La primera pasada dejó **2 supervivientes**
(quitar el `scrub` de la API o el del conector no se notaba porque cada capa tapaba a la otra): se añadieron pruebas por capa
y las dos mutaciones quedaron detectadas. Escribir las pruebas además destapó un fallo real: los códigos propios de CJ
(`1600xxx`) se clasificaban como «error del servidor» y se reintentaban; ahora solo 500-599 lo son.
