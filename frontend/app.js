const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const money = (n) => `$${Number(n).toFixed(2)}`;
const fmt = (n) => Number(n).toLocaleString("es");
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const FLAGS = { CN: "🇨🇳", US: "🇺🇸", ES: "🇪🇸", DE: "🇩🇪", MX: "🇲🇽", AR: "🇦🇷", CL: "🇨🇱", CO: "🇨🇴" };

async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: { "Content-Type": "application/json" },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || res.statusText);
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
  if (view === "settings") loadSettings();
}
$$(".tab").forEach((b) => b.addEventListener("click", () => show(b.dataset.view)));

/* ---------------- Búsqueda ---------------- */
let page = 1;

function buildQuery() {
  const p = new URLSearchParams();
  const q = $("#q").value.trim();
  if (q) p.set("q", q);
  p.set("sort", $("#sort").value);
  for (const key of ["category", "supplier", "warehouse"]) {
    $$(`#f-${key} input:checked`).forEach((i) => p.append(key, i.value));
  }
  $$("#filters input[name], #filters select[name]").forEach((el) => {
    if (el.type === "checkbox") { if (el.checked) p.set(el.name, "1"); }
    else if (el.value !== "") p.set(el.name, el.value);
  });
  p.set("page", page);
  return p;
}

async function search() {
  const params = buildQuery();
  $("#resultsInfo").textContent = "Buscando…";
  try {
    const data = await api(`/api/products?${params}`);
    renderGrid(data);
  } catch (e) {
    $("#resultsInfo").textContent = `Error: ${e.message}`;
  }
}

function card(p) {
  return `<article class="card" data-id="${p.id}">
    <div class="img"><img loading="lazy" src="${esc(p.image)}" alt="">
      <span class="badge score" title="Score ganador">★ ${p.score}</span>
      ${p.trending ? `<span class="badge hot">🔥 +${Math.round(p.trend)}%</span>` : ""}</div>
    <div class="body">
      <div class="title">${esc(p.title)}</div>
      <div class="meta"><span class="chip">${esc(p.supplier)}</span><span class="chip">${FLAGS[p.warehouse] || ""} ${p.warehouse}</span>
        <span class="chip">🚚 ${p.shipping_days}d</span><span class="chip">⭐ ${p.rating}</span></div>
      <div class="meta">${fmt(p.orders)} pedidos · ${p.stock > 0 ? `stock ${fmt(p.stock)}` : '<b class="bad">sin stock</b>'}</div>
      <div class="kpis">
        <div><b>${money(p.total_cost)}</b><span>Costo</span></div>
        <div><b>${money(p.suggested_price)}</b><span>Venta</span></div>
        <div><b class="${p.profit >= 0 ? "good" : "bad"}">${money(p.profit)}</b><span>${p.margin}%</span></div>
      </div>
      <div class="actions">${p.imported_id
        ? `<button class="ghost" disabled>✓ Importado</button>`
        : `<button class="primary import" data-id="${p.id}">Importar</button>`}</div>
    </div></article>`;
}

function renderGrid(data) {
  const from = data.total ? (data.page - 1) * data.page_size + 1 : 0;
  const to = Math.min(data.total, data.page * data.page_size);
  $("#resultsInfo").textContent = data.total ? `${fmt(data.total)} productos · mostrando ${from}–${to}` : "Sin resultados";
  $("#grid").innerHTML = data.items.length
    ? data.items.map(card).join("")
    : `<p class="empty">No hay productos que cumplan los filtros. Prueba a relajar alguno.</p>`;
  const pager = $("#pager");
  if (data.pages > 1) {
    pager.innerHTML = `<button class="ghost" data-p="${data.page - 1}" ${data.page <= 1 ? "disabled" : ""}>← Anterior</button>
      <span class="muted">Página ${data.page} de ${data.pages}</span>
      <button class="ghost" data-p="${data.page + 1}" ${data.page >= data.pages ? "disabled" : ""}>Siguiente →</button>`;
  } else pager.innerHTML = "";
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
    await importProduct(Number(imp.dataset.id), imp);
    return;
  }
  const c = e.target.closest(".card");
  if (c) openDetail(Number(c.dataset.id));
});

async function importProduct(id, btn) {
  try {
    if (btn) btn.disabled = true;
    await api("/api/imports", { method: "POST", body: { product_id: id } });
    if (btn) { btn.textContent = "✓ Importado"; btn.className = "ghost"; }
    toast("Producto importado a Mis productos");
    refreshCounts();
  } catch (e) {
    if (btn) btn.disabled = false;
    toast(e.message);
  }
}

$("#searchForm").addEventListener("submit", (e) => { e.preventDefault(); page = 1; search(); });
$("#sort").addEventListener("change", () => { page = 1; search(); });
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
  for (const key of ["category", "supplier", "warehouse"]) {
    $(`#f-${key}`).innerHTML = f[key].map((o) => `<label><input type="checkbox" value="${esc(o.value)}">
      ${key === "warehouse" ? (FLAGS[o.value] || "") + " " : ""}${esc(o.value)}<span class="count">${fmt(o.count)}</span></label>`).join("");
  }
}

