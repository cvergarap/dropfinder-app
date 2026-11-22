# Prueba de punta a punta (navegador real)

Levanta el servidor real, con el motor real y el conector real de Shopify apuntando a un **servidor Shopify simulado**
(`tests/fake_shopify.py`), y recorre la interfaz con Playwright: registro, importar productos, conectar Shopify,
publicar como borrador, mostrar con confirmación, ocultar por falta de stock, volver a mostrar, credenciales revocadas,
secretos en reposo, CSP y 390 px en modo oscuro.

```bash
cd tests/e2e
export E2E_DIR=$(mktemp -d)      # directorio de trabajo NUEVO en cada ejecución (base de datos, clave, CSV, capturas)
mkdir -p "$E2E_DIR/e2e"
python3 launch.py &              # servidor en :8891 y panel de control del simulador en :8892
node e2e.js                      # requiere Node y Playwright (npm i playwright) con Chromium instalado
```

Sin `E2E_DIR` usa `tests/e2e/work/` (ignorado por Git). Usa siempre un directorio nuevo: la prueba empieza registrando
al primer propietario, así que una base de datos de una ejecución anterior la haría fallar. No usa internet ni una
tienda real.
