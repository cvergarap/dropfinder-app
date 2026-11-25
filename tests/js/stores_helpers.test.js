// Pruebas de las funciones puras de frontend/js/stores.js. Ejecutar: node tests/js/stores_helpers.test.js
const assert = require("node:assert/strict");
const path = require("node:path");
const h = require(path.join(__dirname, "../../frontend/js/stores.js"));

let n = 0;
const t = (name, fn) => { fn(); n += 1; console.log(`ok - ${name}`); };

t("parseDomainInput: Tiendanube conserva el ID dentro del enlace pegado", () => {
  assert.equal(h.parseDomainInput("1234567", "tiendanube"), "1234567");
  assert.equal(h.parseDomainInput("  https://www.tiendanube.com/admin/v2/stores/1234567/products ", "tiendanube"),
    "https://www.tiendanube.com/admin/v2/stores/1234567/products");
  assert.equal(h.parseDomainInput("x".repeat(500), "tiendanube").length, 300);
  assert.equal(h.parseDomainInput("https://MiTienda.com/wp-admin", "woocommerce"), "mitienda.com");   // otras: solo el host
  assert.equal(h.parseDomainInput("https://MiTienda.com/x"), "mitienda.com");
});

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
  assert.deepEqual(h.listingActions({ status: "error", imported_id: 3 }), ["retry", "remove"]);
  assert.deepEqual(h.listingActions({ status: "removed", imported_id: 3 }), ["retry", "remove"]);       // borrado a mano: republicar
  for (const status of ["published", "hidden", "error", "removed", "pending"]) {
    assert.deepEqual(h.listingActions({ status, imported_id: null }), ["remove"], status);               // huérfano: solo Quitar
  }
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

t("borrador vs oculto: textos", () => {
  assert.equal(h.statusLabel({ status: "hidden" }).label, "Oculto · no visible para tus clientes");
  assert.match(h.statusLabel({ status: "hidden" }).help, /borrador/i);
  assert.equal(h.statusLabel({ status: "hidden" }, true).label, "Borrador en tienda");
  assert.equal(h.statusLabel({ status: "hidden", hidden_by_rule: true }).label, "Oculto automáticamente (sin stock)");
  assert.equal(h.statusLabel({ status: "hidden", hidden_by_rule: true }, true).label, "Oculto automáticamente (sin stock)");
});

t("skipText: mensaje del servidor primero, mapa local de respaldo", () => {
  assert.equal(h.skipText({ reason: "already_published", message: "Ya existe; usa Reintentar" }), "Ya existe; usa Reintentar");
  assert.equal(h.skipText({ reason: "already_published" }), "Ya publicado");
  assert.equal(h.skipText({ reason: "x", message: "  " }), "No se pudo publicar");
});

t("grupos de credenciales con los ids reales del conector de Shopify", () => {
  const fields = [
    { key: "access_token", label: "Token de acceso de la API de administración", secret: true, required: false, group: "token" },
    { key: "client_id", label: "ID de cliente (Client ID)", secret: false, required: false, group: "app" },
    { key: "client_secret", label: "Secreto de cliente (Client secret)", secret: true, required: false, group: "app" },
  ];
  const p = h.planCredentialFields(fields);
  assert.deepEqual(p.modes.map((m) => [m.id, m.title, m.fields.map((f) => f.key)]),
    [["token", "Token de acceso", ["access_token"]], ["app", "ID y secreto de la app", ["client_id", "client_secret"]]]);
  assert.equal(p.flat.length, 0);
  // ids de grupo desconocidos (o raros): título con las etiquetas, sin romper
  const u = h.planCredentialFields([{ key: "a", label: "Uno", required: false, group: "<b>x</b>" }, { key: "b", label: "Dos", required: false, group: 7 }]);
  assert.equal(u.modes.length, 2); assert.equal(u.modes[0].title, "Uno"); assert.equal(u.modes[1].id, "7");
});

t("credId sanea claves; publishRisks", () => {
  assert.equal(h.credId("stc", 'a" onfocus="x'), "stc-a__onfocus__x");
  assert.equal(h.credId("stc", "client_id"), "stc-client_id");
  assert.deepEqual(h.publishRisks({ margin: 30, profit: 5, stock: 10 }), []);
  assert.equal(h.publishRisks({ margin: -5, profit: -1, stock: 10 }).length, 1);
  assert.equal(h.publishRisks({ margin: 10, stock: 0 }).length, 1);
  assert.equal(h.publishRisks({ margin: -1, stock: 0 }).length, 2);
  assert.deepEqual(h.publishRisks({}), []);
});

console.log(`${n} pruebas OK`);
