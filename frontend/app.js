const $ = (s, el = document) => el.querySelector(s);

/* Puntos de extensión para los módulos de funcionalidades (js/history.js, js/compare.js, js/saved.js).
   Cada módulo se registra aquí; app.js no los conoce. */
const DF = {
  cardExtras: [],      // f(p) -> HTML extra dentro de la tarjeta de producto (ya escapado)
  detailSections: [],  // f(p, container) -> agrega secciones al detalle (puede ser async)
  viewLoaders: {},     // { nombreVista: f() } al abrir la pestaña data-view="nombreVista"
  onResults: [],       // f(data, params) tras renderizar resultados de búsqueda
  currencyChange: [],  // f() cuando cambia la moneda mostrada (para re-renderizar)
  /* Sesión (la maneja js/auth.js). api() espera a `ready` (sesión iniciada) antes de llamar a la API,
     envía `csrf` en las peticiones que modifican datos y llama a onUnauthorized() ante un 401. */
  auth: { user: null, csrf: null, ready: null, onUnauthorized: null },
  isOwner: () => !!(DF.auth.user && DF.auth.user.role === "owner"),
};
window.DF = DF;
{
  let openGate;
  DF.auth.ready = new Promise((r) => { openGate = r; });
  DF.auth.open = () => openGate();                                         // sesión lista
  DF.auth.close = () => { DF.auth.ready = new Promise((r) => { openGate = r; }); };   // bloquea hasta entrar
}

/* Ejecuta una extensión sin que su error (síncrono o promesa rechazada) rompa la app */
function dfSafe(fn, ...args) {
  try {
    const r = fn(...args);
    if (r && typeof r.then === "function") r.catch((e) => console.error("[DF]", e));
    return r;
  } catch (e) {
    console.error("[DF]", e);
    return undefined;
  }
}
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
/* money(), moneyValue(), moneyToUsd() y Currency vienen de js/currency.js */
const fmt = (n) => Number(n).toLocaleString("es");
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const FLAGS = { CN: "🇨🇳", US: "🇺🇸", ES: "🇪🇸", DE: "🇩🇪", MX: "🇲🇽", AR: "🇦🇷", CL: "🇨🇱", CO: "🇨🇴", PE: "🇵🇪", BR: "🇧🇷", UY: "🇺🇾" };
const SOURCES = { mock: "Demo (ficticio)", csv: "CSV del proveedor", api: "API" };

/* Ilustración local para productos sin foto (o cuya foto no carga). No depende de internet. */
const CAT_STYLE = {
  "Hogar y cocina": ["#e0f2fe", "#0369a1", "🏠"], "Mascotas": ["#fef3c7", "#b45309", "🐾"],
  "Belleza": ["#fce7f3", "#be185d", "💄"], "Fitness": ["#dcfce7", "#15803d", "🏋️"],
  "Electrónica": ["#ede9fe", "#6d28d9", "🔌"], "Bebés y niños": ["#ffedd5", "#c2410c", "🧸"],
  "Auto": ["#e2e8f0", "#334155", "🚗"], "Moda y accesorios": ["#fae8ff", "#a21caf", "👜"],
};
function placeholder(title, category) {
  const [bg, fg, icon] = CAT_STYLE[category] || ["#eef2ff", "#4338ca", "📦"];
  const initials = String(title).split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0]).join("").toUpperCase();
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="300" height="300" viewBox="0 0 300 300">
    <rect width="300" height="300" fill="${bg}"/><circle cx="150" cy="128" r="64" fill="#fff" opacity=".7"/>
    <text x="150" y="150" font-size="64" text-anchor="middle">${icon}</text>
    <text x="150" y="250" font-size="34" font-weight="700" font-family="system-ui,sans-serif" fill="${fg}" text-anchor="middle">${esc(initials)}</text></svg>`;
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
}
const imgTag = (p, cls = "") =>
  `<img class="${cls}" loading="lazy" alt="" src="${esc(p.image || placeholder(p.title, p.category))}"
     data-title="${esc(p.title)}" data-cat="${esc(p.category)}">`;
// Si una foto real falla (enlace roto, sin internet), se reemplaza por la ilustración
document.addEventListener("error", (e) => {
  const img = e.target;
  if (img.tagName === "IMG" && img.dataset.title && !img.src.startsWith("data:")) {
    img.src = placeholder(img.dataset.title, img.dataset.cat);
  }
}, true);
const aliexpressSearch = (title) => `https://www.aliexpress.com/wholesale?SearchText=${encodeURIComponent(title)}`;

