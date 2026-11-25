# Prueba de punta a punta (navegador real)

Levanta el servidor real, con el motor real y los conectores reales de **Shopify, Tiendanube y WooCommerce** apuntando a
**servidores simulados locales** (`tests/fake_shopify.py`, `tests/fake_tiendanube.py`, `tests/fake_woocommerce.py`), y
recorre la interfaz con Playwright.

- **Shopify** (29 comprobaciones): registro, importar productos, conectar, publicar como borrador, mostrar con
  confirmación, ocultar por falta de stock, volver a mostrar, credenciales revocadas, secretos en reposo, CSP y 390 px en
  modo oscuro.
- **Tiendanube y WooCommerce** (32 comprobaciones cada una): conexión desde el asistente con el selector de plataforma,
  publicación como borrador con las etiquetas de propiedad (`dropfinder` y `dfi-…`) verificadas en la tienda simulada,
  stock sincronizado, regla de sin stock que oculta y vuelve a mostrar, «Mostrar» con confirmación, y credenciales ausentes
  de la página, `localStorage`/`sessionStorage`, la API, la base de datos y el log.
- **Conflicto de propiedad** (por plataforma): se siembra en la tienda un producto **ajeno** con el mismo SKU `DF-<id>` y
  sin etiquetas; la publicación debe fallar con un mensaje claro en la interfaz y el producto ajeno debe quedar intacto,
  incluso al quitar el listado pidiendo «También eliminar de la tienda», al borrar el producto propio y al desconectar.

```bash
cd tests/e2e
export E2E_DIR=$(mktemp -d)      # directorio de trabajo NUEVO en cada ejecución (base de datos, clave, CSV, capturas)
mkdir -p "$E2E_DIR/e2e"
python3 launch.py > "$E2E_DIR/launch.log" 2>&1 &    # servidor en :8891 y panel de control de los simuladores en :8892
node e2e.js                      # requiere Node y Playwright (npm i playwright) con Chromium instalado
```

Al terminar imprime `Shopify: 29/29`, `Tiendanube: 32/32`, `WooCommerce: 32/32` y el total (93). Sin `E2E_DIR` usa
`tests/e2e/work/` (ignorado por Git). Usa siempre un directorio nuevo: la prueba empieza registrando al primer propietario,
así que una base de datos de una ejecución anterior la haría fallar. La prueba lee el log del servidor en
`$E2E_DIR/launch.log` (o `$E2E_DIR/e2e/launch.log`) para comprobar que no contiene credenciales. Si algo falla, deja
`error.png` y `error.html` en `$E2E_DIR/e2e/`. No usa internet ni una tienda real.
