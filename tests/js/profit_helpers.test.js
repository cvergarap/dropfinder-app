// Pruebas de las funciones puras de frontend/js/profit.js. Ejecutar: node tests/js/profit_helpers.test.js
const assert = require("node:assert/strict");
const path = require("node:path");
const h = require(path.join(__dirname, "../../frontend/js/profit.js"));

let n = 0;
const t = (name, fn) => { fn(); n += 1; console.log(`ok - ${name}`); };

t("parseNumber: formatos colombianos y de EE. UU.", () => {
  assert.equal(h.parseNumber("12.500", 0), 12500);          // COP: punto de miles
  assert.equal(h.parseNumber("12.500", 2), 12.5);           // USD: punto decimal
  assert.equal(h.parseNumber("12,5", 0), 12.5);
  assert.equal(h.parseNumber("1.234,56"), 1234.56);
  assert.equal(h.parseNumber("1,234.56"), 1234.56);
  assert.equal(h.parseNumber("$ 89.900", 0), 89900);
  assert.equal(h.parseNumber("USD 12.99"), 12.99);
  assert.equal(h.parseNumber("1.234.567", 0), 1234567);
  assert.equal(h.parseNumber(".5"), 0.5);
  assert.equal(h.parseNumber("-5"), -5);
  assert.equal(h.parseNumber("  7  "), 7);
});

t("parseNumber: vacío es null y lo inválido es NaN", () => {
  assert.equal(h.parseNumber(""), null);
  assert.equal(h.parseNumber("   "), null);
  assert.equal(h.parseNumber(null), null);
  assert.equal(h.parseNumber(undefined), null);
  for (const bad of ["abc", "1e5", "5.", "1,,2", "1.2.3", "--1", "12a", "Infinity", "NaN", "1.234,5,6"]) {
    assert.ok(Number.isNaN(h.parseNumber(bad, 0)), bad);
  }
});

t("complementPct liga devoluciones y efectividad", () => {
  assert.equal(h.complementPct("25"), "75");
  assert.equal(h.complementPct("33,3"), "66.7");
  assert.equal(h.complementPct("0"), "100");
  assert.equal(h.complementPct("100"), "0");
  assert.equal(h.complementPct("101"), "");
  assert.equal(h.complementPct("-1"), "");
  assert.equal(h.complementPct(""), "");
  assert.equal(h.complementPct("x"), "");
});

t("buildPayload: omite vacíos y envía números", () => {
  const { payload, errors, missing } = h.buildPayload(
    { price: "89.900", product_cost: "30000", delivery_freight: "", return_rate_pct: "25", delivery_rate_pct: "75" },
    { currency: "COP", mode: "cpa", ad: "10.000" });
  assert.deepEqual(errors, {});
  assert.deepEqual(missing, []);
  assert.deepEqual(payload, { currency: "COP", price: 89900, product_cost: 30000, return_rate_pct: 25, cpa: 10000 });
  assert.ok(!("delivery_rate_pct" in payload));              // la efectividad no se envía
  assert.ok(!("delivery_freight" in payload));
});

t("buildPayload: obligatorios vacíos son 'missing', no error", () => {
  const r = h.buildPayload({ price: "", product_cost: "5" }, { currency: "USD" });
  assert.deepEqual(r.missing, ["price"]);
  assert.deepEqual(r.errors, {});
});

t("buildPayload: errores de validación", () => {
  const r = h.buildPayload({ price: "abc", product_cost: "-3", return_rate_pct: "101", packaging: "1,,2" }, { currency: "COP" });
  assert.deepEqual(Object.keys(r.errors).sort(), ["packaging", "price", "product_cost", "return_rate_pct"]);
  assert.match(r.errors.product_cost, /negativo/);
  assert.match(r.errors.return_rate_pct, /entre 0 y 100/);
});

t("buildPayload: ROAS objetivo y su rango", () => {
  const ok = h.buildPayload({ price: "1", product_cost: "1" }, { currency: "USD", mode: "roas", ad: "3,5" });
  assert.equal(ok.payload.target_roas, 3.5);
  assert.ok(!("cpa" in ok.payload));
  for (const bad of ["0", "100.5", "x", "-1"]) {
    const r = h.buildPayload({ price: "1", product_cost: "1" }, { currency: "USD", mode: "roas", ad: bad });
    assert.ok(r.errors.target_roas, bad);
  }
  assert.ok(h.buildPayload({ price: "1", product_cost: "1" }, { currency: "COP", mode: "cpa", ad: "-5" }).errors.cpa);
});

