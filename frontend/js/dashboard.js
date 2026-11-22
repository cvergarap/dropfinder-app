/* Módulo dashboard: pestaña "Panel" (vista de inicio). Se registra en DF (ver app.js).
   Una sola llamada a GET /api/dashboard: indicadores, "Requieren tu atención", gráficos (SVG y barras HTML
   escritos a mano, sin librerías), "Para ti" y "Primeros pasos". Montos en USD → money() (moneda mostrada).
   Colores de los gráficos en css/dashboard.css (validados para la superficie clara y la oscura). */
(() => {
  const SVGNS = "http://www.w3.org/2000/svg";
  const root = () => $("#view-dashboard");
  const FRESH_MS = 3000;             // la precarga del arranque se reutiliza una vez si es reciente
  let data = null;                   // última respuesta (para re-renderizar al cambiar de moneda)
  let prefetchedAt = 0;              // > 0 solo mientras la precarga del arranque no se haya usado
  let loading = null;
  let lastWidth = 0;

  const pctFmt = (v, digits = 1) => `${Number(v).toLocaleString("es", { maximumFractionDigits: digits })} %`;
  const signed = (v) => `${v > 0 ? "+" : v < 0 ? "−" : ""}${pctFmt(Math.abs(v))}`;
  const dayLabel = (s) => new Date(`${s}T12:00:00`).toLocaleDateString("es", { day: "numeric", month: "short" });

  function greeting() {
    const h = new Date().getHours();
    const base = h < 12 ? "Buenos días" : h < 19 ? "Buenas tardes" : "Buenas noches";
    const user = (window.DF && (DF.user || (DF.auth && DF.auth.user))) || null;
    const name = user && (user.name || "").trim().split(/\s+/)[0];
    return name ? `${base}, ${name}` : base;
  }

  /* ================= Carga ================= */
  async function fetchData() {
    if (!loading) {
      loading = api("/api/dashboard").then((d) => { data = d; return d; })
        .finally(() => { loading = null; });
    }
    return loading;
  }

  async function load() {
    const sec = root();
    if (!sec) return;
    if (!data) sec.innerHTML = `<p class="muted db-loading">Cargando tu panel…</p>`;
    else sec.classList.add("db-stale");                 // se mantiene lo anterior mientras recarga
    try {
      const reuse = data && prefetchedAt && Date.now() - prefetchedAt < FRESH_MS;
      prefetchedAt = 0;
      if (!reuse) await fetchData();
      render();
    } catch (e) {
      if (e.status === 401) { sec.innerHTML = ""; data = null; return; }   // el módulo de cuentas muestra el inicio de sesión
      if (!data) sec.innerHTML = `<p class="empty">No se pudo cargar el panel: ${esc(e.message)}
        <button type="button" class="ghost" data-db-retry>Reintentar</button></p>`;
      else toast(`No se pudo actualizar el panel: ${e.message}`);
    } finally {
      sec.classList.remove("db-stale");
    }
  }

  /* ================= Render ================= */
  function render() {
    const sec = root();
    if (!sec || !data) return;
    const d = data;
    const k = d.kpis;
    const fresh = k.imported === 0;
    const updated = new Date(d.generated_at).toLocaleTimeString("es", { hour: "2-digit", minute: "2-digit" });
    sec.innerHTML = `
      <div class="db-head">
        <div><h2>${esc(greeting())}</h2>
          <p class="muted">${fresh ? "Este es tu panel. Te ayudamos a empezar." : "Así va tu tienda hoy."}
            <span class="db-updated">Actualizado a las ${esc(updated)}</span></p></div>
        <button type="button" class="ghost" data-db-refresh>↻ Actualizar</button>
      </div>
      ${fresh ? emptyHero() : ""}
      <div class="db-kpis">${tiles(k)}</div>
      <details class="db-assume"><summary>¿Cómo se calcula la ganancia estimada del mes?</summary>
        <p>${esc(d.assumptions)}</p></details>
      <div class="db-row">
        ${fresh ? "" : attentionCard(d.attention)}
        ${onboardingCard(d.onboarding)}
      </div>
      ${fresh ? "" : `<div class="db-charts">
        <figure class="db-card db-chart-card" id="dbCost">
          <figcaption><h3>Costo de mis productos</h3>
            <p class="muted small">Índice del costo total promedio, últimos ${d.charts.alerts_by_type.days} días (día 1 = 100)</p></figcaption>
          <div class="db-line" data-chart="cost"></div>
          ${costTable(d.charts.cost_index)}
        </figure>
        ${barCard("Alertas por tipo", `Últimos ${d.charts.alerts_by_type.days} días`,
          d.charts.alerts_by_type.items.map((x) => ({ label: x.label, value: x.count })),
          "Sin alertas en los últimos 30 días.", "alerts", "alertas")}
        ${barCard("Mis productos por categoría", "Productos importados",
          d.charts.imports_by_category.items.map((x) => ({ label: x.category, value: x.count })),
          "Aún no importaste productos.", "imports", "productos")}
      </div>`}
      ${recsCard(d.recommendations, fresh)}`;
    drawCost();
  }

  function emptyHero() {
    return `<div class="db-card db-hero">
      <div><h3>Encuentra tu primer producto ganador</h3>
        <p class="muted">Busca en el catálogo, revisa costo, margen y tendencia, e importa los que quieras vender.
          Aquí verás tus indicadores, alertas y ganancia estimada.</p></div>
      <div class="db-hero-actions">
        <button type="button" class="primary" data-db-go="search">Buscar productos</button>
        <button type="button" class="ghost" data-db-go="trends">Ver tendencias</button>
      </div></div>`;
  }

  function tile(view, label, value, sub, cls = "") {
    return `<button type="button" class="db-tile" data-db-go="${view}">
      <span class="db-tile-label">${esc(label)}</span>
      <span class="db-tile-value ${cls}">${value}</span>
      <span class="db-tile-sub">${sub}</span></button>`;
  }

  function tiles(k) {
    const neg = (v) => (v < 0 ? "bad" : "");
    const problems = [k.out_of_stock ? `${fmt(k.out_of_stock)} sin stock` : "", k.negative_margin ? `${fmt(k.negative_margin)} a pérdida` : ""]
      .filter(Boolean).join(" · ");
    return [
      tile("imports", "Productos importados", fmt(k.imported), `${fmt(k.active)} activos · ${fmt(k.drafts)} borradores`),
      tile("imports", "Margen promedio", k.avg_margin === null ? "—" : pctFmt(k.avg_margin),
        k.imported ? "con tu precio de venta" : "importa productos para verlo", k.avg_margin === null ? "" : neg(k.avg_margin)),
      tile("imports", "Ganancia por venta", money(k.potential_profit_per_sale),
        k.active ? `1 venta de cada producto activo (${fmt(k.active)})` : "activa productos para verla", neg(k.potential_profit_per_sale)),
      tile("imports", "Ganancia estimada del mes", money(k.estimated_monthly_profit),
        `supuesto: ${esc(pctFmt(data.estimate.order_share * 100, 2))} de los pedidos del proveedor`, neg(k.estimated_monthly_profit)),
      tile("alerts", "Alertas sin leer", fmt(k.unread_alerts), problems ? esc(problems) : "todo en orden",
        k.unread_alerts ? "warn" : ""),
      tile("saved", "Búsquedas guardadas", fmt(k.saved_searches),
        k.new_in_saved_searches ? `<b>${fmt(k.new_in_saved_searches)}</b> productos nuevos` : "sin productos nuevos"),
    ].join("");
  }

  const REASONS = {
    negative_margin: ["⛔", "A pérdida", "crit"],
    out_of_stock: ["⚠", "Sin stock", "warn"],
    cost_up: ["▲", "Subió el costo", "warn"],
  };

  function attentionCard(att) {
    const rows = att.items.map((a) => {
      const [icon, label, cls] = REASONS[a.reason] || ["•", a.reason, ""];
      const detail = a.reason === "negative_margin"
        ? `Ganancia ${money(a.profit)} por venta (${pctFmt(a.margin)})`
        : a.reason === "cost_up" ? `Costo total ${money(a.total_cost)} (${signed(a.change_pct)})` : esc(a.category);
      return `<li class="db-att">
        ${imgTag(a, "db-thumb")}
        <div class="db-att-main">
          <span class="db-status ${cls}"><span aria-hidden="true">${icon}</span> ${esc(label)}</span>
          <b class="db-att-title">${esc(a.title)}</b>
          <span class="muted small">${esc(a.message)} · ${detail}</span>
        </div>
        <div class="db-att-actions">
          <button type="button" class="ghost small" data-db-detail="${a.product_id}">Ver detalle</button>
          ${a.action === "imports" ? `<button type="button" class="primary small" data-db-go="imports">Ajustar precio</button>` : ""}
        </div></li>`;
    }).join("");
    const more = att.total > att.items.length ? `<p class="muted small">…y ${fmt(att.total - att.items.length)} más en Mis productos.</p>` : "";
    return `<section class="db-card db-attention" aria-labelledby="dbAttTitle">
      <div class="db-card-head"><h3 id="dbAttTitle">Requieren tu atención</h3>
        <button type="button" class="link" data-db-go="imports">Ir a Mis productos →</button></div>
      ${rows ? `<ul class="db-att-list">${rows}</ul>${more}`
        : `<p class="db-ok"><span aria-hidden="true">✓</span> Todo en orden: ningún producto pierde dinero, se quedó sin stock o subió de costo.</p>`}
    </section>`;
  }

  function onboardingCard(ob) {
    if (ob.hidden) return "";
    const pct = ob.total ? Math.round((ob.done / ob.total) * 100) : 0;
    const items = ob.items.map((it) => `
      <li class="db-step ${it.done ? "done" : ""} ${it.disabled ? "disabled" : ""}">
        <span class="db-check" aria-hidden="true">${it.done ? "✓" : ""}</span>
        <div class="db-step-main"><b>${esc(it.label)}</b><span class="muted small">${esc(it.hint || "")}</span></div>
        <div class="db-step-actions">${it.disabled
          ? `<span class="db-soon">Próximamente</span>`
          : it.done ? `<span class="sr-only">Hecho</span>`
          : `${it.action ? `<button type="button" class="ghost small" data-db-go="${esc(it.action.view)}">${esc(it.action.label)}</button>` : ""}
             ${it.dismiss_label ? `<button type="button" class="link small" data-db-dismiss="${esc(it.key)}">${esc(it.dismiss_label)}</button>` : ""}`}
        </div></li>`).join("");
    return `<section class="db-card db-onboarding" aria-labelledby="dbObTitle">
      <div class="db-card-head"><h3 id="dbObTitle">Primeros pasos</h3>
        <button type="button" class="link small" data-db-dismiss="checklist">Ocultar</button></div>
      <div class="db-progress-row"><div class="db-meter" role="progressbar" aria-valuemin="0" aria-valuemax="${ob.total}"
          aria-valuenow="${ob.done}" aria-label="Primeros pasos completados"><span style="width:${pct}%"></span></div>
        <span class="small">${fmt(ob.done)} de ${fmt(ob.total)}</span></div>
      ${ob.complete ? `<p class="db-ok"><span aria-hidden="true">✓</span> ¡Completaste los primeros pasos!</p>` : ""}
      <ul class="db-steps">${items}</ul>
    </section>`;
  }

  /* Barras horizontales (un solo tono): valor al final de la barra, tabla equivalente debajo */
  function barCard(title, sub, rows, emptyMsg, view, unit) {
    const max = Math.max(1, ...rows.map((r) => r.value));
    const total = rows.reduce((s, r) => s + r.value, 0);
    const aria = rows.length ? `${title}: ${rows.map((r) => `${r.label} ${r.value}`).join(", ")}` : `${title}: sin datos`;
    const bars = rows.map((r) => `
      <div class="db-bar-row" title="${esc(r.label)}: ${fmt(r.value)} ${unit}">
        <span class="db-bar-label">${esc(r.label)}</span>
        <span class="db-bar-track"><span class="db-bar" style="width:${Math.max(2, (r.value / max) * 100)}%"></span></span>
        <span class="db-bar-value">${fmt(r.value)}</span></div>`).join("");
    return `<figure class="db-card db-chart-card">
      <figcaption><div class="db-card-head"><h3>${esc(title)}</h3>
        <button type="button" class="link small" data-db-go="${view}">Ver →</button></div>
        <p class="muted small">${esc(sub)}${rows.length ? ` · ${fmt(total)} en total` : ""}</p></figcaption>
      ${rows.length ? `<div class="db-bars" role="img" aria-label="${esc(aria)}">${bars}</div>
        <details class="db-table"><summary>Ver datos en tabla</summary>
          <table class="table"><thead><tr><th>${esc(title.split(" por ")[1] || "Tipo")}</th><th class="num">Cantidad</th></tr></thead>
          <tbody>${rows.map((r) => `<tr><td>${esc(r.label)}</td><td class="num">${fmt(r.value)}</td></tr>`).join("")}</tbody></table></details>`
        : `<p class="muted db-chart-empty">${esc(emptyMsg)}</p>`}
    </figure>`;
  }

  function costTable(ci) {
    if (!ci.points.length) return "";
    return `<details class="db-table"><summary>Ver datos en tabla</summary>
      <table class="table"><thead><tr><th>Fecha</th><th class="num">Índice</th><th class="num">Costo promedio</th></tr></thead>
      <tbody>${ci.points.map((p) => `<tr><td>${esc(dayLabel(p.date))}</td><td class="num">${p.index.toLocaleString("es")}</td>
        <td class="num">${money(p.avg_total_cost)}</td></tr>`).join("")}</tbody></table></details>`;
  }

  function recsCard(recs, fresh) {
    const cats = recs.categories.length ? `En tendencia en ${recs.categories.map(esc).join(", ")}` : "Lo que más crece en el catálogo";
    const cards = recs.items.map((p) => {
      const ch = p.change_pct;
      const delta = ch === null || ch === undefined ? ""
        : `<span class="db-delta ${ch > 0 ? "up" : ch < 0 ? "down" : ""}"><span aria-hidden="true">${ch > 0 ? "▲" : ch < 0 ? "▼" : "■"}</span> ${signed(ch)}</span>`;
      return `<article class="db-rec" data-db-detail="${p.id}" tabindex="0" role="button"
          aria-label="${esc(p.title)}: costo ${esc(money(p.total_cost))}, margen ${esc(pctFmt(p.margin))}">
        ${imgTag(p, "db-rec-img")}
        <div class="db-rec-body">
          <b class="db-rec-title">${esc(p.title)}</b>
          <span class="meta"><span class="chip">${esc(p.category)}</span><span class="chip">${esc(p.supplier)}</span>
            ${p.source === "mock" ? `<span class="chip" title="Producto ficticio de demostración">Demo</span>` : ""}</span>
          <span class="db-rec-nums"><span>${money(p.total_cost)} → <b>${money(p.suggested_price, { kind: "price" })}</b></span>
            <span>Margen <b>${pctFmt(p.margin)}</b></span><span>★ ${fmt(p.score)}</span></span>
          <span class="small muted">${esc(p.reason)} ${delta}</span>
        </div></article>`;
    }).join("");
    return `<section class="db-card db-recs" aria-labelledby="dbRecsTitle">
      <div class="db-card-head"><div><h3 id="dbRecsTitle">Para ti</h3>
        <p class="muted small">${fresh ? "Productos en tendencia para empezar" : cats} · sin los que ya importaste</p></div>
        <button type="button" class="link" data-db-go="trends">Ver tendencias →</button></div>
      ${cards ? `<div class="db-rec-grid">${cards}</div>` : `<p class="muted">Aún no hay suficiente historial para recomendar productos.</p>`}
    </section>`;
  }

  /* ================= Gráfico de línea: índice de costo ================= */
  function el(tag, attrs = {}) {
    const n = document.createElementNS(SVGNS, tag);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
    return n;
  }

  function ticksFor(lo, hi) {
    const span = hi - lo || 1;
    const raw = span / 3;
    const mag = 10 ** Math.floor(Math.log10(raw));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw);
    const a = Math.floor(lo / step) * step;
    const b = Math.ceil(hi / step) * step;
    const out = [];
    for (let v = a; v <= b + 1e-9; v += step) out.push(Math.round(v * 100) / 100);
    return { lo: a, hi: b, ticks: out };
  }

  function drawCost() {
    const box = root() && $(".db-line", root());
    if (!box || !data) return;
    const ci = data.charts.cost_index;
    const pts = ci.points;
    if (!pts.length) { box.innerHTML = `<p class="muted db-chart-empty">Importa productos para ver cómo cambia su costo.</p>`; return; }
    const W = Math.max(260, Math.floor(box.clientWidth || 320));
    lastWidth = W;
    const H = 190, L = 40, R = 52, T = 14, B = 24;
    const vals = pts.map((p) => p.index);
    const { lo, hi, ticks } = ticksFor(Math.min(100, ...vals) - 1, Math.max(100, ...vals) + 1);
    const xAt = (i) => L + (pts.length === 1 ? 0 : (i / (pts.length - 1)) * (W - L - R));
    const yAt = (v) => T + (1 - (v - lo) / (hi - lo || 1)) * (H - T - B);
    const last = pts[pts.length - 1];
    const svg = el("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: "img", tabindex: "0",
      "aria-label": `Índice de costo de tus ${ci.products} productos: de 100 el ${dayLabel(pts[0].date)} a ` +
        `${last.index.toLocaleString("es")} el ${dayLabel(last.date)} (${signed(ci.change_pct)}). Usa las flechas para recorrer los días.` });
    for (const t of ticks) {
      svg.appendChild(el("line", { class: "db-grid", x1: L, x2: W - R, y1: yAt(t), y2: yAt(t) }));
      const tx = el("text", { class: "db-axis", x: L - 6, y: yAt(t) + 4, "text-anchor": "end" });
      tx.textContent = t.toLocaleString("es");
      svg.appendChild(tx);
    }
    svg.appendChild(el("line", { class: "db-ref", x1: L, x2: W - R, y1: yAt(100), y2: yAt(100) }));
    [0, Math.floor((pts.length - 1) / 2), pts.length - 1].forEach((i, n) => {
      const tx = el("text", { class: "db-axis", x: xAt(i), y: H - 6, "text-anchor": n === 0 ? "start" : n === 2 ? "end" : "middle" });
      tx.textContent = dayLabel(pts[i].date);
      svg.appendChild(tx);
    });
    const d = pts.map((p, i) => `${i ? "L" : "M"}${xAt(i).toFixed(1)},${yAt(p.index).toFixed(1)}`).join("");
    svg.appendChild(el("path", { class: "db-cost-line", d }));
    svg.appendChild(el("circle", { class: "db-cost-dot", cx: xAt(pts.length - 1), cy: yAt(last.index), r: 4 }));
    const end = el("text", { class: "db-end", x: xAt(pts.length - 1) + 8, y: yAt(last.index) + 4 });
    end.textContent = last.index.toLocaleString("es");
    svg.appendChild(end);
    const cross = el("line", { class: "db-cross", x1: 0, x2: 0, y1: T, y2: H - B, visibility: "hidden" });
    const hdot = el("circle", { class: "db-cost-dot", r: 4, visibility: "hidden" });
    svg.appendChild(cross);
    svg.appendChild(hdot);
    const tip = document.createElement("div");
    tip.className = "db-tip";
    tip.hidden = true;
    box.replaceChildren(svg, tip);

    let active = -1;
    const showAt = (i) => {
      if (i < 0 || i >= pts.length) return;
      active = i;
      const p = pts[i];
      const x = xAt(i);
      cross.setAttribute("x1", x); cross.setAttribute("x2", x); cross.setAttribute("visibility", "visible");
      hdot.setAttribute("cx", x); hdot.setAttribute("cy", yAt(p.index)); hdot.setAttribute("visibility", "visible");
      tip.replaceChildren();
      const v = document.createElement("b");
      v.textContent = `Índice ${p.index.toLocaleString("es")}`;
      const c = document.createElement("span");
      c.textContent = `Costo promedio ${money(p.avg_total_cost)}`;
      const dt = document.createElement("span");
      dt.className = "muted";
      dt.textContent = dayLabel(p.date);
      tip.append(v, c, dt);
      tip.hidden = false;
      const tw = tip.offsetWidth;
      tip.style.left = `${Math.min(W - tw, Math.max(0, x - tw / 2))}px`;
      tip.style.top = `${Math.max(0, yAt(p.index) - tip.offsetHeight - 12)}px`;
    };
    const hide = () => {
      active = -1;
      cross.setAttribute("visibility", "hidden"); hdot.setAttribute("visibility", "hidden");
      tip.hidden = true;
    };
    svg.addEventListener("pointermove", (ev) => {
      const r = svg.getBoundingClientRect();
      const x = ((ev.clientX - r.left) / r.width) * W;
      const i = Math.round(((x - L) / (W - L - R)) * (pts.length - 1));
      showAt(Math.max(0, Math.min(pts.length - 1, i)));
    });
    svg.addEventListener("pointerleave", hide);
    svg.addEventListener("blur", hide);
    svg.addEventListener("focus", () => showAt(pts.length - 1));
    svg.addEventListener("keydown", (ev) => {
      if (ev.key === "ArrowLeft" || ev.key === "ArrowRight") {
        ev.preventDefault();
        showAt(Math.max(0, Math.min(pts.length - 1, (active < 0 ? pts.length - 1 : active) + (ev.key === "ArrowLeft" ? -1 : 1))));
      } else if (ev.key === "Escape") hide();
    });
  }

  /* ================= Eventos ================= */
  async function dismiss(key, btn) {
    btn.disabled = true;
    try {
      await api(`/api/dashboard/onboarding/${encodeURIComponent(key)}/dismiss`, { method: "POST" });
      data = null;
      await load();
      if (key === "checklist") toast("Primeros pasos ocultos");
    } catch (e) { btn.disabled = false; toast(e.message); }
  }

  function bind() {
    const sec = root();
    if (!sec) return;
    sec.addEventListener("click", (e) => {
      const go = e.target.closest("[data-db-go]");
      if (go) { show(go.dataset.dbGo); window.scrollTo({ top: 0 }); return; }
      const det = e.target.closest("[data-db-detail]");
      if (det) { openDetail(Number(det.dataset.dbDetail)); return; }
      const dis = e.target.closest("[data-db-dismiss]");
      if (dis) { dismiss(dis.dataset.dbDismiss, dis); return; }
      if (e.target.closest("[data-db-refresh]")) { load(); return; }
      if (e.target.closest("[data-db-retry]")) { data = null; load(); }
    });
    sec.addEventListener("keydown", (e) => {
      const rec = e.target.closest(".db-rec");
      if (rec && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); openDetail(Number(rec.dataset.dbDetail)); }
    });
    let t;
    window.addEventListener("resize", () => {
      clearTimeout(t);
      t = setTimeout(() => {
        const box = $(".db-line", sec);
        if (box && sec.classList.contains("active") && Math.floor(box.clientWidth) !== lastWidth) drawCost();
      }, 150);
    });
  }

  DF.viewLoaders.dashboard = load;          // al abrir la pestaña siempre se recarga (es una sola llamada)
  DF.currencyChange.push(() => { if (data) render(); });
  /* Para otros módulos (p. ej. cuentas tras iniciar sesión): ir al panel como vista de inicio */
  DF.dashboard = { reload: load, home: () => show("dashboard") };

  bind();
  /* Vista inicial: si el usuario aún no cambió de pestaña, se abre el Panel. Con 401 (sin sesión) no se
     hace nada: el módulo de cuentas muestra el inicio de sesión y el panel se carga al abrir la pestaña. */
  const startView = () => ($(".tab.active") || {}).dataset;
  const initial = startView() && startView().view;
  fetchData().then(() => {
    prefetchedAt = Date.now();
    const cur = startView() && startView().view;
    if (cur === initial && cur !== "dashboard") show("dashboard");
    else if (cur === "dashboard") render();
  }).catch(() => { /* 401 u otro error: se reintenta al abrir la pestaña */ });
})();
