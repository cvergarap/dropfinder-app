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
   - Se cifran en reposo con una clave maestra de 32 bytes. Cifrado: flujo de claves HMAC-SHA256 en modo contador +
     etiqueta HMAC-SHA256 (cifrar y luego autenticar), nonce aleatorio de 16 bytes (obligatorio), claves de cifrado y
     de autenticación derivadas por separado. La etiqueta cubre `versión | len(aad) | aad | len(nonce) | nonce |
     texto cifrado` con **longitudes explícitas**, así ninguna reubicación de bytes entre campos conserva una etiqueta
     válida. Los datos asociados (`aad`) son `usuario | plataforma | dominio` de la tienda: un valor cifrado para una
     conexión no se puede copiar a otra fila ni a otro usuario.
   - **Dónde vive la clave** (`dropfinder.key`): en el directorio de datos **del usuario del sistema** (Windows:
     `%LOCALAPPDATA%\DropFinder`; Linux/macOS: `~/.local/share/dropfinder`), **nunca dentro del repositorio ni junto a
     la base de datos**; se puede cambiar con `DROPFINDER_KEY_FILE`. `*.key` está en `.gitignore`. Un respaldo o una copia
     de la carpeta del proyecto o de la base **no** incluye la clave (a propósito).
   - **Si se pierde la clave** las credenciales guardadas son irrecuperables: **hay que volver a conectar las tiendas**.
     No se genera una clave nueva en silencio si ya hay credenciales; las conexiones aparecen en estado «error» con un
     mensaje claro. Respaldar la clave aparte (gestor de contraseñas) es responsabilidad del usuario.
   - **Windows**: `0o600` no existe; el archivo hereda la ACL de la carpeta del perfil del usuario (privada para su
     cuenta y los administradores). No se probó en Windows desde el entorno de desarrollo.
   - **Qué protege y qué no**: protege contra la copia o filtración **del archivo de base de datos solo** (respaldos,
     envío a soporte, capturas). **No** protege contra alguien con acceso a la cuenta del usuario que también lea el
     archivo de clave. La biblioteca estándar no trae AES; migrar a una biblioteca criptográfica auditada es una
     condición de revisión (ver abajo).
   - Las credenciales **nunca** se devuelven al navegador (solo `••••••••` + los últimos 4 caracteres, y nada si el
     secreto es corto) ni se escriben en logs. **Todo texto que venga de una tienda pasa por `scrub()`** antes de
     guardarse, registrarse o mostrarse (quita credenciales conocidas y todo lo que parezca un token).
2. **SSRF** — un solo cliente HTTP para todas las plataformas (`store_connectors/http.py`), **obligatorio** en todo
   conector: solo HTTPS y puerto 443; el nombre se resuelve **una vez** y se conecta a esa IP (sin segunda resolución:
   DNS rebinding); se rechazan direcciones privadas, de bucle, de enlace local, reservadas, de metadatos de la nube
   (IPv4 e IPv6, incluidas las IPv4 mapeadas); **no se siguen redirecciones**; tiempo máximo y tope de tamaño de
   respuesta; TLS siempre verificado. Además cada plataforma valida su dominio: Shopify solo `*.myshopify.com`;
   plataformas de dominio libre (WooCommerce) dependen de las comprobaciones de IP del cliente compartido. Las URL
   de imágenes de productos se pasan a la plataforma (que las descarga); DropFinder no las descarga.
3. **Permisos mínimos**: Shopify `write_products`, `write_inventory`, `read_locations`. Nada de clientes ni pedidos
   hasta el sprint de pedidos.
4. **Aislamiento por usuario**: cada conexión, anuncio y trabajo pertenece a un usuario; las rutas devuelven 404 para
   los ajenos. Las tareas de fondo actúan con el `user_id` de cada fila (`auth_context.as_user`).
5. **Idempotencia**: publicar dos veces el mismo producto en la misma tienda no crea duplicados:
   `UNIQUE(imported_id, connection_id)`, índice único parcial `(connection_id, external_id)` (también cuando
   `imported_id` es NULL) y un único trabajo activo por (anuncio, acción). Los anuncios guardan `product_id` e
   `is_demo`, así que la regla «los productos de demostración nunca se sincronizan solos» sigue valiendo aunque el
   usuario quite el producto de Mis productos; `hidden_by_rule` distingue lo que ocultó una regla de lo que ocultó el usuario.
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