async function api(path, opts = {}) {
  const method = (opts.method || "GET").toUpperCase();
  const authCall = path.startsWith("/api/auth/");
  if (!authCall) await DF.auth.ready;          // nada se pide a la API antes de iniciar sesión
  const headers = { "Content-Type": "application/json" };
  if (method !== "GET" && method !== "HEAD" && DF.auth.csrf) headers["X-CSRF-Token"] = DF.auth.csrf;
  const res = await fetch(path, {
    ...opts,
    method,
    headers,
    credentials: "same-origin",
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  let data = {};
  try { data = await res.json(); } catch { /* respuesta sin JSON */ }
  if (res.status === 401 && !authCall && DF.auth.onUnauthorized) dfSafe(DF.auth.onUnauthorized);
  if (!res.ok) {
    const err = new Error((data && data.error) || res.statusText || `Error ${res.status}`);
    err.status = res.status;
    throw err;
  }
  return data;
}

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.remove("show"), 2600);
}

/* ---------------- Navegación ---------------- */
function show(view) {
  $$(".tab").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${view}`));
  if (view === "imports") loadImports();
  if (view === "alerts") loadAlerts();
  if (view === "settings") { loadSettings(); Currency.load(); }
  if (view === "suppliers") loadSuppliers();
  if (view === "search" && DF.deferInitialSearch) { DF.deferInitialSearch = false; search(); }
  if (DF.viewLoaders[view]) dfSafe(DF.viewLoaders[view]);
}
$$(".tab").forEach((b) => b.addEventListener("click", () => show(b.dataset.view)));

/* ---------------- Búsqueda ---------------- */
let page = 1;
let lastParams = "";       // parámetros de la búsqueda que produjo lastData
let lastData = null;       // última respuesta de /api/products (para re-renderizar al cambiar de moneda)
let sortTouched = false;   // el usuario eligió un orden a mano
let exactSearch = false;   // "Buscar en cambio X": pedir la búsqueda sin corrección
const COST_FIELDS = new Set(["min_cost", "max_cost"]);

/* Con texto y sin orden elegido → Relevancia. Sin texto, Relevancia no aplica → Mejor score. */
function syncSort() {
  const q = $("#q").value.trim();
  const sel = $("#sort");
  if (q && !sortTouched) sel.value = "relevance";
  else if (!q && sel.value === "relevance") { sel.value = "score"; sortTouched = false; }
}

function buildQuery() {
  const p = new URLSearchParams();
  const q = $("#q").value.trim();
  if (q) p.set("q", q);
  if (q && exactSearch) p.set("exact", "1");
  p.set("sort", $("#sort").value);
  for (const key of ["source", "category", "supplier", "warehouse"]) {
    $$(`#f-${key} input:checked`).forEach((i) => p.append(key, i.value));
  }
  $$("#filters input[name], #filters select[name]").forEach((el) => {
    if (el.type === "checkbox") { if (el.checked) p.set(el.name, "1"); }
    else if (COST_FIELDS.has(el.name)) {
      // Los costos se escriben en la moneda mostrada; la API filtra en USD
      const usd = moneyToUsd(el.value);
      if (usd !== null) p.set(el.name, usd);
    } else if (el.value !== "") p.set(el.name, el.value);
  });
  p.set("page", page);
  return p;
}

let searchSeq = 0;
async function search() {
  syncSort();
  const params = buildQuery();
  const seq = ++searchSeq;
  $("#resultsInfo").textContent = "Buscando…";
  try {
    const data = await api(`/api/products?${params}`);
    if (seq !== searchSeq) return;   // llegó una búsqueda más nueva
    lastData = data;
    lastParams = params.toString();
    renderGrid(data);
  } catch (e) {
    if (seq !== searchSeq) return;
    $("#resultsInfo").textContent = `Error: ${e.message}`;
    $("#tookMs").textContent = "";
  }
}

/* Lanza una búsqueda de texto desde una sugerencia o corrección */
function searchFor(text, { exact = false } = {}) {
  $("#q").value = text;
  exactSearch = exact;
  page = 1;
  search();
}

function renderHint(data) {
  const box = $("#searchHint");
  const qLink = (t, extra = "") => `<a href="#" data-q="${esc(t)}"${extra}>${esc(t)}</a>`;
  let html = "";
  if (data.corrected_from && exactSearch) {
    // El servidor volvió a corregir aunque pedimos la búsqueda exacta: no hay resultados exactos
    html = `No hay resultados para <b>${esc(data.corrected_from)}</b>; se muestran los de <b>${esc(data.query || data.suggestion || "")}</b>.`;
  } else if (data.corrected_from) {
    const used = data.corrected_query || data.q_used || data.query || data.suggestion;
    html = (used ? `Mostrando resultados para <b>${esc(used)}</b>. ` : "Mostrando resultados con la búsqueda corregida. ") +
      `Buscar en cambio ${qLink(data.corrected_from, ' data-exact="1"')}`;
  } else if (data.suggestion) {
    html = `¿Quisiste decir ${qLink(data.suggestion)}?`;
  }
  box.innerHTML = html;
  box.classList.toggle("hidden", !html);
}
$("#searchHint").addEventListener("click", (e) => {
  const a = e.target.closest("a[data-q]");
  if (!a) return;
  e.preventDefault();
  searchFor(a.dataset.q, { exact: a.dataset.exact === "1" });
});

