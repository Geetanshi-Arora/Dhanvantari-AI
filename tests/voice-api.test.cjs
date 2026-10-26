/* Offline contract tests. Real Auth helpers, fake HTTP, fictional identifiers.
 * No browser, servers, provider transports, media devices or persistent storage. */
const assert = require("node:assert/strict");
const test = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const base = path.resolve(__dirname, "../frontend/assets/js");
const P = "20000000-0000-4000-8000-000000000001";
const V = "30000000-0000-4000-8000-000000000001";
const R = "40000000-0000-4000-8000-000000000001";
const N = "10000000-0000-4000-8000-000000000001";
const NOW = "2026-01-01T00:00:00Z";
const TEXT = "FICTIONAL UNVERIFIED EXAMPLE ONLY";
const CORRECTED = "FICTIONAL REVIEWED EXAMPLE ONLY";
const context = { patient_id: P, visit_id: V, language: "hi" };
const audio = () => ({ language: "hi", media_type: "audio/wav", byte_count: 3, duration_ms: 1000, filename: "fictional.wav", audio_base64: "YWJj" });
const draft = () => ({ request_id: R, ...context, provider: "fictional_offline", original_text: TEXT, created_at: NOW, state: "unverified" });
const json = (value, status = 200) => new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
const root = `/api/v1/voice/patients/${P}/visits/${V}`;
async function fixture() {
  const calls = [], authCalls = [], timers = new Map(); let next = 0;
  const denied = () => { throw new Error("Forbidden I/O, media or logging"); };
  const config = { actor: N, role: "nurse", authStatus: 200, csrfStatus: 200, csrf: "fictional-csrf", exchange: () => json(draft()) };
  const sandbox = vm.createContext({
    Response, ReadableStream, TextEncoder, TextDecoder, AbortController,
    setTimeout: (fn, delay) => { assert.equal(delay, 15000); timers.set(++next, fn); return next; },
    clearTimeout: id => timers.delete(id),
    fetch: async (url, options) => {
      authCalls.push({ url, options });
      assert.ok(["/api/v1/auth/me", "/api/v1/auth/csrf"].includes(url), "No real or voice fetch permitted");
      assert.equal(options.credentials, "same-origin"); assert.equal(options.cache, "no-store");
      return url.endsWith("/me") ? json({ id: config.actor, role: config.role, display_name: "Fictional Nurse" }, config.authStatus)
        : json({ csrf_token: config.csrf }, config.csrfStatus);
    },
    navigator: { get mediaDevices() { return denied(); } },
    get MediaRecorder() { return denied(); }, get Audio() { return denied(); }, get speechSynthesis() { return denied(); },
    get localStorage() { return denied(); }, get sessionStorage() { return denied(); },
    console: { log: denied, warn: denied, error: denied },
  });
  const cache = new Map();
  async function load(name) {
    if (cache.has(name)) return cache.get(name);
    const module = new vm.SourceTextModule(fs.readFileSync(name, "utf8"), { context: sandbox, identifier: name });
    cache.set(name, module);
    await module.link(specifier => load(path.resolve(path.dirname(name), specifier)));
    return module;
  }
  const module = await load(path.join(base, "voice-api.js")); await module.evaluate();
  const api = module.namespace;
  const client = api.createOfflineVoiceClient({ exchange: async (url, options) => {
    assert.ok(url.startsWith(`/api/v1/voice/patients/${P}/visits/`));
    calls.push({ url, options }); return config.exchange(url, options);
  } });
  const reset = () => client.setContext(context);
  reset();
  async function transcribe() { return client.transcribe(audio()); }
  async function reviewed() {
    config.exchange = () => json(draft()); const first = await transcribe();
    const correct = { expected_revision: 0, action: "correct", corrected_text: CORRECTED };
    config.exchange = () => json({ draft: draft(), reviews: [{ revision: 1, nurse_id: N, reviewed_at: NOW, action: correct }], state: "nurse_corrected" });
    const second = await client.review(first, correct);
    const confirm = { expected_revision: 1, action: "confirm" };
    config.exchange = () => json({ draft: draft(), reviews: [
      { revision: 1, nurse_id: N, reviewed_at: NOW, action: correct },
      { revision: 2, nurse_id: N, reviewed_at: NOW, action: { ...confirm, corrected_text: null } },
    ], state: "nurse_confirmed" });
    const third = await client.review(second, confirm);
    return { first, second, third };
  }
  return { api, client, config, calls, authCalls, timers, reset, transcribe, reviewed };
}
async function rejects(code, operation) {
  await assert.rejects(operation, error => {
    assert.equal(error.code, code);
    assert.ok(!error.message.includes(TEXT) && !error.message.includes(CORRECTED) && !error.message.includes("PRIVATE_MARKER"));
    return true;
  });
}
async function flush() { for (let i = 0; i < 30; i++) await Promise.resolve(); }

