/** Read-only, same-origin patient directory. No patient data or tokens are persisted. */
import { languages } from "./store.js";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
class DirectoryError extends Error {
  constructor(status = 0) {
    super("Patient directory request failed");
    this.status = status;
  }
}

function page(value, cursor) {
  const text = (value, limit) => typeof value === "string" &&
    value.length > 0 && value.length <= limit && !value.includes("\0");
  if (!value || !Array.isArray(value.items) || value.items.length > 20 ||
      !(value.next_cursor === null || (typeof value.next_cursor === "string" &&
        UUID.test(value.next_cursor) && value.next_cursor.toLowerCase() !== cursor?.toLowerCase()))) {
    throw new DirectoryError();
  }
  const items = value.items.map((item) => {
    if (!item || typeof item.id !== "string" || !UUID.test(item.id) ||
        !text(item.reference, 30) || !text(item.name, 80) ||
        !Number.isInteger(item.age) || item.age < 18 || item.age > 120 ||
        typeof item.age_recorded_on !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(item.age_recorded_on) ||
        typeof item.preferred_language !== "string" || !Object.hasOwn(languages, item.preferred_language)) throw new DirectoryError();
    return {
      id: item.id.toLowerCase(), reference: item.reference, name: item.name,
      age: item.age, age_recorded_on: item.age_recorded_on,
      preferred_language: item.preferred_language,
    };
  });
  return { items, next_cursor: value.next_cursor?.toLowerCase() ?? null };
}

export async function patientDirectory(cursor = null, { signal } = {}) {
  if (cursor !== null && (typeof cursor !== "string" || !UUID.test(cursor))) {
    throw new DirectoryError();
  }
  const query = new URLSearchParams({ limit: "20" });
  if (cursor !== null) query.set("cursor", cursor);
  return page(await read(`/api/v1/patients?${query}`, { signal }), cursor);
}

async function read(path, { signal } = {}) {
  const controller = new AbortController();
  const abort = () => controller.abort();
  if (signal?.aborted) abort();
  signal?.addEventListener("abort", abort, { once: true });
  const timer = setTimeout(abort, 15000);
  try {
    const response = await fetch(path, {
      method: "GET", credentials: "same-origin", cache: "no-store",
      redirect: "error", signal: controller.signal,
      headers: { Accept: "application/json" },
    });
    if (!response.ok) throw new DirectoryError(response.status);
    return await response.json();
  } catch (error) {
    throw error instanceof DirectoryError ? error : new DirectoryError();
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", abort);
  }
}

export function directoryMessage(error) {
  if (error.status === 403) return "Patient directory access is unavailable for this account.";
  if (error.status === 404) return "The patient directory is unavailable.";
  if (error.status === 503) return "The patient directory is temporarily unavailable. Please try again.";
  return "Could not load the patient directory. Check your connection and try again.";
}


const text = (value, limit) => typeof value === "string" && value.length > 0 &&
  value.length <= limit && !value.includes("\0");
const timestamp = (value) => typeof value === "string" &&
  /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(value) &&
  Number.isFinite(Date.parse(value));
function patientId(value) {
  if (typeof value !== "string" || !UUID.test(value)) throw new DirectoryError();
  return value.toLowerCase();
}
function detail(value, id) {
  const base = page({ items: [value], next_cursor: null }, null).items[0];
  if (base.id !== id || ![null, "Female", "Male", "Intersex", "Prefer not to say"].includes(value.sex) ||
      !timestamp(value.created_at)) throw new DirectoryError();
  const result = { ...base, sex: value.sex, created_at: value.created_at };
  for (const [field, limit] of [["contact_reference", 60], ["history", 1000], ["allergies", 500], ["current_medicines", 500]]) {
    const item = value[field];
    if (!(item === null || (typeof item === "string" && item.length <= limit && !item.includes("\0")))) throw new DirectoryError();
    result[field] = item;
  }
  return result;
}
function registration(value, id) {
  if (!value || patientId(value.id) !== id || !text(value.reference, 30) ||
      typeof value.preferred_language !== "string" || !Object.hasOwn(languages, value.preferred_language) ||
      !timestamp(value.created_at) || !["enabled", "blocked"].includes(value.care_access) ||
      !Array.isArray(value.consents) || value.consents.length < 1 || value.consents.length > 3) throw new DirectoryError();
  const purposes = new Set();
  const consents = value.consents.map((consent) => {
    if (!consent || !["care", "uploads", "future_ai"].includes(consent.purpose) ||
        purposes.has(consent.purpose) || typeof consent.granted !== "boolean" ||
        !text(consent.notice_version, 40) || typeof consent.language !== "string" ||
        !Object.hasOwn(languages, consent.language) || !timestamp(consent.created_at) ||
        !(consent.revoked_at === null || (timestamp(consent.revoked_at) &&
          Date.parse(consent.revoked_at) >= Date.parse(consent.created_at)))) throw new DirectoryError();
    const expected = consent.revoked_at !== null ? "revoked" : consent.granted ? "granted" : "declined";
    if (consent.status !== expected) throw new DirectoryError();
    purposes.add(consent.purpose);
    return { id: patientId(consent.id), purpose: consent.purpose, status: consent.status,
      granted: consent.granted, notice_version: consent.notice_version, language: consent.language,
      created_at: consent.created_at, revoked_at: consent.revoked_at };
  });
  const care = consents.find((consent) => consent.purpose === "care");
  if (!care || value.care_access !== (care.status === "granted" ? "enabled" : "blocked")) throw new DirectoryError();
  return { id, reference: value.reference, preferred_language: value.preferred_language,
    created_at: value.created_at, care_access: value.care_access, consents };
}

export async function patientDetail(id, options) {
  id = patientId(id);
  return detail(await read(`/api/v1/patients/${id}`, options), id);
}
export async function patientRegistration(id, options) {
  id = patientId(id);
  return registration(await read(`/api/v1/patients/${id}/registration`, options), id);
}
export function patientReadMessage(error) {
  if ([403, 404].includes(error.status)) return "Patient record unavailable.";
  if (error.status === 503) return "Patient record temporarily unavailable. Please try again.";
  return "Could not load the patient record. Please try again.";
}
