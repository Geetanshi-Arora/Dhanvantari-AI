import { signIn, currentStaff, signOut, authMessage } from "./auth.js";
import { initializeVoiceDemo } from "./voice-demo.js";
import { createVoiceClient } from "./voice-api.js";
import { patientDirectory, directoryMessage, patientDetail, patientRegistration, patientReadMessage } from "./patients-api.js";
import {
  languages,
  readState,
  saveState,
  clearState,
} from "./store.js";

const $ = (selector) => document.querySelector(selector);
const storageMessage =
  "Temporary browser storage is unavailable. Enable session storage to use the demo.";
const menu = $(".menu-toggle");
menu?.addEventListener("click", () => {
  const open = menu.getAttribute("aria-expanded") !== "true";
  menu.setAttribute("aria-expanded", String(open));
  $("#navigation").classList.toggle("open", open);
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && menu) {
    menu.setAttribute("aria-expanded", "false");
    $("#navigation").classList.remove("open");
  }
});
let toastTimer;
function notify(message) {
  const toast = $(".toast");
  toast.textContent = message;
  toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    toast.hidden = true;
  }, 6000);
}

const workspace = document.body.dataset.requiredRole;
const destination = (role) => role === "doctor" ? "doctor.html" : "dashboard.html";
let clearDirectory = () => {};
let voiceDemo = { clear() {}, setPatientContext() {} };
const voiceClient = createVoiceClient(); // Always disabled; no active visit or live transport is bound.
const hideWorkspace = () => {
  voiceClient.clear();
  voiceDemo.clear();
  clearDirectory();
  if (!workspace) return;
  $("#main").hidden = true;
  $(".workspace-bar").hidden = true;
};
window.addEventListener("pagehide", hideWorkspace);
window.addEventListener("pageshow", (event) => {
  if (event.persisted && workspace) {
    hideWorkspace();
    window.location.reload();
  }
});

async function requireWorkspace() {
  if (!workspace) return null;
  try {
    const staff = await currentStaff();
    if (staff.role !== workspace) {
      window.location.replace(destination(staff.role));
      return null;
    }
    return staff;
  } catch (error) {
    if (error.status === 401) {
      clearState();
      window.location.replace("login.html?reason=expired");
    } else {
      $("#auth-status-message").textContent = authMessage(error);
      $("#auth-retry").hidden = false;
    }
    return null;
  }
}
$("#auth-retry")?.addEventListener("click", () => window.location.reload());

function loginError(message) {
  const target = $("#login-error");
  if (target) {
    target.textContent = message;
    target.hidden = !message;
  } else notify(message);
}
document.querySelectorAll("[data-logout]").forEach((button) => {
  button.addEventListener("click", async () => {
    if (button.disabled) return;
    button.disabled = true;
    try {
      await signOut();
      clearState();
      hideWorkspace();
      window.location.replace("login.html");
    } catch (error) {
      loginError(authMessage(error, "logout"));
      button.disabled = false;
    }
  });
});

const login = $("#login-form");
if (login) {
  let role = "nurse";
  let busy = false;
  const roleButtons = [...document.querySelectorAll("[data-role]")];
  roleButtons.forEach((button) => {
    button.addEventListener("click", () => {
      if (busy) return;
      role = button.dataset.role;
      roleButtons.forEach((option) => {
        const selected = option === button;
        option.classList.toggle("selected", selected);
        option.setAttribute("aria-pressed", String(selected));
      });
      loginError("");
    });
  });
  if (new URLSearchParams(window.location.search).get("reason") === "expired") {
    loginError("Your session has expired. Please sign in again.");
  }
  login.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (busy || !login.reportValidity()) return;
    const email = $("#login-email").value.trim();
    let password = $("#login-password").value;
    if (!email || email.length > 254 || !password || password.length > 1024) {
      loginError("Enter a valid email address and password.");
      return;
    }
    busy = true;
    loginError("");
    const controls = [...login.querySelectorAll("input, button"), ...roleButtons];
    controls.forEach((control) => { control.disabled = true; });
    $("#login-submit").textContent = "Signing in…";
    $("#login-password").value = "";
    try {
      const staff = await signIn(email, password);
      password = "";
      clearState();
      if (staff.role !== role) {
        const message = `This account belongs to the ${staff.role} workspace. Select ${staff.role === "doctor" ? "Doctor" : "Nurse"} and sign in again.`;
        try {
          await signOut();
          loginError(message);
        } catch (error) {
          loginError(`${message} ${authMessage(error, "logout")}`);
          $("#login-logout").hidden = false;
        }
        return;
      }
      window.location.replace(destination(staff.role));
    } catch (error) {
      loginError(authMessage(error));
    } finally {
      password = "";
      busy = false;
      controls.forEach((control) => { control.disabled = false; });
      $("#login-submit").textContent = "Sign In →";
    }
  });
}