function card(p) {
  return `<article class="card" data-id="${p.id}">
    <div class="img">${imgTag(p)}
      <span class="badge score" title="Score ganador">★ ${p.score}</span>
      ${p.trending ? `<span class="badge hot">🔥 +${Math.round(p.trend)}%</span>` : ""}
      ${p.source === "mock" ? `<span class="badge demo" title="Producto ficticio de demostración">DEMO</span>` : ""}</div>
    <div class="body">
      <div class="title">${esc(p.title)}</div>
      <div class="meta"><span class="chip">${esc(p.supplier)}</span><span class="chip">${FLAGS[p.warehouse] || ""} ${p.warehouse}</span>
        <span class="chip">🚚 ${p.shipping_days}d</span><span class="chip">⭐ ${p.rating}</span></div>
      <div class="meta">${fmt(p.orders)} pedidos · ${p.stock > 0 ? `stock ${fmt(p.stock)}` : '<b class="bad">sin stock</b>'}</div>
      <div class="kpis">
        <div><b>${money(p.total_cost)}</b><span>Costo</span></div>
        <div><b>${money(p.suggested_price, { kind: "price" })}</b><span>Venta</span></div>
        <div><b class="${p.profit >= 0 ? "good" : "bad"}">${money(p.profit)}</b><span><i class="kpi-label">Ganancia · </i>${p.margin}%</span></div>
      </div>
      ${DF.cardExtras.map((f) => dfSafe(f, p) || "").join("")}
      <div class="actions">${p.imported_id
        ? `<button class="ghost" disabled>✓ Importado</button>`
        : `<button class="primary import" data-id="${p.id}">Importar</button>`}</div>
    </div></article>`;
}

function renderGrid(data) {
  const from = data.total ? (data.page - 1) * data.page_size + 1 : 0;
  const to = Math.min(data.total, data.page * data.page_size);
  $("#resultsInfo").textContent = data.total ? `${fmt(data.total)} productos · mostrando ${from}–${to}` : "Sin resultados";
  $("#tookMs").textContent = Number.isFinite(data.took_ms) ? `· ${fmt(Math.round(data.took_ms))} ms` : "";
  renderHint(data);
  $("#grid").innerHTML = data.items.length
    ? data.items.map(card).join("")
    : `<p class="empty">No hay productos que cumplan los filtros. Prueba a relajar alguno.</p>`;
  const pager = $("#pager");
  if (data.pages > 1) {
    pager.innerHTML = `<button class="ghost" data-p="${data.page - 1}" ${data.page <= 1 ? "disabled" : ""}>← Anterior</button>
      <span class="muted">Página ${data.page} de ${data.pages}</span>
      <button class="ghost" data-p="${data.page + 1}" ${data.page >= data.pages ? "disabled" : ""}>Siguiente →</button>`;
  } else pager.innerHTML = "";
  // Parámetros de la búsqueda que produjo `data` (sin página), no los del formulario actual
  const params = new URLSearchParams(lastParams || "");
  params.delete("page");
  DF.onResults.forEach((f) => dfSafe(f, data, params));
}

$("#pager").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-p]");
  if (!b) return;
  page = Number(b.dataset.p);
  search();
  window.scrollTo({ top: 0, behavior: "smooth" });
});

$("#grid").addEventListener("click", async (e) => {
  const imp = e.target.closest("button.import");
  if (imp) {
    e.stopPropagation();
    const p = lastData && lastData.items.find((x) => x.id === Number(imp.dataset.id));
    await importProduct(Number(imp.dataset.id), imp, p);
    return;
  }
  const c = e.target.closest(".card");
  if (c) openDetail(Number(c.dataset.id));
});

/* En moneda local se guarda el precio redondeado que el usuario ve en la tarjeta (convertido a USD) */
function importBody(id, p) {
  const body = { product_id: id };
  if (p && Currency.isLocal()) {
    const usd = moneyToUsd(moneyValue(p.suggested_price, { kind: "price" }));
    if (usd) body.sell_price = usd;
  }
  return body;
}

async function importProduct(id, btn, p) {
  try {
    if (btn) btn.disabled = true;
    await api("/api/imports", { method: "POST", body: importBody(id, p) });
    if (btn) { btn.textContent = "✓ Importado"; btn.className = "ghost"; }
    toast("Producto importado a Mis productos");
    refreshCounts();
  } catch (e) {
    if (btn) btn.disabled = false;
    toast(e.message);
  }
}

