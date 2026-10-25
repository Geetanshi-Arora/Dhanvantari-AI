# Phase 2, stage 1: backend foundation and authentication

FastAPI, SQLAlchemy, PostgreSQL migrations, Supabase Auth, server-side sessions,
CSRF protection, role dependencies and append-only authentication audit events are implemented.
The initial migration includes all planned clinical entities, RLS read policies and
urgent-escalation storage. Clinical write permissions, patient APIs, file storage
endpoints and the doctor review UI are reserved for subsequent stages.

The Phase 1 frontend is unchanged. Its demo login still does **not** authenticate
with the backend; its forms still use fictional tab-scoped data. Never use that
demo to enter actual patient information. Backend authentication is independently
testable through the APIs below. No production deployment or remote migration is performed.

## Runtime credential integration status

The normal FastAPI factory now reads **only `backend/runtime.env`**, using a fixed
path relative to the installed source. It never opens `backend/.env`. Alembic and
administrative provisioning keep their existing, separate `MIGRATION_DATABASE_URL`
configuration; neither their implementation nor migrations were changed.

The credential loader implements the confirmed current-user Windows DPAPI format:
`CryptUnprotectData` with flags `1` and ASCII entropy `PROJECT + ":" + ROLE`.
The decrypted UTF-8 JSON must contain exactly `project`, `role`, `commit` and
`password`, without duplicate keys. The first three values are pinned to the
reviewed Dhanvantari project, runtime role and credential-preparation commit
`7c47b25f59cfe6cc5606156b09375d56bcab7a6a`. This metadata commit remains fixed
when the application is updated; it identifies the existing credential's provenance.
The password must match `[A-Za-z0-9_-]{64}`. Empty or oversized encrypted files
(over 65536 bytes), malformed payloads and decryption failures are rejected.

The loader opens paths read-only using native Windows handles, rejects reparse
points in every component, and holds checked ancestor handles open until the read
finishes. No write/delete sharing is granted during the read. The immediate private
directory must have a protected DACL and one inheritable current-user FullControl
entry; both it and the file must be owned by the current user, with exactly one
current-user FullControl entry. Broader permissions fail closed; the loader never
repairs ACLs, creates files or regenerates credentials. A sharing conflict also fails
closed. No password fallback or administrator connection is used.

The DPAPI output is wiped before `LocalFree`, including validation/error paths,
and the mutable plaintext copy is cleared. Python's decoded JSON strings and the
password retained by SQLAlchemy cannot be reliably wiped; they remain process
secrets. Do not enable debugger-local capture or credential-bearing debug logs.

Automated tests use fictional data and native Windows API doubles. They do not
establish actual DPAPI or filesystem compatibility on Windows. **Before activation,
a separately reviewed local Windows verification must check the existing private
credential and restrictive ACLs without printing its contents, opening a database
connection or starting FastAPI.** Do not regenerate or overwrite the credential.

`runtime.env.example` documents the proposed local configuration. Do not create it
with real values as part of the current code-review step. It has no database URL or
database password: connection assembly fixes the project, restricted pooler username,
port 5432, database `postgres` and TLS `verify-full`. The exact session pooler host
must come from the previously verified Dhanvantari development project. The CA
file must exist at an absolute path. The encrypted credential must remain outside
the checkout. URL passwords are passed in an in-memory SQLAlchemy URL object,
never formatted into dotenv text or printed.

The future runtime file is ignored by Git. The Supabase publishable key and session
encryption key still require private configuration: reuse the existing session key
privately when activation is approved. No administrator credential belongs in this
file. Runtime dotenv interpolation is disabled, unknown keys are rejected, and the
server refuses database URL overrides or libpq service/host-address/options/password
overrides. Use a fresh terminal without `MIGRATION_DATABASE_URL`, `DATABASE_URL`,
`PGHOSTADDR`, `PGSERVICE`, `PGSERVICEFILE`, `PGOPTIONS`, `PGPASSWORD` or `PGPASSFILE`
for future server startup. Explicit TLS URL parameters take precedence over TLS
environment defaults; TLS verification must never be disabled.

Supabase Auth `/user` verification still precedes clinical role/UUID assertion on
login and authenticated requests. The readiness endpoint now runs `SELECT 1`
without applying a synthetic clinical identity. Existing transaction-local identity,
role reset, consent, authorization and RLS behavior remain unchanged.

## Run locally in VS Code

Prerequisites: Python 3.12+, PostgreSQL 15+ for a standalone local database or a
separate development Supabase project. For Windows, use VS Code with WSL2 for the
Linux PostgreSQL test tooling. A live Supabase Auth project is needed for actual
API login; automated tests use a local fake Auth adapter and real temporary PostgreSQL.

