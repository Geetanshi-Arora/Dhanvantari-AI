/* Offline browser checks for the database-backed nurse case dashboard. */
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const origin = "http://localhost:8765";
const frontend = path.resolve(__dirname, "../frontend");
const nurse = { id: "10000000-0000-4000-8000-000000000001", display_name: "Nurse Fictional", role: "nurse" };
const patient = "20000000-0000-4000-8000-000000000001";
const now = "2026-01-01T00:00:00Z";
const entry = (id, name) => ({ id, patient_id: patient, patient_reference: `DV-${id.slice(-8)}`,
  patient_name: name, preferred_language: "hi", nurse_id: nurse.id, doctor_id: null,
  status: "REGISTERED", created_at: now, updated_at: now });
let checks = 0;
const check = (value, message) => { assert.ok(value, message); checks++; };

(async () => {
  const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || "/usr/bin/chromium", headless: true, args: ["--no-sandbox"] });
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    const page = await context.newPage();
    const calls = [], errors = [];
    let role = "nurse", meStatus = 200, replies = [], holdCases, releaseCases;
    page.on("pageerror", (error) => errors.push(error.message));
    await context.route("**/*", async (route) => {
      const request = route.request(), url = new URL(request.url());
      if (url.origin !== origin) { errors.push("External request"); return route.abort(); }
      if (url.pathname.startsWith("/api/")) {
        calls.push({ path: url.pathname, method: request.method(), query: url.searchParams });
        const respond = (status, body) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
        if (url.pathname === "/api/v1/auth/me") return respond(meStatus, meStatus === 200 ? { ...nurse, role } : {});
        if (url.pathname === "/api/v1/patients/cases") {
          if (url.searchParams.has("search")) {
            const matching = url.searchParams.get("search").toLowerCase().includes("asha");
            return respond(200, { items: matching ? [entry("30000000-0000-4000-8000-000000000001", "Asha Fictional")] : [], next_cursor: null });
          }
          const reply = replies.shift() || { items: [], next_cursor: null };
          if (holdCases) await holdCases;
          if (reply.network) return route.abort("failed");
          return respond(reply.status || 200, reply.body || reply);
        }
        if (url.pathname.startsWith("/api/v1/patients/cases/") && request.method() === "GET") {
          const id = url.pathname.split("/")[5];
          if (url.pathname.endsWith("/history")) return respond(200, { items: [
            { id: "40000000-0000-4000-8000-000000000001", case_id: id, previous_status: null,
              status: "REGISTERED", changed_by: nurse.id, changed_at: now },
          ] });
          const item = entry(id, "Asha Fictional");
          return respond(200, item);
        }
        if (url.pathname.endsWith("/registration")) return respond(200, { id: patient, case_id: "30000000-0000-4000-8000-000000000001",
          reference: "DV-TEST", preferred_language: "hi", created_at: now, care_access: "blocked", consents: [
            { id: "50000000-0000-4000-8000-000000000001", purpose: "care", status: "declined", granted: false,
              notice_version: "DRAFT-SYNTHETIC-1", language: "hi", created_at: now, revoked_at: null },
          ] });
        errors.push(`Unexpected API endpoint ${url.pathname}`);
        return respond(404, {});
      }
      const name = url.pathname === "/" ? "index.html" : decodeURIComponent(url.pathname.slice(1));
      const file = path.resolve(frontend, name);
      if (!file.startsWith(frontend + path.sep)) return route.abort();
      try { return route.fulfill({ status: 200, body: await fs.readFile(file), contentType: { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml" }[path.extname(file)] }); }
      catch { errors.push("Missing asset"); return route.abort(); }
    });
    const go = async (items = []) => { replies = items; await page.goto(`${origin}/dashboard.html`); await page.locator("#main").waitFor(); };
    const settled = () => page.waitForFunction(() => document.querySelector("#patient-directory").getAttribute("aria-busy") === "false");

    let release;
    holdCases = new Promise((resolve) => { release = resolve; });
    await page.goto(`${origin}/dashboard.html`, { waitUntil: "commit" });
    await page.locator("#auth-status").waitFor();
    check(calls.every((call) => call.path === "/api/v1/auth/me"), "Case API is not called before staff identity verification");
    check(await page.locator("#main").isHidden(), "Workspace remains hidden while identity is checked");
    replies = [{ items: [entry("30000000-0000-4000-8000-000000000001", "Asha Fictional")], next_cursor: null }];
    release(); holdCases = null;
    await page.locator("#patient-rows tr").waitFor();
    check((await page.locator("#patient-rows").textContent()).includes("Hindi"), "Case uses the patient's preferred language");
    check((await page.locator("#patient-rows").textContent()).includes("REGISTERED"), "Current case status is displayed");
    check(calls.some((call) => call.path === "/api/v1/patients/cases"), "Dashboard reads cases from the backend");

    await page.locator("#patient-search").fill("Asha");
    await page.waitForTimeout(350);
    check(calls.some((call) => call.path === "/api/v1/patients/cases" && call.query.get("search") === "Asha"), "Search is sent to the server");
    check(await page.locator("#patient-rows tr").count() === 1, "Matching case remains visible");
    check(!(await page.evaluate(() => JSON.stringify({ ...sessionStorage, ...localStorage }))).includes("Asha"), "Patient details are never stored in browser storage");

    await page.getByRole("button", { name: /Open case/ }).click();
    await page.locator("#patient-view").waitFor();
    check((await page.locator("#patient-registration-content").textContent()).includes("DRAFT-SYNTHETIC-1"), "Case details show recorded consent version");
    check((await page.locator("#patient-registration-content").textContent()).includes("REGISTERED"), "Case status history is displayed");
    check(await page.locator("#patient-detail-content textarea").count() === 0, "Declined care does not expose clinical details");

    await go([]); await settled();
    check(await page.locator("#directory-empty").isVisible(), "Empty database response has an explicit empty state");
    await go([{ status: 503, body: { detail: "PRIVATE_DB_MARKER" } }]);
    await page.locator("#directory-error").waitFor();
    check((await page.locator("#directory-error").textContent()).includes("temporarily unavailable"), "Database failure is visible and sanitized");
    check(!(await page.locator("#main").textContent()).includes("PRIVATE_DB_MARKER"), "Backend errors never expose server details");

    role = "doctor";
    const beforeDoctor = calls.filter((call) => call.path === "/api/v1/patients/cases").length;
    await page.goto(`${origin}/dashboard.html`); await page.waitForURL("**/doctor.html");
    check(calls.filter((call) => call.path === "/api/v1/patients/cases").length === beforeDoctor, "Doctor workspace cannot load nurse case directory");
    role = "nurse"; meStatus = 401;
    await page.goto(`${origin}/dashboard.html`); await page.waitForURL("**/login.html?reason=expired");
    await page.setViewportSize({ width: 390, height: 844 });
    check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), "Case dashboard fits mobile viewport");
    check(errors.length === 0, "No browser errors, missing files or external calls");
    console.log(`PASS: ${checks} nurse case dashboard checks.`);
    await context.close();
  } finally { await browser.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
