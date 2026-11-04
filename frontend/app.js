const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const money = (n) => `$${Number(n).toFixed(2)}`;
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
  if (view === "suppliers") loadSuppliers();
}
$$(".tab").forEach((b) => b.addEventListener("click", () => show(b.dataset.view)));

/* ---------------- Búsqueda ---------------- */
let page = 1;

function buildQuery() {
  const p = new URLSearchParams();
  const q = $("#q").value.trim();
  if (q) p.set("q", q);
  p.set("sort", $("#sort").value);
  for (const key of ["source", "category", "supplier", "warehouse"]) {
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
async function openDetail(id) {
  const p = await api(`/api/products/${id}`);
  const dlg = $("#detail");
  const link = p.product_url
    ? `<a class="btn ghost" href="${esc(p.product_url)}" target="_blank" rel="noopener noreferrer">Ver en ${esc(p.supplier)} ↗</a>`
    : p.source === "mock"
      ? `<p class="note">Producto de <b>demostración</b>: es ficticio y no tiene ficha en ningún proveedor.
           <a href="${esc(aliexpressSearch(p.title))}" target="_blank" rel="noopener noreferrer">Buscar similares en AliExpress ↗</a></p>`
      : `<p class="note">El proveedor no incluyó el enlace del producto (columna <code>url</code> del CSV).</p>`;
  dlg.innerHTML = `<div class="detail" style="position:relative">
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
    <td>${imgTag(r)}${esc(r.title)}${r.alerts ? `<span class="alert-dot" title="Alertas sin leer">⚠ ${r.alerts}</span>` : ""}</td>
    <td>${r.product_url ? `<a href="${esc(r.product_url)}" target="_blank" rel="noopener noreferrer">${esc(r.supplier)} ↗</a>` : esc(r.supplier)}</td>
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
    toast(`Monitor: ${r.checked} revisados, ${r.alerts.length} alertas nuevas` +
      (r.skipped ? ` · ${r.skipped} de CSV (se actualizan al volver a subir el archivo)` : ""));
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

/* ---------------- Proveedores ---------------- */
async function loadSuppliers() {
  const rows = await api("/api/suppliers");
  $("#suppliersBody").innerHTML = rows.length ? rows.map((r) => `<tr>
    <td>${esc(r.supplier)}</td>
    <td><span class="chip ${r.source === "mock" ? "demo-chip" : ""}">${esc(SOURCES[r.source] || r.source)}</span></td>
    <td class="num">${fmt(r.products)}</td><td class="num">${fmt(r.imported)}</td>
    <td>${new Date(r.updated_at).toLocaleString("es")}</td>
    <td>${r.source === "csv" ? `<button class="link del-sup" data-supplier="${esc(r.supplier)}">Eliminar catálogo</button>` : ""}</td>
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