1. Open the repository folder in VS Code. Open **Terminal → New Terminal**.
2. Create a virtual environment at the repository root:

   ```sh
   python3 -m venv .venv
   source .venv/bin/activate
   python -m pip install -r backend/requirements-test.lock
   python -m pip install --no-deps -e backend
   ```

   Select `.venv/bin/python` using **Python: Select Interpreter**. The lock file
   records the versions used for the automated checks, including test tooling.
   For runtime-only installation use `python -m pip install -e backend` instead.

3. For administrative operations only, copy `backend/.env.example` to
   `backend/.env`. Replace the placeholders in that ignored local file. Generate the session key using the command in the
   example. Do not put credentials in frontend files, VS Code launch settings,
   shell history, issue comments or tracked configuration. Use environment-variable
   injection from a secret manager outside development.

4. Set up the development database using the migration instructions in
   `database/migrations/README.md`. Use the migration-owner connection only for
   migrations and staff provisioning; `DATABASE_URL` must use a separate restricted
   login. Add `?sslmode=verify-full&sslrootcert=/path/to/trusted-ca.crt` as appropriate
   for your Supabase connection. Obtain connection details and CA trust instructions
   from your project's Connect panel; do not disable TLS verification. Passwords in
   URLs must be URL-encoded.

   The reviewed runtime-isolation implementation requires revision `0003`.
   Its hierarchy is runtime LOGIN -> `dhanvantari_api` ->
   `dhanvantari_clinical_actor`, with no shared `authenticated` membership.
   Applying this revision or provisioning a live login requires separate approval.
   Keep `MIGRATION_DATABASE_URL` out of the normal server process; use a separate
   private administrative environment for migration/provisioning commands.

5. **Pending Windows verification and separate activation review:** the normal
   server now requires `backend/runtime.env`; `backend/.env` alone cannot start it.
   After the blocker above is resolved and activation is approved, use the backend directory:

   ```sh
   cd backend
   python -m uvicorn app.main:create_app --factory --reload --host 127.0.0.1 --port 8000
   ```

   Open `http://localhost:8000`. FastAPI serves the existing frontend and `/api/v1`
   together. Use exactly the origin in `ALLOWED_ORIGIN`; `127.0.0.1` and `localhost`
   are different origins. `COOKIE_SECURE=false` is permitted only for development.
   Outside development, HTTPS and secure cookies are mandatory.

6. Health endpoints:

   ```text
   GET /api/v1/health/live
   GET /api/v1/health/ready
   GET /openapi.json
   ```

   Readiness checks connectivity only, not schema access or live Supabase Auth availability.
   The app refuses to start with a superuser, RLS-bypass role or table-owner runtime
   connection. Interactive CDN-based API documentation is disabled to retain a
   strict content security policy; the OpenAPI JSON remains available.

The frontend can also run independently, retaining its original demo behavior:

```sh
python3 -m http.server 8000 --bind 127.0.0.1 --directory frontend
```

Stop the backend first if using the same port. This static server does not run API routes.

## Create development clinicians

In a **development** Supabase project, disable public signup and create two fictional
Auth users through the Supabase dashboard: one nurse and one doctor. Use unique
passwords and normal email verification. Do not commit their passwords or UUIDs.
Auth metadata and the frontend role selector do not grant application permissions.

From `backend/`, run the operator command once per identity:

```sh
python -m app.provision --id AUTH_USER_UUID --role nurse --name 'Nurse Fictional' --registration FICTIONAL-NURSE-001
python -m app.provision --id OTHER_AUTH_USER_UUID --role doctor --name 'Doctor Fictional' --registration FICTIONAL-DOCTOR-001
```

It requires `MIGRATION_DATABASE_URL`, verifies the identity exists in `auth.users`,
creates the staff and profession rows atomically, and records an audit event.
It will not silently change an existing user's role. For deactivation, an authorized
operator sets `app.staff_profiles.active=false` using the administrative database
connection. Deactivation takes effect on the next protected API request.

## Authentication contract

| Method and route | Behavior |
| --- | --- |
| `POST /api/v1/auth/login` | JSON `email`, `password`; exact `Origin` required; no role parameter |
| `GET /api/v1/auth/me` | Current verified active clinician's ID, display name and role |
| `GET /api/v1/auth/csrf` | CSRF token for the authenticated same-origin browser client |
| `POST /api/v1/auth/logout` | Exact `Origin` and `X-CSRF-Token` required for an existing session; deletes local session |

Use an HTTP client with a cookie jar. Supply `Origin: http://localhost:8000` when
using the example configuration. After login the client must retain the returned
cookies, obtain `/auth/csrf`, and send its token as `X-CSRF-Token` for mutations.
There is deliberately no bearer-token or demo-auth bypass endpoint.

