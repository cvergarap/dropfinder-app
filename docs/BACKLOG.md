# Backlog de DropFinder

Ideas y trabajo pendiente, ordenado por prioridad dentro de cada bloque. Se actualiza al cerrar cada sprint.
Última revisión: tras el Sprint 7.

## BUGS — Defectos y fallos de experiencia abiertos

### BUG-001 — «Reglas de precio»: tras guardar no hay un paso siguiente claro  *(UX, prioridad media)*
**Reportado por:** el usuario, con captura de la pantalla «Reglas de precio» (tras el Sprint 7).
**Qué pasa.** Hay dos botones, «Guardar» (reglas de precio) y «Guardar moneda». Al pulsarlos solo aparece un texto gris
al lado («Guardado ✓ — las métricas se recalcularon» / «Guardado ✓»). No hay botón «Aceptar» ni «Volver», no se
sugiere adónde ir y el texto pasa desapercibido. La persona no sabe si terminó, qué efecto tuvo ni dónde verlo.
**Comportamiento esperado.**
- Confirmación visible (aviso emergente accesible con `aria-live`) que diga qué cambió y cuántos productos se recalcularon.
- Acciones claras tras guardar: «Ver productos» (lleva a Buscar productos) y «Seguir editando»; botón «Volver» o «Cancelar» que
  descarte los cambios sin guardar y restaure los valores guardados.
- Un solo flujo de guardado, o aviso explícito de que son dos formularios independientes (hoy cambiar una regla y la moneda
  exige pulsar dos botones y es fácil dejar uno sin guardar).
- Aviso al salir de la pestaña con cambios sin guardar.
- Aclarar la mezcla de unidades: la pantalla está en COP pero «Monto fijo» y «Comisión fija» siguen en USD (el texto lo
  dice en pequeño); mostrar el equivalente en la moneda elegida.
**Pasos para reproducir.** Menú «Reglas de precio» → cambiar cualquier valor → «Guardar» → no hay otra acción disponible.
**Criterio de aceptación.** Prueba E2E con navegador: guardar muestra confirmación, ofrece «Ver productos» y «Volver», y
«Cancelar» restaura los valores; sin inline styles/handlers; 390 px y modo oscuro.

### BUG-002 — «Proveedores»: no se pueden agregar ni editar proveedores de forma genérica (API, MCP o archivo)  *(funcional, prioridad alta)*
**Reportado por:** el usuario, con captura de la pestaña «Proveedores» (tras el Sprint 7).
**Qué pasa.** La pantalla lista AliExpress, Almacén Local, Amazon y CJ Dropshipping, pero son los catálogos **demo (ficticios)**
y la fila no ofrece ninguna acción: no se puede abrir, editar, conectar ni configurar un proveedor. Lo único disponible hoy es:
(a) el panel específico de «CJ Dropshipping (API)» (clave API), (b) «Cargar catálogo desde CSV» con un nombre de proveedor
escrito a mano y (c) «Eliminar productos de demostración». No existe un concepto de «proveedor» administrable: no hay alta,
edición, baja, estado de conexión ni elección del tipo de integración. AliExpress y Amazon no tienen conector real
(`SupplierConnector` solo implementado para demo, CSV y CJ).
**Comportamiento esperado.** Un registro de proveedores con alta, edición y baja, y por cada uno el tipo de integración:
- **API**: conector con credenciales cifradas (CJ ya existe; AliExpress Open Platform y Amazon PA-API quedan por hacer, con
  sus restricciones de acceso), estado de conexión, validar, importar y monitoreo de precio/stock.
- **MCP**: conectar un servidor MCP como fuente de catálogo (requiere diseño: seguridad, permisos y mapeo de herramientas
  al contrato `SupplierConnector`).
- **Archivo**: CSV/Excel con plantilla y **mapeo de columnas**, y recarga programada o manual asociada al proveedor guardado
  (hoy se vuelve a escribir el nombre cada vez).
- Datos propios del proveedor: nombre, país, moneda, tiempos de envío por defecto, notas, activo/inactivo.
- Convivencia clara de demo y real bajo el mismo nombre (hoy «CJ Dropshipping» demo y real comparten nombre; ver ADR 0004).
**Pasos para reproducir.** Menú «Proveedores» → intentar editar o configurar la fila AliExpress o Amazon → no hay acción posible.
**Por verificar antes de planear.** Qué ocurre exactamente al cargar un CSV con el nombre «AliExpress» o «Amazon» sobre el
catálogo demo (se mezclarían bajo el mismo proveedor; no está comprobado).
**Criterio de aceptación.** Se puede crear un proveedor de cada tipo (API, MCP, archivo), editarlo, verlo con su estado y
eliminarlo sin perder los productos importados por usuarios; pruebas con conectores simulados, permisos solo del propietario,
E2E con navegador y sin fugas de credenciales.

