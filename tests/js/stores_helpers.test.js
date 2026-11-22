// Pruebas de las funciones puras de frontend/js/stores.js. Ejecutar: node tests/js/stores_helpers.test.js
const assert = require("node:assert/strict");
const path = require("node:path");
const h = require(path.join(__dirname, "../../frontend/js/stores.js"));

let n = 0;
const t = (name, fn) => { fn(); n += 1; console.log(`ok - ${name}`); };

t("parseDomainInput: enlaces pegados", () => {
  assert.equal(h.parseDomainInput("  https://MiTienda.myshopify.com/admin/products?x=1 "), "mitienda.myshopify.com");
  assert.equal(h.parseDomainInput("http://user:pw@tienda.com:8080/"), "tienda.com:8080");
  assert.equal(h.parseDomainInput("//tienda.com/"), "tienda.com");
  assert.equal(h.parseDomainInput("mitienda.myshopify.com"), "mitienda.myshopify.com");
  assert.equal(h.parseDomainInput("tienda.com."), "tienda.com");
  assert.equal(h.parseDomainInput("https://admin.shopify.com/store/Mi-Tienda/products"), "mi-tienda.myshopify.com");
  assert.equal(h.parseDomainInput(""), "");
  assert.equal(h.parseDomainInput(null), "");
});

t("formatStorePrice: moneda de la tienda", () => {
  const cop = h.formatStorePrice(89900, "COP");
  assert.match(cop, /89\.900/); assert.doesNotMatch(cop, /,\d\d$/);
  assert.match(h.formatStorePrice(12.5, "USD"), /12,50|12\.50/);
  assert.equal(h.formatStorePrice(null, "COP"), "—");
  assert.equal(h.formatStorePrice("abc", "COP"), "—");
  assert.ok(h.formatStorePrice(5, "zz").length > 0);           // moneda inválida → USD
  assert.match(h.formatStorePrice(1234.5, "EUR"), /1234,50|1\.234,50|1234\.50/);
});

t("safeHttpsUrl: solo https", () => {
  assert.equal(h.safeHttpsUrl("https://a.com/p?x=1"), "https://a.com/p?x=1");
  assert.equal(h.safeHttpsUrl("http://a.com"), "");
  assert.equal(h.safeHttpsUrl("javascript:alert(1)"), "");
  assert.equal(h.safeHttpsUrl("data:text/html,x"), "");
  assert.equal(h.safeHttpsUrl(""), "");
  assert.equal(h.safeHttpsUrl(null), "");
});

t("splitHelp: pasos numerados y párrafos, sin HTML", () => {
  const b = h.splitHelp("Antes de empezar:\n1. Abre <b>Ajustes</b>\n2) Crea la app\n\n- Copia el token\nListo");
  assert.deepEqual(b.map((x) => x.type), ["p", "ol", "p"]);
  assert.deepEqual(b[1].items, ["Abre <b>Ajustes</b>", "Crea la app", "Copia el token"]);   // el escape se hace al dibujar
  assert.deepEqual(h.splitHelp(""), []);
});

t("planCredentialFields: obligatorios, opciones por grupo y opcionales sueltos", () => {
  const shopify = [
    { key: "domain_note", label: "X", required: true },
    { key: "access_token", label: "Token de acceso", secret: true, required: false, group: "token" },
    { key: "client_id", label: "ID de la app", required: false, group: "app" },
    { key: "client_secret", label: "Secreto de la app", secret: true, required: false, group: "app" },
  ];
  const p = h.planCredentialFields(shopify);
  assert.equal(p.required.length, 1);
  assert.deepEqual(p.modes.map((m) => [m.id, m.title, m.fields.length]),
    [["token", "Token de acceso", 1], ["app", "ID y secreto de la app", 2]]);
  const flat = h.planCredentialFields([{ key: "a", label: "A", required: false }, { key: "b", label: "B", required: false }]);
  assert.equal(flat.modes.length, 0); assert.equal(flat.flat.length, 2);
  const one = h.planCredentialFields([{ key: "a", label: "A", required: false, group: "x" }]);
  assert.equal(one.modes.length, 0); assert.equal(one.flat.length, 1);
  assert.deepEqual(h.planCredentialFields(undefined), { required: [], modes: [], flat: [] });
  const unk = h.planCredentialFields([{ key: "a", label: "Uno", required: false, group: "g1" }, { key: "b", label: "Dos", required: false, group: "g2" }]);
  assert.equal(unk.modes[0].title, "Uno");
});

t("queueBadge y listingActions", () => {
  assert.equal(h.queueBadge({ pending: 2, running: 5, failed: 1 }), 3);
  assert.equal(h.queueBadge(null), 0);
  assert.deepEqual(h.listingActions({ status: "published" }), ["sync", "hide", "remove"]);
  assert.deepEqual(h.listingActions({ status: "hidden" }), ["sync", "show", "remove"]);
  assert.deepEqual(h.listingActions({ status: "error" }), ["retry", "remove"]);
  assert.deepEqual(h.listingActions({ status: "published", pending_action: "sync" }), []);
});

t("statusLabel, skipReasonText y fechas", () => {
  assert.equal(h.statusLabel({ status: "published" }).label, "Publicado");
  assert.equal(h.statusLabel({ status: "pending", pending_action: "publish" }).pending, "Publicando…");
  assert.equal(h.skipReasonText("already_published"), "Ya publicado");
  assert.equal(h.skipReasonText("demo_not_allowed"), "Producto de demostración");
  assert.equal(h.skipReasonText("currency_unsupported"), "Moneda de la tienda no soportada todavía");
  assert.equal(h.skipReasonText("zzz"), "No se pudo publicar");
  assert.equal(h.fmtDateTime(""), "");
  assert.equal(h.fmtDateTime("no es fecha"), "no es fecha");
  assert.ok(h.fmtDateTime("2026-11-22T10:00:00Z").length > 5);
  assert.equal(h.MAX_BULK, 50);
});

console.log(`${n} pruebas OK`);
