const { chromium } = require('playwright');
const fs = require('fs');
const BASE = 'http://127.0.0.1:8891/', CTL = 'http://127.0.0.1:8892';
const D = (process.env.E2E_DIR || (__dirname + '/work')) + '/e2e/';
fs.mkdirSync(D, { recursive: true });
const LOGF = [D + 'launch.log', (process.env.E2E_DIR || (__dirname + '/work')) + '/launch.log'].find(f => fs.existsSync(f));   // log del servidor (launch.py > ...)
fs.writeFileSync(D + 'prov.csv', 'sku;titulo;costo;categoria;stock;pedidos;url\nE-1;Taza térmica acero;4,00;Hogar;50;300;https://prov.example/E-1\nE-2;Botella deportiva;3,50;Fitness;50;200;https://prov.example/E-2\nE-3;Lámpara escritorio LED;8,90;Hogar;50;120;https://prov.example/E-3\n');
const results = []; const problems = []; let plat = 'Shopify';
const ok = (name, cond, extra = '') => { results.push([cond, name, extra, plat]); console.log((cond ? 'OK   ' : 'FAIL ') + name + (extra ? '  ' + extra : '')); if (!cond) problems.push(name); };
const state = async () => (await fetch(CTL + '/state')).json();
const until = async (fn, ms = 20000, step = 250) => { const t = Date.now(); for (;;) { try { const v = await fn(); if (v) return v; } catch {} if (Date.now() - t > ms) return null; await new Promise(r => setTimeout(r, step)); } };
const products = async () => Object.values((await state()).products);

