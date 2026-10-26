/* Built-in Node tests only: actual frontend modules in an isolated VM with DOM,
 * Auth and patient-response doubles. No browser, services, network or devices. */
const assert = require("node:assert/strict");
const test = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const base = path.resolve(__dirname, "../frontend");
const html = fs.readFileSync(path.join(base, "dashboard.html"), "utf8");
const voiceSource = fs.readFileSync(path.join(base, "assets/js/voice-demo.js"), "utf8");
const langs = ["en", "hi", "bn", "ta", "te", "mr", "gu", "pa"];

class Element {
  constructor(id = "", owner = null) {
    this.id = id; this.owner = owner; this.value = ""; this.disabled = false;
    this.hidden = false; this.dataset = {}; this.attributes = new Map();
    this.listeners = new Map(); this.children = []; this.text = "";
    this.classList = { toggle() {} };
  }
  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() { return this.text + this.children.map(child => child.textContent).join(""); }
  set innerHTML(_) { throw new Error("Unsafe HTML rendering is forbidden"); }
  get innerHTML() { throw new Error("HTML rendering is forbidden"); }
  setAttribute(key, value) { this.attributes.set(key, String(value)); }
  getAttribute(key) { return this.attributes.get(key) ?? null; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.text = ""; this.children = [...items]; }
  focus() { if (this.owner) this.owner.activeElement = this; }
  addEventListener(type, callback) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(callback);
  }
  async emit(type) {
    if (type === "click" && this.disabled) return;
    for (const callback of this.listeners.get(type) || []) {
      await callback({ target: this, preventDefault() {} });
    }
  }
}
function documentDouble(markup = html) {
  const doc = { nodes: new Map(), activeElement: null, listeners: new Map() };
  for (const match of markup.matchAll(/<[^>]+\bid="([^"]+)"[^>]*>/g)) {
    const node = new Element(match[1], doc);
    node.disabled = /\sdisabled(?:\s|>)/.test(match[0]);
    node.hidden = /\shidden(?:\s|>)/.test(match[0]);
    doc.nodes.set(node.id, node);
  }
  doc.body = new Element("body", doc);
  doc.body.dataset.requiredRole = "nurse";
  doc.bar = new Element("bar", doc);
  doc.logout = new Element("logout", doc);
  doc.querySelector = selector => selector.startsWith("#") ? doc.nodes.get(selector.slice(1)) || null
    : selector === ".workspace-bar" ? doc.bar : null;
  doc.querySelectorAll = selector => selector === "[data-logout]" ? [doc.logout] : [];
  doc.createElement = () => new Element("", doc);
  doc.addEventListener = (type, callback) => doc.listeners.set(type, callback);
  const root = doc.nodes.get("voice-demo");
  if (root) root.querySelector = selector => doc.nodes.get(selector.slice(1)) || null;
  return doc;
}
function sandbox(doc, responder = () => { throw new Error("Unexpected network request"); }) {
  const calls = [], redirects = [], writes = [], timers = new Map(), windowEvents = new Map();
  let timerId = 0;
  const fail = () => { throw new Error("Device, playback, storage or network operation forbidden"); };
  const window = { location: { search: "", replace: value => redirects.push(value), reload() {} },
    addEventListener: (type, callback) => windowEvents.set(type, callback) };
  Object.defineProperty(window, "speechSynthesis", { get: fail });
  const environment = {
    document: doc, window, AbortController, URLSearchParams, URL,
    navigator: { get mediaDevices() { return fail(); } },
    get MediaRecorder() { return fail(); }, get Audio() { return fail(); },
    get speechSynthesis() { return fail(); }, get SpeechSynthesisUtterance() { return fail(); },
    get localStorage() { return fail(); },
    sessionStorage: { getItem: () => null, setItem: (...args) => { writes.push(args); fail(); }, removeItem: () => writes.push("clear") },
    fetch: async (url, options) => { calls.push({ url, options }); return responder(url, options); },
    setTimeout: fn => { timers.set(++timerId, fn); return timerId; },
    clearTimeout: id => timers.delete(id),
    console: { log: fail, warn: fail, error: fail },
  };
  const context = vm.createContext(environment);
  return { context, calls, redirects, writes, timers, windowEvents };
}
async function loadModule(filename, context, cache = new Map()) {
  if (cache.has(filename)) return cache.get(filename);
  const module = new vm.SourceTextModule(fs.readFileSync(filename, "utf8"), { context, identifier: filename });
  cache.set(filename, module);
  await module.link(specifier => loadModule(path.resolve(path.dirname(filename), specifier), context, cache));
  return module;
}
async function fixture() {
  const doc = documentDouble();
  const env = sandbox(doc);
  const module = await loadModule(path.join(base, "assets/js/voice-demo.js"), env.context);
  await module.evaluate();
  const api = module.namespace.initializeVoiceDemo(doc.nodes.get("voice-demo"));
  const node = suffix => doc.nodes.get(`voice-demo-${suffix}`);
  async function choose(language = "hi", scenario = "ready") {
    node("language").value = language; await node("language").emit("change");
    node("scenario").value = scenario; await node("scenario").emit("change");
  }
  async function draft() {
    await choose(); await node("start").emit("click"); await node("stop").emit("click"); await node("reveal").emit("click");
  }
  return { doc, env, api, node, choose, draft };
}

