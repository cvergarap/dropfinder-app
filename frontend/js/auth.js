/* Cuentas de usuario: pantalla de acceso (primer propietario / inicio de sesión), menú del usuario,
   "Mi cuenta" y administración de usuarios (propietario). Usa DF.auth (ver app.js): abre la API con
   DF.auth.open() cuando hay sesión y la bloquea con DF.auth.close() ante un 401. */
(function () {
  const A = DF.auth;
  const ROLE = { owner: "Propietario", member: "Miembro" };
  let screen = null;          // pantalla de acceso
  let lastUserId = null;      // para recargar si entra otra persona tras un 401

  /* Llamada a /api/auth/* o /api/users*: devuelve el JSON o lanza Error con .status */
  async function call(path, method = "GET", body) {
    const headers = { "Content-Type": "application/json" };
    if (method !== "GET" && A.csrf) headers["X-CSRF-Token"] = A.csrf;
    const res = await fetch(path, { method, headers, credentials: "same-origin", body: body ? JSON.stringify(body) : undefined });
    let data = {};
    try { data = await res.json(); } catch { /* sin JSON */ }
    if (!res.ok) {
      const err = new Error((data && data.error) || `Error ${res.status}`);
      err.status = res.status;
      throw err;
    }
    return data;
  }

  /* ---------------- Pantalla de acceso ---------------- */
  function showScreen(mode, notice = "", needToken = false) {
    document.body.classList.add("df-locked");
    if (!screen) {
      screen = document.createElement("main");
      screen.id = "authScreen";
      screen.className = "auth-screen";
      document.body.prepend(screen);
    }
    const first = mode === "register";
    screen.innerHTML = `<div class="auth-card">
      <div class="brand">Drop<span>Finder</span></div>
      <h1>${first ? "Crear cuenta de propietario" : "Iniciar sesión"}</h1>
      ${first ? `<p class="muted">Es la primera vez que se abre esta instalación. Esta cuenta será la del
        <b>propietario</b>: administra el catálogo, las reglas de precio y los usuarios del equipo.
        Los productos importados, alertas y búsquedas guardadas que ya existen quedarán en esta cuenta.</p>` : ""}
      ${notice ? `<p class="note">${esc(notice)}</p>` : ""}
      <form id="authForm" novalidate>
        ${first ? `<label class="field">Nombre<input name="name" autocomplete="name" required maxlength="80"></label>` : ""}
        <label class="field">Correo<input name="email" type="email" autocomplete="email" required maxlength="254"
          inputmode="email" autocapitalize="none" spellcheck="false"></label>
        <label class="field">Contraseña<input name="password" type="password" required
          autocomplete="${first ? "new-password" : "current-password"}"${first ? ' minlength="10" aria-describedby="pwHelp"' : ""}></label>
        ${first ? `<p id="pwHelp" class="muted small">Mínimo 10 caracteres y distinta de tu correo.</p>
          <label class="field">Repite la contraseña<input name="password2" type="password" autocomplete="new-password" required></label>` : ""}
        ${first && needToken ? `<label class="field">Código de instalación<input name="setup_token" autocomplete="off"
            required spellcheck="false" autocapitalize="none" aria-describedby="tokenHelp"></label>
          <p id="tokenHelp" class="muted small">Estás entrando desde otro equipo. El código aparece en la consola
            del servidor al arrancar (o es el valor de DROPFINDER_SETUP_TOKEN).</p>` : ""}
        <p class="auth-error" role="alert"></p>
        <button class="primary" type="submit">${first ? "Crear cuenta y entrar" : "Entrar"}</button>
      </form>
      ${first ? "" : `<p class="muted small">¿No tienes cuenta? Pídele al propietario de la instalación que te cree una.</p>`}
    </div>`;
    const form = $("#authForm", screen);
    form.addEventListener("submit", (e) => { e.preventDefault(); submit(form, first); });
    $("input", form).focus();
  }

  async function submit(form, first) {
    const err = $(".auth-error", form);
    const btn = $("button[type=submit]", form);
    err.textContent = "";
    const v = Object.fromEntries(new FormData(form));
    if (first) {
      if (!v.name.trim()) { err.textContent = "Escribe tu nombre."; return; }
      if ((v.password || "").length < 10) { err.textContent = "La contraseña debe tener al menos 10 caracteres."; return; }
      if (v.password !== v.password2) { err.textContent = "Las contraseñas no coinciden."; return; }
      if (form.setup_token && !v.setup_token.trim()) { err.textContent = "Escribe el código de instalación."; return; }
    } else if (!v.email || !v.password) { err.textContent = "Escribe tu correo y tu contraseña."; return; }
    btn.disabled = true;
    try {
      const d = first
        ? await call("/api/auth/register", "POST", { name: v.name, email: v.email, password: v.password,
          ...(v.setup_token ? { setup_token: v.setup_token.trim() } : {}) })
        : await call("/api/auth/login", "POST", { email: v.email, password: v.password });
      signedIn(d);
    } catch (e) {
      err.textContent = e.message;
      form.password.value = "";
      if (form.password2) form.password2.value = "";
      form.password.focus();
    } finally { btn.disabled = false; }
  }

  /* ---------------- Sesión iniciada ---------------- */
  function signedIn(d) {
    if (lastUserId !== null && d.user.id !== lastUserId) { location.reload(); return; }   // otra persona: estado limpio
    lastUserId = d.user.id;
    A.user = d.user;
    A.csrf = d.csrf_token;
    document.body.classList.remove("df-locked");
    document.body.classList.toggle("df-owner", d.user.role === "owner");
    document.body.classList.toggle("df-member", d.user.role !== "owner");
    if (screen) { screen.remove(); screen = null; }
    renderMenu();
    A.open();
    DF.start();
    if (DF.dashboard && DF.dashboard.home) dfSafe(DF.dashboard.home);   // el Panel es la vista de inicio
  }

  A.onUnauthorized = () => {
    if (document.body.classList.contains("df-locked")) return;
    A.close();
    A.csrf = null;
    closeDialogs();
    showScreen("login", "Tu sesión terminó. Vuelve a iniciar sesión para continuar.");
  };

  /* ---------------- Menú del usuario ---------------- */
  let menuWired = false;
  function renderMenu() {
    let box = $("#userMenu");
    if (!box) {
      box = document.createElement("div");
      box.id = "userMenu";
      box.className = "user-menu";
      $(".topbar-tools").append(box);
    }
    const u = A.user;
    box.innerHTML = `<button type="button" class="ghost user-btn" aria-haspopup="true" aria-expanded="false" aria-controls="userMenuList">
        <span class="user-avatar" aria-hidden="true">${esc((u.name || "?").trim().charAt(0).toUpperCase())}</span>
        <span class="user-name">${esc(u.name)}</span><span class="sr-only"> (${ROLE[u.role] || esc(u.role)}), menú de la cuenta</span>
      </button>
      <div id="userMenuList" class="user-list" role="menu" hidden>
        <div class="user-head"><b>${esc(u.name)}</b><span class="muted small">${esc(u.email)}</span>
          <span><span class="chip">${ROLE[u.role] || esc(u.role)}</span></span></div>
        <button type="button" role="menuitem" data-act="account">Mi cuenta</button>
        ${u.role === "owner" ? `<button type="button" role="menuitem" data-act="users">Usuarios</button>` : ""}
        <button type="button" role="menuitem" data-act="logout">Cerrar sesión</button>
      </div>`;
    if (menuWired) return;
    menuWired = true;
    const list = () => $(".user-list", box);
    const btn = () => $(".user-btn", box);
    const toggle = (open) => { list().hidden = !open; btn().setAttribute("aria-expanded", String(open)); };
    box.addEventListener("click", (e) => {
      if (e.target.closest(".user-btn")) {
        toggle(list().hidden);
        if (!list().hidden) $("[role=menuitem]", list()).focus();
        return;
      }
      const b = e.target.closest("[data-act]");
      if (!b) return;
      toggle(false);
      ({ account: openAccount, users: openUsers, logout })[b.dataset.act]();
    });
    box.addEventListener("keydown", (e) => {
      if (list().hidden) return;
      if (e.key === "Escape") { toggle(false); btn().focus(); }
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        const items = $$("[role=menuitem]", list());
        const i = items.indexOf(document.activeElement);
        items[(i + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length].focus();
        e.preventDefault();
      }
    });
    document.addEventListener("click", (e) => { if (!box.contains(e.target)) toggle(false); });
  }

  async function logout() {
    try { await call("/api/auth/logout", "POST"); } catch { /* sesión ya cerrada */ }
    location.reload();                 // estado limpio: la próxima persona no ve datos de esta sesión
  }

  /* ---------------- Diálogos ---------------- */
  function dialog(id, title) {
    let dlg = $(`#${id}`);
    if (!dlg) {
      dlg = document.createElement("dialog");
      dlg.id = id;
      dlg.className = "auth-dialog";
      dlg.setAttribute("aria-labelledby", `${id}Title`);
      document.body.append(dlg);
      dlg.addEventListener("click", (e) => { if (e.target === dlg) dlg.close(); });
    }
    dlg.innerHTML = `<div class="auth-dlg-head"><h2 id="${id}Title">${title}</h2>
      <button type="button" class="close" aria-label="Cerrar">×</button></div><div class="auth-dlg-body"></div>`;
    $(".close", dlg).addEventListener("click", () => dlg.close());
    return dlg;
  }
  function closeDialogs() { $$("dialog[open]").forEach((d) => d.close()); }

  /* Mi cuenta: nombre y contraseña */
  function openAccount() {
    const dlg = dialog("accountDlg", "Mi cuenta");
    const u = A.user;
    $(".auth-dlg-body", dlg).innerHTML = `
      <p class="muted">${esc(u.email)} · ${ROLE[u.role] || esc(u.role)}</p>
      <form id="nameForm" class="auth-form">
        <label class="field">Nombre<input name="name" autocomplete="name" required maxlength="80" value="${esc(u.name)}"></label>
        <p class="auth-error" role="alert"></p>
        <button class="primary" type="submit">Guardar nombre</button>
      </form>
      <form id="pwForm" class="auth-form">
        <h3>Cambiar contraseña</h3>
        <p class="muted small">Se cerrará la sesión en tus otros dispositivos.</p>
        <input type="email" name="username" autocomplete="email" value="${esc(u.email)}" hidden>
        <label class="field">Contraseña actual<input name="current_password" type="password" autocomplete="current-password" required></label>
        <label class="field">Nueva contraseña<input name="new_password" type="password" autocomplete="new-password" minlength="10" required></label>
        <label class="field">Repite la nueva contraseña<input name="new_password2" type="password" autocomplete="new-password" required></label>
        <p class="auth-error" role="alert"></p>
        <button class="primary" type="submit">Cambiar contraseña</button>
      </form>`;
    $("#nameForm", dlg).addEventListener("submit", async (e) => {
      e.preventDefault();
      const err = $(".auth-error", e.target);
      err.textContent = "";
      try {
        const d = await call("/api/auth/me", "PATCH", { name: e.target.name.value });
        A.user = d.user; renderMenu(); toast("Nombre actualizado");
      } catch (x) { err.textContent = x.message; }
    });
    $("#pwForm", dlg).addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = e.target;
      const err = $(".auth-error", f);
      err.textContent = "";
      if (f.new_password.value.length < 10) { err.textContent = "La nueva contraseña debe tener al menos 10 caracteres."; return; }
      if (f.new_password.value !== f.new_password2.value) { err.textContent = "Las contraseñas nuevas no coinciden."; return; }
      try {
        const d = await call("/api/auth/me", "PATCH", { current_password: f.current_password.value, new_password: f.new_password.value });
        A.user = d.user; A.csrf = d.csrf_token;
        f.reset(); dlg.close(); toast("Contraseña cambiada. Se cerraron tus otras sesiones.");
      } catch (x) { err.textContent = x.message; }
    });
    dlg.showModal();
  }

  /* Usuarios (propietario): lista, crear, rol, desactivar, restablecer contraseña, eliminar */
  function openUsers() {
    const dlg = dialog("usersDlg", "Usuarios");
    const body = $(".auth-dlg-body", dlg);
    body.innerHTML = `<p class="muted">Cada usuario tiene sus propios productos importados, alertas, búsquedas guardadas y
        moneda. El catálogo y las reglas de precio son de toda la instalación.</p>
      <ul class="user-rows" aria-live="polite"></ul>
      <p class="auth-error" id="usersErr" role="alert"></p>
      <form id="resetForm" class="auth-form hidden">
        <h3 id="resetTitle">Restablecer contraseña</h3>
        <input type="hidden" name="uid">
        <input type="text" name="username" autocomplete="username" hidden>
        <label class="field">Nueva contraseña<input name="password" type="password" autocomplete="new-password" minlength="10" required></label>
        <p class="auth-error" role="alert"></p>
        <div class="row-btns"><button class="primary" type="submit">Guardar contraseña</button>
          <button class="ghost" type="button" data-cancel>Cancelar</button></div>
      </form>
      <form id="newUserForm" class="auth-form">
        <h3>Agregar usuario</h3>
        <label class="field">Nombre<input name="name" autocomplete="off" required maxlength="80"></label>
        <label class="field">Correo<input name="email" type="email" autocomplete="off" required maxlength="254"></label>
        <label class="field">Contraseña inicial<input name="password" type="password" autocomplete="new-password" minlength="10" required
          aria-describedby="newPwHelp"></label>
        <p id="newPwHelp" class="muted small">Mínimo 10 caracteres. Compártela por un canal seguro; la persona puede cambiarla en «Mi cuenta».</p>
        <label class="field">Rol<select name="role"><option value="member">Miembro</option><option value="owner">Propietario</option></select></label>
        <p class="auth-error" role="alert"></p>
        <button class="primary" type="submit">Crear usuario</button>
      </form>`;
    const list = $(".user-rows", body);
    const topErr = $("#usersErr", body);
    const reset = $("#resetForm", body);
    let items = [];

    async function load() {
      try {
        items = (await call("/api/users")).items;
        list.innerHTML = items.map((u) => {
          const me = u.id === A.user.id;
          return `<li data-id="${u.id}" class="${u.disabled ? "is-disabled" : ""}">
            <div class="who"><b>${esc(u.name)}${me ? " (tú)" : ""}</b><span class="muted small">${esc(u.email)}</span>
              <span class="small"><span class="chip">${ROLE[u.role] || esc(u.role)}</span>${u.disabled ? ' <span class="chip chip-bad">Desactivado</span>' : ""}
              <span class="muted">· Último acceso: ${u.last_login_at ? new Date(u.last_login_at).toLocaleString("es") : "nunca"}</span></span></div>
            ${me ? "" : `<div class="acts">
              <button type="button" class="link" data-act="role">${u.role === "owner" ? "Hacer miembro" : "Hacer propietario"}</button>
              <button type="button" class="link" data-act="toggle">${u.disabled ? "Activar" : "Desactivar"}</button>
              <button type="button" class="link" data-act="reset">Restablecer contraseña</button>
              <button type="button" class="link danger" data-act="delete">Eliminar</button></div>`}
          </li>`;
        }).join("");
      } catch (e) { topErr.textContent = e.message; }
    }

    list.addEventListener("click", async (e) => {
      const b = e.target.closest("[data-act]");
      if (!b) return;
      const u = items.find((x) => x.id === Number(b.closest("li").dataset.id));
      if (!u) return;
      topErr.textContent = "";
      try {
        if (b.dataset.act === "role") {
          await call(`/api/users/${u.id}`, "PATCH", { role: u.role === "owner" ? "member" : "owner" });
        } else if (b.dataset.act === "toggle") {
          if (!u.disabled && !confirm(`¿Desactivar a ${u.name}? No podrá entrar y se cerrarán sus sesiones.`)) return;
          await call(`/api/users/${u.id}`, "PATCH", { disabled: !u.disabled });
        } else if (b.dataset.act === "reset") {
          reset.classList.remove("hidden");
          reset.uid.value = u.id;
          reset.username.value = u.email;
          $("#resetTitle", reset).textContent = `Restablecer contraseña de ${u.name}`;
          reset.password.focus();
          return;
        } else if (b.dataset.act === "delete") {
          if (!confirm(`¿Eliminar a ${u.name}? Se borrarán sus productos importados, alertas y búsquedas guardadas. No se puede deshacer.`)) return;
          await call(`/api/users/${u.id}`, "DELETE");
          toast("Usuario eliminado");
        }
        load();
      } catch (x) { topErr.textContent = x.message; }
    });

    reset.addEventListener("submit", async (e) => {
      e.preventDefault();
      const err = $(".auth-error", reset);
      err.textContent = "";
      try {
        await call(`/api/users/${reset.uid.value}`, "PATCH", { password: reset.password.value });
        reset.reset(); reset.classList.add("hidden"); toast("Contraseña restablecida; se cerraron sus sesiones");
      } catch (x) { err.textContent = x.message; }
    });
    $("[data-cancel]", reset).addEventListener("click", () => { reset.reset(); reset.classList.add("hidden"); });

    $("#newUserForm", body).addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = e.target;
      const err = $(".auth-error", f);
      err.textContent = "";
      try {
        await call("/api/users", "POST", Object.fromEntries(new FormData(f)));
        f.reset(); toast("Usuario creado"); load();
      } catch (x) { err.textContent = x.message; }
    });
    dlg.showModal();
    load();
  }

  /* ---------------- Inicio ---------------- */
  async function boot() {
    try {
      const st = await call("/api/auth/status");
      if (!st.has_users) { showScreen("register", "", !!st.setup_token_required); return; }
      if (!st.authenticated) { showScreen("login"); return; }
      signedIn(await call("/api/auth/me"));
    } catch (e) {
      showScreen("login", e.status === 401 ? "" : `No se pudo conectar con el servidor: ${e.message}`);
    }
  }
  boot();
}());

/* Si la sesión termina a mitad de uso, api() ya mostró la pantalla de acceso; las llamadas que no capturan
   su error (carga de listas, etc.) no deben dejar un aviso suelto en la consola. */
window.addEventListener("unhandledrejection", (e) => {
  if (e.reason && e.reason.status === 401) e.preventDefault();
});