(async () => {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await ctx.newPage(); globalThis.__page = page;
  const consoleErrs = [];
  page.on('console', m => { if (['error'].includes(m.type()) && !/Failed to load resource/.test(m.text())) consoleErrs.push(m.text()); });
  page.on('pageerror', e => consoleErrs.push('pageerror: ' + e.message));
  const toastText = async () => (await page.textContent('#toast').catch(() => '')) || '';

  // 1) Registro del propietario → Panel
  await page.goto(BASE); await page.waitForSelector('#authForm');
  await page.fill('[name=name]', 'Dueña'); await page.fill('[name=email]', 'duena@ejemplo.com');
  await page.fill('[name=password]', 'clave-segura-123'); await page.fill('[name=password2]', 'clave-segura-123');
  await page.click('#authForm button[type=submit]'); await page.waitForSelector('#view-dashboard.active');
  ok('registro del propietario y arranque en el Panel', true);

  // 2) Catálogo de un proveedor (CSV) e importar 3 productos
  await page.click('.tab[data-view=suppliers]');
  await page.fill('#csvForm [name=supplier]', 'Mayorista E2E');
  await page.setInputFiles('#csvForm [name=file]', D + 'prov.csv'); await page.click('#csvForm button[type=submit]');
  await page.waitForSelector('#csvResult .result.ok');
  await page.click('.tab[data-view=search]');
  for (const q of ['taza termica', 'botella deportiva', 'lampara escritorio']) {
    await page.fill('#q', q); await page.press('#q', 'Enter'); await page.waitForTimeout(700);
    await page.waitForSelector('#grid .card .import', { timeout: 8000 });
    await page.locator('#grid .card .import').first().click(); await page.waitForTimeout(500);
  }
  await page.click('.tab[data-view=imports]'); await page.waitForSelector('#importsBody tr');
  ok('3 productos importados', (await page.locator('#importsBody tr').count()) === 3);

  const KEYS = { Taza: 'E-1', Botella: 'E-2', Lámpara: 'E-3' };
  const firstRow = (await page.locator('#importsBody tr').first().textContent());
  const T1 = Object.keys(KEYS).find(k => firstRow.includes(k)); const T2 = Object.keys(KEYS).find(k => k !== T1);
  const mkCsv = (outName, stockOf) => fs.writeFileSync(D + outName, 'sku;titulo;costo;categoria;stock;pedidos;url\n' +
    [['E-1','Taza térmica acero','4,00','Hogar',300],['E-2','Botella deportiva','3,50','Fitness',200],['E-3','Lámpara escritorio LED','8,90','Hogar',120]]
      .map(([sku,t,c,cat,o]) => `${sku};${t};${c};${cat};${sku === KEYS[T1] ? stockOf : 50};${o};https://prov.example/${sku}`).join('\n') + '\n');
  mkCsv('prov_stockout.csv', 0); mkCsv('prov_back.csv', 40);
  const find1 = async () => (await products()).find(p => p.title.includes(T1));
  const find2 = async () => (await products()).find(p => p.title.includes(T2));
  console.log('   producto objetivo:', T1, '| segundo:', T2);

  // 3) Sin tienda conectada, "Publicar en tienda" explica y lleva a Tiendas
  const noStoreOff = await page.locator('#importsBody .st-publish').first().getAttribute('aria-disabled');
  ok('sin tienda: el botón de publicar está deshabilitado con explicación', noStoreOff === 'true');

  // 4) Conectar Shopify desde el asistente (se pega la URL completa, en mayúsculas)
  await page.click('.tab[data-view=stores]'); await page.click('#stConnect'); await page.waitForSelector('#stcDomain');
  const modes = await page.locator('.st-mode-head').allTextContents();
  ok('el asistente muestra las dos formas de credenciales de Shopify', modes.length === 2 && /Token/.test(modes[0]) && /secreto/.test(modes[1]), JSON.stringify(modes));
  await page.fill('#stcDomain', 'https://MITIENDA.myshopify.com/admin');
  await page.locator('input[data-cred=access_token]').fill('shpat_valido_123');
  await page.click('#stcGo');
  const connected = await until(async () => /Conectada/.test(await page.textContent('#stBody')), 15000);
  ok('Shopify conectada (el servidor normalizó la URL pegada)', !!connected);
  const inputsEmpty = await page.locator('input[type=password]').evaluateAll(els => els.every(e => e.value === ''));
  ok('las credenciales no quedan en los campos tras conectar', inputsEmpty);

  // 5) Publicar desde Mis productos (como borrador por defecto)
  await page.click('.tab[data-view=imports]'); await page.waitForSelector('#importsBody .st-publish');
  await page.locator('#importsBody .st-publish').first().click(); await page.waitForSelector('#stpGo');
  ok('el diálogo de publicar abre con "borrador" marcado', await page.isChecked('#stpDraft'));
  if (await page.locator('#stpRisk').count()) await page.check('#stpRisk');
  await page.click('#stpGo');
  const draft = await until(async () => (await find1()), 20000);
  ok('el producto llegó a la tienda', !!draft);
  ok('se creó como BORRADOR (no visible)', draft && draft.status === 'DRAFT', draft && draft.status);
  ok('lleva la etiqueta idempotente df-sku', draft && (draft.tags || []).some(t => /^df-sku:/.test(t)), JSON.stringify(draft && draft.tags));
  const st1 = await state();
  const v = Object.values(st1.variants)[0];
  ok('precio enviado como texto con 2 decimales', v && /^\d+\.\d{2}$/.test(String(v.price)), JSON.stringify(v && v.price));
  ok('el SKU DF-<id> viaja en la etiqueta y el stock llegó a la ubicación', draft && (draft.tags || []).some(t => /^df-sku:DF-\d+$/.test(t)) && Object.values(st1.levels).some(q => q === 50), JSON.stringify(st1.levels));
  await page.keyboard.press('Escape');

  // 6) Productos publicados: aparece "Oculto", y "Mostrar" pide confirmación antes de hacerlo visible
  await page.click('.tab[data-view=stores]'); await page.waitForSelector('[data-st=listings]'); await page.click('[data-st=listings]');
  await page.waitForTimeout(1500); fs.writeFileSync(D + 'dbg.txt', (await page.locator('#view-stores').innerText()) + '\n---\n' + (await page.locator('#view-stores').innerHTML()).slice(0, 3000)); await page.screenshot({ path: D + 'dbg.png' });
  console.log('   paso 6: esperando la fila'); await page.getByRole('row', { name: new RegExp(T1) }).first().waitFor({ timeout: 15000 });
  const rowText = await page.getByRole('row', { name: new RegExp(T1) }).first().textContent();
  ok('el borrador se lee como "Oculto · no visible para tus clientes"', /no visible para tus clientes/.test(rowText), rowText.replace(/\s+/g, ' ').slice(0, 90));
  await page.getByRole('row', { name: new RegExp(T1) }).locator('[data-act=show]').click();
  await page.waitForTimeout(1200); fs.writeFileSync(D + 'dbg2.txt', await page.evaluate(() => [...document.querySelectorAll('dialog')].map(d => (d.open ? 'ABIERTO: ' : 'cerrado: ') + d.innerText).join('\n=====\n'))); await page.screenshot({ path: D + 'dbg2.png' });
  await page.waitForSelector('text=Esto hará visible el producto a tus clientes ahora');
  const stillDraft = (await find1()).status === 'DRAFT';
  ok('"Mostrar" pide confirmación y no cambia nada antes de confirmar', stillDraft);
  await page.getByRole('button', { name: 'Mostrar', exact: true }).last().click();
  const active = await until(async () => (await find1()).status === 'ACTIVE', 20000);
  ok('tras confirmar, el producto pasa a ACTIVO en la tienda', !!active);

  // 7) Sin stock: la regla lo oculta sola (vuelve a borrador) y se ve en la interfaz
  await page.click('.tab[data-view=suppliers]');
  await page.fill('#csvForm [name=supplier]', 'Mayorista E2E');
  await page.setInputFiles('#csvForm [name=file]', D + 'prov_stockout.csv');
  await page.check('#csvForm [name=replace]').catch(() => {}); await page.click('#csvForm button[type=submit]');
  await page.waitForSelector('#csvResult .result.ok');
  const hidden = await until(async () => (await find1()).status === 'DRAFT', 40000, 500);
  ok('al agotarse en el proveedor, la regla oculta el producto en la tienda', !!hidden);
  const stock0 = Object.values((await state()).levels).some(l => Object.values(l).some ? true : true);
  await page.click('.tab[data-view=stores]'); await page.click('[data-st=listings]'); console.log('   paso 6: esperando la fila'); await page.getByRole('row', { name: new RegExp(T1) }).first().waitFor({ timeout: 15000 });
  const t2 = await until(async () => /Oculto automáticamente/.test(await page.getByRole('row', { name: new RegExp(T1) }).first().textContent()), 15000);
  ok('la interfaz explica "Oculto automáticamente (sin stock)"', !!t2);

  // 8) Regresa el stock: la regla lo vuelve a mostrar SOLO porque lo ocultó ella
  await page.click('.tab[data-view=suppliers]');
  await page.fill('#csvForm [name=supplier]', 'Mayorista E2E');
  await page.setInputFiles('#csvForm [name=file]', D + 'prov_back.csv'); await page.click('#csvForm button[type=submit]');
  await page.waitForSelector('#csvResult .result.ok');
  const shown = await until(async () => (await find1()).status === 'ACTIVE', 40000, 500);
  ok('al volver el stock, se muestra de nuevo (solo lo que ocultó la regla)', !!shown);

  // 9) Otro producto publicado como borrador y ocultado a mano por el usuario: NO se muestra solo al volver el stock
  // (se cubre en pruebas unitarias; aquí comprobamos que el borrador de la botella sigue sin mostrarse)
  await page.click('.tab[data-view=imports]'); await page.waitForSelector('#importsBody .st-publish');
  await page.locator('#importsBody tr').filter({ hasText: T2 }).locator('.st-publish').click(); await page.waitForSelector('#stpGo');
  if (await page.locator('#stpRisk').count()) await page.check('#stpRisk');
  await page.click('#stpGo');
  const botella = await until(async () => (await find2()), 20000);
  ok('segundo producto publicado como borrador', botella && botella.status === 'DRAFT');
  await page.keyboard.press('Escape');

  // 10) Credenciales revocadas: la conexión pasa a error y se explica
  await fetch(CTL + '/revoke', { method: 'POST' });
  await page.click('.tab[data-view=stores]'); await page.waitForSelector('[data-st=validate]');
  await page.locator('[data-st=validate]').first().click();
  const errShown = await until(async () => /Con error/.test(await page.textContent('#stBody')), 15000);
  ok('con credenciales revocadas la tienda queda "Con error" en la interfaz', !!errShown);
  const bodyHtml = await page.content();
  ok('el token no aparece en ninguna parte de la página', !/shpat_valido_123/.test(bodyHtml));
  const ls = await page.evaluate(() => JSON.stringify({ ...localStorage }) + JSON.stringify({ ...sessionStorage }));
  ok('el token no está en localStorage ni sessionStorage', !/shpat_valido_123/.test(ls));
  await fetch(CTL + '/unrevoke', { method: 'POST' });


  // ===================== TIENDANUBE y WOOCOMMERCE (misma batería; ver README de esta carpeta) =====================
  const WC_KEY = 'ck_' + 'a1'.repeat(20), WC_SECRET = 'cs_' + 'b2'.repeat(20);
  const PLATFORMS = [
    { plat: 'Tiendanube', value: 'tiendanube', ctl: '/tn', name: 'Tienda TN E2E', domain: '1234567', labelRe: /ID de la tienda/, hintRe: /número de tu tienda/,
      fill: async () => { await page.locator('input[data-cred=access_token]').fill('tn_token_valido_123'); await page.locator('input[data-cred=contact_email]').fill('contacto@ejemplo.com'); },
      secrets: ['tn_token_valido_123'] },
    { plat: 'WooCommerce', value: 'woocommerce', ctl: '/wc', name: 'Tienda WOO E2E', domain: 'https://WWW.mitienda.com/', labelRe: /Dirección de la tienda/, hintRe: /EXACTO/,
      fill: async () => { await page.locator('input[data-cred=consumer_key]').fill(WC_KEY); await page.locator('input[data-cred=consumer_secret]').fill(WC_SECRET); },
      secrets: [WC_KEY, WC_SECRET, Buffer.from(WC_KEY + ':' + WC_SECRET).toString('base64')] },
  ];
  const importsApi = () => page.evaluate(() => fetch('/api/imports').then(r => r.json()));
  const storesApi = () => page.evaluate(() => fetch('/api/stores').then(r => r.json()).then(d => d.items));
  const listingsApi = (id) => page.evaluate((i) => fetch(`/api/stores/${i}/listings?page_size=50`).then(r => r.json()), id);
  const csvUpload = async (file) => {
    await page.click('.tab[data-view=suppliers]'); await page.fill('#csvForm [name=supplier]', 'Mayorista E2E');
    await page.setInputFiles('#csvForm [name=file]', D + file); await page.click('#csvForm button[type=submit]');
    await page.waitForSelector('#csvResult .result.ok');
  };
  mkCsv('prov_s25.csv', 25);
  const T3 = Object.keys(KEYS).find(k => k !== T1 && k !== T2);

  for (const P of PLATFORMS) {
    plat = P.plat; console.log('\n   === ' + P.plat + ' ===');
    const pstate = async () => (await (await fetch(CTL + P.ctl + '/state')).json()).products;
    const bySku = async (sku) => (await pstate()).filter(p => p.sku === sku);
    const imps = await importsApi();
    const skuOf = (t) => 'DF-' + imps.find(i => i.title.includes(t)).product_id;
    const sku1 = skuOf(T1), sku3 = skuOf(T3);
    const one = async (sku) => (await bySku(sku))[0];

    // a) conectar desde el asistente con el selector de plataforma
    await page.click('.tab[data-view=stores]'); await page.waitForSelector('[data-st=connect], #stConnect');
    await page.click('#stConnect'); await page.waitForSelector('#stcDomain');
    const radios = await page.locator('input[name=platform]').evaluateAll(els => els.map(e => e.value));
    ok(`el asistente ofrece el selector con las 3 plataformas`, ['shopify', 'tiendanube', 'woocommerce'].every(v => radios.includes(v)), JSON.stringify(radios));
    await page.check(`input[name=platform][value=${P.value}]`);
    await page.waitForFunction((v) => document.querySelector('#stcDomain') && !/myshopify/.test(document.querySelector('#stcDomain').placeholder), null);
    const label = await page.textContent('label[for=stcDomain]'), hint = await page.textContent('#stcDomainHint');
    ok(`${P.plat}: la etiqueta y la ayuda del campo de dirección son las de la plataforma`, P.labelRe.test(label) && P.hintRe.test(hint), label.trim() + ' | ' + hint.trim().slice(0, 60));
    await page.fill('#stcDomain', P.domain); await page.fill('#stcName', P.name);
    await P.fill(); await page.click('#stcGo');
    const conn = await until(async () => (await storesApi()).find(x => x.name === P.name && x.status === 'active'), 20000);
    ok(`${P.plat} conectada desde el asistente (queda "Conectada")`, !!conn && /Conectada/.test(await page.locator(`[data-store="${conn.id}"]`).textContent()));
    ok(`${P.plat}: la conexión se guarda con su plataforma`, conn && conn.platform === P.value, conn && conn.platform);
    ok(`${P.plat}: las credenciales no quedan en los campos tras conectar`, await page.locator('input[type=password]').evaluateAll(els => els.every(e => e.value === '')));
    const storeId = conn && conn.id;

    // b) publicar como borrador desde Mis productos eligiendo la tienda
    const publishVia = async (title) => {
      await page.click('.tab[data-view=imports]'); await page.waitForSelector('#importsBody .st-publish');
      await page.locator('#importsBody tr').filter({ hasText: title }).locator('.st-publish').click(); await page.waitForSelector('#stpGo');
      await page.selectOption('#stpStore', { label: (await page.locator('#stpStore option').allTextContents()).find(t => t.startsWith(P.name)) });
      const draftChecked = await page.isChecked('#stpDraft');
      if (await page.locator('#stpRisk').count()) await page.check('#stpRisk');
      await page.click('#stpGo');
      await page.getByRole('button', { name: 'Listo', exact: true }).click({ timeout: 20000 });   // resultado del diálogo (cola) y cierre
      return draftChecked;
    };
    const draftChecked = await publishVia(T1);
    ok(`${P.plat}: el diálogo de publicar abre con "borrador" marcado`, draftChecked);
    const p1 = await until(() => one(sku1), 25000);
    ok(`${P.plat}: el producto llegó a la tienda simulada`, !!p1);
    ok(`${P.plat}: se creó como BORRADOR (no visible)`, p1 && p1.status === 'DRAFT', p1 && p1.status);
    ok(`${P.plat}: lleva las etiquetas de propiedad dropfinder y dfi-…`, p1 && p1.tags.includes('dropfinder') && p1.tags.some(t => /^dfi-[0-9a-f]+$/.test(t)), JSON.stringify(p1 && p1.tags));
    ok(`${P.plat}: SKU DF-<id>, precio con 2 decimales y stock = el del proveedor (40)`, p1 && /^DF-\d+$/.test(p1.sku) && /^\d+\.\d{2}$/.test(String(p1.price)) && p1.stock === 40, JSON.stringify(p1 && [p1.sku, p1.price, p1.stock]));

    // c) Mostrar pide confirmación
    const openListings = async () => {
      await page.click('.tab[data-view=stores]'); await page.waitForSelector(`[data-st=listings][data-id="${storeId}"]`);
      await page.click(`[data-st=listings][data-id="${storeId}"]`);
    };
    const row = (t) => page.getByRole('row', { name: new RegExp(t) }).first();
    await openListings(); await row(T1).waitFor({ timeout: 15000 });
    ok(`${P.plat}: el borrador se lee como "Oculto · no visible para tus clientes"`, /no visible para tus clientes/.test(await row(T1).textContent()));
    await row(T1).locator('[data-act=show]').click();
    await page.waitForSelector('text=Esto hará visible el producto a tus clientes ahora');
    ok(`${P.plat}: "Mostrar" pide confirmación y no cambia nada antes de confirmar`, (await one(sku1)).status === 'DRAFT');
    await page.getByRole('button', { name: 'Mostrar', exact: true }).last().click();
    ok(`${P.plat}: tras confirmar, el producto pasa a visible en la tienda`, !!(await until(async () => (await one(sku1)).status === 'ACTIVE', 20000)));

    // d) stock sincronizado y regla de sin stock
    await csvUpload('prov_s25.csv');
    ok(`${P.plat}: el cambio de stock del proveedor llega a la tienda (25)`, !!(await until(async () => (await one(sku1)).stock === 25, 40000, 500)));
    await csvUpload('prov_stockout.csv');
    ok(`${P.plat}: al agotarse en el proveedor, la regla oculta el producto y deja stock 0`, !!(await until(async () => { const p = await one(sku1); return p.status === 'DRAFT' && p.stock === 0; }, 40000, 500)));
    await openListings();
    ok(`${P.plat}: la interfaz explica "Oculto automáticamente (sin stock)"`, !!(await until(async () => /Oculto automáticamente/.test(await row(T1).textContent()), 15000)));
    await csvUpload('prov_back.csv');
    ok(`${P.plat}: al volver el stock se muestra de nuevo (lo ocultó la regla) con stock 40`, !!(await until(async () => { const p = await one(sku1); return p.status === 'ACTIVE' && p.stock === 40; }, 40000, 500)));

    // e) las credenciales no aparecen en página, almacenamiento, BD ni log
    const html = await page.content();
    const lsx = await page.evaluate(() => JSON.stringify({ ...localStorage }) + JSON.stringify({ ...sessionStorage }));
    const dbB = fs.readFileSync(D + 'e2e.db', 'latin1') + (fs.existsSync(D + 'e2e.db-wal') ? fs.readFileSync(D + 'e2e.db-wal', 'latin1') : '');
    const logB = fs.readFileSync(LOGF, 'latin1');
    ok(`${P.plat}: las credenciales no aparecen en la página`, P.secrets.every(x => !html.includes(x)));
    ok(`${P.plat}: las credenciales no están en localStorage ni sessionStorage`, P.secrets.every(x => !lsx.includes(x)));
    ok(`${P.plat}: las credenciales no están en claro en la base de datos`, P.secrets.every(x => !dbB.includes(x)));
    ok(`${P.plat}: las credenciales no aparecen en el log del servidor`, P.secrets.every(x => !logB.includes(x)));
    const apiText = JSON.stringify(await storesApi());
    ok(`${P.plat}: la API de tiendas nunca devuelve las credenciales`, P.secrets.every(x => !apiText.includes(x)));

    // f) CONFLICTO DE PROPIEDAD: un producto ajeno con el mismo SKU DF-<id> y sin etiquetas
    const seeded = await (await fetch(CTL + P.ctl + '/seed/' + sku3, { method: 'POST' })).json();
    const foreignBefore = JSON.stringify((await bySku(sku3))[0]);
    ok(`${P.plat}: producto ajeno sembrado con el SKU ${sku3} y sin etiquetas`, seeded.id && JSON.parse(foreignBefore).tags.length === 0, foreignBefore);
    await publishVia(T3);
    await openListings();
    const errRow = await until(async () => { const t = await row(T3).textContent(); return /Ya existe en tu tienda un producto con el SKU/.test(t) ? t : null; }, 30000, 500);
    ok(`${P.plat}: la publicación en conflicto falla con un mensaje claro en la interfaz`, !!errRow, (errRow || await row(T3).textContent().catch(() => '')).replace(/\s+/g, ' ').slice(0, 140));
    ok(`${P.plat}: el mensaje dice que el producto no lo creó esta instalación y qué hacer`, errRow && /no creó esta instalación/.test(errRow) && /cámbiale el SKU|bórralo/.test(errRow));
    const after = await bySku(sku3);
    ok(`${P.plat}: el producto ajeno queda intacto (sin cambios, sin etiquetas, sin duplicados)`, after.length === 1 && JSON.stringify(after[0]) === foreignBefore, JSON.stringify(after));
    const l3 = (await listingsApi(storeId)).items.find(x => x.title.includes(T3));
    ok(`${P.plat}: el listado queda "Con error" (no publicado)`, l3 && l3.status === 'error', l3 && l3.status);

    // g) quitar el listado en conflicto marcando "también eliminar de la tienda" NO borra el producto ajeno
    await row(T3).locator('[data-act=remove]').click();
    await page.waitForSelector('text=¿Quitar este producto?');
    await page.getByLabel('También eliminar de la tienda').check();
    await page.getByRole('button', { name: 'Quitar', exact: true }).last().click();
    await until(async () => !(await listingsApi(storeId)).items.some(x => x.title.includes(T3) && x.status === 'error'), 15000);
    await page.waitForTimeout(3000);
    const after2 = await bySku(sku3);
    ok(`${P.plat}: quitar el listado (aun pidiendo borrar en la tienda) no elimina el producto ajeno`, after2.length === 1 && JSON.stringify(after2[0]) === foreignBefore, JSON.stringify(after2));

    // h) quitar el producto PROPIO sí lo borra de la tienda; el ajeno sigue ahí
    await openListings(); await row(T1).waitFor({ timeout: 15000 });
    await row(T1).locator('[data-act=remove]').click();
    await page.waitForSelector('text=¿Quitar este producto?');
    await page.getByLabel('También eliminar de la tienda').check();
    await page.getByRole('button', { name: 'Quitar', exact: true }).last().click();
    ok(`${P.plat}: quitar el producto propio con "eliminar de la tienda" lo borra (tiene nuestras etiquetas)`, !!(await until(async () => (await bySku(sku1)).length === 0, 30000, 500)));
    ok(`${P.plat}: el producto ajeno sigue intacto tras borrar el propio`, JSON.stringify((await bySku(sku3))[0]) === foreignBefore);

    // i) desconectar nunca elimina el producto ajeno
    await page.click('.tab[data-view=stores]'); await page.waitForSelector(`[data-st=disconnect][data-id="${storeId}"]`);
    await page.click(`[data-st=disconnect][data-id="${storeId}"]`);
    await page.locator('dialog[open] button.danger, dialog[open] .primary, dialog[open] button:has-text("Desconectar")').last().click();
    ok(`${P.plat}: la tienda se desconecta`, !!(await until(async () => !(await storesApi()).some(x => x.id === storeId), 15000)));
    await page.waitForTimeout(1500);
    ok(`${P.plat}: tras desconectar, el producto ajeno sigue intacto en la tienda`, JSON.stringify((await bySku(sku3))[0]) === foreignBefore);
  }
  plat = 'Shopify';

  // 11) Seguridad de datos en reposo: ni el token ni el secreto están en claro en la base ni en el log
  const dbBytes = fs.readFileSync(D + 'e2e.db', 'latin1') + (fs.existsSync(D + 'e2e.db-wal') ? fs.readFileSync(D + 'e2e.db-wal', 'latin1') : '');
  ok('el token NO está en claro en el archivo de la base de datos', !dbBytes.includes('shpat_valido_123'));
  ok('la conexión sí guarda credenciales cifradas (v1$…)', dbBytes.includes('v1$'));
  const log = fs.readFileSync(LOGF, 'latin1');
  ok('el token no aparece en el log del servidor', !log.includes('shpat_valido_123'));

  // 12) CSP y consola
  ok('cero errores de consola ni violaciones de CSP', consoleErrs.length === 0, consoleErrs.slice(0, 3).join(' | '));

  // 13) Móvil 390 px y modo oscuro en Tiendas
  await page.setViewportSize({ width: 390, height: 800 }); await page.emulateMedia({ colorScheme: 'dark' });
  for (const view of ['stores', 'imports', 'dashboard']) {
    await page.click(`.tab[data-view=${view}]`); await page.waitForTimeout(500);
    const [sw, iw] = await page.evaluate(() => [document.documentElement.scrollWidth, window.innerWidth]);
    ok(`390 px sin desplazamiento horizontal en ${view}`, sw <= iw, `${sw}/${iw}`);
  }
  await page.screenshot({ path: D + 'e2e-390-dark.png' });

  await browser.close();
  fs.writeFileSync(D + 'results.json', JSON.stringify(results, null, 1));
  console.log('');
  for (const pl of ['Shopify', 'Tiendanube', 'WooCommerce']) { const rs = results.filter(r => r[3] === pl); console.log(`${pl}: ${rs.filter(r => r[0]).length}/${rs.length}`); }
  console.log(`\n${results.filter(r => r[0]).length}/${results.length} comprobaciones OK`);
  if (problems.length) { console.log('FALLAS:', problems); process.exitCode = 1; }
})().catch(async e => { console.error('ERROR DE LA PRUEBA:', e.message.split('\n')[0]); try { await globalThis.__page.screenshot({ path: D + 'error.png' }); fs.writeFileSync(D + 'error.html', await globalThis.__page.content()); } catch {} process.exitCode = 2; });
