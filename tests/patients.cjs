/* Offline browser integration: intercepted static files, Auth and patient responses.
 * No listening server, database, Supabase or external request is permitted. */
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const origin = "http://localhost:8765";
const frontend = path.resolve(__dirname, "../frontend");
const first = "20000000-0000-4000-8000-000000000001";
const second = "20000000-0000-4000-8000-000000000002";
const nurse = { id: "10000000-0000-4000-8000-000000000001", display_name: "Nurse Fictional", role: "nurse" };
const row = (id = first, name = "Adult Fictional") => ({ id, name, reference: id === first ? "FICTIONAL-001" : "FICTIONAL-002", age: 36, age_recorded_on: "2026-01-01", preferred_language: "hi" });
let checks = 0;
const check = (value, message) => { assert.ok(value, message); checks++; };

(async () => {
  const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || "/usr/bin/chromium", headless: true, args: ["--no-sandbox"] });
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    const page = await context.newPage();
    const calls = [], errors = [];
    let replies = [], role = "nurse", meStatus = 200, holdMe, holdDirectory;
    let directoryCalls = 0, writes = 0;
    page.on("pageerror", (error) => errors.push(error.message));
    await context.addInitScript(() => {
      window.directoryRequests = [];
      const original = window.fetch;
      window.fetch = (url, options) => {
        if (String(url).startsWith("/api/v1/patients")) {
          window.directoryRequests.push({ url: String(url), method: options.method, credentials: options.credentials, cache: options.cache, redirect: options.redirect });
        }
        return original(url, options);
      };
    });
    await context.route("**/*", async (route) => {
      const request = route.request(), url = new URL(request.url());
      if (url.origin !== origin) { errors.push("External request blocked"); return route.abort(); }
      const respond = (status, body) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
      if (url.pathname.startsWith("/api/")) {
        if (request.method() !== "GET") writes++;
        calls.push(url.pathname);
        if (url.pathname === "/api/v1/auth/me") {
          if (holdMe) await holdMe;
          return respond(meStatus, meStatus === 200 ? { ...nurse, role } : {});
        }
        if (url.pathname === "/api/v1/patients") {
          directoryCalls++;
          check(url.searchParams.get("limit") === "20", "Directory requests bounded pages of 20");
          const reply = replies.shift() || { items: [], next_cursor: null };
          if (holdDirectory) await holdDirectory;
          if (reply.network) return route.abort("failed");
          return respond(reply.status || 200, reply.body || reply);
        }
        errors.push("Unexpected API endpoint");
        return respond(404, {});
      }
      const name = url.pathname === "/" ? "index.html" : decodeURIComponent(url.pathname.slice(1));
      const file = path.resolve(frontend, name);
      if (!file.startsWith(frontend + path.sep)) { errors.push("Unsafe static path"); return route.abort(); }
      try {
        return route.fulfill({ status: 200, body: await fs.readFile(file), contentType: { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml" }[path.extname(file)] });
      } catch { errors.push("Missing static file"); return route.abort(); }
    });
    async function go(repliesToUse) {
      replies = repliesToUse;
      await page.goto(`${origin}/dashboard.html`);
      await page.locator("#main").waitFor();
    }
    async function settled() { await page.waitForFunction(() => document.querySelector("#patient-directory").getAttribute("aria-busy") === "false"); }

    // Patient calls cannot precede /me; no sample rows are rendered while awaiting it.
    let release;
    holdMe = new Promise((resolve) => { release = resolve; });
    await page.goto(`${origin}/dashboard.html`, { waitUntil: "commit" });
    await page.locator("#auth-status").waitFor();
    check(directoryCalls === 0, "No patient request before verified nurse session");
    check(await page.locator("#main").isHidden(), "Workspace stays hidden before /me");
    replies = [{ items: [row()], next_cursor: null }];
    release(); holdMe = null;
    await page.locator("#patient-rows tr").waitFor();
    check(await page.locator("#patient-count").textContent() === "1", "API response populates loaded count");
    check((await page.locator("#patient-rows").textContent()).includes("Hindi"), "Stored patient language is displayed");
    const options = await page.evaluate(() => window.directoryRequests);
    check(options.every((r) => r.url.startsWith("/api/v1/patients?") && r.method === "GET" && r.credentials === "same-origin" && r.cache === "no-store" && r.redirect === "error"), "Relative GETs use same-origin credentials and no-store");

    holdDirectory = new Promise((resolve) => { release = resolve; });
    const beforeLoading = directoryCalls;
    await page.goto(`${origin}/dashboard.html`, { waitUntil: "commit" });
    await page.locator("#main").waitFor();
    await page.waitForFunction(() => document.querySelector("#directory-status").textContent.includes("Loading"));
    check(await page.locator("#patient-rows tr").count() === 0, "Loading never shows demo fallback");
    check(directoryCalls >= beforeLoading, "Loading state is shown during API request");
    release(); holdDirectory = null;
    await settled();

    const hostile = "<img src=x onerror=alert(1)> Fictional";
    await go([{ items: [row(first, hostile)], next_cursor: first }, { items: [row(first, hostile), row(second)], next_cursor: null }]);
    await page.locator("#patient-load-more").waitFor();
    await page.locator("#patient-load-more").click();
    await settled();
    check(await page.locator("#patient-rows tr").count() === 2, "Pagination deduplicates patient UUIDs");
    check(await page.locator("#patient-count").textContent() === "2", "Count reports unique loaded records");
    check(await page.locator("#patient-rows img").count() === 0, "Patient text cannot inject markup");
    check((await page.locator("#patient-rows").textContent()).includes(hostile), "Patient text renders literally");
    check(await page.locator("#patient-load-more").isHidden(), "Last page hides Load more");
    check((await page.locator("#directory-status").textContent()).includes("loaded records only"), "Search scope is explicit");
    check((await page.locator(".stats-grid").textContent()).includes("not a database total"), "Count is not presented as database total");
    const requests = await page.evaluate(() => window.directoryRequests);
    check(new URL(requests[1].url, origin).searchParams.get("cursor") === first, "Pagination sends returned UUID cursor");
    await page.locator("#patient-search").fill("FICTIONAL-002");
    check(await page.locator("#patient-rows tr").count() === 1, "Search filters loaded references");
    await page.locator("#patient-search").fill("absent");
    check(await page.locator("#empty-search").isVisible(), "Empty search is distinct from empty directory");
    check(!(await page.evaluate(() => JSON.stringify({ ...sessionStorage, ...localStorage }))).includes("FICTIONAL-001"), "API patient records are never stored");

    await go([{ items: [], next_cursor: null }]); await settled();
    check(await page.locator("#directory-empty").isVisible(), "Empty API result has an explicit empty state");
    check(await page.locator("#patient-count").textContent() === "0", "Empty directory reports zero loaded records");
    check(await page.locator("#empty-search").isHidden(), "Empty directory does not claim an unmatched search");

    const timeout = await page.evaluate(async () => {
      const { patientDirectory } = await import("/assets/js/patients-api.js");
      const fetch = window.fetch, timer = window.setTimeout;
      let delay, aborted = false;
      window.setTimeout = (callback, ms) => { delay = ms; return timer(callback, 1); };
      window.fetch = (url, options) => new Promise((resolve, reject) => {
        options.signal.addEventListener("abort", () => { aborted = true; reject(new Error("PRIVATE_TIMEOUT")); });
      });
      try { await patientDirectory(); return {}; }
      catch (error) { return { delay, aborted, message: error.message }; }
      finally { window.fetch = fetch; window.setTimeout = timer; }
    });
    check(timeout.delay === 15000 && timeout.aborted, "GET timeout aborts the actual fetch signal at a bounded deadline");
    check(!timeout.message.includes("PRIVATE_"), "Timeout errors are sanitized");

    // Forged/legacy demo state is never merged into directory results.
    await page.evaluate(() => sessionStorage.setItem("dhanvantari.phase1", JSON.stringify({ language: "hi", patients: [{ id: "DEMO-ATTACK", name: "PRIVATE_LEGACY", age: 36, language: "en" }] })));
    for (const reply of [
      { status: 403, body: { detail: "PRIVATE_MARKER" } },
      { status: 404, body: { detail: "PRIVATE_MARKER" } },
      { status: 503, body: { detail: "PRIVATE_MARKER" } }, { network: true },
      { items: [{ ...row(), preferred_language: ["en"] }], next_cursor: null },
      { items: [row()], next_cursor: "https://forbidden.invalid" },
    ]) {
      await go([reply]); await page.locator("#directory-error").waitFor();
      check(await page.locator("#patient-rows tr").count() === 0, "Failure/malformed result leaves no patient records");
      check(!(await page.locator("#main").textContent()).includes("PRIVATE_"), "Error messages do not echo backend or legacy data");
      check(await page.locator("#patient-retry").isVisible(), "Safe retry offered after directory failure");
    }
    check(!(await page.evaluate(() => sessionStorage.getItem("dhanvantari.phase1"))).includes("PRIVATE_LEGACY"), "Legacy demo records are removed while language preference is retained");
    replies = [{ items: [row()], next_cursor: null }];
    await page.locator("#patient-retry").click(); await page.locator("#patient-rows tr").waitFor();
    check(await page.locator("#directory-error").isHidden(), "Retry can recover without demo fallback");

    await go([{ items: [row()], next_cursor: first }, { status: 503, body: {} }]);
    await page.locator("#patient-load-more").click(); await page.locator("#directory-error").waitFor();
    check(await page.locator("#patient-rows tr").count() === 0, "Pagination failure clears previously displayed records");

    await go([{ items: [row()], next_cursor: first }, { status: 401, body: {} }]);
    await page.locator("#patient-load-more").click();
    await page.waitForURL("**/login.html?reason=expired");
    check(await page.locator("#patient-rows").count() === 0, "Expired session navigates away from all patient information");
    check(await page.evaluate(() => sessionStorage.getItem("dhanvantari.phase1")) === null, "401 clears tab state");

    role = "doctor";
    const beforeDoctor = directoryCalls;
    await page.goto(`${origin}/dashboard.html`); await page.waitForURL("**/doctor.html");
    check(directoryCalls === beforeDoctor, "Doctor workspace cannot load nurse directory");
    role = "nurse"; meStatus = 401;
    const beforeMissing = directoryCalls;
    await page.goto(`${origin}/dashboard.html`); await page.waitForURL("**/login.html?reason=expired");
    check(directoryCalls === beforeMissing, "Missing session cannot request directory data");
    meStatus = 200;

    // Even re-enabling the button cannot resurrect the deleted demo save handler.
    await page.goto(`${origin}/register.html`); await page.locator("#main").waitFor();
    check(await page.locator('#registration-form button[type="submit"]').isDisabled(), "Registration submission remains disabled");
    check(await page.locator("#patient-history").isDisabled(), "Clinical preview starts disabled without a care selection");
    check((await page.locator("#registration-error").textContent()).includes("approved consent notices"), "Registration explains its fail-closed notice gate");
    const beforeSubmit = await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage }));
    await page.locator("#registration-form").evaluate((form) => {
      form.querySelectorAll("input, select, textarea, button").forEach((control) => { control.disabled = false; });
      form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    });
    check(page.url().endsWith("register.html"), "Forced submit cannot navigate or save a demo patient");
    check(await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage })) === beforeSubmit, "Forced submit does not persist a patient");
    check(writes === 0, "Read-only integration makes no API writes or consent submissions");
    await page.setViewportSize({ width: 390, height: 844 });
    await go([{ items: [row()], next_cursor: null }]); await settled();
    check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), "Patient directory preserves mobile layout");
    check(errors.length === 0, "No browser exceptions, missing assets or external requests");
    console.log(`PASS: ${checks} focused offline patient directory checks.`);
    await context.close();
  } finally { await browser.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
