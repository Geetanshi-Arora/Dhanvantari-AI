# Dhanvantari AI

A multilingual, nurse-assisted healthcare support platform for rural India.

The existing frontend uses FastAPI's Supabase-backed staff authentication. Nurses
and doctors sign in with email and password; the verified active staff profile
determines their role. Nurse registration and case listing/detail use FastAPI and
PostgreSQL. The active registration flow is restricted to fictional synthetic adults
and records clearly marked draft consent metadata with no notice text. It is not
valid for real-patient use. Gnani and Gemini adapters remain tested but disconnected
from the workflow. No live speech, clinical AI, doctor review, WhatsApp or
autonomous prescribing is connected.

## Run locally

Use Python 3.12 and the backend virtual environment. Configure the dedicated
`backend/runtime.env` using `backend/runtime.env.example`, the existing private
Windows DPAPI credential and your reviewed development pooler host. Keep database
passwords and administrator credentials out of the runtime configuration.

From the repository root, in a separate runtime terminal:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:create_app --factory --app-dir .\backend --host 127.0.0.1 --port 8000
```

Open **http://localhost:8000** and choose **Sign in**. Use the configured origin
exactly; `localhost` and `127.0.0.1` differ. `ALLOWED_ORIGIN=http://localhost:8000`
and `COOKIE_SECURE=false` are for this local development setup only.
A standalone static server cannot authenticate staff or open protected workflows.

See [step-by-step login testing](docs/frontend-authentication.md) and
[backend setup](backend/README.md) before starting. Supabase Auth users also need
active application staff profiles; creating those records requires separate approval.

## Explore the authenticated prototype

1. Choose Nurse or Doctor, enter your fictional development account and sign in.
2. Nurses reach the case dashboard; doctors reach a separate placeholder.
3. Nurses can register fictional test patients through the backend. Registration,
   case creation and draft consent metadata are stored atomically in PostgreSQL.
4. The dashboard reads assigned cases from the database. Search is sent to the API;
   browser storage is not used for patient records.
5. Draft notices have no text and cannot be used with real patients. Officially
   approved, published notices are required before real-patient workflows can exist.
6. Sign out revokes the backend session using CSRF protection, clears demo state,
   and returns to the login page. Failed revocation is reported explicitly.

Passwords, Supabase tokens and session tokens are never stored by frontend
JavaScript in localStorage or sessionStorage. The existing HttpOnly cookie carries
the backend session. Browser role selection never grants authorization. Frontend
page guards are a usability measure; every clinical API must enforce its own
verified identity, permissions and consent requirements.

## Files

- `frontend/login.html`: real staff sign-in.
- `frontend/doctor.html`: authenticated doctor placeholder, with no clinical actions.
- `frontend/dashboard.html`, `language.html`, `register.html`: guarded nurse case workflow and language selection.
- `frontend/assets/js/auth.js`: same-origin cookie authentication client.
- `frontend/assets/js/app.js`: navigation, guards, case dashboard and registration interactions.
- `frontend/assets/js/store.js`: non-clinical tab state only; patient records come from the backend.
- `frontend/assets/css/styles.css`: existing shared theme, unchanged.
- `backend/`: FastAPI configuration, services and automated tests.
- `database/migrations/`: staged Alembic migrations, including the case lifecycle foundation.

## Validate

The browser suite uses intercepted local files and fictional API responses. It
starts **no server**, connects to no database or Supabase project, and makes no
external requests. Node.js, Playwright and Chromium are development prerequisites.

```sh
node tests/smoke.cjs
```

`CHROMIUM_PATH` selects Chromium (default `/usr/bin/chromium`). The suite covers
login/role routing, page hiding during verification, missing/expired sessions,
error handling, CSRF logout and failed revocation, synthetic registration/search,
browser storage, back-forward cache restoration and mobile layout. Mocked browser
tests do not establish live Supabase compatibility; the backend tests independently
verify the real session and authorization code using disposable PostgreSQL.

See [architecture](docs/architecture.md) for the implementation status and target
case workflow. Voice intake, AI structuring, observations, safety screening, doctor
review/approval, instruction delivery and follow-up remain future work.
