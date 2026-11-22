# API de tiendas (contrato compartido del Sprint 4-5)

Todas las rutas exigen sesión; los datos son del usuario (ajeno = 404). Errores: `{"error": "mensaje en español"}`.
Los errores 409 llevan un `code` estable para distinguir casos: `max_connections`, `duplicate_store`, `has_listings`,
`pending_deletes`, `credentials_unreadable`; los de tienda: `store_auth`, `store_validation`, `store_missing_scopes`,
`store_rate_limited`, `store_unreachable`. Todo texto que venga de la tienda se muestra siempre **escapado**.
Nunca se devuelve 401 por un fallo de la tienda (el navegador lo tomaría por sesión perdida): credenciales
rechazadas = 400, tienda caída = 502, límite de velocidad = 429.

## Conexiones (`backend/stores_mod.py`, ya implementado)
| Método y ruta | Descripción |
|---|---|
| `GET /api/stores/platforms` | `{items:[{platform,label,credential_fields:[{key,label,secret,help,required}],setup_help}], default_settings}` |
| `GET /api/stores` | `{items:[store], max}` |
| `POST /api/stores` | `{platform, domain, credentials:{…}, name?, settings?}` → store (valida contra la tienda; guarda cifrado) |
| `GET /api/stores/{id}` · `PATCH /api/stores/{id}` | `{name?, settings?, credentials?}` (rotar credenciales las valida) |
| `POST /api/stores/{id}/validate` | revalida y actualiza el estado |
| `DELETE /api/stores/{id}?confirm=1` | desconecta; los productos publicados **quedan en la tienda** |

`store` = `{id, platform, name, domain, status (active|error|disabled), last_error, currency, settings, credentials (enmascaradas), listings (cantidad), created_at, last_validated_at}`.

`settings` (por tienda): `publish_as_draft` (true), `sync_stock` (true), `hide_when_out_of_stock` (true),
`show_when_back_in_stock` (true), `auto_raise_price` (false), `min_margin_pct` (20), `max_price_increase_pct` (30).

## Publicación y sincronización (`backend/store_sync.py`)
| Método y ruta | Descripción |
|---|---|
| `POST /api/stores/{id}/publish` | `{imported_ids:[int] (máx. 50), as_draft?:bool, allow_demo?:bool}` → `{queued:[{listing_id, imported_id}], skipped:[{imported_id, reason}]}`. `reason`: `already_published`, `demo_not_allowed`, `currency_unsupported`, `not_found`, `store_error` |
| `GET /api/store-listings?imported_ids=1,2,3` | `{items:[listing]}` (máx. 200 ids) — para marcar "Mis productos" |
| `GET /api/stores/{id}/listings?status=&page=&page_size=` | `{items:[listing], total, page, page_size, pages}` |
| `POST /api/store-listings/{id}/sync` · `/hide` · `/show` · `/retry` | encola la acción → `{listing}` |
| `DELETE /api/store-listings/{id}?remove_remote=1` | quita el anuncio de DropFinder (y de la tienda si `remove_remote=1`) → `{ok, queued_remote_delete}` |
| `GET /api/stores/{id}/log?limit=50` | `{items:[{id, listing_id, action, ok, message, created_at}]}` |
| `POST /api/stores/sync` | procesa ya los trabajos vencidos del usuario → `{processed, failed}` |
| `GET /api/stores/queue` | `{pending, running, failed}` del usuario |

`listing` (`is_demo` sale de la columna del anuncio, no del producto: sobrevive a que se quite de Mis productos) = `{id, imported_id (puede ser null), connection_id, store_name, platform, title, status (pending|published|hidden|error|removed), external_url, last_price, last_stock, last_synced_at, last_error, pending_action (publish|sync|hide|show|delete|null), is_demo, currency, hidden_by_rule, created_at}`. Los precios (`last_price`) están en `currency`: la moneda en la que se publicó el anuncio.

