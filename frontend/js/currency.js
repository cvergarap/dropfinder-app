/* Moneda de visualización (USD / COP).
 * El catálogo y todas las APIs trabajan en USD; aquí solo se convierte y se formatea al mostrar.
 * Las funciones puras (roundCop, displayValue, formatAmount, toUsd) también se pueden cargar en Node para pruebas. */
(function (root) {
  "use strict";

  const DEFAULT_CURRENCIES = {
    USD: { code: "USD", symbol: "US$", decimals: 2, locale: "en-US" },
    COP: { code: "COP", symbol: "$", decimals: 0, locale: "es-CO" },
  };
  const DEFAULT_SETTINGS = {
    display_currency: "USD", rate_mode: "trm", manual_rate: null, hedge_pct: 0,
    cop_rounding: "900", vat_enabled: false, vat_pct: 19,
  };

  const round = (v, d) => { const f = 10 ** d; return Math.round(v * f) / f; };

  /* Redondeo comercial de precios de venta en COP.
   * "900": sube al siguiente valor terminado en 900 (87.350 → 87.900; 87.900 → 87.900; 87.901 → 88.900)
   * "1000": sube al millar · "100": sube a la centena · "none": al peso más cercano. */
  function roundCop(value, mode) {
    if (!Number.isFinite(value)) return value;
    // Quita el ruido de coma flotante de la multiplicación (87.899,9999999 → 87.900)
    const v = round(value, 2);
    if (v <= 0) return Math.round(v);
    switch (String(mode)) {
      case "900": return Math.ceil((v - 900) / 1000) * 1000 + 900;
      case "1000": return Math.ceil(v / 1000) * 1000;
      case "100": return Math.ceil(v / 100) * 100;
      default: return Math.round(v);
    }
  }

  /* Estado mínimo para convertir: {code, rate, rounding, currencies}. rate = unidades de `code` por 1 USD. */
  function currencyInfo(state, code) {
    const list = (state && state.currencies) || {};
    return list[code] || DEFAULT_CURRENCIES[code] || { code, symbol: code, decimals: 2, locale: "en-US" };
  }

  /* ¿Se puede mostrar en la moneda elegida? Si falta la tasa, se cae a USD. */
  function effectiveCode(state) {
    const code = (state && state.code) || "USD";
    if (code === "USD") return "USD";
    return Number(state.rate) > 0 ? code : "USD";
  }

  /* Valor numérico tal como se muestra (ya convertido y redondeado). kind: "price" | "amount". */
  function displayValue(usd, state, kind = "amount") {
    const n = Number(usd);
    if (usd === null || usd === undefined || usd === "" || !Number.isFinite(n)) return null;
    const code = effectiveCode(state);
    if (code === "USD") return round(n, 2);
    const v = n * Number(state.rate);
    if (code === "COP") return kind === "price" ? roundCop(v, state.rounding) : Math.round(v);
    return round(v, currencyInfo(state, code).decimals);
  }

  /* Formatea un valor YA expresado en la moneda `code`: "$ 89.900" · "US$ 12.99". */
  function formatAmount(value, code, state) {
    if (value === null || value === undefined || !Number.isFinite(Number(value))) return "—";
    const c = currencyInfo(state, code);
    const d = Number.isInteger(c.decimals) ? c.decimals : 2;
    const num = new Intl.NumberFormat(c.locale || "en-US", { minimumFractionDigits: d, maximumFractionDigits: d })
      .format(Math.abs(Number(value)));
    return `${Number(value) < 0 ? "-" : ""}${c.symbol}\u00a0${num}`;   // espacio no separable: "$ 89.900"
  }

  function formatMoney(usd, state, kind = "amount") {
    return formatAmount(displayValue(usd, state, kind), effectiveCode(state), state);
  }

  /* De la moneda de visualización a USD (4 decimales) para enviar a la API. */
  function toUsd(value, state) {
    const n = Number(value);
    if (value === "" || value === null || value === undefined || !Number.isFinite(n)) return null;
    const code = effectiveCode(state);
    if (code === "USD") return round(n, 4);
    return round(n / Number(state.rate), 4);
  }

  const pure = { roundCop, displayValue, formatAmount, formatMoney, toUsd, effectiveCode, DEFAULT_CURRENCIES };
  if (typeof module !== "undefined" && module.exports) { module.exports = pure; return; }

  /* ------------------------------------------------------------ estado en el navegador */
  const listeners = [];
  const Currency = {
    available: false,           // false si el backend aún no tiene /api/currency
    settings: { ...DEFAULT_SETTINGS },
    rates: { USD: 1 },
    rateInfo: null,
    currencies: { ...DEFAULT_CURRENCIES },

    /* Estado compacto que usan las funciones puras */
    state() {
      const code = this.settings.display_currency || "USD";
      return { code, rate: this.rates[code], rounding: this.settings.cop_rounding, currencies: this.currencies };
    },
    code() { return effectiveCode(this.state()); },
    isLocal() { return this.code() !== "USD"; },
    /* Unidades de la moneda mostrada por 1 USD */
    rate() { return this.code() === "USD" ? 1 : Number(this.rates[this.code()]); },
    vat() { return this.settings.vat_enabled ? Number(this.settings.vat_pct) || 0 : 0; },

    apply(data) {
      if (!data || typeof data !== "object") return;
      this.available = true;
      this.settings = { ...DEFAULT_SETTINGS, ...(data.settings || {}) };
      this.rates = { USD: 1, ...(data.rates || {}) };
      this.rateInfo = data.rate_info || null;
      if (Array.isArray(data.currencies) && data.currencies.length) {
        this.currencies = {};
        for (const c of data.currencies) if (c && c.code) this.currencies[c.code] = { ...(DEFAULT_CURRENCIES[c.code] || {}), ...c };
      }
      listeners.forEach((fn) => { try { fn(); } catch (e) { console.error(e); } });
    },
    onChange(fn) { listeners.push(fn); },

    async load() {
      try { this.apply(await api("/api/currency")); }
      catch (e) {
        // 404 = el servidor aún no tiene la API de moneda; otro error con datos previos = se conservan
        if (e.status === 404 || !this.available) {
          this.available = false;
          this.settings = { ...DEFAULT_SETTINGS };
          this.rates = { USD: 1 };
          this.rateInfo = null;
        }
        listeners.forEach((fn) => { try { fn(); } catch (err) { console.error(err); } });
      }
    },
    async save(partial) { this.apply(await api("/api/currency", { method: "PUT", body: partial })); },
    async refresh() { this.apply(await api("/api/currency/refresh", { method: "POST" })); },
  };

  /* money(usd, {kind}) — kind "price" aplica el redondeo comercial en COP; "amount" (por defecto) redondea al peso. */
  function money(usd, opts = {}) { return formatMoney(usd, Currency.state(), opts.kind || "amount"); }
  /* Valor numérico mostrado (para <input>) */
  function moneyValue(usd, opts = {}) { return displayValue(usd, Currency.state(), opts.kind || "amount"); }
  /* Formatea un número que ya está en la moneda mostrada */
  function moneyLocal(value) { return formatAmount(value, Currency.code(), Currency.state()); }
  function moneyToUsd(value) { return toUsd(value, Currency.state()); }

  Object.assign(root, { Currency, money, moneyValue, moneyLocal, moneyToUsd, roundCop });
})(typeof window !== "undefined" ? window : globalThis);
