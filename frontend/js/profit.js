/* Módulo profit: pestaña «Rentabilidad» (calculadora de rentabilidad contraentrega) y el botón «Calcular contraentrega»
 * del detalle de producto. Las fórmulas viven SOLO en el servidor (backend/profit.py, docs/profit-calculator.md): aquí se
 * lee el formulario, se llama a POST /api/profit/calc con un pequeño retraso y se dibuja el resultado.
 * Las funciones puras (parseNumber, buildPayload, verdict…) también se cargan en Node para pruebas (tests/js/profit_helpers.test.js).
 * Usa las globales de app.js: $, $$, esc, api, toast, show, DF y de currency.js: Currency. Sin estilos ni manejadores en línea. */
(function (root) {
  "use strict";

  /* ============================================================ funciones puras */

  /* Campos del formulario: clave de la API → etiqueta, tipo (money | pct) y ayuda. Orden = orden en pantalla. */
  const GROUPS = [
    { id: "product", title: "Producto", fields: [
      { key: "price", label: "Precio de venta", type: "money", required: true, help: "Lo que paga el cliente al recibir." },
      { key: "product_cost", label: "Costo del producto", type: "money", required: true, help: "Lo que te cobra el proveedor." },
      { key: "supplier_shipping", label: "Envío del proveedor", type: "money", help: "Si no aplica, déjalo en 0." },
    ] },
    { id: "logistics", title: "Logística", fields: [
      { key: "delivery_freight", label: "Flete de entrega", type: "money", help: "Lo cobra la transportadora por cada pedido despachado." },
      { key: "return_freight", label: "Flete de devolución", type: "money", help: "Lo que cuesta el regreso cuando el cliente no recibe." },
      { key: "packaging", label: "Costo de empaque", type: "money" },
    ] },
    { id: "cod", title: "Contraentrega y devoluciones", fields: [
      { key: "return_rate_pct", label: "Devoluciones / no recibidos (%)", type: "pct", help: "Pedidos despachados que no se entregan." },
      { key: "delivery_rate_pct", label: "Efectividad de entrega (%)", type: "pct", help: "Es 100 − devoluciones: editar una cambia la otra.", derived: true },
      { key: "cod_fee_pct", label: "Comisión de recaudo (%)", type: "pct", help: "Porcentaje del valor recaudado que cobra la transportadora o la pasarela." },
      { key: "cod_fee_fixed", label: "Comisión fija de recaudo", type: "money", help: "Valor fijo por pedido entregado." },
      { key: "return_product_loss_pct", label: "Producto devuelto que se pierde (%)", type: "pct", help: "Parte de las devoluciones donde el producto no se puede revender." },
    ] },
  ];
  const ALL_FIELDS = GROUPS.flatMap((g) => g.fields);
  /* Los que tienen un supuesto guardable por usuario (coinciden con DEFAULT_KEYS del servidor, salvo el umbral de aviso) */
  const DEFAULT_FIELDS = ["delivery_freight", "return_freight", "packaging", "cod_fee_pct", "cod_fee_fixed",
    "return_rate_pct", "return_product_loss_pct"];
  const MAX_ROAS = 100, MIN_ROAS = 0.01;

  /* Convierte lo que el usuario escribe a número: "12.500" · "12,5" · "1.234,56" · "$ 89.900".
   * null = vacío; NaN = no es un número. `decimals` (de la moneda) desambigua un único separador seguido de 3 dígitos:
   * en COP (0 decimales) "12.500" son doce mil quinientos; en USD el punto es decimal y la coma, de miles. */
  function parseNumber(text, decimals = 2) {
    if (text === null || text === undefined) return null;
    let s = String(text).replace(/USD|COP|US|[\s\u00a0$]/gi, "");
    if (s === "") return null;
    if (!/^-?[0-9.,]*[0-9]$/.test(s) || /[.,]{2}/.test(s)) return NaN;
    const neg = s.startsWith("-");
    if (neg) s = s.slice(1);
    const dots = (s.match(/\./g) || []).length, commas = (s.match(/,/g) || []).length;
    let dec = null;                                   // separador decimal elegido (null = todos son de miles)
    if (dots && commas) dec = s.lastIndexOf(".") > s.lastIndexOf(",") ? "." : ",";
    else if (dots + commas === 1) {
      const sep = dots ? "." : ",";
      const thousands = s.length - s.indexOf(sep) - 1 === 3 && s.indexOf(sep) > 0 && (sep === "," || decimals === 0);
      dec = thousands ? null : sep;
    }
    let out;
    if (dec) {
      const other = dec === "." ? "," : ".";
      const i = s.lastIndexOf(dec);
      const head = s.slice(0, i).split(other).join(""), tail = s.slice(i + 1);
      if (/[.,]/.test(head) || /[.,]/.test(tail)) return NaN;
      out = `${head}.${tail}`;
    } else {
      if (dots + commas && !/^\d{1,3}([.,]\d{3})+$/.test(s)) return NaN;   // miles: grupos de 3 dígitos
      out = s.replace(/[.,]/g, "");
    }
    const n = Number(out);
    return Number.isFinite(n) ? (neg ? -n : n) : NaN;
  }

  /* 100 − v (para ligar «devoluciones» y «efectividad»); vacío/ inválido → "" */
  function complementPct(text, decimals = 2) {
    const n = parseNumber(text, decimals);
    if (n === null || !Number.isFinite(n) || n < 0 || n > 100) return "";
    return String(Math.round((100 - n) * 100) / 100);
  }

  /* Texto del formulario → carga útil de la API. values: {clave: texto}. mode: "cpa" | "roas" con `ad`.
   * Los vacíos NO se envían (el servidor usa tus supuestos). Devuelve {payload, errors:{clave: mensaje}, missing:[claves obligatorias vacías]}. */
  function buildPayload(values, { currency = "COP", mode = "cpa", ad = "" } = {}) {
    const decimals = currency === "COP" ? 0 : 2;
    const payload = { currency };
    const errors = {};
    const missing = [];                               // obligatorios vacíos: no es un error mientras el usuario escribe
    for (const f of ALL_FIELDS) {
      if (f.derived) continue;                        // la efectividad se envía como devoluciones
      const n = parseNumber(values[f.key], decimals);
      if (n === null) { if (f.required) missing.push(f.key); continue; }
      if (Number.isNaN(n)) { errors[f.key] = `${f.label}: escribe un número`; continue; }
      if (n < 0) { errors[f.key] = `${f.label}: no puede ser negativo`; continue; }
      if (f.type === "pct" && n > 100) { errors[f.key] = `${f.label}: debe estar entre 0 y 100`; continue; }
      payload[f.key] = n;
    }
    const a = parseNumber(ad, mode === "cpa" ? decimals : 2);
    const adKey = mode === "roas" ? "target_roas" : "cpa";
    if (a !== null) {
      if (Number.isNaN(a)) errors[adKey] = "Escribe un número";
      else if (a < 0) errors[adKey] = "No puede ser negativo";
      else if (mode === "roas" && (a < MIN_ROAS || a > MAX_ROAS)) errors[adKey] = `El ROAS objetivo debe estar entre ${MIN_ROAS} y ${MAX_ROAS}`;
      else payload[adKey] = a;
    }
    return { payload, errors, missing };
  }

  function formatPct(v) {
    if (v === null || v === undefined || !Number.isFinite(Number(v))) return "—";
    return `${Number(v).toLocaleString("es", { maximumFractionDigits: 1 })} %`;
  }
  function formatRoas(v, empty = "No alcanzable") {
    if (v === null || v === undefined || !Number.isFinite(Number(v))) return empty;
    return `${Number(v).toLocaleString("es", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}×`;
  }

  /* Veredicto de un resultado de la API: tono (clase CSS) y texto. Siempre texto además del color. */
  function verdict(r) {
    if (!r || typeof r.profit_per_confirmed !== "number") return { tone: "muted", icon: "", label: "Completa precio y costo para calcular" };
    if (r.profit_per_confirmed < 0) return { tone: "bad", icon: "✕", label: "Pierdes dinero con estos supuestos" };
    if (r.margin_pct === null || r.margin_pct < 10) return { tone: "warn", icon: "!", label: "Margen bajo: cualquier desvío te deja en pérdida" };
    return { tone: "good", icon: "✓", label: "Rentable con estos supuestos" };
  }

  /* Filas del resumen (etiqueta, valor, ayuda). fmt(valor) da el monto en la moneda del resultado. */
  function summaryRows(r, fmt) {
    return [
      { key: "profit_per_delivered", label: "Ganancia por pedido entregado", value: r.profit_per_delivered === null ? "—" : fmt(r.profit_per_delivered),
        help: "Cada entrega absorbe las pérdidas de las devoluciones y la publicidad.", tone: toneOf(r.profit_per_delivered) },
      { key: "profit_per_confirmed", label: "Ganancia por pedido confirmado", value: fmt(r.profit_per_confirmed),
        help: "Promedio esperado por cada pedido que despachas.", tone: toneOf(r.profit_per_confirmed) },
      { key: "margin_pct", label: "Margen", value: formatPct(r.margin_pct), help: "Sobre lo que realmente cobras (pedidos entregados).", tone: toneOf(r.margin_pct) },
      { key: "breakeven_roas", label: "ROAS de equilibrio", value: formatRoas(r.breakeven_roas), help: "Por debajo de esto la publicidad te hace perder." },
      { key: "max_cpa", label: "CPA máximo para no perder", value: fmt(r.max_cpa), help: "Costo por pedido confirmado que puedes pagar sin perder." },
      { key: "profit_per_100", label: "Utilidad por cada 100 pedidos", value: fmt(r.profit_per_100), help: `Con ${Number(r.delivered_per_100).toLocaleString("es")} entregas.`, tone: toneOf(r.profit_per_100) },
    ];
  }
  function toneOf(v) { return typeof v === "number" ? (v < 0 ? "bad" : v > 0 ? "good" : "") : ""; }

  /* Líneas de dinero esperado por pedido confirmado (signo: ingreso +, costos −). */
  const BREAKDOWN_LABELS = [
    ["revenue", "Ingreso esperado", 1], ["product_cost", "Costo del producto", -1], ["supplier_shipping", "Envío del proveedor", -1],
    ["packaging", "Empaque", -1], ["delivery_freight", "Flete de entrega", -1], ["return_freight", "Flete de devolución", -1],
    ["returned_product_loss", "Producto devuelto perdido", -1], ["cod_fee", "Comisión de recaudo", -1], ["ads", "Publicidad (CPA)", -1],
    ["profit", "Ganancia esperada", 0],
  ];
  function breakdownRows(r, fmt) {
    return BREAKDOWN_LABELS.filter(([k, , sign]) => sign === 0 || sign === 1 || r.breakdown[k] !== 0)
      .map(([k, label, sign]) => ({ key: k, label, value: fmt(sign < 0 ? -r.breakdown[k] : r.breakdown[k]), total: sign === 0 }));
  }

  /* Texto accesible que se anuncia al cambiar el resultado (una sola frase, no cada tecla) */
  function announce(r, fmt) {
    const v = verdict(r);
    if (!r || typeof r.profit_per_confirmed !== "number") return v.label;
    return `${v.label}. Ganancia por pedido confirmado ${fmt(r.profit_per_confirmed)}; margen ${formatPct(r.margin_pct)}.`;
  }

  function debounce(fn, ms, timers = { set: (f, ms2) => setTimeout(f, ms2), clear: (h) => clearTimeout(h) }) {
    let h = null;
    const d = (...a) => { timers.clear(h); h = timers.set(() => { h = null; fn(...a); }, ms); };
    d.cancel = () => { timers.clear(h); h = null; };
    return d;
  }

  const pure = { parseNumber, complementPct, buildPayload, formatPct, formatRoas, verdict, summaryRows, breakdownRows, announce,
    debounce, GROUPS, ALL_FIELDS, DEFAULT_FIELDS, MAX_ROAS, MIN_ROAS };
  if (typeof module !== "undefined" && module.exports) { module.exports = pure; return; }

  /* ============================================================ navegador */

  const view = () => $("#view-profit");
  let st = { currency: null, defaults: null, builtin: null, custom: [], product: null, seq: 0, rendered: false, mode: "cpa" };
  const decimalsOf = () => (st.currency === "COP" ? 0 : 2);
  const fmtMoney = (v) => {
    if (typeof v !== "number") return "—";
    const code = st.currency || "COP";
    const n = new Intl.NumberFormat(code === "COP" ? "es-CO" : "en-US", { minimumFractionDigits: decimalsOf(), maximumFractionDigits: decimalsOf() }).format(Math.abs(v));
    return `${v < 0 ? "−" : ""}${code === "COP" ? "$" : "US$"} ${n}`;
  };

  function fieldHtml(f) {
    const id = `pf-${f.key}`;
    return `<div class="pf-field">
      <label for="${id}">${esc(f.label)}${f.required ? ` <span class="pf-req" aria-hidden="true">*</span>` : ""}</label>
      <input id="${id}" name="${f.key}" type="text" inputmode="decimal" autocomplete="off" data-key="${f.key}" ${f.required ? "required" : ""}
        aria-describedby="${id}-hint ${id}-err">
      <small id="${id}-hint" class="pf-hint">${esc(f.help || "")}</small>
      <small id="${id}-err" class="pf-err" role="alert"></small>
    </div>`;
  }

  function render() {
    const sec = view();
    if (!sec) return;
    sec.innerHTML = `
      <div class="section-head">
        <div><h2>Rentabilidad contraentrega</h2>
          <p class="muted">Calcula cuánto ganas (o pierdes) por pedido cuando el cliente paga al recibir, contando las devoluciones.</p></div>
        <label class="pf-cur">Moneda
          <select id="pf-currency"><option value="COP">COP — peso colombiano</option><option value="USD">USD — dólar</option></select></label>
      </div>
      <p class="note pf-note">Son <b>estimaciones</b> hechas con supuestos que puedes editar. Los campos que dejes vacíos usan
        <b>tus supuestos</b> (se ven en gris); reemplázalos por los datos reales de tu transportadora y de tus pedidos.</p>
      <div id="pf-product" class="pf-product hidden"></div>
      <div class="pf-layout">
        <form id="pf-form" class="pf-form panel" novalidate autocomplete="off">
          ${GROUPS.map((g) => `<fieldset class="pf-group"><legend>${esc(g.title)}</legend><div class="pf-fields">${g.fields.map(fieldHtml).join("")}</div></fieldset>`).join("")}
          <fieldset class="pf-group"><legend>Publicidad</legend>
            <div class="pf-modes" role="radiogroup" aria-label="Cómo indicas la publicidad">
              <label class="toggle"><input type="radio" name="pf-mode" value="cpa" checked> Costo por pedido (CPA)</label>
              <label class="toggle"><input type="radio" name="pf-mode" value="roas"> ROAS objetivo</label>
            </div>
            <div class="pf-fields"><div class="pf-field">
              <label for="pf-ad" id="pf-ad-label">CPA: costo de publicidad por pedido confirmado</label>
              <input id="pf-ad" type="text" inputmode="decimal" autocomplete="off" data-key="ad" aria-describedby="pf-ad-hint pf-ad-err">
              <small id="pf-ad-hint" class="pf-hint">Vacío = sin publicidad en el cálculo.</small>
              <small id="pf-ad-err" class="pf-err" role="alert"></small>
            </div></div>
          </fieldset>
          <div class="pf-actions">
            <button type="button" class="ghost" id="pf-save">Guardar como mis supuestos</button>
            <button type="button" class="ghost" id="pf-reset-defaults">Volver a los de fábrica</button>
            <button type="button" class="link" id="pf-clear">Limpiar formulario</button>
          </div>
          <p id="pf-msg" class="muted pf-msg" role="status"></p>
        </form>
        <div class="pf-results" id="pf-results-wrap">
          <div id="pf-summary" class="pf-summary" aria-live="polite" aria-atomic="true"></div>
          <div id="pf-detail"></div>
        </div>
      </div>`;
    st.rendered = true;
    bind();
    applyDefaultsToPlaceholders();
    paint(null);
  }

  function bind() {
    const sec = view();
    const run = debounce(calculate, 350);
    st.run = run;
    $("#pf-form", sec).addEventListener("input", (e) => {
      const t = e.target;
      if (t.dataset.key === "return_rate_pct") $("#pf-delivery_rate_pct").value = complementPct(t.value, decimalsOf());
      if (t.dataset.key === "delivery_rate_pct") $("#pf-return_rate_pct").value = complementPct(t.value, decimalsOf());
      run();
    });
    $("#pf-form", sec).addEventListener("submit", (e) => { e.preventDefault(); run.cancel(); calculate(); });
    $$("input[name=pf-mode]", sec).forEach((r) => r.addEventListener("change", () => {
      st.mode = r.value;
      $("#pf-ad-label").textContent = st.mode === "roas" ? "ROAS objetivo: ventas por cada unidad gastada en publicidad (ej. 3)" : "CPA: costo de publicidad por pedido confirmado";
      $("#pf-ad").value = "";
      run();
    }));
    $("#pf-currency", sec).addEventListener("change", (e) => changeCurrency(e.target.value));
    $("#pf-save", sec).addEventListener("click", saveDefaults);
    $("#pf-reset-defaults", sec).addEventListener("click", resetDefaults);
    $("#pf-clear", sec).addEventListener("click", () => { clearForm(); setProduct(null); });
  }

  function values() {
    const v = {};
    for (const f of ALL_FIELDS) { const el = $(`#pf-${f.key}`); v[f.key] = el ? el.value : ""; }
    return v;
  }
  function clearForm() {
    $$("#pf-form input[type=text]").forEach((i) => { i.value = ""; });
    msg("");
    calculate();
  }
  function msg(text) { const m = $("#pf-msg"); if (m) m.textContent = text; }

  /* Los supuestos se muestran como texto de ejemplo (placeholder) de cada campo vacío */
  function applyDefaultsToPlaceholders() {
    const d = st.defaults || {};
    const fmtNum = (k, v) => (v === undefined ? "" : k.endsWith("_pct") ? String(v)
      : st.currency === "COP" ? Math.round(v).toLocaleString("es-CO") : String(Number(v.toFixed(2))));
    for (const f of ALL_FIELDS) {
      const el = $(`#pf-${f.key}`);
      if (!el) continue;
      if (f.key === "delivery_rate_pct") el.placeholder = d.return_rate_pct !== undefined ? String(100 - d.return_rate_pct) : "";
      else el.placeholder = d[f.key] !== undefined ? fmtNum(f.key, d[f.key]) : (f.required ? "obligatorio" : "0");
    }
    const cur = $("#pf-currency");
    if (cur) cur.value = st.currency;
  }

  async function loadDefaults(code) {
    try {
      const d = await api(`/api/profit/defaults?currency=${encodeURIComponent(code)}`);
      st.defaults = d.defaults; st.builtin = d.builtin; st.custom = d.custom_keys;
    } catch (e) {
      st.defaults = null;
      msg(`No se pudieron cargar tus supuestos: ${e.message}`);
    }
    applyDefaultsToPlaceholders();
  }

  async function changeCurrency(code) {
    const had = ALL_FIELDS.some((f) => !f.derived && $(`#pf-${f.key}`) && $(`#pf-${f.key}`).value && f.type === "money") || $("#pf-ad").value;
    st.currency = code;
    if (had) {
      $$("#pf-form input[type=text]").forEach((i) => { i.value = ""; });
      msg("Cambiaste de moneda: vuelve a escribir los montos en la nueva moneda (no se convierten solos).");
    }
    await loadDefaults(code);
    calculate();
  }

  /* ---------------- cálculo en vivo ---------------- */
  async function calculate() {
    if (!st.rendered) return;
    const { payload, errors, missing } = buildPayload(values(), { currency: st.currency, mode: st.mode, ad: $("#pf-ad").value });
    showErrors(errors);
    if (Object.keys(errors).length || missing.length) { paint(null); return; }
    const seq = ++st.seq;                              // descarta respuestas que llegan fuera de orden
    try {
      const r = await api("/api/profit/calc", { method: "POST", body: payload });
      if (seq === st.seq) paint(r);
    } catch (e) {
      if (seq !== st.seq || e.status === 401) return;
      const key = e.code && e.code.startsWith("invalid_") ? e.code.slice(8) : null;
      if (key) showErrors({ [key]: e.message });
      paint(null, e.message);
    }
  }

  function showErrors(errors) {
    $$("#pf-form .pf-err").forEach((el) => { el.textContent = ""; });
    $$("#pf-form input").forEach((i) => i.removeAttribute("aria-invalid"));
    for (const [k, text] of Object.entries(errors)) {
      const key = k === "cpa" || k === "target_roas" ? "ad" : k;
      const id = key === "ad" ? "pf-ad" : `pf-${key}`;
      const el = $(`#${id}-err`), inp = $(`#${id}`);
      if (el) el.textContent = text;
      if (inp) inp.setAttribute("aria-invalid", "true");
    }
  }

  function paint(r, problem) {
    const sum = $("#pf-summary"), det = $("#pf-detail");
    if (!sum) return;
    if (!r) {
      sum.className = "pf-summary muted";
      sum.textContent = problem || "Escribe el precio de venta y el costo del producto para ver el resultado.";
      det.innerHTML = "";
      return;
    }
    const v = verdict(r);
    sum.className = `pf-summary pf-${v.tone}`;
    sum.innerHTML = `<span class="pf-verdict"><span aria-hidden="true">${v.icon}</span> ${esc(v.label)}</span>
      <span class="sr-only">${esc(announce(r, fmtMoney))}</span>`;
    const rows = summaryRows(r, fmtMoney), bd = breakdownRows(r, fmtMoney);
    det.innerHTML = `
      <dl class="pf-metrics">${rows.map((x) => `<div class="pf-metric"><dt>${esc(x.label)}</dt>
        <dd class="${x.tone || ""}">${esc(x.value)}</dd><dd class="pf-help">${esc(x.help)}</dd></div>`).join("")}</dl>
      ${r.warnings.length ? `<ul class="pf-warnings" aria-label="Advertencias">${r.warnings.map((w) => `<li class="pf-w-${esc(w.code)}">${esc(w.message)}</li>`).join("")}</ul>` : ""}
      <h3 class="pf-sub">Dinero esperado por cada pedido confirmado</h3>
      <ul class="pf-breakdown">${bd.map((x) => `<li class="${x.total ? "pf-total" : ""}"><span>${esc(x.label)}</span><span>${esc(x.value)}</span></li>`).join("")}</ul>
      <p class="muted pf-foot">Estimación: un pedido confirmado se entrega con probabilidad ${esc(formatPct(r.inputs.delivery_rate_pct))} y vuelve con
        ${esc(formatPct(r.inputs.return_rate_pct))}. Las líneas se redondean por separado, así que pueden diferir de la suma en una unidad.
        Fórmulas en <code>docs/profit-calculator.md</code>.</p>`;
  }

  /* ---------------- supuestos del usuario ---------------- */
  async function saveDefaults() {
    const { payload } = buildPayload(values(), { currency: st.currency });
    const body = {};
    for (const k of DEFAULT_FIELDS) if (k in payload) body[k] = payload[k];
    if (!Object.keys(body).length) { msg("Escribe al menos un valor de logística o contraentrega para guardarlo como supuesto."); return; }
    try {
      const d = await api("/api/profit/defaults", { method: "PUT", body: { currency: st.currency, defaults: body } });
      st.defaults = d.defaults; st.custom = d.custom_keys;
      $$("#pf-form input[type=text]").forEach((i) => { if (DEFAULT_FIELDS.includes(i.dataset.key) || i.dataset.key === "delivery_rate_pct") i.value = ""; });
      applyDefaultsToPlaceholders();
      msg(`Guardado: estos valores son ahora tus supuestos en ${st.currency}.`);
      calculate();
    } catch (e) { msg(e.message); }
  }

  async function resetDefaults() {
    const body = Object.fromEntries(DEFAULT_FIELDS.map((k) => [k, null]));
    try {
      const d = await api("/api/profit/defaults", { method: "PUT", body: { currency: st.currency, defaults: body } });
      st.defaults = d.defaults; st.custom = d.custom_keys;
      applyDefaultsToPlaceholders();
      msg("Tus supuestos volvieron a los de fábrica.");
      calculate();
    } catch (e) { msg(e.message); }
  }

  /* ---------------- precarga desde un producto ---------------- */
  function setProduct(p) {
    st.product = p;
    const box = $("#pf-product");
    if (!box) return;
    if (!p) { box.classList.add("hidden"); box.textContent = ""; return; }
    box.classList.remove("hidden");
    box.innerHTML = `<span>Producto precargado: <b>${esc(p.title)}</b> · precio sugerido, costo y envío del proveedor.</span>
      <button type="button" class="link" id="pf-unload">Quitar</button>`;
    $("#pf-unload").addEventListener("click", () => { clearForm(); setProduct(null); });
  }

  async function loadProduct(id) {
    if (!st.rendered) await init();
    try {
      const d = await api(`/api/products/${id}/profit`);
      st.currency = d.inputs.currency;
      await loadDefaults(st.currency);
      $$("#pf-form input[type=text]").forEach((i) => { i.value = ""; });
      const dec = d.inputs.currency === "COP" ? 0 : 2;
      for (const k of ["price", "product_cost", "supplier_shipping"]) {
        const el = $(`#pf-${k}`);
        if (el && d.inputs[k] !== undefined) el.value = String(Number(d.inputs[k].toFixed(dec)));
      }
      setProduct(d.product);
      msg("");
      calculate();
      const price = $("#pf-delivery_freight");
      if (price) price.focus({ preventScroll: true });
    } catch (e) { toast(e.message); }
  }

  async function init() {
    if (st.rendered) return;
    st.currency = (window.Currency && Currency.code && Currency.code()) || "COP";
    if (st.currency !== "COP" && st.currency !== "USD") st.currency = "COP";
    render();
    await loadDefaults(st.currency);
  }

  DF.viewLoaders.profit = () => { init(); };

  /* Detalle de producto: botón «Calcular contraentrega» */
  DF.detailSections.push((p, container) => {
    const wrap = document.createElement("div");
    wrap.className = "pf-detail-cta";
    wrap.innerHTML = `<button type="button" class="ghost" id="pfFromDetail">Calcular contraentrega</button>
      <small class="muted">Estima ganancia con devoluciones, fletes y recaudo.</small>`;
    container.appendChild(wrap);
    $("#pfFromDetail", wrap).addEventListener("click", () => {
      const dlg = $("#detail");
      if (dlg && dlg.open) dlg.close();
      show("profit");
      loadProduct(p.id);
    });
  });
})(typeof window !== "undefined" ? window : globalThis);