const registrationForm = $("#registration-form");
if (registrationForm) {
  registrationForm.addEventListener("submit", (event) => event.preventDefault());
  registrationForm.setAttribute("aria-disabled", "true");
  registrationForm.querySelectorAll("input, select, textarea, button").forEach((control) => {
    control.disabled = true;
  });
  registrationForm.querySelector('[type="submit"]').textContent = "Registration unavailable";
  $("#registration-error").textContent =
    "Patient registration is unavailable until approved consent notices are published.";
  $("#registration-error").hidden = false;
}

const staff = await requireWorkspace();
if (!workspace || staff) initializeWorkspace(staff);

function initializeWorkspace(staff) {
  if ($(".language-grid")) {
    let selected = readState().language;
    function updateLanguage() {
      document.querySelectorAll("[data-language]").forEach((button) => {
        const active = button.dataset.language === selected;
        button.classList.toggle("selected", active);
        button.setAttribute("aria-pressed", String(active));
      });
      $("#language-continue").disabled = !languages[selected];
      $("#language-status").textContent = languages[selected]
        ? `${languages[selected]} selected · Demo preference only`
        : "Select a language to continue.";
    }
    document.querySelectorAll("[data-language]").forEach((button) => {
      button.addEventListener("click", () => {
        selected = button.dataset.language;
        updateLanguage();
      });
    });
    $("#language-continue").addEventListener("click", () => {
      if (!languages[selected]) return;
      if (!saveState({ language: selected })) {
        notify(storageMessage);
        return;
      }
      window.location.href = "register.html";
    });
    updateLanguage();
  }

  if ($("#patient-rows")) {
    const state = readState();
    // Discard legacy demo records; only the non-clinical language preference survives.
    if (Object.hasOwn(state, "patients") || Object.hasOwn(state, "registrationSaved")) {
      clearState();
      if (Object.hasOwn(languages, state.language)) saveState({ language: state.language });
    }
    $("#greeting-name").textContent = staff.display_name;
    $("#dashboard-language").textContent = languages[state.language] || "Not selected";
    voiceDemo = initializeVoiceDemo($("#voice-demo"));
    initializeDirectory();
  }

  const registration = $("#registration-form");
  if (registration) {
    if (Object.hasOwn(languages, readState().language)) {
      $("#patient-language").value = readState().language;
    }
    initializeRegistrationPreview(registration);
  }

  if (workspace) {
    $("#auth-status").hidden = true;
    $("#main").hidden = false;
    $(".workspace-bar").hidden = false;
  }
}


function initializeRegistrationPreview(form) {
  // Preview only: no payload construction, notice approval, persistence or API writes.
  form.removeAttribute("aria-disabled");
  form.querySelectorAll("input, select, textarea").forEach((control) => {
    control.disabled = false;
  });
  const care = $("#consent-care");
  const clinical = [$("#patient-history"), $("#patient-allergies"), $("#patient-medicines")];
  function updateClinical() {
    const blocked = care.value !== "granted";
    clinical.forEach((control) => {
      if (blocked) {
        control.value = "";
        control.setCustomValidity("");
        control.removeAttribute("aria-invalid");
      }
      control.disabled = blocked;
    });
  }
  function validate(control) {
    if (!control.matches("input, select, textarea") || control.disabled) return;
    const value = control.value;
    let message = "";
    if (control.id === "patient-name" && !value.trim()) {
      message = "Enter a fictional patient name (1–80 characters).";
    } else if (control.id === "patient-age" &&
               (!value || !Number.isInteger(control.valueAsNumber) ||
                control.valueAsNumber < 18 || control.valueAsNumber > 120)) {
      message = "Enter a whole-number age from 18 to 120.";
    } else if (control.id === "patient-language" && !Object.hasOwn(languages, value)) {
      message = "Select a supported patient language.";
    } else if (control.id === "consent-care" && !["granted", "declined"].includes(value)) {
      message = "Select granted or declined for care in this preview.";
    } else if (value.includes("\0")) {
      message = "Remove unsupported control characters.";
    } else if (control.maxLength >= 0 && value.length > control.maxLength) {
      message = `Use no more than ${control.maxLength} characters.`;
    }
    control.setCustomValidity(message);
    control.setAttribute("aria-invalid", String(Boolean(message)));
  }
  form.addEventListener("input", (event) => validate(event.target));
  form.addEventListener("change", (event) => {
    if (event.target === care) updateClinical();
    validate(event.target);
  });
  form.addEventListener("invalid", (event) => validate(event.target), true);
  updateClinical();
}

