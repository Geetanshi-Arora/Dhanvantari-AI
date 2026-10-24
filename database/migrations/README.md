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
reuses the Supabase `authenticated` role; standalone PostgreSQL creates a NOLOGIN
version of that role when needed. It grants `app_owner` to the migration operator
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
CREATE ROLE dhanvantari_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
GRANT dhanvantari_api TO dhanvantari_runtime;
ALTER ROLE dhanvantari_runtime INHERIT;
```

Set its password privately using psql's `\password dhanvantari_runtime` prompt
(or an equivalent managed secret workflow), not a literal SQL statement committed
to source. Configure the corresponding restricted connection in `DATABASE_URL`.
Do not grant table ownership or membership in administrative roles. If your managed
pooler requires a project-qualified username, follow that project's connection
instructions. Use TLS verification for remote connections. Keep the `app` schema
out of Supabase's exposed Data API schemas; the browser uses FastAPI.

The trusted backend runtime can manage authentication sessions and throttles, and
append audit events. For clinician queries it uses `SET LOCAL ROLE authenticated`
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