/* ---------------- Detalle ---------------- */
async function openDetail(id) {
  const p = await api(`/api/products/${id}`);
  const dlg = $("#detail");
  dlg.innerHTML = `<div class="detail" style="position:relative">
    <img src="${esc(p.image)}" alt="">
    <div class="info">
      <button class="close" aria-label="Cerrar">×</button>
      <div><h2>${esc(p.title)}</h2><p class="muted">${esc(p.category)} · ${esc(p.supplier)} · SKU ${esc(p.supplier_sku)}</p></div>
      <dl>
        <dt>Score ganador</dt><dd>★ ${p.score}/100</dd>
        <dt>Tendencia 7 días</dt><dd class="${p.trend >= 0 ? "good" : "bad"}">${p.trend >= 0 ? "+" : ""}${p.trend}% (${fmt(p.orders_7d)} vs ${fmt(p.orders_prev_7d)})</dd>
        <dt>Pedidos totales</dt><dd>${fmt(p.orders)}</dd>
        <dt>Rating</dt><dd>⭐ ${p.rating} (${fmt(p.reviews)} reseñas)</dd>
        <dt>Costo producto</dt><dd>${money(p.cost)}</dd>
        <dt>Envío</dt><dd>${p.shipping_cost ? money(p.shipping_cost) : "Gratis"} · ${p.shipping_days} días desde ${FLAGS[p.warehouse] || ""} ${p.warehouse}</dd>
        <dt>Precio sugerido</dt><dd>${money(p.suggested_price)}</dd>
        <dt>Punto de equilibrio</dt><dd>${money(p.break_even)}</dd>
        <dt>Ganancia estimada</dt><dd class="${p.profit >= 0 ? "good" : "bad"}">${money(p.profit)} (${p.margin}%)</dd>
        <dt>Stock</dt><dd>${p.stock > 0 ? fmt(p.stock) : '<span class="bad">Sin stock</span>'}</dd>
      </dl>
      <div class="meta">${p.tags.map((t) => `<span class="chip">#${esc(t)}</span>`).join("")}</div>
      ${p.imported_id ? `<button class="ghost" disabled>✓ Ya importado</button>`
        : `<label class="field">Precio de venta<input id="dPrice" type="number" step="0.01" min="0.01" value="${p.suggested_price}"></label>
           <button class="primary" id="dImport">Importar a Mis productos</button>`}
    </div></div>`;
  dlg.showModal();
  $(".close", dlg).onclick = () => dlg.close();
  const b = $("#dImport", dlg);
  if (b) b.onclick = async () => {
    try {
      await api("/api/imports", { method: "POST", body: { product_id: p.id, sell_price: Number($("#dPrice").value) } });
      dlg.close(); toast("Producto importado"); refreshCounts(); search();
    } catch (e) { toast(e.message); }
  };
}
$("#detail").addEventListener("click", (e) => { if (e.target.id === "detail") e.target.close(); });

/* ---------------- Mis productos ---------------- */
async function loadImports() {
  const rows = await api("/api/imports");
  $("#importsEmpty").classList.toggle("hidden", rows.length > 0);
  $("#importsBody").innerHTML = rows.map((r) => `<tr data-id="${r.id}">
    <td><img src="${esc(r.image)}" alt="">${esc(r.title)}${r.alerts ? `<span class="alert-dot" title="Alertas sin leer">⚠ ${r.alerts}</span>` : ""}</td>
    <td>${esc(r.supplier)}</td>
    <td class="num">${money(r.total_cost)}</td>
    <td class="num"><input class="price" type="number" step="0.01" min="0.01" value="${r.sell_price}"></td>
    <td class="num ${r.profit >= 0 ? "good" : "bad"}">${money(r.profit)}</td>
    <td class="num ${r.margin >= 0 ? "good" : "bad"}">${r.margin}%</td>
    <td class="num ${r.stock ? "" : "bad"}">${r.stock ? fmt(r.stock) : "0"}</td>
    <td><button class="status ${r.status}">${r.status === "active" ? "Activo" : "Draft"}</button></td>
    <td><button class="link del">Quitar</button></td></tr>`).join("");
  $("#importsCount").textContent = rows.length;
}

$("#importsBody").addEventListener("change", async (e) => {
  if (!e.target.matches("input.price")) return;
  const id = e.target.closest("tr").dataset.id;
  try { await api(`/api/imports/${id}`, { method: "PATCH", body: { sell_price: Number(e.target.value) } }); toast("Precio actualizado"); }
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
    toast(`Monitor: ${r.checked} productos revisados, ${r.alerts.length} alertas nuevas`);
    loadImports(); refreshCounts();
  } finally { e.target.disabled = false; }
});

/* ---------------- Alertas ---------------- */
async function loadAlerts() {
  const rows = await api("/api/alerts");
  $("#alertsList").innerHTML = rows.length
    ? rows.map((a) => `<li class="${a.type} ${a.read ? "" : "unread"}">${esc(a.title)}: ${esc(a.message)}
        <small>${new Date(a.created_at).toLocaleString("es")}</small></li>`).join("")
    : `<li class="empty">Sin alertas. Importa productos y ejecuta el monitor.</li>`;
}
$("#markRead").addEventListener("click", async () => { await api("/api/alerts/read", { method: "POST" }); loadAlerts(); refreshCounts(); });

async function refreshCounts() {
  const [imports, alerts] = await Promise.all([api("/api/imports"), api("/api/alerts")]);
  $("#importsCount").textContent = imports.length;
  $("#alertsCount").textContent = alerts.filter((a) => !a.read).length;
}

/* ---------------- Reglas ---------------- */
async function loadSettings() {
  const s = await api("/api/settings");
  for (const [k, v] of Object.entries(s)) { const i = $(`#settingsForm [name=${k}]`); if (i) i.value = v; }
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

/* ---------------- Inicio ---------------- */
loadFacets().then(search);
refreshCounts();
