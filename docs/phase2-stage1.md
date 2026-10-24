# Phase 2 stage 1 — foundation, migrations and authentication

Completed locally on 24 October 2026. No push, merge, deployment or live Supabase
migration was performed. Work stops here before patient workflow implementation,
as requested.

## Implemented

- Modular FastAPI app factory with routes, services, SQLAlchemy models, Pydantic
  schemas, settings, database contexts and future integration Protocols.
- Frozen Alembic migration for 20 PostgreSQL tables, including all approved clinical
  entities and nurse-raised urgent escalation records. Every table has RLS enabled.
- Restricted runtime database role, separate NOLOGIN owner, transaction-local actor
  identity, patient assignments and consent-gated clinical read policies.
- Supabase password authentication boundary, remote identity verification, active
  staff role checks, role authorization dependency and operator-only provisioning.
- Opaque, revocable browser sessions; encrypted provider access tokens; hashed
  session/CSRF identifiers; secure cookie settings; exact-origin and CSRF checks.
- Persistent account/IP login throttling, bounded authentication requests, redacted
  validation/provider/database errors, security headers and append-only audit events.
- Environment example, reproducible test dependencies and VS Code/local setup guide.

The existing frontend HTML, JavaScript and CSS match their pre-change SHA-256
hashes. Its five pages, theme and fictional-data demo behavior are preserved.

## Verification results

| Check | Result |
| --- | --- |
| Backend/API/PostgreSQL tests | **43 passed** |
| Backend line coverage | **95%** (540/567 executable statements) |
| Original Playwright browser smoke suite | **49 checks passed** |
| Ruff lint and formatting | Passed |
| Installed dependency compatibility | Passed, 43 installed packages |
| Frontend hashes | All eight existing HTML/JS/CSS files unchanged |
| Git whitespace checks | Passed |

Tests used an actual temporary local PostgreSQL server and fictional data only.
The Supabase HTTP boundary was tested with HTTP mocks; no live project credentials
were available or used. Tests cover schema/migration execution and reapplication,
RLS isolation and identity reset, consent withdrawal, assignment revocation, inactive
staff, role elevation denial, unauthorized writes, prescription and urgent-state
constraints, provisioning, login/session lifecycle, encryption, rotation, CSRF,
logout during provider outages, throttling, body limits, fail-closed provider/database
errors, audit immutability and database connection release before network calls.

The browser suite covers five-page navigation, demo role/language selection,
registration validation, safe text rendering, search, reload persistence, logout,
mobile layout and browser errors.

Two non-failing tooling warnings remain: pgserver uses a temporary runtime directory
when XDG_RUNTIME_DIR is unset, and the installed Starlette test client reports an
upcoming HTTP-client migration. Neither is a test failure. Coverage includes the
schema definitions, so it does not establish clinical workflow correctness.

Commands run from the repository root:

```sh
.venv/bin/python -m pytest -c backend/pyproject.toml backend/tests -q --cov=app --cov-report=term-missing
.venv/bin/ruff check --config backend/pyproject.toml backend database/migrations
.venv/bin/ruff format --check --config backend/pyproject.toml backend database/migrations
node tests/smoke.cjs
```

The browser suite ran against a local static server; FastAPI's unchanged frontend
serving and health endpoints were also exercised through TestClient.

## Stage boundary and remaining work

Patient registration, consent capture, visit creation, symptoms/vitals entry,
patient-history APIs and frontend API wiring have **not** been implemented yet.
Clinical writes remain denied at the database layer. The demo login is still a
public fictional-data demonstration, separate from the new authentication APIs.

Urgent escalation has database storage and state constraints only. The next stage
must add nurse flagging and an assigned-doctor priority/acknowledgement workflow;
it must not claim a live emergency notification service before that exists.

Private upload endpoints/buckets, scanner/quarantine handling, clinical write
policies, doctor review transitions, versioned approvals and prescription APIs are
subsequent stages. AI assessments are separate unapproved source records; there is
no AI approval path or autonomous prescribing. Only future integration contracts
exist for Gemini, Gnani.ai and WhatsApp.

Live Supabase connectivity, managed-role migration permissions and real Auth login
need verification against a configured **development** project before use. See
`backend/README.md` for VS Code setup and `database/migrations/README.md` for database
roles and operator-run migrations. No remote changes have been made.
