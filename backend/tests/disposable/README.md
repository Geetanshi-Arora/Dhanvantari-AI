# Disposable PostgreSQL harness — execution approval pending

Preparation does not authorize starting this harness. No cluster has been started
by this implementation. No migration or SQL has been executed during preparation.

This Linux-only harness uses the already installed `pgserver` package's PostgreSQL
16.2 binaries. It deliberately uses its own start/stop wrapper rather than calling
`pgserver.get_server()`: the latter may create an OS account when run as root.
Here root requires an **already existing** unprivileged `pgserver` account; a normal
non-root user runs the child processes under their own identity. No OS user is created.

## Isolation

- Explicit `--run-disposable-postgres` opt-in; otherwise DB tests skip.
- Use `--confcutdir=tests/disposable` to exclude older, unguarded parent fixtures.
  Do not use a broad `pytest tests` command for this milestone.
- Refuse inherited PG variables and database URL/DSN overrides, even local ones.
- Create a fresh mode-0700 directory under `/tmp`, with private data/socket files.
- Disable TCP listening; reject host authentication and use SCRAM for local sockets.
- Generate ephemeral passwords only after execution approval, in process memory.
  Supply initdb's password through stdin; never write passwords into `.env` or argv.
- Exact socket, database, port, username and password checks before libpq connects;
  reject service files, multi-host strings, hostaddr and connection-option overrides.
- Block Python DNS/TCP; disable operator dotenv loading and guard Alembic engines.
- Start PostgreSQL children with a minimal environment, bounded tool timeouts and
  bounded server statement, lock and idle-transaction timeouts.
- Suppress query/parameter logging. Do not enable traceback locals, SQL echo, or
  debugger dumps; the fixture rejects `--showlocals`.

These guards protect the reviewed test paths; they are not an OS security boundary
against malicious Python code or a compromised dependency that deliberately bypasses
the guards. No custom subprocess, async database client or remote client is allowed.

## Prepared tests

Fresh install and upgrade 0003→0004; empty registry; RLS and role/definer ACLs;
fictional adult registration and nurse assignment; care decline and omitted optional
consents; invalid inputs and actor spoofing; repeated/concurrent idempotency;
concurrent consent changes; revocation after notice retirement; immutable history;
caller/audit-failure rollback; authorization before locks; pooled identity reset;
legacy creation-time ties and ambiguous-history migration rollback.

Successful-write tests would insert **test-only notice metadata** into their own
fresh cluster (`TEST-ONLY-NOT-A-LEGAL-NOTICE`), never approve legal wording or alter
the development registry. These fixture inserts require the separate DB-test
execution approval. Fresh-install tests verify the real migration leaves it empty.

## Future execution — only after separate approval

From `/workspace/Dhanvantari-AI-phase2/backend`, using the existing tooling venv:

```sh
/workspace/Dhanvantari-AI/.venv/bin/python -B -m pytest \
  --confcutdir=tests/disposable -p no:cacheprovider \
  tests/disposable --run-disposable-postgres -q
```

Each test gets a new cluster. `finally` and an atexit fallback dispose engines,
stop the exact private server, confirm it is stopped, and then remove its directory.
A shutdown failure retains the private directory and fails rather than deleting
files beneath a possibly running server. SIGKILL/host crashes cannot guarantee
Python cleanup; inspect residual processes/directories before rerunning. No
migration retry or remote fallback exists. A migration failure is reported with a
fixed category; debugging must preserve secret redaction.

PostgreSQL 17-specific behavior and actual Supabase pooler compatibility remain
outside this PostgreSQL 16 harness. Supabase Auth is not contacted: DB actor claims
are fictional and API JWT/CSRF ordering remains covered by separate auth tests.
