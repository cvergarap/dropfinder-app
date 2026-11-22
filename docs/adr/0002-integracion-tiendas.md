# ADR 0002 — Integración con tiendas (Shopify, Tiendanube, WooCommerce…)

- **Estado:** aceptada (Sprint 4-5, noviembre 2026)
- **Depende de:** ADR 0001 (Python stdlib + SQLite; seguridad hecha a mano y probada).

## Qué se construye

El flujo que convierte a DropFinder de buscador en herramienta diaria:
**publicar un producto importado en la tienda del usuario y mantener su precio y stock sincronizados.**

## Arquitectura

```
Mis productos ──publicar──► store_jobs (cola persistente) ──► worker (hilo de fondo)
      ▲                                                        │ usa StoreConnector
monitor / cambios de costo ──► reglas de sincronización ───────┘ (Shopify, luego Tiendanube, WooCommerce)
```

- **`StoreConnector`** (`backend/store_connectors/base.py`): contrato común. Cada plataforma lo implementa:
  `validate()`, `publish()`, `update_price_stock()`, `set_active()`, `delete()`. Se registran por nombre
  (`store_connectors.register`). Agregar una plataforma no toca el resto.
- **Tablas** (esquema v7, por usuario):
  - `store_connections`: tienda conectada; credenciales **cifradas** (`credentials_enc`).
  - `store_listings`: un producto importado publicado en una tienda (ids externos, estado, último precio/stock).
    `imported_id` es opcional (`ON DELETE SET NULL`): si el usuario quita el producto de "Mis productos", el
    anuncio de la tienda no se pierde de vista y se puede retirar.
  - `store_jobs`: cola persistente con reintentos y espera creciente.
  - `store_sync_log`: bitácora corta (se poda) para explicar qué pasó.
- **Trabajo en segundo plano**: hilo *daemon* en lotes cortos, igual que el historial y las búsquedas guardadas.
  Las peticiones del usuario solo encolan trabajos y responden al instante.

## Seguridad (obligaciones de esta funcionalidad)

1. **Credenciales de tiendas** (`backend/secrets_store.py`):
   - Se cifran en reposo con una clave maestra de 32 bytes guardada en un archivo `dropfinder.key` junto a la base
     (permisos 0600 donde el sistema lo permite; en Windows, el perfil del usuario). Cifrado: flujo de claves
     HMAC-SHA256 en modo contador + etiqueta HMAC-SHA256 (cifrar y luego autenticar), con nonce aleatorio, claves
     derivadas por separado y datos asociados (usuario + conexión) para que un valor no se pueda mover a otra fila.
   - **Qué protege y qué no**: protege contra la copia o filtración **del archivo de base de datos solo** (respaldos,
     envío a soporte, capturas). **No** protege contra alguien con acceso al PC que también lea el archivo de clave.
     La biblioteca estándar no trae AES; migrar a una biblioteca criptográfica auditada es una condición de revisión
     (ver abajo) si la app se ofrece como servicio.
   - Las credenciales **nunca** se devuelven al navegador (solo los últimos 4 caracteres) ni se escriben en logs.
2. **SSRF**: el dominio de la tienda se valida con una expresión estricta (`*.myshopify.com` para Shopify), solo
   HTTPS, sin seguir redirecciones, con tiempo máximo y tope de tamaño de respuesta. Las URL de imágenes de
   productos se pasan a la plataforma (que las descarga); DropFinder no las descarga.
3. **Permisos mínimos**: Shopify `write_products`, `write_inventory`, `read_locations`. Nada de clientes ni pedidos
   hasta el sprint de pedidos.
4. **Aislamiento por usuario**: cada conexión, anuncio y trabajo pertenece a un usuario; las rutas devuelven 404 para
   los ajenos. Las tareas de fondo actúan con el `user_id` de cada fila (`auth_context.as_user`).
5. **Idempotencia**: publicar dos veces el mismo producto en la misma tienda no crea duplicados
   (`UNIQUE(imported_id, connection_id)` + estado del anuncio).
6. Las acciones sobre la tienda real (publicar, cambiar precio, ocultar, eliminar) son siempre una **acción del
   usuario** o una **regla que el usuario activó** y quedan en la bitácora.

## Reglas de sincronización (por tienda, configurables, desactivadas por defecto salvo el stock)

- Si el producto se agota en el proveedor → **ocultar** en la tienda; si vuelve el stock → **mostrar**.
- Si sube el costo y el margen cae bajo el mínimo del usuario → **subir el precio** (con tope) o **avisar**.
- Nunca bajar precios automáticamente sin que el usuario lo haya activado.

## Cuándo revisar

Migrar el cifrado a una biblioteca auditada y la cola a un sistema externo si: la app se ofrece como servicio a
terceros, hay más de unos miles de anuncios por usuario, o se necesita OAuth de tienda pública (Shopify/Tiendanube)
en lugar de claves de aplicación personalizada.

## Qué no se puede verificar desde el entorno de desarrollo

Las APIs reales de Shopify, Tiendanube y WooCommerce no son accesibles desde la red de desarrollo. Los conectores se
escriben contra la documentación pública y se prueban contra servidores simulados que imitan sus respuestas
(incluidos errores, límites de velocidad y casos borde). **La primera conexión con una tienda real debe hacerse en
una tienda de desarrollo**, con la guía del README.