function initializeDirectory() {
  const records = new Map();
  const cursors = new Set();
  let next = null;
  let busy = false;
  let generation = 0;
  let pending;
  const rows = $("#patient-rows");
  const search = $("#patient-search");
  const more = $("#patient-load-more");
  const retry = $("#patient-retry");
  const status = $("#directory-status");
  const error = $("#directory-error");

  function clear() {
    viewer.clear();
    generation++;
    pending?.abort();
    records.clear();
    cursors.clear();
    next = null;
    busy = false;
    rows.replaceChildren();
    search.value = "";
    search.disabled = true;
    $("#patient-count").textContent = "—";
    $("#directory-table").hidden = true;
    $("#directory-empty").hidden = true;
    $("#empty-search").hidden = true;
    more.hidden = true;
    retry.hidden = true;
    status.textContent = "";
    error.hidden = true;
    $("#patient-directory").setAttribute("aria-busy", "false");
  }
  const viewer = initializePatientViewer({
    blocked: (id) => { records.delete(id); render(); },
    denied: clear,
  });
  clearDirectory = clear;

  function render() {
    const query = search.value.trim().toLowerCase();
    rows.replaceChildren();
    for (const patient of records.values()) {
      if (!`${patient.name} ${patient.reference}`.toLowerCase().includes(query)) continue;
      const row = document.createElement("tr");
      for (const [index, value] of [patient.name, patient.reference, patient.age,
                           languages[patient.preferred_language], "Fictional record"].entries()) {
        const cell = document.createElement("td");
        if (index === 4) {
          const badge = document.createElement("span");
          badge.className = "record-badge";
          badge.textContent = value;
          cell.append(badge);
          const button = document.createElement("button");
          button.type = "button";
          button.className = "text-button";
          button.textContent = "View record";
          button.setAttribute("aria-label", `View record ${patient.reference}`);
          button.addEventListener("click", () => viewer.open(patient.id));
          cell.append(button);
        } else cell.textContent = String(value);
        row.append(cell);
      }
      rows.append(row);
    }
    $("#patient-count").textContent = String(records.size);
    $("#directory-table").hidden = records.size === 0;
    $("#directory-empty").hidden = records.size !== 0;
    $("#empty-search").hidden = records.size === 0 || rows.children.length !== 0;
    search.disabled = records.size === 0;
  }

  async function load() {
    if (busy) return;
    busy = true;
    const version = generation;
    pending = new AbortController();
    more.disabled = true;
    retry.hidden = true;
    error.hidden = true;
    status.textContent = "Loading authorized patient records…";
    $("#patient-directory").setAttribute("aria-busy", "true");
    try {
      const result = await patientDirectory(next, { signal: pending.signal });
      if (version !== generation) return;
      if (result.next_cursor && cursors.has(result.next_cursor)) {
        throw new Error("Directory pagination unavailable");
      }
      if (result.next_cursor) cursors.add(result.next_cursor);
      result.items.forEach((patient) => records.set(patient.id, patient));
      next = result.next_cursor;
      render();
      more.hidden = next === null;
      status.textContent = `${records.size} authorized records loaded. Search covers loaded records only.`;
    } catch (failure) {
      if (version !== generation) return;
      clear();
      if (failure.status === 401) {
        hideWorkspace();
        clearState();
        window.location.replace("login.html?reason=expired");
        return;
      }
      error.textContent = directoryMessage(failure);
      error.hidden = false;
      retry.hidden = false;
    } finally {
      if (version === generation) {
        busy = false;
        more.disabled = false;
        $("#patient-directory").setAttribute("aria-busy", "false");
      }
    }
  }
  search.addEventListener("input", render);
  more.addEventListener("click", load);
  retry.addEventListener("click", load);
  load();
}


