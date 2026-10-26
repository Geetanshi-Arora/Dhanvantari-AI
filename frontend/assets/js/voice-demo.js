/** Fictional UI practice only. No providers, devices, playback, storage or I/O.
 * Language fixtures are illustrative text, not verified provider capabilities,
 * consent notices, translations of clinical information or medical advice.
 */
const NAMES = Object.freeze({
  en: "English", hi: "Hindi", bn: "Bengali", ta: "Tamil",
  te: "Telugu", mr: "Marathi", gu: "Gujarati", pa: "Punjabi",
});
const FIXTURES = Object.freeze({
  en: "This is a fictional practice sentence only.",
  hi: "यह केवल अभ्यास का एक काल्पनिक वाक्य है।",
  bn: "এটি কেবল অনুশীলনের জন্য একটি কাল্পনিক বাক্য।",
  ta: "இது பயிற்சிக்கான கற்பனை வாக்கியம் மட்டுமே.",
  te: "ఇది సాధన కోసం మాత్రమే ఒక కల్పిత వాక్యం.",
  mr: "हे केवळ सरावासाठी एक काल्पनिक वाक्य आहे.",
  gu: "આ માત્ર અભ્યાસ માટેનું એક કાલ્પનિક વાક્ય છે.",
  pa: "ਇਹ ਸਿਰਫ਼ ਅਭਿਆਸ ਲਈ ਇੱਕ ਕਾਲਪਨਿਕ ਵਾਕ ਹੈ।",
});
const SCENARIOS = new Set(["unavailable", "ready", "consent", "language", "backend"]);
const STAGES = Object.freeze({
  unavailable: "Not available",
  ready: "Ready · mock demonstration only",
  recording: "Recording · visual simulation only",
  processing: "Processing · mock only",
  draft: "Transcript ready · mock, unverified",
  confirmed: "Nurse confirmed · mock only",
});