test("default client is unconditionally disabled; construction and all calls cause no network", async () => {
  const f = await fixture();
  const disabled = f.api.createVoiceClient({ enabled: true, exchange: () => { throw 1; } });
  assert.equal(disabled.mode, "disabled");
  assert.throws(() => disabled.setContext(context), /disabled/);
  for (const op of ["transcribe", "review", "synthesize"]) await rejects("disabled", () => disabled[op](audio()));
  disabled.clear();
  assert.equal(f.authCalls.length, 0); assert.equal(f.calls.length, 0);
  assert.throws(() => f.api.createOfflineVoiceClient(), /disabled/);
});

test("STT exact route, server-controlled fields absent, cookie/CSRF and timeout conventions", async () => {
  const f = await fixture(); const receipt = await f.transcribe();
  assert.equal(f.calls.length, 1);
  const { url, options } = f.calls[0];
  assert.equal(url, root + "/transcriptions");
  assert.deepEqual(JSON.parse(options.body), audio());
  assert.equal(options.method, "POST"); assert.equal(options.credentials, "same-origin");
  assert.equal(options.cache, "no-store"); assert.equal(options.redirect, "error");
  assert.equal(options.mode, "same-origin"); assert.equal(options.headers["x-csrf-token"], "fictional-csrf");
  assert.equal(options.headers["Content-Type"], "application/json");
  assert.ok(!Object.hasOwn(options.headers, "Origin") && !Object.hasOwn(options.headers, "Authorization"));
  assert.equal(receipt.transcript.state, "unverified");
  assert.equal(receipt.clinicalAcceptance, false); assert.equal(receipt.source, "offline-test");
  assert.ok(Object.isFrozen(receipt) && Object.isFrozen(receipt.transcript.draft));
  assert.equal(f.timers.size, 0);
});