test("initial unavailable state and permanently disabled live controls", async () => {
  const f = await fixture();
  assert.match(f.node("stage").textContent, /Not available/);
  assert.equal(f.node("start").disabled, true);
  for (const id of ["voice-live-record", "voice-live-play"]) {
    const control = f.doc.nodes.get(id);
    assert.equal(control.disabled, true);
    assert.equal(control.listeners.size, 0);
  }
  assert.equal(f.env.calls.length, 0);
});

test("explicit simulated states and keyboard-friendly focus sequence", async () => {
  const f = await fixture();
  await f.choose();
  assert.match(f.node("stage").textContent, /Ready.*mock/);
  await f.node("start").emit("click");
  assert.match(f.node("stage").textContent, /Recording.*visual simulation/);
  assert.equal(f.doc.activeElement, f.node("stop"));
  await f.node("stop").emit("click");
  assert.match(f.node("stage").textContent, /Processing.*mock/);
  assert.equal(f.doc.activeElement, f.node("reveal"));
  await f.node("reveal").emit("click");
  assert.match(f.node("stage").textContent, /Transcript ready.*unverified/);
  assert.equal(f.doc.activeElement, f.node("correction"));
  assert.equal(f.node("confirm").disabled, true);
  assert.equal(f.env.calls.length, 0);
});

test("explicit correction and separate confirmation preserve the original", async () => {
  const f = await fixture(); await f.draft();
  const original = f.node("original").textContent;
  f.node("correction").value = "Fictional corrected sentence only.";
  await f.node("correction").emit("input");
  await f.node("confirm").emit("click");
  assert.match(f.node("review-status").textContent, /Unverified/);
  await f.node("apply").emit("click");
  assert.equal(f.node("confirm").disabled, false);
  assert.equal(f.node("original").textContent, original);
  await f.node("confirm").emit("click");
  assert.match(f.node("review-status").textContent, /Nurse confirmed.*demonstration/);
  assert.equal(f.node("correction").disabled, true);
  assert.equal(f.node("original").textContent, original);
  assert.equal(f.node("output").textContent, "No speech has been generated or played.");
  await f.node("preview").emit("click");
  assert.equal(f.node("output").textContent, "Fictional corrected sentence only.");
  assert.equal(f.env.calls.length, 0); assert.equal(f.env.writes.length, 0);
});

test("editing invalidates deliberate review and confirmation cannot be forced", async () => {
  const f = await fixture(); await f.draft();
  await f.node("apply").emit("click");
  f.node("correction").value = "Another fictional revision.";
  // Even a programmatic change without an input event cannot confirm stale text.
  await f.node("confirm").emit("click");
  assert.match(f.node("stage").textContent, /unverified/);
  await f.node("correction").emit("input");
  assert.equal(f.node("confirm").disabled, true);
  f.node("confirm").disabled = false;
  await f.node("confirm").emit("click");
  assert.match(f.node("stage").textContent, /unverified/);
});

