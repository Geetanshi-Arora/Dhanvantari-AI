import { signIn, currentStaff, signOut, authMessage } from "./auth.js";
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
const hideWorkspace = () => {
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
    $("#greeting-name").textContent = staff.display_name;
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

  if (workspace) {
    $("#auth-status").hidden = true;
    $("#main").hidden = false;
    $(".workspace-bar").hidden = false;
  }
}
