/** Tab-scoped synthetic demo state. This is not an authentication boundary. */
const KEY = "dhanvantari.phase1";
export const languages = Object.freeze({
  en: "English",
  hi: "Hindi",
  bn: "Bengali",
  ta: "Tamil",
  te: "Telugu",
  mr: "Marathi",
  gu: "Gujarati",
  pa: "Punjabi",
});
export const samples = [
  { id: "DEMO-001", name: "Asha Demo", age: 36, language: "hi" },
  { id: "DEMO-002", name: "Ravi Sample", age: 48, language: "en" },
  { id: "DEMO-003", name: "Meena Example", age: 29, language: "ta" },
];
export function readState() {
  try {
    const parsed = JSON.parse(sessionStorage.getItem(KEY) || "{}");
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? parsed
      : {};
  } catch {
    return {};
  }
}
export function saveState(patch) {
  try {
    sessionStorage.setItem(KEY, JSON.stringify({ ...readState(), ...patch }));
    return true;
  } catch {
    return false;
  }
}
export function clearState() {
  try {
    sessionStorage.removeItem(KEY);
  } catch {
    /* Storage may be unavailable. */
  }
}
export function patients() {
  const entries = readState().patients;
  return [
    ...samples,
    ...(Array.isArray(entries)
      ? entries.filter(
          (p) =>
            p &&
            typeof p.name === "string" &&
            typeof p.id === "string" &&
            Number.isInteger(p.age) &&
            languages[p.language],
        )
      : []),
  ];
}