## Reglas que el motor debe respetar
- Los productos de demostración (`source = 'mock'`) son ficticios: solo se publican con `allow_demo` explícito y **nunca**
  se sincronizan automáticamente.
- Publicar crea el producto como **borrador** salvo que la tienda o la petición digan otra cosa.
- Nunca se baja un precio automáticamente; subir precio solo con `auto_raise_price` y respetando `max_price_increase_pct`.
- Toda acción sobre la tienda queda en `store_sync_log`.

## Notas del motor de sincronización (aditivas; no rompen lo anterior)
- `skipped[]` puede traer además `message` (texto en español para mostrar). `listing.currency` es la moneda del precio del anuncio (guardada al publicar/sincronizar); `hidden_by_rule` (bool) indica que lo ocultó la regla de stock (solo esos se vuelven a mostrar solos).
- Un producto publicado **como borrador** queda con `status: "hidden"` (no visible en la tienda); "Mostrar" lo activa. Las reglas
  automáticas nunca lo hacen visible: solo vuelven a mostrar lo que ellas mismas ocultaron por falta de stock.
- `already_published` aplica a cualquier anuncio existente (también `error`/`removed`): para volver a publicarlo, `POST /api/store-listings/{id}/retry`.
- `retry`: `removed` o `error` sin id externo → nueva publicación; si el último trabajo falló → se repite; `error` con id externo → sincroniza. 409 si ya hay una acción en curso.
- Anuncios huérfanos (el usuario quitó el producto de "Mis productos"): `imported_id: null`; no se sincronizan solos; se pueden ocultar/mostrar/retirar,
  no `sync` (409).
- Rutas nuevas: `GET /api/stores/monitor-schedule` → `{minutes, allowed, last_run}` y `PUT /api/stores/monitor-schedule {minutes: 0|15|30|60|180|360|720|1440}` (solo propietario; 0 = apagado).
- `publish` responde 429 si el usuario encola más de 50 publicaciones en un minuto. `store_error` también se usa si la tienda está en estado `error`.
- Alerta nueva: `type: "store_margin_low"` (margen por debajo del mínimo tras subir el costo).
- **Acción manual nunca se pierde**: `sync`/`hide`/`show` responden 409 `action_in_progress` si ya hay una igual *en curso*; si hay una de regla *pendiente*, se convierte en manual. `retry` responde 409 solo si ya hay en curso la misma acción.
- **Fallos de ocultar/mostrar**: DropFinder conserva el estado anterior (no afirma "oculto" si la tienda pudo seguir visible), deja `last_error` y una alerta `store_visibility_failed`. `retry` lo vuelve a ejecutar aunque la regla ya no aplique.
- **Precio**: cuando el motor sube un precio por regla también actualiza el `sell_price` de Mis productos y crea la alerta `store_price_raised`. Un `sync` manual sí puede bajar el precio (es una acción explícita del usuario).
- **Cambio de moneda de la tienda tras publicar**: las reglas no empujan nada; el anuncio pasa a `error` ("La moneda de la tienda cambió (USD→COP)…") hasta un `sync` manual o volver a publicar.
- **Publicación interrumpida**: si no se sabe si el producto llegó a crearse (caída, o no se pudo guardar el resultado) el anuncio queda en `error` con aviso de revisar la tienda (SKU `DF-<id>`); solo los conectores con `idempotent_publish` reintentan solos.
- `POST /api/stores/sync` procesa como máximo **5** trabajos por petición (el hilo de fondo hace el resto). `GET /api/stores/queue`: `failed` cuenta solo fallos que siguen siendo el último trabajo de su anuncio.
- Ids fuera de rango (> 2^63) → 400 en `imported_ids` y 404 en rutas con id.
- El límite de 50 publicaciones por minuto se guarda por usuario y sobrevive a borrar anuncios.
- Un `show` manual de un producto sin stock vuelve a ocultarse por la regla si `hide_when_out_of_stock` está activa.
