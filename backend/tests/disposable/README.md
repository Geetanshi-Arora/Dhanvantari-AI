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

## Phase 3C API integration preparation (separate execution approval required)

`test_patient_api.py` adds the real FastAPI HTTP routes, service layer and migration
0004 functions to the same guarded `local_pg` fixture. Only the Supabase Auth
provider is fictional: login, encrypted cookie sessions, CSRF/origin checks, active
staff checks, clinical role switching, RLS, clinical functions and audit inserts
remain real. Each test uses a fresh cluster and explicitly injected restricted
runtime engine. Settings never load an operator env file or DPAPI credential.

Test-only notice metadata is seeded into that cluster and into the service's
trusted validation context. It is neither approved wording nor deployment data.
Tests cover all six endpoints, nurse/doctor/unassigned access, care decline and
revocation, optional decisions, retries, concurrency, history, fail-closed notices,
CSRF/origin/session rejection, body limits, audit failures and rollback. Failure
injection triggers exist solely in the disposable cluster. A response-validation
failure is injected after the real DB function returns to test rollback before
commit. Auth-provider networking is never used. All existing network, driver,
environment, exact-socket and cleanup guards remain unchanged and active.

Preparation/collection performs no fixture setup or SQL. Safe collection command:

```sh
cd /workspace/Dhanvantari-AI-phase2/backend
/workspace/Dhanvantari-AI/.venv/bin/python -B -m pytest \
  --confcutdir=tests/disposable -p no:cacheprovider \
  tests/disposable/test_patient_api.py --collect-only -q
```

After **separate approval only**, execute that same test file with
`--run-disposable-postgres` instead of `--collect-only`. Do not run the ordinary
parent conftest, supply connection overrides, enable `--showlocals`, or add a remote
fallback. TestClient uses in-process ASGI calls and does not start an HTTP server.
Each client/lifespan closes before fixture cleanup disposes engines, stops the
private cluster and removes its directory. The sandbox may block TestClient's
cross-thread event-loop wakeup; any execution permission workaround must keep the
existing Python/libpq network guards active. No workaround is part of preparation.

The PG16.2 harness does not verify PG17/Supabase pooler or real Auth-provider
compatibility. HTTPX/TestClient's installed deprecation warning is also unchanged.
The generator-based HTTP size test is complemented by the offline route suite's
explicit ASGI multi-message boundary test (TestClient may coalesce chunks).

## Phase 3E Part 1 — browser lifecycle preparation; execution BLOCKED

Real browser integration testing is deferred. Preserve these safeguards while
continuing other approved work; resumption requires a separate isolation review
and explicit execution approval.

Only `dhanvantari_browser_guard.py`, the browser opt-in gate and offline guard tests
are prepared. No browser scenarios, patient seeds, runner script or real network
confinement are implemented. **Do not run browser-to-backend tests yet.**

Both `--run-disposable-postgres` and `--run-disposable-browser` are required. Browser
cases are identified by `test_patient_browser.py` or the `browser_preflight` /
`browser_lifecycle` fixture dependencies. Collection marks them skipped before any
fixture setup; `local_pg` independently rechecks browser preflight before startup.
This prevents a PostgreSQL dependency starting before a later browser fixture
rejects execution. Ordinary PostgreSQL-only cases retain their existing opt-in,
connection guards and cleanup. `dhanvantari_pg_guard.py` is unchanged.

Flags are necessary but insufficient: `require_network_boundary()` always rejects
execution, and `require_reviewed_runner()` independently rejects an unreviewed
runner. Neither accepts an environment flag, boolean attestation or override.
Merely installing software or adding `tests/patient-backend.cjs` cannot unlock them.

Python's existing `no_network()` and exact libpq target checks remain essential,
but they do **not** confine a Node/Chromium child. Playwright route interception,
CSP, Chromium flags and URL policy predicates alone do not supply an enforceable
network boundary. No such boundary is implemented or claimed by this milestone.
A future separately reviewed implementation needs OS/platform-enforced confinement
(for example, a fresh shared server/browser network namespace with only loopback,
no external routes/interfaces and proven egress rejection). Do not relax the
PostgreSQL guards or treat the cloud HTTP proxy policy as that boundary.

Prepared policies/lifecycle contracts:

- Parent PG/database, Node preload/module, browser, Uvicorn and application-config
  overrides are rejected without displaying values. Child environment is restricted
  to fixed PATH/LANG and private HOME/TMPDIR; no operator credentials are inherited.
- The only command shape is the pinned absolute Node executable,
  `tests/patient-backend.cjs`, and the exact generated origin. No shell, extra args,
  arbitrary commands, SQL, administrator HTTP endpoints or control-channel input.
  Future runner source/content and confinement launcher still need review/pinning.
- Listener binds **only** `127.0.0.1:0`; its assigned port becomes the sole origin.
  Uvicorn uses one worker, no reload/access logging, asyncio/h11 and real lifespan.
- Browser policy denies external origins/navigation, every WebSocket, arbitrary
  endpoints and patient writes. Only same-origin reads and Auth login/logout are
  allowed. Context settings require blocked service workers/downloads and reject
  storage state, HAR, video, tracing and additional context options. The absent
  runner cannot enforce/export anything yet; future source review must also forbid
  artifact-export APIs, including `storage_state()` and tracing calls.
- No app factory, environment file, DPAPI loader or Supabase client is loaded by
  this guard. Future fixtures must explicitly inject disposable-only settings,
  restricted runtime engine and fictional Auth; keep approved notices empty.
