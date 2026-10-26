/** Disconnected voice client. The application receives an unconditionally disabled
 * client; there is NO live transport, recording switch or fetch fallback here.
 * createOfflineVoiceClient is a trusted test seam only: its injected exchange must
 * be a fake, never fetch. It cannot grant consent or clinical/playback authority.
 * Mock UI text is neither imported nor accepted as a server transcript receipt.
 */
import { currentStaff, csrfToken } from "./auth.js";
import { languages } from "./store.js";

const MAX_AUDIO = 4 * 1024 * 1024;
const MAX_JSON = 1024 * 1024;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const MEDIA = /^audio\/[a-z0-9.+-]{1,50}$/;
const IDENTIFIER = /^[A-Za-z0-9_-]{1,64}$/;
const messages = Object.freeze({
  disabled: [503, "Live voice remains disabled."],
  session: [401, "Your session could not be verified. Sign in again."],
  forbidden: [403, "Voice access or required consent is unavailable."],
  stale: [409, "Voice context or transcript revision changed. Start again."],
  oversized: [413, "Voice request or response exceeds the allowed size."],
  invalid: [422, "Voice input, format or language is invalid or unsupported."],
  unavailable: [503, "Voice capability or provider is unavailable."],
  timeout: [504, "Voice operation timed out. Nothing was automatically retried."],
  busy: [409, "A voice operation is already in progress."],
});
class VoiceError extends Error {
  constructor(code) {
    const safe = Object.hasOwn(messages, code) ? code : "unavailable";
    super(messages[safe][1]);
    this.code = safe; this.status = messages[safe][0];
  }
}
const fail = code => { throw new VoiceError(code); };
const uuid = value => typeof value === "string" && UUID.test(value) ? value.toLowerCase() : fail("invalid");
const text = (value, max) => typeof value === "string" && value.length <= max * 2 && value.trim().length > 0 &&
  [...value].length <= max && !value.includes("\0") &&
  ![...value].some(char => char.codePointAt(0) >= 0xd800 && char.codePointAt(0) <= 0xdfff);
