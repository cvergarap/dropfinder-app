const { chromium } = require('playwright');
const fs = require('fs');
const BASE = 'http://127.0.0.1:8891/', CTL = 'http://127.0.0.1:8892';
const D = (process.env.E2E_DIR || (__dirname + '/work')) + '/e2e/';
fs.mkdirSync(D, { recursive: true });
fs.writeFileSync(D + 'prov.csv', 'sku;titulo;costo;categoria;stock;pedidos;url\nE-1;Taza térmica acero;4,00;Hogar;50;300;https://prov.example/E-1\nE-2;Botella deportiva;3,50;Fitness;50;200;https://prov.example/E-2\nE-3;Lámpara escritorio LED;8,90;Hogar;50;120;https://prov.example/E-3\n');
const results = []; const problems = [];
const ok = (name, cond, extra = '') => { results.push([cond, name, extra]); console.log((cond ? 'OK   ' : 'FAIL ') + name + (extra ? '  ' + extra : '')); if (!cond) problems.push(name); };
const state = async () => (await fetch(CTL + '/state')).json();
const until = async (fn, ms = 20000, step = 250) => { const t = Date.now(); for (;;) { try { const v = await fn(); if (v) return v; } catch {} if (Date.now() - t > ms) return null; await new Promise(r => setTimeout(r, step)); } };
const products = async () => Object.values((await state()).products);

(async () => {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await ctx.newPage();
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

  // 11) Seguridad de datos en reposo: ni el token ni el secreto están en claro en la base ni en el log
  const dbBytes = fs.readFileSync(D + 'e2e.db', 'latin1') + (fs.existsSync(D + 'e2e.db-wal') ? fs.readFileSync(D + 'e2e.db-wal', 'latin1') : '');
  ok('el token NO está en claro en el archivo de la base de datos', !dbBytes.includes('shpat_valido_123'));
  ok('la conexión sí guarda credenciales cifradas (v1$…)', dbBytes.includes('v1$'));
  const log = fs.readFileSync(D + 'launch.log', 'latin1');
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
  console.log(`\n${results.filter(r => r[0]).length}/${results.length} comprobaciones OK`);
  if (problems.length) { console.log('FALLAS:', problems); process.exitCode = 1; }
})().catch(e => { console.error('ERROR DE LA PRUEBA:', e.message.split('\n')[0]); process.exitCode = 2; });