For manual API checks without putting passwords into command history, use Python's
`getpass` and an in-memory `httpx.Client` in a VS Code terminal:

```python
from getpass import getpass
import httpx

with httpx.Client(base_url="http://localhost:8000") as client:
    response = client.post(
        "/api/v1/auth/login",
        headers={"Origin": "http://localhost:8000"},
        json={"email": input("Development clinician email: "), "password": getpass()},
    )
    print("Login status:", response.status_code)
    if response.is_success:
        print("Verified role:", client.get("/api/v1/auth/me").json()["role"])
        csrf = client.get("/api/v1/auth/csrf").json()["csrf_token"]
        print(
            "Logout status:",
            client.post(
                "/api/v1/auth/logout",
                headers={"Origin": "http://localhost:8000", "X-CSRF-Token": csrf},
            ).status_code,
        )
```

No password, provider token or cookie is printed. Do not log request/response
bodies or enable credential-bearing HTTP debug logs.

Sessions have a maximum one-hour lifetime, capped by the Supabase token lifetime;
there is no automatic refresh in this stage. Login again after expiry. Tokens are
verified against Supabase `/user` on every protected request, then staff membership
is checked with RLS. Provider failure denies access. Logout remains possible during
provider outages and after local expiry, using the previously obtained CSRF token.
Logout revokes the local session; it does not sign out every Supabase session.

Opaque session identifiers and CSRF tokens are stored only as hashes in PostgreSQL.
Provider access tokens are encrypted with the environment-supplied Fernet key;
Supabase refresh tokens are not retained. Rotating that key invalidates sessions.
Cookies use HttpOnly for the session, SameSite=Strict, `/api` scope, and Secure
outside local development. Authentication bodies are capped at 16 KiB, including
chunked bodies, and validation errors never echo submitted values.

A PostgreSQL-backed throttle limits login attempts by account and client address
across workers. All attempts count, including successes. Forwarded IP headers are
ignored; do not blindly trust client-supplied forwarding headers. A reverse proxy
must validate and strip them if later used. Expired sessions/throttle entries can
be deleted periodically by an administrative maintenance job; audit logs must not
be truncated or rewritten. Retention/export policy is an operator responsibility.

## Tests

From `backend/`, with the virtual environment active:

```sh
python -m pytest -q --cov=app --cov-report=term-missing
ruff check --config pyproject.toml app tests ../database/migrations
ruff format --check --config pyproject.toml app tests ../database/migrations
```

Tests create and destroy a temporary PostgreSQL server with `pgserver`; no live
Supabase credentials, patient data, Docker, remote database or SQLite substitution
is involved. They test migration execution, RLS/consent/assignment isolation, active
staff checks, privilege escalation, encrypted sessions, role authorization, CSRF,
login throttling, provider failures, prescription constraints, urgent-escalation
constraints, provisioning and append-only audits. Run as a normal non-root user.
The packaged PostgreSQL test tooling supports the Linux environment used here;
use WSL2 on Windows. Live Supabase Auth/network setup is a separate manual check.

Runtime-isolation regressions additionally exercise an upgrade from 0002, fresh
migrations with permissive creator defaults, ledger recreation, transactional
rollback after ledger verification failure, role membership restrictions and
identity cleanup across one reused pooled connection. Supabase-managed permissions
and default ACLs are represented by fictional local objects, not a live project.
See `database/migrations/README.md` for approval gates and pooler checks.

The original browser checks remain available from the repository root with the
frontend or backend serving port 8000:

```sh
node tests/smoke.cjs
```

## Next stages

1. Patient registration, consent, case assignments, visits, original-language
   symptoms, vital signs and history; add narrowly scoped clinical write policies.
2. Nurse-raised urgent flags with assigned-doctor priority queue and acknowledgement.
   Urgency is a human flag; it never performs diagnosis, approves care or waits for
   an external messaging integration.
3. Consent-gated private uploads, validation/quarantine/scanner interface and secure downloads.
4. Doctor review transitions and versioned decisions/prescriptions with transactional
   audit events. AI suggestions remain separate, unapproved source material.

Gemini, Gnani.ai and WhatsApp have Protocol interfaces only. There are no provider
implementations or autonomous diagnosis/prescribing paths. Stage 1 has no patient
write API or clinical approval endpoint and does not claim a functioning urgent
response service yet. No push, merge, remote migration or deployment is authorized.

## Stage 1 security corrections

Use migration head `0002`, not just the original foundation revision. Consent now
protects entire patient rows. Runtime validation also rejects administrative role
attributes and membership in roles carrying those attributes. Startup errors are
redacted, and rejected origin/CSRF/session requests produce fixed-category audit
events without request values or untrusted actor identifiers. Audit-storage failure
returns 503 rather than silently dropping an event. No patient workflow was added.
