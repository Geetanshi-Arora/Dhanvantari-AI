/* Offline fictional registration preview. Intercept all requests; no backend or database. */
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const origin = "http://localhost:8765";
const frontend = path.resolve(__dirname, "../frontend");
let checks = 0;
const check = (value, message) => { assert.ok(value, message); checks++; };

(async () => {
  const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || "/usr/bin/chromium", headless: true, args: ["--no-sandbox"] });
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    const page = await context.newPage();
    const errors = [], requests = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await context.route("**/*", async (route) => {
      const request = route.request(), url = new URL(request.url());
      if (url.origin !== origin) { errors.push("External request blocked"); return route.abort(); }
      if (url.pathname.startsWith("/api/")) {
        requests.push({ path: url.pathname, method: request.method() });
        if (url.pathname !== "/api/v1/auth/me" || request.method() !== "GET") {
          errors.push("Unexpected API request"); return route.abort();
        }
        return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({
          id: "10000000-0000-4000-8000-000000000001", display_name: "Nurse Fictional", role: "nurse"
        }) });
      }
      const file = path.resolve(frontend, decodeURIComponent(url.pathname.slice(1)));
      if (!file.startsWith(frontend + path.sep)) { errors.push("Unsafe file path"); return route.abort(); }
      try {
        return route.fulfill({ status: 200, body: await fs.readFile(file), contentType: {
          ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml"
        }[path.extname(file)] });
      } catch { errors.push("Missing static file"); return route.abort(); }
    });
    await page.goto(`${origin}/register.html`);
    await page.locator("#main").waitFor();
    const form = page.locator("#registration-form");
    check(await page.locator("#consultation-date, #registration-form [type=date]").count() === 0, "Obsolete visit date is absent");
    check(await form.locator("[type=checkbox]").count() === 0, "No checkbox is treated as consent evidence");
    check(await form.evaluate((form) => [...form.querySelectorAll("input,select,textarea")].every((control) => control.labels?.length > 0)), "Every field has an associated accessible label");
    check(await form.evaluate((form) => [...form.querySelectorAll("[aria-describedby]")].every((control) => control.getAttribute("aria-describedby").split(/\s+/).every((id) => document.getElementById(id)))), "Help and validation descriptions resolve to existing content");
    check((await form.locator("[required]").evaluateAll((controls) => controls.map((control) => control.name).sort())).join(",") === "age,care_consent,name,preferred_language", "Required fields are explicit and optional purposes remain optional");
    const names = await form.locator("input,select,textarea").evaluateAll((controls) => controls.map((control) => control.name));
    check(["name", "age", "preferred_language", "sex", "contact_reference", "history", "allergies", "current_medicines"].every((name) => names.includes(name)), "Patient field names match backend contracts");
    check(!names.some((name) => /notice_version|recorded_by|registered_by|representative|guardian/.test(name)), "No invented notice metadata, actor fields or representative workflow");
    check((await page.locator("#registration-error").textContent()).includes("approved consent notices are published"), "Unavailable state explains notice publication requirement");
    check((await page.locator("#consent-preview-help").textContent()).includes("do not record"), "Preview choices do not claim to record consent");
    check(await form.locator('[type="submit"]').isDisabled(), "Submission is disabled in HTML and JavaScript");

    for (const value of ["", "0", "17", "18.5", "121"]) {
      await page.locator("#patient-age").fill(value);
      const result = await page.locator("#patient-age").evaluate((control) => ({ valid: control.checkValidity(), message: control.validationMessage }));
      check(!result.valid && result.message.includes("whole-number age from 18 to 120"), `Age ${value || "empty"} rejected with a fixed message`);
    }
    for (const value of ["18", "35", "120"]) {
      await page.locator("#patient-age").fill(value);
      check(await page.locator("#patient-age").evaluate((control) => control.checkValidity()), `Integer age ${value} accepted`);
    }
    await page.locator("#patient-name").fill("   ");
    check(!(await page.locator("#patient-name").evaluate((control) => control.checkValidity())), "Whitespace-only name rejected");
    check((await page.locator("#patient-name").evaluate((control) => control.validationMessage)).includes("fictional patient name"), "Name validation gives a clear fixed instruction");
    await page.locator("#patient-name").fill("Adult Fictional");
    check(await page.locator("#patient-name").evaluate((control) => control.checkValidity()), "Fictional name accepted");
    const languageCodes = ["en", "hi", "bn", "ta", "te", "mr", "gu", "pa"];
    check((await page.locator("#patient-language option").evaluateAll((options) => options.map((option) => option.value).filter(Boolean))).join(",") === languageCodes.join(","), "Exactly the eight supported language codes remain");
    for (const code of languageCodes) {
      await page.locator("#patient-language").selectOption(code);
      check(await page.locator("#patient-language").inputValue() === code && await page.locator("#patient-language").evaluate((control) => control.checkValidity()), `Language ${code} retained and valid`);
    }
    await page.locator("#patient-language").selectOption("");
    check(!(await page.locator("#patient-language").evaluate((control) => control.checkValidity())), "Patient language cannot be omitted");
    await page.locator("#patient-language").selectOption("hi");
    check((await page.locator("#patient-sex option").evaluateAll((options) => options.map((option) => option.value))).join("|") === "|Female|Male|Intersex|Prefer not to say", "Optional sex values match backend enumeration");
    for (const [id, limit] of [["patient-name", 80], ["patient-contact", 60], ["patient-history", 1000], ["patient-allergies", 500], ["patient-medicines", 500]]) {
      check(await page.locator(`#${id}`).getAttribute("maxlength") === String(limit), `${id} has the contract length limit`);
    }

    check(await page.locator("#consent-care").inputValue() === "", "Care has no default grant or decline");
    check(!(await page.locator("#consent-care").evaluate((control) => control.checkValidity())), "Care requires an explicit decision");
    for (const id of ["consent-care", "consent-uploads", "consent-future-ai"]) {
      check((await page.locator(`#${id} option`).evaluateAll((options) => options.map((option) => option.value))).join("|") === "|granted|declined", `${id} has separate blank, grant and decline states`);
    }
    for (const id of ["consent-uploads", "consent-future-ai"]) {
      check(await page.locator(`#${id}`).inputValue() === "" && await page.locator(`#${id}`).evaluate((control) => control.checkValidity()), `${id} is initially unanswered and valid`);
      for (const value of ["granted", "declined", ""]) {
        await page.locator(`#${id}`).selectOption(value);
        check(await page.locator(`#${id}`).inputValue() === value, `${id} preserves explicit ${value || "unanswered"} choice`);
      }
    }
    for (const id of ["patient-history", "patient-allergies", "patient-medicines"]) check(await page.locator(`#${id}`).isDisabled(), `${id} starts blocked without care`);
    await page.locator("#consent-care").selectOption("granted");
    check(await page.locator("#consent-uploads").inputValue() === "" && await page.locator("#consent-future-ai").inputValue() === "", "Granting care never grants or declines optional purposes");
    for (const id of ["patient-sex", "patient-contact", "patient-history", "patient-allergies", "patient-medicines"]) {
      check(await page.locator(`#${id}`).evaluate((control) => !control.required && control.checkValidity()), `${id} can remain empty`);
    }
    for (const id of ["patient-contact", "patient-history", "patient-allergies", "patient-medicines"]) {
      const control = page.locator(`#${id}`);
      await control.evaluate((element) => { element.value = "x".repeat(element.maxLength + 1); element.dispatchEvent(new Event("input", { bubbles: true })); });
      check(!(await control.evaluate((element) => element.checkValidity())), `${id} also rejects programmatically overlong input`);
      await control.fill("Fictional detail");
    }
    await page.locator("#patient-contact").evaluate((element) => { element.value = "Fictional\0"; element.dispatchEvent(new Event("input", { bubbles: true })); });
    check(!(await page.locator("#patient-contact").evaluate((element) => element.checkValidity())), "Unsupported NUL text is rejected without echoing input");
    await page.locator("#patient-contact").fill("");
    await page.locator("#consent-uploads").selectOption("granted");
    await page.locator("#consent-future-ai").selectOption("declined");
    await page.locator("#consent-care").selectOption("declined");
    for (const id of ["patient-history", "patient-allergies", "patient-medicines"]) {
      check(await page.locator(`#${id}`).isDisabled() && await page.locator(`#${id}`).inputValue() === "", `${id} is cleared and disabled on care decline`);
    }
    check(await page.locator("#consent-uploads").inputValue() === "granted" && await page.locator("#consent-future-ai").inputValue() === "declined", "Care changes do not silently rewrite optional decisions");
    check(await page.locator("#patient-contact").isEnabled(), "Minimal administrative fields remain available on care decline");
    await page.locator("#consent-care").selectOption("granted");
    for (const id of ["patient-history", "patient-allergies", "patient-medicines"]) check(await page.locator(`#${id}`).inputValue() === "", `${id} is not silently restored after regrant`);
    await page.locator("#patient-history").fill("Fictional history");
    await page.locator("#consent-care").selectOption("");
    check(await page.locator("#patient-history").isDisabled() && await page.locator("#patient-history").inputValue() === "", "Returning care to unanswered also clears clinical information");
    await page.locator("#consent-care").selectOption("granted");
    check(await form.evaluate((element) => element.checkValidity()), "Valid preview fields can pass validation independently of the notice gate");
    check(await form.locator('[type="submit"]').isDisabled(), "Valid fields never enable submission without notices");
    const before = await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage }));
    await page.locator("#patient-name").press("Enter");
    check(page.url().endsWith("/register.html"), "Implicit Enter cannot submit the locked form");
    const cancelled = await form.evaluate((form) => {
      form.querySelector('[type="submit"]').disabled = false;
      const cancelled = !form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
      form.requestSubmit();
      form.querySelector('[type="submit"]').disabled = true;
      return cancelled;
    });
    check(cancelled && page.url().endsWith("/register.html"), "Forced submit is prevented without constructing an API request");
    check(await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage })) === before, "Preview and consent choices are never persisted");
    check(requests.every((request) => request.method === "GET" && request.path === "/api/v1/auth/me"), "No patient POST, consent mutation or other write is attempted");
    await page.reload(); await page.locator("#main").waitFor();
    check(await page.locator("#patient-name").inputValue() === "" && await page.locator("#consent-care").inputValue() === "", "Reload does not restore patient fields or consent choices");
    await page.setViewportSize({ width: 390, height: 844 });
    check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), "Aligned form and separate consent controls fit the mobile layout");
    check(errors.length === 0, "No browser exceptions, missing assets or external requests");
    console.log(`PASS: ${checks} offline registration alignment checks.`);
    await context.close();
  } finally { await browser.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
