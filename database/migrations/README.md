# PostgreSQL / Supabase migration setup

Revision `0001` creates 20 tables in the private `app` schema: staff profiles,
nurses, doctors, sessions, login throttles, patients, assignments, consent records,
visits, symptoms, vital signs, uploaded file metadata, reports, images, AI
assessments, clinician decisions, prescriptions and items, urgent escalations,
and audit events. See `backend/app/models/` for the ORM representation. Migration
DDL is frozen in the revision's `.sql` files; future changes need new revisions.

All tables have row-level security enabled. A NOLOGIN `app_owner` owns them.
The runtime role must not be a member of `app_owner` or have BYPASSRLS/superuser
privileges. Never use the Supabase `postgres` connection as `DATABASE_URL`.

## Apply in a separate development project

No live migration is applied by tests or application startup. Back up any existing
database before an operator applies a migration. Use the database owner's direct
or session-mode connection, not a transaction-pooler connection, for migrations.
The operator needs permission to create roles/schema, grant role membership and
transfer ownership. On Supabase, use the project database owner. The migration
historically reuses `authenticated` in frozen revision 0001; revision 0003 removes
that application dependency without changing Supabase Auth. It grants `app_owner` to the migration operator
for controlled administrative maintenance, never to the application runtime.

With the environment configured as in `backend/.env.example`, from repository root:

```sh
python -m alembic -c database/alembic.ini upgrade head
python -m alembic -c database/alembic.ini current
```

Alembic reads `MIGRATION_DATABASE_URL` from the environment or ignored backend `.env`.
Do not place connection URLs in `alembic.ini` or CLI arguments. An already-applied
upgrade is a no-op. Destructive downgrade is intentionally disabled: use a reviewed
forward migration or restore a backup rather than silently dropping medical records.

## Provision the runtime database login

After migration, an operator creates a dedicated login and grants its group role:

```sql
CREATE ROLE dhanvantari_runtime LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
GRANT dhanvantari_api TO dhanvantari_runtime;
```

Set its password privately using psql's `\password dhanvantari_runtime` prompt
(or an equivalent managed secret workflow), not a literal SQL statement committed
to source. Configure the corresponding restricted connection in `DATABASE_URL`.
Do not grant table ownership or membership in administrative roles. If your managed
pooler requires a project-qualified username, follow that project's connection
instructions. Use TLS verification for remote connections. Keep the `app` schema
out of Supabase's exposed Data API schemas; the browser uses FastAPI.

The trusted backend runtime can manage authentication sessions and throttles, and
append audit events. For clinician queries it uses `SET LOCAL ROLE dhanvantari_clinical_actor`
and a transaction-local verified UUID. RLS restricts access to the active staff
member and explicit, unrevoked patient assignments. Clinical read policies also
require active care consent. Consent records remain accessible to assigned staff for inspection; revision
0002 protects the entire patient row because it includes clinical information. Unassigned users see no records.
Identity settings reset when the transaction ends, including on errors.

Clinical writes and all upload metadata access are denied in this foundation stage.
Do not broaden grants manually to make a future endpoint work. Add write policies,
service checks and authorization tests together in the next migration. Upload
storage buckets are not created in this stage; later work must use private buckets
and quarantine, never public file URLs.

Audit entries cannot be updated/deleted by the runtime; a trigger also rejects
ordinary UPDATE/DELETE by an operator. A database administrator can still bypass
such protections, so external audit export/backups are needed for operational
tamper evidence. This stage does not claim cryptographic audit immutability.

## Stage 1 security correction (revision 0002)

Revision `0001` remains frozen. Upgrade to `0002` using the same operator-run
Alembic command; no live migration has been applied automatically. The table count
remains 20. Patient rows now require current care consent, because those rows include
medical history, allergies and medicines. Assigned staff can still inspect consent
records, but registration-metadata access must be designed separately in Stage 2.
Do not relax the row policy to expose those clinical fields.

Decision insert/update triggers require an active doctor assigned to both the visit
and patient, current care consent, a matching application actor when present, and
an assessment from the same visit when referenced. Assessment visit links are
immutable. Privileged administrative operations remain a trusted boundary. These
constraints do not grant clinical writes or implement approval workflows.

## Runtime isolation (revision 0003; local implementation, live approval required)

Role hierarchy: `dhanvantari_runtime` LOGIN -> `dhanvantari_api` NOLOGIN ->
`dhanvantari_clinical_actor` NOLOGIN. Arrows mean membership/inherited privileges.
The runtime has no ADMIN OPTION, administrative attributes, object ownership,
schema CREATE, `authenticated` membership or `app_owner` membership. PUBLIC rights
still apply and must be inspected in the development project before provisioning.
The migration creates no runtime login or password. The SQL above is an operator
reference, not authorization to provision a live login.

0003 grants the clinical actor exactly the existing 14 clinician-readable tables
and five RLS helper functions. It changes only the role targets of 14 SELECT
policies, preserving their expressions. It removes the old authenticated grants
only on application objects and removes that membership from dhanvantari_api.
Session/throttle/audit policies and permissions, consent checks, doctor/assessment
triggers, urgent-escalation constraints and upload quarantine remain unchanged.
Neither managed schemas nor shared default privileges are changed. Supabase Auth
continues through the existing HTTP API with verified UUIDs supplied by FastAPI.
The database trusts FastAPI's identity assertion; the runtime credential can set
claims and must never be available to a browser or untrusted operator.

The migration runner uses one outer PostgreSQL transaction, a transaction-local
5-second lock timeout and an explicit public ledger schema. After migrations and
revision bookkeeping, but before COMMIT, it revokes ledger permissions from PUBLIC
and existing known application roles with RESTRICT. It verifies owner ACL retention,
administrator access and absence of application table/column access or owner-role
membership. Unexpected owners, recipients or column grants abort the transaction.
Current owner must be the effective migration administrator; an unexpected owner
requires review, not automatic ownership transfer. Only ledger ACLs are changed;
creator defaults are untouched. This guard also runs for no-op upgrades and stamp
operations. Offline execution is disabled because it cannot verify these conditions.
Use the reviewed runner; external tools or manual ledger recreation bypass it.

Safe sequence: review code and disposable-PostgreSQL tests; separately approve and
run a pinned-project, verify-full preflight and upgrade to 0003 once; inspect the
result read-only; then obtain separate approval to provision the runtime login.
Do not run the new backend against revision 0002. Failures roll back roles, grants,
policy targets, ledger ACLs and version changes together. A connection loss around
COMMIT requires read-only outcome verification, never an automatic retry.
Downgrades remain disabled. Keep the server stopped if review fails; use a reviewed
forward correction. Restoring authenticated membership and old policy targets would
require an explicitly approved inverse migration coordinated with the old backend.
Never restore broad ledger grants as an automatic rollback step.

Local tests establish real PostgreSQL role switching and pooled-connection cleanup,
including SQL and application exceptions. They do not establish Supabase pooler
compatibility. After separate approval, verify that the project's Session pooler
accepts the custom login (including its required username format), uses verify-full,
passes startup validation and supports transaction-local role/identity changes.
Inspect managed-object/PUBLIC effective permissions and test real Auth with fictional
accounts separately. No live connectivity, login provisioning or migration is part
of this local milestone.
