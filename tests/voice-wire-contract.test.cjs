/* Same reviewed fictional wire fixture consumed by Python tests. Actual frontend
 * client/Auth helpers, in-memory Response doubles only. NOT browser-to-server E2E. */
const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const DATA = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures/voice-wire-contract.json'), 'utf8'));
const BASE = path.resolve(__dirname, '../frontend/assets/js');

async function fixture() {
  const calls = [], auth = [], timers = new Map(); let id = 0, index = 0;
  const stages = ['transcription', 'correction', 'confirmation', 'synthesis'];
  const fail = () => { throw new Error('Unexpected media, persistence, network or logging'); };
  const config = { change: value => value };
  const context = vm.createContext({
    Response, TextEncoder, TextDecoder, AbortController,
    setTimeout: fn => { timers.set(++id, fn); return id; }, clearTimeout: key => timers.delete(key),
    fetch: async (url, options) => {
      auth.push(url);
      assert.equal(options.credentials, 'same-origin');
      assert.equal(options.cache, 'no-store');
      assert.ok(['/api/v1/auth/me', '/api/v1/auth/csrf'].includes(url));
      return new Response(JSON.stringify(url.endsWith('/me') ? { id: DATA.actor_id, display_name: 'Fictional Nurse', role: 'nurse' } : { csrf_token: 'fictional-csrf' }), { headers: { 'Content-Type': 'application/json' } });
    },
    navigator: { get mediaDevices() { return fail(); } },
    get MediaRecorder() { return fail(); }, get Audio() { return fail(); }, get speechSynthesis() { return fail(); },
    get localStorage() { return fail(); }, get sessionStorage() { return fail(); },
    console: { log: fail, warn: fail, error: fail },
  });
  const modules = new Map();
  async function load(name) {
    if (modules.has(name)) return modules.get(name);
    const module = new vm.SourceTextModule(fs.readFileSync(name, 'utf8'), { context, identifier: name });
    modules.set(name, module);
    await module.link(specifier => load(path.resolve(path.dirname(name), specifier)));
    return module;
  }
  const module = await load(path.join(BASE, 'voice-api.js')); await module.evaluate();
  const client = module.namespace.createOfflineVoiceClient({ exchange: async (url, options) => {
    const stage = stages[index++], expected = DATA[stage];
    calls.push(stage);
    assert.equal(url, expected.path);
    assert.deepEqual(JSON.parse(options.body), expected.request);
    assert.equal(options.method, 'POST'); assert.equal(options.credentials, 'same-origin');
    assert.equal(options.cache, 'no-store'); assert.equal(options.redirect, 'error');
    assert.equal(options.headers['x-csrf-token'], 'fictional-csrf');
    assert.ok(!Object.hasOwn(options.headers, 'Authorization'));
    if (stage === 'synthesis') return new Response(Buffer.from(expected.audio_base64, 'base64'), { headers: expected.headers });
    return new Response(JSON.stringify(config.change(structuredClone(expected.response))), { headers: { 'Content-Type': 'application/json' } });
  } });
  return { api: module.namespace, client, calls, auth, timers, config };
}

test('shared wire contract: real frontend STT, correction, confirmation and TTS', async () => {
  const f = await fixture();
  assert.equal(f.calls.length, 0); assert.equal(f.auth.length, 0);
  f.client.setContext(DATA.context);
  const original = await f.client.transcribe(DATA.transcription.request);
  assert.equal(original.transcript.draft.original_text, DATA.transcription.response.original_text);
  assert.equal(original.transcript.state, 'unverified');
  const { language: ignored, ...correct } = DATA.correction.request;
  const corrected = await f.client.review(original, correct);
  assert.equal(corrected.transcript.state, 'nurse_corrected');
  const { language: ignoredAgain, ...confirm } = DATA.confirmation.request;
  const confirmed = await f.client.review(corrected, confirm);
  assert.equal(confirmed.transcript.state, 'nurse_confirmed');
  assert.equal(confirmed.transcript.reviews.length, 2);
  assert.equal(confirmed.transcript.draft.original_text, DATA.transcription.response.original_text);
  assert.equal(confirmed.transcript.draft.language, DATA.context.language);
  const output = await f.client.synthesize(confirmed, { media_type: DATA.synthesis.request.media_type });
  assert.equal(output.playbackAuthorized, false); assert.equal(output.clinicalAcceptance, false);
  assert.equal(Buffer.from(output.audio).toString('base64'), DATA.synthesis.audio_base64);
  assert.deepEqual(f.calls, ['transcription', 'correction', 'confirmation', 'synthesis']);
  assert.equal(f.timers.size, 0);
  f.client.clear();
});

test('shared fixture cannot activate the default client', async () => {
  const f = await fixture(); const client = f.api.createVoiceClient({ enabled: true });
  await assert.rejects(() => client.transcribe(DATA.transcription.request), error => error.code === 'disabled');
  assert.equal(f.calls.length, 0); assert.equal(f.auth.length, 0);
});

test('shared fixture: an automatically confirmed STT response is rejected', async () => {
  const f = await fixture(); f.config.change = value => ({ ...value, state: 'nurse_confirmed' });
  f.client.setContext(DATA.context);
  await assert.rejects(() => f.client.transcribe(DATA.transcription.request), error => error.code === 'unavailable');
  assert.equal(f.calls.length, 1);
  assert.equal(f.timers.size, 0);
});