$("#searchForm").addEventListener("submit", (e) => { e.preventDefault(); exactSearch = false; page = 1; search(); });
$("#sort").addEventListener("change", () => { sortTouched = true; page = 1; search(); });
$("#q").addEventListener("input", () => {
  exactSearch = false;
  // Al borrar el texto (también con la ✕ del campo) se vuelve al listado completo
  if (!$("#q").value.trim() && lastData) { page = 1; search(); }
});
let debounce;
$("#filters").addEventListener("input", () => { clearTimeout(debounce); debounce = setTimeout(() => { page = 1; search(); }, 300); });
$("#resetFilters").addEventListener("click", () => {
  $$("#filters input").forEach((i) => { if (i.type === "checkbox") i.checked = i.name === "in_stock"; else i.value = ""; });
  $$("#filters select").forEach((s) => (s.value = ""));
  page = 1; search();
});
$("#toggleFilters").addEventListener("click", () => $("#filters").classList.toggle("open"));

async function loadFacets() {
  const f = await api("/api/facets");
  for (const key of ["source", "category", "supplier", "warehouse"]) {
    const checked = new Set($$(`#f-${key} input:checked`).map((i) => i.value));
    $(`#f-${key}`).innerHTML = f[key].map((o) => {
      const label = key === "source" ? SOURCES[o.value] || o.value
        : key === "warehouse" ? `${FLAGS[o.value] || ""} ${o.value}` : o.value;
      return `<label><input type="checkbox" value="${esc(o.value)}" ${checked.has(o.value) ? "checked" : ""}>
        ${esc(label)}<span class="count">${fmt(o.count)}</span></label>`;
    }).join("");
  }
}

/* ---------------- Detalle ---------------- */
let lastDetail = null;

/* Línea "con IVA" bajo un precio de venta (vacía si el IVA está desactivado) */
function vatLine(usd, kind = "price") {
  const vat = Currency.vat();
  if (!vat) return "";
  const v = moneyValue(usd, { kind });
  if (v === null) return "";
  return moneyLocal(Currency.isLocal() ? Math.round(v * (1 + vat / 100)) : Math.round(v * (1 + vat / 100) * 100) / 100);
}

/* Atributos del <input> de precio de venta en la moneda mostrada */
function priceInputAttrs(value) {
  const local = Currency.isLocal();
  return `type="number" inputmode="decimal" step="${local ? 100 : 0.01}" min="${local ? 0 : 0.01}" value="${value ?? ""}"`;
}

async function openDetail(id) {
  const p = await api(`/api/products/${id}`);
  lastDetail = p;
  $("#detail").innerHTML = "";   // no arrastrar el precio escrito para otro producto
  renderDetail(p);
  $("#detail").showModal();
}