function initializePatientViewer({ blocked, denied }) {
  const panel = $("#patient-view");
  const message = $("#patient-view-message");
  const status = $("#patient-registration-content");
  const clinical = $("#patient-detail-content");
  let generation = 0;
  let pending;
  function clear() {
    voiceClient.clear();
    voiceDemo.clear();
    generation++;
    pending?.abort();
    status.replaceChildren();
    clinical.replaceChildren();
    message.textContent = "";
    panel.hidden = true;
    panel.setAttribute("aria-busy", "false");
  }
  function fields(container, heading, values) {
    const title = document.createElement("h4");
    title.textContent = heading;
    container.append(title);
    const grid = document.createElement("div");
    grid.className = "form-grid";
    values.forEach(([label, value]) => {
      const field = document.createElement("div");
      field.className = "field wide";
      const caption = document.createElement("label");
      const output = document.createElement("textarea");
      output.id = `record-output-${container.id}-${grid.children.length}-${container.children.length}`;
      caption.htmlFor = output.id;
      caption.textContent = label;
      output.readOnly = true;
      output.autocomplete = "off";
      output.rows = value !== null && String(value).length > 80 ? 3 : 1;
      output.textContent = value === null ? "Not recorded" : String(value);
      field.append(caption, output);
      grid.append(field);
    });
    container.append(grid);
  }
  function renderStatus(value) {
    fields(status, "Registration status", [["Patient reference", value.reference],
      ["Patient language", languages[value.preferred_language]], ["Registered at", value.created_at],
      ["Care access", value.care_access]]);
    for (const [purpose, label] of [["care", "Care consent"], ["uploads", "Uploads consent"], ["future_ai", "Future AI consent"]]) {
      const consent = value.consents.find((item) => item.purpose === purpose);
      fields(status, label, consent ? [["Decision status", consent.status],
        ["Recorded notice version", consent.notice_version], ["Notice language", languages[consent.language]],
        ["Decision recorded at", consent.created_at], ["Revoked at", consent.revoked_at]] : [["Decision status", "Not recorded"]]);
    }
  }
  async function open(id) {
    clear();
    const version = generation;
    pending = new AbortController();
    panel.hidden = false;
    panel.setAttribute("aria-busy", "true");
    message.textContent = "Loading patient record…";
    try {
      const state = await patientRegistration(id, { signal: pending.signal });
      if (version !== generation) return;
      // Never request clinical detail when care access is blocked.
      const detail = state.care_access === "enabled" ? await patientDetail(id, { signal: pending.signal }) : null;
      if (version !== generation) return;
      renderStatus(state);
      voiceDemo.setPatientContext({ language: state.preferred_language, careAccess: state.care_access });
      if (detail) fields(clinical, "Patient details", [["Name", detail.name], ["Age", detail.age],
        ["Age recorded on", detail.age_recorded_on], ["Sex", detail.sex],
        ["Contact reference", detail.contact_reference], ["History", detail.history],
        ["Allergies", detail.allergies], ["Current medicines", detail.current_medicines]]);
      else blocked(id);
      message.textContent = detail ? "Read-only patient record." : "Care access blocked. Clinical details are unavailable.";
    } catch (failure) {
      if (version !== generation) return;
      clear();
      if (failure.status === 401) {
        hideWorkspace();
        clearState();
        window.location.replace("login.html?reason=expired");
        return;
      }
      if ([403, 404].includes(failure.status)) denied();
      panel.hidden = false;
      message.textContent = patientReadMessage(failure);
    } finally {
      if (version === generation) panel.setAttribute("aria-busy", "false");
    }
  }
  $("#patient-view-close").addEventListener("click", clear);
  return { open, clear };
}
