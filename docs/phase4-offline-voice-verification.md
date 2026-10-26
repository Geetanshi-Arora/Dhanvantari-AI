# Phase 4 Part 8A — offline voice verification

Reviewed baseline: `ba2eb682ea0ec07bd3b850434de0ccec8cf1a6bc`, branch
`phase-2-backend-foundation`. All application files remain unchanged.

## Scope and evidence

This is **not a browser-to-server end-to-end test**. No browser, listening server,
database, Supabase, actual provider transport or patient recording was used.

- `tests/fixtures/voice-wire-contract.json` is an explicitly fictional shared wire
  fixture. Its small PCM WAV samples are generated silence, not patient audio.
- Python validates the fixture against the actual request/response schemas and
  compares actual in-process route responses against it. A real VoiceService and
  real Gnani STT/TTS adapters use httpx.MockTransport; cookie/Auth/profile access,
  authorization decisions, audit and ephemeral handle storage are fakes.
- JavaScript independently exercises the actual frontend adapter/Auth helpers
  against that same fixture, checking every route and submitted JSON body. HTTP
  responses are in-memory doubles. No frontend mock-UI transcript is used.
- The STT language identifier and registry in the Python test are **fictional test
  policy**, not verified Gnani support or real approval evidence. The Hindi TTS
  example is test configuration only. Production configuration remains disabled.

## Results

| Check | Result |
| --- | --- |
| Backend offline suite | 1,596 passed; 87 database/CLI cases deselected; no selected failures or skips |
| Frontend adapter suite | 69 passed |
| Frontend mock UI and actual-module regressions | 48 passed |
| New shared frontend wire suite | 3 passed |
| New backend shared/adapter integration cases | 3 passed, included in the backend total |
| New Python test lint | Passed |
| Backend formatting | Passed, 74 files |
| Python syntax | Passed, 71 backend Python files |
| Frontend/test JavaScript syntax | Passed |
| HTML nesting, accessibility references; CSS structure | Passed |
| Git whitespace | Passed |

The offline backend runner collected only root `backend/tests/test_*.py` files;
disposable database suites were not collected. It excluded DB-dependent fixtures
and the CLI migration failure test before setup. It supplied fictional settings without loading .env,
blocked real PostgreSQL/SQLAlchemy connections and HTTP transport, blocked server
subprocesses and permitted only read-only Git subprocesses. Ordinary pytest with
repository conftest is **not** equivalent: it can create a database and is outside
this approval. Auth/JWT-user lookup, RLS and actual database behavior are not
validated against a real environment by this report.

Warnings: existing TestClient deprecation, unset XDG runtime directory; Node VM
modules are experimental. No dependencies were changed to suppress warnings.

### Existing lint maintenance issue

Full backend lint is **not clean**. These seven I001 import-order errors were
independently reproduced from the unchanged baseline content at HEAD:

- `backend/tests/test_consent_catalogue.py`
- `backend/tests/test_consent_notices.py`
- `backend/tests/test_consent_releases.py`
- `backend/tests/test_gnani_stt.py`
- `backend/tests/test_gnani_tts.py`
- `backend/tests/test_voice_capabilities.py`
- `backend/tests/test_voice_schemas.py`

No new lint error was found. These existing files were not edited.

## Contract and security findings

- STT sends the exact six JSON fields: language, media_type, byte_count,
  duration_ms, filename, audio_base64. Patient/visit UUIDs are path-bound; the
  request/transcript UUID, provider and timestamps are server-controlled.
- Review sends language, expected_revision, action and correction text only when
  applicable. Backend confirmation serializes corrected_text as null; the client
  normalizes that optional internal action field without changing the wire input.
  Responses contain draft, reviews and state, with original text and language intact.
- Synthesis sends language, media_type and server-reviewed text. Its response is
  binary audio with attachment, request UUID and playback-authorization=false headers.
  No field or HTTP shape mismatch was found for this verified workflow. Client-side
  validation is defense in depth, not an authorization boundary.
- App imports/constructs only the unconditionally disabled frontend client. The
  offline exchange factory is a trusted test seam, never a production transport.
  Mock UI is independent; copied, foreign, expired and stale review receipts fail.
- Backend startup has no voice service/provider/workflow binding. Its gate is false;
  service and provider gates/capabilities are disabled by default. STT and TTS
  mappings are independent and never fall back to another language or voice.
- Existing nurse dependencies verify cookie session, Origin/CSRF and Supabase user
  identity before applying clinical identity. Voice authorization additionally
  requires explicit active nurse, patient assignment, care, recording and external
  processing decisions; uncertain decisions fail closed. These voice decisions
  are currently injectable contracts, **not implemented DB authorization**.
- Existing consent purposes are only care, uploads and future_ai. None implies
  voice consent. Fake revocation during real-adapter mock processing rejects the
  response; original provider drafts remain unverified until correction AND
  separate confirmation. TTS requires exact reviewed-text authorization and grants
  neither clinical approval nor playback permission.
- Rechecks cover assignment/care/voice/context revision changes, actor/session,
  patient/visit and transcript revisions, including the HTTP acceptance boundary.
  An injected authorizer must issue non-reused change epochs to prevent ABA races.
