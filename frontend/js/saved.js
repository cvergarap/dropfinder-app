/* Búsquedas guardadas con alerta de productos nuevos.
 * - Botón "☆ Guardar búsqueda" en la cabecera de resultados (DF.onResults) → diálogo con nombre y aviso.
 * - Pestaña "Búsquedas guardadas" (DF.viewLoaders.saved): tarjetas con criterios, coincidencias y nuevos.
 * - "Ver resultados" restaura los criterios en el formulario de búsqueda y marca la búsqueda como vista.
 * Usa los globales de app.js: $, $$, esc, fmt, api, toast, show, search, loadFacets, refreshCounts,
 * page, exactSearch, sortTouched (variables de nivel superior compartidas entre scripts clásicos). */
(function () {
  "use strict";

  const LIST_KEYS = ["source", "category", "supplier", "warehouse"];
  const MAX_NAME = 80;
  let current = null;          // { data, params } de los últimos resultados mostrados
  let items = [];              // búsquedas guardadas (última carga)
  let highlight = null;        // { ids: Set, key } productos nuevos de la búsqueda guardada abierta
  let pendingKey = false;      // la próxima respuesta de resultados fija la clave del resaltado

  /* URLSearchParams → objeto de parámetros de /api/products (listas para los filtros múltiples) */
  function paramsToObject(sp) {
    const o = {};
    for (const [k, v] of sp) {
      if (k === "page" || k === "page_size" || k === "exact") continue;
      if (LIST_KEYS.includes(k)) (o[k] = o[k] || []).push(v);
      else o[k] = v;
    }
    return o;
  }
  const keyOf = (sp) => { const s = new URLSearchParams(sp); s.delete("page"); s.sort(); return s.toString(); };

  /* Los costos se guardan en USD; si se muestra otra moneda, el chip de costo se escribe en esa moneda */
  function chipsHtml(chips, params) {
    return chips.map((c) => {
      let label = c.label;
      if (c.key === "cost" && typeof Currency !== "undefined" && Currency.isLocal()) {
        const lo = params.min_cost, hi = params.max_cost;
        const m = (v) => money(v);
        label = lo != null && hi != null ? `costo ${m(lo)}–${m(hi)}` : lo != null ? `costo ≥ ${m(lo)}` : `costo ≤ ${m(hi)}`;
      }
      return `<span class="chip">${esc(label)}</span>`;
    }).join("") || `<span class="chip">Todos los productos</span>`;
  }

  /* ---------------- Botón y diálogo "Guardar búsqueda" ---------------- */
  function ensureButton() {
    const head = $(".results-head");
    if (!head || $("#ssSaveBtn")) return;
    const b = document.createElement("button");
    b.type = "button";
    b.id = "ssSaveBtn";
    b.className = "ghost ss-save-btn";
    b.textContent = "☆ Guardar búsqueda";
    b.title = "Guarda estos criterios y recibe una alerta cuando aparezcan productos nuevos";
    b.addEventListener("click", openDialog);
    head.appendChild(b);
  }

  function dialog() {
    let dlg = $("#ssDialog");
    if (dlg) return dlg;
    dlg = document.createElement("dialog");
    dlg.id = "ssDialog";
    dlg.className = "ss-dialog";
    dlg.setAttribute("aria-labelledby", "ssDlgTitle");
    dlg.innerHTML = `<form method="dialog" class="ss-form" novalidate>
        <h2 id="ssDlgTitle">Guardar búsqueda</h2>
        <div class="ss-chips meta" id="ssDlgChips" aria-label="Criterios"></div>
        <p class="muted small" id="ssDlgTotal" aria-live="polite"></p>
        <label class="field">Nombre<input name="name" required maxlength="${MAX_NAME}" autocomplete="off"></label>
        <label class="toggle"><input type="checkbox" name="notify" checked> Avisarme cuando aparezcan productos nuevos</label>
        <p class="ss-error bad hidden" role="alert" id="ssDlgError"></p>
        <div class="ss-actions">
          <button type="button" class="ghost" value="cancel" id="ssDlgCancel">Cancelar</button>
          <button type="submit" class="primary" id="ssDlgSave">Guardar</button>
        </div>
      </form>`;
    document.body.appendChild(dlg);
    $("#ssDlgCancel", dlg).addEventListener("click", () => dlg.close());
    $("form", dlg).addEventListener("submit", submitDialog);
    return dlg;
  }

  /* Parámetros a guardar: los de la búsqueda que produjo los resultados mostrados. Si el servidor corrigió
     el texto ("Mostrando resultados para …"), se guarda el texto corregido. */
  function paramsToSave() {
    if (!current) return {};
    const o = paramsToObject(current.params);
    if (current.data && current.data.corrected_from && current.data.query) o.q = current.data.query;
    return o;
  }

  async function openDialog() {
    const dlg = dialog();
    const f = $("form", dlg);
    const params = paramsToSave();
    dlg._params = params;
    f.name.value = "";
    f.notify.checked = true;
    $("#ssDlgError", dlg).classList.add("hidden");
    $("#ssDlgChips", dlg).innerHTML = "";
    $("#ssDlgTotal", dlg).textContent = "Calculando…";
    dlg.showModal();
    f.name.focus();
    try {
      const qs = new URLSearchParams();
      for (const [k, v] of Object.entries(params)) (Array.isArray(v) ? v : [v]).forEach((x) => qs.append(k, x));
      const p = await api(`/api/saved-searches/preview?${qs}`);
      if (!f.name.value) f.name.value = p.default_name;
      f.name.select();
      $("#ssDlgChips", dlg).innerHTML = chipsHtml(p.chips, p.params);
      $("#ssDlgTotal", dlg).textContent = `${fmt(p.total)} productos cumplen hoy estos criterios. Solo se avisará de los que aparezcan desde ahora.`;
    } catch (e) {
      $("#ssDlgTotal", dlg).textContent = "";
      showError(e.message);
    }
  }

  function showError(msg) {
    const el = $("#ssDlgError");
    el.textContent = msg;
    el.classList.remove("hidden");
  }

  async function submitDialog(e) {
    e.preventDefault();
    const dlg = dialog();
    const f = e.target;
    const name = f.name.value.trim();
    if (name.length > MAX_NAME) return showError(`El nombre admite como máximo ${MAX_NAME} caracteres.`);
    const btn = $("#ssDlgSave", dlg);
    btn.disabled = true;
    try {
      const it = await api("/api/saved-searches", {
        method: "POST", body: { name, params: dlg._params || {}, notify: f.notify.checked },
      });
      dlg.close();
      toast(`Búsqueda guardada: ${it.name}`);
      refreshSavedPill();
    } catch (err) {
      showError(err.message);
    } finally { btn.disabled = false; }
  }

  /* ---------------- Pestaña "Búsquedas guardadas" ---------------- */
  function view() { return $("#view-saved"); }

  function renderShell() {
    const v = view();
    if ($("#ssList", v)) return;
    v.innerHTML = `<div class="section-head">
        <div><h2>Búsquedas guardadas</h2>
          <p class="muted">Te avisamos en <b>Alertas</b> cuando aparece un producto nuevo que cumple tus criterios.</p></div>
        <button type="button" id="ssCheck" class="primary">↻ Revisar ahora</button>
      </div>
      <div id="ssList" class="ss-list" aria-live="polite"></div>`;
    $("#ssCheck", v).addEventListener("click", checkNow);
    $("#ssList", v).addEventListener("click", onListClick);
    $("#ssList", v).addEventListener("change", onListChange);
  }

  function emptyState() {
    return `<div class="ss-empty">
      <h3>Aún no tienes búsquedas guardadas</h3>
      <p class="muted">Busca y filtra como siempre (por ejemplo <i>Mascotas</i> con margen ≥ 45 % y envío ≤ 5 días)
        y pulsa <b>☆ Guardar búsqueda</b> junto al número de resultados.</p>
      <p class="muted">Cuando cargues un catálogo o el monitor actualice productos, DropFinder revisa tus búsquedas
        y te avisa en <b>Alertas</b> de los productos nuevos que las cumplan.</p>
      <button type="button" class="ghost" data-act="go-search">Ir a Buscar productos</button>
    </div>`;
  }

  function cardHtml(s) {
    const checked = s.last_checked_at ? new Date(s.last_checked_at).toLocaleString("es") : "nunca";
    const isNew = s.new > 0;
    return `<article class="ss-card${isNew ? " has-new" : ""}" data-id="${s.id}">
      <div class="ss-card-head">
        <h3 class="ss-name">${esc(s.name)}</h3>
        ${isNew ? `<span class="ss-new-badge">${fmt(s.new)} ${s.new === 1 ? "nuevo" : "nuevos"}</span>` : ""}
      </div>
      <div class="meta ss-chips">${chipsHtml(s.chips, s.params)}</div>
      <p class="ss-stats"><b>${fmt(s.total)}</b> ${s.total === 1 ? "producto cumple" : "productos cumplen"} hoy
        <span class="muted">· revisada: ${esc(checked)}</span></p>
      <label class="toggle ss-notify"><input type="checkbox" data-act="notify" ${s.notify ? "checked" : ""}>
        Avisarme de productos nuevos</label>
      <div class="ss-actions">
        <button type="button" class="primary" data-act="open">Ver resultados</button>
        <button type="button" class="ghost" data-act="rename">Renombrar</button>
        <button type="button" class="ghost danger" data-act="delete">Eliminar</button>
      </div>
    </article>`;
  }

  function renderList() {
    const list = $("#ssList", view());
    if (!list) return;
    list.innerHTML = items.length ? items.map(cardHtml).join("") : emptyState();
    $("#ssCheck", view()).disabled = !items.length;
  }

  /* Solo se aplica la respuesta de la última petición de la lista: una respuesta vieja (p. ej. el refresco
     diferido de los contadores) no debe pisar a una posterior a "marcar como vista". */
  let listSeq = 0;
  async function fetchList() {
    const seq = ++listSeq;
    const r = await api("/api/saved-searches");
    return seq === listSeq ? r.items || [] : null;
  }

  async function loadSaved() {
    renderShell();
    try {
      const fresh = await fetchList();
      if (!fresh) return;
      items = fresh;
      renderList();
      updatePill();
    } catch (e) {
      $("#ssList", view()).innerHTML = `<p class="empty">No se pudieron cargar las búsquedas guardadas: ${esc(e.message)}</p>`;
    }
  }

  const byId = (id) => items.find((s) => s.id === id);

  async function onListClick(e) {
    const b = e.target.closest("button[data-act]");
    if (!b) return;
    if (b.dataset.act === "go-search") { show("search"); $("#q").focus(); return; }
    const card = b.closest(".ss-card");
    const s = card && byId(Number(card.dataset.id));
    if (!s) return;
    if (b.dataset.act === "open") return openResults(s, b);
    if (b.dataset.act === "rename") {
      const name = prompt("Nuevo nombre de la búsqueda:", s.name);
      if (name === null) return;
      if (!name.trim() || name.trim().length > MAX_NAME) return toast(`El nombre debe tener entre 1 y ${MAX_NAME} caracteres`);
      try {
        await api(`/api/saved-searches/${s.id}`, { method: "PATCH", body: { name: name.trim() } });
        toast("Nombre actualizado");
        loadSaved();
      } catch (err) { toast(err.message); }
    }
    if (b.dataset.act === "delete") {
      if (!confirm(`¿Eliminar la búsqueda guardada «${s.name}»? También se borran sus alertas.`)) return;
      try {
        await api(`/api/saved-searches/${s.id}`, { method: "DELETE" });
        toast("Búsqueda eliminada");
        await loadSaved();
        refreshCounts();
      } catch (err) { toast(err.message); }
    }
  }

  async function onListChange(e) {
    const box = e.target.closest("input[data-act=notify]");
    if (!box) return;
    const s = byId(Number(box.closest(".ss-card").dataset.id));
    box.disabled = true;
    try {
      await api(`/api/saved-searches/${s.id}`, { method: "PATCH", body: { notify: box.checked } });
      s.notify = box.checked;
      toast(box.checked ? "Te avisaremos de productos nuevos" : "Avisos desactivados para esta búsqueda");
    } catch (err) {
      box.checked = !box.checked;
      toast(err.message);
    } finally { box.disabled = false; }
  }

  async function checkNow() {
    const b = $("#ssCheck", view());
    b.disabled = true;
    try {
      const r = await api("/api/saved-searches/check", { method: "POST" });
      const n = r.alerts.length;
      toast(n ? `${fmt(n)} ${n === 1 ? "alerta nueva" : "alertas nuevas"} en Alertas` : "Sin productos nuevos");
      await loadSaved();
      refreshCounts();
    } catch (err) { toast(err.message); }
    finally { b.disabled = !items.length; }
  }

  /* ---------------- "Ver resultados": restaurar los criterios en el formulario ---------------- */
  async function applyParams(p) {
    try { await loadFacets(); } catch { /* se usan las facetas que ya hay */ }
    const q = p.q || "";
    $("#q").value = q;
    const def = q ? "relevance" : "score";
    $("#sort").value = p.sort || def;
    if (!$("#sort").value) $("#sort").value = def;       // orden que ya no existe
    sortTouched = $("#sort").value !== def;
    exactSearch = false;
    let missing = 0;
    for (const key of LIST_KEYS) {
      const vals = p[key] || [];
      const inputs = $$(`#f-${key} input`);
      inputs.forEach((i) => (i.checked = vals.includes(i.value)));
      missing += vals.filter((v) => !inputs.some((i) => i.value === v)).length;
    }
    $$("#filters input[name], #filters select[name]").forEach((el) => {
      const v = p[el.name];
      if (el.type === "checkbox") el.checked = v === 1 || v === "1" || v === true;
      else if (el.name === "min_cost" || el.name === "max_cost") {
        // Guardado en USD; el campo se escribe en la moneda mostrada
        el.value = v == null ? "" : moneyValue(v);
      } else if (el.tagName === "SELECT") {
        const opt = [...el.options].find((o) => o.value !== "" && Number(o.value) === Number(v));
        el.value = v == null || !opt ? "" : opt.value;
      } else el.value = v == null ? "" : v;
    });
    return missing;
  }

  async function openResults(s, btn) {
    if (btn) btn.disabled = true;
    try {
      let ids = [];
      try { ids = (await api(`/api/saved-searches/${s.id}/results?page=1&page_size=1`)).new_ids || []; }
      catch { /* sin resaltado */ }
      const missing = await applyParams(s.params);
      highlight = ids.length ? { ids: new Set(ids), key: null } : null;
      pendingKey = !!highlight;
      show("search");
      page = 1;
      await search();
      window.scrollTo({ top: 0 });
      if (missing) toast("Algunos filtros de esta búsqueda ya no existen en el catálogo");
      await api(`/api/saved-searches/${s.id}/seen`, { method: "POST" });
      refreshCounts();
      refreshSavedPill();
    } catch (err) { toast(err.message); }
    finally { if (btn) btn.disabled = false; }
  }

  /* Marca "Nuevo" en las tarjetas de la búsqueda guardada abierta */
  DF.cardExtras.push((p) => (highlight && highlight.ids.has(p.id)
    ? `<div class="ss-new-flag"><span class="chip ss-new-chip">★ Nuevo para tu búsqueda</span></div>` : ""));

  /* ---------------- Contador de nuevos en la pestaña ---------------- */
  function pill() {
    let el = $("#savedCount");
    if (!el) {
      const tab = $('.tab[data-view="saved"]');
      if (!tab) return null;
      el = document.createElement("span");
      el.id = "savedCount";
      el.className = "pill ss-pill hidden";
      tab.append(" ", el);
    }
    return el;
  }
  function updatePill() {
    const el = pill();
    if (!el) return;
    const n = items.reduce((a, s) => a + (s.new || 0), 0);
    el.textContent = n;
    el.title = `${n} productos nuevos en tus búsquedas guardadas`;
    el.classList.toggle("hidden", !n);
  }
  async function refreshSavedPill() {
    try {
      const fresh = await fetchList();
      if (!fresh) return;
      items = fresh;
      updatePill();
      renderList();      // también oculta: al volver a la pestaña no se ve un instante la lista vieja
    }
    catch { /* el contador es opcional */ }
  }

  /* La revisión corre en segundo plano poco después de cargar un catálogo: se actualizan los contadores
     un momento después de que app.js los refresca (carga de CSV, monitor…). */
  const baseRefresh = refreshCounts;
  let later;
  // eslint-disable-next-line no-global-assign
  refreshCounts = async function () {
    const r = await baseRefresh();
    clearTimeout(later);
    if (items.some((s) => s.notify)) later = setTimeout(() => { baseRefresh().catch(() => {}); refreshSavedPill(); }, 2500);
    return r;
  };

  /* ---------------- Registro en los puntos de extensión ---------------- */
  DF.onResults.push((data, params) => {
    current = { data, params };
    ensureButton();
    if (highlight) {
      const k = keyOf(params);
      if (pendingKey) { highlight.key = k; pendingKey = false; }
      else if (highlight.key !== k) {           // el usuario cambió la búsqueda: se quita el resaltado
        highlight = null;
        $$("#grid .ss-new-flag").forEach((el) => el.remove());
      }
    }
  });
  DF.viewLoaders.saved = loadSaved;
  DF.currencyChange.push(() => { if ($("#view-saved.active") && items.length) renderList(); });

  refreshSavedPill();
})();
