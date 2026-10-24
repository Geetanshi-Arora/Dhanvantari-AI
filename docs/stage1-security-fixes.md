# Stage 1 security fixes

The five approved findings are corrected locally. No Stage 2 workflows, clinical
write grants, external integrations, remote database changes or Git operations
were introduced.

- Consent: revision 0002 requires care consent to read any patient row, including
  history, allergies and medicines. Consent records remain separately readable by
  assigned staff. Registration metadata interfaces remain Stage 2 work.
- Secrets: Settings error rendering hides input; application construction, database
  startup, migration CLI and provisioning CLI suppress underlying exception chains
  that could disclose configuration values. Do not log raw Settings/error input.
- Roles: runtime startup rejects administrative attributes (including CREATEROLE,
  CREATEDB, REPLICATION) and memberships outside the runtime/authenticated allowlist.
- Audits: rejected origin/CSRF/session requests produce fixed-category security
  events without headers, cookie values, tokens, email, URL or untrusted actor IDs.
  Audit-storage failure denies access with 503.
- Decisions: database triggers require the active doctor assigned to the visit and
  patient, active care consent, matching actor when supplied, and same-visit existing
  assessment references. Assessment visit links cannot be moved. These checks apply
  to inserts and updates; privileged administrators remain a trusted boundary.

Changed files:

- backend/app/core/config.py
- backend/app/db/session.py
- backend/app/main.py
- backend/app/provision.py
- database/migrations/env.py
- database/migrations/versions/0002_security.py (new)
- database/migrations/versions/0002_security.sql (new)
- backend/tests/test_database.py
- backend/tests/test_security_fixes.py (new)
- backend/README.md
- database/migrations/README.md
- docs/stage1-security-fixes.md (this report)

Validation: 61 backend tests passed (43 original tests plus 18 regression cases),
49 original browser checks passed, Ruff lint/format passed, and all eight saved
frontend HTML/JS/CSS hashes matched. Two existing non-failing tooling warnings
remain (pgserver runtime directory and Starlette test-client deprecation).
Database tests use real temporary PostgreSQL with fictional data. Supabase Auth
is mocked; no live Supabase credentials or connections were used.

Revision 0001 is unchanged; 0002 is an additive forward migration retaining all
20 tables. It has been executed only in disposable local test databases. Applying
it remotely requires separate approval. Branch remains work at c396d23; no commit,
push, merge or deployment was performed. Stage 2 is stopped pending approval.