for (const [scenario, message] of [["consent", /Separate approved recording/], ["language", /Provider support.*unverified/], ["backend", /Backend voice capability is disabled/], ["unavailable", /Live voice is not available/]]) {
  test(`unavailable practice scenario: ${scenario}`, async () => {
    const f = await fixture(); await f.choose("hi", scenario);
    assert.equal(f.node("start").disabled, true);
    assert.match(f.node("reason").textContent, message);
    assert.equal(f.node("confirm").disabled, true);
  });
}
for (const language of langs) {
  test(`exact application language ${language}; fictional fixture is not a provider claim`, async () => {
    const f = await fixture(); await f.choose(language);
    await f.node("start").emit("click"); await f.node("stop").emit("click"); await f.node("reveal").emit("click");
    assert.match(f.node("language-label").textContent, new RegExp(`\\(${language}\\)`));
    for (const id of ["original", "correction", "output"]) assert.equal(f.node(id).getAttribute("lang"), language);
    for (const id of ["review-language", "output-language"]) assert.equal(f.node(id).textContent, f.node("language-label").textContent);
    assert.ok(f.node("original").textContent.length > 0);
    assert.equal(f.node("original").textContent, f.node("correction").value);
    assert.equal(f.env.calls.length, 0);
  });
}
for (const language of ["", "fr", "hi-IN", "__proto__", "EN"]) {
  test(`no language fallback for ${language || "missing language"}`, async () => {
    const f = await fixture(); await f.choose(language);
    assert.equal(f.node("start").disabled, true);
    assert.match(f.node("language-label").textContent, /No language selected/);
    assert.equal(f.node("original").textContent, "");
  });
}
for (const value of ["", " ", "Fictional\0text", "x".repeat(8001), "\ud800", "\udc00"]) {
  test(`invalid correction stays unverified (${value.length} characters)`, async () => {
    const f = await fixture(); await f.draft(); f.node("correction").value = value;
    await f.node("correction").emit("input"); await f.node("apply").emit("click");
    assert.equal(f.node("correction").getAttribute("aria-invalid"), "true");
    assert.equal(f.node("confirm").disabled, true);
    assert.doesNotMatch(f.node("review-status").textContent, /Fictional\0text/);
  });
}

test("safe literal text rendering, no automatic preview or playback", async () => {
  const f = await fixture(); await f.draft();
  const literal = '<img src=x onerror="throw 1"> FICTIONAL ONLY';
  f.node("correction").value = literal;
  await f.node("correction").emit("input"); await f.node("apply").emit("click"); await f.node("confirm").emit("click");
  assert.notEqual(f.node("output").textContent, literal);
  await f.node("preview").emit("click");
  assert.equal(f.node("output").textContent, literal);
  assert.equal(f.doc.nodes.get("voice-live-play").disabled, true);
  assert.equal(f.env.calls.length, 0);
});

test("patient changes and close clear mock text and invalidate review", async () => {
  const f = await fixture(); await f.draft(); await f.node("apply").emit("click");
  f.api.setPatientContext({ language: "ta", careAccess: "enabled" });
  assert.equal(f.node("language").disabled, true);
  assert.equal(f.node("language").value, "ta");
  assert.equal(f.node("original").textContent, "");
  assert.equal(f.node("correction").value, "");
  assert.equal(f.node("confirm").disabled, true);
  assert.match(f.node("context").textContent, /not attached/);
  f.api.clear();
  assert.equal(f.node("language").value, "");
  assert.equal(f.node("language").disabled, false);
  assert.equal(f.node("original").textContent, "");
});
for (const careAccess of ["blocked", "declined", "revoked", undefined]) {
  test(`care unavailable cannot be bypassed by a ready scenario: ${careAccess}`, async () => {
    const f = await fixture(); f.api.setPatientContext({ language: "hi", careAccess });
    f.node("scenario").value = "ready"; await f.node("scenario").emit("change");
    assert.equal(f.node("start").disabled, true);
    assert.match(f.node("reason").textContent, /Care consent.*unavailable/);
    assert.equal(f.env.calls.length, 0);
  });
}

test("reset during processing cannot later restore a transcript", async () => {
  const f = await fixture(); await f.choose(); await f.node("start").emit("click"); await f.node("stop").emit("click");
  f.api.clear(); f.node("reveal").disabled = false; await f.node("reveal").emit("click");
  assert.equal(f.node("original").textContent, "");
  assert.match(f.node("stage").textContent, /Not available/);
  assert.equal(f.env.timers.size, 0);
});

test("reset and language changes preserve disabled real controls", async () => {
  const f = await fixture(); await f.draft(); await f.node("reset").emit("click");
  assert.equal(f.node("original").textContent, "");
  assert.equal(f.node("confirm").disabled, true);
  await f.choose("en");
  for (const id of ["voice-live-record", "voice-live-play"]) assert.equal(f.doc.nodes.get(id).disabled, true);
});