- Patient switch, close, access errors, logout and pagehide clear frontend state;
  late responses cannot restore it. No capture, speech/playback APIs, transcript
  browser storage or clinical/provider-body logging was added. Audit tests verify
  fixed metadata only. Registration/catalogue lockouts remain unchanged.

## Blockers

### Critical before live use

1. Human-approved recording/external-processing consent, notices, translations and
   revocation/privacy policy: no voice-specific DB consent model currently exists.
2. Real authorization binding: verified active nurse, exact patient-to-visit and
   assignment checks, active care and voice permissions, fresh session checks and
   trustworthy change epochs before/after provider calls. Existing patient RLS
   helpers alone do not establish all these voice rights.
3. Real audit binding and server-side evidence for approval of the exact TTS text.
   Neither client confirmation nor a caller-supplied consent flag is sufficient.
   Provider calls must remain outside DB transactions; acceptance must fail closed
   if required auditing/authorization fails.
4. Verified STT language identifiers and independently reviewed TTS profiles; secure
   backend credential provisioning. Only Hindi TTS eligibility metadata exists;
   no production capability is enabled and no STT languages are verified.
5. A real active-visit binding and a separately reviewed live frontend workflow.
   Never reuse mock state or the offline exchange factory as a live transport.
   Actual recording/encoding and explicit playback authorization do not exist.
6. Provider privacy/retention approval: a post-call revocation check can discard a
   result but cannot recall audio already sent while consent was valid.

### Required before production deployment

- Bounded, expiring workflow storage with atomic revision compare-and-swap,
  revocation/session invalidation and cleanup. Current weak server handles and
  injectable callbacks have no production implementation or cross-worker guarantee.
  Define deliberate ephemeral semantics or an approved durable-history design;
  do not infer a retention period. No clinical records are written by this workflow.
- Authorized disposable/live-environment verification of runtime grants, RLS,
  actual Auth/session behavior and audit writes; none was run here.
- Define handling for same-nurse session rotation: /auth/me returns identity, not a
  session fingerprint. Backend session-bound handles remain authoritative.
- Browser accessibility/layout, browser-generated Origin/cookie behavior, real
  cancellation and memory-lifetime checks in a reviewed isolated environment.
- Keep offline factories/test policy out of live activation wiring; review privacy
  controls for operational logging and observability before introducing live data.

### Optional improvements

- Align/deprecate the legacy generic VoiceProvider protocol: the working service
  and adapters currently use richer structured STT/TTS protocols instead.
- Consider a separately designed authenticated readiness contract. Current 403
  deliberately combines assignment/consent failure; 503 combines disabled
  capability/provider/workflow unavailability. The client uses safe fixed messages.
- Resolve the seven baseline lint issues and retain shared-fixture drift checks.

## Deferred browser verification

`tests/smoke.cjs`, `tests/patients.cjs`, `tests/registration.cjs` and
`tests/patient-details.cjs` require Chromium and were syntax-checked only. Existing
browser guard/launcher execution blockers remain intact: enforceable browser
network/IPC confinement is not complete. No Playwright test was launched, browser
installed, namespace created or service started.

## Exact implementation files inspected

Frontend:

- `frontend/dashboard.html`, `frontend/register.html`
- `frontend/assets/css/styles.css`
- `frontend/assets/js/app.js`, `frontend/assets/js/auth.js`
- `frontend/assets/js/store.js`, `frontend/assets/js/patients-api.js`
- `frontend/assets/js/voice-demo.js`, `frontend/assets/js/voice-api.js`

Backend/security:

- `backend/app/main.py`, `backend/app/core/http.py`, `backend/app/core/security.py`
- `backend/app/routes/auth.py`, `backend/app/routes/voice.py`
- `backend/app/schemas/patients.py`, `backend/app/schemas/voice.py`
- `backend/app/services/patients.py`, `backend/app/services/voice.py`
- `backend/app/services/audit.py`, `backend/app/services/supabase_auth.py`
- `backend/app/db/session.py`, `backend/app/models/clinical.py`
- `backend/app/integrations/interfaces.py`, `backend/app/integrations/gnani.py`
- `backend/app/integrations/voice_capabilities.py`
- `database/migrations/versions/0001_security.sql`
- `database/migrations/versions/0002_security.sql`
- `database/migrations/versions/0003_runtime_isolation.sql`
- `database/migrations/versions/0004_patient_registration.sql`

Test infrastructure/coverage inspected includes `backend/tests/conftest.py`,
`test_patient_routes.py`, `test_voice_routes.py`, `test_voice_service.py`,
`test_voice_schemas.py`, `test_gnani_stt.py`, `test_gnani_tts.py`,
`test_voice_capabilities.py`, and the disposable browser guard/launcher policies;
frontend `tests/voice-api.test.cjs` and `tests/voice-demo.test.cjs`. The new Python,
Node and JSON shared-fixture files were reviewed and executed as described above.

The three new backend tests can be run without conftest DB fixtures:

```powershell
$env:PYTHONPATH = "backend"
python -B -m pytest --noconftest -p no:cacheprovider backend/tests/test_voice_wire_contract.py -q
node --experimental-vm-modules tests/voice-wire-contract.test.cjs
```

These commands use TestClient/in-memory mocks, not an application server. Full
backend verification requires the explicitly filtered/guarded offline runner,
not unrestricted test discovery. This report authorizes no activation or execution.
