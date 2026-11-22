/* Módulo collections: favoritos (♡ en cada tarjeta), colecciones y exportación a Excel (CSV).
 * Se registra en DF (ver app.js):
 *  - cardExtras: botón ♡ (no abre el detalle) · onResults: marca favoritos y agrega "Exportar resultados".
 *  - detailSections: "Guardar en colección" (casillas, crear colección y nota).
 *  - viewLoaders.collections: pestaña Colecciones (lista y detalle de una colección).
 *  - currencyChange: vuelve a dibujar los montos.
 * Usa las globales de app.js: $, $$, esc, fmt, api, toast, money, imgTag, openDetail, Currency. */
(function () {
  "use strict";

  const COLORS = {
    indigo: "Índigo", teal: "Verde azulado", green: "Verde", amber: "Ámbar",
    orange: "Naranja", rose: "Rosa", violet: "Violeta", slate: "Gris",
  };
  const EXPORT_CAP = 10000;
  const PREF_KEY = "dropfinder.export.v1";     // separador y moneda elegidos (comodidad de este navegador)
  const SORTS = [
    ["added", "Agregados recientemente"], ["added_asc", "Agregados primero"], ["title", "Título (A–Z)"],
    ["score", "Mejor score"], ["margin", "Mayor margen"], ["profit", "Mayor ganancia"], ["orders", "Más pedidos"],
    ["price_asc", "Precio ↑"], ["price_desc", "Precio ↓"],
  ];

  /* ---------------- favoritos ---------------- */
  const favs = new Set();         // ids favoritos conocidos (los de las tarjetas vistas)
  let changed = false;            // algo cambió desde el detalle: recargar la pestaña al cerrarlo

  function setFavTotal(n) {
    const pill = $("#favCount");
    if (!pill) return;
    pill.textContent = fmt(Number(n) || 0);
    pill.title = `${fmt(Number(n) || 0)} favoritos`;
  }
  async function refreshFavPill() {
    try { setFavTotal((await api("/api/favorites/ids")).total); } catch { /* sin sesión o sin servidor */ }
  }

  function favButton(id) {
    const on = favs.has(id);
    // Etiqueta fija + aria-pressed (patrón de botón alternable); el title cambia para quien ve
    return `<button type="button" class="fav-toggle" data-fav-id="${id}" aria-pressed="${on}"
      aria-label="Agregar a favoritos" title="${on ? "Quitar de favoritos" : "Agregar a favoritos"}">${on ? "♥" : "♡"}</button>`;
  }
  function syncFavButtons() {
    $$(".fav-toggle").forEach((b) => {
      const on = favs.has(Number(b.dataset.favId));
      b.setAttribute("aria-pressed", String(on));
      b.title = on ? "Quitar de favoritos" : "Agregar a favoritos";
      b.textContent = on ? "♥" : "♡";
    });
  }
  function markFav(id, on) {
    if (on) favs.add(id); else favs.delete(id);
    syncFavButtons();
  }

  async function toggleFav(id, btn) {
    if (btn) btn.disabled = true;
    try {
      const r = await api(`/api/favorites/${id}`, { method: "POST" });
      markFav(id, r.favorite);
      setFavTotal(r.total);
      detailCache.delete(id);
      toast(r.favorite ? "Agregado a Favoritos" : "Quitado de Favoritos");
      if (viewActive()) loadView();
    } catch (e) { toast(e.message); }
    finally { if (btn) btn.disabled = false; }
  }

  DF.cardExtras.push((p) => favButton(p.id));

  /* Clics en captura: app.js no debe abrir el detalle al pulsar ♡ dentro de la tarjeta */
  document.addEventListener("click", (e) => {
    const t = e.target.closest(".fav-toggle");
    if (!t) return;
    e.stopPropagation(); e.preventDefault();
    toggleFav(Number(t.dataset.favId), t);
  }, true);

  /* ---------------- resultados: marcar favoritos y "Exportar resultados" ---------------- */
  let lastSearch = null;      // {params, total}

  DF.onResults.push(async (data, params) => {
    lastSearch = { params: new URLSearchParams(params), total: data.total || 0 };
    ensureResultsButton();
    const ids = (data.items || []).map((p) => p.id);
    if (!ids.length) return;
    try {
      const r = await api(`/api/favorites/ids?ids=${ids.join(",")}`);
      const on = new Set(r.ids || []);
      ids.forEach((id) => (on.has(id) ? favs.add(id) : favs.delete(id)));
      setFavTotal(r.total);
      syncFavButtons();
    } catch { /* sin marcas de favoritos */ }
  });

  function ensureResultsButton() {
    const head = $(".results-head");
    if (!head) return;
    let b = $("#collExportResults");
    if (!b) {
      b = document.createElement("button");
      b.type = "button";
      b.id = "collExportResults";
      b.className = "ghost coll-export-btn";
      b.textContent = "⬇ Exportar resultados";
      b.title = "Descargar todos los resultados de esta búsqueda en un archivo para Excel (CSV)";
      b.addEventListener("click", () => {
        if (!lastSearch) return;
        const n = Math.min(lastSearch.total, EXPORT_CAP);
        openExport({
          kind: "search", params: lastSearch.params, title: "Exportar resultados",
          info: `${fmt(n)} productos de la búsqueda actual` +
            (lastSearch.total > EXPORT_CAP ? ` (de ${fmt(lastSearch.total)}: se exportan los primeros ${fmt(EXPORT_CAP)})` : "") + ".",
        });
      });
      head.appendChild(b);
    }
    b.disabled = !lastSearch || !lastSearch.total;
  }

  /* ---------------- Mis productos: "Exportar a Excel" ---------------- */
  function ensureImportsButton() {
    const view = $("#view-imports");
    if (!view || $("#collExportImports")) return;
    const b = document.createElement("button");
    b.type = "button";
    b.id = "collExportImports";
    b.className = "ghost coll-export-btn";
    b.textContent = "⬇ Exportar a Excel (CSV)";
    b.addEventListener("click", () => {
      const n = Number(($("#importsCount") || {}).textContent) || 0;
      openExport({ kind: "imports", params: new URLSearchParams(), title: "Exportar Mis productos",
        info: `${fmt(n)} productos importados, con tu precio de venta y estado.` });
    });
    const head = $("#view-imports .section-head");
    const monitor = $("#runMonitor");
    if (head && monitor && monitor.parentElement === head) head.insertBefore(b, monitor);
    else if (head) head.appendChild(b);
    else view.prepend(b);
  }

  /* ---------------- diálogo de exportación ---------------- */
  function readPrefs() {
    try {
      const v = JSON.parse(localStorage.getItem(PREF_KEY) || "{}");
      return { sep: v.sep === "," ? "," : ";", currency: ["USD", "COP"].includes(v.currency) ? v.currency : null };
    } catch { return { sep: ";", currency: null }; }
  }
  function writePrefs(p) {
    try { localStorage.setItem(PREF_KEY, JSON.stringify(p)); } catch { /* sin almacenamiento */ }
  }
  const copAvailable = () => !!(Currency.available && Number(Currency.rates && Currency.rates.COP) > 0);

  function exportDialog() {
    let dlg = $("#collExportDlg");
    if (dlg) return dlg;
    dlg = document.createElement("dialog");
    dlg.id = "collExportDlg";
    dlg.className = "coll-dialog";
    dlg.setAttribute("aria-labelledby", "collExportTitle");
    dlg.innerHTML = `<form method="dialog" class="coll-form">
      <h2 id="collExportTitle">Exportar</h2>
      <p class="muted" id="collExportInfo"></p>
      <fieldset class="coll-opts"><legend>Separador</legend>
        <label><input type="radio" name="sep" value=";"> <span>Punto y coma <b>;</b> <small class="muted">— Excel en español (decimales con coma)</small></span></label>
        <label><input type="radio" name="sep" value=","> <span>Coma <b>,</b> <small class="muted">— Google Sheets o Excel en inglés (decimales con punto)</small></span></label>
      </fieldset>
      <fieldset class="coll-opts"><legend>Moneda</legend>
        <label><input type="radio" name="currency" value="USD"> <span>USD — dólar</span></label>
        <label><input type="radio" name="currency" value="COP"> <span>COP — peso colombiano <small class="muted" id="collCopHint"></small></span></label>
      </fieldset>
      <p class="muted small">Se descarga un archivo CSV (UTF-8) que se abre con doble clic en Excel.
        Los SKU que Excel convertiría en número o fecha (por ejemplo <code>0012</code>, <code>1-2</code> o un código de 13 dígitos)
        y los textos que empiezan con <code>=</code>, <code>+</code>, <code>-</code> o <code>@</code> llevan un apóstrofo (<code>'</code>) delante
        para que se vean tal cual y no se ejecuten como fórmula.</p>
      <p class="coll-error bad hidden" id="collExportError" role="alert"></p>
      <div class="coll-dlg-actions">
        <button type="submit" class="ghost" value="cancel">Cancelar</button>
        <button type="button" class="primary coll-download" id="collExportGo">⬇ Descargar CSV</button>
      </div></form>`;
    document.body.appendChild(dlg);
    dlg.addEventListener("click", (e) => { if (e.target === dlg) dlg.close(); });
    $("#collExportGo", dlg).addEventListener("click", () => download(dlg));
    return dlg;
  }

  let exportReq = null;
  function exportUrl(dlg) {
    const p = new URLSearchParams(exportReq.params);
    p.delete("page"); p.delete("page_size");
    p.set("sep", $("input[name=sep]:checked", dlg).value);
    p.set("currency", $("input[name=currency]:checked", dlg).value);
    return `/api/export/${exportReq.kind}.csv?${p}`;
  }
  /* Nombre del archivo: el de Content-Disposition o, si falta, uno calculado igual que el servidor */
  function fileName(res) {
    const m = /filename="?([^";]+)"?/i.exec(res.headers.get("Content-Disposition") || "");
    if (m) return m[1];
    const d = new Date();
    const day = `${d.getFullYear()}${String(d.getMonth() + 1).padStart(2, "0")}${String(d.getDate()).padStart(2, "0")}`;
    return `dropfinder-${exportReq.kind}-${day}.csv`;
  }
  /* Descarga con fetch + blob: si el servidor responde con error (401 sin sesión, 400, 500) se muestra
     el mensaje en el diálogo en vez de guardar un archivo con el JSON del error. */
  async function download(dlg) {
    if (!exportReq) return;
    const btn = $("#collExportGo", dlg);
    const err = $("#collExportError", dlg);
    err.classList.add("hidden");
    btn.disabled = true;
    btn.textContent = "Preparando…";
    try {
      const res = await fetch(exportUrl(dlg), { credentials: "same-origin" });
      if (!res.ok) {
        let msg = res.statusText || `Error ${res.status}`;
        try { msg = (await res.json()).error || msg; } catch { /* sin JSON */ }
        if (res.status === 401) msg = "Tu sesión terminó: vuelve a iniciar sesión para exportar.";
        throw new Error(msg);
      }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = fileName(res);
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 30000);
      writePrefs({ sep: $("input[name=sep]:checked", dlg).value, currency: $("input[name=currency]:checked", dlg).value });
      dlg.close();
      toast(`Archivo descargado: ${a.download}`);
    } catch (e) {
      err.textContent = `No se pudo exportar: ${e.message}`;
      err.classList.remove("hidden");
      toast(`No se pudo exportar: ${e.message}`);
    } finally {
      btn.disabled = false;
      btn.textContent = "⬇ Descargar CSV";
    }
  }
  function openExport(req) {
    exportReq = req;
    const dlg = exportDialog();
    const prefs = readPrefs();
    $("#collExportTitle", dlg).textContent = req.title;
    $("#collExportInfo", dlg).textContent = req.info || "";
    $(`input[name=sep][value="${prefs.sep}"]`, dlg).checked = true;
    const cop = $("input[name=currency][value=COP]", dlg);
    cop.disabled = !copAvailable();
    $("#collCopHint", dlg).textContent = cop.disabled ? "(sin tasa de cambio disponible)" : "— precios con tu redondeo";
    const cur = !cop.disabled && (prefs.currency || Currency.code()) === "COP" ? "COP" : "USD";
    $(`input[name=currency][value=${cur}]`, dlg).checked = true;
    $("#collExportError", dlg).classList.add("hidden");
    if (!dlg.open) dlg.showModal();
  }

  /* ---------------- diálogo crear / editar colección ---------------- */
  function editDialog() {
    let dlg = $("#collEditDlg");
    if (dlg) return dlg;
    dlg = document.createElement("dialog");
    dlg.id = "collEditDlg";
    dlg.className = "coll-dialog";
    dlg.setAttribute("aria-labelledby", "collEditTitle");
    dlg.innerHTML = `<form class="coll-form" novalidate>
      <h2 id="collEditTitle">Nueva colección</h2>
      <label class="field">Nombre<input name="name" maxlength="60" required placeholder="ej. Ideas Navidad" autocomplete="off"></label>
      <label class="field">Descripción (opcional)<input name="description" maxlength="200" placeholder="ej. Probar en marzo" autocomplete="off"></label>
      <fieldset class="coll-colors"><legend>Color</legend>
        ${Object.entries(COLORS).map(([k, label]) => `<label class="coll-swatch c-${k}" title="${label}">
          <input type="radio" name="color" value="${k}"><span class="sr-only">${label}</span></label>`).join("")}
      </fieldset>
      <p class="coll-error bad hidden" role="alert"></p>
      <div class="coll-dlg-actions">
        <button type="button" class="ghost" data-close>Cancelar</button>
        <button type="submit" class="primary">Guardar</button>
      </div></form>`;
    document.body.appendChild(dlg);
    $("[data-close]", dlg).addEventListener("click", () => dlg.close());
    dlg.addEventListener("click", (e) => { if (e.target === dlg) dlg.close(); });
    $("form", dlg).addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = e.target;
      const err = $(".coll-error", dlg);
      const body = { name: f.name.value.trim(), description: f.description.value.trim(),
        color: ($("input[name=color]:checked", f) || {}).value };
      const editing = dlg.dataset.cid;
      if (editing && dlg.dataset.fav === "1") delete body.name;
      else if (!body.name) { err.textContent = "Escribe un nombre."; err.classList.remove("hidden"); f.name.focus(); return; }
      try {
        const c = editing
          ? await api(`/api/collections/${editing}`, { method: "PATCH", body })
          : await api("/api/collections", { method: "POST", body });
        dlg.close();
        toast(editing ? "Colección actualizada" : `Colección «${c.name}» creada`);
        if (dlg._done) dlg._done(c);
      } catch (ex) { err.textContent = ex.message; err.classList.remove("hidden"); }
    });
    return dlg;
  }
  function openEdit(c, done) {
    const dlg = editDialog();
    const f = $("form", dlg);
    f.reset();
    $(".coll-error", dlg).classList.add("hidden");
    $("#collEditTitle", dlg).textContent = c ? "Editar colección" : "Nueva colección";
    dlg.dataset.cid = c ? c.id : "";
    dlg.dataset.fav = c && c.is_favorites ? "1" : "";
    f.name.value = c ? c.name : "";
    f.name.disabled = !!(c && c.is_favorites);
    f.name.title = f.name.disabled ? "Favoritos no se puede renombrar" : "";
    f.description.value = c ? c.description || "" : "";
    const color = (c && c.color) || "indigo";
    ($(`input[name=color][value="${color}"]`, f) || $("input[name=color]", f)).checked = true;
    dlg._done = done;
    dlg.showModal();
    (f.name.disabled ? f.description : f.name).focus();
  }

  /* ---------------- detalle: "Guardar en colección" ---------------- */
  const detailCache = new Map();     // product_id -> respuesta de /api/product-collections (sin parpadeo)

  DF.detailSections.push((p, container) => {
    const sec = document.createElement("section");
    sec.className = "coll-sec";
    sec.setAttribute("aria-labelledby", "collSecTitle");
    container.appendChild(sec);
    const cached = detailCache.get(p.id);
    if (cached) renderDetailSec(p, sec, cached);
    else sec.innerHTML = `<h3 id="collSecTitle">Guardar en colección</h3><p class="muted">Cargando…</p>`;
    loadDetailSec(p, sec, cached);
  });

  async function loadDetailSec(p, sec, cached) {
    try {
      const d = await api(`/api/product-collections/${p.id}`);
      detailCache.set(p.id, d);
      if (sec.isConnected && JSON.stringify(d) !== JSON.stringify(cached)) renderDetailSec(p, sec, d);
    } catch (e) {
      if (sec.isConnected && !cached) sec.innerHTML = `<h3 id="collSecTitle">Guardar en colección</h3><p class="muted">${esc(e.message)}</p>`;
    }
  }

  /* Cada colección marcada tiene SU nota: se muestra y se guarda solo en ella (nunca se copia a otras). */
  function renderDetailSec(p, sec, d) {
    // Notas que el usuario está escribiendo (sin guardar) se conservan al volver a dibujar
    const typing = new Map($$("textarea[data-cid]", sec).filter((t) => t.dataset.dirty).map((t) => [t.dataset.cid, t.value]));
    sec.innerHTML = `<h3 id="collSecTitle">Guardar en colección</h3>
      <div class="coll-checks" role="group" aria-labelledby="collSecTitle">
        ${d.items.map((c) => {
          const key = c.virtual ? "fav" : String(c.id);
          const name = `${c.is_favorites ? "♥ " : ""}${esc(c.name)}`;
          return `<div class="coll-check-row">
            <label class="coll-check"><input type="checkbox" data-cid="${key}" ${c.is_favorites ? 'data-fav="1"' : ""} ${c.contains ? "checked" : ""}>
              <span class="coll-dot c-${esc(c.color)}" aria-hidden="true"></span><span class="coll-check-name">${name}</span>
              <small class="muted">${fmt(c.count)}</small></label>
            ${c.contains ? `<label class="coll-row-note"><span class="sr-only">Nota en ${esc(c.name)}</span>
              <textarea rows="1" maxlength="500" data-cid="${key}" placeholder="Nota para esta colección (opcional)">${esc(typing.has(key) ? typing.get(key) : c.note)}</textarea></label>` : ""}
          </div>`;
        }).join("")}
      </div>
      <p class="muted small">La nota se guarda al salir del campo (o con Enter) y solo en esa colección.</p>
      <form class="coll-inline-new">
        <label class="sr-only" for="collInlineName">Nombre de la nueva colección</label>
        <input id="collInlineName" maxlength="60" placeholder="Nueva colección…" autocomplete="off">
        <button type="submit" class="ghost">+ Crear y guardar</button>
      </form>`;
    $$("textarea[data-cid]", sec).forEach((t) => {
      if (typing.has(t.dataset.cid)) t.dataset.dirty = "1";
    });
    const reload = () => { changed = true; loadDetailSec(p, sec, detailCache.get(p.id)); };

    $(".coll-checks", sec).addEventListener("change", async (e) => {
      const box = e.target.closest("input[type=checkbox]");
      if (!box) return;
      box.disabled = true;
      try {
        if (box.dataset.fav) {
          // Favoritos puede ser «virtual» (aún sin crear): el endpoint de favoritos lo crea si hace falta
          const want = box.checked;
          let r = await api(`/api/favorites/${p.id}`, { method: "POST" });
          if (r.favorite !== want) r = await api(`/api/favorites/${p.id}`, { method: "POST" });
          markFav(p.id, r.favorite);
          setFavTotal(r.total);
          toast(r.favorite ? "Agregado a Favoritos" : "Quitado de Favoritos");
        } else if (box.checked) {
          await api(`/api/collections/${box.dataset.cid}/items`, { method: "POST", body: { product_ids: [p.id] } });
          toast("Guardado en la colección");
        } else {
          await api(`/api/collections/${box.dataset.cid}/items/${p.id}`, { method: "DELETE" });
          toast("Quitado de la colección");
        }
        reload();
      } catch (ex) { box.checked = !box.checked; toast(ex.message); }
      finally { box.disabled = false; }
    });

    sec.addEventListener("input", (e) => { if (e.target.matches("textarea[data-cid]")) e.target.dataset.dirty = "1"; });
    sec.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey && e.target.matches("textarea[data-cid]")) { e.preventDefault(); e.target.blur(); }
    });
    sec.addEventListener("change", async (e) => {
      const t = e.target.closest("textarea[data-cid]");
      if (!t || !t.dataset.dirty) return;
      const cid = t.dataset.cid === "fav" ? (detailCache.get(p.id) || { items: [] }).items.find((c) => c.is_favorites)?.id : t.dataset.cid;
      if (!cid) return;
      try {
        await api(`/api/collections/${cid}/items/${p.id}`, { method: "PATCH", body: { note: t.value } });
        delete t.dataset.dirty;
        toast("Nota guardada");
        reload();
      } catch (ex) { toast(ex.message); }
    });

    $(".coll-inline-new", sec).addEventListener("submit", async (e) => {
      e.preventDefault();
      const input = $("input", e.target);
      const name = input.value.trim();
      if (!name) { input.focus(); return; }
      try {
        const c = await api("/api/collections", { method: "POST", body: { name } });
        await api(`/api/collections/${c.id}/items`, { method: "POST", body: { product_ids: [p.id] } });
        toast(`Guardado en «${c.name}»`);
        reload();
      } catch (ex) { toast(ex.message); }
    });
  }

  const detailDlg = $("#detail");
  if (detailDlg) detailDlg.addEventListener("close", () => {
    if (changed && viewActive()) loadView();
    changed = false;
  });

  /* ---------------- pestaña Colecciones ---------------- */
  const root = () => $("#view-collections");
  const viewActive = () => { const v = root(); return !!(v && v.classList.contains("active")); };
  let list = null;          // respuesta de GET /api/collections
  let current = null;       // colección abierta: {id, sort, page, data}

  /* Favoritos aún no existe en el servidor (id null): se muestra vacío; el primer ♡ lo crea */
  function openVirtualFavorites() {
    const fav = (list && list.items.find((c) => c.is_favorites)) || { name: "Favoritos", color: "rose", is_favorites: true, virtual: true };
    current = { id: null, sort: "added", page: 1, data: { collection: fav, items: [], total: 0, page: 1, pages: 0 } };
    renderCollection();
  }

  async function loadView() {
    if (current && current.id === null) {        // ¿ya se creó Favoritos (♡ desde otra pestaña)?
      try {
        list = await api("/api/collections");
        const fav = list.items.find((c) => c.is_favorites);
        if (fav && !fav.virtual) return loadCollection(fav.id);
        return openVirtualFavorites();
      } catch (e) { toast(e.message); return undefined; }
    }
    if (current) return loadCollection(current.id, { keep: true });
    try {
      list = await api("/api/collections");
      const fav = list.items.find((c) => c.is_favorites);
      if (fav) setFavTotal(fav.count);
      renderList();
    } catch (e) {
      root().innerHTML = `<p class="empty">No se pudieron cargar las colecciones: ${esc(e.message)}</p>`;
    }
  }

  function mosaic(c) {
    const cells = (c.preview || []).slice(0, 4).map((p) => `<span class="coll-cell">${imgTag(p)}</span>`);
    while (cells.length < 4) cells.push(`<span class="coll-cell empty-cell"></span>`);
    return `<div class="coll-mosaic" aria-hidden="true">${cells.join("")}</div>`;
  }

  const fmtDate = (iso) => {
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? "" : `actualizada ${d.toLocaleDateString("es-CO", { day: "numeric", month: "short", year: "numeric" })}`;
  };
  const nProducts = (n) => (n === 1 ? "1 producto" : `${fmt(n)} productos`);

  function renderList() {
    const items = list.items;
    const empty = !items.some((c) => !c.is_favorites) && !items.some((c) => c.count);
    root().innerHTML = `<div class="section-head">
        <div><h2>Colecciones</h2><p class="muted">Agrupa productos para decidir después: «Ideas Navidad», «Probar en marzo»… Toca ♡ en una tarjeta para guardarla en Favoritos.</p></div>
        <button type="button" class="primary" id="collNew">+ Nueva colección</button>
      </div>
      <div class="coll-grid">${items.map((c) => `<article class="coll-card c-${esc(c.color)}" data-cid="${c.id ?? ""}">
          ${mosaic(c)}
          <div class="coll-card-body">
            <h3><button type="button" class="coll-open">${c.is_favorites ? "♥ " : ""}${esc(c.name)}</button></h3>
            ${c.description ? `<p class="muted coll-desc">${esc(c.description)}</p>` : ""}
            <p class="muted small">${nProducts(c.count)} · ${esc(fmtDate(c.updated_at))}</p>
          </div></article>`).join("")}</div>
      ${empty ? `<div class="coll-empty"><p><b>Aún no guardas productos.</b></p>
        <p class="muted">En <b>Buscar productos</b> toca ♡ para marcar favoritos, o abre un producto y usa «Guardar en colección».</p></div>` : ""}`;
  }

  async function loadCollection(id, { keep = false } = {}) {
    const sort = keep && current ? current.sort : "added";
    const page = keep && current ? current.page : 1;
    try {
      const data = await api(`/api/collections/${id}/items?sort=${encodeURIComponent(sort)}&page=${page}`);
      if (data.page > 1 && !data.items.length && data.pages) {       // se quitó el último de la página
        current = { id, sort, page: data.pages, data };
        return loadCollection(id, { keep: true });
      }
      current = { id, sort, page, data };
      if (data.collection.is_favorites) setFavTotal(data.total);
      renderCollection();
    } catch (e) {
      current = null;
      if (e.status === 404) { toast("La colección ya no existe"); return loadView(); }
      root().innerHTML = `<p class="empty">${esc(e.message)}</p>`;
    }
  }

  function itemRow(p) {
    return `<article class="coll-item" data-pid="${p.id}">
      <button type="button" class="coll-thumb" data-open="${p.id}" aria-label="Ver detalle de ${esc(p.title)}">${imgTag(p)}</button>
      <div class="coll-item-main">
        <button type="button" class="link coll-title" data-open="${p.id}">${esc(p.title)}</button>
        <div class="meta"><span class="chip">${esc(p.supplier)}</span><span class="chip">🚚 ${p.shipping_days}d</span>
          <span class="chip">★ ${p.score}</span>${p.stock > 0 ? "" : '<span class="chip bad">sin stock</span>'}
          ${p.imported_id ? '<span class="chip">✓ Importado</span>' : ""}</div>
        <div class="coll-kpis"><span>Costo <b>${money(p.total_cost)}</b></span><span>Venta <b>${money(p.suggested_price, { kind: "price" })}</b></span>
          <span>Ganancia <b class="${p.profit >= 0 ? "good" : "bad"}">${money(p.profit)}</b> · ${p.margin}%</span></div>
        <label class="coll-note"><span class="sr-only">Nota para ${esc(p.title)}</span>
          <textarea rows="1" maxlength="500" placeholder="Agregar nota…" data-note="${p.id}">${esc(p.note || "")}</textarea></label>
      </div>
      <button type="button" class="link coll-remove" data-remove="${p.id}" aria-label="Quitar ${esc(p.title)} de la colección">Quitar</button>
    </article>`;
  }

  function renderCollection() {
    const { data, sort } = current;
    const c = data.collection;
    root().innerHTML = `<button type="button" class="link coll-back" id="collBack">← Colecciones</button>
      <div class="section-head coll-head">
        <div class="coll-head-text"><h2><span class="coll-dot c-${esc(c.color)}" aria-hidden="true"></span>${c.is_favorites ? "♥ " : ""}${esc(c.name)}</h2>
          <p class="muted">${c.description ? `${esc(c.description)} · ` : ""}${nProducts(data.total)}</p></div>
        <div class="coll-actions">
          <label class="sr-only" for="collSort">Ordenar productos</label>
          <select id="collSort">${SORTS.map(([k, l]) => `<option value="${k}" ${k === sort ? "selected" : ""}>${l}</option>`).join("")}</select>
          <button type="button" class="ghost" id="collExport" ${data.total ? "" : "disabled"}>⬇ Exportar a Excel (CSV)</button>
          ${c.virtual ? "" : `<button type="button" class="ghost" id="collEdit">${c.is_favorites ? "Editar color" : "Editar"}</button>`}
          ${c.is_favorites ? "" : '<button type="button" class="ghost danger" id="collDelete">Eliminar</button>'}
        </div>
      </div>
      ${data.items.length ? `<div class="coll-items">${data.items.map(itemRow).join("")}</div>`
        : `<div class="coll-empty"><p><b>Esta colección está vacía.</b></p>
           <p class="muted">${c.is_favorites ? "Toca ♡ en las tarjetas de <b>Buscar productos</b>." : "Abre un producto en <b>Buscar productos</b> y usa «Guardar en colección»."}</p>
           <button type="button" class="ghost" id="collGoSearch">Ir a Buscar productos</button></div>`}
      ${data.pages > 1 ? `<div class="pager"><button type="button" class="ghost" data-page="${data.page - 1}" ${data.page <= 1 ? "disabled" : ""}>← Anterior</button>
        <span class="muted">Página ${data.page} de ${data.pages}</span>
        <button type="button" class="ghost" data-page="${data.page + 1}" ${data.page >= data.pages ? "disabled" : ""}>Siguiente →</button></div>` : ""}`;
  }

  /* Eventos de la pestaña, delegados (el contenido se vuelve a dibujar) */
  function bindView() {
    const r = root();
    if (!r || r.dataset.collBound) return;
    r.dataset.collBound = "1";
    r.addEventListener("click", async (e) => {
      const t = e.target;
      if (t.closest("#collNew")) { openEdit(null, () => loadView()); return; }
      const card = t.closest(".coll-card");
      if (!current && card) {
        if (card.dataset.cid) loadCollection(Number(card.dataset.cid));
        else openVirtualFavorites();
        return;
      }
      if (t.closest("#collBack")) { current = null; loadView(); return; }
      if (t.closest("#collGoSearch")) { const tab = $('.tab[data-view="search"]'); if (tab) tab.click(); return; }
      const det = t.closest("[data-open]");
      if (det) { Promise.resolve(openDetail(Number(det.dataset.open))).catch((ex) => toast(ex.message)); return; }
      const rm = t.closest("[data-remove]");
      if (rm && current) {
        const pid = Number(rm.dataset.remove);
        try {
          await api(`/api/collections/${current.id}/items/${pid}`, { method: "DELETE" });
          if (current.data.collection.is_favorites) { markFav(pid, false); }
          detailCache.delete(pid);
          toast("Quitado de la colección");
          loadCollection(current.id, { keep: true });
        } catch (ex) { toast(ex.message); }
        return;
      }
      if (t.closest("#collExport") && current) {
        const c = current.data.collection;
        openExport({ kind: "collection", params: new URLSearchParams({ id: c.id, sort: current.sort }),
          title: `Exportar «${c.name}»`, info: `${nProducts(current.data.total)}, con sus notas.` });
        return;
      }
      if (t.closest("#collEdit") && current) {
        openEdit(current.data.collection, () => loadCollection(current.id, { keep: true }));
        return;
      }
      if (t.closest("#collDelete") && current) {
        const c = current.data.collection;
        if (!confirm(`¿Eliminar la colección «${c.name}»? Los productos siguen en el catálogo; solo se borran la colección y sus notas.`)) return;
        try {
          await api(`/api/collections/${c.id}`, { method: "DELETE" });
          toast("Colección eliminada");
          current = null;
          loadView();
        } catch (ex) { toast(ex.message); }
        return;
      }
      const pg = t.closest("[data-page]");
      if (pg && current) { current.page = Number(pg.dataset.page); loadCollection(current.id, { keep: true }); }
    });
    // Enter en una nota la guarda (Mayús+Enter agrega una línea)
    r.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey && e.target.matches("textarea[data-note]")) { e.preventDefault(); e.target.blur(); }
    });
    r.addEventListener("change", async (e) => {
      if (e.target.id === "collSort" && current && current.id !== null) {
        current.sort = e.target.value; current.page = 1;
        loadCollection(current.id, { keep: true });
        return;
      }
      const area = e.target.closest("textarea[data-note]");
      if (area && current) {
        const pid = Number(area.dataset.note);
        try {
          await api(`/api/collections/${current.id}/items/${pid}`, { method: "PATCH", body: { note: area.value } });
          const it = current.data.items.find((x) => x.id === pid);
          if (it) it.note = area.value.trim();
          detailCache.delete(pid);
          toast("Nota guardada");
        } catch (ex) { toast(ex.message); }
      }
    });
  }

  /* ---------------- registro ---------------- */
  DF.viewLoaders.collections = () => { bindView(); loadView(); };
  DF.currencyChange.push(() => { if (viewActive() && current && current.data) renderCollection(); });

  ensureImportsButton();
  refreshFavPill();
})();
