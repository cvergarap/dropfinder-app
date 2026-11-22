/* Módulo history: historial de pedidos/costo en el detalle del producto y pestaña "Tendencias"
   (rankings, estacionalidad y comparación de proveedores). Se registra en DF (ver app.js).
   Gráficos en SVG escrito a mano (sin librerías); los colores salen de variables CSS (css/history.css). */
(() => {
  const SVGNS = "http://www.w3.org/2000/svg";
  const MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre",
    "octubre", "noviembre", "diciembre"];
  const compact = new Intl.NumberFormat("es", { notation: "compact", maximumFractionDigits: 1 });
  const parseDay = (s) => new Date(`${s}T12:00:00`);
  const dayLabel = (s, opts = { day: "numeric", month: "short" }) => parseDay(s).toLocaleDateString("es", opts);
  const pct = (v) => (v === null || v === undefined) ? "" : `${v > 0 ? "+" : v < 0 ? "−" : ""}${fmt(Math.abs(v))} %`;

  /* Variación con flecha (no solo color) */
  function delta(change, prev, cur) {
    if (change === null || change === undefined) {
      return cur > 0 && !prev ? `<span class="hx-delta new">● Nuevo</span>` : `<span class="hx-delta">—</span>`;
    }
    const cls = change > 0 ? "up" : change < 0 ? "down" : "";
    const arrow = change > 0 ? "▲" : change < 0 ? "▼" : "■";
    return `<span class="hx-delta ${cls}"><span aria-hidden="true">${arrow}</span> ${pct(change)}</span>`;
  }

  /* Escala "limpia" para el eje Y: máximo redondeado y 3–4 marcas */
  function niceTicks(max, count = 4) {
    if (!(max > 0)) return { max: 1, ticks: [0, 1] };
    const raw = max / count;
    const mag = 10 ** Math.floor(Math.log10(raw));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw);
    const top = Math.ceil(max / step) * step;
    const ticks = [];
    for (let v = 0; v <= top + 1e-9; v += step) ticks.push(Math.round(v * 1e6) / 1e6);
    return { max: top, ticks };
  }

  function el(tag, attrs = {}) {
    const n = document.createElementNS(SVGNS, tag);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
    return n;
  }

  /* Columna con extremo de datos redondeado (4 px) y base recta */
  function colPath(x, y, w, y0) {
    const r = Math.min(4, w / 2, Math.max(0, y0 - y));
    if (r < 1) return `M${x},${y0}V${y}H${x + w}V${y0}Z`;
    return `M${x},${y0}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y0}Z`;
  }

  /* ================= Detalle del producto: sección "Historial" ================= */
  /* Caché del historial: clave "id:updated_at:días" (si el producto cambia, la clave cambia), vence en
     60 s (la foto del día y la demo avanzan en segundo plano) y se vacía tras cada búsqueda (DF.onResults),
     que es lo que la app vuelve a pedir después de una carga de CSV o del monitor. */
  const histCache = new Map();          // clave -> { at, promise }
  const HIST_TTL_MS = 60_000;
  let histDays = 30;

  async function getHistory(p, days) {
    const key = `${p.id}:${p.updated_at || ""}:${days}`;
    const hit = histCache.get(key);
    if (hit && Date.now() - hit.at < HIST_TTL_MS) return hit.promise;
    const promise = api(`/api/products/${p.id}/history?days=${days}`).then((h) => {
      if (h.generating) histCache.delete(key);          // la demo se está generando: no guardar
      return h;
    }, (e) => { histCache.delete(key); throw e; });
    histCache.set(key, { at: Date.now(), promise });
    return promise;
  }

  function detailSection(p, container) {
    const sec = document.createElement("section");
    sec.className = "hx-detail";
    sec.innerHTML = `
      <div class="hx-head">
        <h3>Historial</h3>
        <div class="hx-seg" role="group" aria-label="Período del historial">
          ${[30, 90, 365].map((d) => `<button type="button" data-days="${d}" aria-pressed="${d === histDays}">${d} días</button>`).join("")}
        </div>
      </div>
      <div class="hx-body"><p class="muted hx-loading">Cargando historial…</p></div>`;
    container.appendChild(sec);
    const body = $(".hx-body", sec);
    const load = async (days) => {
      histDays = days;
      $$(".hx-seg button", sec).forEach((b) => b.setAttribute("aria-pressed", String(Number(b.dataset.days) === days)));
      body.classList.add("hx-stale");
      try {
        const h = await getHistory(p, days);
        if (!sec.isConnected || histDays !== days) return;
        renderHistory(body, h);
        if (h.generating && !h.points.length) setTimeout(() => { if (sec.isConnected && histDays === days) load(days); }, 2500);
      } catch (e) {
        body.innerHTML = `<p class="muted">No se pudo cargar el historial: ${esc(e.message)}</p>`;
      } finally {
        body.classList.remove("hx-stale");
      }
    };
    $(".hx-seg", sec).addEventListener("click", (e) => {
      const b = e.target.closest("button[data-days]");
      if (b) load(Number(b.dataset.days));
    });
    load(histDays);
  }

  function renderHistory(body, h) {
    const pts = h.points || [];
    const s = h.summary;
    const unit = h.resolution === "week" ? "semana" : "día";
    if (!pts.length) {
      body.innerHTML = h.generating
        ? `<p class="muted hx-generating" role="status"><span class="hx-spin" aria-hidden="true"></span> Generando el historial de la demostración… aparece en unos segundos.</p>`
        : `<p class="muted">Aún no hay historial para este producto. DropFinder guarda una foto por día
        cada vez que se actualiza el catálogo o corre el monitor.</p>`;
      return;
    }
    const stale = h.last_observed && h.last_observed < h.until;
    if (pts.every((x) => x.orders === null)) {
      body.innerHTML = `<p class="muted">Sin datos en este período: la última foto de este producto es del
        ${esc(dayLabel(h.last_observed || h.until, { day: "numeric", month: "long", year: "numeric" }))}. Prueba con un período más largo.</p>`;
      return;
    }
    const season = h.seasonality
      ? `<span class="hx-badge" title="Su promedio de ese mes es ${fmt(h.seasonality.strength)} veces el promedio del año, en los dos últimos años">📅 ${esc(h.seasonality.label)}</span>` : "";
    const peak = s.peak_date ? (h.resolution === "week" ? `al ${dayLabel(s.peak_date)}` : dayLabel(s.peak_date)) : "—";
    body.innerHTML = `
      <div class="hx-stats">
        <div class="hx-stat"><span class="hx-label">Pedidos en ${h.days} días</span><b>${fmt(s.orders_period)}</b>
          <span class="hx-sub">${delta(s.change_pct, s.orders_prev_period, s.orders_period)} <span class="muted">vs ${h.days} días anteriores (${fmt(s.orders_prev_period)})</span></span></div>
        <div class="hx-stat"><span class="hx-label">Mejor ${unit}</span><b class="hx-peak">${esc(peak)}</b></div>
        ${season ? `<div class="hx-stat hx-season-stat">${season}</div>` : ""}
      </div>
      ${stale ? `<p class="note hx-stale-note">Sin datos desde el ${esc(dayLabel(h.last_observed, { day: "numeric", month: "long" }))}:
        este producto no se actualizó después. Las cifras se miden hasta esa fecha.</p>` : ""}
      <div class="hx-charts">
        <p class="hx-chart-title">Pedidos por ${unit}</p>
        <div class="hx-chart hx-orders"></div>
        <p class="hx-chart-title">Costo total (producto + envío)</p>
        <div class="hx-chart hx-cost"></div>
        <div class="hx-tip" role="status" hidden></div>
      </div>
      <details class="hx-table"><summary>Ver datos en tabla</summary>
        <div class="table-wrap"><table class="table"><thead><tr><th>${h.resolution === "week" ? "Semana al" : "Fecha"}</th>
          <th class="num">Pedidos</th><th class="num">Costo total</th><th class="num">Stock</th></tr></thead>
          <tbody>${pts.slice().reverse().map((x) => `<tr><td>${esc(dayLabel(x.date, { day: "numeric", month: "short", year: "numeric" }))}</td>
            <td class="num">${x.orders === null ? "—" : fmt(x.orders)}</td><td class="num">${x.total_cost === null ? "—" : money(x.total_cost)}</td>
            <td class="num">${x.stock === null ? "—" : fmt(x.stock)}</td></tr>`).join("")}</tbody></table></div>
      </details>`;
    drawHistoryCharts(body, h);
  }

  /* Dos gráficos con el mismo eje X (sin doble eje Y): columnas de pedidos y línea de costo. La guía vertical
     y el tooltip se comparten. */
  function drawHistoryCharts(body, h) {
    const wrap = $(".hx-charts", body);
    const pts = h.points;
    const n = pts.length;
    const tip = $(".hx-tip", wrap);
    const unit = h.resolution === "week" ? "semana" : "día";
    let active = -1;
    let geo = null;

    function draw() {
      const W = Math.max(260, Math.round(wrap.clientWidth || 520));
      // Margen izquierdo según la etiqueta más larga (los montos en COP son anchos); igual en ambos gráficos
      const costVals = pts.map((x) => x.total_cost).filter((v) => v !== null);
      const labels = [money(Math.min(...costVals)), money(Math.max(...costVals)),
        fmt(niceTicks(Math.max(0, ...pts.map((x) => x.orders || 0))).max)];
      const L = Math.round(Math.max(36, 6.6 * Math.max(...labels.map((t) => t.length)) + 10)), R = 10;
      const plotW = W - L - R;
      const slot = plotW / n;
      const xAt = (i) => L + slot * i + slot / 2;
      geo = { L, slot, n };

      /* --- pedidos --- */
      const H = 150, T = 8, B = 22, y0 = H - B;
      const known = pts.filter((x) => x.orders !== null);
      const maxO = Math.max(0, ...known.map((x) => x.orders));
      const { max, ticks } = niceTicks(maxO);
      const yAt = (v) => y0 - (v / max) * (y0 - T);
      const total = known.reduce((a, x) => a + x.orders, 0);
      const best = known.reduce((a, x) => (x.orders > a.orders ? x : a), known[0] || { orders: 0, date: pts[0].date });
      const gapFrom = pts.findIndex((x, i) => x.orders === null && i > 0 && pts[i - 1].orders !== null);
      const svg = el("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: "img", tabindex: "0",
        "aria-label": `Pedidos por ${unit}, últimos ${h.days} días: ${fmt(total)} en total` +
          (h.summary.change_pct !== null ? `, ${pct(h.summary.change_pct)} frente al período anterior` : "") +
          (best.orders ? `. Máximo: ${fmt(best.orders)} (${dayLabel(best.date)}).` : ".") +
          (gapFrom > 0 ? ` Sin datos desde el ${dayLabel(pts[gapFrom - 1].date)}.` : "") +
          " Use las flechas para recorrer los valores." });
      for (const t of ticks) {
        const y = yAt(t);
        svg.appendChild(el("line", { class: "hx-grid", x1: L, x2: W - R, y1: y, y2: y }));
        const tx = el("text", { class: "hx-axis", x: L - 6, y: y + 4, "text-anchor": "end" });
        tx.textContent = t >= 10000 ? compact.format(t) : fmt(t);
        svg.appendChild(tx);
      }
      const bw = Math.max(1, Math.min(24, slot - 2));
      const bars = el("g", { class: "hx-bars" });
      pts.forEach((x, i) => {
        if (!(x.orders > 0)) return;
        const p = el("path", { d: colPath(xAt(i) - bw / 2, yAt(x.orders), bw, y0), "data-i": i });
        bars.appendChild(p);
      });
      svg.appendChild(bars);
      // Tramo sin datos (después de la última foto): banda tenue con rótulo, no barras en cero
      if (gapFrom > 0) {
        const gx = L + slot * gapFrom;
        svg.appendChild(el("rect", { class: "hx-nodata", x: gx, y: T, width: W - R - gx, height: y0 - T }));
        if (W - R - gx > 56) {
          const tx = el("text", { class: "hx-axis", x: (gx + W - R) / 2, y: T + 14, "text-anchor": "middle" });
          tx.textContent = "Sin datos";
          svg.appendChild(tx);
        }
      }
      svg.appendChild(el("line", { class: "hx-base", x1: L, x2: W - R, y1: y0, y2: y0 }));
      // Etiquetas del eje X: inicio, medio y fin
      [0, Math.floor((n - 1) / 2), n - 1].forEach((i, k) => {
        const tx = el("text", { class: "hx-axis", x: xAt(i), y: H - 6, "text-anchor": k === 0 ? "start" : k === 2 ? "end" : "middle" });
        if (k === 0) tx.setAttribute("x", L);
        if (k === 2) tx.setAttribute("x", W - R);
        tx.textContent = dayLabel(pts[i].date);
        svg.appendChild(tx);
      });
      svg.appendChild(el("line", { class: "hx-cross", x1: 0, x2: 0, y1: T, y2: y0, visibility: "hidden" }));

      /* --- costo total --- */
      const CH = 78, CT = 10, CB = 8, cy0 = CH - CB;
      const costs = pts.map((x) => x.total_cost).filter((v) => v !== null);
      const cmin = Math.min(...costs), cmax = Math.max(...costs);
      const pad = (cmax - cmin) * 0.15 || cmax * 0.05 || 1;
      const lo = Math.max(0, cmin - pad), hi = cmax + pad;
      const cy = (v) => cy0 - ((v - lo) / (hi - lo)) * (cy0 - CT);
      const first = pts.find((x) => x.total_cost !== null);
      const lastIdx = pts.map((x) => x.total_cost !== null).lastIndexOf(true);
      const last = pts[lastIdx];
      const csvg = el("svg", { width: W, height: CH, viewBox: `0 0 ${W} ${CH}`, role: "img",
        "aria-label": `Costo total: de ${money(first.total_cost)} a ${money(last.total_cost)}` +
          (cmin !== cmax ? ` (mínimo ${money(cmin)}, máximo ${money(cmax)}).` : ", sin cambios.") });
      [cmin, cmax].filter((v, i, a) => a.indexOf(v) === i).forEach((v) => {
        csvg.appendChild(el("line", { class: "hx-grid", x1: L, x2: W - R, y1: cy(v), y2: cy(v) }));
        const tx = el("text", { class: "hx-axis", x: L - 6, y: cy(v) + 4, "text-anchor": "end" });
        tx.textContent = money(v);
        csvg.appendChild(tx);
      });
      let d = "";
      pts.forEach((x, i) => {
        if (x.total_cost === null) { d += " "; return; }
        // escalones: el costo se mantiene hasta que cambia
        const X = xAt(i), Y = cy(x.total_cost);
        d += d && !d.endsWith(" ") ? `H${X}V${Y}` : `M${X},${Y}`;
      });
      csvg.appendChild(el("path", { class: "hx-line", d }));
      csvg.appendChild(el("circle", { class: "hx-dot", cx: xAt(lastIdx), cy: cy(last.total_cost), r: 4 }));
      csvg.appendChild(el("line", { class: "hx-cross", x1: 0, x2: 0, y1: 2, y2: CH - 2, visibility: "hidden" }));

      $(".hx-orders", wrap).replaceChildren(svg);
      $(".hx-cost", wrap).replaceChildren(csvg);
      if (active >= 0) show(active);
    }

    function show(i) {
      if (!geo || i < 0 || i >= n) return hide();
      active = i;
      const x = pts[i];
      const X = geo.L + geo.slot * i + geo.slot / 2;
      $$(".hx-cross", wrap).forEach((c) => { c.setAttribute("x1", X); c.setAttribute("x2", X); c.setAttribute("visibility", "visible"); });
      $$(".hx-bars path", wrap).forEach((b) => b.classList.toggle("on", Number(b.dataset.i) === i));
      tip.replaceChildren();
      const head = document.createElement("div");
      head.className = "hx-tip-head";
      head.textContent = h.resolution === "week"
        ? `Semana al ${dayLabel(x.date, { day: "numeric", month: "short", year: "numeric" })}`
        : dayLabel(x.date, { weekday: "short", day: "numeric", month: "short", year: "numeric" });
      tip.appendChild(head);
      const row = (key, value, label) => {
        const r = document.createElement("div");
        r.className = "hx-tip-row";
        const k = document.createElement("i");
        k.className = `hx-key ${key}`;
        const b = document.createElement("b");
        b.textContent = value;
        const s = document.createElement("span");
        s.textContent = label;
        r.append(k, b, s);
        tip.appendChild(r);
      };
      if (x.orders === null) row("none", "Sin datos", "");
      else row("orders", fmt(x.orders), x.orders === 1 ? "pedido" : "pedidos");
      if (x.total_cost !== null) row("cost", money(x.total_cost), "costo total");
      if (x.stock !== null) row("stock", x.stock > 0 ? fmt(x.stock) : "Sin stock", x.stock > 0 ? "en stock" : "");
      tip.hidden = false;
      const ww = wrap.clientWidth;
      const tw = tip.offsetWidth;
      tip.style.left = `${Math.min(Math.max(0, X + 12 + tw > ww ? X - tw - 12 : X + 12), Math.max(0, ww - tw))}px`;
    }

    function hide() {
      active = -1;
      tip.hidden = true;
      $$(".hx-cross", wrap).forEach((c) => c.setAttribute("visibility", "hidden"));
      $$(".hx-bars path.on", wrap).forEach((b) => b.classList.remove("on"));
    }

    const indexAt = (ev) => {
      const svg = ev.currentTarget.querySelector("svg");
      if (!svg || !geo) return -1;
      const r = svg.getBoundingClientRect();
      return Math.floor((ev.clientX - r.left - geo.L) / geo.slot);
    };
    $$(".hx-chart", wrap).forEach((c) => {
      c.addEventListener("pointermove", (ev) => show(Math.min(n - 1, Math.max(0, indexAt(ev)))));
      c.addEventListener("pointerleave", hide);
    });
    wrap.addEventListener("keydown", (ev) => {
      if (ev.target.tagName !== "svg") return;
      if (ev.key === "ArrowRight" || ev.key === "ArrowLeft") {
        ev.preventDefault();
        show(active < 0 ? n - 1 : Math.min(n - 1, Math.max(0, active + (ev.key === "ArrowRight" ? 1 : -1))));
      } else if (ev.key === "Home") { ev.preventDefault(); show(0); }
      else if (ev.key === "End") { ev.preventDefault(); show(n - 1); }
      else if (ev.key === "Escape" && active >= 0) { ev.stopPropagation(); ev.preventDefault(); hide(); }
    });
    wrap.addEventListener("focusin", (ev) => { if (ev.target.tagName === "svg" && active < 0) show(n - 1); });
    wrap.addEventListener("focusout", hide);

    draw();
    if (!wrap.clientWidth) requestAnimationFrame(draw);     // el diálogo aún no estaba visible
    let lastW = wrap.clientWidth;
    const ro = new ResizeObserver(() => {
      if (!wrap.isConnected) { ro.disconnect(); return; }
      if (Math.abs(wrap.clientWidth - lastW) > 2) { lastW = wrap.clientWidth; draw(); }
    });
    ro.observe(wrap);
  }

  /* Minigráfico de 14 días: línea en tono atenuado y el último día como punto de acento */
  function sparkline(vals, label) {
    const W = 84, H = 26, P = 3;
    const max = Math.max(1, ...vals);
    const step = (W - 2 * P) / Math.max(1, vals.length - 1);
    const pts = vals.map((v, i) => [P + i * step, H - P - (v / max) * (H - 2 * P)]);
    const d = pts.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`).join("");
    const [lx, ly] = pts[pts.length - 1];
    return `<svg class="hx-spark" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(label)}">
      <path d="${d}"/><circle cx="${lx.toFixed(1)}" cy="${ly.toFixed(1)}" r="3"/></svg>`;
  }

  /* ================= Pestaña Tendencias ================= */
  const TYPES = [
    ["top", "Más vendidos", "Los que más pedidos sumaron en el período."],
    ["rising", "En ascenso", "Mayor crecimiento frente al período anterior de igual duración (con un mínimo de pedidos)."],
    ["new", "Nuevos en tendencia", "Casi sin ventas en los 30 días previos al período y con fuerte demanda ahora."],
    ["falling", "En caída", "Mayor caída frente al período anterior: mejor evitarlos o liquidarlos."],
  ];
  const state = { type: "top", days: 30, category: "", month: (new Date().getMonth() + 1) % 12 + 1,
    rank: null, season: null, sup: null, sort: { key: "reliability", dir: -1 }, built: false, seq: 0 };

  function buildTrends() {
    const v = $("#view-trends");
    v.innerHTML = `
      <div class="section-head">
        <div><h2>Tendencias</h2><p class="muted" id="hxUntil">Lo que más se vende, lo que sube y lo que cae, a partir del historial diario.</p></div>
      </div>
      <div class="hx-filters">
        <div class="hx-tabs" role="tablist" aria-label="Tipo de ranking">
          ${TYPES.map(([k, l]) => `<button type="button" role="tab" id="hx-tab-${k}" aria-controls="hxList" data-type="${k}"
            aria-selected="${k === state.type}" tabindex="${k === state.type ? 0 : -1}">${l}</button>`).join("")}
        </div>
        <div class="hx-seg" role="group" aria-label="Período">
          ${[7, 30, 90].map((d) => `<button type="button" data-days="${d}" aria-pressed="${d === state.days}">${d} días</button>`).join("")}
        </div>
        <label class="sr-only" for="hxCat">Categoría</label>
        <select id="hxCat"><option value="">Todas las categorías</option></select>
      </div>
      <p class="muted hx-def" id="hxDef"></p>
      <p id="hxGenerating" class="note hx-generating" role="status" hidden><span class="hx-spin" aria-hidden="true"></span>
        Generando el historial de la demostración… los rankings se completan solos en unos segundos.</p>
      <div id="hxList" class="hx-list" role="tabpanel" aria-labelledby="hx-tab-${state.type}" tabindex="0" aria-live="polite"></div>

      <section class="panel hx-season-panel">
        <div class="hx-season-head">
          <h3 id="hxSeasonTitle">Se viene</h3>
          <label class="sr-only" for="hxMonth">Mes</label>
          <select id="hxMonth">${MESES.map((m, i) => `<option value="${i + 1}">${m[0].toUpperCase() + m.slice(1)}</option>`).join("")}</select>
        </div>
        <p class="muted small">Productos cuyo historial muestra un pico ese mes en los dos últimos años (promedio del mes ≥ 1,8 × el del año).</p>
        <div id="hxSeason" class="hx-season-list"></div>
      </section>

      <section class="hx-sup">
        <h3>Comparar proveedores</h3>
        <p class="muted small">Promedios del catálogo de cada proveedor. Toca un encabezado para ordenar; el mejor valor de cada columna va resaltado.</p>
        <div class="table-wrap"><table class="table hx-sup-table"><thead></thead><tbody></tbody></table></div>
        <p class="muted small" id="hxFormula"></p>
      </section>`;
    $(".hx-tabs", v).addEventListener("click", (e) => {
      const b = e.target.closest("[data-type]");
      if (!b) return;
      state.type = b.dataset.type;
      loadRanking();
    });
    $(".hx-tabs", v).addEventListener("keydown", (e) => {
      if (!["ArrowRight", "ArrowLeft", "Home", "End"].includes(e.key)) return;
      e.preventDefault();
      const i = TYPES.findIndex(([k]) => k === state.type);
      const j = e.key === "Home" ? 0 : e.key === "End" ? TYPES.length - 1
        : (i + (e.key === "ArrowRight" ? 1 : TYPES.length - 1)) % TYPES.length;
      state.type = TYPES[j][0];
      loadRanking();
      $(`.hx-tabs [data-type="${state.type}"]`).focus();
    });
    $(".hx-filters .hx-seg", v).addEventListener("click", (e) => {
      const b = e.target.closest("[data-days]");
      if (!b) return;
      state.days = Number(b.dataset.days);
      loadRanking();
    });
    $("#hxCat").addEventListener("change", (e) => { state.category = e.target.value; loadRanking(); });
    $("#hxMonth").value = String(state.month);
    $("#hxMonth").addEventListener("change", (e) => { state.month = Number(e.target.value); loadSeason(); });
    $("#hxList").addEventListener("click", openRow);
    $("#hxSeason").addEventListener("click", openRow);
    $(".hx-sup-table thead", v).addEventListener("click", (e) => {
      const b = e.target.closest("button[data-sort]");
      if (!b) return;
      const key = b.dataset.sort;
      const col = SUP_COLS.find((c) => c.key === key);
      state.sort = state.sort.key === key ? { key, dir: -state.sort.dir } : { key, dir: col.better === "min" ? 1 : -1 };
      renderSuppliers();
    });
    state.built = true;
  }

  function openRow(e) {
    const r = e.target.closest("[data-id]");
    if (r) openDetail(Number(r.dataset.id));
  }

  async function loadCategories() {
    try {
      const f = await api("/api/facets");
      const sel = $("#hxCat");
      sel.innerHTML = `<option value="">Todas las categorías</option>` +
        f.category.map((c) => `<option value="${esc(c.value)}">${esc(c.value)}</option>`).join("");
      sel.value = state.category;
    } catch { /* el filtro queda solo con "Todas" */ }
  }

  async function loadRanking() {
    $$(".hx-tabs [data-type]").forEach((b) => {
      const on = b.dataset.type === state.type;
      b.setAttribute("aria-selected", String(on));
      b.tabIndex = on ? 0 : -1;
    });
    $("#hxList").setAttribute("aria-labelledby", `hx-tab-${state.type}`);
    $$(".hx-filters .hx-seg button").forEach((b) => b.setAttribute("aria-pressed", String(Number(b.dataset.days) === state.days)));
    $("#hxDef").textContent = TYPES.find(([k]) => k === state.type)[2];
    const seq = ++state.seq;
    const list = $("#hxList");
    list.classList.add("hx-stale");
    const qs = new URLSearchParams({ type: state.type, days: state.days, limit: 20 });
    if (state.category) qs.set("category", state.category);
    try {
      const data = await api(`/api/rankings?${qs}`);
      if (seq !== state.seq) return;
      state.rank = data;
      renderRanking();
    } catch (e) {
      if (seq === state.seq) list.innerHTML = `<p class="empty">No se pudo cargar el ranking: ${esc(e.message)}</p>`;
    } finally {
      if (seq === state.seq) list.classList.remove("hx-stale");
    }
  }

  let pollTimer = null;
  function pollWhileGenerating(flag) {
    $("#hxGenerating").hidden = !flag;
    clearTimeout(pollTimer);
    if (flag) {
      pollTimer = setTimeout(() => {
        if ($("#view-trends").classList.contains("active")) loadTrends();
      }, 3000);
    }
  }

  function renderRanking() {
    const data = state.rank;
    if (!data) return;
    pollWhileGenerating(Boolean(data.generating));
    if (data.until) {
      $("#hxUntil").textContent = `Datos hasta el ${dayLabel(data.until, { day: "numeric", month: "long", year: "numeric" })}. ` +
        "Lo que más se vende, lo que sube y lo que cae, a partir del historial diario.";
    }
    const list = $("#hxList");
    if (!data.items.length) {
      list.innerHTML = `<p class="empty">No hay productos en este ranking para el período elegido.
        ${state.type === "new" && state.days === 7 ? "Prueba con 30 o 90 días." : "Prueba con otro período o categoría."}</p>`;
      return;
    }
    list.innerHTML = data.items.map((p, i) => `
      <article class="hx-row" data-id="${p.id}" tabindex="0" role="button" aria-label="${esc(p.title)}: ${fmt(p.period_orders)} pedidos en ${data.days} días">
        <span class="hx-rank">${i + 1}</span>
        ${imgTag(p, "hx-thumb")}
        <div class="hx-main">
          <div class="hx-title">${esc(p.title)}${p.source === "mock" ? ` <span class="chip demo-chip">DEMO</span>` : ""}</div>
          <div class="meta"><span class="chip">${esc(p.supplier)}</span><span class="chip">${FLAGS[p.warehouse] || ""} ${esc(p.warehouse)}</span>
            <span>${esc(p.category)}</span><span>Costo ${money(p.total_cost)}</span><span>Venta ${money(p.suggested_price, { kind: "price" })}</span></div>
        </div>
        <div class="hx-num"><b>${fmt(p.period_orders)}</b><span>pedidos · ${data.days} d</span></div>
        <div class="hx-chg">${delta(p.change_pct, p.prev_period_orders, p.period_orders)}<span class="muted">antes ${fmt(p.prev_period_orders)}</span></div>
        ${sparkline(p.sparkline, `Pedidos por día, últimos 14 días: ${p.sparkline.join(", ")}`)}
      </article>`).join("");
  }

  async function loadSeason() {
    const box = $("#hxSeason");
    box.classList.add("hx-stale");
    const m = state.month;
    $("#hxSeasonTitle").textContent = `Se viene: sube cada año en ${MESES[m - 1]}`;
    try {
      const qs = new URLSearchParams({ month: m, limit: 12 });
      const data = await api(`/api/seasonal?${qs}`);
      if (m !== state.month) return;
      state.season = data;
      renderSeason();
    } catch (e) {
      box.innerHTML = `<p class="muted">No se pudo cargar: ${esc(e.message)}</p>`;
    } finally {
      box.classList.remove("hx-stale");
    }
  }

  function renderSeason() {
    const data = state.season;
    if (!data) return;
    const box = $("#hxSeason");
    if (!data.items.length) {
      box.innerHTML = `<p class="muted">Ningún producto muestra un pico recurrente en ${esc(data.month_name)} (hacen falta casi dos años de historial).</p>`;
      return;
    }
    box.innerHTML = data.items.map((p) => `
      <article class="hx-scard" data-id="${p.id}" tabindex="0" role="button" aria-label="${esc(p.title)}: ${fmt(p.strength)} veces su promedio en ${esc(data.month_name)}">
        ${imgTag(p, "hx-thumb")}
        <div><div class="hx-title">${esc(p.title)}</div>
          <div class="meta"><span>${esc(p.category)}</span><span>${money(p.total_cost)}</span></div>
          <div class="hx-strength">×${fmt(p.strength)} <span class="muted">su promedio</span></div></div>
      </article>`).join("") +
      (data.total > data.items.length ? `<p class="muted small hx-more">y ${fmt(data.total - data.items.length)} más</p>` : "");
  }

  /* Tabla de proveedores: better = "max" | "min" indica el mejor valor de la columna */
  const SUP_COLS = [
    { key: "supplier", label: "Proveedor" },
    { key: "products", label: "Productos", num: true, f: fmt },
    { key: "avg_shipping_days", label: "Envío prom.", num: true, better: "min", f: (v) => `${fmt(v)} d` },
    { key: "pct_out_of_stock", label: "Sin stock", num: true, better: "min", f: (v) => `${fmt(v)} %` },
    { key: "avg_rating", label: "Rating", num: true, better: "max", f: (v) => `⭐ ${fmt(v)}` },
    { key: "avg_margin", label: "Margen prom.", num: true, better: "max", f: (v) => `${fmt(v)} %` },
    { key: "price_changes_30d", label: "Cambios de precio 30 d", num: true, better: "min", f: (v) => v === null ? "—" : `${fmt(v)} %` },
    { key: "reliability", label: "Confiabilidad", num: true, better: "max", f: (v) => `${v}/100` },
  ];
  const SOURCE_LABEL = { mock: "Demo", csv: "CSV", api: "API" };

  async function loadSuppliers() {
    try {
      state.sup = await api("/api/suppliers/stats");
      renderSuppliers();
    } catch (e) {
      $(".hx-sup-table tbody").innerHTML = `<tr><td colspan="${SUP_COLS.length}">No se pudo cargar: ${esc(e.message)}</td></tr>`;
    }
  }

  function renderSuppliers() {
    const data = state.sup;
    if (!data) return;
    const { key, dir } = state.sort;
    const items = data.items.slice().sort((a, b) => {
      const x = a[key], y = b[key];
      if (x === null || x === undefined) return 1;
      if (y === null || y === undefined) return -1;
      return (typeof x === "string" ? x.localeCompare(y, "es") : x - y) * dir || a.supplier.localeCompare(b.supplier, "es");
    });
    const best = {};
    if (items.length > 1) {
      for (const c of SUP_COLS.filter((c) => c.better)) {
        const vals = items.map((i) => i[c.key]).filter((v) => v !== null && v !== undefined);
        if (vals.length > 1 && new Set(vals).size > 1) best[c.key] = c.better === "max" ? Math.max(...vals) : Math.min(...vals);
      }
    }
    $(".hx-sup-table thead").innerHTML = `<tr>${SUP_COLS.map((c) => {
      const sorted = c.key === key ? (dir > 0 ? "ascending" : "descending") : "none";
      const arrow = c.key === key ? (dir > 0 ? " ▲" : " ▼") : "";
      return `<th class="${c.num ? "num" : ""}" aria-sort="${sorted}"><button type="button" class="hx-sort" data-sort="${c.key}">${c.label}${arrow}</button></th>`;
    }).join("")}</tr>`;
    $(".hx-sup-table tbody").innerHTML = items.map((r) => `<tr>${SUP_COLS.map((c) => {
      if (c.key === "supplier") {
        return `<td><b>${esc(r.supplier)}</b> <span class="chip ${r.source === "mock" ? "demo-chip" : ""}">${esc(SOURCE_LABEL[r.source] || r.source)}</span></td>`;
      }
      const v = r[c.key];
      const isBest = c.key in best && v === best[c.key];
      const meter = c.key === "reliability"
        ? `<span class="hx-meter" aria-hidden="true"><span style="width:${Math.max(0, Math.min(100, v))}%"></span></span>` : "";
      return `<td class="num${isBest ? " hx-best" : ""}">${meter}${c.f(v)}${isBest ? `<span class="sr-only"> (mejor)</span>` : ""}</td>`;
    }).join("")}</tr>`).join("");
    $("#hxFormula").textContent = `Confiabilidad = ${data.formula}. Envío: 2 días o menos = 1, 25 o más = 0. ` +
      "Rating: 3 = 0, 5 = 1. Estabilidad = 1 − % de productos que cambiaron de costo en los últimos 30 días del historial.";
  }

  function loadTrends() {
    if (!state.built) {
      buildTrends();
      loadCategories();
    }
    loadRanking();
    loadSeason();
    loadSuppliers();
  }

  /* Filas con role=button: Enter o espacio abren el detalle */
  document.addEventListener("keydown", (e) => {
    if ((e.key === "Enter" || e.key === " ") && e.target.matches && e.target.matches(".hx-row, .hx-scard")) {
      e.preventDefault();
      openDetail(Number(e.target.dataset.id));
    }
  });

  DF.detailSections.push(detailSection);
  DF.viewLoaders.trends = loadTrends;
  DF.onResults.push(() => histCache.clear());
  DF.currencyChange.push(() => {
    if (!state.built) return;
    renderRanking();
    renderSeason();
  });
})();
