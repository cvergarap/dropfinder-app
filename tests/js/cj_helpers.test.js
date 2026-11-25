// Pruebas de las funciones puras de frontend/js/cj.js. Ejecutar: node tests/js/cj_helpers.test.js
const assert = require("node:assert/strict");
const path = require("node:path");
const h = require(path.join(__dirname, "../../frontend/js/cj.js"));

let n = 0;
const t = (name, fn) => { fn(); n += 1; console.log(`ok - ${name}`); };

t("cleanApiKey: acepta una clave normal y recorta espacios", () => {
  assert.deepEqual(h.cleanApiKey("  abc@api@123xyz  "), { ok: true, value: "abc@api@123xyz" });
});

t("cleanApiKey: rechaza vacío, espacios internos, control y exceso", () => {
  for (const bad of ["", "   ", null, undefined, "a b", "a\nb", "a\tb", "ñandú", "x".repeat(201)]) {
    assert.equal(h.cleanApiKey(bad).ok, false, JSON.stringify(bad));
    assert.ok(h.cleanApiKey(bad).error);
  }
  assert.equal(h.cleanApiKey("x".repeat(200)).ok, true);
});

t("importBody: exige palabra clave o categoría y acota el tope", () => {
  assert.ok(h.importBody("", "", 10).error);
  assert.ok(h.importBody("   ", "  ", 10).error);
  assert.deepEqual(h.importBody("  lámpara   led ", "", "25").body, { query: "lámpara led", limit: 25 });
  assert.deepEqual(h.importBody("", "cat-1", 5).body, { query: "", limit: 5, category_id: "cat-1" });
  assert.equal(h.importBody("x", "", 9999).body.limit, 200);
  assert.equal(h.importBody("x", "", 0).body.limit, 1);
  assert.equal(h.importBody("x", "", -7).body.limit, 1);
  assert.equal(h.importBody("x", "", "abc").body.limit, 50);
  assert.equal(h.importBody("x", "", 500, 100).body.limit, 100);
  assert.equal(h.importBody("y".repeat(300), "", 5).body.query.length, 100);
});

t("statusInfo: estados", () => {
  assert.equal(h.statusInfo(null).tone, "off");
  assert.equal(h.statusInfo({ connected: false }).tone, "off");
  assert.equal(h.statusInfo({ connected: true, status: "active" }).tone, "ok");
  const e = h.statusInfo({ connected: true, status: "error", last_error: "CJ rechazó" });
  assert.equal(e.tone, "err");
  assert.equal(e.detail, "CJ rechazó");
  assert.match(e.badge, /Con error/);          // texto además del color
});

t("progressInfo: búsqueda, stock, guardado y fin", () => {
  assert.equal(h.progressInfo(null), null);
  const s = h.progressInfo({ state: "running", phase: "search", fetched: 12 });
  assert.equal(s.running, true);
  assert.match(s.text, /12 encontrados/);
  const k = h.progressInfo({ state: "running", phase: "stock", done: 30, total: 120 });
  assert.equal(k.percent, 25);
  assert.match(k.text, /30 de 120/);
  assert.equal(h.progressInfo({ state: "running", phase: "stock", done: 500, total: 120 }).percent, 100);
  assert.equal(h.progressInfo({ state: "running", phase: "stock", done: 0, total: 0 }).percent, 0);
  assert.equal(h.progressInfo({ state: "done", phase: "done", message: "Importación terminada" }).percent, 100);
  assert.equal(h.progressInfo({ state: "done", phase: "done" }).running, false);
});

t("resultLines: resumen y errores", () => {
  assert.deepEqual(h.resultLines({ state: "running" }), []);
  assert.deepEqual(h.resultLines({ state: "error", error: "CJ pidió esperar" }), ["CJ pidió esperar"]);
  assert.deepEqual(h.resultLines({ state: "error" }), ["La importación falló."]);
  const l = h.resultLines({ state: "done", new: 3, updated: 2, alerts: 1, invalid: 4, conflicts: 1, stock_unknown: 2, stock_failed: 5 });
  assert.equal(l.length, 6);
  assert.match(l[0], /3 nuevos, 2 actualizados/);
  assert.deepEqual(h.resultLines({ state: "done", new: 0, updated: 0 }), ["0 nuevos, 0 actualizados."]);
});

t("categoryOptions: descarta basura y acota", () => {
  assert.deepEqual(h.categoryOptions(null), []);
  assert.deepEqual(h.categoryOptions([{ id: "a", name: "A", path: "X › A" }, { id: 5, name: "B" }, null, "x", { id: "c", name: "C" }]),
    [{ id: "a", label: "X › A" }, { id: "c", label: "C" }]);
  assert.equal(h.categoryOptions([{ id: "a", name: "n".repeat(500) }])[0].label.length, 120);
  assert.equal(h.categoryOptions(Array.from({ length: 3000 }, (_, i) => ({ id: `i${i}`, name: "n" }))).length, 2000);
});

console.log(`${n} pruebas OK`);