function renderDetail(p) {
  const dlg = $("#detail");
  const typed = $("#dPrice", dlg);
  const vat = vatLine(p.suggested_price);
  const link = p.product_url
    ? `<a class="btn ghost" href="${esc(p.product_url)}" target="_blank" rel="noopener noreferrer">Ver en ${esc(p.supplier)} ↗</a>`
    : p.source === "mock"
      ? `<p class="note">Producto de <b>demostración</b>: es ficticio y no tiene ficha en ningún proveedor.
           <a href="${esc(aliexpressSearch(p.title))}" target="_blank" rel="noopener noreferrer">Buscar similares en AliExpress ↗</a></p>`
      : `<p class="note">El proveedor no incluyó el enlace del producto (columna <code>url</code> del CSV).</p>`;
  dlg.innerHTML = `<div class="detail">
    ${imgTag(p)}
    <div class="info">
      <button class="close" aria-label="Cerrar">×</button>
      <div><h2>${esc(p.title)}</h2><p class="muted">${esc(p.category)} · ${esc(p.supplier)} · SKU ${esc(p.supplier_sku)}</p></div>
      ${link}
      <dl>
        <dt>Score ganador</dt><dd>★ ${p.score}/100</dd>
        <dt>Tendencia 7 días</dt>${p.orders_7d || p.orders_prev_7d
          ? `<dd class="${p.trend >= 0 ? "good" : "bad"}">${p.trend >= 0 ? "+" : ""}${p.trend}% (${fmt(p.orders_7d)} vs ${fmt(p.orders_prev_7d)})</dd>`
          : `<dd class="muted">Sin datos</dd>`}
        <dt>Pedidos totales</dt><dd>${fmt(p.orders)}</dd>
        <dt>Rating</dt><dd>⭐ ${p.rating} (${fmt(p.reviews)} reseñas)</dd>
        <dt>Costo producto</dt><dd>${money(p.cost)}</dd>
        <dt>Envío</dt><dd>${p.shipping_cost ? money(p.shipping_cost) : "Gratis"} · ${p.shipping_days} días desde ${FLAGS[p.warehouse] || ""} ${p.warehouse}</dd>
        <dt>Precio sugerido</dt><dd>${money(p.suggested_price, { kind: "price" })}${vat ? `<small class="vat">con IVA: ${vat}</small>` : ""}</dd>
        <dt>Punto de equilibrio</dt><dd>${money(p.break_even)}</dd>
        <dt>Ganancia estimada</dt><dd class="${p.profit >= 0 ? "good" : "bad"}">${money(p.profit)} (${p.margin}%)</dd>
        <dt>Stock</dt><dd>${p.stock > 0 ? fmt(p.stock) : '<span class="bad">Sin stock</span>'}</dd>
      </dl>
      <div class="meta">${p.tags.map((t) => `<span class="chip">#${esc(t)}</span>`).join("")}</div>
      <div id="detailExtras" class="detail-extras"></div>
      ${p.imported_id ? `<button class="ghost" disabled>✓ Ya importado</button>`
        : `<label class="field">Precio de venta (${Currency.code()})<input id="dPrice" ${priceInputAttrs(moneyValue(p.suggested_price, { kind: "price" }))}>
             <small id="dPriceVat" class="vat"></small></label>
           <button class="primary" id="dImport">Importar a Mis productos</button>`}
    </div></div>`;
  $(".close", dlg).onclick = () => dlg.close();
  const extras = $("#detailExtras", dlg);
  DF.detailSections.forEach((f) => dfSafe(f, p, extras));
  const input = $("#dPrice", dlg);
  if (input) {
    // Si el usuario ya había escrito un precio y luego cambió la moneda, se conserva convertido
    if (typed && typed.dataset.usd) {
      input.dataset.usd = typed.dataset.usd;
      input.value = moneyValue(Number(typed.dataset.usd));
    }
    const showVat = () => {
      const usd = moneyToUsd(input.value);
      const v = usd !== null ? vatLine(usd, "amount") : "";
      $("#dPriceVat", dlg).textContent = v ? `con IVA: ${v}` : "";
    };
    input.addEventListener("input", () => { input.dataset.usd = moneyToUsd(input.value) ?? ""; showVat(); });
    showVat();
  }
  const b = $("#dImport", dlg);
  if (b) b.onclick = async () => {
    const usd = moneyToUsd(input.value);
    if (!usd || usd <= 0) { toast("Escribe un precio de venta válido"); return; }
    try {
      await api("/api/imports", { method: "POST", body: { product_id: p.id, sell_price: usd } });
      dlg.close(); toast("Producto importado"); refreshCounts(); search();
    } catch (e) { toast(e.message); }
  };
}
$("#detail").addEventListener("click", (e) => { if (e.target.id === "detail") e.target.close(); });
$("#detail").addEventListener("close", () => { lastDetail = null; });

/* ---------------- Mis productos ---------------- */
let lastImports = null;
async function loadImports() {
  const rows = await api("/api/imports");
  lastImports = rows;
  renderImports(rows);
}

function renderImports(rows) {
  $("#importsEmpty").classList.toggle("hidden", rows.length > 0);
  $("#importsBody").innerHTML = rows.map((r) => {
    const vat = vatLine(r.sell_price, "amount");
    return `<tr data-id="${r.id}">
    <td>${imgTag(r)}${esc(r.title)}${r.alerts ? `<span class="alert-dot" title="Alertas sin leer">⚠ ${r.alerts}</span>` : ""}</td>
    <td>${r.product_url ? `<a href="${esc(r.product_url)}" target="_blank" rel="noopener noreferrer">${esc(r.supplier)} ↗</a>` : esc(r.supplier)}</td>
    <td class="num">${money(r.total_cost)}</td>
    <td class="num"><input class="price" ${priceInputAttrs(moneyValue(r.sell_price))} aria-label="Precio de venta (${Currency.code()})"
      ${vat ? `title="Con IVA: ${esc(vat)}"` : ""}>${vat ? `<small class="vat">con IVA ${vat}</small>` : ""}</td>
    <td class="num ${r.profit >= 0 ? "good" : "bad"}">${money(r.profit)}</td>
    <td class="num ${r.margin >= 0 ? "good" : "bad"}">${r.margin}%</td>
    <td class="num ${r.stock ? "" : "bad"}">${r.stock ? fmt(r.stock) : "0"}</td>
    <td><button class="status ${r.status}">${r.status === "active" ? "Activo" : "Draft"}</button></td>
    <td><button class="link del">Quitar</button></td></tr>`;
  }).join("");
  $("#importsCount").textContent = rows.length;
}

$("#importsBody").addEventListener("change", async (e) => {
  if (!e.target.matches("input.price")) return;
  const id = e.target.closest("tr").dataset.id;
  const usd = moneyToUsd(e.target.value);
  if (!usd || usd <= 0) { toast("Escribe un precio de venta válido"); loadImports(); return; }
  try { await api(`/api/imports/${id}`, { method: "PATCH", body: { sell_price: usd } }); toast("Precio actualizado"); }
  catch (err) { toast(err.message); }
  loadImports();
});
$("#importsBody").addEventListener("click", async (e) => {
  const tr = e.target.closest("tr");
  if (!tr) return;
  if (e.target.matches(".status")) {
    const status = e.target.classList.contains("active") ? "draft" : "active";
    await api(`/api/imports/${tr.dataset.id}`, { method: "PATCH", body: { status } });
    loadImports();
  } else if (e.target.matches(".del")) {
    await api(`/api/imports/${tr.dataset.id}`, { method: "DELETE" });
    toast("Producto quitado"); loadImports(); refreshCounts();
  }
});

$("#runMonitor").addEventListener("click", async (e) => {
  e.target.disabled = true;
  try {
    const r = await api("/api/monitor/run", { method: "POST" });
    toast(`Monitor: ${r.checked} revisados, ${r.alerts.length} alertas nuevas` +
      (r.skipped ? ` · ${r.skipped} de CSV (se actualizan al volver a subir el archivo)` : "") +
      (r.cooldown ? ` · ${r.cooldown} revisados hace menos de 15 min (se usa el último dato)` : ""));
    loadImports(); refreshCounts();
  } finally { e.target.disabled = false; }
});

/* ---------------- Alertas ---------------- */
async function loadAlerts() {
  const rows = await api("/api/alerts");
  $("#alertsList").innerHTML = rows.length
    ? rows.map((a) => `<li class="${a.type} ${a.read ? "" : "unread"}">${esc(a.title)}: ${esc(a.message)}
        <small>${new Date(a.created_at).toLocaleString("es")}</small></li>`).join("")
    : `<li class="empty">Sin alertas. Importa productos y ejecuta el monitor, o guarda una búsqueda para enterarte de productos nuevos.</li>`;
}
$("#markRead").addEventListener("click", async () => { await api("/api/alerts/read", { method: "POST" }); loadAlerts(); refreshCounts(); });

async function refreshCounts() {
  const [imports, alerts] = await Promise.all([api("/api/imports"), api("/api/alerts/count")]);
  $("#importsCount").textContent = imports.length;
  $("#alertsCount").textContent = alerts.unread;
}

/* ---------------- Proveedores ---------------- */
async function loadSuppliers() {
  const rows = await api("/api/suppliers");
  $("#suppliersBody").innerHTML = rows.length ? rows.map((r) => `<tr>
    <td>${esc(r.supplier)}</td>
    <td><span class="chip ${r.source === "mock" ? "demo-chip" : ""}">${esc(SOURCES[r.source] || r.source)}</span></td>
    <td class="num">${fmt(r.products)}</td><td class="num">${fmt(r.imported)}</td>
    <td>${new Date(r.updated_at).toLocaleString("es")}</td>
    <td>${r.source === "csv" ? `<button class="link del-sup owner-only" data-supplier="${esc(r.supplier)}">Eliminar catálogo</button>` : ""}</td>
  </tr>`).join("") : `<tr><td colspan="6" class="empty">No hay catálogos cargados.</td></tr>`;
  $("#deleteDemo").classList.toggle("hidden", !rows.some((r) => r.source === "mock"));
}

async function reloadCatalog() {
  page = 1;
  await Promise.all([loadSuppliers(), loadFacets()]);
  search(); refreshCounts();
}

$("#suppliersBody").addEventListener("click", async (e) => {
  const b = e.target.closest(".del-sup");
  if (!b || !confirm(`¿Eliminar el catálogo de "${b.dataset.supplier}"? Los productos ya importados se conservan.`)) return;
  const r = await api(`/api/catalog?source=csv&supplier=${encodeURIComponent(b.dataset.supplier)}`, { method: "DELETE" });
  toast(`${fmt(r.deleted)} productos eliminados`);
  reloadCatalog();
});

$("#deleteDemo").addEventListener("click", async () => {
  if (!confirm("¿Eliminar todos los productos de demostración? Los que ya importaste se conservan. No se puede deshacer.")) return;
  const r = await api("/api/catalog?source=mock", { method: "DELETE" });
  toast(`${fmt(r.deleted)} productos de demostración eliminados`);
  reloadCatalog();
});

$("#csvForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.target;
  const file = form.file.files[0];
  const out = $("#csvResult");
  if (!file) return;
  const btn = $("button[type=submit]", form);
  btn.disabled = true;
  out.innerHTML = `<p class="muted">Procesando ${esc(file.name)}…</p>`;
  try {
    const text = await readCsvFile(file);
    const r = await api("/api/catalog/csv", {
      method: "POST", body: { supplier: form.supplier.value, csv: text, replace: form.replace.checked },
    });
    const cols = Object.entries(r.columns).map(([h, c]) => `<code>${esc(h)}</code>→${esc(c)}`).join(", ");
    out.innerHTML = `<div class="result ok">✓ <b>${esc(r.supplier)}</b>: ${fmt(r.new)} nuevos, ${fmt(r.updated)} actualizados` +
      (r.removed ? `, ${fmt(r.removed)} quitados` : "") + (r.alerts ? `, ${fmt(r.alerts)} alertas generadas` : "") +
      `<br><small>Columnas reconocidas: ${cols}</small>` +
      (r.errors.length ? `<details><summary>${r.errors.length} filas con problemas (omitidas)</summary><ul>${r.errors.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></details>` : "") +
      `</div>`;
    form.reset();
    reloadCatalog();
  } catch (err) {
    out.innerHTML = `<div class="result err">✗ ${esc(err.message)}</div>`;
  } finally { btn.disabled = false; }
});

/* Excel en español suele guardar CSV en Windows-1252: si no es UTF-8 válido, se reintenta así. */
async function readCsvFile(file) {
  const buf = await file.arrayBuffer();
  try { return new TextDecoder("utf-8", { fatal: true }).decode(buf); }
  catch { return new TextDecoder("windows-1252").decode(buf); }
}

/* ---------------- Reglas ---------------- */
async function loadSettings() {
  const s = await api("/api/settings");
  for (const [k, v] of Object.entries(s)) { const i = $(`#settingsForm [name=${k}]`); if (i) i.value = v; }
  $$("#settingsForm input").forEach((i) => { i.disabled = !DF.isOwner(); });
}
$("#settingsForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = Object.fromEntries(new FormData(e.target));
  try {
    await api("/api/settings", { method: "PUT", body });
    $("#settingsMsg").textContent = "Guardado ✓ — las métricas se recalcularon.";
    search();
  } catch (err) { $("#settingsMsg").textContent = err.message; }
});