t("formatPct y formatRoas", () => {
  assert.match(h.formatPct(18.8), /18,8\s%/);
  assert.equal(h.formatPct(null), "—");
  assert.equal(h.formatPct(NaN), "—");
  assert.match(h.formatRoas(3.97), /3,97×/);
  assert.equal(h.formatRoas(null), "No alcanzable");
});

const fmt = (v) => `$${v}`;
const base = {
  profit_per_confirmed: 12652, profit_per_delivered: 16870, margin_pct: 18.8, breakeven_roas: 3.97, max_cpa: 22652,
  profit_per_100: 1265225, delivered_per_100: 75,
  breakdown: { revenue: 67425, product_cost: 22500, supplier_shipping: 0, packaging: 1500, delivery_freight: 15000,
    return_freight: 3750, returned_product_loss: 0, cod_fee: 2023, ads: 10000, profit: 12652 },
};

t("verdict: tres tonos y estado vacío", () => {
  assert.equal(h.verdict(base).tone, "good");
  assert.equal(h.verdict({ ...base, profit_per_confirmed: -1 }).tone, "bad");
  assert.equal(h.verdict({ ...base, margin_pct: 5 }).tone, "warn");
  assert.equal(h.verdict({ ...base, margin_pct: null }).tone, "warn");
  assert.equal(h.verdict({ ...base, profit_per_confirmed: 0, margin_pct: 0 }).tone, "warn");
  assert.equal(h.verdict(null).tone, "muted");
  assert.ok(h.verdict({ ...base, profit_per_confirmed: -1 }).label.length > 5);       // siempre hay texto, no solo color
});

t("summaryRows: valores y 'sin dato'", () => {
  const rows = Object.fromEntries(h.summaryRows(base, fmt).map((r) => [r.key, r]));
  assert.equal(rows.profit_per_delivered.value, "$16870");
  assert.equal(rows.max_cpa.value, "$22652");
  assert.equal(rows.profit_per_100.tone, "good");
  const none = Object.fromEntries(h.summaryRows({ ...base, profit_per_delivered: null, breakeven_roas: null, margin_pct: null,
    profit_per_confirmed: -5 }, fmt).map((r) => [r.key, r]));
  assert.equal(none.profit_per_delivered.value, "—");
  assert.equal(none.breakeven_roas.value, "No alcanzable");
  assert.equal(none.margin_pct.value, "—");
  assert.equal(none.profit_per_confirmed.tone, "bad");
});

t("breakdownRows: oculta costos en cero y marca el total", () => {
  const rows = h.breakdownRows(base, fmt);
  const keys = rows.map((r) => r.key);
  assert.ok(!keys.includes("supplier_shipping") && !keys.includes("returned_product_loss"));
  assert.equal(keys[0], "revenue");
  assert.equal(rows.find((r) => r.key === "product_cost").value, "$-22500");    // los costos van con signo negativo
  assert.equal(rows.at(-1).key, "profit");
  assert.ok(rows.at(-1).total);
});

t("announce: una frase con ganancia y margen", () => {
  const s = h.announce(base, fmt);
  assert.match(s, /\$12652/);
  assert.match(s, /18,8/);
  assert.equal(h.announce(null, fmt), h.verdict(null).label);
});

t("debounce agrupa llamadas y se puede cancelar", () => {
  const q = new Map();
  let id = 0;
  const timers = { set: (f) => { q.set(++id, f); return id; }, clear: (i) => { q.delete(i); } };
  const flush = () => { const fs = [...q.values()]; q.clear(); fs.forEach((f) => f()); };
  const calls = [];
  const d = h.debounce((x) => calls.push(x), 10, timers);
  d(1); d(2); d(3);
  flush();
  assert.deepEqual(calls, [3]);
  d(4); d.cancel();
  flush();
  assert.deepEqual(calls, [3]);
});

t("los campos del formulario coinciden con la API", () => {
  const keys = h.ALL_FIELDS.map((f) => f.key);
  for (const k of ["price", "product_cost", "supplier_shipping", "delivery_freight", "return_freight", "packaging",
    "return_rate_pct", "delivery_rate_pct", "cod_fee_pct", "cod_fee_fixed", "return_product_loss_pct"]) assert.ok(keys.includes(k), k);
  assert.ok(h.DEFAULT_FIELDS.every((k) => keys.includes(k)));
  assert.equal(new Set(keys).size, keys.length);
});

console.log(`${n} pruebas OK`);
