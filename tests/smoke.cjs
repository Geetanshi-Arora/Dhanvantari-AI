/* Development-only functional smoke checks using synthetic records. */
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const base = process.env.BASE_URL || "http://127.0.0.1:8000";
(async () => {
  const browser = await chromium.launch({
    executablePath: process.env.CHROMIUM_PATH || "/usr/bin/chromium",
    headless: true,
    args: ["--no-sandbox"],
  });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 1000 },
  });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("response", (response) => {
    if (response.status() >= 400)
      errors.push(`${response.status()} ${response.url()}`);
  });
  let checks = 0;
  function check(value, message) {
    assert.ok(value, message);
    checks++;
  }
  try {
    for (const path of [
      "index.html",
      "login.html",
      "language.html",
      "dashboard.html",
      "register.html",
    ]) {
      await page.goto(`${base}/${path}`);
      check((await page.locator("h1").count()) === 1, `${path} has a heading`);
      const hrefs = await page
        .locator("a[href]")
        .evaluateAll((links) =>
          links
            .map((a) => a.getAttribute("href"))
            .filter((h) => !h.startsWith("#")),
        );
      for (const href of new Set(hrefs)) {
        const response = await context.request.get(
          new URL(href, page.url()).href,
        );
        check(response.ok(), `${path}: link ${href} resolves`);
      }
    }
    await page.goto(base);
    await page.screenshot({
      path: "/tmp/dhanvantari-desktop.png",
      fullPage: true,
    });
    await page.getByRole("link", { name: "Explore the demo" }).click();
    await page.getByRole("button", { name: "Doctor", exact: false }).click();
    await page.getByRole("button", { name: "View doctor roadmap" }).click();
    check(
      await page.locator("#doctor-roadmap").isVisible(),
      "Doctor roadmap is clear",
    );
    await page.getByRole("button", { name: "Nurse", exact: false }).click();
    await page.getByLabel("Display name").fill("Nurse Demo");
    await page.getByRole("button", { name: "Enter nurse demo" }).click();
    await page.waitForURL("**/dashboard.html");
    check(
      (await page.locator("#greeting-name").textContent()) === "Nurse Demo",
      "Demo name rendered",
    );
    check(
      (await page.locator("#patient-rows tr").count()) === 3,
      "Three synthetic patients",
    );
    await page.getByRole("link", { name: "Register demo patient" }).click();
    check(
      await page.locator("#language-continue").isDisabled(),
      "Selection required",
    );
    await page.locator('[data-language="hi"]').click();
    check(
      (await page
        .locator('[data-language="hi"]')
        .getAttribute("aria-pressed")) === "true",
      "Language selection announced",
    );
    await page
      .getByRole("button", { name: "Continue to registration" })
      .click();
    await page.waitForURL("**/register.html");
    check(
      (await page.locator("#patient-language").inputValue()) === "hi",
      "Language retained",
    );
    await page.getByRole("button", { name: "Save demo patient" }).click();
    check(page.url().endsWith("register.html"), "Empty form blocked");
    await page.getByLabel("Fictional full name").fill("   ");
    await page.getByLabel("Age in years").fill("121");
    check(
      !(await page
        .locator("#patient-age")
        .evaluate((el) => el.checkValidity())),
      "Invalid age rejected",
    );
    await page.getByLabel("Age in years").fill("35");
    await page.locator('[name="consent"]').check();
    await page.getByRole("button", { name: "Save demo patient" }).click();
    check(
      !(await page
        .locator("#patient-name")
        .evaluate((el) => el.checkValidity())),
      "Whitespace name rejected",
    );
    const syntheticName = "<img src=x onerror=alert(1)> Demo";
    await page.getByLabel("Fictional full name").fill(syntheticName);
    await page
      .getByLabel("Relevant medical history")
      .fill("Synthetic history only");
    await page.getByRole("button", { name: "Save demo patient" }).click();
    await page.waitForURL("**/dashboard.html");
    check(
      (await page.locator("#patient-count").textContent()) === "4",
      "Patient saved",
    );
    check(
      (await page.locator("#patient-rows tr").count()) === 4,
      "Directory updated",
    );
    check(
      (await page.locator("#patient-rows img").count()) === 0,
      "Patient text cannot inject HTML",
    );
    await page.locator("#patient-search").fill(syntheticName);
    check(
      (await page.locator("#patient-rows tr").count()) === 1,
      "Search works",
    );
    await page.locator("#patient-search").fill("missing");
    check(
      await page.locator("#empty-search").isVisible(),
      "Empty result shown",
    );
    await page.reload();
    check(
      (await page.locator("#patient-count").textContent()) === "4",
      "Tab data survives reload",
    );
    await page.getByRole("button", { name: "Exit demo" }).click();
    await page.waitForURL("**/login.html");
    await page.goto(`${base}/dashboard.html`);
    check(
      (await page.locator("#patient-count").textContent()) === "3",
      "Exit clears demo registrations",
    );
    await page.setViewportSize({ width: 390, height: 844 });
    for (const path of [
      "index.html",
      "login.html",
      "language.html",
      "dashboard.html",
      "register.html",
    ]) {
      await page.goto(`${base}/${path}`);
      check(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= window.innerWidth,
        ),
        `${path}: no mobile horizontal overflow`,
      );
    }
    await page.getByRole("button", { name: "Toggle navigation" }).click();
    check(await page.locator("#navigation").isVisible(), "Mobile menu opens");
    await page.keyboard.press("Escape");
    check(
      !(await page.locator("#navigation").isVisible()),
      "Escape closes menu",
    );
    await page.goto(base);
    await page.screenshot({
      path: "/tmp/dhanvantari-mobile.png",
      fullPage: true,
    });
    check(errors.length === 0, `No browser errors: ${errors.join("; ")}`);
    console.log(
      `PASS: ${checks} checks; five pages, registration, roles, language, search, session lifecycle, mobile layout, and browser errors.`,
    );
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