for (const value of [
  { ...context, patient_id: "../escape" }, { ...context, visit_id: "https://evil.invalid" },
  { ...context, language: "hi-IN" }, { ...context, language: "__proto__" },
  { ...context, language: "fr" }, { ...context, nurse_id: N },
]) {
  test("invalid context or unsupported language fails without HTTP", async () => {
    const f = await fixture(); assert.throws(() => f.client.setContext(value));
    await rejects("disabled", f.transcribe);
    assert.equal(f.calls.length, 0); assert.equal(f.authCalls.length, 0);
  });
}
for (const change of [
  { language: "en" }, { language: "hi-IN" }, { duration_ms: 60001 }, { duration_ms: true },
  { byte_count: 4 * 1024 * 1024 + 1 }, { byte_count: 1 }, { filename: "../input.wav" },
  { audio_base64: "bad" }, { audio_base64: "YR==", byte_count: 1 },
  { media_type: null }, { nurse_id: N }, { care: true, uploads: true, future_ai: true },
]) {
  test("invalid metadata, sizes, language or caller authority rejected", async () => {
    const f = await fixture(); await rejects("invalid", () => f.client.transcribe({ ...audio(), ...change }));
    assert.equal(f.calls.length, 0);
  });
}
for (const [status, code] of [[401, "session"], [403, "forbidden"], [404, "forbidden"], [409, "stale"], [413, "oversized"], [415, "invalid"], [422, "invalid"], [429, "unavailable"], [500, "unavailable"], [503, "unavailable"], [504, "timeout"]]) {
  test(`fixed error mapping and cleanup for HTTP ${status}`, async () => {
    const f = await fixture(); f.config.exchange = () => json({ detail: TEXT + "PRIVATE_MARKER" }, status);
    await rejects(code, f.transcribe); await rejects("disabled", f.transcribe);
    assert.equal(f.calls.length, 1);
  });
}
for (const [status, role, code] of [[401, "nurse", "session"], [403, "nurse", "forbidden"], [200, "doctor", "forbidden"]]) {
  test(`existing authenticated identity check: ${status}/${role}`, async () => {
    const f = await fixture(); f.config.authStatus = status; f.config.role = role;
    await rejects(code, f.transcribe); assert.equal(f.calls.length, 0);
  });
}
for (const csrf of ["", "bad\r\nheader", "x".repeat(257)]) {
  test("malformed CSRF never reaches voice transport", async () => {
    const f = await fixture(); f.config.csrf = csrf;
    await rejects("unavailable", f.transcribe); assert.equal(f.calls.length, 0);
  });
}

test("CSRF 401 clears context and never posts", async () => {
  const f = await fixture(); f.config.csrfStatus = 401;
  await rejects("session", f.transcribe); assert.equal(f.calls.length, 0);
});

test("explicit review contracts, original identity and immutable revision history", async () => {
  const f = await fixture(); const { first, second, third } = await f.reviewed();
  assert.equal(first.transcript.state, "unverified");
  assert.equal(second.transcript.state, "nurse_corrected");
  assert.equal(third.transcript.state, "nurse_confirmed");
  assert.equal(third.transcript.draft.original_text, TEXT);
  assert.equal(first.transcript.reviews.length, 0);
  assert.equal(f.calls[1].url, `${root}/transcripts/${R}/review`);
  assert.deepEqual(JSON.parse(f.calls[1].options.body), { language: "hi", expected_revision: 0, action: "correct", corrected_text: CORRECTED });
  assert.deepEqual(JSON.parse(f.calls[2].options.body), { language: "hi", expected_revision: 1, action: "confirm" });
  await rejects("stale", () => f.client.review(first, { expected_revision: 0, action: "confirm" }));
});

test("mock UI objects, copied receipts and premature confirmation cannot become reviewed", async () => {
  const f = await fixture(); const first = await f.transcribe();
  await rejects("stale", () => f.client.review({ ...first }, { expected_revision: 0, action: "correct", corrected_text: CORRECTED }));
  f.reset(); await rejects("stale", () => f.client.review({ state: "confirmed", transcript: CORRECTED }, { expected_revision: 0, action: "confirm" }));
  f.reset(); const unverified = await f.transcribe();
  await rejects("stale", () => f.client.review(unverified, { expected_revision: 0, action: "confirm" }));
});

test("stale expected revision never submits a review", async () => {
  const f = await fixture(); const first = await f.transcribe();
  await rejects("stale", () => f.client.review(first, { expected_revision: 1, action: "correct", corrected_text: CORRECTED }));
  assert.equal(f.calls.length, 1);
});

for (const changed of [{ state: "nurse_confirmed" }, { language: "en" }, { patient_id: V }, { original_text: "" }, { approved: true }]) {
  test("malformed or automatically approved provider response rejected", async () => {
    const f = await fixture(); f.config.exchange = () => json({ ...draft(), ...changed });
    await rejects("unavailable", f.transcribe);
  });
}

for (const [label, result, code] of [
  ["malformed JSON", () => new Response("not-json", { headers: { "Content-Type": "application/json" } }), "unavailable"],
  ["wrong content type", () => new Response(JSON.stringify(draft()), { headers: { "Content-Type": "text/html" } }), "unavailable"],
  ["oversized stream", () => new Response(new Uint8Array(1024 * 1024 + 1), { headers: { "Content-Type": "application/json" } }), "oversized"],
]) {
  test(`bounded response rejects ${label}`, async () => {
    const f = await fixture(); f.config.exchange = result;
    await rejects(code, f.transcribe);
  });
}