function exact(value, required, optional = []) {
  if (!value || typeof value !== "object" || Array.isArray(value) ||
      !required.every(key => Object.hasOwn(value, key)) ||
      Object.keys(value).some(key => !required.includes(key) && !optional.includes(key))) fail("invalid");
}
function contextValue(value) {
  exact(value, ["patient_id", "visit_id", "language"]);
  if (typeof value.language !== "string" || !Object.hasOwn(languages, value.language)) fail("invalid");
  return Object.freeze({ patient_id: uuid(value.patient_id), visit_id: uuid(value.visit_id), language: value.language });
}
const integer = (value, min, max) => Number.isInteger(value) && value >= min && value <= max;
function actionValue(value) {
  exact(value, ["expected_revision", "action"], ["corrected_text"]);
  if (!integer(value.expected_revision, 0, 10) || !["correct", "confirm"].includes(value.action) ||
      (value.action === "correct" ? !text(value.corrected_text, 8000) : value.corrected_text != null)) fail("invalid");
  return { expected_revision: value.expected_revision, action: value.action,
    ...(value.action === "correct" ? { corrected_text: value.corrected_text } : {}) };
}
function audioBody(value, language) {
  exact(value, ["language", "media_type", "byte_count", "duration_ms", "filename", "audio_base64"]);
  if (value.language !== language || typeof value.media_type !== "string" || !MEDIA.test(value.media_type) ||
      !integer(value.byte_count, 1, MAX_AUDIO) || !integer(value.duration_ms, 1, 60000) ||
      typeof value.filename !== "string" || !/^[A-Za-z0-9_-]{1,64}\.[A-Za-z0-9]{1,5}$/.test(value.filename) ||
      typeof value.audio_base64 !== "string" || value.audio_base64.length < 4 ||
      value.audio_base64.length > 4 * Math.ceil(MAX_AUDIO / 3) || value.audio_base64.length % 4 ||
      !/^[A-Za-z0-9+/]*={0,2}$/.test(value.audio_base64)) fail("invalid");
  const encoded = value.audio_base64;
  const padding = encoded.endsWith("==") ? 2 : encoded.endsWith("=") ? 1 : 0;
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  if (encoded.length / 4 * 3 - padding !== value.byte_count ||
      (padding === 2 && (alphabet.indexOf(encoded.at(-3)) & 15)) ||
      (padding === 1 && (alphabet.indexOf(encoded.at(-2)) & 3))) fail("invalid");
  return { ...value };
}
function timestamp(value) {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(value) ||
      !Number.isFinite(Date.parse(value))) fail("invalid");
  return Date.parse(value);
}
function draftValue(value, context) {
  exact(value, ["request_id", "patient_id", "visit_id", "language", "provider", "original_text", "created_at", "state"]);
  if (uuid(value.patient_id) !== context.patient_id || uuid(value.visit_id) !== context.visit_id ||
      value.language !== context.language || value.state !== "unverified" ||
      typeof value.provider !== "string" || !IDENTIFIER.test(value.provider) || !text(value.original_text, 8000)) fail("invalid");
  timestamp(value.created_at);
  return Object.freeze({ ...value, request_id: uuid(value.request_id), patient_id: context.patient_id, visit_id: context.visit_id });
}
function reviewValue(value, previous, action, actor) {
  exact(value, ["draft", "reviews", "state"]);
  const draft = draftValue(value.draft, previous.draft);
  if (Object.keys(draft).some(key => draft[key] !== previous.draft[key]) ||
      !Array.isArray(value.reviews) || value.reviews.length !== previous.reviews.length + 1 || value.reviews.length > 10 ||
      value.state !== (action.action === "correct" ? "nurse_corrected" : "nurse_confirmed")) fail("invalid");
  let at = timestamp(draft.created_at);
  const reviews = value.reviews.map((event, index) => {
    exact(event, ["revision", "nurse_id", "reviewed_at", "action"]);
    const validated = actionValue(event.action);
    const time = timestamp(event.reviewed_at);
    if (event.revision !== index + 1 || validated.expected_revision !== index || uuid(event.nurse_id) !== actor || time < at ||
        (index < value.reviews.length - 1 && validated.action === "confirm")) fail("invalid");
    at = time;
    const old = previous.reviews[index];
    if (old && (event.revision !== old.revision || event.nurse_id !== old.nurse_id || event.reviewed_at !== old.reviewed_at ||
        JSON.stringify(validated) !== JSON.stringify(actionValue(old.action)))) fail("invalid");
    if (!old && JSON.stringify(validated) !== JSON.stringify(action)) fail("invalid");
    return Object.freeze({ ...event, action: Object.freeze(validated) });
  });
  return Object.freeze({ draft, reviews: Object.freeze(reviews), state: value.state });
}
function httpError(status) {
  return status === 401 ? "session" : [403, 404].includes(status) ? "forbidden" : status === 409 ? "stale"
    : status === 413 ? "oversized" : [415, 422].includes(status) ? "invalid" : status === 504 ? "timeout" : "unavailable";
}
async function boundedBytes(response, limit, signal) {
  const length = response.headers.get("content-length");
  if (length !== null && (!/^\d+$/.test(length) || Number(length) > limit)) fail("oversized");
  if (!response.body || typeof response.body.getReader !== "function") fail("unavailable");
  const reader = response.body.getReader();
  const cancel = () => { reader.cancel().catch(() => {}); };
  signal.addEventListener("abort", cancel, { once: true });
  const chunks = []; let size = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      if (!ArrayBuffer.isView(value) || value.BYTES_PER_ELEMENT !== 1) fail("unavailable");
      size += value.byteLength;
      if (size > limit) fail("oversized");
      chunks.push(value);
    }
  } catch (error) {
    try { await reader.cancel(); } catch { /* Never expose transport details. */ }
    throw error;
  } finally { signal.removeEventListener("abort", cancel); reader.releaseLock(); }
  if (!size) fail("unavailable");
  const result = new Uint8Array(size); let offset = 0;
  for (const chunk of chunks) { result.set(chunk, offset); offset += chunk.byteLength; }
  return result;
}

/** Always disabled, even if callers supply arguments or feature-like options. */
export function createVoiceClient() {
  const disabled = async () => fail("disabled");
  return Object.freeze({ mode: "disabled", setContext() { fail("disabled"); }, clear() {},
    transcribe: disabled, review: disabled, synthesize: disabled });
}

/** OFFLINE TEST INJECTION ONLY. No default transport and no production activation.
 * Auth/CSRF use the existing application helpers; tests must mock those HTTP calls
 * as well. The UI never imports this factory or passes its mock transcript here.
 */