## B1 — Servicio público: registro, correo y recuperación de cuentas  *(interesa; sin fecha)*

**Contexto.** Hoy no hay ningún servicio de correo: la cuenta del propietario se crea desde el mismo PC, el propietario da de
alta a los miembros (sin registro abierto), el correo es solo un identificador de inicio de sesión que no se valida y no
existe «olvidé mi contraseña» (se arregla a mano en la base de datos). Es correcto para uso interno de un equipo; para
abrir la aplicación a cualquiera (como AutoDS) hace falta lo siguiente.

**Alcance propuesto (sprint propio):**
1. **Decisión de infraestructura (ADR nuevo).** Salir de SQLite + `http.server` hacia base de datos de servidor
   (PostgreSQL) y servidor de aplicación de producción detrás de proxy HTTPS; ver `docs/adr/0001-stack.md`. Decidir
   multi-inquilino: hoy el catálogo y las reglas son «de la instalación» y solo cambia el propietario; con registro
   público cada cliente necesita su propio espacio.
2. **Servicio de correo transaccional** (SMTP propio, Amazon SES, SendGrid, Resend…) con dominio propio, SPF y DKIM.
   Capa `mailer` intercambiable, con servidor de correo simulado para pruebas (igual que los conectores de tienda).
3. **Registro público** con verificación de correo: token de un solo uso, con caducidad y guardado con hash; cuenta
   inactiva hasta verificar; reenvío con límite.
4. **Recuperación de contraseña**: enlace de un solo uso con caducidad, respuesta idéntica exista o no la cuenta (sin
   enumeración de usuarios), cierre de las demás sesiones al cambiarla.
5. **Avisos por correo** opcionales: stock agotado, subida de costo, fallos de sincronización con tiendas, resumen
   del monitor. Preferencias por usuario.
6. **Protección contra abuso**: límites de envío por IP y por cuenta, CAPTCHA o equivalente en el registro, lista de
   dominios desechables, bloqueo escalonado.
7. **Planes y límites** (suscripción por tramos, como AutoDS: nº de productos importados/monitoreados), facturación.
8. **Legal y privacidad**: términos, política de datos personales (Colombia: Ley 1581 de 2012), borrado de cuenta y
   exportación de datos.

**Criterios de aceptación mínimos:** registro, verificación y recuperación probados de punta a punta con navegador real y
servidor de correo simulado; ningún token en claro en base de datos ni logs; sin enumeración de usuarios; límites de tasa
probados con mutaciones; migración atómica desde la base actual.

**Riesgos:** entregabilidad del correo (spam), costo del servicio de correo y de la base de datos, y el trabajo de
aislar por inquilino el catálogo y los ajustes que hoy son globales.

## B2 — Verificación contra servicios reales *(antes de usar con tiendas reales)*
- Probar Shopify, Tiendanube, WooCommerce y CJ Dropshipping con tienda de desarrollo y API key reales; corregir los
  supuestos listados en los encabezados de cada conector y en `docs/adr/0003-tiendanube-woocommerce.md` y `0004-proveedor-cj.md`.
- CJ: calcular el flete al importar (hoy envío 0 / 15 días y métricas de pedidos en 0, margen optimista).

## B3 — Deuda técnica y calidad
- Vigilar la prueba intermitente `MigrationV8Test` («database is locked» una vez en una corrida).
- Ampliar la E2E con navegador para cubrir la pestaña Rentabilidad y el panel de CJ.
- Si cambia `installation_id`, los listings ya publicados no se pueden borrar desde DropFinder (falla segura); persistir
  `orphan_external_id` (`docs/adr/0002-integracion-tiendas.md`).
- Limpiar las ramas remotas de trabajo ya fusionadas.

## B4 — Producto *(del análisis de competencia, `docs/analisis-competencia-y-mejoras.md`)*
- Cumplimiento automático de pedidos (webhooks `orders/create` → pedido al proveedor → tracking).
- Señales de demanda externas (TikTok/Facebook Ads) y productos curados.
- Calculadora: impuestos/IVA, cancelaciones previas al despacho y fletes por zona/peso.
- Más proveedores y tiendas (Amazon, AliExpress, eBay, Wix…).
