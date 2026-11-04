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
- **Conectores de proveedor** intercambiables (`backend/connectors/`). Hoy usa un proveedor simulado
  con 1.500 productos sintéticos; incluye un esqueleto para CJ Dropshipping.

## Requisitos
Python 3.10 o superior. No requiere dependencias externas.

## Ejecutar
```bash
cd backend && python3 server.py
```
Luego abre http://127.0.0.1:8000. La base SQLite (`backend/dropfinder.db`) se crea y se llena sola
la primera vez. Para empezar de cero, bórrala.

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
  connectors/      Interfaz SupplierConnector, proveedor simulado y esqueleto de CJ
frontend/          SPA en HTML, CSS y JS vanilla
docs/RAE-AutoDS.md Ingeniería inversa, análisis y especificación
tests/             Pruebas de la API
```
