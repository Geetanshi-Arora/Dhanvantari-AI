# Frontend authentication and local verification

Email/password sign-in now calls the existing FastAPI authentication endpoints.
The authenticated server profile, not the selected browser role, determines access.
No backend authentication implementation, database grants or migrations were changed.

## Local Windows setup

These are manual instructions; implementing and testing this frontend did not start
FastAPI, connect to Supabase or change any local credentials or database records.

1. Open the reviewed Dhanvantari checkout in VS Code. Ensure it contains this local
   implementation; the changes have not been committed or pushed automatically.
2. Keep `backend/.env` for administration only. If `backend/runtime.env` does not
   already exist, copy `backend/runtime.env.example` to it privately, without
   overwriting an existing file. Git ignores the real runtime file.
3. Privately configure the reviewed session pooler host, existing `runtime.dpapi`
   path, trusted certificate path, development Supabase URL/publishable key and
   existing session encryption key. Never enter a database password or an
   administrative URL into this runtime file. Reuse the existing DPAPI credential;
   do not regenerate it. Do not paste configuration or credentials into chat.
4. Set local `ALLOWED_ORIGIN=http://localhost:8000`, `ENVIRONMENT=development` and
   `COOKIE_SECURE=false`. TLS to Supabase still uses `verify-full`; the local HTTP
   cookie setting does not disable database TLS verification.
5. Open a separate runtime PowerShell terminal at the repository root, without
   database URL or libpq override variables from an administrative session.
   The loader rejects conflicting settings; see `backend/README.md` for the list.
6. Start the backend manually, without a separate static server:

   ```powershell
   .\.venv\Scripts\python.exe -m uvicorn app.main:create_app --factory --app-dir .\backend --host 127.0.0.1 --port 8000
   ```

7. Open `http://localhost:8000`, select **Sign in**, select **Nurse** and enter the
   existing fictional nurse account privately in the form. The password is sent
   only to the same-origin login API; no Supabase SDK or API keys are in the frontend.
8. Successful nurses reach the dashboard. Confirm the name comes from the staff
   profile and all patient records are marked as synthetic. Language selection and
   registration still affect only fictional browser state; do not enter real data.
9. Use **Sign out** and then open `/dashboard.html`, `/language.html` or
   `/register.html` directly. Each must return to sign-in. Sign in using Doctor with
   an active fictional doctor account: it must open `/doctor.html`, and nurse-only
   URLs must redirect back to that placeholder.

Normal real login/logout tests write authentication sessions, throttles and audit
entries as designed. No live test was performed during implementation.

## Provisioning blocker and failures

An account existing in Supabase Auth is insufficient: its verified UUID must also
have an active application StaffProfile with the correct profession records. An
inactive or unprovisioned account receives 403 and cannot reach a workspace. This
work does not inspect or create live staff records. If 403 occurs, stop and review
staff provisioning separately; do not grant roles or modify records as a workaround.

Wrong credentials/expired sessions, denied staff access, rate limits and service
failures have fixed helpful messages. Provider/database response bodies are never
displayed. If a selected role does not match the returned role, the frontend attempts
CSRF-protected revocation and asks the user to select the correct workspace. Any
revocation failure remains explicit and exposes a retry-sign-out button.

Logout retrieves `/api/v1/auth/csrf`, then posts `/api/v1/auth/logout` with the
`x-csrf-token` header. Only a successful logout response triggers demo-state clearing
and redirect. If `/csrf` returns 401, logout still attempts to clear a missing
session. A persisted expired/inactive server session may prevent token retrieval or
cause logout rejection; the UI says revocation is unconfirmed instead of claiming
success. Sign in again or review the session state with an authorized administrator.
No CSRF or authentication protections were weakened to bypass this condition.

## Security and verification scope

Protected HTML starts with main/workspace sections hidden and calls `/auth/me`
before initializing demo forms or rendering a verified staff name. Failed checks
remain hidden. Back-forward cache restores trigger revalidation. Static HTML still
contains public synthetic templates; browser hiding is not access control for real
clinical records. Backend APIs remain the security boundary.

JavaScript uses `credentials: "same-origin"`, disallows redirected fetches, uses
bounded request timeouts and keeps CSRF values only in memory. Session cookies are
HttpOnly. The demo store only permits language, synthetic patients and registration
status; browser role/name values are not used for authentication.

`node tests/smoke.cjs` uses local file interception, browser API doubles and fictional
identities with no listening server. Backend pytest uses mocked Supabase Auth and
disposable PostgreSQL. Together they exercise the frontend contract and real backend
session implementation, but are not a live Supabase end-to-end authentication test.
