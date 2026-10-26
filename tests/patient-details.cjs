/* Offline patient reads: all requests intercepted; fictional data, no listening server. */
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const origin = "http://localhost:8765", frontend = path.resolve(__dirname, "../frontend");
const A = "20000000-0000-4000-8000-000000000001", B = "20000000-0000-4000-8000-000000000002";
const NOW = "2026-01-01T00:00:00+00:00";
const row = (id) => ({ id, name: id === A ? "Fictional Alpha" : "Fictional Beta", reference: id === A ? "FICTIONAL-A" : "FICTIONAL-B", age: 36, age_recorded_on: "2026-01-01", preferred_language: "hi" });
const detail = (id) => ({ ...row(id), created_at: NOW, sex: null, contact_reference: null, history: `Fictional history ${id === A ? "ALPHA" : "BETA"}`, allergies: null, current_medicines: null });
const status = (id, care = "granted") => ({ id, reference: row(id).reference, preferred_language: "hi", created_at: NOW, care_access: care === "granted" ? "enabled" : "blocked", consents: [{ id: "30000000-0000-4000-8000-000000000001", purpose: "care", status: care, granted: care !== "declined", notice_version: "TEST-ONLY-HISTORICAL", language: "en", created_at: NOW, revoked_at: care === "revoked" ? "2026-01-02T00:00:00+00:00" : null }] });
let checks = 0;
const check = (value, message) => { assert.ok(value, message); checks++; };

