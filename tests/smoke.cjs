/* Offline browser checks: intercepted static files + fictional Auth API doubles.
 * No listening server, Supabase, credentials or external network are used. */
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const origin = "http://localhost:8765";
const frontend = path.resolve(__dirname, "../frontend");
const nurse = { id: "10000000-0000-0000-0000-000000000001", display_name: "Nurse Fictional", role: "nurse" };
const doctor = { id: "10000000-0000-0000-0000-000000000002", display_name: "Doctor Fictional", role: "doctor" };
const password = "fictional-test-password";
const csrf = "fictional-csrf-value";
let checks = 0;
function check(value, message) { assert.ok(value, message); checks++; }

(async () => {
  const browser = await chromium.launch({
    executablePath: process.env.CHROMIUM_PATH || "/usr/bin/chromium",
    headless: true,
    args: ["--no-sandbox"],
  });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage();
  const errors = [];
  const server = {
    staff: nurse, loggedIn: false, loginStatus: 200, meStatus: 200,
    logoutStatus: 204, csrfStatus: 200, offline: false, holdMe: null, malformed: false,
  };
  const calls = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await context.addInitScript(() => {
    const original = window.fetch;
    window.authFetchOptions = [];
    window.fetch = (url, options) => {
      window.authFetchOptions.push({ url: String(url), credentials: options.credentials });
      return original(url, options);
    };
  });
  await context.route("**/*", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.origin !== origin) {
      errors.push("Unexpected external request");
      return route.abort();
    }
    if (url.pathname === "/api/v1/patients") {
      if (!server.loggedIn) return route.fulfill({ status: 401, contentType: "application/json", body: "{}" });
      if (server.staff.role !== "nurse") return route.fulfill({ status: 403, contentType: "application/json", body: "{}" });
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({
        items: [{ id: "20000000-0000-4000-8000-000000000001", reference: "FICTIONAL-001",
          name: "Adult Fictional", age: 36, age_recorded_on: "2026-01-01", preferred_language: "hi" }],
        next_cursor: null,
      }) });
    }
    if (url.pathname.startsWith("/api/")) {
      const endpoint = url.pathname.slice("/api/v1/auth/".length);
      calls.push({ endpoint, method: request.method() });
      if (server.offline) return route.abort("failed");
      const respond = (status, value, headers = {}) => route.fulfill({
        status, contentType: "application/json", headers: { "Cache-Control": "no-store", ...headers },
        ...(status === 204 ? {} : { body: JSON.stringify(value) }),
      });
      if (endpoint === "login") {
        const body = request.postDataJSON();
        check(Object.keys(body).sort().join(",") === "email,password", "Browser role is never sent as authorization");
        check(request.headers().origin === origin, "Browser sends the exact same origin");
        if (server.loginStatus !== 200) return respond(server.loginStatus, { detail: "FICTIONAL_PRIVATE_PROVIDER_DETAIL" });
        check(body.password === password, "Password is passed only to login API");
        server.loggedIn = true;
        return respond(200, server.staff, { "Set-Cookie": "dhanvantari_session=fictional-cookie; HttpOnly; Path=/api; SameSite=Strict" });
      }
      if (endpoint === "me") {
        if (server.holdMe) await server.holdMe;
        if (server.malformed) return respond(200, { role: "nurse" });
        if (!server.loggedIn) return respond(401, { detail: "expired" });
        if (server.meStatus !== 200) return respond(server.meStatus, { detail: "FICTIONAL_PRIVATE_PROVIDER_DETAIL" });
        return respond(200, server.staff);
      }
      if (endpoint === "csrf") {
        if (!server.loggedIn) return respond(401, {});
        if (server.csrfStatus !== 200) return respond(server.csrfStatus, {});
        return respond(200, { csrf_token: csrf });
      }
      if (endpoint === "logout") {
        if (server.loggedIn && server.csrfStatus === 200) check(request.headers()["x-csrf-token"] === csrf, "Logout sends fetched CSRF header");
        if (server.logoutStatus !== 204) return respond(server.logoutStatus, { detail: "FICTIONAL_PRIVATE_PROVIDER_DETAIL" });
        server.loggedIn = false;
        return respond(204, null, { "Set-Cookie": "dhanvantari_session=; Max-Age=0; HttpOnly; Path=/api; SameSite=Strict" });
      }
      errors.push("Unexpected API endpoint");
      return respond(404, {});
    }
    const name = url.pathname === "/" ? "index.html" : decodeURIComponent(url.pathname.slice(1));
    const file = path.resolve(frontend, name);
    if (!file.startsWith(frontend + path.sep)) return route.abort();
    try {
      const body = await fs.readFile(file);
      const contentType = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml" }[path.extname(file)];
      return route.fulfill({ status: 200, contentType, body, headers: {
        "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
      } });
    } catch {
      errors.push("Missing static asset");
      return route.fulfill({ status: 404, body: "Missing asset" });
    }
  });
  async function go(name) { await page.goto(`${origin}/${name}`); }
  async function login(role = "nurse") {
    await go("login.html");
    await page.locator(`[data-role="${role}"]`).click();
    await page.getByLabel("Email address").fill(`${role}@example.com`);
    await page.getByLabel("Password", { exact: true }).fill(password);
    await page.getByRole("button", { name: "Sign In" }).click();
  }
  try {
    await go("index.html");
    check(await page.getByRole("heading", { level: 1 }).count() === 1, "Public homepage preserved");
    check(calls.length === 0, "Homepage does not require authentication");
    await page.getByRole("link", { name: "Sign in", exact: false }).first().click();
    await page.waitForURL("**/login.html");
    check(await page.getByLabel("Email address").isVisible(), "Homepage navigation opens real sign-in form");
    const beforeEmpty = calls.filter((call) => call.endpoint === "login").length;
    await page.getByRole("button", { name: "Sign In" }).click();
    check(calls.filter((call) => call.endpoint === "login").length === beforeEmpty, "Empty credentials never reach login API");
    for (const name of ["dashboard", "language", "register", "doctor"]) {
      await go(`${name}.html`);
      await page.waitForURL("**/login.html?reason=expired");
      check(await page.locator("#login-error").isVisible(), `${name}: unauthenticated users return to sign in`);
    }
    // Old browser state must not bypass /me.
    await page.evaluate(() => sessionStorage.setItem("dhanvantari.phase1", JSON.stringify({ role: "nurse", displayName: "Untrusted" })));
    await go("dashboard.html");
    await page.waitForURL("**/login.html?reason=expired");
    check(!server.loggedIn, "Forged browser role cannot authenticate");

    server.loggedIn = true;
    let release;
    server.holdMe = new Promise((resolve) => { release = resolve; });
    await page.goto(`${origin}/dashboard.html`, { waitUntil: "commit" });
    await page.locator("#auth-status").waitFor();
    check(await page.locator("#main").isHidden(), "Protected main stays hidden during identity verification");
    check(await page.locator(".workspace-bar").isHidden(), "Protected workspace controls stay hidden");
    release(); server.holdMe = null;
    await page.locator("#main").waitFor();
    check(await page.locator("#greeting-name").textContent() === nurse.display_name, "Greeting uses verified identity");
    server.loggedIn = false;

    await login();
    await page.waitForURL("**/dashboard.html");
    await page.locator("#main").waitFor();
    await page.locator("#patient-rows tr").first().waitFor();
    check(await page.locator("#patient-rows tr").count() === 1, "Nurse reaches authorized API directory");
    check(await page.locator(".notice.compact").textContent().then((s) => s.includes("Fictional")), "Development records remain clearly labeled");
    await page.evaluate(async () => {
      const store = await import("/assets/js/store.js");
      store.saveState({ password: "fictional", access_token: "fictional", role: "doctor" });
    });
    check(await page.evaluate(() => sessionStorage.getItem("dhanvantari.phase1")) === "{}", "Demo store rejects credential and role fields");
    const stored = await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage }));
    check(!stored.includes(password) && !stored.includes("fictional-cookie") && !stored.includes(csrf), "No authentication secrets in browser storage");
    check(await page.evaluate(() => window.authFetchOptions.every((call) => call.credentials === "same-origin")), "Authentication fetches explicitly use same-origin credentials");
    check(!(await page.evaluate(() => document.cookie)).includes("dhanvantari_session"), "Session cookie is not exposed to JavaScript");

    await go("language.html");
    await page.locator("#main").waitFor();
    check(await page.locator("#language-continue").isDisabled(), "Language selection required");
    await page.locator('[data-language="hi"]').click();
    check(await page.locator('[data-language="hi"]').getAttribute("aria-pressed") === "true", "Language selection is announced");
    await page.getByRole("button", { name: "Continue to registration" }).click();
    await page.waitForURL("**/register.html");
    await page.locator("#main").waitFor();
    check(await page.locator("#patient-language").inputValue() === "hi", "Language preference retained");
    check(await page.getByRole("button", { name: "Registration unavailable" }).isDisabled(), "Registration stays disabled without approved notices");
    check(await page.locator("#registration-error").isVisible(), "Notice-unavailable explanation is visible");
    await page.locator("#registration-form").evaluate((form) => form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
    check(page.url().endsWith("register.html"), "Programmatic submit does not save or navigate");
    await go("dashboard.html");
    await page.locator("#patient-rows tr").first().waitFor();
    check(await page.locator("#patient-count").textContent() === "1", "Only authorized API records are counted");
    await page.locator("#patient-search").fill("missing");
    check(await page.locator("#empty-search").isVisible(), "Loaded-record search has an empty state");
    await page.reload(); await page.locator("#patient-rows tr").first().waitFor();
    check(await page.locator("#patient-count").textContent() === "1", "Reload fetches API records without demo fallback");

    server.logoutStatus = 503;
    await page.getByRole("button", { name: "Sign out" }).click();
    await page.locator(".toast").waitFor();
    check((await page.locator(".toast").textContent()).includes("may still be active"), "Failed logout is never presented as confirmed revocation");
    check(server.loggedIn && page.url().endsWith("dashboard.html"), "Failed logout does not redirect or clear active session");
    server.csrfStatus = 401; server.logoutStatus = 403;
    await page.getByRole("button", { name: "Sign out" }).click();
    await page.waitForFunction(() => !document.querySelector("[data-logout]").disabled);
    check(server.loggedIn && page.url().endsWith("dashboard.html"), "Expired-session revocation failure remains explicit");
    server.csrfStatus = 200; server.logoutStatus = 204;
    await page.getByRole("button", { name: "Sign out" }).click();
    await page.waitForURL("**/login.html");
    check(!server.loggedIn, "Successful logout revokes mock server session");
    check(await page.evaluate(() => sessionStorage.getItem("dhanvantari.phase1")) === null, "Successful logout clears synthetic state");
    await go("dashboard.html"); await page.waitForURL("**/login.html?reason=expired");
    check(!server.loggedIn, "Signed-out user cannot reopen nurse workflow");

    for (const [status, message] of [[401, "Incorrect email"], [403, "inactive"], [429, "Too many"], [422, "Check your email"], [503, "unavailable"]]) {
      server.loginStatus = status;
      await login();
      await page.locator("#login-error").waitFor();
      check((await page.locator("#login-error").textContent()).includes(message), `Login status ${status} has helpful error`);
      check(!(await page.locator("#login-error").textContent()).includes("PRIVATE_PROVIDER"), "Backend error details are hidden");
      check(await page.locator("#login-password").inputValue() === "", "Submitted password is cleared");
    }
    server.loginStatus = 200; server.offline = true;
    await login(); await page.locator("#login-error").waitFor();
    check((await page.locator("#login-error").textContent()).includes("unavailable"), "Network failure is handled");
    server.offline = false;

    server.staff = doctor;
    await login("nurse"); await page.locator("#login-error").waitFor();
    check((await page.locator("#login-error").textContent()).includes("doctor workspace"), "Role mismatch blocks workspace redirect");
    check(!server.loggedIn, "Mismatched workspace login is revoked through CSRF logout");
    server.logoutStatus = 503;
    await login("nurse"); await page.locator("#login-logout").waitFor();
    check((await page.locator("#login-error").textContent()).includes("may still be active"), "Role-mismatch revocation failures remain visible");
    server.logoutStatus = 204;
    await page.getByRole("button", { name: "Retry sign out" }).click();
    await page.waitForFunction(() => document.querySelector("#login-logout").hidden);
    check(!server.loggedIn, "Role-mismatch sign-out retry can revoke the session");
    await login("doctor"); await page.waitForURL("**/doctor.html"); await page.locator("#main").waitFor();
    check((await page.locator("#main").textContent()).includes("coming soon"), "Doctor receives verified placeholder");
    for (const name of ["dashboard", "language", "register"]) {
      await go(`${name}.html`); await page.waitForURL("**/doctor.html");
      check(await page.locator("#registration-form").count() === 0, `Doctor excluded from ${name} workflow`);
    }
    server.staff = nurse;
    await go("doctor.html"); await page.waitForURL("**/dashboard.html"); await page.locator("#main").waitFor();
    check(await page.locator("#greeting-name").textContent() === nurse.display_name, "Nurse excluded from doctor placeholder");

    server.meStatus = 503;
    await go("register.html"); await page.locator("#auth-retry").waitFor();
    check(await page.locator("#main").isHidden(), "Backend outage leaves protected forms hidden");
    server.meStatus = 200;
    await page.getByRole("button", { name: "Retry verification" }).click();
    await page.locator("#main").waitFor();
    check(await page.locator("#registration-form").isVisible(), "Retry can recover after backend failure");
    server.malformed = true;
    await go("dashboard.html"); await page.locator("#auth-retry").waitFor();
    check(await page.locator("#main").isHidden(), "Malformed staff response cannot reveal protected content");
    server.malformed = false;

    // Simulate bfcache restoration and ensure the hidden page is revalidated.
    await go("dashboard.html"); await page.locator("#main").waitFor();
    server.loggedIn = false;
    await page.evaluate(() => window.dispatchEvent(new PageTransitionEvent("pageshow", { persisted: true })));
    await page.waitForURL("**/login.html?reason=expired");
    check(!server.loggedIn, "Back-forward cache restoration revalidates the session");
    server.loggedIn = true;

    await page.setViewportSize({ width: 390, height: 844 });
    for (const name of ["index", "login", "dashboard", "language", "register"]) {
      await go(`${name}.html`);
      if (["dashboard", "language", "register"].includes(name)) await page.locator("#main").waitFor();
      check(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), `${name}: mobile layout has no horizontal overflow`);
    }
    await page.getByRole("button", { name: "Toggle navigation" }).click();
    check(await page.locator("#navigation").isVisible(), "Mobile navigation opens");
    await page.keyboard.press("Escape");
    check(!(await page.locator("#navigation").isVisible()), "Escape closes navigation");
    server.staff = doctor;
    await go("doctor.html"); await page.locator("#main").waitFor();
    check(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), "Doctor placeholder fits mobile viewport");
    check(errors.length === 0, "No browser exceptions, missing assets or external requests");
    console.log(`PASS: ${checks} offline browser checks; cookie API contract, role guards, logout, errors, read-only directory, registration lock and mobile layout.`);
  } finally {
    await browser.close();
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
