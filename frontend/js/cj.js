/* Módulo cj: panel «CJ Dropshipping (API)» de la pestaña Proveedores (conexión, estado, importación y desconexión).
 * Contrato: GET/POST/DELETE /api/suppliers/cj, POST /api/suppliers/cj/validate, GET /api/suppliers/cj/categories y
 * POST /api/suppliers/cj/import (ver docs/adr/0004-proveedor-cj.md). La API key solo vive en el <input> del formulario:
 * se envía y se vacía; el servidor nunca la devuelve. Todo texto del servidor se escapa con esc() antes de pintarse.
 * Las funciones puras también se cargan en Node (tests/js/cj_helpers.test.js). Usa las globales de app.js. */
(function (root) {
  "use strict";

  const MAX_IMPORT = 200;

  /* ============================================================ funciones puras */

  /* API key escrita por el usuario: sin espacios alrededor, sin blancos ni caracteres de control, hasta 200. */
  function cleanApiKey(value) {
    const v = String(value == null ? "" : value).trim();
    if (!v) return { ok: false, error: "Escribe la API key de CJ." };
    if (v.length > 200) return { ok: false, error: "La API key es demasiado larga (máx. 200 caracteres)." };
    if (/[^\x21-\x7e]/.test(v)) return { ok: false, error: "La API key no puede tener espacios ni caracteres especiales." };
    return { ok: true, value: v };
  }

  /* Cuerpo de «Importar»: palabra clave y/o categoría y un tope entre 1 y 200 (el servidor vuelve a validar). */
  function importBody(query, category, limit, max) {
    const top = max || MAX_IMPORT;
    const q = String(query == null ? "" : query).replace(/\s+/g, " ").trim().slice(0, 100);
    const cat = String(category == null ? "" : category).trim();
    if (!q && !cat) return { error: "Escribe una palabra clave o elige una categoría." };
    let n = Number.parseInt(limit, 10);
    if (!Number.isFinite(n)) n = 50;
    n = Math.min(top, Math.max(1, n));
    const body = { query: q, limit: n };
    if (cat) body.category_id = cat;
    return { body };
  }

  /* Estado de la conexión → insignia (texto + icono, nunca solo color) y detalle. */
  function statusInfo(st) {
    if (!st || !st.connected) return { tone: "off", badge: "◌ No conectado", detail: "" };
    if (st.status === "error") return { tone: "err", badge: "⚠ Con error", detail: st.last_error || "Vuelve a validar la conexión." };
    return { tone: "ok", badge: "✓ Conectado", detail: "" };
  }

  /* Progreso de la importación: texto, porcentaje (0-100) y si sigue en curso. */
  function progressInfo(job) {
    if (!job) return null;
    const running = job.state === "running";
    let percent = 0;
    if (job.phase === "stock" && job.total > 0) percent = Math.round(100 * Math.min(job.done, job.total) / job.total);
    else if (job.phase === "save") percent = 100;
    else if (job.state === "done") percent = 100;
    let text = job.message || "";
    if (running && job.phase === "search") text = `Buscando productos en CJ… (${job.fetched || 0} encontrados)`;
    if (running && job.phase === "stock") text = `Consultando stock: ${job.done || 0} de ${job.total || 0}`;
    return { running, percent, text, state: job.state };
  }

  /* Resumen final (lista de frases) de una importación terminada. */
  function resultLines(job) {
    if (!job || job.state === "running") return [];
    const out = [];
    if (job.state === "error") { out.push(job.error || "La importación falló."); return out; }
    out.push(`${job.new || 0} nuevos, ${job.updated || 0} actualizados.`);
    if (job.alerts) out.push(`${job.alerts} alertas generadas para tus productos importados.`);
    if (job.invalid) out.push(`${job.invalid} productos de CJ omitidos por datos inválidos (sin precio o título).`);
    if (job.conflicts) out.push(`${job.conflicts} omitidos porque ya existen cargados por ti (CSV) con el mismo código.`);
    if (job.stock_unknown) out.push(`${job.stock_unknown} sin dato de stock en CJ (quedan en 0 hasta la próxima revisión).`);
    if (job.stock_failed) out.push(`${job.stock_failed} no se importaron porque no se pudo consultar su stock; vuelve a importar.`);
    return out;
  }

  /* Aplana las categorías para el <select>: [{id, label}] con tope. */
  function categoryOptions(items) {
    return (Array.isArray(items) ? items : []).filter((c) => c && typeof c.id === "string" && typeof c.name === "string")
      .slice(0, 2000).map((c) => ({ id: c.id, label: String(c.path || c.name).slice(0, 120) }));
  }

  const pure = { MAX_IMPORT, cleanApiKey, importBody, statusInfo, progressInfo, resultLines, categoryOptions };
  if (typeof module !== "undefined" && module.exports) { module.exports = pure; return; }

  /* ============================================================ navegador */

  const T = { state: null, timer: null, catsLoaded: false, busy: false };
  const active = () => !!($("#view-suppliers") && $("#view-suppliers").classList.contains("active"));
  const when = (iso) => { const d = new Date(iso); return Number.isNaN(d.getTime()) ? "" : d.toLocaleString("es"); };

  function renderStatus() {
    const st = T.state;
    const info = statusInfo(st);
    const parts = [`<span class="cj-badge cj-${info.tone}">${esc(info.badge)}</span>`];
    if (st && st.connected) {
      parts.push(`<span>${fmt(st.products)} productos de CJ en el catálogo</span>`);
      if (st.last_validated_at) parts.push(`<span class="muted">Validado ${esc(when(st.last_validated_at))}</span>`);
      if (st.last_import_at) parts.push(`<span class="muted">Última importación ${esc(when(st.last_import_at))}</span>`);
    } else if (st && !st.can_manage) {
      parts.push(`<span class="muted">El propietario puede conectar CJ.</span>`);
    }
    if (info.detail) parts.push(`<span class="cj-error">${esc(info.detail)}</span>`);
    $("#cjStatus").innerHTML = parts.join("");
    const connected = !!(st && st.connected);
    $("#cjConnectForm").classList.toggle("hidden", connected);
    $("#cjImportForm").classList.toggle("hidden", !connected);
    $("#cjValidate").classList.toggle("hidden", !connected);
    $("#cjDisconnect").classList.toggle("hidden", !connected);
    const running = !!(st && st.import && st.import.state === "running");
    $$("button, input, select", $("#cjImportForm")).forEach((el) => { el.disabled = running; });
    $("#cjDisconnect").disabled = running;
  }

  function renderProgress() {
    const job = T.state && T.state.import;
    const p = progressInfo(job);
    const box = $("#cjProgress");
    if (!p) { box.innerHTML = ""; return; }
    if (p.running) {
      box.innerHTML = `<p class="muted">${esc(p.text)}</p><progress max="100" value="${Number(p.percent) || 0}"></progress>`;
      return;
    }
    const lines = resultLines(job);
    const warns = (job.warnings || []).map((w) => `<li>${esc(w)}</li>`).join("");
    box.innerHTML = `<div class="result ${job.state === "error" ? "err" : "ok"}">${job.state === "error" ? "✗" : "✓"} ` +
      `${lines.map(esc).join("<br>")}` + (warns ? `<ul>${warns}</ul>` : "") + `</div>`;
  }

  function schedulePoll() {
    clearTimeout(T.timer);
    const running = T.state && T.state.import && T.state.import.state === "running";
    if (running && active()) T.timer = setTimeout(() => { load(); }, 1500);
  }

  async function load() {
    try {
      const was = T.state && T.state.import && T.state.import.state === "running";
      T.state = await api("/api/suppliers/cj");
      renderStatus();
      renderProgress();
      if (was && T.state.import && T.state.import.state !== "running") {     // terminó: refrescar catálogo y tabla
        if (typeof reloadCatalog === "function") reloadCatalog();
      }
      if (T.state.connected && T.state.can_manage && !T.catsLoaded) loadCategories();
      schedulePoll();
    } catch (e) {
      $("#cjStatus").innerHTML = `<span class="cj-error">${esc(e.message || "No se pudo cargar el estado de CJ")}</span>`;
    }
  }

  async function loadCategories() {
    T.catsLoaded = true;
    try {
      const r = await api("/api/suppliers/cj/categories");
      const sel = $("#cjImportForm").category_id;
      categoryOptions(r.items).forEach((c) => {
        const o = document.createElement("option");
        o.value = c.id;
        o.textContent = c.label;
        sel.appendChild(o);
      });
    } catch { T.catsLoaded = false; /* sin categorías: queda la palabra clave */ }
  }

  $("#cjConnectForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const form = e.target;
    const k = cleanApiKey(form.api_key.value);
    if (!k.ok) { toast(k.error); return; }
    const btn = $("button[type=submit]", form);
    btn.disabled = true;
    btn.textContent = "Validando con CJ…";
    try {
      await api("/api/suppliers/cj", { method: "POST", body: { api_key: k.value } });
      form.reset();                                   // la clave no se queda en la página
      T.catsLoaded = false;
      toast("CJ conectado");
      await load();
    } catch (err) {
      toast(err.message);
    } finally {
      form.api_key.value = "";
      btn.disabled = false;
      btn.textContent = "Conectar y validar";
    }
  });

  $("#cjImportForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    const r = importBody(f.query.value, f.category_id.value, f.limit.value, (T.state && T.state.max_import) || MAX_IMPORT);
    if (r.error) { toast(r.error); return; }
    try {
      await api("/api/suppliers/cj/import", { method: "POST", body: r.body });
      await load();
    } catch (err) { toast(err.message); }
  });

  $("#cjValidate").addEventListener("click", async () => {
    try { await api("/api/suppliers/cj/validate", { method: "POST" }); toast("La conexión con CJ funciona"); }
    catch (err) { toast(err.message); }
    load();
  });

  $("#cjDisconnect").addEventListener("click", async () => {
    if (!confirm("¿Desconectar CJ? Los productos ya importados se conservan, pero DropFinder dejará de revisar su precio y stock.")) return;
    try {
      const r = await api("/api/suppliers/cj", { method: "DELETE" });
      toast(`CJ desconectado (${fmt(r.products_kept)} productos conservados)`);
      T.catsLoaded = false;
      const sel = $("#cjImportForm").category_id;
      while (sel.options.length > 1) sel.remove(1);
    } catch (err) { toast(err.message); }
    load();
  });

  DF.viewLoaders.suppliers = () => { load(); };
})(typeof window !== "undefined" ? window : globalThis);
