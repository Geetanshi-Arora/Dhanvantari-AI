/* Browser contract checks for the synthetic-only, database-backed registration flow. */
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const origin = "http://localhost:8765";
const frontend = path.resolve(__dirname, "../frontend");
const nurse = { id: "10000000-0000-4000-8000-000000000001", display_name: "Nurse Fictional", role: "nurse" };
const patientId = "20000000-0000-4000-8000-000000000001";
const caseId = "30000000-0000-4000-8000-000000000001";
const now = "2026-01-01T00:00:00Z";
let checks = 0;
const check = (value, message) => { assert.ok(value, message); checks++; };

(async () => {
  const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || "/usr/bin/chromium", headless: true, args: ["--no-sandbox"] });
  try {
    const context = await browser.newContext();
    const page = await context.newPage();
    const calls = [], errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await context.route("**/*", async (route) => {
      const request = route.request(), url = new URL(request.url());
      if (url.origin !== origin) { errors.push("External request"); return route.abort(); }
      if (url.pathname.startsWith("/api/")) {
        calls.push({ path: url.pathname, method: request.method(), body: request.method() === "POST" ? request.postDataJSON() : null, headers: request.headers() });
        if (url.pathname === "/api/v1/auth/me") return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(nurse) });
        if (url.pathname === "/api/v1/auth/csrf") return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ csrf_token: "fictional-csrf" }) });
        if (url.pathname === "/api/v1/patients/consent-notices") {
          const language = url.searchParams.get("language");
          return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ items: ["care", "uploads", "future_ai"].map((purpose) => ({ purpose, notice_version: "DRAFT-SYNTHETIC-1", language, publication_state: "draft", content_state: "not_provided", content: null, synthetic_only: true })) }) });
        }
        if (url.pathname === "/api/v1/patients" && request.method() === "POST") {
          check(request.headers()["x-csrf-token"] === "fictional-csrf", "Registration sends the existing CSRF token");
          check(Boolean(request.headers()["idempotency-key"]), "Registration sends an idempotency key");
          const body = request.postDataJSON();
          check(body.preferred_language === "hi", "Patient language is sent to the API");
          check(body.consents[0].notice_version === "DRAFT-SYNTHETIC-1" && body.consents[0].language === "hi", "Draft consent version and language are explicit");
          return route.fulfill({ status: 201, contentType: "application/json", body: JSON.stringify({
            id: patientId, case_id: caseId, reference: "DV-200000000000000000000000000", preferred_language: "hi",
            created_at: now, care_access: "enabled", consents: [{ id: "40000000-0000-4000-8000-000000000001", purpose: "care", status: "granted", granted: true, notice_version: "DRAFT-SYNTHETIC-1", language: "hi", created_at: now, revoked_at: null }],
          }) });
        }
        errors.push(`Unexpected API request ${request.method()} ${url.pathname}`);
        return route.fulfill({ status: 404, contentType: "application/json", body: "{}" });
      }
      const file = path.resolve(frontend, decodeURIComponent(url.pathname.slice(1)));
      if (!file.startsWith(frontend + path.sep)) return route.abort();
      try { return route.fulfill({ status: 200, body: await fs.readFile(file), contentType: { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml" }[path.extname(file)] }); }
      catch { errors.push("Missing asset"); return route.abort(); }
    });
    await page.goto(`${origin}/register.html`);
    await page.locator("#main").waitFor();
    await page.locator("#patient-language").selectOption("hi");
    await page.locator("#consent-draft-notice").waitFor();
    check(await page.locator("#registration-form").getAttribute("aria-disabled") === null, "Authenticated registration form is enabled");
    check((await page.locator("#consent-draft-notice").textContent()).includes("DRAFT CONSENT"), "Draft status is prominently disclosed");
    check((await page.locator("#consent-draft-notice").textContent()).includes("not valid for real-patient use"), "Draft notice cannot be mistaken for approved consent");
    check((await page.locator("#consent-draft-notice").textContent()).includes("text has not been supplied"), "No consent wording is invented");
    await page.locator("#patient-name").fill("Adult Fictional");
    await page.locator("#patient-age").fill("36");
    await page.locator("#consent-care").selectOption("granted");
    await page.getByRole("button", { name: "Register synthetic patient" }).click();
    await page.locator("#registration-success").waitFor();
    check((await page.locator("#registration-success").textContent()).includes(caseId), "Successful response displays generated case ID");
    const writes = calls.filter((call) => call.path === "/api/v1/patients" && call.method === "POST");
    check(writes.length === 1, "Form submits once to the backend registration API");
    check(calls.every((call) => call.path.startsWith("/api/v1/")), "Only same-origin application APIs are used");
    check(await page.evaluate(() => JSON.stringify({ ...sessionStorage, ...localStorage })) === "{}", "Patient information is not persisted in browser storage");
    check(errors.length === 0, "No browser exceptions or unexpected requests");
    console.log(`PASS: ${checks} synthetic registration API checks.`);
    await context.close();
  } finally { await browser.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
