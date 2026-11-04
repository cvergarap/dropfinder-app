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

## Pruebas
```bash
python3 -m unittest discover tests
```

## Estructura
```
backend/
  server.py        API REST + servidor de archivos estáticos
  db.py            Esquema SQLite, semilla y conexión
  metrics.py       Precio sugerido, ganancia, margen y score
  connectors/      Interfaz SupplierConnector: demo (mock.py), CSV (csv_connector.py) y esqueleto de CJ
frontend/          SPA en HTML, CSS y JS vanilla (+ plantilla-proveedor.csv)
docs/RAE-AutoDS.md Ingeniería inversa, análisis y especificación
tests/             Pruebas de la API
```