- Startup has a 10-second deadline, runner completion a 60-second deadline, and
  shutdown waits are bounded to 5 seconds per attempt with escalation. Child stdio
  is discarded; error messages are fixed categories. No passwords/URLs/tokens or
  private exception contents are printed. No automatic startup/migration retry.
- Teardown attempts runner group termination, server shutdown and socket close even
  after partial failures. A cleanup failure retains private files and fails closed.
  Real process-tree behavior remains unverified and must be tested before unlocking
  execution. SIGKILL/host failure still cannot guarantee Python finalizers.

Safe offline verification, from the repository root (starts no services):

```sh
PYTHONPATH=backend /workspace/Dhanvantari-AI/.venv/bin/python -B -m pytest \
  --noconftest -p no:cacheprovider \
  backend/tests/test_patient_browser_guard.py \
  backend/tests/test_disposable_pg_guard.py \
  backend/tests/test_patient_api_harness_static.py -q
```

Future command shape, **not runnable/authorized at this stage**:

```sh
cd /workspace/Dhanvantari-AI-phase2/backend
PYTHONPATH=. /workspace/Dhanvantari-AI/.venv/bin/python -B -m pytest \
  --confcutdir=tests/disposable -p no:cacheprovider \
  tests/disposable/test_patient_browser.py \
  --run-disposable-postgres --run-disposable-browser -q --tb=short
```

That test file and runner do not exist yet, and both hard blockers remain in place.
Preparation does not authorize PostgreSQL, Uvicorn, Node or Chromium execution.
The existing no-custom-subprocess rule remains in force: the validated fixed
browser command is a proposed narrow exception only after separate review of its
source, confinement and execution approval. No arbitrary subprocess is permitted.

## Phase 3E Part 2C — non-executable isolated launcher proposal

`dhanvantari_browser_launcher.py` prepares pure command builders and readback
validators only. It imports no process, socket or syscall execution interface.
`execute_plan()` retains both existing hard blockers and an additional unconditional
rejection. A fabricated snapshot, plan or successful validator cannot enable it.
**There is no runnable namespace bootstrap, server entrypoint or browser runner.**

The earlier capability check established that an unprivileged fresh user/network
namespace can be created. It did not establish firewall, mount, seccomp or browser
compatibility. This milestone creates no namespace and runs no workload.

Proposed sequence and boundaries:

- A fixed absolute `unshare` command creates fresh user, network and PID namespaces.
  Verify empty interfaces/routes, install IPv4/IPv6 default-DROP policies, then
  initialize only `lo` with `127.0.0.1/8`. No veth, external route or DNS is configured.
- Bind a newly owned `127.0.0.1:0` listener. Its assigned port must match the exact
  approved origin. IPv4 rules permit TCP requests only to that port and established
  replies from it; other ports and all IPv6 traffic remain denied. Strict filter
  readback rejects additional rules, chains and non-DROP policies. These are proposed
  rules, not evidence that this kernel accepts or enforces them.
- Separate empty-root bubblewrap mount/PID/IPC capsules share only the fresh network
  namespace. Each has private `/proc`, `/tmp`, `/run` and home. Staged code, runtime,
  libraries and fonts must be reviewed copies, mounted read-only; host root, repository,
  credential directories and host IPC sockets are excluded. Bundle validation rejects
  symlinks, special files, multiply linked files and credential/environment paths.
- Only the server capsule receives the exact disposable PG socket file at its
  original generated path; the browser gets neither that socket nor a view of the
  server's `/proc`. The existing PG connection guards and target path stay unchanged.
  The server receives only its newly owned listener and sealed private configuration
  descriptors; the browser receives only devnull standard streams. All inherited
  network/proxy/management descriptors must be closed before capsule construction.
- Fixed executable/argument profiles and a four-variable child environment exclude
  inherited credentials, database URLs, proxies and module preloads. Workloads require
  zero effective/permitted/inheritable/ambient/bounding capabilities, NoNewPrivs,
  seccomp, restrictive umask and disabled core dumps. The native filter must prevent
  namespace reentry/creation (including `clone` flags and `clone3`) and process-memory
  access. Its actual BPF implementation and installation are **not supplied**.
- Supervision must retain the existing bounded startup/completion/shutdown deadlines,
  discard child output and kill/reap descendants on partial initialization failure.
  The proposed outer PID namespace additionally requires init-death cleanup. Artifact
  exports, storage-state files, tracing, HAR, videos and credential logs stay forbidden.

Before any execution can be reviewed, implement and verify: a pinned native
bootstrap/restriction filter; executable and bundle content manifests; descriptor
closure/readback and sealed-config protocol; actual mount and process isolation;
kernel firewall support/readback and exact-port denial; safe Chromium compatibility;
and real bounded descendant cleanup. Namespace route snapshots in these tests are
normalized policy inputs, not live OS observations. The server/runner entrypoints
and scenarios also remain absent. Do not route the old lifecycle around its blockers.
Flags, request interception, validator success or this proposal cannot authorize runs.

Offline verification only (no databases, servers, browsers or namespaces):

```sh
PYTHONPATH=backend /workspace/Dhanvantari-AI/.venv/bin/python -B -m pytest \
  --noconftest -p no:cacheprovider \
  backend/tests/test_browser_launcher.py \
  backend/tests/test_patient_browser_guard.py \
  backend/tests/test_disposable_pg_guard.py \
  backend/tests/test_patient_api_harness_static.py -q
```