const A = "20000000-0000-4000-8000-000000000001";
const B = "20000000-0000-4000-8000-000000000002";
const row = (id, language = "hi") => ({ id, reference: id === A ? "FICTIONAL-A" : "FICTIONAL-B", name: "Fictional Adult", age: 36, age_recorded_on: "2026-01-01", preferred_language: language });
const status = (id, language = "hi", care = "enabled") => ({ id, reference: row(id).reference, preferred_language: language, created_at: "2026-01-01T00:00:00Z", care_access: care, consents: [{ id: "30000000-0000-4000-8000-000000000001", purpose: "care", notice_version: "TEST-ONLY-HISTORICAL", language, granted: care === "enabled", status: care === "enabled" ? "granted" : "declined", created_at: "2026-01-01T00:00:00Z", revoked_at: null }] });
const detail = id => ({ ...row(id), sex: null, contact_reference: null, history: "Fictional history", allergies: null, current_medicines: null, created_at: "2026-01-01T00:00:00Z" });
const response = (body, code = 200) => ({ ok: code >= 200 && code < 300, status: code, json: async () => body });
async function flush() { for (let i = 0; i < 30; i++) await Promise.resolve(); }
async function application(overrides = {}) {
  const doc = documentDouble();
  const config = { directory: () => response({ items: [row(A), row(B, "ta")], next_cursor: null }),
    registration: id => response(status(id, id === A ? "hi" : "ta")),
    detail: id => response(detail(id)), auth: () => response({ id: A, role: "nurse", display_name: "Fictional Nurse" }), ...overrides };
  const env = sandbox(doc, async (url, options) => {
    assert.ok(url.startsWith("/api/v1/"));
    assert.equal(options.credentials, "same-origin");
    assert.equal(options.cache, "no-store");
    if (url === "/api/v1/auth/me") return config.auth();
    if (url === "/api/v1/auth/csrf") return response({ csrf_token: "fictional-csrf" });
    if (url === "/api/v1/auth/logout") return response(null, 204);
    if (url.startsWith("/api/v1/patients?")) return config.directory(url);
    const match = url.match(/^\/api\/v1\/patients\/([^/]+)(\/registration)?$/);
    if (match) return match[2] ? config.registration(match[1]) : config.detail(match[1]);
    throw new Error("Unexpected or voice API request forbidden");
  });
  const module = await loadModule(path.join(base, "assets/js/app.js"), env.context);
  await module.evaluate(); await flush();
  const node = id => doc.nodes.get(id);
  const open = async (index = 0) => {
    const button = node("patient-rows").children[index].children[4].children[1];
    await button.emit("click"); await flush();
  };
  return { doc, env, config, node, open };
}

test("actual app: verified nurse, authorized record language and close reset", async () => {
  const f = await application();
  assert.equal(f.node("main").hidden, false);
  assert.equal(f.node("patient-rows").children.length, 2);
  await f.open();
  assert.equal(f.node("voice-demo-language").value, "hi");
  assert.match(f.node("patient-view-message").textContent, /Read-only/);
  await f.node("patient-view-close").emit("click");
  assert.equal(f.node("voice-demo-language").value, "");
  assert.equal(f.node("patient-detail-content").children.length, 0);
  assert.equal(f.env.calls.filter(call => call.url.includes("/voice")).length, 0);
  assert.equal(f.env.writes.length, 0);
});

test("actual app: directory failure never falls back to synthetic patients", async () => {
  const f = await application({ directory: () => response({ detail: "PRIVATE_MARKER" }, 503) });
  assert.equal(f.node("patient-rows").children.length, 0);
  assert.equal(f.node("directory-error").hidden, false);
  assert.doesNotMatch(f.node("directory-error").textContent, /PRIVATE_MARKER/);
  assert.equal(f.node("voice-demo-original").textContent, "");
});

test("actual app: declined care never reads clinical detail and blocks simulation", async () => {
  const f = await application({ registration: id => response(status(id, "pa", "blocked")) });
  await f.open();
  assert.equal(f.node("voice-demo-language").value, "pa");
  f.node("voice-demo-scenario").value = "ready"; await f.node("voice-demo-scenario").emit("change");
  assert.equal(f.node("voice-demo-start").disabled, true);
  assert.ok(!f.env.calls.some(call => call.url === `/api/v1/patients/${A}`));
});

for (const code of [401, 403, 404, 503]) {
  test(`actual app: access failure ${code} clears voice and protected state`, async () => {
    const f = await application(); await f.open();
    f.config.registration = () => response({ detail: "PRIVATE_MARKER" }, code);
    await f.open(1);
    assert.equal(f.node("voice-demo-language").value, "");
    assert.equal(f.node("voice-demo-original").textContent, "");
    assert.equal(f.node("patient-detail-content").children.length, 0);
    assert.doesNotMatch(f.node("patient-view-message").textContent, /PRIVATE_MARKER/);
    if (code === 401) assert.equal(f.env.redirects.at(-1), "login.html?reason=expired");
  });
}

