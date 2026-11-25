/* Módulo compare: comparador de 2 a 4 productos y "el mismo producto en otros proveedores".
 * Se registra en DF (ver app.js): botón "⇄ Comparar" en cada tarjeta, badge "N proveedores",
 * sección en el detalle y la pestaña "Comparar". Usa las globales de app.js: $, $$, esc, fmt, api,
 * toast, money, imgTag, openDetail, FLAGS. */
(function () {
  "use strict";

  const STORE_KEY = "dropfinder.compare.v1";
  const MAX = 4;
  const COUNT_TTL = 60_000;           // ms que se reutiliza el conteo de proveedores de una tarjeta

  /* ---------------- selección (persistida por navegador; funciona sin almacenamiento) ---------------- */
  let selection = readSelection();

  function readSelection() {
    try {
      const v = JSON.parse(localStorage.getItem(STORE_KEY) || "[]");
      return Array.isArray(v) ? [...new Set(v.filter(Number.isInteger))].slice(0, MAX) : [];
    } catch { return []; }
  }
  function writeSelection() {
    try { localStorage.setItem(STORE_KEY, JSON.stringify(selection)); } catch { /* sin almacenamiento */ }
  }
  const isSelected = (id) => selection.includes(id);

  function setSelection(ids) {
    selection = [...new Set(ids)].slice(0, MAX);
    writeSelection();
    syncUi();
    if (viewActive()) loadView();
  }
  function add(id) {
    if (isSelected(id)) return true;
    if (selection.length >= MAX) {
      toast(`La comparación está llena (máx. ${MAX}). Quita alguno en la pestaña Comparar.`);
      return false;
    }
    setSelection([...selection, id]);
    return true;
  }
  const remove = (id) => setSelection(selection.filter((x) => x !== id));
  function toggle(id) {
    if (isSelected(id)) { remove(id); toast("Quitado de la comparación"); }
    else if (add(id)) toast(`Agregado a la comparación (${selection.length}/${MAX})`);
  }

  /* Pastilla de la pestaña y estado de todos los botones visibles */
  function syncUi() {
    const pill = $("#compareCount");
    if (pill) pill.textContent = selection.length;
    $$(".cmp-toggle").forEach((b) => {
      const on = isSelected(Number(b.dataset.cmpId));
      b.setAttribute("aria-pressed", String(on));
      b.textContent = on ? "✓ En comparación" : "⇄ Comparar";
    });
  }

  const viewActive = () => { const v = $("#view-compare"); return !!(v && v.classList.contains("active")); };
  const gotoCompare = () => { const t = $('.tab[data-view="compare"]'); if (t) t.click(); };

  /* ---------------- tarjeta: botón "⇄ Comparar" ---------------- */
  DF.cardExtras.push((p) => {
    const on = isSelected(p.id);
    return `<button type="button" class="cmp-toggle" data-cmp-id="${p.id}" aria-pressed="${on}"
      title="Agregar o quitar de la comparación (máx. ${MAX})">${on ? "✓ En comparación" : "⇄ Comparar"}</button>`;
  });

  /* Clics del módulo: en captura, para que app.js no abra el detalle al pulsar dentro de la tarjeta */
  let scrollToAltsFor = null;
  document.addEventListener("click", (e) => {
    const t = e.target.closest(".cmp-toggle");
    if (t) {
      e.stopPropagation(); e.preventDefault();
      toggle(Number(t.dataset.cmpId));
      return;
    }
    const b = e.target.closest(".cmp-alts-badge");
    if (b) {
      e.stopPropagation(); e.preventDefault();
      scrollToAltsFor = Number(b.dataset.id);
      safeOpen(scrollToAltsFor);
    }
  }, true);

  /* openDetail puede llamarse con el diálogo ya abierto (fila de alternativas) */
  function safeOpen(id) {
    Promise.resolve().then(() => openDetail(id)).catch((e) => {
      if (!$("#detail").open) toast(e.message || "No se pudo abrir el producto");
    });
  }

  /* ---------------- resultados: badge "N proveedores" ---------------- */
  const counts = new Map();   // id -> {n, sup, t}

  DF.onResults.push(async (data) => {
    const ids = (data.items || []).map((p) => p.id);
    const now = Date.now();
    const need = ids.filter((id) => !counts.has(id) || now - counts.get(id).t > COUNT_TTL);
    if (need.length) {
      try {
        const r = await api(`/api/alternatives/counts?ids=${need.slice(0, 100).join(",")}`);
        need.forEach((id) => {
          const k = String(id);
          counts.set(id, { n: (r.counts || {})[k] || 1, sup: (r.suppliers || {})[k] || (r.counts || {})[k] || 1, t: now });
        });
      } catch { return; }       // sin conteos no se muestran badges
    }
    $$("#grid .card[data-id]").forEach((card) => {
      const id = Number(card.dataset.id);
      const c = counts.get(id);
      const box = $(".img", card);
      if (!c || !box || $(".cmp-alts-badge", box) || c.sup < 2) return;
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "badge cmp-alts-badge";
      btn.dataset.id = id;
      const word = document.createElement("span");
      word.className = "cmp-badge-word";
      word.textContent = " proveedores";
      btn.append(`⇄ ${c.sup}`, word);       // en tarjetas angostas solo "⇄ N" (el aria-label lo dice completo)
      btn.title = `${c.n} ofertas de ${c.sup} proveedores: ver y comparar`;
      btn.setAttribute("aria-label", `Disponible en ${c.sup} proveedores: ver alternativas`);
      box.appendChild(btn);
    });
  });

  /* ---------------- detalle: "Este producto en otros proveedores" ---------------- */
  const altCache = new Map();   // id -> respuesta de /alternatives (para re-render por moneda sin parpadeo)

  DF.detailSections.push((p, container) => {
    const sec = document.createElement("section");
    sec.className = "cmp-alts-sec";
    sec.id = "cmpAlts";
    sec.setAttribute("aria-labelledby", "cmpAltsTitle");
    sec.innerHTML = `<h3 id="cmpAltsTitle">Este producto en otros proveedores</h3><p class="muted">Buscando…</p>`;
    container.appendChild(sec);
    const render = (d) => {
      if (!sec.isConnected) return;
      sec.innerHTML = altsHtml(p, d);
      if (scrollToAltsFor === p.id) {
        scrollToAltsFor = null;
        sec.scrollIntoView({ block: "start", behavior: "smooth" });
      }
    };
    if (altCache.has(p.id)) render(altCache.get(p.id));
    api(`/api/products/${p.id}/alternatives`)
      .then((d) => { altCache.set(p.id, d); render(d); })
      .catch(() => { if (!altCache.has(p.id) && sec.isConnected) sec.remove(); });
    sec.addEventListener("click", (e) => {
      const all = e.target.closest(".cmp-all");
      if (all) { compareGroup(altCache.get(p.id)); return; }
      const tr = e.target.closest("tr[data-id]");
      if (tr && Number(tr.dataset.id) !== p.id) safeOpen(Number(tr.dataset.id));
    });
    sec.addEventListener("keydown", (e) => {
      const tr = e.target.closest("tr[data-id]");
      if (tr && (e.key === "Enter" || e.key === " ") && Number(tr.dataset.id) !== p.id) {
        e.preventDefault(); safeOpen(Number(tr.dataset.id));
      }
    });
  });

  function altsHtml(p, d) {
    const items = d.items || [];
    if (items.length < 2) {
      return `<h3 id="cmpAltsTitle">Este producto en otros proveedores</h3>
        <p class="muted small">No encontramos este producto en otros proveedores del catálogo.</p>`;
    }
    const tags = (id) => [
      d.best.cheapest === id ? `<span class="cmp-tag cheap">Más barato</span>` : "",
      d.best.fastest === id ? `<span class="cmp-tag fast">Más rápido</span>` : "",
      d.best.best_margin === id ? `<span class="cmp-tag margin">Mejor margen</span>` : "",
    ].join("");
    const rows = items.map((x) => {
      const me = x.id === p.id;
      return `<tr data-id="${x.id}" class="${me ? "current" : ""}" ${me ? 'aria-current="true"' : 'tabindex="0"'}
          title="${me ? "Producto actual" : "Ver este producto"}">
        <th scope="row"><span class="cmp-sup">${esc(x.supplier)}</span>${me ? ` <span class="cmp-here">(este)</span>` : ""}
          <span class="cmp-tags">${tags(x.id)}</span></th>
        <td>${FLAGS[x.warehouse] || ""} ${esc(x.warehouse)}</td>
        <td class="num">${money(x.total_cost)}</td>
        <td class="num">${money(x.suggested_price, { kind: "price" })}</td>
        <td class="num">${x.margin}%</td>
        <td class="num">${x.shipping_known === false ? "no informado" : `${x.shipping_days} d`}</td>
        <td class="num">⭐ ${x.rating}</td>
        <td class="num">${x.stock > 0 ? fmt(x.stock) : '<span class="bad">0</span>'}</td>
      </tr>`;
    }).join("");
    const more = d.total > items.length ? `<p class="muted small">Se muestran ${items.length} de ${fmt(d.total)} ofertas (las más baratas).</p>` : "";
    return `<h3 id="cmpAltsTitle">Este producto en otros proveedores <span class="pill">${fmt(d.total || items.length)}</span></h3>
      <div class="cmp-scroll"><table class="cmp-alts">
        <thead><tr><th scope="col">Proveedor</th><th scope="col">Almacén</th><th scope="col" class="num">Costo total</th>
          <th scope="col" class="num">Precio sug.</th><th scope="col" class="num">Margen</th><th scope="col" class="num">Envío</th>
          <th scope="col" class="num">Rating</th><th scope="col" class="num">Stock</th></tr></thead>
        <tbody>${rows}</tbody></table></div>
      ${more}
      <div class="cmp-alts-actions"><button type="button" class="ghost cmp-all">⇄ Comparar todos</button>
        <span class="muted small">Agrega hasta ${MAX} a la comparación.</span></div>`;
  }

  /* "Comparar todos": el actual y los ganadores primero, luego por costo; solo hasta llenar los MAX lugares */
  function compareGroup(d) {
    if (!d) return;
    const order = [d.product_id, d.best.cheapest, d.best.fastest, d.best.best_margin, d.best.best_rating,
      ...d.items.map((x) => x.id)];
    const group = [...new Set(order.filter(Number.isInteger))];
    const next = [...selection];
    let added = 0;
    for (const id of group) {
      if (next.length >= MAX) break;
      if (!next.includes(id)) { next.push(id); added++; }
    }
    const left = group.filter((id) => !next.includes(id)).length;
    setSelection(next);
    toast(left
      ? `Se agregaron ${added}; ${left} no caben (máx. ${MAX}). Quita alguno en Comparar.`
      : `Comparando ${next.length} productos`);
    const dlg = $("#detail");
    if (dlg.open) dlg.close();
    gotoCompare();
  }

  /* ---------------- pestaña "Comparar" ---------------- */
  // Fila → [etiqueta, formato]. El servidor indica el ganador de cada una en `best`.
  const ROWS = [
    ["total_cost", "Costo total", (v) => money(v)],
    ["suggested_price", "Precio sugerido", (v) => money(v, { kind: "price" })],
    ["profit", "Ganancia", (v) => money(v)],
    ["margin", "Margen", (v) => `${v}%`],
    ["shipping_days", "Días de envío", (v) => `${v} días`],
    ["rating", "Rating", (v) => `⭐ ${v}`],
    ["orders", "Pedidos", (v) => fmt(v)],
    ["trend", "Tendencia 7 días", (v) => `${v > 0 ? "+" : ""}${v}%`],
    ["score", "Score ganador", (v) => `★ ${v}/100`],
    ["stock", "Stock", (v) => (v > 0 ? fmt(v) : '<span class="bad">Sin stock</span>')],
  ];
  const SHIP_FIELDS = new Set(["total_cost", "suggested_price", "profit", "margin", "shipping_days"]);
  let last = null;        // última respuesta de /api/compare (re-render al cambiar la moneda)
  let single = null;      // producto cuando solo hay 1 seleccionado
  let loadSeq = 0;

  function shell() {
    const v = $("#view-compare");
    if (!$("#cmpBody", v)) {
      v.innerHTML = `<div class="section-head">
          <div><h2>Comparar productos</h2>
            <p class="muted">Hasta ${MAX} productos lado a lado. El mejor valor de cada fila queda marcado con ✓.</p></div>
          <button type="button" id="cmpClear" class="ghost">Vaciar</button></div>
        <div id="cmpBody" aria-live="polite"></div>`;
      $("#cmpClear", v).addEventListener("click", () => { setSelection([]); toast("Comparación vaciada"); });
      $("#cmpBody", v).addEventListener("click", (e) => {
        const rm = e.target.closest(".cmp-remove");
        if (rm) { remove(Number(rm.dataset.id)); return; }
        if (e.target.closest(".cmp-go-search")) { const t = $('.tab[data-view="search"]'); if (t) t.click(); return; }
        const op = e.target.closest(".cmp-open");
        if (op) safeOpen(Number(op.dataset.id));
      });
    }
    return $("#cmpBody", v);
  }

  async function loadView() {
    const body = shell();
    $("#cmpClear").disabled = selection.length === 0;
    const seq = ++loadSeq;
    last = null; single = null;
    if (selection.length === 0) { body.innerHTML = emptyHtml(); return; }
    if (selection.length === 1) {
      try { single = await api(`/api/products/${selection[0]}`); }
      catch (e) {
        if (e.status === 404) { setSelection([]); return; }
        if (seq === loadSeq) body.innerHTML = `<p class="empty">No se pudo cargar el producto: ${esc(e.message)}</p>`;
        return;
      }
      if (seq === loadSeq) renderView();
      return;
    }
    body.classList.add("loading");
    try {
      const d = await api(`/api/compare?ids=${selection.join(",")}`);
      if (seq !== loadSeq) return;
      if (d.missing && d.missing.length) {
        toast(`${d.missing.length === 1 ? "Un producto ya no existe" : `${d.missing.length} productos ya no existen`} en el catálogo: se quitó de la comparación`);
        setSelection(selection.filter((id) => !d.missing.includes(id)));
        return;
      }
      last = d;
      renderView();
      loadChart(d.items, seq);
    } catch (e) {
      if (seq === loadSeq) body.innerHTML = `<p class="empty">No se pudo cargar la comparación: ${esc(e.message)}</p>`;
    } finally { body.classList.remove("loading"); }
  }

  function emptyHtml() {
    return `<div class="cmp-empty">
      <p class="cmp-empty-icon" aria-hidden="true">⇄</p>
      <h3>Aún no hay productos para comparar</h3>
      <ol>
        <li>En <b>Buscar productos</b>, pulsa <b>⇄ Comparar</b> en 2 a ${MAX} tarjetas.</li>
        <li>O abre un producto con el badge <b>N proveedores</b> y pulsa <b>Comparar todos</b>.</li>
        <li>Vuelve a esta pestaña: verás costo, precio, margen, envío, rating y tendencia lado a lado.</li>
      </ol>
      <button type="button" class="primary cmp-go-search">Ir a buscar productos</button>
    </div>`;
  }

  function renderView() {
    const body = shell();
    if (single) {
      body.innerHTML = `<p class="note">Tienes 1 producto seleccionado. Agrega al menos otro desde <b>Buscar productos</b> para compararlos.</p>
        <div class="cmp-single">${imgTag(single)}<div><b>${esc(single.title)}</b><br><span class="muted">${esc(single.supplier)} · ${money(single.total_cost)}</span></div>
        <button type="button" class="link cmp-remove" data-id="${single.id}">Quitar</button></div>`;
      return;
    }
    if (!last) return;
    const { items, best } = last;
    const head = items.map((p, i) => `<th scope="col" class="cmp-col">
        <div class="cmp-head">
          <button type="button" class="cmp-open cmp-img" data-id="${p.id}" aria-label="Ver detalle de ${esc(p.title)}">${imgTag(p)}</button>
          <span class="cmp-key" aria-hidden="true"><i class="cmp-s${i + 1}"></i>${i + 1}</span>
          <button type="button" class="link cmp-open cmp-title" data-id="${p.id}">${esc(p.title)}</button>
          <span class="chip">${esc(p.supplier)}</span>
          <span class="muted small">${FLAGS[p.warehouse] || ""} ${esc(p.warehouse)} · ${esc(p.category)}</span>
          <button type="button" class="link cmp-remove" data-id="${p.id}" aria-label="Quitar ${esc(p.title)} de la comparación">✕ Quitar</button>
        </div></th>`).join("");
    const rows = ROWS.map(([field, label, f]) => {
      const winner = best[field];
      const winVal = winner == null ? null : (items.find((x) => x.id === winner) || {})[field];
      const cells = items.map((p) => {
        // CJ no informa el envío: su costo total/días son valores por defecto y no compiten por «mejor»
        const unk = p.shipping_known === false && SHIP_FIELDS.has(field);
        const isBest = !unk && winner != null && p[field] === winVal;     // empates: se marcan todos los ganadores
        const text = unk && field === "shipping_days" ? "Envío no informado" : f(p[field]);
        return `<td class="num${isBest ? " best" : ""}">${text}${unk && field !== "shipping_days" ? ` <span class="muted small">(sin envío)</span>` : ""}${isBest ? ` <span class="cmp-best">✓ mejor</span>` : ""}</td>`;
      }).join("");
      return `<tr><th scope="row">${label}</th>${cells}</tr>`;
    }).join("");
    body.innerHTML = `<div class="cmp-wrap"><table class="cmp-table">
        <caption class="sr-only">Comparación de ${items.length} productos; el mejor valor de cada fila está marcado como “mejor”.</caption>
        <thead><tr><th scope="col" class="cmp-corner">Métrica</th>${head}</tr></thead>
        <tbody>${rows}</tbody></table></div>
      ${items.length < MAX ? `<p class="muted small cmp-hint">Puedes agregar ${MAX - items.length} más desde Buscar productos.</p>` : ""}
      <section id="cmpChart" class="cmp-chart" hidden></section>`;
    if (chartState && chartState.key === items.map((p) => p.id).join(",")) drawChart();
  }

  /* ---------------- gráfico de pedidos superpuesto (si existe /api/products/{id}/history) ---------------- */
  let historyMissing = false;   // el servidor no tiene el endpoint: no se vuelve a pedir en esta sesión
  let chartState = null;        // {key, series:[{id, title, supplier, idx, points:[{date, v}]}]}

  async function loadChart(items, seq) {
    chartState = null;
    if (historyMissing) return;
    const get = (p) => api(`/api/products/${p.id}/history?days=30`);
    // Primero uno: si el endpoint no existe, una sola petición fallida (y no se vuelve a pedir)
    const first = await Promise.allSettled([get(items[0])]);
    if (seq !== loadSeq) return;
    const noRoute = (r) => r.status === "rejected" && r.reason && r.reason.status === 404 && /ruta/i.test(r.reason.message || "");
    const res = noRoute(first[0]) ? first : first.concat(await Promise.allSettled(items.slice(1).map(get)));
    if (seq !== loadSeq) return;
    if (res.some(noRoute)) { historyMissing = true; return; }
    const series = [];
    res.forEach((r, i) => {
      if (r.status !== "fulfilled" || !Array.isArray(r.value.points)) return;
      const pts = r.value.points
        .filter((x) => x && x.date && Number.isFinite(Number(x.orders)))
        .map((x) => ({ date: String(x.date).slice(0, 10), v: Number(x.orders) }))
        .sort((a, b) => (a.date < b.date ? -1 : 1));
      if (pts.length >= 2) series.push({ id: items[i].id, title: items[i].title, supplier: items[i].supplier, idx: i, points: pts });
    });
    if (!series.length) return;     // sin datos suficientes: el gráfico queda oculto
    chartState = { key: items.map((p) => p.id).join(","), series };
    drawChart();
  }

  const SVGNS = "http://www.w3.org/2000/svg";
  const compact = new Intl.NumberFormat("es", { notation: "compact", maximumFractionDigits: 1 });
  const fmtDate = (iso) => { const [y, m, d] = iso.split("-"); return `${d}/${m}`; };

  function niceMax(v) {
    if (v <= 0) return 1;
    const p = 10 ** Math.floor(Math.log10(v));
    const n = v / p;
    return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10) * p;
  }

  function drawChart() {
    const box = $("#cmpChart");
    if (!box || !chartState) return;
    const { series } = chartState;
    const dates = [...new Set(series.flatMap((s) => s.points.map((p) => p.date)))].sort();
    if (dates.length < 2) return;
    const allV = series.flatMap((s) => s.points.map((p) => p.v));
    const minV = Math.min(...allV);
    const maxV = Math.max(...allV);
    // Eje desde 0 salvo que los valores (acumulados) estén muy lejos de 0: así se ven las diferencias
    let lo = 0;
    if (minV > maxV * 0.6) { const st = niceMax((maxV - minV || 1) / 4); lo = Math.floor(minV / st) * st; }
    const step = niceMax((maxV - lo || 1) / 4);            // marcas redondas (1, 2, 2,5 o 5 × 10ⁿ)
    const nTicks = Math.max(1, Math.ceil((maxV - lo) / step));
    const hi = lo + nTicks * step;

    box.hidden = false;
    $("#cmpBody").classList.add("has-chart");
    box.innerHTML = `<h3>Pedidos · últimos 30 días</h3>
      <ul class="cmp-legend">${series.map((s) => `<li><i class="cmp-s${s.idx + 1}"></i><b>${s.idx + 1}</b> <span></span></li>`).join("")}</ul>
      <div class="cmp-plot"><div class="cmp-tip" role="status" hidden></div></div>
      <details class="cmp-data"><summary>Ver datos del gráfico</summary><div class="cmp-scroll"><table class="table"></table></div></details>`;
    // Nombres con textContent (vienen de catálogos CSV)
    $$(".cmp-legend li span", box).forEach((el, i) => { el.textContent = `${series[i].title} · ${series[i].supplier}`; });

    // Ancho real del contenedor (texto a 11 px reales, sin escalar); se redibuja al cambiar el tamaño
    const W = Math.max(280, Math.round($(".cmp-plot", box).clientWidth || 720));
    const H = W < 500 ? 200 : 240, M = { l: 44, r: 30, t: 12, b: 26 };
    const iw = W - M.l - M.r, ih = H - M.t - M.b;
    const x = (i) => M.l + (dates.length === 1 ? iw / 2 : (i / (dates.length - 1)) * iw);
    const y = (v) => M.t + ih - ((v - lo) / (hi - lo)) * ih;
    const svg = document.createElementNS(SVGNS, "svg");
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    svg.setAttribute("class", "cmp-svg");
    svg.setAttribute("role", "img");
    svg.setAttribute("tabindex", "0");
    svg.setAttribute("aria-label", `Pedidos de los últimos 30 días de ${series.length} productos. Usa las flechas para recorrer las fechas; los datos están también en la tabla.`);
    const el = (tag, attrs, parent = svg) => {
      const n = document.createElementNS(SVGNS, tag);
      for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
      parent.appendChild(n);
      return n;
    };
    // Rejilla y eje Y
    for (let k = 0; k <= nTicks; k++) {
      const v = lo + step * k;
      el("line", { x1: M.l, x2: W - M.r, y1: y(v), y2: y(v), class: k === 0 ? "cmp-axis" : "cmp-grid" });
      el("text", { x: M.l - 6, y: y(v) + 4, "text-anchor": "end", class: "cmp-tick" }).textContent = compact.format(v);
    }
    // Eje X: primera, media y última fecha
    [0, Math.floor((dates.length - 1) / 2), dates.length - 1].forEach((i, k) => {
      el("text", { x: x(i), y: H - 6, "text-anchor": k === 0 ? "start" : k === 2 ? "end" : "middle", class: "cmp-tick" })
        .textContent = fmtDate(dates[i]);
    });
    // Líneas
    const di = new Map(dates.map((d, i) => [d, i]));
    series.forEach((s) => {
      const d = s.points.map((p, k) => `${k ? "L" : "M"}${x(di.get(p.date)).toFixed(1)},${y(p.v).toFixed(1)}`).join("");
      el("path", { d, class: `cmp-line cmp-k${s.idx + 1}` });
    });
    // Puntos finales con anillo y número de serie (etiqueta directa; se separan con una guía si chocan)
    const ends = series.map((s) => { const p = s.points[s.points.length - 1]; return { s, cx: x(di.get(p.date)), cy: y(p.v) }; })
      .sort((a, b) => a.cy - b.cy);
    let prev = -Infinity;
    ends.forEach((e) => { e.ly = Math.max(e.cy, prev + 12); prev = e.ly; });
    ends.forEach((e) => {
      if (Math.abs(e.ly - e.cy) > 1) el("line", { x1: e.cx + 5, y1: e.cy, x2: e.cx + 12, y2: e.ly - 4, class: "cmp-leader" });
      el("circle", { cx: e.cx, cy: e.cy, r: 4, class: `cmp-dot cmp-k${e.s.idx + 1}` });
      el("text", { x: e.cx + 14, y: e.ly, class: "cmp-endlabel" }).textContent = String(e.s.idx + 1);
    });
    // Capa de interacción: línea vertical que sigue al puntero + tooltip con todas las series
    const cross = el("line", { y1: M.t, y2: M.t + ih, class: "cmp-cross", visibility: "hidden" });
    const hit = el("rect", { x: M.l, y: M.t, width: iw, height: ih, fill: "transparent" });
    const plot = $(".cmp-plot", box);
    plot.insertBefore(svg, plot.firstChild);
    const tip = $(".cmp-tip", box);
    let cur = -1;
    const showAt = (i) => {
      cur = Math.max(0, Math.min(dates.length - 1, i));
      const cx = x(cur);
      cross.setAttribute("x1", cx); cross.setAttribute("x2", cx);
      cross.setAttribute("visibility", "visible");
      tip.replaceChildren();
      const h = document.createElement("div"); h.className = "cmp-tip-date"; h.textContent = fmtDate(dates[cur]);
      tip.appendChild(h);
      series.forEach((s) => {
        const pt = s.points.find((p) => p.date === dates[cur]);
        const row = document.createElement("div"); row.className = "cmp-tip-row";
        const key = document.createElement("i"); key.style.background = `var(--cmp-s${s.idx + 1})`;
        const val = document.createElement("b"); val.textContent = pt ? fmt(pt.v) : "—";
        const name = document.createElement("span"); name.textContent = `${s.idx + 1} · ${s.title}`;
        row.append(key, val, name);
        tip.appendChild(row);
      });
      tip.hidden = false;
      const rect = svg.getBoundingClientRect();
      const px = (cx / W) * rect.width;
      tip.style.left = `${Math.min(Math.max(px + 10, 0), Math.max(0, rect.width - tip.offsetWidth))}px`;
      if (px + 10 + tip.offsetWidth > rect.width) tip.style.left = `${Math.max(0, px - tip.offsetWidth - 10)}px`;
    };
    const hide = () => { cross.setAttribute("visibility", "hidden"); tip.hidden = true; };
    hit.addEventListener("pointermove", (e) => {
      const rect = svg.getBoundingClientRect();
      const sx = ((e.clientX - rect.left) / rect.width) * W;
      showAt(Math.round(((sx - M.l) / iw) * (dates.length - 1)));
    });
    hit.addEventListener("pointerleave", hide);
    svg.addEventListener("focus", () => showAt(cur < 0 ? dates.length - 1 : cur));
    svg.addEventListener("blur", hide);
    svg.addEventListener("keydown", (e) => {
      if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
        e.preventDefault(); showAt((cur < 0 ? dates.length - 1 : cur) + (e.key === "ArrowLeft" ? -1 : 1));
      }
    });

    // Tabla con los mismos datos (accesible sin pasar el puntero)
    const table = $(".cmp-data table", box);
    const thead = table.createTHead().insertRow();
    ["Fecha", ...series.map((s) => `${s.idx + 1} · ${s.title}`)].forEach((t, i) => {
      const th = document.createElement("th"); th.textContent = t; if (i) th.className = "num"; thead.appendChild(th);
    });
    const tb = table.createTBody();
    dates.forEach((d) => {
      const tr = tb.insertRow();
      tr.insertCell().textContent = fmtDate(d);
      series.forEach((s) => {
        const pt = s.points.find((p) => p.date === d);
        const td = tr.insertCell(); td.className = "num"; td.textContent = pt ? fmt(pt.v) : "—";
      });
    });
  }

  let resizeT;
  window.addEventListener("resize", () => {
    clearTimeout(resizeT);
    resizeT = setTimeout(() => { if (viewActive() && chartState && $("#cmpChart") && !$("#cmpChart").hidden) drawChart(); }, 150);
  });

  /* ---------------- registro ---------------- */
  DF.viewLoaders.compare = loadView;
  DF.currencyChange.push(() => { if (viewActive() && (last || single)) renderView(); });
  // Otra pestaña del navegador cambió la selección
  window.addEventListener("storage", (e) => {
    if (e.key !== STORE_KEY) return;
    selection = readSelection();
    syncUi();
    if (viewActive()) loadView();
  });
  syncUi();
})();
