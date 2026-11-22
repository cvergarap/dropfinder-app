# ADR 0001 — Stack tecnológico de DropFinder

- **Estado:** aceptada (Sprint 3, noviembre 2026)
- **Contexto de la decisión:** antes de agregar cuentas de usuario y las integraciones con tiendas (sprints 3 a 6), el análisis de mejoras (§4.1) pedía decidir si migrar a un framework web (FastAPI o Django) con PostgreSQL y una cola de tareas.

## Contexto

DropFinder hoy es Python 3 sin dependencias externas (`http.server` + SQLite con FTS5) y un frontend en HTML/CSS/JS sin frameworks. Corre en el PC con Windows del usuario. Datos medidos al cerrar el Sprint 2:

| Aspecto | Medición |
|---|---|
| Búsqueda con 100.000 productos | 3–11 ms por consulta |
| Pruebas automatizadas | 130 |
| Instalación | Python 3.10+, nada más |
| Usuarios simultáneos esperados | 1 a 5 (el vendedor y su equipo) |
| Trabajo en segundo plano | hilos *daemon* (historial, búsquedas guardadas, TRM) |

## Opciones evaluadas

| Opción | A favor | En contra |
|---|---|---|
| **A. Mantener stdlib + SQLite** | Cero instalación en Windows; ya cumple las metas de rendimiento; 130 pruebas sobre el código actual; despliegue = copiar la carpeta | Seguridad web (sesiones, CSRF, cabeceras) hecha a mano; un solo proceso; SQLite admite un escritor a la vez |
| B. FastAPI + PostgreSQL + cola (Celery/RQ) | Ecosistema maduro (autenticación, validación, OpenAPI); varios procesos; escala a hosting remoto | Requiere instalar PostgreSQL, Redis y dependencias en el PC del usuario; reescritura de ~3.000 líneas probadas; no mejora nada medible hoy |
| C. Django + PostgreSQL | Admin y autenticación incluidos | Las mismas instalaciones de B y una reescritura aún mayor |

## Decisión

**Se mantiene la opción A** (stdlib + SQLite) para los sprints 3 a 7, con estas reglas:

1. **Seguridad hecha con cuidado y probada:**
   - contraseñas con `hashlib.scrypt` y sal aleatoria;
   - sesiones con token aleatorio (`secrets`), cookie `HttpOnly` + `SameSite=Lax`, que se guardan solo como hash;
   - protección CSRF en toda petición que modifica datos;
   - límite de intentos de inicio de sesión;
   - cabeceras de seguridad (CSP, `X-Content-Type-Options`, `X-Frame-Options`).
   - **Parámetros fijos de scrypt:** `n=2**14`, `r=8`, `p=1`, sal de 16 bytes; la comparación se hace en tiempo constante (`hmac.compare_digest`). Lo mismo se aplica al token CSRF.
   - **Defensa contra DNS rebinding y CSRF:** se valida `Host` contra una lista permitida (`127.0.0.1`, `localhost` y los hosts que se configuren) y `Origin`/`Referer` cuando vienen.
   - **Detrás del proxy HTTPS:** la cookie se emite con `Secure` (prefijo `__Host-`) y la IP real del cliente se toma de `X-Forwarded-For` **solo** si la conexión viene del proxy configurado. Esto importa para el límite de intentos por IP.
   - **Correos en minúsculas** (Unicode, `casefold`) antes de guardarlos o compararlos.
2. **Por defecto se escucha solo en `127.0.0.1`.** Para exponer la app a internet hay que ponerla detrás de un proxy con HTTPS (Caddy o nginx); está documentado y la app no habla HTTPS por sí misma.
3. **El acceso a datos queda concentrado** en `db.py` y en los módulos de funcionalidad, para que una futura migración a PostgreSQL sea acotada. Las partes propias de SQLite están identificadas y se reemplazarían una por una: FTS5, `PRAGMA user_version` para el versionado del esquema, `COLLATE NOCASE`, `INSERT … ON CONFLICT` y las tablas `WITHOUT ROWID`.
4. **Las tareas largas** (sincronización con tiendas, monitor programado) usan hilos de fondo con lotes cortos y transacciones breves, como el historial del Sprint 2.

## Cuándo revisar esta decisión

Se migra a la opción B si se cumple **cualquiera** de estas condiciones:
- más de 5 usuarios trabajando a la vez, o la app se ofrece como servicio en internet a otros vendedores;
- catálogos de más de 1 millón de productos;
- las integraciones con tiendas necesitan reintentos y colas persistentes que los hilos no cubren bien (por ejemplo, miles de pedidos por día);
- un requisito de cumplimiento (auditoría, cifrado gestionado) que SQLite no permita.

## Consecuencias

- El usuario sigue instalando solo Python. Las cuentas de usuario sirven para un equipo pequeño que comparte una instalación.
- **Reglas de precio y catálogo compartidos por la instalación.** Las reglas de precio, el catálogo de proveedores y los cálculos de margen precalculados son de la instalación (los administra el rol *propietario*).
- **Datos de cada usuario.** Son propios de cada usuario: productos importados, alertas, búsquedas guardadas, colecciones y la moneda en que ve los precios.
- La seguridad web es responsabilidad del código propio: cada sprint que toque autenticación lleva su revisión de seguridad específica.
