/* Autocompletado accesible (patrón ARIA combobox + listbox) para el buscador.
 * initSuggest({input, onSelect}) — onSelect(item) recibe {text, type}. */
(function (root) {
  "use strict";

  const TYPES = {
    producto: { icon: "📦", label: "" },
    categoria: { icon: "🏷️", label: "Categoría" },
    busqueda: { icon: "🔍", label: "Búsqueda popular" },
  };
  const escHtml = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  /* Normaliza carácter a carácter (sin tildes, minúsculas) conservando la posición de cada letra original. */
  function normChars(s) {
    const out = []; const map = [];
    [...String(s)].forEach((ch, i) => {
      const n = ch.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();
      for (const c of n) { out.push(c); map.push(i); }
    });
    return { text: out.join(""), map };
  }

  /* Resalta la coincidencia con lo escrito: preferentemente al inicio de una palabra. */
  function highlight(text, query) {
    const chars = [...String(text)];
    const q = normChars(query.trim()).text;
    if (!q) return escHtml(text);
    const { text: nt, map } = normChars(text);
    let idx = -1;
    for (let from = 0; (from = nt.indexOf(q, from)) !== -1; from++) {
      if (from === 0 || /[\s\-/(]/.test(nt[from - 1])) { idx = from; break; }
      if (idx === -1) idx = from;
    }
    if (idx === -1) return escHtml(text);
    const start = map[idx]; const end = map[idx + q.length - 1] + 1;
    return escHtml(chars.slice(0, start).join("")) + "<mark>" + escHtml(chars.slice(start, end).join("")) +
      "</mark>" + escHtml(chars.slice(end).join(""));
  }

  function initSuggest({ input, onSelect, minChars = 2, delay = 120, limit = 8 }) {
    const list = document.createElement("ul");
    list.id = `${input.id}-suggest`;
    list.className = "suggest hidden";
    list.setAttribute("role", "listbox");
    list.setAttribute("aria-label", "Sugerencias");
    input.insertAdjacentElement("afterend", list);
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-autocomplete", "list");
    input.setAttribute("aria-expanded", "false");
    input.setAttribute("aria-controls", list.id);

    let items = []; let active = -1; let timer = null; let ctrl = null; let disabled = false; let lastQuery = "";

    const isOpen = () => !list.classList.contains("hidden");
    function close() {
      clearTimeout(timer);
      if (ctrl) { ctrl.abort(); ctrl = null; }
      list.classList.add("hidden");
      input.setAttribute("aria-expanded", "false");
      input.removeAttribute("aria-activedescendant");
      active = -1;
    }
    function setActive(i) {
      active = i;
      [...list.children].forEach((li, j) => li.setAttribute("aria-selected", j === i ? "true" : "false"));
      if (i >= 0 && list.children[i]) {
        input.setAttribute("aria-activedescendant", list.children[i].id);
        list.children[i].scrollIntoView({ block: "nearest" });
      } else input.removeAttribute("aria-activedescendant");
    }
    function render(q) {
      if (!items.length) { close(); return; }
      list.innerHTML = items.map((it, i) => {
        const t = TYPES[it.type] || TYPES.producto;
        return `<li role="option" id="${list.id}-${i}" data-i="${i}" aria-selected="false" class="sg-${escHtml(it.type || "producto")}">
          <span class="sg-icon" aria-hidden="true">${t.icon}</span>
          <span class="sg-text">${highlight(it.text, q)}</span>
          ${t.label ? `<span class="sg-type">${t.label}</span>` : ""}</li>`;
      }).join("");
      list.classList.remove("hidden");
      input.setAttribute("aria-expanded", "true");
      setActive(-1);
    }
    async function fetchItems(q) {
      if (ctrl) ctrl.abort();
      ctrl = new AbortController();
      const my = ctrl;
      try {
        const res = await fetch(`/api/suggest?q=${encodeURIComponent(q)}&limit=${limit}`, { signal: my.signal });
        if (res.status === 404) { disabled = true; close(); return; }   // el backend aún no tiene autocompletado
        if (!res.ok) return;
        const data = await res.json();
        if (my !== ctrl || input.value.trim() !== q || document.activeElement !== input) return;  // respuesta vieja
        items = (Array.isArray(data.items) ? data.items : []).filter((it) => it && it.text).slice(0, limit);
        lastQuery = q;
        render(q);
      } catch (e) {
        if (e.name !== "AbortError") close();
      } finally { if (my === ctrl) ctrl = null; }
    }
    function choose(i) {
      const it = items[i];
      close();
      if (it) onSelect(it);
    }

    input.addEventListener("input", () => {
      clearTimeout(timer);
      const q = input.value.trim();
      if (disabled || q.length < minChars) { items = []; close(); return; }
      timer = setTimeout(() => fetchItems(q), delay);
    });
    input.addEventListener("keydown", (e) => {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        if (!isOpen()) { if (items.length && input.value.trim() === lastQuery) render(lastQuery); else return; }
        e.preventDefault();
        const n = items.length;
        setActive(e.key === "ArrowDown" ? (active + 1) % n : (active <= 0 ? n - 1 : active - 1));
      } else if (e.key === "Enter") {
        if (isOpen() && active >= 0) { e.preventDefault(); choose(active); }
        else close();
      } else if (e.key === "Escape") {
        if (isOpen()) { e.preventDefault(); close(); }   // evita que el input type=search se borre
      } else if (e.key === "Tab") close();
    });
    input.addEventListener("blur", () => setTimeout(() => { if (document.activeElement !== input) close(); }, 0));
    list.addEventListener("mousedown", (e) => e.preventDefault());   // mantiene el foco en el input
    list.addEventListener("click", (e) => {
      const li = e.target.closest("li[data-i]");
      if (li) choose(Number(li.dataset.i));
    });
    list.addEventListener("mousemove", (e) => {
      const li = e.target.closest("li[data-i]");
      if (li && Number(li.dataset.i) !== active) setActive(Number(li.dataset.i));
    });
    document.addEventListener("click", (e) => { if (e.target !== input && !list.contains(e.target)) close(); });
    return { close };
  }

  root.initSuggest = initSuggest;
  root.suggestHighlight = highlight;
})(window);