(async () => {
  const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || "/usr/bin/chromium", headless: true, args: ["--no-sandbox"] });
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    const page = await context.newPage();
    const errors = [], calls = [];
    let replyStatus = () => ({ body: status(A) }), replyDetail = (id) => ({ body: detail(id) });
    page.on("pageerror", (error) => errors.push(error.message));
    await context.addInitScript(() => {
      window.patientReadOptions = [];
      const fetch = window.fetch;
      window.fetch = (url, options) => {
        if (String(url).startsWith("/api/v1/patients/")) window.patientReadOptions.push({ method: options.method, credentials: options.credentials, cache: options.cache, redirect: options.redirect });
        return fetch(url, options);
      };
    });
    await context.route("**/*", async (route) => {
      const request = route.request(), url = new URL(request.url());
      if (url.origin !== origin) { errors.push("External request blocked"); return route.abort(); }
      if (url.pathname.startsWith("/api/")) {
        calls.push({ path: url.pathname, method: request.method() });
        const respond = (code, body) => route.fulfill({ status: code, contentType: "application/json", body: JSON.stringify(body) });
        if (url.pathname === "/api/v1/auth/me") return respond(200, { id: "10000000-0000-4000-8000-000000000001", display_name: "Nurse Fictional", role: "nurse" });
        if (url.pathname === "/api/v1/patients") return respond(200, { items: [row(A), row(B)], next_cursor: null });
        const match = url.pathname.match(/^\/api\/v1\/patients\/([^/]+)(\/registration)?$/);
        if (match) {
          const reply = match[2] ? replyStatus(match[1]) : replyDetail(match[1]);
          if (reply.network) return route.abort("failed");
          return respond(reply.code || 200, reply.body || { detail: "PRIVATE_ERROR_MARKER" });
        }
        errors.push("Unexpected API request"); return respond(404, {});
      }
      const file = path.resolve(frontend, url.pathname === "/" ? "index.html" : decodeURIComponent(url.pathname.slice(1)));
      if (!file.startsWith(frontend + path.sep)) return route.abort();
      try { return route.fulfill({ status: 200, body: await fs.readFile(file), contentType: { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml" }[path.extname(file)] }); }
      catch { errors.push("Missing asset"); return route.abort(); }
    });
    async function go() {
      replyStatus = (id) => ({ body: status(id) }); replyDetail = (id) => ({ body: detail(id) });
      await page.goto(`${origin}/dashboard.html`); await page.getByRole("button", { name: "View record FICTIONAL-A", exact: true }).waitFor();
    }
    const select = (ref) => page.getByRole("button", { name: `View record FICTIONAL-${ref}`, exact: true }).click();
    const ready = () => page.waitForFunction(() => document.querySelector("#patient-view-message").textContent === "Read-only patient record.");
    const detailCalls = () => calls.filter((call) => /^\/api\/v1\/patients\/[^/]+$/.test(call.path)).length;

    await go();
    check(await page.locator("#patient-view").isHidden(), "No detail is shown until a patient is selected");
    await select("A"); await ready();
    check(await page.getByLabel("Name", { exact: true }).inputValue() === "Fictional Alpha", "Authorized detail displays actual response fields");
    check(await page.getByLabel("History", { exact: true }).inputValue() === "Fictional history ALPHA", "Clinical history is displayed only after successful reads");
    check(await page.getByLabel("Sex", { exact: true }).inputValue() === "Not recorded", "Nullable detail fields are represented safely");
    check(await page.getByLabel("Patient language", { exact: true }).inputValue() === "Hindi", "Patient language is preserved independently of notice language");
    check((await page.locator("#patient-registration-content").textContent()).includes("TEST-ONLY-HISTORICAL"), "Historical notice metadata comes only from server response");
    check((await page.locator("#patient-registration-content").textContent()).includes("Not recorded"), "Absent optional consent decisions are not labeled declined");
    check(await page.locator("#patient-view textarea").evaluateAll((fields) => fields.every((field) => field.readOnly)), "All displayed fields are read-only");
    check(!calls.slice(0, 3).some((call) => call.method !== "GET"), "Read flow uses GET only");
    const options = await page.evaluate(() => window.patientReadOptions);
    check(options.every((item) => item.method === "GET" && item.credentials === "same-origin" && item.cache === "no-store" && item.redirect === "error"), "Both helpers retain same-origin/no-store request protections");
    const initialReads = calls.filter((call) => call.path.startsWith(`/api/v1/patients/${A}`));
    check(initialReads[0].path.endsWith("/registration") && initialReads[1].path.endsWith(A), "Registration status is checked before clinical details");
    await page.getByRole("button", { name: "Close record" }).click();
    check(await page.locator("#patient-view").isHidden() && await page.locator("#patient-detail-content textarea").count() === 0, "Close removes clinical DOM, not merely visibility");

    const hostile = "<img src=x onerror=alert(1)> Fictional";
    replyDetail = (id) => ({ body: { ...detail(id), name: hostile, history: hostile } });
    await select("A"); await ready();
    check(await page.locator("#patient-view img, #patient-view script").count() === 0, "Patient content cannot introduce HTML");
    check(await page.getByLabel("Name", { exact: true }).inputValue() === hostile, "Patient-controlled content renders literally");
    check(!(await page.evaluate(() => JSON.stringify({ ...sessionStorage, ...localStorage }))).includes(hostile), "Patient details are never written to browser storage");

    for (const care of ["declined", "revoked"]) {
      await go();
      replyStatus = (id) => ({ body: { ...status(id, care), history: "PRIVATE_STATUS_CLINICAL", name: "PRIVATE_STATUS_NAME" } });
      const before = detailCalls();
      await select("A");
      await page.waitForFunction(() => document.querySelector("#patient-view-message").textContent.includes("Care access blocked"));
      check(detailCalls() === before, `${care} care never triggers a clinical-detail request`);
      check(await page.locator("#patient-detail-content textarea").count() === 0, `${care} care exposes no clinical values`);
      const text = await page.locator("#patient-registration-content").textContent();
      check(text.includes(care) && !text.includes("PRIVATE_STATUS"), `${care} displays only allowlisted registration metadata`);
      check(await page.getByRole("button", { name: "View record FICTIONAL-A", exact: true }).count() === 0, "Blocked-care patient is removed from stale directory rows");
    }

    for (const stage of ["status", "detail"]) {
      let inaccessibleMessage;
      for (const code of [403, 404, 503, "network"]) {
        await go(); await select("A"); await ready();
        const fail = () => code === "network" ? { network: true } : { code };
        if (stage === "status") replyStatus = fail; else replyDetail = fail;
        const before = detailCalls();
        await select("B");
        await page.waitForFunction(() => !document.querySelector("#patient-view-message").textContent.includes("Loading"));
        const message = await page.locator("#patient-view-message").textContent();
        check(await page.locator("#patient-detail-content textarea, #patient-registration-content textarea").count() === 0, `${stage} ${code} clears previous clinical and status data`);
        check(!message.includes("PRIVATE_") && !message.includes("Fictional"), "Safe error contains no server detail or identifying values");
        if (stage === "status") check(detailCalls() === before, "Failed authorization/status never proceeds to detail");
        if ([403, 404].includes(code)) {
          check(message === "Patient record unavailable.", "403 and 404 share the same non-enumerating message");
          if (inaccessibleMessage) check(message === inaccessibleMessage, "Inaccessible existence cannot be distinguished");
          inaccessibleMessage = message;
          check(await page.locator("#patient-rows tr").count() === 0, "Access denial clears stale directory information too");
        }
      }
    }

    await go();
    replyStatus = () => ({ body: status(B) });
    const beforeMismatch = detailCalls();
    await select("A");
    await page.waitForFunction(() => document.querySelector("#patient-view-message").textContent.startsWith("Could not"));
    check(detailCalls() === beforeMismatch, "Mismatched status UUID fails closed before clinical access");
    await go();
    const beforeInvalid = calls.length;
    const rejected = await page.evaluate(async () => {
      const api = await import("/assets/js/patients-api.js");
      let rejected = 0;
      for (const read of [api.patientDetail, api.patientRegistration]) {
        try { await read("https://forbidden.invalid/PRIVATE_MARKER"); } catch { rejected++; }
      }
      return rejected;
    });
    check(rejected === 2 && calls.length === beforeInvalid, "Both helpers reject invalid UUIDs before constructing/fetching URLs");

    // Deliberately ignore AbortSignal: generation guards must still reject late results.
    await page.evaluate(() => {
      const fetch = window.fetch;
      window.pendingReads = [];
      window.fetch = (url, options) => String(url).startsWith("/api/v1/patients/")
        ? new Promise((resolve) => window.pendingReads.push({ url: String(url), resolve })) : fetch(url, options);
    });
    async function resolve(id, registration, value) {
      const url = `/api/v1/patients/${id}${registration ? "/registration" : ""}`;
      await page.waitForFunction((url) => window.pendingReads.some((item) => item.url === url), url);
      await page.evaluate(({ url, value }) => {
        const index = window.pendingReads.findIndex((item) => item.url === url);
        window.pendingReads.splice(index, 1)[0].resolve(new Response(JSON.stringify(value), { status: 200 }));
      }, { url, value });
    }
    await select("A"); await select("B");
    check(await page.locator("#patient-detail-content textarea").count() === 0, "Rapid switching immediately clears clinical DOM");
    await resolve(B, true, status(B)); await resolve(B, false, detail(B)); await ready();
    await resolve(A, true, status(A));
    check(await page.getByLabel("Name", { exact: true }).inputValue() === "Fictional Beta", "Late status cannot replace a newer patient's display");
    check(await page.evaluate((id) => !window.pendingReads.some((item) => item.url === `/api/v1/patients/${id}`), A), "Late status cannot trigger a stale clinical request");
    await select("A");
    check(await page.locator("#patient-detail-content textarea").count() === 0, "Switch clears prior details before the next response");
    await resolve(A, true, status(A));
    await select("B"); await resolve(B, true, status(B)); await resolve(B, false, detail(B)); await ready();
    await resolve(A, false, detail(A));
    check(await page.getByLabel("History", { exact: true }).inputValue() === "Fictional history BETA", "Late clinical response cannot restore the old patient");
    await select("A"); await resolve(A, true, status(A));
    await page.getByRole("button", { name: "Close record" }).click();
    await resolve(A, false, detail(A));
    check(await page.locator("#patient-view").isHidden() && await page.locator("#patient-detail-content textarea").count() === 0, "Late clinical response cannot reopen a closed panel");

    await go();
    replyDetail = () => ({ code: 401 });
    await select("A"); await page.waitForURL("**/login.html?reason=expired");
    check(await page.locator("#patient-view").count() === 0, "Session expiry removes all patient content and follows existing sign-in flow");
    check(await page.evaluate(() => sessionStorage.getItem("dhanvantari.phase1")) === null, "Session expiry clears tab state");
    await go(); await select("A"); await ready();
    await page.setViewportSize({ width: 390, height: 844 });
    check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), "Detail/status panel preserves responsive layout");
    check(calls.every((call) => call.method === "GET"), "No registration or consent writes were introduced");
    check(errors.length === 0, "No browser exceptions, external requests or missing assets");
    console.log(`PASS: ${checks} offline patient detail/status checks.`);
    await context.close();
  } finally { await browser.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