export function initializeVoiceDemo(root) {
  if (!root) return Object.freeze({ clear() {}, setPatientContext() {} });
  const find = (id) => root.querySelector(`#${id}`);
  const language = find("voice-demo-language");
  const scenario = find("voice-demo-scenario");
  const stage = find("voice-demo-stage");
  const reason = find("voice-demo-reason");
  const context = find("voice-demo-context");
  const languageLabel = find("voice-demo-language-label");
  const original = find("voice-demo-original");
  const correction = find("voice-demo-correction");
  const reviewStatus = find("voice-demo-review-status");
  const preview = find("voice-demo-output");
  const start = find("voice-demo-start");
  const stop = find("voice-demo-stop");
  const reveal = find("voice-demo-reveal");
  const apply = find("voice-demo-apply");
  const confirm = find("voice-demo-confirm");
  const output = find("voice-demo-preview");
  let state = "unavailable";
  let selectedRecord = false;
  let blockedCare = false;
  let reviewed = null;

  const validLanguage = () => Object.hasOwn(NAMES, language.value);
  function textIsValid() {
    const value = correction.value;
    if (typeof value !== "string" || !value.trim() || value.length > 8000 || value.includes("\0")) return false;
    // Reject unpaired UTF-16 surrogates, just as backend UTF-8 validation does.
    for (let i = 0; i < value.length; i++) {
      const code = value.charCodeAt(i);
      if (code >= 0xd800 && code <= 0xdbff) {
        const next = value.charCodeAt(++i);
        if (!(next >= 0xdc00 && next <= 0xdfff)) return false;
      } else if (code >= 0xdc00 && code <= 0xdfff) return false;
    }
    return true;
  }
  function unavailableReason() {
    if (blockedCare) return "Care consent is declined, revoked or unavailable. Voice actions are unavailable for this record.";
    if (!validLanguage()) return "Choose one of the eight patient languages for a fictional practice example. No language is substituted.";
    if (scenario.value === "consent") return "Separate approved recording and external-processing permission is missing. Care, uploads and future-AI consent do not grant voice permission.";
    if (scenario.value === "language") return "Provider support for this language is unverified. Speech recognition and speech output must each be verified; no fallback is used.";
    if (scenario.value === "backend") return "Backend voice capability is disabled. Live processing remains unavailable.";
    return "Live voice is not available. Select the fictional ready scenario to explore the interface without recording or playback.";
  }
  function render() {
    stage.textContent = STAGES[state];
    root.dataset.voiceStage = state;
    const label = validLanguage() ? `${NAMES[language.value]} (${language.value})` : "No language selected";
    languageLabel.textContent = label;
    find("voice-demo-review-language").textContent = label;
    find("voice-demo-output-language").textContent = label;
    const editable = state === "draft";
    start.disabled = state !== "ready";
    stop.disabled = state !== "recording";
    reveal.disabled = state !== "processing";
    correction.disabled = !editable;
    apply.disabled = !editable;
    confirm.disabled = !editable || reviewed === null || reviewed !== correction.value || !textIsValid();
    output.disabled = state !== "confirmed";
    find("voice-demo-transcript").hidden = !["draft", "confirmed"].includes(state);
    // These controls have no live handlers and can never be enabled by this module.
    find("voice-live-record").disabled = true;
    find("voice-live-play").disabled = true;
    const code = validLanguage() ? language.value : "en";
    original.setAttribute("lang", code);
    correction.setAttribute("lang", code);
    preview.setAttribute("lang", code);
  }
  function erase() {
    reviewed = null;
    original.textContent = "";
    correction.value = "";
    preview.textContent = "No speech has been generated or played.";
    reviewStatus.textContent = "Unverified · fictional example only.";
    correction.setAttribute("aria-invalid", "false");
  }
  function resetPractice() {
    erase();
    state = !blockedCare && validLanguage() && scenario.value === "ready" ? "ready" : "unavailable";
    reason.textContent = state === "ready"
      ? "Fictional practice only. No consent is granted, audio captured, provider contacted or patient record updated."
      : unavailableReason();
    render();
  }
  function clear() {
    selectedRecord = false;
    blockedCare = false;
    language.value = "";
    language.disabled = false;
    scenario.value = "unavailable";
    context.textContent = "No record selected. You may explore an independent fictional practice example.";
    resetPractice();
  }
  language.addEventListener("change", resetPractice);
  scenario.addEventListener("change", () => {
    if (!SCENARIOS.has(scenario.value)) scenario.value = "unavailable";
    resetPractice();
  });
  find("voice-demo-reset").addEventListener("click", resetPractice);
  start.addEventListener("click", () => {
    if (start.disabled || state !== "ready" || blockedCare || !validLanguage() || scenario.value !== "ready") return;
    state = "recording";
    reason.textContent = "Visual simulation only. No microphone is connected and no audio is captured. Select Stop simulation to continue.";
    render();
    stop.focus();
  });
  stop.addEventListener("click", () => {
    if (stop.disabled || state !== "recording") return;
    state = "processing";
    reason.textContent = "Mock processing only. Nothing is uploaded. Select Show mock transcript to reveal fixed fictional text.";
    render();
    reveal.focus();
  });
  reveal.addEventListener("click", () => {
    if (reveal.disabled || state !== "processing" || !validLanguage()) return;
    original.textContent = FIXTURES[language.value];
    correction.value = FIXTURES[language.value];
    state = "draft";
    reason.textContent = "Review the fixed fictional sentence. Changes never replace the original. Nurse confirmation is not clinical approval.";
    render();
    correction.focus();
  });
  correction.addEventListener("input", () => {
    if (state !== "draft") return;
    reviewed = null;
    reviewStatus.textContent = "Unverified · apply your mock review again after editing.";
    correction.setAttribute("aria-invalid", "false");
    render();
  });
  apply.addEventListener("click", () => {
    if (apply.disabled || state !== "draft") return;
    if (!textIsValid()) {
      reviewed = null;
      correction.setAttribute("aria-invalid", "true");
      reviewStatus.textContent = "Enter fictional text only: 1–8,000 characters, with no invalid text characters.";
    } else {
      reviewed = correction.value;
      correction.setAttribute("aria-invalid", "false");
      reviewStatus.textContent = "Mock review applied. Explicit nurse confirmation is still required.";
    }
    render();
  });
  confirm.addEventListener("click", () => {
    if (confirm.disabled || state !== "draft" || reviewed === null || reviewed !== correction.value || !textIsValid()) return;
    state = "confirmed";
    reviewStatus.textContent = "Nurse confirmed · demonstration only. No clinical decision or patient record has been created.";
    render();
    reviewStatus.focus();
  });
  output.addEventListener("click", () => {
    if (output.disabled || state !== "confirmed" || reviewed === null) return;
    preview.textContent = reviewed;
    // Text preview only: never construct an audio element or trigger playback.
  });
  clear();
  return Object.freeze({
    clear,
    setPatientContext(value) {
      clear();
      if (!value || !Object.hasOwn(NAMES, value.language)) return;
      selectedRecord = true;
      blockedCare = value.careAccess !== "enabled";
      language.value = value.language;
      language.disabled = selectedRecord;
      context.textContent = "Selected record language shown below. Practice text is fictional and is not attached to this patient or a visit.";
      resetPractice();
    },
  });
}
