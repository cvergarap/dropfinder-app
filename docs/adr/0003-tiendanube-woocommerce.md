# ADR 0003 — Conectores de Tiendanube y WooCommerce

- **Estado:** aceptada (Sprint 6, noviembre 2026). **Depende de** ADR 0001 y 0002 (contrato `StoreConnector`, cliente HTTP
  compartido, `scrub`, motor de sincronización).
- Todo lo que sigue sobre las APIs de las plataformas está escrito **de memoria de su documentación pública y no se pudo
  verificar** (el entorno de desarrollo no tiene red). Cada conector lo declara en su encabezado, como Shopify.

## Reglas que valen para ambos (obligatorias)

1. **Red solo por `store_connectors/http.py`** (HTTPS, puerto 443, IP pública resuelta una vez, sin redirecciones, tiempo y
   tamaño máximos). Nada de `urllib.request`, `http.client` ni `socket` en el conector (solo `urllib.parse` para analizar URL).
2. **Idempotencia** (`idempotent_publish = True`): antes de crear, buscar por SKU `DF-<id>` y **adoptar** el producto si ya existe.
   Un fallo a mitad de la publicación no deja huérfanos: se limpia con reintentos acotados o se informa `orphan_external_id`.
3. **Siempre se crea como borrador / no publicado**; mostrar es un paso aparte (`set_active`). Un precio ≤ 0, un stock no
   válido o un título vacío se rechazan con `StoreValidationError` **antes** de tocar la tienda.
4. **Errores**: 401/403 → `StoreAuthError`; 429 → `StoreRateLimited(retry_after)`; 5xx / red → `StoreError(retryable=True)`
   (el cliente compartido ya marca `not_sent`); 404 de un producto → `StoreNotFound`; 404 del propio endpoint o dominio →
   `StoreValidationError` (nunca `StoreNotFound`, o un `delete` lo tomaría por éxito); rechazo de datos → `StoreValidationError`.
   Todo texto que venga de la tienda pasa por `scrub(texto, secretos)`; las credenciales y los tokens nunca aparecen en
   mensajes, `repr` ni registros.
5. **Dominio / identificador** validado de forma estricta en `normalize_domain` (acepta URL pegada y la normaliza; rechaza
   IP, puertos, usuario@, unicode engañoso, rutas raras). `validate()` devuelve `StoreInfo` con nombre, moneda (3 letras) y
   permisos; los datos de la tienda se tratan como hostiles.
6. **Valores**: precio como cadena decimal con 2 decimales (Decimal, `ROUND_HALF_UP`, ≤ 1e9), stock entero 0…1e9, título y SKU
   ≤ 255, imágenes solo `https` ASCII sin IP literal (máx. 10), nunca enviar `compare_at`/precio de oferta nulo si no se pasó
   (no borrar lo que el usuario puso a mano).
7. **Pruebas**: un servidor simulado local por plataforma (`tests/fake_<plataforma>.py`, 127.0.0.1, con
   `store_connectors.http.ALLOW_LOOPBACK = True` solo en la prueba), que **valida** operaciones, claves y tipos de lo que recibe
   (no solo registra), e inyecta fallos (429, 5xx, 401, 404, JSON roto, redirección, respuesta enorme, respuesta perdida
   tras aplicar). Pruebas por mutación de lo crítico.

## Tiendanube (Nuvemshop)

- API: `https://api.tiendanube.com/v1/{store_id}/…` (host **fijo**; `store_id` numérico). Cabeceras: `Authentication: bearer <token>`
  y `User-Agent: <nombre de la app> (<correo de contacto>)` obligatorio. JSON. Límite de velocidad por cubo con fugas (~40/min;
  `429` con `x-rate-limit-reset`).
- Credenciales: `access_token` (de la aplicación de Tiendanube instalada en la tienda) y `contact_email` (para el `User-Agent`).
  El `domain` del contrato es el **ID numérico de la tienda** (se acepta pegarlo o pegar una URL del panel que lo contenga).
  Permisos: `write_products` (y lectura de productos y de la tienda).
- `validate()`: `GET /store` → nombre, `main_currency`; permisos si la API los informa.
- Productos: `POST /products` con `name` (objeto por idioma, p. ej. `{"es": …}`), `description`, `published: false`, `variants:
  [{price, stock, sku, cost?}]`, `images: [{src}]`, `tags` (cadena separada por comas), `brand`. Actualizar variante: `PUT
  /products/{id}/variants/{variant_id}` (`price`, `stock`, `promotional_price`); visibilidad: `PUT /products/{id}`
  `{published: bool}`; borrar: `DELETE /products/{id}`; buscar por SKU: `GET /products/sku/{sku}` (404 = no existe).
- Moneda: `main_currency` de la tienda. Monedas fuera de USD/COP las rechaza el motor (`currency_unsupported`).

## WooCommerce

- API: `https://<dominio>/wp-json/wc/v3/…`. **Dominio libre** (cada tienda tiene el suyo): la protección anti-SSRF depende por
  completo de `http.py` (IP pública, 443, sin redirecciones). Si el sitio redirige (www ↔ sin www, http → https) el cliente lo
  rechaza: el mensaje debe pedir **el dominio exacto con el que carga la tienda**.
- Credenciales: `consumer_key` (`ck_…`) y `consumer_secret` (`cs_…`) con permiso de **lectura/escritura**, enviadas por
  autenticación básica sobre HTTPS (nunca en la URL: acabarían en registros). Si el hosting elimina la cabecera
  `Authorization`, la respuesta será 401: explicar la causa probable.
- `validate()`: `GET /wp-json/wc/v3/system_status` o, si no hay permiso, `GET /wp-json/wc/v3/settings/general/woocommerce_currency`
  y `GET /wp-json/wc/v3/products?per_page=1` para comprobar lectura; nombre desde `GET /wp-json` (`name`). Moneda de ahí.
- Productos: `POST /products` con `name`, `type: "simple"`, `status: "draft"`, `sku`, `regular_price` (cadena), `manage_stock:
  true`, `stock_quantity`, `description`, `images: [{src}]`, `tags: [{name}]`; actualizar `PUT /products/{id}`; visibilidad
  `status: "publish" | "draft"`; borrar `DELETE /products/{id}?force=true`; buscar por SKU `GET /products?sku=<sku>` (lista).
  Un SKU repetido lo rechaza WooCommerce (`woocommerce_rest_product_sku_already_exists`): tratarlo como señal de adopción.
- Límite de velocidad: depende del hosting; tratar 429 y 5xx como reintentables; sin cabeceras estándar, `retry_after` por defecto.

## Interfaz

La pestaña Tiendas es genérica (campos, ayuda y grupos vienen de `credential_fields` y `setup_help`). Cada plataforma solo
necesita su sugerencia de dominio/identificador (`domainExample`) y, si hace falta, etiquetas; el contrato no cambia.

## Qué no se puede verificar desde el entorno de desarrollo

Rutas, nombres de campo, códigos de error, límites y cabeceras exactas de ambas APIs. Se prueban solo contra los simuladores.
La primera conexión real debe hacerse en una **tienda de desarrollo** (README).