export function createOfflineVoiceClient({ exchange } = {}) {
  if (typeof exchange !== "function") fail("disabled");
  let context = null, epoch = 0, sequence = 0, pending = null, receipts = new WeakMap();
  function clear() {
    epoch++; pending?.abort(); pending = null; context = null; receipts = new WeakMap(); sequence++;
  }
  function setContext(value) { clear(); context = contextValue(value); }
  function receipt(value, actor) {
    const result = Object.freeze({ source: "offline-test", transcript: value, clinicalAcceptance: false });
    receipts.set(result, { actor, epoch, sequence: ++sequence });
    return result;
  }
  function known(value, allowed) {
    const record = value && typeof value === "object" ? receipts.get(value) : null;
    if (!record || record.epoch !== epoch || record.sequence !== sequence || !allowed.includes(value.transcript.state)) fail("stale");
    return record;
  }
  async function operation(build, parse) {
    if (!context) fail("disabled");
    if (pending) fail("busy");
    const snapshot = context, version = epoch;
    const controller = new AbortController(); pending = controller;
    const timer = setTimeout(() => controller.abort(), 15000);
    let aborted;
    const cancellation = new Promise((resolve, reject) => {
      aborted = () => reject(new VoiceError(version !== epoch ? "stale" : "timeout"));
      controller.signal.addEventListener("abort", aborted, { once: true });
    });
    const wait = promise => Promise.race([promise, cancellation]);
    const check = () => {
      if (version !== epoch || context !== snapshot || pending !== controller) fail("stale");
      if (controller.signal.aborted) fail("timeout");
    };
    try {
      const staff = await wait(currentStaff()); check();
      if (staff.role !== "nurse") fail("forbidden");
      const actor = uuid(staff.id);
      const { suffix, body, binary = false } = build(snapshot, actor);
      const encoded = JSON.stringify(body);
      if (new TextEncoder().encode(encoded).byteLength > (suffix === "/transcriptions" ? 4 * Math.ceil(MAX_AUDIO / 3) + 16384 : 32768)) fail("oversized");
      const token = await wait(csrfToken()); check();
      const root = `/api/v1/voice/patients/${snapshot.patient_id}/visits/${snapshot.visit_id}`;
      const response = await wait(exchange(root + suffix, {
        method: "POST", credentials: "same-origin", cache: "no-store", redirect: "error", mode: "same-origin",
        referrerPolicy: "no-referrer", signal: controller.signal,
        headers: { "Content-Type": "application/json", Accept: binary ? body.media_type : "application/json", "x-csrf-token": token },
        body: encoded,
      }));
      check();
      if (!response.ok) fail(httpError(response.status));
      if (response.status !== 200) fail("unavailable");
      const bytes = await wait(boundedBytes(response, binary ? MAX_AUDIO : MAX_JSON, controller.signal)); check();
      let parsed;
      try { parsed = parse(response, bytes, snapshot, actor); }
      catch { fail("unavailable"); }
      const after = await wait(currentStaff()); check();
      if (after.role !== "nurse" || uuid(after.id) !== actor) fail("session");
      return binary ? parsed : receipt(parsed, actor);
    } catch (error) {
      const code = version !== epoch ? "stale" : controller.signal.aborted ? "timeout"
        : error instanceof VoiceError ? error.code : error?.status === 401 ? "session" : error?.status === 403 ? "forbidden" : "unavailable";
      // A late request must not erase a newer patient's context.
      if (version === epoch) clear();
      throw new VoiceError(code);
    } finally {
      clearTimeout(timer);
      controller.signal.removeEventListener("abort", aborted);
      if (pending === controller) pending = null;
    }
  }
  function json(response, bytes) {
    if (response.headers.get("content-type")?.split(";", 1)[0].trim().toLowerCase() !== "application/json") fail("unavailable");
    try { return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes)); }
    catch { fail("unavailable"); }
  }
  return Object.freeze({ mode: "offline-test", clear, setContext,
    transcribe(value) {
      return operation(ctx => ({ suffix: "/transcriptions", body: audioBody(value, ctx.language) }),
        (response, bytes, ctx) => Object.freeze({ draft: draftValue(json(response, bytes), ctx), reviews: Object.freeze([]), state: "unverified" }));
    },
    review(previous, value) {
      let requested;
      return operation((ctx, actor) => {
        const record = known(previous, ["unverified", "nurse_corrected"]);
        const action = Object.freeze(actionValue(value));
        requested = action;
        if (record.actor !== actor || action.expected_revision !== previous.transcript.reviews.length ||
            (action.action === "confirm" && previous.transcript.state !== "nurse_corrected")) fail("stale");
        return { suffix: `/transcripts/${previous.transcript.draft.request_id}/review`, body: { language: ctx.language, ...action } };
      }, (response, bytes, ctx, actor) => reviewValue(json(response, bytes), previous.transcript, requested, actor));
    },
    synthesize(previous, value) {
      let media;
      return operation((ctx, actor) => {
        const record = known(previous, ["nurse_confirmed"]);
        exact(value, ["media_type"]);
        if (record.actor !== actor || typeof value.media_type !== "string" || !MEDIA.test(value.media_type)) fail("invalid");
        media = value.media_type;
        const reviewed = [...previous.transcript.reviews].reverse().find(event => event.action.action === "correct")?.action.corrected_text;
        if (!text(reviewed, 2000)) fail("invalid");
        return { suffix: "/synthesis", binary: true, body: { language: ctx.language, media_type: value.media_type, text: reviewed } };
      }, (response, bytes, ctx) => {
        if (response.headers.get("x-voice-playback-authorized") !== "false" ||
            response.headers.get("content-type")?.split(";", 1)[0] !== media ||
            !response.headers.get("content-disposition")?.startsWith("attachment;")) fail("unavailable");
        return Object.freeze({ source: "offline-test", audio: bytes, requestId: uuid(response.headers.get("x-voice-request-id")), playbackAuthorized: false, clinicalAcceptance: false });
      });
    },
  });
}

export function voiceMessage(error) {
  return typeof error?.code === "string" && Object.hasOwn(messages, error.code) ? messages[error.code][1] : messages.unavailable[1];
}