test("context switch aborts an in-flight request and discards late results", async () => {
  const f = await fixture(); let deliver;
  f.config.exchange = () => new Promise(resolve => { deliver = resolve; });
  const pending = f.transcribe(); const assertion = rejects("stale", () => pending); await flush();
  assert.ok(deliver); f.client.setContext({ ...context, visit_id: R }); await assertion;
  assert.equal(f.calls[0].options.signal.aborted, true);
  deliver(json(draft())); await flush();
  // The old completion must not erase the new context.
  f.config.exchange = () => json({ ...draft(), visit_id: R });
  const newer = await f.transcribe(); assert.equal(newer.transcript.draft.visit_id, R);
});

test("logout clear invalidates receipts and aborts pending work", async () => {
  const f = await fixture(); const first = await f.transcribe();
  f.client.clear(); f.reset();
  await rejects("stale", () => f.client.review(first, { expected_revision: 0, action: "correct", corrected_text: CORRECTED }));
  assert.equal(f.calls.length, 1);
});

test("bounded operation timeout rejects even if fake transport ignores abort", async () => {
  const f = await fixture(); let deliver;
  f.config.exchange = () => new Promise(resolve => { deliver = resolve; });
  const pending = f.transcribe(); const assertion = rejects("timeout", () => pending); await flush();
  for (const fn of [...f.timers.values()]) fn();
  await assertion; assert.equal(f.calls[0].options.signal.aborted, true);
  deliver(json(draft())); await flush(); assert.equal(f.timers.size, 0);
});

test("identity change after provider response prevents acceptance", async () => {
  const f = await fixture(); f.config.exchange = () => { f.config.actor = V; return json(draft()); };
  await rejects("session", f.transcribe);
});

test("TTS uses only confirmed server review text and never grants playback", async () => {
  const f = await fixture(); const { third } = await f.reviewed();
  f.config.exchange = () => new Response(new Uint8Array([1, 2, 3]), { headers: {
    "Content-Type": "audio/wav", "Content-Disposition": 'attachment; filename="voice-audio.bin"',
    "X-Voice-Playback-Authorized": "false", "X-Voice-Request-ID": V,
  } });
  const result = await f.client.synthesize(third, { media_type: "audio/wav" });
  assert.equal(f.calls.at(-1).url, root + "/synthesis");
  assert.deepEqual(JSON.parse(f.calls.at(-1).options.body), { language: "hi", media_type: "audio/wav", text: CORRECTED });
  assert.equal(result.playbackAuthorized, false); assert.equal(result.clinicalAcceptance, false);
  assert.equal(result.audio.byteLength, 3); assert.ok(Object.isFrozen(result));
});

for (const headers of [{ "X-Voice-Playback-Authorized": "true" }, { "Content-Type": "audio/mpeg" }, { "Content-Disposition": "inline" }, { "X-Voice-Request-ID": "bad" }]) {
  test("unsafe TTS metadata cannot authorize playback", async () => {
    const f = await fixture(); const { third } = await f.reviewed();
    f.config.exchange = () => new Response(new Uint8Array([1]), { headers: {
      "Content-Type": "audio/wav", "Content-Disposition": "attachment; filename=fictional.wav",
      "X-Voice-Playback-Authorized": "false", "X-Voice-Request-ID": V, ...headers,
    } });
    await rejects("unavailable", () => f.client.synthesize(third, { media_type: "audio/wav" }));
  });
}

test("TTS rejects an unverified or mock transcript", async () => {
  const f = await fixture(); const first = await f.transcribe();
  await rejects("stale", () => f.client.synthesize(first, { media_type: "audio/wav" }));
  f.reset(); await rejects("stale", () => f.client.synthesize({ source: "mock", transcript: CORRECTED }, { media_type: "audio/wav" }));
});