test("actual app: late patient status cannot restore an obsolete language", async () => {
  let deliver;
  const f = await application({ registration: id => id === A
    ? new Promise(resolve => { deliver = resolve; }) : response(status(id, "ta")) });
  const first = f.open(); await flush(); await f.open(1);
  assert.equal(f.node("voice-demo-language").value, "ta");
  deliver(response(status(A, "hi"))); await first; await flush();
  assert.equal(f.node("voice-demo-language").value, "ta");
});

test("actual app: logout and pagehide erase practice text", async () => {
  const f = await application();
  const n = id => f.node(`voice-demo-${id}`);
  n("language").value = "en"; await n("language").emit("change");
  n("scenario").value = "ready"; await n("scenario").emit("change");
  await n("start").emit("click"); await n("stop").emit("click"); await n("reveal").emit("click");
  assert.ok(n("original").textContent);
  await f.doc.logout.emit("click"); await flush();
  assert.equal(n("original").textContent, "");
  assert.equal(f.env.redirects.at(-1), "login.html");
  f.env.windowEvents.get("pagehide")();
  assert.equal(n("correction").value, "");
});

test("actual app: invalid session never initializes voice controls", async () => {
  const f = await application({ auth: () => response({}, 401) });
  assert.equal(f.env.redirects.at(-1), "login.html?reason=expired");
  assert.equal(f.node("voice-demo-start").listeners.size, 0);
  assert.equal(f.node("main").hidden, true);
});

test("static safety: no microphone, playback, persistence or provider request APIs", () => {
  for (const forbidden of ["getUserMedia", "MediaRecorder", "speechSynthesis", "SpeechSynthesisUtterance", "AudioContext", "new Audio", "fetch(", "XMLHttpRequest", "WebSocket", "sendBeacon", "localStorage", "sessionStorage", "innerHTML", "/api/v1/voice"]) {
    assert.ok(!voiceSource.includes(forbidden), `Voice module must not contain ${forbidden}`);
  }
  assert.doesNotMatch(html, /<(?:audio|video)\b|\bautoplay\b/i);
  const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map(match => match[1]);
  assert.equal(new Set(ids).size, ids.length);
  const voiceMarkup = html.slice(html.indexOf('id="voice-demo"'));
  for (const match of voiceSource.matchAll(/find\("([^"]+)"\)/g)) assert.ok(ids.includes(match[1]));
  for (const match of voiceMarkup.matchAll(/<label for="([^"]+)"/g)) assert.ok(ids.includes(match[1]));
  for (const match of voiceMarkup.matchAll(/<button[^>]*>/g)) assert.match(match[0], /type="button"/);
  assert.match(voiceMarkup, /role="status" aria-live="polite"/);
  for (const id of ["voice-live-record", "voice-live-play", "voice-demo-start", "voice-demo-confirm"]) {
    const tag = html.match(new RegExp(`<button[^>]*id="${id}"[^>]*>`))[0];
    assert.match(tag, /disabled/);
  }
  const registration = fs.readFileSync(path.join(base, "register.html"), "utf8");
  assert.match(registration, /type="submit" disabled/);
  assert.match(registration, /Registration is unavailable until approved consent notices/);
});


test("actual app: wrong role cannot initialize the nurse voice preview", async () => {
  const f = await application({ auth: () => response({ id: A, role: "doctor", display_name: "Fictional Doctor" }) });
  assert.equal(f.env.redirects.at(-1), "doctor.html");
  assert.equal(f.node("voice-demo-start").listeners.size, 0);
  assert.equal(f.node("main").hidden, true);
});

test("actual app: read-only directory pagination deduplicates without voice requests", async () => {
  const f = await application({ directory: url => url.includes("cursor=")
    ? response({ items: [row(A), row(B)], next_cursor: null })
    : response({ items: [row(A)], next_cursor: A }) });
  assert.equal(f.node("patient-rows").children.length, 1);
  assert.equal(f.node("patient-load-more").hidden, false);
  await f.node("patient-load-more").emit("click"); await flush();
  assert.equal(f.node("patient-rows").children.length, 2);
  assert.equal(f.node("patient-load-more").hidden, true);
  assert.ok(f.env.calls.every(call => call.options.method === "GET"));
  assert.ok(f.env.calls.every(call => !call.url.includes("/voice")));
  assert.equal(f.env.writes.length, 0);
});
