import {
  languages,
  readState,
  saveState,
  clearState,
  patients,
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
document.querySelector("[data-logout]")?.addEventListener("click", () => {
  clearState();
  window.location.href = "login.html";
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

// Demo role selection never requests or stores a password.
const login = $("#login-form");
if (login) {
  let role = "nurse";
  document.querySelectorAll("[data-role]").forEach((button) => {
    button.addEventListener("click", () => {
      role = button.dataset.role;
      document.querySelectorAll("[data-role]").forEach((option) => {
        const selected = option === button;
        option.classList.toggle("selected", selected);
        option.setAttribute("aria-pressed", String(selected));
      });
      $("#doctor-roadmap").hidden = true;
      $("#login-submit").textContent =
        role === "nurse" ? "Enter nurse demo →" : "View doctor roadmap →";
    });
  });
  login.addEventListener("submit", (event) => {
    event.preventDefault();
    if (role === "doctor") {
      $("#doctor-roadmap").hidden = false;
      return;
    }
    const displayName = $("#demo-name").value.trim() || "Demo nurse";
    if (!saveState({ displayName, role: "nurse" })) {
      notify(storageMessage);
      return;
    }
    window.location.href = "dashboard.html";
  });
}

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
  $("#greeting-name").textContent =
    typeof state.displayName === "string" ? state.displayName : "Demo nurse";
  $("#dashboard-language").textContent =
    languages[state.language] || "Not selected";
  $("#patient-count").textContent = String(patients().length);
  function renderPatients() {
    const query = $("#patient-search").value.trim().toLowerCase();
    const results = patients().filter((patient) =>
      `${patient.name} ${patient.id}`.toLowerCase().includes(query),
    );
    $("#patient-rows").replaceChildren();
    results.forEach((patient) => {
      const row = document.createElement("tr");
      [
        patient.name,
        patient.id,
        patient.age,
        languages[patient.language],
        "Demo only",
      ].forEach((value, index) => {
        const cell = document.createElement("td");
        if (index === 4) {
          const badge = document.createElement("span");
          badge.className = "record-badge";
          badge.textContent = value;
          cell.append(badge);
        } else {
          cell.textContent = String(value);
        }
        row.append(cell);
      });
      $("#patient-rows").append(row);
    });
    $("#empty-search").hidden = results.length !== 0;
  }
  $("#patient-search").addEventListener("input", renderPatients);
  renderPatients();
  if (state.registrationSaved) {
    notify("Demo patient saved. No consultation has been created.");
    saveState({ registrationSaved: false });
  }
}

const registration = $("#registration-form");
if (registration) {
  const state = readState();
  if (languages[state.language]) $("#patient-language").value = state.language;
  const today = new Date();
  const localDate = [
    today.getFullYear(),
    String(today.getMonth() + 1).padStart(2, "0"),
    String(today.getDate()).padStart(2, "0"),
  ].join("-");
  $("#consultation-date").value = localDate;
  $("#patient-name").addEventListener("input", (event) => {
    event.target.setCustomValidity("");
  });
  registration.addEventListener("submit", (event) => {
    event.preventDefault();
    const data = new FormData(registration);
    const name = String(data.get("name")).trim();
    if (!name) {
      $("#patient-name").setCustomValidity("Enter a fictional patient name.");
      $("#patient-name").reportValidity();
      return;
    }
    if (!registration.reportValidity()) return;
    const age = Number(data.get("age"));
    const language = String(data.get("language"));
    if (!Number.isInteger(age) || age < 0 || age > 120 || !languages[language])
      return;
    const existing = patients().filter(
      (p) => !["DEMO-001", "DEMO-002", "DEMO-003"].includes(p.id),
    );
    const patient = {
      id: `DEMO-${crypto.randomUUID().slice(0, 8).toUpperCase()}`,
      name,
      age,
      language,
      sex: String(data.get("sex")),
      contact: String(data.get("contact")).trim(),
      date: String(data.get("date")),
      history: String(data.get("history")).trim(),
      allergies: String(data.get("allergies")).trim(),
      medicines: String(data.get("medicines")).trim(),
      simulatedConsent: data.get("consent") === "on",
    };
    if (
      !saveState({
        patients: [...existing, patient],
        language,
        registrationSaved: true,
      })
    ) {
      $("#registration-error").textContent = storageMessage;
      $("#registration-error").hidden = false;
      return;
    }
    window.location.href = "dashboard.html";
  });
}