test("adapter has no media, persistence, provider URL or live-network fallback", () => {
  const source = fs.readFileSync(path.join(base, "voice-api.js"), "utf8");
  for (const forbidden of ["getUserMedia", "MediaRecorder", "speechSynthesis", "new Audio", "createObjectURL", "localStorage", "sessionStorage", "console.", "https://api.vachana.ai", "await fetch("]) assert.ok(!source.includes(forbidden));
  const mock = fs.readFileSync(path.join(base, "voice-demo.js"), "utf8");
  assert.ok(!mock.includes("voice-api"));
  const app = fs.readFileSync(path.join(base, "app.js"), "utf8");
  assert.ok(app.includes("const voiceClient = createVoiceClient()"));
  assert.ok(!app.includes("createOfflineVoiceClient"));
  assert.equal((app.match(/voiceClient\.clear\(\)/g) || []).length, 2);
});

for (const change of [
  value => ({ ...value, draft: { ...value.draft, original_text: "PRIVATE_MARKER" } }),
  value => ({ ...value, state: "nurse_confirmed" }),
  value => ({ ...value, reviews: [{ ...value.reviews[0], nurse_id: V }] }),
  value => ({ ...value, reviews: [{ ...value.reviews[0], revision: 2 }] }),
]) {
  test("review response cannot change provenance, actor or requested action", async () => {
    const f = await fixture(); const first = await f.transcribe();
    const action = { expected_revision: 0, action: "correct", corrected_text: CORRECTED };
    f.config.exchange = () => json(change({ draft: draft(), reviews: [
      { revision: 1, nurse_id: N, reviewed_at: NOW, action },
    ], state: "nurse_corrected" }));
    await rejects("unavailable", () => f.client.review(first, action));
  });
}

test("review binds the submitted action snapshot, not later caller mutations", async () => {
  const f = await fixture(); const first = await f.transcribe();
  const action = { expected_revision: 0, action: "correct", corrected_text: CORRECTED };
  f.config.exchange = (url, options) => {
    const requested = JSON.parse(options.body);
    action.corrected_text = "PRIVATE_MARKER";
    return json({ draft: draft(), reviews: [{ revision: 1, nurse_id: N, reviewed_at: NOW,
      action: { expected_revision: requested.expected_revision, action: requested.action, corrected_text: requested.corrected_text } }], state: "nurse_corrected" });
  };
  const receipt = await f.client.review(first, action);
  assert.equal(receipt.transcript.reviews[0].action.corrected_text, CORRECTED);
});

test("concurrent operation is rejected without cancelling the pending request", async () => {
  const f = await fixture(); let deliver;
  f.config.exchange = () => new Promise(resolve => { deliver = resolve; });
  const pending = f.transcribe(); await flush();
  await rejects("busy", f.transcribe);
  deliver(json(draft())); const receipt = await pending;
  assert.equal(receipt.transcript.state, "unverified"); assert.equal(f.calls.length, 1);
});

for (const length of [0, 4 * 1024 * 1024 + 1]) {
  test(`TTS audio size ${length} is rejected without playback`, async () => {
    const f = await fixture(); const { third } = await f.reviewed();
    f.config.exchange = () => new Response(new Uint8Array(length), { headers: {
      "Content-Type": "audio/wav", "Content-Disposition": "attachment; filename=fictional.wav",
      "X-Voice-Playback-Authorized": "false", "X-Voice-Request-ID": V,
    } });
    await rejects(length ? "oversized" : "unavailable", () => f.client.synthesize(third, { media_type: "audio/wav" }));
  });
}

test("receipt from another client instance cannot be replayed", async () => {
  const first = await fixture(), second = await fixture(); const receipt = await first.transcribe();
  await rejects("stale", () => second.client.review(receipt, { expected_revision: 0, action: "correct", corrected_text: CORRECTED }));
  assert.equal(second.calls.length, 0);
});
