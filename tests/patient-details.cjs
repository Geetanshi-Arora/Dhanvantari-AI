/* Offline browser security checks for opening individual assigned patient cases. */
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const origin = "http://localhost:8765";
const frontend = path.resolve(__dirname, "../frontend");
const nurse = { id: "10000000-0000-4000-8000-000000000001", display_name: "Nurse Fictional", role: "nurse" };
const PATIENT = "20000000-0000-4000-8000-000000000001";
const CASE = "30000000-0000-4000-8000-000000000001";
const now = "2026-01-01T00:00:00Z";
const detail = { id: PATIENT, reference: "DV-TEST", name: "<img src=x> Fictional", age: 36,
  age_recorded_on: "2026-01-01", preferred_language: "hi", sex: null, contact_reference: null,
  history: "Fictional history", allergies: null, current_medicines: null, created_at: now };
const caseData = { id: CASE, patient_id: PATIENT, patient_reference: "DV-TEST", patient_name: "Asha Fictional",
  preferred_language: "hi", nurse_id: nurse.id, doctor_id: null, status: "REGISTERED", created_at: now, updated_at: now };
let checks = 0;
const check = (value, message) => { assert.ok(value, message); checks++; };

(async () => {
  const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || "/usr/bin/chromium", headless: true, args: ["--no-sandbox"] });
  try {
    const context = await browser.newContext();
    const page = await context.newPage();
    const calls = [], errors = [];
    let care = "granted", caseStatus = 200, detailStatus = 200, historyStatus = 200;
    page.on("pageerror", (error) => errors.push(error.message));
    await context.addInitScript(() => {
      window.patientReadOptions = [];
      const original = window.fetch;
      window.fetch = (url, options) => {
        if (String(url).startsWith("/api/v1/patients/")) window.patientReadOptions.push({ method: options.method, credentials: options.credentials, cache: options.cache, redirect: options.redirect });
        return original(url, options);
      };
    });
    await context.route("**/*", async (route) => {
      const request = route.request(), url = new URL(request.url());
      if (url.origin !== origin) { errors.push("External request"); return route.abort(); }
      const respond = (status, body) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
      if (url.pathname.startsWith("/api/")) {
        calls.push({ path: url.pathname, method: request.method() });
        if (url.pathname === "/api/v1/auth/me") return respond(200, nurse);
        if (url.pathname === "/api/v1/patients/cases") return respond(200, { items: [caseData], next_cursor: null });
        if (url.pathname === `/api/v1/patients/cases/${CASE}`) return respond(caseStatus, caseStatus === 200 ? caseData : { detail: "PRIVATE" });
        if (url.pathname === `/api/v1/patients/cases/${CASE}/history`) return respond(historyStatus, historyStatus === 200 ? { items: [{ id: "40000000-0000-4000-8000-000000000001", case_id: CASE, previous_status: null, status: "REGISTERED", changed_by: nurse.id, changed_at: now }] } : { detail: "PRIVATE" });
        if (url.pathname === `/api/v1/patients/${PATIENT}/registration`) return respond(200, { id: PATIENT,
          case_id: CASE, reference: "DV-TEST", preferred_language: "hi", created_at: now,
          care_access: care === "granted" ? "enabled" : "blocked", consents: [{ id: "50000000-0000-4000-8000-000000000001",
            purpose: "care", status: care, granted: care === "granted", notice_version: "DRAFT-SYNTHETIC-1",
            language: "hi", created_at: now, revoked_at: null }] });
        if (url.pathname === `/api/v1/patients/${PATIENT}`) return respond(detailStatus, detailStatus === 200 ? detail : { detail: "PRIVATE" });
        errors.push(`Unexpected API ${url.pathname}`);
        return respond(404, {});
      }
      const file = path.resolve(frontend, decodeURIComponent(url.pathname.slice(1)));
      if (!file.startsWith(frontend + path.sep)) return route.abort();
      try { return route.fulfill({ status: 200, body: await fs.readFile(file), contentType: { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml" }[path.extname(file)] }); }
      catch { errors.push("Missing asset"); return route.abort(); }
    });
    async function open() {
      await page.goto(`${origin}/dashboard.html`);
      await page.locator("#patient-rows tr").waitFor();
      await page.getByRole("button", { name: /Open case/ }).click();
      await page.waitForFunction(() => !document.querySelector("#patient-view").getAttribute("aria-busy") || document.querySelector("#patient-view-message").textContent !== "Loading assigned patient case…");
    }
    await open();
    check((await page.locator("#patient-view").textContent()).includes(CASE), "Case ID is shown in the detail panel");
    check((await page.locator("#patient-view").textContent()).includes("REGISTERED"), "Current status and history are shown");
    check(await page.locator("#patient-detail-content img").count() === 0, "Patient-supplied text cannot inject HTML");
    check(await page.getByLabel("Name", { exact: true }).inputValue() === detail.name, "Patient text is rendered literally in a read-only field");
    check(calls.some((call) => call.path === `/api/v1/patients/${PATIENT}`), "Clinical details are fetched only after care status is read");
    check((await page.evaluate(() => window.patientReadOptions)).every((item) => item.method === "GET" && item.credentials === "same-origin" && item.cache === "no-store" && item.redirect === "error"), "Patient and case reads remain same-origin/no-store GETs");

    await page.getByRole("button", { name: "Close record" }).click();
    check(await page.locator("#patient-detail-content textarea").count() === 0, "Closing removes patient details from the DOM");
    care = "declined";
    const detailReadsBeforeBlockedCase = calls.filter((call) => call.path === `/api/v1/patients/${PATIENT}`).length;
    await open();
    check(await page.locator("#patient-detail-content textarea").count() === 0, "Declined care prevents clinical detail display");
    check((await page.locator("#patient-view-message").textContent()).includes("Care access is blocked"), "Blocked care has an explicit status message");
    check(calls.filter((call) => call.path === `/api/v1/patients/${PATIENT}`).length === detailReadsBeforeBlockedCase, "Declined care does not trigger a clinical detail read");

    await page.setViewportSize({ width: 390, height: 844 });
    check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), "Case details preserve mobile layout");
    check(errors.length === 0, "No browser exceptions, missing files or external requests");
    console.log(`PASS: ${checks} individual patient case checks.`);
    await context.close();
  } finally { await browser.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
