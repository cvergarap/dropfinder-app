/* Módulo stores: conexión de tiendas (Shopify y las que vengan), publicación y sincronización.
 * Contrato de la API: docs/stores-api.md. Se registra en DF (ver app.js):
 *  - viewLoaders.stores: pestaña "Tiendas" (tarjetas, asistente de conexión, ajustes, productos publicados, registro).
 *  - importRowLead / importRowExtras / importActions / onImports / importRemoved: "Mis productos"
 *    (casillas, insignias de tiendas, botón "Publicar en tienda", publicación en lote).
 * Las funciones puras (parseDomainInput, formatStorePrice, planCredentialFields…) también se cargan en Node para pruebas
 * (tests/js/stores_helpers.test.js). Usa las globales de app.js: $, $$, esc, fmt, api, toast, money, show, DF.
 * Las credenciales solo viven en los <input> del diálogo: se vacían al terminar y nunca se guardan en variables. */
(function (root) {
  "use strict";

  /* ============================================================ funciones puras */

  const STATUS = {
    pending: ["Pendiente", "⏳"], published: ["Publicado", "✓"], hidden: ["Oculto · no visible para tus clientes", "◌"],
    error: ["Con error", "⚠"], removed: ["Quitado", "✕"],
  };
  const PENDING_ACTION = {
    publish: "Publicando…", sync: "Sincronizando…", hide: "Ocultando…", show: "Mostrando…", delete: "Quitando…",
  };
  const LOG_ACTION = {
    publish: "Publicar", sync: "Sincronizar", hide: "Ocultar", show: "Mostrar", delete: "Quitar",
    validate: "Revisar conexión", price: "Cambiar precio", stock: "Actualizar stock",
  };
  const SKIP_REASONS = {
    already_published: "Ya publicado",
    demo_not_allowed: "Producto de demostración",
    currency_unsupported: "Moneda de la tienda no soportada todavía",
    not_found: "No encontrado",
    store_error: "La tienda no respondió",
  };
  const MONITOR_CHOICES = [
    [0, "Apagado"], [15, "Cada 15 minutos"], [30, "Cada 30 minutos"], [60, "Cada hora"],
    [180, "Cada 3 horas"], [360, "Cada 6 horas"], [720, "Cada 12 horas"], [1440, "Cada 24 horas"],
  ];
  const GROUP_TITLES = {
    token: "Token de acceso", access_token: "Token de acceso", admin_token: "Token de acceso",
    app: "ID y secreto de la app", client: "ID y secreto de la app", client_credentials: "ID y secreto de la app",
    oauth: "ID y secreto de la app",
  };
  const MAX_BULK = 50;

  /* Dirección de la tienda: acepta enlaces pegados y devuelve solo el dominio (el servidor vuelve a normalizar). */
  function parseDomainInput(value, platform) {
    let s = String(value == null ? "" : value).trim();
    if (!s) return "";
    // Tiendanube se identifica por el ID NUMÉRICO de la tienda, que puede venir dentro de un enlace del panel: no se
    // reduce a un host (se perdería el ID). Se envía el texto tal cual (acotado) y el servidor extrae el número.
    if (platform === "tiendanube") return s.replace(/\s+/g, " ").slice(0, 300);
    const m = /^[a-z][a-z0-9+.-]*:\/\/(.*)$/i.exec(s);
    if (m) s = m[1]; else if (s.startsWith("//")) s = s.slice(2);
    const cut = s.search(/[/?#]/);
    let host = cut >= 0 ? s.slice(0, cut) : s;
    const path = cut >= 0 ? s.slice(cut) : "";
    host = host.replace(/^.*@/, "").toLowerCase().replace(/\.+$/, "");
    if (host === "admin.shopify.com") {            // https://admin.shopify.com/store/mitienda/products
      const p = /^\/store\/([a-z0-9][a-z0-9-]*)/i.exec(path);
      if (p) return `${p[1].toLowerCase()}.myshopify.com`;
    }
    return host;
  }

  /* Precio en la moneda DE LA TIENDA (no la de visualización del usuario). COP sin decimales. */
  function formatStorePrice(value, currency) {
    if (value === null || value === undefined || value === "" || !Number.isFinite(Number(value))) return "—";
    const code = /^[A-Za-z]{3}$/.test(String(currency || "")) ? String(currency).toUpperCase() : "USD";
    const d = code === "COP" ? 0 : 2;
    try {
      return new Intl.NumberFormat(code === "COP" ? "es-CO" : "es", {
        style: "currency", currency: code, minimumFractionDigits: d, maximumFractionDigits: d,
      }).format(Number(value));
    } catch { return `${Number(value).toFixed(d)} ${code}`; }
  }

  /* Solo enlaces https (el enlace al producto de la tienda viene del servidor: no se confía en él) */
  function safeHttpsUrl(u) {
    try {
      const url = new URL(String(u || ""));
      return url.protocol === "https:" ? url.href : "";
    } catch { return ""; }
  }

  /* Texto de ayuda de la plataforma → bloques seguros: listas numeradas y párrafos (nunca HTML) */
  function splitHelp(text) {
    const blocks = [];
    for (const raw of String(text || "").split(/\r?\n/)) {
      const line = raw.trim();
      if (!line) continue;
      const m = /^(?:\d+\s*[.)]|[-•*])\s+(.*)$/.exec(line);
      const last = blocks[blocks.length - 1];
      if (m) {
        if (last && last.type === "ol") last.items.push(m[1]);
        else blocks.push({ type: "ol", items: [m[1]] });
      } else blocks.push({ type: "p", text: line });
    }
    return blocks;
  }

  /* Campos de credenciales: obligatorios, y los opcionales agrupados en "formas" alternativas.
     modes: ≥2 grupos (por `group`) → el usuario elige una. flat: opcionales sueltos ("usa una de las dos formas"). */
  function planCredentialFields(fields) {
    const list = Array.isArray(fields) ? fields : [];
    const required = list.filter((f) => f.required !== false);
    const optional = list.filter((f) => f.required === false);
    const groups = [];
    for (const f of optional) if (f.group && !groups.includes(f.group)) groups.push(f.group);
    if (groups.length >= 2) {
      const modes = groups.map((g) => {
        const fs = optional.filter((f) => f.group === g);
        return { id: String(g), title: GROUP_TITLES[g] || fs.map((f) => f.label).join(" y "), fields: fs };
      });
      return { required, modes, flat: optional.filter((f) => !f.group) };
    }
    return { required, modes: [], flat: optional };
  }

  /* Pendientes + fallidos: lo que requiere atención (insignia de la pestaña) */
  function queueBadge(q) { return Math.max(0, (Number(q && q.pending) || 0) + (Number(q && q.failed) || 0)); }

  /* Acciones de fila según el estado del anuncio */
  function listingActions(l) {
    if (!l || l.pending_action) return [];
    if (l.imported_id === null) return ["remove"];       // huérfano: sin producto en Mis productos el servidor responde 409 no_product
    switch (l.status) {
      case "published": return ["sync", "hide", "remove"];
      case "hidden": return ["sync", "show", "remove"];
      case "error": return ["retry", "remove"];
      case "removed": return ["retry", "remove"];        // borrado a mano en la tienda: Reintentar = volver a publicar
      default: return ["sync", "remove"];
    }
  }

  const HIDDEN_HELP = "Los productos publicados como borrador empiezan en este estado («Borrador en tienda»): no se ven en tu tienda hasta que pulses «Mostrar».";
  /* short: texto corto para insignias en «Mis productos» (allí «Draft» ya significa otra cosa) */
  function statusLabel(l, short = false) {
    const st = l && l.status;
    let [label, icon] = STATUS[st] || [String(st || "—"), "•"];
    let help = "";
    if (st === "hidden") {
      if (l.hidden_by_rule) { label = "Oculto automáticamente (sin stock)"; help = "DropFinder lo ocultó porque el proveedor se quedó sin stock. Volverá a mostrarse si regresa el stock (según tus ajustes)."; }
      else { help = HIDDEN_HELP; if (short) label = "Borrador en tienda"; }
    }
    return { label, icon, help, pending: l && PENDING_ACTION[l.pending_action] ? PENDING_ACTION[l.pending_action] : "" };
  }

  /* Id seguro para atributos id/for/selectores a partir de la clave del campo */
  function credId(prefix, key) { return `${prefix}-${String(key).replace(/[^a-z0-9_-]/gi, "_")}`; }

  /* Riesgos al publicar un producto importado: margen negativo o stock 0 (si faltan los datos, no se avisa) */
  function publishRisks(r) {
    const out = [];
    if (r && (Number(r.margin) < 0 || Number(r.profit) < 0)) out.push("margen negativo (perderías dinero con cada venta)");
    if (r && r.stock !== null && r.stock !== undefined && r.stock !== "" && Number(r.stock) === 0) out.push("sin stock en el proveedor");
    return out;
  }

  function skipReasonText(r) { return SKIP_REASONS[r] || "No se pudo publicar"; }
  /* Texto de un omitido: primero el mensaje del servidor, si no el mapa local */
  function skipText(s) { return (s && typeof s.message === "string" && s.message.trim()) || skipReasonText(s && s.reason); }

  function fmtDateTime(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return String(iso);
    return d.toLocaleString("es-CO", { dateStyle: "medium", timeStyle: "short" });
  }

  const pure = {
    parseDomainInput, formatStorePrice, safeHttpsUrl, splitHelp, planCredentialFields, queueBadge, listingActions,
    statusLabel, skipReasonText, skipText, credId, publishRisks, fmtDateTime, MAX_BULK, SKIP_REASONS, MONITOR_CHOICES,
  };
  if (typeof module !== "undefined" && module.exports) { module.exports = pure; return; }

  /* ============================================================ navegador */

  const root$ = () => $("#view-stores");
  const isStoresActive = () => !!(root$() && root$().classList.contains("active"));
  const isImportsActive = () => !!($("#view-imports") && $("#view-imports").classList.contains("active"));
  const PAGE_SIZE = 20;

  const S = {
    platforms: null,       // [{platform,label,credential_fields,setup_help}]
    defaults: null,        // ajustes por defecto
    stores: null,          // [store] (null = aún no cargado)
    max: 10,
    sub: null,             // null | {kind:"listings", id, status, page, data} | {kind:"log", id, items}
    seq: 0,
    lastKey: "",           // evita redibujar (y perder el foco) si nada cambió
  };
  const storeById = (id) => (S.stores || []).find((s) => s.id === Number(id));
  const activeStores = () => (S.stores || []).filter((s) => s.status === "active");
  const platformInfo = (key) => (S.platforms || []).find((p) => p.platform === key);
  const platformLabel = (key) => (platformInfo(key) || {}).label || key;

  /* ---------------- utilidades de interfaz ---------------- */
  function errMessage(e) {
    if (!e) return "Ocurrió un error.";
    if (e.status === undefined) return "No se pudo conectar con DropFinder. Revisa tu conexión e inténtalo de nuevo.";
    let m = e.message || `Error ${e.status}`;
    if (e.code === "credentials_unreadable") {
      return `${m} No se pueden leer las credenciales guardadas (la clave de cifrado se perdió o cambió): vuelve a ingresarlas en Ajustes → «Cambiar credenciales».`;
    }
    if (e.code === "pending_deletes") return `${m} Hay eliminaciones pendientes en la tienda: espera un momento y vuelve a intentarlo.`;
    const retryable = ["store_rate_limited", "store_unreachable"].includes(e.code) || (!e.code && [429, 502, 503, 504].includes(e.status));
    if (retryable && !/reintent|intenta/i.test(m)) {
      m += " Puedes volver a intentarlo en unos segundos.";
    }
    return m;
  }
  function showError(el, e) {
    if (!el) return;
    el.textContent = typeof e === "string" ? e : errMessage(e);
    el.classList.remove("hidden");
  }
  const hideError = (el) => { if (el) { el.textContent = ""; el.classList.add("hidden"); } };

  /* Diálogo accesible: aria-labelledby, Escape, foco al abrir y de vuelta al cerrar, sin cerrar mientras trabaja */
  let dlgSeq = 0;
  function openModal(html, { cls = "", focus = "" } = {}) {
    const opener = document.activeElement;
    const dlg = document.createElement("dialog");
    const id = `stDlg${++dlgSeq}`;
    dlg.className = `st-dialog ${cls}`;
    dlg.setAttribute("aria-labelledby", `${id}t`);
    dlg.innerHTML = html.replace("%ID%", `${id}t`);
    document.body.appendChild(dlg);
    dlg.addEventListener("cancel", (e) => { if (dlg._busy) e.preventDefault(); });
    dlg.addEventListener("click", (e) => { if (e.target === dlg && !dlg._busy) dlg.close(); });
    dlg.addEventListener("close", () => {
      dlg.remove();
      if (opener && opener.isConnected && typeof opener.focus === "function") opener.focus();
    });
    dlg.showModal();
    const first = (focus && $(focus, dlg)) || $("input:not([type=hidden]):not(:disabled), select, textarea", dlg) || $("button", dlg);
    if (first) first.focus();
    return dlg;
  }
  function setBusy(dlg, busy, btn, busyText, idleText) {
    dlg._busy = busy;
    dlg.setAttribute("aria-busy", String(busy));
    $$("input, select, button, textarea", dlg).forEach((el) => {
      if (busy) { el._was = el.disabled; el.disabled = true; }
      else if (el._was !== undefined) { el.disabled = el._was; delete el._was; }
    });
    if (btn) btn.textContent = busy ? busyText : idleText;
  }

  /* Confirmación: devuelve {ok, checked} */
  function confirmDialog({ title, bodyHtml, confirmLabel, danger = false, checkbox = null, onOpen = null }) {
    return new Promise((resolve) => {
      const dlg = openModal(`<form method="dialog" class="st-form" novalidate>
        <h2 id="%ID%">${esc(title)}</h2>
        <div class="st-confirm-body">${bodyHtml}</div>
        ${checkbox ? `<label class="st-check"><input type="checkbox" name="extra"> <span>${esc(checkbox)}</span></label>` : ""}
        <div class="st-actions">
          <button type="button" class="ghost" data-cancel>Cancelar</button>
          <button type="submit" class="primary ${danger ? "st-danger" : ""}">${esc(confirmLabel)}</button>
        </div></form>`, { cls: "st-narrow", focus: "[data-cancel]" });
      let result = { ok: false, checked: false };
      if (onOpen) onOpen(dlg);
      $("[data-cancel]", dlg).addEventListener("click", () => dlg.close());
      $("form", dlg).addEventListener("submit", (e) => {
        e.preventDefault();
        result = { ok: true, checked: !!(checkbox && $("input[name=extra]", dlg).checked) };
        dlg.close();
      });
      dlg.addEventListener("close", () => resolve(result));
    });
  }

  function helpHtml(text) {
    return splitHelp(text).map((b) => (b.type === "ol"
      ? `<ol>${b.items.map((i) => `<li>${esc(i)}</li>`).join("")}</ol>`
      : `<p>${esc(b.text)}</p>`)).join("");
  }

  /* ---------------- datos ---------------- */
  async function loadPlatforms() {
    if (S.platforms) return S.platforms;
    const d = await api("/api/stores/platforms");
    S.platforms = d.items || [];
    S.defaults = d.default_settings || {};
    return S.platforms;
  }
  async function fetchStores() {
    const d = await api("/api/stores");
    S.stores = d.items || [];
    S.max = d.max || S.max;
    return S.stores;
  }
  async function ensureStores() { return S.stores || fetchStores(); }

  /* Después de conectar/cambiar/desconectar una tienda: refresca todo lo que depende de ellas */
  async function storesChanged() {
    try { await fetchStores(); } catch { /* se verá al abrir la pestaña */ }
    if (isStoresActive() && !S.sub) renderStores();
    if (isStoresActive() && S.sub && S.sub.kind === "listings") renderListings();
    syncPublishButtons();
    kick();
  }

  /* ============================================================ cola: sondeo cada 3 s */
  const Q = { timer: null, inflight: false, again: false, key: null, active: false };

  function setBadge(q) {
    const el = $("#storesBadge");
    if (!el) return;
    const n = queueBadge(q);
    el.textContent = String(n);
    el.classList.toggle("hidden", n === 0);
    el.title = n ? `${n} trabajo(s) pendientes o con error` : "";
  }

  function stopPolling() { clearTimeout(Q.timer); Q.timer = null; }
  const wantsPolling = () => !document.hidden && (isStoresActive() || isImportsActive());

  async function tick() {
    stopPolling();
    if (document.hidden) return;
    if (Q.inflight) { Q.again = true; return; }
    Q.inflight = true;
    let q = null;
    try { q = await api("/api/stores/queue"); } catch { /* sin servidor: se reintenta con la próxima acción */ }
    finally { Q.inflight = false; }
    if (q) {
      setBadge(q);
      const busy = (Number(q.pending) || 0) + (Number(q.running) || 0) > 0;
      const key = `${q.pending}/${q.running}/${q.failed}`;
      const first = Q.key === null;
      const refresh = !first && (key !== Q.key || (Q.active && !busy));
      Q.key = key;
      Q.active = busy;
      if (refresh) refreshViews();
    }
    // Se sigue solo mientras haya trabajo en curso y alguna vista que lo muestre esté abierta
    if (Q.again) { Q.again = false; Q.timer = setTimeout(tick, 0); }
    else if (Q.active && wantsPolling()) Q.timer = setTimeout(tick, 3000);
  }
  /* Revisa la cola ya (al abrir la pestaña o tras una acción) y sigue mientras haya trabajo */
  function kick() { tick(); }
  document.addEventListener("visibilitychange", () => { if (document.hidden) stopPolling(); else kick(); });

  function refreshViews() {
    if (isStoresActive()) {
      if (!S.sub) loadStores({ silent: true });
      else if (S.sub.kind === "listings") loadListings({ silent: true });
    }
    if (isImportsActive()) refreshBadges();
  }

  /* ============================================================ pestaña Tiendas */
  function ensureSkeleton() {
    const v = root$();
    if (!v || $("#stBody", v)) return;
    v.innerHTML = `<div class="section-head st-head">
        <div><h2>Tiendas</h2>
          <p class="muted">Publica tus productos en tu tienda y mantén precio y stock al día.</p></div>
        <div class="st-head-actions">
          <button type="button" class="ghost hidden" id="stSyncAll">↻ Sincronizar todo ahora</button>
          <button type="button" class="primary" id="stConnect">+ Conectar una tienda</button>
        </div></div>
      <div id="stBody" aria-live="polite"></div>`;
    $("#stConnect").addEventListener("click", () => openConnect());
    $("#stSyncAll").addEventListener("click", syncAll);
    v.addEventListener("click", onViewClick);
    v.addEventListener("change", onViewChange);
  }

  async function loadStores({ silent = false } = {}) {
    ensureSkeleton();
    const seq = ++S.seq;
    const body = $("#stBody");
    if (!silent) { S.sub = null; S.lastKey = ""; body.innerHTML = `<p class="muted st-loading">Cargando tiendas…</p>`; }
    try {
      await Promise.all([loadPlatforms().catch(() => null), fetchStores()]);
    } catch (e) {
      if (seq !== S.seq) return;
      if (!silent) body.innerHTML = `<div class="st-empty"><p class="bad" role="alert">${esc(errMessage(e))}</p>
        <button type="button" class="ghost" data-st="reload">Reintentar</button></div>`;
      return;
    }
    if (seq !== S.seq || S.sub) return;
    renderStores();
    syncPublishButtons();
  }

  function renderStores() {
    ensureSkeleton();
    const body = $("#stBody");
    const stores = S.stores || [];
    const key = JSON.stringify(["s", stores, S.max]);
    if (key === S.lastKey) return;
    S.lastKey = key;
    keepFocus(body, () => {
      $("#stSyncAll").classList.toggle("hidden", !stores.length);
      $("#stConnect").disabled = stores.length >= S.max;
      $("#stConnect").title = stores.length >= S.max ? `Máximo ${S.max} tiendas conectadas` : "";
      body.innerHTML = stores.length ? `<div class="st-grid">${stores.map(storeCard).join("")}</div>
        <p class="muted st-foot">${stores.length} de ${S.max} tiendas conectadas.</p>` : emptyState();
    });
  }

  function emptyState() {
    return `<div class="st-empty">
      <div class="st-empty-icon" aria-hidden="true">🛍️</div>
      <h3>Conecta tu tienda y vende desde DropFinder</h3>
      <p>Publica tus productos importados en tu tienda con un clic y mantén el <b>precio</b> y el <b>stock</b> sincronizados
        con el proveedor. Si un producto se agota, lo ocultamos por ti; nunca bajamos un precio sin que lo decidas.</p>
      <ul class="st-benefits">
        <li>Publicas como borrador y lo revisas antes de que sea visible.</li>
        <li>Tus credenciales se guardan cifradas y no se muestran nunca.</li>
        <li>Puedes desconectar cuando quieras: tus productos quedan en la tienda.</li>
      </ul>
      <button type="button" class="primary" data-st="connect">Conectar una tienda</button></div>`;
  }

  function storeStatusBadge(s) {
    const map = { active: ["ok", "✓", "Conectada"], error: ["err", "⚠", "Con error"], disabled: ["off", "⏸", "Deshabilitada"] };
    const [c, icon, label] = map[s.status] || ["off", "•", s.status];
    return `<span class="st-badge st-${c}"><span aria-hidden="true">${icon}</span> ${esc(label)}</span>`;
  }

  function storeCard(s) {
    const validated = s.last_validated_at ? esc(fmtDateTime(s.last_validated_at)) : "Sin revisar todavía";
    return `<article class="st-card" data-store="${s.id}" aria-label="Tienda ${esc(s.name)}">
      <header class="st-card-head">
        <div class="st-card-title"><h3>${esc(s.name)}</h3>
          <span class="muted">${esc(platformLabel(s.platform))} · ${esc(s.domain)}</span></div>
        ${storeStatusBadge(s)}</header>
      ${s.status === "error" ? `<p class="st-card-error"><b>Problema con la conexión:</b> ${esc(s.last_error || "No se pudo conectar con la tienda.")}
        <button type="button" class="ghost" data-st="validate" data-id="${s.id}" data-key="validate-${s.id}">Revisar conexión</button></p>` : ""}
      <dl class="st-meta">
        <div><dt>Moneda</dt><dd>${esc(s.currency || "—")}</dd></div>
        <div><dt>Productos publicados</dt><dd>${fmt(s.listings || 0)}</dd></div>
        <div><dt>Última revisión</dt><dd>${validated}</dd></div>
      </dl>
      <div class="st-card-actions">
        <button type="button" class="primary" data-st="listings" data-id="${s.id}" data-key="listings-${s.id}">Productos publicados</button>
        ${s.status === "active" ? `<button type="button" class="ghost" data-st="validate" data-id="${s.id}" data-key="validate-${s.id}">Revisar conexión</button>` : ""}
        <button type="button" class="ghost" data-st="settings" data-id="${s.id}" data-key="settings-${s.id}">Ajustes</button>
        <button type="button" class="ghost" data-st="log" data-id="${s.id}" data-key="log-${s.id}">Registro</button>
        <button type="button" class="link st-disconnect" data-st="disconnect" data-id="${s.id}" data-key="disconnect-${s.id}">Desconectar</button>
      </div></article>`;
  }

  /* Redibuja conservando el foco (los botones llevan data-key) */
  function keepFocus(container, render) {
    const a = document.activeElement;
    const key = a && container.contains(a) && a.dataset ? a.dataset.key : null;
    render();
    if (key) { const el = container.querySelector(`[data-key="${CSS.escape(key)}"]`); if (el && !el.disabled) el.focus(); }
  }

  async function validateStore(id, btn) {
    if (btn) btn.disabled = true;
    toast("Revisando la conexión con la tienda…");
    try {
      const s = await api(`/api/stores/${id}/validate`, { method: "POST" });
      toast(`Conexión correcta con ${s.name}`);
    } catch (e) {
      toast(errMessage(e));
      if (e.code === "credentials_unreadable") { await storesChanged(); offerRotate(id, e); return; }
    }
    await storesChanged();       // el servidor deja la tienda en estado "error" con su mensaje si falló
  }

  /* Credenciales ilegibles (clave de cifrado perdida/cambiada): hay que volver a ingresarlas */
  async function offerRotate(id, e) {
    const r = await confirmDialog({
      title: "Hay que volver a conectar las credenciales", confirmLabel: "Cambiar credenciales",
      bodyHtml: `<p>${esc(e.message || "")}</p><p>La clave de cifrado con que se guardaron las credenciales se perdió o cambió, así que DropFinder ya no puede leerlas.
        Ingresa de nuevo el token o las claves de tu tienda; tus productos publicados no se tocan.</p>`,
    });
    if (r.ok) openSettings(id, { openCreds: true });
  }

  async function disconnectStore(id) {
    const s = storeById(id);
    if (!s) return;
    const n = Number(s.listings) || 0;
    let live = n;                                    // «publicados» = publicados + ocultos (no los quitados ni con error)
    try {
      const [a, b] = await Promise.all(["published", "hidden"].map((st) => api(`/api/stores/${id}/listings?status=${st}&page_size=1`)));
      live = (a.total || 0) + (b.total || 0);
    } catch { /* se usa el total */ }
    const r = await confirmDialog({
      title: `¿Desconectar ${s.name}?`,
      confirmLabel: "Desconectar", danger: true,
      bodyHtml: live
        ? `<p>Tienes <b>${fmt(live)} producto${live === 1 ? "" : "s"} publicado${live === 1 ? "" : "s"}</b> en esta tienda.</p>
           <p><b>Seguirán en tu tienda</b> tal como están (con el último precio y stock), pero DropFinder dejará de sincronizarlos.
              Si quieres retirarlos, hazlo antes desde «Productos publicados».</p>`
        : `<p>DropFinder dejará de conectarse con esta tienda. Podrás volver a conectarla cuando quieras.</p>`,
    });
    if (!r.ok) return;
    try {
      await api(`/api/stores/${id}${n ? "?confirm=1" : ""}`, { method: "DELETE" });
      toast(n ? "Tienda desconectada. Los productos publicados siguen en la tienda." : "Tienda desconectada");
    } catch (e) {
      if (e.code === "has_listings" && !n) {      // la cifra local estaba desactualizada: confirmar y reintentar
        await storesChanged();
        return disconnectStore(id);
      }
      toast(errMessage(e));
    }
    await storesChanged();
  }

  async function syncAll(e) {
    const btn = e && e.currentTarget;
    if (btn) btn.disabled = true;
    try {
      const r = await api("/api/stores/sync", { method: "POST" });
      toast(`Sincronización lista: ${fmt(r.processed || 0)} procesados` + (r.failed ? `, ${fmt(r.failed)} con error` : ""));
    } catch (err) { toast(errMessage(err)); }
    finally { if (btn) btn.disabled = false; }
    refreshViews(); kick();
  }

  function onViewClick(e) {
    const b = e.target.closest("[data-st]");
    if (!b || b.disabled) return;
    const id = Number(b.dataset.id);
    switch (b.dataset.st) {
      case "connect": openConnect(); break;
      case "reload": loadStores(); break;
      case "validate": validateStore(id, b); break;
      case "settings": openSettings(id); break;
      case "disconnect": disconnectStore(id); break;
      case "listings": openListings(id); break;
      case "log": openLog(id); break;
      case "back": loadStores(); break;
      case "page": if (S.sub && S.sub.kind === "listings") { S.sub.page = Number(b.dataset.p); loadListings(); } break;
      case "row": rowAction(b.dataset.act, Number(b.dataset.lid), b); break;
      case "reload-sub": if (S.sub) (S.sub.kind === "log" ? openLog(S.sub.id) : loadListings()); break;
      default: break;
    }
  }
  function onViewChange(e) {
    if (e.target.matches("#stStatusFilter") && S.sub && S.sub.kind === "listings") {
      S.sub.status = e.target.value; S.sub.page = 1; loadListings();
    }
  }

  /* ============================================================ asistente de conexión */
  function fieldRow(f, prefix, hint) {
    const id = credId(prefix, f.key);
    const optional = f.required === false;
    return `<div class="st-field">
      <label for="${id}">${esc(f.label)}${optional ? ` <span class="st-opt">(opcional)</span>` : ""}</label>
      <div class="st-input-row">
        <input id="${id}" name="${esc(f.key)}" data-cred="${esc(f.key)}" type="${f.secret ? "password" : "text"}"
          autocomplete="off" autocapitalize="none" spellcheck="false" ${optional ? "" : 'aria-required="true"'}>
        ${f.secret ? `<button type="button" class="ghost st-reveal" data-for="${id}" data-label="${esc(f.label)}" aria-pressed="false"
          aria-label="Mostrar ${esc(f.label)}">Mostrar</button>` : ""}
      </div>
      ${hint ? `<small class="muted">${esc(hint)}</small>` : ""}
      ${f.help ? `<small class="muted">${esc(f.help)}</small>` : ""}</div>`;
  }

  /* HTML de las credenciales de una plataforma. `masked`: valores enmascarados actuales (solo informativo). */
  function credentialsHtml(platform, prefix, masked) {
    const plan = planCredentialFields(platform.credential_fields);
    const cur = (f) => (masked && masked[f.key] ? `Actual: ${masked[f.key]}` : "");
    let html = plan.required.map((f) => fieldRow(f, prefix, cur(f))).join("");
    if (plan.modes.length) {
      html += `<fieldset class="st-modes"><legend>Elige una de las formas de conectar</legend>
        ${plan.modes.map((m, i) => `<div class="st-mode">
          <label class="st-mode-head"><input type="radio" name="${prefix}-mode" value="${esc(m.id)}" ${i === 0 ? "checked" : ""}>
            <b>Opción ${i + 1}: ${esc(m.title)}</b></label>
          <fieldset class="st-mode-fields" data-mode="${esc(m.id)}" ${i === 0 ? "" : "disabled"}>
            <legend class="sr-only">${esc(m.title)}</legend>
            ${m.fields.map((f) => fieldRow({ ...f, required: true }, prefix, cur(f))).join("")}
          </fieldset></div>`).join("")}</fieldset>`;
    }
    if (plan.flat.length) {
      html += `<div class="st-flat"><p class="muted small">${plan.flat.length > 1 ? "Estos datos son opcionales: usa una de las dos formas que ofrece tu tienda." : "Dato opcional."}</p>
        ${plan.flat.map((f) => fieldRow(f, prefix, cur(f))).join("")}</div>`;
    }
    return html;
  }

  /* Lee las credenciales del formulario. Devuelve {creds} o {error}. */
  function readCredentials(form, platform, prefix) {
    const plan = planCredentialFields(platform.credential_fields);
    const val = (f) => {
      const el = form.querySelector(`#${CSS.escape(credId(prefix, f.key))}`);
      return el ? el.value.trim() : "";
    };
    const creds = {};
    for (const f of plan.required) {
      const v = val(f);
      if (!v) return { error: `Falta: ${f.label}` };
      creds[f.key] = v;
    }
    if (plan.modes.length) {
      const chosen = (form.querySelector(`input[name="${prefix}-mode"]:checked`) || {}).value;
      const m = plan.modes.find((x) => x.id === chosen) || plan.modes[0];
      for (const f of m.fields) {
        const v = val(f);
        if (!v) return { error: `Completa «${m.title}»: falta ${f.label}.` };
        creds[f.key] = v;
      }
    }
    for (const f of plan.flat) { const v = val(f); if (v) creds[f.key] = v; }
    return { creds };
  }

  /* Vacía los campos de credenciales y vuelve a ocultar los secretos (no se conservan tras usarlos) */
  function wipeCredentials(form) {
    $$("[data-cred]", form).forEach((i) => { i.value = ""; });
    $$(".st-reveal", form).forEach((b) => {
      const input = $(`#${CSS.escape(b.dataset.for)}`, form);
      if (input) input.type = "password";
      b.setAttribute("aria-pressed", "false"); b.textContent = "Mostrar";
      b.setAttribute("aria-label", `Mostrar ${b.dataset.label}`);
    });
  }

  /* Mostrar/ocultar secretos y cambio de opción: delegado en el diálogo */
  function bindCredentialUi(dlg) {
    dlg.addEventListener("click", (e) => {
      const b = e.target.closest(".st-reveal");
      if (!b) return;
      const input = $(`#${CSS.escape(b.dataset.for)}`, dlg);
      const showIt = input.type === "password";
      input.type = showIt ? "text" : "password";
      b.setAttribute("aria-pressed", String(showIt));
      b.textContent = showIt ? "Ocultar" : "Mostrar";
      b.setAttribute("aria-label", `${showIt ? "Ocultar" : "Mostrar"} ${b.dataset.label}`);
    });
    dlg.addEventListener("change", (e) => {
      if (!e.target.matches("input[type=radio][name$='-mode']")) return;
      const group = e.target.closest("fieldset.st-modes");
      $$(".st-mode-fields", group).forEach((fs) => { fs.disabled = fs.dataset.mode !== e.target.value; });
    });
  }

  /* Cómo pedir la dirección de cada plataforma. Lo demás (campos, ayuda, grupos) viene del servidor. */
  const PLATFORM_UI = {
    shopify: { label: "Dirección de la tienda", example: "mitienda.myshopify.com",
      hint: "Puedes pegar el enlace completo: nos quedamos con el dominio." },
    tiendanube: { label: "ID de la tienda", example: "1234567",
      hint: "Es el número de tu tienda. Puedes pegar un enlace de tu panel de Tiendanube que lo contenga." },
    woocommerce: { label: "Dirección de la tienda", example: "mitienda.com",
      hint: "Escribe el dominio EXACTO con el que carga tu tienda (con o sin www, como lo ves en el navegador): no se siguen redirecciones." },
  };
  const platformUi = (p) => PLATFORM_UI[p] || { label: "Dirección de la tienda", example: "mitienda.com",
    hint: "Puedes pegar el enlace completo: nos quedamos con el dominio." };
  const domainExample = (p) => platformUi(p).example;

  async function openConnect() {
    let platforms;
    try { platforms = await loadPlatforms(); }
    catch (e) { toast(errMessage(e)); return; }
    if (!platforms.length) { toast("Todavía no hay plataformas disponibles para conectar."); return; }
    const dlg = openModal(`<form class="st-form st-connect" novalidate autocomplete="off">
      <h2 id="%ID%">Conectar una tienda</h2>
      <div id="stcPlatforms"></div>
      <div id="stcPlatformBody"></div>
      <p class="st-error bad hidden" role="alert" id="stcError"></p>
      <div class="st-actions">
        <button type="button" class="ghost" data-cancel>Cancelar</button>
        <button type="submit" class="primary" id="stcGo">Conectar tienda</button>
      </div></form>`, { cls: "st-wide" });
    const form = $("form", dlg);
    const err = $("#stcError", dlg);
    let platform = platforms[0];
    bindCredentialUi(dlg);

    const renderPlatform = () => {
      hideError(err);
      $("#stcPlatformBody", dlg).innerHTML = `
        <details class="st-help" open><summary>Cómo conectar ${esc(platform.label)}</summary>
          <div class="st-help-body">${helpHtml(platform.setup_help) || "<p>Completa los datos que te pide la plataforma.</p>"}</div></details>
        <div class="st-field">
          <label for="stcDomain">${esc(platformUi(platform.platform).label)}</label>
          <input id="stcDomain" name="domain" type="text" inputmode="url" autocomplete="off" autocapitalize="none" spellcheck="false"
            placeholder="${esc(domainExample(platform.platform))}" aria-required="true" aria-describedby="stcDomainHint">
          <small class="muted" id="stcDomainHint">Ej.: ${esc(domainExample(platform.platform))}. ${esc(platformUi(platform.platform).hint)}</small>
        </div>
        <div class="st-field">
          <label for="stcName">Nombre para reconocerla <span class="st-opt">(opcional)</span></label>
          <input id="stcName" name="name" type="text" maxlength="80" autocomplete="off" placeholder="Si lo dejas vacío usamos el de la tienda">
        </div>
        <div class="st-creds">${credentialsHtml(platform, "stc")}</div>`;
    };
    if (platforms.length > 1) {
      $("#stcPlatforms", dlg).innerHTML = `<fieldset class="st-platforms"><legend>Plataforma</legend>
        ${platforms.map((p, i) => `<label class="st-plat"><input type="radio" name="platform" value="${esc(p.platform)}" ${i === 0 ? "checked" : ""}>
          <span>${esc(p.label)}</span></label>`).join("")}</fieldset>`;
      $("#stcPlatforms", dlg).addEventListener("change", (e) => {
        if (!e.target.matches("input[name=platform]")) return;
        platform = platforms.find((p) => p.platform === e.target.value) || platform;
        renderPlatform();
      });
    } else {
      $("#stcPlatforms", dlg).innerHTML = `<p class="muted">Plataforma: <b>${esc(platform.label)}</b></p>`;
    }
    renderPlatform();
    $("#stcDomain", dlg).focus();

    // Si pegan un enlace, se deja solo el dominio
    form.addEventListener("change", (e) => {
      if (e.target.id !== "stcDomain") return;
      e.target.value = parseDomainInput(e.target.value, platform.platform);
    });
    $("[data-cancel]", dlg).addEventListener("click", () => dlg.close());

    let sending = false;
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (sending) return;                                  // sin envíos dobles
      hideError(err);
      const domain = parseDomainInput($("#stcDomain", dlg).value, platform.platform);
      if (!domain) { showError(err, `Escribe ${platformUi(platform.platform).label === "ID de la tienda" ? "el ID" : "la dirección"} de tu tienda.`); $("#stcDomain", dlg).focus(); return; }
      $("#stcDomain", dlg).value = domain;
      const c = readCredentials(form, platform, "stc");
      if (c.error) { showError(err, c.error); return; }
      const body = { platform: platform.platform, domain, credentials: c.creds };
      const name = $("#stcName", dlg).value.trim();
      if (name) body.name = name;
      sending = true;
      const go = $("#stcGo", dlg);
      setBusy(dlg, true, go, "Validando con la tienda…", "Conectar tienda");
      try {
        const s = await api("/api/stores", { method: "POST", body });
        wipeCredentials(form);
        setBusy(dlg, false, go, "", "Conectar tienda");
        dlg.close();
        toast(`Tienda conectada: ${s.name}${s.currency ? ` (${s.currency})` : ""}`);
        await storesChanged();
      } catch (ex) {
        setBusy(dlg, false, go, "", "Conectar tienda");
        wipeCredentials(form);                      // tampoco se conserva el secreto tras un intento fallido
        showError(err, ex);
        (ex.status === 409 || ex.status === 400 ? $("#stcDomain", dlg) : go).focus();
      } finally { sending = false; }
    });
  }

  /* ============================================================ ajustes de la tienda */
  const SETTING_UI = [
    { key: "publish_as_draft", label: "Publicar como borrador", help: "Lo revisas en tu tienda antes de que sea visible para tus clientes." },
    { key: "sync_stock", label: "Mantener el stock al día", help: "Copia las existencias del proveedor a tu tienda." },
    { key: "hide_when_out_of_stock", label: "Ocultar si se agota", help: "Si el proveedor se queda sin stock, el producto se oculta en tu tienda." },
    { key: "show_when_back_in_stock", label: "Volver a mostrar cuando regrese el stock", help: "Si el producto estaba oculto por falta de stock, se muestra de nuevo." },
    { key: "auto_raise_price", label: "Subir el precio automáticamente", help: "Si sube el costo y tu margen cae por debajo del mínimo, se sube el precio. Nunca se baja el precio solo." },
  ];
  const NUM_UI = [
    { key: "min_margin_pct", label: "Margen mínimo (%)", min: 0, max: 95, step: 1, help: "Con la subida automática activa: si el margen cae por debajo de este valor, se sube el precio." },
    { key: "max_price_increase_pct", label: "Subida máxima por vez (%)", min: 1, max: 200, step: 1, help: "Tope de cada subida automática de precio, respecto al precio actual." },
  ];

  async function openSettings(id, { openCreds = false } = {}) {
    const s = storeById(id);
    if (!s) return;
    const st = s.settings || {};
    const owner = DF.isOwner();
    const platform = platformInfo(s.platform) || { credential_fields: [], label: s.platform };
    const dlg = openModal(`<div class="st-form st-settings">
      <h2 id="%ID%">Ajustes de ${esc(s.name)}</h2>
      <p class="muted">${esc(platform.label)} · ${esc(s.domain)} · ${esc(s.currency || "")}</p>
      <form id="stsForm" novalidate>
        <div class="st-field"><label for="stsName">Nombre</label>
          <input id="stsName" name="name" maxlength="80" value="${esc(s.name)}" autocomplete="off"></div>
        <fieldset class="st-group"><legend>Al publicar y sincronizar</legend>
          ${SETTING_UI.map((o) => `<label class="st-check st-setting"><input type="checkbox" name="${o.key}" ${st[o.key] ? "checked" : ""}>
            <span><b>${esc(o.label)}</b><small class="muted">${esc(o.help)}</small></span></label>`).join("")}
        </fieldset>
        <fieldset class="st-group st-nums" id="stsNums"><legend>Reglas de precio</legend>
          ${NUM_UI.map((o) => `<div class="st-field"><label for="sts-${o.key}">${esc(o.label)}</label>
            <input id="sts-${o.key}" name="${o.key}" type="number" inputmode="decimal" min="${o.min}" max="${o.max}" step="${o.step}"
              value="${esc(st[o.key] ?? "")}" aria-describedby="sts-${o.key}-h">
            <small class="muted" id="sts-${o.key}-h">${esc(o.help)} (${o.min}–${o.max})</small></div>`).join("")}
        </fieldset>
        <p class="st-error bad hidden" role="alert" id="stsError"></p>
        <p class="st-okmsg hidden" role="status" id="stsOk"></p>
        <div class="st-actions"><button type="submit" class="primary" id="stsSave">Guardar ajustes</button></div>
      </form>

      <details class="st-sub" id="stsCredBox"><summary>Cambiar credenciales</summary>
        <form id="stsCredForm" novalidate autocomplete="off">
          <p class="muted small">Úsalo si renovaste el token o las claves de la app en tu tienda. Se vuelven a validar antes de guardarlas.</p>
          ${credentialsHtml(platform, "stk", s.credentials)}
          <p class="st-error bad hidden" role="alert" id="stkError"></p>
          <p class="st-okmsg hidden" role="status" id="stkOk"></p>
          <div class="st-actions"><button type="submit" class="ghost" id="stkSave">Validar y guardar credenciales</button></div>
        </form></details>

      ${owner ? `<section class="st-sub" id="stsMonitor" aria-labelledby="stsMonTitle">
        <h3 id="stsMonTitle">Revisión automática (toda la instalación)</h3>
        <p class="muted small">Cada cuánto revisa DropFinder el costo y el stock de los proveedores y actualiza las tiendas conectadas.
          Los productos de demostración son ficticios y <b>nunca se sincronizan con tiendas reales</b>.</p>
        <form id="stsMonForm" class="st-inline" novalidate>
          <label class="sr-only" for="stsMonSel">Frecuencia de revisión</label>
          <select id="stsMonSel" disabled>${MONITOR_CHOICES.map(([v, l]) => `<option value="${v}">${esc(l)}</option>`).join("")}</select>
          <button type="submit" class="ghost" id="stsMonSave" disabled>Guardar frecuencia</button>
        </form>
        <p class="st-error bad hidden" role="alert" id="stmError"></p>
        <p class="st-okmsg hidden" role="status" id="stmOk"></p></section>` : ""}

      <div class="st-actions"><button type="button" class="ghost" data-cancel>Cerrar</button></div></div>`,
    { cls: "st-wide", focus: "#stsName" });
    bindCredentialUi(dlg);
    if (openCreds) { $("#stsCredBox", dlg).open = true; const f = $("#stsCredForm input", dlg); if (f) f.focus(); }
    $("[data-cancel]", dlg).addEventListener("click", () => dlg.close());

    const form = $("#stsForm", dlg);
    const syncNums = () => { $("#stsNums", dlg).classList.toggle("st-dim", !form.auto_raise_price.checked); };
    form.addEventListener("change", syncNums);
    syncNums();
    let saving = false;
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (saving) return;
      const err = $("#stsError", dlg), ok = $("#stsOk", dlg);
      hideError(err); hideError(ok);
      const settings = {};
      for (const o of SETTING_UI) settings[o.key] = form[o.key].checked;
      for (const o of NUM_UI) {
        const v = Number(form[o.key].value);
        if (form[o.key].value === "" || !Number.isFinite(v) || v < o.min || v > o.max) {
          showError(err, `${o.label}: escribe un número entre ${o.min} y ${o.max}.`); form[o.key].focus(); return;
        }
        settings[o.key] = v;
      }
      const name = form.name.value.trim();
      if (!name) { showError(err, "El nombre no puede estar vacío."); form.name.focus(); return; }
      saving = true;
      const btn = $("#stsSave", dlg);
      btn.disabled = true;
      try {
        await api(`/api/stores/${id}`, { method: "PATCH", body: { name, settings } });
        ok.textContent = "Ajustes guardados ✓"; ok.classList.remove("hidden");
        await storesChanged();
      } catch (ex) { showError(err, ex); }
      finally { saving = false; btn.disabled = false; }
    });

    const credForm = $("#stsCredForm", dlg);
    let rotating = false;
    credForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (rotating) return;
      const err = $("#stkError", dlg), ok = $("#stkOk", dlg);
      hideError(err); hideError(ok);
      const c = readCredentials(credForm, platform, "stk");
      if (c.error) { showError(err, c.error); return; }
      rotating = true;
      const btn = $("#stkSave", dlg);
      setBusy(dlg, true, btn, "Validando con la tienda…", "Validar y guardar credenciales");
      try {
        await api(`/api/stores/${id}`, { method: "PATCH", body: { credentials: c.creds } });
        wipeCredentials(credForm);
        ok.textContent = "Credenciales actualizadas y validadas ✓"; ok.classList.remove("hidden");
        setBusy(dlg, false, btn, "", "Validar y guardar credenciales");
        await storesChanged();
      } catch (ex) {
        setBusy(dlg, false, btn, "", "Validar y guardar credenciales");
        wipeCredentials(credForm);
        showError(err, ex);
      } finally { rotating = false; }
    });

    if (owner) bindMonitor(dlg);
  }

  /* Frecuencia del monitor (solo propietario). Respuesta tolerante: minutes | interval_minutes | every_minutes */
  async function bindMonitor(dlg) {
    const sel = $("#stsMonSel", dlg), save = $("#stsMonSave", dlg), err = $("#stmError", dlg), ok = $("#stmOk", dlg);
    const minutesOf = (d) => Number(d && (d.minutes ?? d.interval_minutes ?? d.every_minutes ?? 0)) || 0;
    try {
      const d = await api("/api/stores/monitor-schedule");
      const m = minutesOf(d);
      if (![...sel.options].some((o) => Number(o.value) === m)) sel.add(new Option(`Cada ${m} minutos`, String(m)));
      sel.value = String(m);
      sel.disabled = false; save.disabled = false;
    } catch (e) { showError(err, e.status === 404 ? "La revisión automática aún no está disponible en el servidor." : e); return; }
    $("#stsMonForm", dlg).addEventListener("submit", async (e) => {
      e.preventDefault();
      hideError(err); hideError(ok);
      save.disabled = true;
      try {
        await api("/api/stores/monitor-schedule", { method: "PUT", body: { minutes: Number(sel.value) } });
        ok.textContent = Number(sel.value) ? "Frecuencia guardada ✓" : "Revisión automática apagada ✓";
        ok.classList.remove("hidden");
      } catch (ex) { showError(err, ex); }
      finally { save.disabled = false; }
    });
  }

  /* ============================================================ productos publicados */
  async function openListings(id) {
    ensureSkeleton();
    S.sub = { kind: "listings", id, status: "", page: 1, data: null };
    S.lastKey = "";
    await loadListings();
  }

  async function loadListings({ silent = false } = {}) {
    const sub = S.sub;
    if (!sub || sub.kind !== "listings") return;
    const seq = ++S.seq;
    const body = $("#stBody");
    if (!silent) body.innerHTML = `<p class="muted st-loading">Cargando productos…</p>`;
    $("#stSyncAll").classList.remove("hidden");
    try {
      const qs = new URLSearchParams({ page: String(sub.page), page_size: String(PAGE_SIZE) });
      if (sub.status) qs.set("status", sub.status);
      const [data] = await Promise.all([api(`/api/stores/${sub.id}/listings?${qs}`), ensureStores().catch(() => null)]);
      if (seq !== S.seq || S.sub !== sub) return;
      if (data.pages && sub.page > data.pages) { sub.page = data.pages; return loadListings(); }
      sub.data = data;
    } catch (e) {
      if (seq !== S.seq || S.sub !== sub) return;
      if (!silent) body.innerHTML = `<div class="st-empty"><p class="bad" role="alert">${esc(errMessage(e))}</p>
        <button type="button" class="ghost" data-st="back">← Volver a Tiendas</button>
        <button type="button" class="ghost" data-st="reload-sub">Reintentar</button></div>`;
      return;
    }
    renderListings();
  }

  const STATUS_FILTERS = [["", "Todos"], ["pending", "Pendientes"], ["published", "Publicados"], ["hidden", "Ocultos"],
    ["error", "Con error"], ["removed", "Quitados"]];

  function statusBadgeHtml(l) {
    const s = statusLabel(l);
    return `<span class="st-badge st-l-${esc(l.status)}" ${s.help ? `title="${esc(s.help)}"` : ""}><span aria-hidden="true">${s.icon}</span> ${esc(s.label)}</span>` +
      (s.pending ? ` <span class="st-pending muted">${esc(s.pending)}</span>` : "") +
      (s.help ? `<small class="st-help-line muted">${esc(s.help)}</small>` : "");
  }

  const ACTION_BTN = {
    sync: ["Sincronizar", "ghost"], hide: ["Ocultar", "ghost"], show: ["Mostrar", "ghost"],
    retry: ["Reintentar", "ghost"], remove: ["Quitar", "link st-remove"],
  };

  function listingRow(l, store) {
    const url = safeHttpsUrl(l.external_url);
    const acts = store && store.status === "disabled" ? listingActions(l).filter((a) => a === "remove") : listingActions(l);
    const orphan = l.imported_id === null || l.imported_id === undefined;
    return `<tr data-lid="${l.id}">
      <td class="st-title"><span class="st-title-text">${esc(l.title)}</span>
        ${orphan ? `<small class="st-orphan">Quitado de Mis productos</small>` : ""}
        ${l.is_demo ? `<small class="st-demo">Producto de demostración (ficticio)</small>` : ""}
        ${l.last_error ? `<small class="st-row-error bad">${esc(l.last_error)}</small>` : ""}</td>
      <td>${statusBadgeHtml(l)}</td>
      <td class="num">${esc(formatStorePrice(l.last_price, store && store.currency))}</td>
      <td class="num">${l.last_stock === null || l.last_stock === undefined ? "—" : fmt(l.last_stock)}</td>
      <td>${l.last_synced_at ? esc(fmtDateTime(l.last_synced_at)) : "—"}</td>
      <td>${url ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">Ver en la tienda<span class="sr-only"> (abre otra pestaña)</span> ↗</a>` : "—"}</td>
      <td class="st-row-actions">${acts.length ? acts.map((a) => `<button type="button" class="${ACTION_BTN[a][1]}" data-st="row" data-act="${a}"
        data-lid="${l.id}" data-key="${a}-${l.id}" aria-label="${ACTION_BTN[a][0]}: ${esc(l.title)}">${ACTION_BTN[a][0]}</button>`).join("") :
        `<span class="muted">${l.pending_action ? "En curso…" : ""}</span>`}</td></tr>`;
  }

  function renderListings() {
    const sub = S.sub;
    if (!sub || sub.kind !== "listings" || !sub.data) return;
    const store = storeById(sub.id);
    const d = sub.data;
    const key = JSON.stringify(["l", sub.id, sub.status, sub.page, d, store && store.currency, store && store.name]);
    if (key === S.lastKey) return;
    S.lastKey = key;
    const body = $("#stBody");
    keepFocus(body, () => {
      const items = d.items || [];
      body.innerHTML = `<div class="st-sub-head">
          <button type="button" class="link st-back" data-st="back">← Tiendas</button>
          <h3>Productos publicados en ${esc(store ? store.name : "la tienda")}</h3></div>
        <div class="st-filter"><label for="stStatusFilter">Estado</label>
          <select id="stStatusFilter">${STATUS_FILTERS.map(([v, l]) => `<option value="${v}" ${v === sub.status ? "selected" : ""}>${l}</option>`).join("")}</select>
          <span class="muted">${fmt(d.total || 0)} producto${d.total === 1 ? "" : "s"}</span></div>
        ${items.length ? `<div class="table-wrap"><table class="table st-table">
          <caption class="sr-only">Productos publicados en ${esc(store ? store.name : "la tienda")}</caption>
          <thead><tr><th>Producto</th><th>Estado</th><th class="num">Precio (${esc(store ? store.currency : "")})</th><th class="num">Stock</th>
            <th>Última sincronización</th><th>En la tienda</th><th><span class="sr-only">Acciones</span></th></tr></thead>
          <tbody>${items.map((l) => listingRow(l, store)).join("")}</tbody></table></div>`
          : `<p class="empty">${sub.status ? "No hay productos con ese estado." : "Aún no publicaste productos en esta tienda. Ve a <b>Mis productos</b> y usa «Publicar en tienda»."}</p>`}
        ${d.pages > 1 ? `<div class="pager">
          <button type="button" class="ghost" data-st="page" data-p="${d.page - 1}" ${d.page <= 1 ? "disabled" : ""}>← Anterior</button>
          <span class="muted">Página ${d.page} de ${d.pages}</span>
          <button type="button" class="ghost" data-st="page" data-p="${d.page + 1}" ${d.page >= d.pages ? "disabled" : ""}>Siguiente →</button></div>` : ""}`;
    });
  }

  async function rowAction(act, lid, btn) {
    const l = S.sub && S.sub.data && S.sub.data.items.find((x) => x.id === lid);
    if (!l) return;
    if (act === "remove") return removeListing(l);
    if (act === "show") {
      const r = await confirmDialog({ title: "¿Mostrar este producto?", confirmLabel: "Mostrar", bodyHtml:
        `<p><b>${esc(l.title)}</b></p><p>Esto hará visible el producto a tus clientes ahora. ¿Continuar?</p>` });
      if (!r.ok) return;
    }
    if (btn) btn.disabled = true;
    try {
      const r = await api(`/api/store-listings/${lid}/${act}`, { method: "POST" });
      if (r && r.listing && S.sub && S.sub.data) {
        const i = S.sub.data.items.findIndex((x) => x.id === lid);
        if (i >= 0) S.sub.data.items[i] = r.listing;
        renderListings();
      }
      toast({ sync: "Sincronización en cola", hide: "Se ocultará en la tienda", show: "Se mostrará en la tienda", retry: "Reintentando" }[act]);
    } catch (e) { toast(errMessage(e)); if (btn) btn.disabled = false; }
    Q.active = true;
    afterListingChange();
  }

  async function removeListing(l) {
    const store = storeById(l.connection_id);
    const r = await confirmDialog({
      title: "¿Quitar este producto?", confirmLabel: "Quitar", danger: true,
      checkbox: "También eliminar de la tienda",
      bodyHtml: `<p><b>${esc(l.title)}</b></p>
        <p>DropFinder dejará de sincronizarlo. Si no marcas la casilla, <b>el producto sigue en ${esc(store ? store.name : "tu tienda")}</b> tal como está.</p>`,
    });
    if (!r.ok) return;
    try {
      const res = await api(`/api/store-listings/${l.id}${r.checked ? "?remove_remote=1" : ""}`, { method: "DELETE" });
      toast(res && res.queued_remote_delete ? "Quitado; se eliminará de la tienda en unos momentos" : "Producto quitado de DropFinder");
    } catch (e) { toast(errMessage(e)); }
    await fetchStores().catch(() => null);
    afterListingChange();
  }

  function afterListingChange() {
    if (S.sub && S.sub.kind === "listings") loadListings({ silent: true });
    if (isImportsActive()) refreshBadges();
    kick();
  }

  /* ============================================================ registro */
  async function openLog(id) {
    ensureSkeleton();
    S.sub = { kind: "log", id, items: null };
    S.lastKey = "";
    const seq = ++S.seq;
    const body = $("#stBody");
    body.innerHTML = `<p class="muted st-loading">Cargando registro…</p>`;
    $("#stSyncAll").classList.remove("hidden");
    try {
      const [d] = await Promise.all([api(`/api/stores/${id}/log?limit=50`), ensureStores().catch(() => null)]);
      if (seq !== S.seq || !S.sub || S.sub.kind !== "log") return;
      S.sub.items = d.items || [];
    } catch (e) {
      if (seq !== S.seq) return;
      body.innerHTML = `<div class="st-empty"><p class="bad" role="alert">${esc(errMessage(e))}</p>
        <button type="button" class="ghost" data-st="back">← Volver a Tiendas</button>
        <button type="button" class="ghost" data-st="reload-sub">Reintentar</button></div>`;
      return;
    }
    const store = storeById(id);
    const items = S.sub.items;
    body.innerHTML = `<div class="st-sub-head">
        <button type="button" class="link st-back" data-st="back">← Tiendas</button>
        <h3>Registro de ${esc(store ? store.name : "la tienda")}</h3></div>
      <p class="muted">Lo que DropFinder hizo en tu tienda (últimas ${items.length} acciones).</p>
      ${items.length ? `<ul class="st-log">${items.map((it) => `<li class="${it.ok ? "ok" : "fail"}">
        <span class="st-log-icon" aria-hidden="true">${it.ok ? "✓" : "✗"}</span>
        <div class="st-log-main"><b>${it.ok ? "Correcto" : "Falló"}</b> · ${esc(LOG_ACTION[it.action] || it.action)}
          ${it.listing_id ? `<span class="muted"> · producto #${esc(it.listing_id)}</span>` : ""}
          <span class="st-log-msg">${esc(it.message || "")}</span></div>
        <time class="muted" datetime="${esc(it.created_at)}">${esc(fmtDateTime(it.created_at))}</time></li>`).join("")}</ul>`
        : `<p class="empty">Todavía no hay actividad en esta tienda.</p>`}`;
  }

  /* ============================================================ Mis productos */
  const byImport = new Map();         // imported_id → [listing]
  const selected = new Set();         // imported_id seleccionados para publicar en lote

  const hasActiveStore = () => (S.stores ? activeStores().length > 0 : null);
  const importRows = () => (typeof lastImports !== "undefined" && Array.isArray(lastImports) ? lastImports : []);
  const importById = (id) => importRows().find((r) => r.id === Number(id));

  function badgesHtml(importedId) {
    const ls = byImport.get(Number(importedId)) || [];
    return ls.map((l) => {
      const s = statusLabel(l, true);
      return `<span class="st-badge st-l-${esc(l.status)}" title="${esc(l.last_error || s.help || `${l.store_name}: ${s.label}`)}"><span aria-hidden="true">${s.icon}</span> ${esc(l.store_name || "Tienda")} · ${esc(s.pending || s.label)}</span>`;
    }).join("");
  }

  DF.importRowLead.push((r) => `<input type="checkbox" class="st-sel" data-id="${r.id}" ${selected.has(r.id) ? "checked" : ""}
    aria-label="Seleccionar ${esc(r.title)}">`);
  DF.importRowExtras.push((r) => `<div class="st-badges" data-st-badges="${r.id}">${badgesHtml(r.id)}</div>`);
  DF.importActions.push((r) => {
    const off = hasActiveStore() === false;
    return `<button type="button" class="ghost st-publish ${off ? "st-off" : ""}" data-id="${r.id}"
      ${off ? `aria-disabled="true" title="${esc(noStoreHint())}"` : ""}>Publicar en tienda</button>`;
  });

  function noStoreHint() {
    if (S.stores && S.stores.length && !activeStores().length) return "Tu tienda tiene un problema de conexión: revísala en la pestaña Tiendas.";
    return "Primero conecta una tienda en la pestaña Tiendas.";
  }

  function syncPublishButtons() {
    const off = hasActiveStore() === false;
    $$("#importsBody .st-publish").forEach((b) => {
      b.classList.toggle("st-off", off);
      if (off) { b.setAttribute("aria-disabled", "true"); b.title = noStoreHint(); }
      else { b.removeAttribute("aria-disabled"); b.removeAttribute("title"); }
    });
    updateBulkBar();
  }

  /* Trae las publicaciones de las filas visibles (en lotes de 200) y dibuja las insignias */
  let badgeSeq = 0;
  async function refreshBadges() {
    const ids = importRows().map((r) => r.id);
    const seq = ++badgeSeq;
    if (!ids.length) return;
    const found = new Map();
    try {
      for (let i = 0; i < ids.length; i += 200) {
        const r = await api(`/api/store-listings?imported_ids=${ids.slice(i, i + 200).join(",")}`);
        for (const l of r.items || []) {
          if (l.imported_id === null || l.imported_id === undefined) continue;
          if (!found.has(l.imported_id)) found.set(l.imported_id, []);
          found.get(l.imported_id).push(l);
        }
      }
    } catch { return; }       // sin insignias: la tabla sigue funcionando
    if (seq !== badgeSeq) return;
    ids.forEach((id) => { if (found.has(id)) byImport.set(id, found.get(id)); else byImport.delete(id); });
    $$("#importsBody [data-st-badges]").forEach((el) => { el.innerHTML = badgesHtml(el.dataset.stBadges); });
    if ([...found.values()].some((ls) => ls.some((l) => l.pending_action || l.status === "pending"))) kick();
  }

  DF.onImports.push((rows) => {
    const ids = new Set(rows.map((r) => r.id));
    [...selected].forEach((id) => { if (!ids.has(id)) selected.delete(id); });
    ensureBulkBar();
    updateBulkBar();
    if (!rows.length) return;
    if (!S.stores) ensureStores().then(syncPublishButtons).catch(() => null);
    refreshBadges();
    kick();
  });
  DF.beforeImportRemove.push(async (id) => {
    const live = (byImport.get(id) || []).filter((l) => l.status !== "removed");
    if (!live.length) return true;
    const r = await confirmDialog({
      title: "Este producto está publicado en tu tienda", confirmLabel: "Quitar de Mis productos", danger: true,
      bodyHtml: `<p>Está publicado en ${esc([...new Set(live.map((l) => l.store_name))].join(", "))}. Si lo quitas de Mis productos,
        <b>seguirá en tu tienda</b> pero DropFinder dejará de sincronizarlo: si luego se agota, <b>no se ocultará solo</b>.</p>
        <p>Puedes retirarlo antes desde <button type="button" class="link" data-go-listings>Tiendas → Productos publicados</button>.</p>`,
      onOpen: (dlg) => $("[data-go-listings]", dlg).addEventListener("click", () => {
        dlg.close(); show("stores"); setTimeout(() => openListings(live[0].connection_id), 0);
      }),
    });
    return r.ok;
  });
  DF.importRemoved.push((id) => {
    const n = (byImport.get(id) || []).filter((l) => l.status !== "removed").length;
    byImport.delete(id); selected.delete(id);
    return n ? `Tenía ${n} anuncio${n === 1 ? "" : "s"} en tienda: ${n === 1 ? "sigue" : "siguen"} allí. Encuéntra${n === 1 ? "lo" : "los"} en Tiendas → Productos publicados.` : "";
  });

  /* ---- barra de selección en lote ---- */
  function ensureBulkBar() {
    if ($("#stBulkBar")) return;
    const wrap = $("#view-imports .table-wrap");
    if (!wrap) return;
    const bar = document.createElement("div");
    bar.id = "stBulkBar";
    bar.className = "st-bulk hidden";
    bar.setAttribute("role", "region");
    bar.setAttribute("aria-label", "Acciones sobre los productos seleccionados");
    bar.innerHTML = `<span id="stBulkCount" role="status" aria-live="polite"></span>
      <button type="button" class="primary" id="stBulkPublish">Publicar seleccionados</button>
      <button type="button" class="link" id="stBulkClear">Quitar selección</button>`;
    wrap.parentElement.insertBefore(bar, wrap);
    $("#stBulkClear").addEventListener("click", () => {
      selected.clear();
      $$("#importsBody .st-sel").forEach((c) => { c.checked = false; });
      updateBulkBar();
    });
    const all = $("#importsSelectAll");
    if (all) {
      all.disabled = false;
      all.addEventListener("change", () => {
        $$("#importsBody .st-sel").forEach((c) => {
          c.checked = all.checked;
          if (all.checked) selected.add(Number(c.dataset.id)); else selected.delete(Number(c.dataset.id));
        });
        updateBulkBar();
      });
    }
  }
  function updateBulkBar() {
    const bar = $("#stBulkBar");
    if (!bar) return;
    const n = selected.size;
    bar.classList.toggle("hidden", n === 0);
    const over = n > MAX_BULK;
    $("#stBulkCount").textContent = over ? `${n} seleccionados: el máximo por vez es ${MAX_BULK}. Quita algunos.`
      : `${n} seleccionado${n === 1 ? "" : "s"}`;
    const b = $("#stBulkPublish");
    const off = hasActiveStore() === false;
    b.classList.toggle("st-off", off);
    b.disabled = over;
    if (off) { b.setAttribute("aria-disabled", "true"); b.title = noStoreHint(); }
    else { b.removeAttribute("aria-disabled"); b.removeAttribute("title"); }
    const all = $("#importsSelectAll");
    if (all) {
      const boxes = $$("#importsBody .st-sel");
      const c = boxes.filter((x) => x.checked).length;
      all.checked = boxes.length > 0 && c === boxes.length;
      all.indeterminate = c > 0 && c < boxes.length;
    }
  }

  document.addEventListener("click", (e) => {
    const t = e.target;
    if (!(t instanceof Element)) return;
    const pub = t.closest("#importsBody .st-publish");
    if (pub) {
      e.stopPropagation();
      if (pub.getAttribute("aria-disabled") === "true") { toast(noStoreHint()); show("stores"); return; }
      openPublish([Number(pub.dataset.id)]);
      return;
    }
    const bulk = t.closest("#stBulkPublish");
    if (bulk && !bulk.disabled) {
      if (bulk.getAttribute("aria-disabled") === "true") { toast(noStoreHint()); show("stores"); return; }
      openPublish([...selected]);
    }
  }, true);
  document.addEventListener("change", (e) => {
    const c = e.target;
    if (c instanceof Element && c.matches("#importsBody .st-sel")) {
      if (c.checked) selected.add(Number(c.dataset.id)); else selected.delete(Number(c.dataset.id));
      updateBulkBar();
    }
  });

  /* ---- diálogo "Publicar en tienda" ---- */
  async function openPublish(ids) {
    try { await ensureStores(); } catch (e) { toast(errMessage(e)); return; }
    const stores = activeStores();
    if (!stores.length) { toast(noStoreHint()); show("stores"); return; }
    const rows = ids.map(importById).filter(Boolean);
    if (!rows.length) return;
    const demo = rows.filter((r) => r.source === "mock");
    const allDemo = demo.length === rows.length;
    const riskRows = rows.map((r) => [r, publishRisks(r)]).filter(([, rs]) => rs.length);
    const single = rows.length === 1;
    const dlg = openModal(`<form class="st-form st-publish-form" novalidate>
      <h2 id="%ID%">${single ? "Publicar en tienda" : `Publicar ${rows.length} productos en tienda`}</h2>
      ${single ? `<p class="st-pub-title"><b>${esc(rows[0].title)}</b></p>`
        : `<ul class="st-pub-list">${rows.slice(0, 5).map((r) => `<li>${esc(r.title)}</li>`).join("")}${rows.length > 5 ? `<li class="muted">y ${rows.length - 5} más…</li>` : ""}</ul>`}
      <div class="st-field"><label for="stpStore">Tienda</label>
        <select id="stpStore">${stores.map((s) => `<option value="${s.id}">${esc(s.name)} (${esc(s.currency)})</option>`).join("")}</select></div>
      <label class="st-check st-setting"><input type="checkbox" id="stpDraft">
        <span><b>Publicar como borrador</b><small class="muted">Lo revisas en tu tienda antes de que sea visible para tus clientes.</small></span></label>
      <p class="note st-price-note" id="stpPrice"></p>
      ${riskRows.length ? `<div class="st-warn" role="note"><b>⚠ ${single ? "Ojo con este producto" : `${riskRows.length} de los productos tienen problemas`}</b>
        <ul>${riskRows.map(([r, rs]) => `<li>${single ? "" : `<b>${esc(r.title)}</b>: `}${esc(rs.join(" y "))}</li>`).join("")}</ul>
        <label class="st-check"><input type="checkbox" id="stpRisk"> <span>Publicar de todos modos</span></label></div>` : ""}
      ${demo.length ? `<div class="st-warn" role="note"><b>⚠ ${allDemo ? (single ? "Producto de demostración: es ficticio." : "Son productos de demostración: son ficticios.") : `${demo.length} de los productos son de demostración (ficticios).`}</b>
        <p>No existen en ningún proveedor y no se sincronizarán nunca. Úsalos solo para probar tu tienda.</p>
        <label class="st-check"><input type="checkbox" id="stpDemo"> <span>${allDemo ? "Entiendo que es ficticio y quiero publicarlo igual" : "Publicar también los de demostración (si no marcas, se omiten)"}</span></label></div>` : ""}
      <p class="st-error bad hidden" role="alert" id="stpError"></p>
      <div id="stpResult" class="st-result hidden" role="status"></div>
      <div class="st-actions">
        <button type="button" class="ghost" data-cancel>Cancelar</button>
        <button type="submit" class="primary" id="stpGo">Publicar</button>
      </div></form>`, { cls: "st-narrow", focus: "#stpStore" });
    const form = $("form", dlg);
    const err = $("#stpError", dlg), go = $("#stpGo", dlg);
    const storeSel = $("#stpStore", dlg);
    const demoBox = $("#stpDemo", dlg);
    const currentStore = () => storeById(storeSel.value);
    const refresh = () => {
      const s = currentStore();
      $("#stpDraft", dlg).checked = !!(s && s.settings && s.settings.publish_as_draft);
      const cur = s ? ` (${s.currency})` : "";
      $("#stpPrice", dlg).textContent = single
        ? `Se publicará con el precio de tu lista (${money(rows[0].sell_price, { kind: "price" })}), convertido a la moneda de la tienda${cur}.`
        : `Cada producto se publicará con el precio de tu lista, convertido a la moneda de la tienda${cur}.`;
    };
    storeSel.addEventListener("change", refresh);
    refresh();
    const riskBox = $("#stpRisk", dlg);
    const syncGo = () => { go.disabled = !!((allDemo && demoBox && !demoBox.checked) || (riskBox && !riskBox.checked)); };
    if (demoBox) demoBox.addEventListener("change", syncGo);
    if (riskBox) riskBox.addEventListener("change", syncGo);
    syncGo();
    $("[data-cancel]", dlg).addEventListener("click", () => dlg.close());
    let sending = false;
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (sending || go.disabled) return;
      hideError(err);
      if (!$("#stpDraft", dlg).checked) {            // publicar sin borrador = visible para clientes reales
        sending = true;
        const c = await confirmDialog({ title: "¿Publicar visible?", confirmLabel: "Publicar visible", bodyHtml:
          `<p>Esto hará visible ${single ? "el producto" : "los productos"} a tus clientes ahora. ¿Continuar?</p><p class="muted">Si prefieres revisarlo antes, marca «Publicar como borrador».</p>` });
        sending = false;
        if (!c.ok) return;
      }
      sending = true;
      const body = { imported_ids: rows.map((r) => r.id), as_draft: $("#stpDraft", dlg).checked, allow_demo: !!(demoBox && demoBox.checked) };
      setBusy(dlg, true, go, "Enviando…", "Publicar");
      try {
        const r = await api(`/api/stores/${storeSel.value}/publish`, { method: "POST", body });
        setBusy(dlg, false, go, "", "Publicar");
        showPublishResult(dlg, rows, r, currentStore(), body.as_draft);
        selected.clear();
        $$("#importsBody .st-sel").forEach((c) => { c.checked = false; });
        updateBulkBar();
        refreshBadges(); fetchStores().catch(() => null); kick();
      } catch (ex) {
        setBusy(dlg, false, go, "", "Publicar");
        syncGo();
        showError(err, ex);
      } finally { sending = false; }
    });
  }

  function showPublishResult(dlg, rows, r, store, asDraft) {
    const queued = r.queued || [], skipped = r.skipped || [];
    const title = (id) => (rows.find((x) => x.id === id) || {}).title || `Producto #${id}`;
    const res = $("#stpResult", dlg);
    res.innerHTML = `${queued.length ? `<p class="good"><b>✓ ${queued.length} producto${queued.length === 1 ? "" : "s"} en cola</b> para publicarse${store ? ` en ${esc(store.name)}` : ""}.
        ${asDraft ? `Se crea como <b>borrador en la tienda</b> (no visible para tus clientes). Para hacerlo visible usa «Mostrar» en Tiendas → Productos publicados.` : "Verás el estado en «Mis productos» y en Tiendas → Productos publicados."}</p>` : ""}
      ${skipped.length ? `<p class="${queued.length ? "" : "bad"}"><b>${skipped.length} no se publicaron:</b></p>
        <ul>${skipped.map((s) => `<li><b>${esc(title(s.imported_id))}</b>: ${esc(skipText(s))}</li>`).join("")}</ul>` : ""}`;
    res.classList.remove("hidden");
    $$(".st-field, .st-setting, .st-price-note, .st-warn", dlg).forEach((el) => el.classList.add("hidden"));
    $(".st-actions", dlg).innerHTML = `<button type="button" class="ghost" data-go-stores>Ver en Tiendas</button>
      <button type="button" class="primary" data-close>Listo</button>`;
    $("[data-close]", dlg).addEventListener("click", () => dlg.close());
    $("[data-go-stores]", dlg).addEventListener("click", () => { dlg.close(); show("stores"); });
    $("[data-close]", dlg).focus();
    if (asDraft && queued.length) toast("Creado como borrador en la tienda: para hacerlo visible usa «Mostrar» en Tiendas → Productos publicados");
  }

  /* ============================================================ arranque */
  DF.viewLoaders.stores = () => { loadStores(); kick(); };
  // Insignia de la pestaña: una consulta al iniciar (api() espera a que haya sesión)
  tick();
})(typeof window !== "undefined" ? window : globalThis);