/* ---------------- Moneda ---------------- */
const fmtRate = (n) => new Intl.NumberFormat("es-CO", { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(n);
const fmtIsoDate = (iso) => { const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso || ""); return m ? `${m[3]}/${m[2]}/${m[1]}` : String(iso || ""); };
let shown = { code: "USD", rate: 1 };   // moneda/tasa con que están escritos los filtros de costo

/* Texto de estado de la tasa: {text, level: "ok" | "info" | "warn"} */
function rateStatus() {
  const i = Currency.rateInfo;
  if (!Currency.available) return { text: "", level: "ok" };
  if (!i || i.source === "none" || !(Number(i.base_rate) > 0)) {
    return { text: `⚠ Sin tasa de cambio disponible: los precios se muestran en USD.${i && i.error ? ` (${i.error})` : ""}`, level: "warn" };
  }
  let text; let level = "ok";
  if (i.source === "manual") {
    text = `ℹ Tasa manual: $ ${fmtRate(i.base_rate)} por USD`; level = "info";
  } else {
    text = `TRM $ ${fmtRate(i.base_rate)}${i.date ? ` del ${fmtIsoDate(i.date)}` : ""} · fuente: datos.gov.co`;
    if (i.stale) { text = `⚠ ${text} · desactualizada`; level = "warn"; }
  }
  if (Number(i.hedge_pct) > 0 && Number(i.effective_rate) > 0) {
    text += ` · con cobertura +${i.hedge_pct} %: $ ${fmtRate(i.effective_rate)}`;
  }
  if (i.error) { text += ` · ⚠ No se pudo actualizar la TRM: ${i.error}`; level = "warn"; }
  return { text, level };
}

function fillCurrencyForm() {
  const f = $("#currencyForm");
  const st = Currency.settings;
  f.display_currency.value = st.display_currency || "USD";
  f.rate_mode.value = st.rate_mode || "trm";
  f.manual_rate.value = st.manual_rate ?? "";
  f.hedge_pct.value = st.hedge_pct ?? 0;
  f.cop_rounding.value = String(st.cop_rounding ?? "900");
  f.vat_enabled.checked = !!st.vat_enabled;
  f.vat_pct.value = st.vat_pct ?? 19;
  syncCurrencyForm();
}
function syncCurrencyForm() {
  const f = $("#currencyForm");
  $$("input, select, button", f).forEach((el) => (el.disabled = !Currency.available));
  if (!Currency.available) return;
  f.manual_rate.disabled = f.rate_mode.value !== "manual";
  f.vat_pct.disabled = !f.vat_enabled.checked;
  // La tasa de cambio y la cobertura son de la instalación: solo el propietario las cambia
  if (!DF.isOwner()) ["rate_mode", "manual_rate", "hedge_pct"].forEach((n) => { f[n].disabled = true; });
  $("#hedgeEcho").textContent = f.hedge_pct.value || 0;
}

function onCurrencyChange() {
  const code = Currency.code();
  const rate = Currency.rate();
  const sel = $("#currencySel");
  sel.value = Currency.available ? (Currency.settings.display_currency || "USD") : "USD";
  [...sel.options].forEach((o) => (o.disabled = o.value !== "USD" && !Currency.available));
  sel.title = Currency.available ? "Moneda en la que se muestran los precios" : "Conversión de moneda no disponible";

  // Filtros de costo: etiqueta en la moneda mostrada y valores convertidos si cambió la moneda
  $("#costLabel").textContent = `Costo total (${code})`;
  let costFilters = false;
  $$("#filters input[name=min_cost], #filters input[name=max_cost]").forEach((i) => {
    i.step = code === "USD" ? "1" : "1000";
    if (i.value === "") return;
    costFilters = true;
    if (shown.code !== code) {
      const v = (Number(i.value) / shown.rate) * rate;
      i.value = code === "USD" ? Math.round(v * 100) / 100 : Math.round(v);
    }
  });
  const rateChanged = shown.code === code && shown.rate !== rate;
  shown = { code, rate };

  // Tasa visible
  const st = rateStatus();
  const status = $("#rateStatus");
  status.textContent = st.text;
  status.className = `rate-status ${st.level}`;
  const badge = $("#rateBadge");
  badge.textContent = code !== "USD" ? `1 USD = ${moneyLocal(Math.round(rate))}` : "";
  badge.title = st.text;
  badge.classList.toggle("warn", code !== "USD" && st.level === "warn");
  $("#currencyUnavailable").classList.toggle("hidden", Currency.available);
  fillCurrencyForm();

  // Re-renderizar todo lo que muestra dinero
  if (costFilters && rateChanged) search();
  else if (lastData) renderGrid(lastData);
  if (lastImports) renderImports(lastImports);
  if (lastDetail && $("#detail").open) renderDetail(lastDetail);
  DF.currencyChange.forEach((f) => dfSafe(f));
}
Currency.onChange(onCurrencyChange);

$("#currencySel").addEventListener("change", async (e) => {
  const prev = Currency.settings.display_currency || "USD";
  e.target.disabled = true;
  try { await Currency.save({ display_currency: e.target.value }); }
  catch (err) {
    e.target.value = prev;
    toast(err.status === 404 ? "La conversión de moneda aún no está disponible en el servidor" : err.message);
  } finally { e.target.disabled = false; }
});

$("#currencyForm").addEventListener("input", syncCurrencyForm);
$("#currencyForm").addEventListener("change", syncCurrencyForm);
$("#currencyForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  const msg = $("#currencyMsg");
  const body = {
    display_currency: f.display_currency.value, rate_mode: f.rate_mode.value,
    hedge_pct: Number(f.hedge_pct.value || 0), cop_rounding: f.cop_rounding.value,
    vat_enabled: f.vat_enabled.checked, vat_pct: Number(f.vat_pct.value || 0),
  };
  if (f.manual_rate.value !== "") body.manual_rate = Number(f.manual_rate.value);
  else if (body.rate_mode === "manual") { msg.textContent = "Escribe la tasa manual (COP por 1 USD)."; return; }
  try {
    await Currency.save(body);
    msg.textContent = "Guardado ✓";
  } catch (err) { msg.textContent = `⚠ ${err.message}`; }
});
$("#refreshRate").addEventListener("click", async (e) => {
  const msg = $("#currencyMsg");
  e.target.disabled = true;
  msg.textContent = "Consultando la TRM…";
  try {
    await Currency.refresh();
    const err = Currency.rateInfo && Currency.rateInfo.error;
    msg.textContent = err ? "⚠ No se pudo obtener la TRM; se usa la última guardada." : "TRM actualizada ✓";
  } catch (err) { msg.textContent = `⚠ ${err.message}`; }
  finally { e.target.disabled = !Currency.available; }
});

/* ---------------- Autocompletado ---------------- */
const norm = (s) => String(s).normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase().trim();
initSuggest({
  input: $("#q"),
  onSelect(item) {
    // Una categoría se aplica como filtro (si existe en las facetas); lo demás se busca como texto
    if (item.type === "categoria") {
      const box = $$("#f-category input").find((i) => norm(i.value) === norm(item.text));
      if (box) {
        box.checked = true;
        $("#q").value = "";
        exactSearch = false; page = 1; search();
        toast(`Filtro de categoría: ${box.value}`);
        return;
      }
    }
    searchFor(item.text);
  },
});

/* ---------------- Inicio ----------------
   js/auth.js llama a DF.start() cuando hay una sesión iniciada (antes no se pide nada a la API).
   Si la vista inicial no es Buscar (el Panel lo es), la primera búsqueda se hace al abrir Buscar. */
DF.deferInitialSearch = !$("#view-search").classList.contains("active");
let started = false;
DF.start = () => {
  if (started) return;
  started = true;
  Promise.allSettled([loadFacets(), Currency.load()]).then(() => { if (!DF.deferInitialSearch && searchSeq === 0) search(); });
  refreshCounts();
};
