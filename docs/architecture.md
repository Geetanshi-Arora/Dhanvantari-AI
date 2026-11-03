# Dhanvantari AI: current architecture and workflow

## Current state

The repository is a staged prototype, not a complete clinical application. FastAPI
serves the existing vanilla JavaScript frontend and provides staff authentication,
patient registration/consent APIs, and disabled voice endpoints. Supabase Auth and
PostgreSQL are the configured technologies. The normal runtime uses a dedicated,
restricted database credential and server-side sessions; secrets stay out of the
browser. The local runtime still needs its private configuration and the reviewed
database setup described in `../backend/README.md`.

| Workflow area | Current implementation |
| --- | --- |
| Staff access | Supabase Auth verification, nurse/doctor role checks, protected cookie sessions, CSRF and security audit events. |
| Patient registration | Nurse form submits to FastAPI; the patient, first case, consent decisions and audits commit in one database transaction. The workflow is synthetic-adult-only. Draft consent versions have no text and cannot be used for real patients. |
| Patient language | Preferred language is stored with the patient and copied to each case. Provider support is not inferred from that list. |
| Case management | A patient receives a UUID case on initial registration. Nurses can open multiple cases for one patient, list/search assigned cases, inspect status history and update limited basic patient fields. |
| Case lifecycle | Statuses are constrained in PostgreSQL. Nurse transitions are validated and audited; the current UI does not expose status editing. Doctor-stage statuses have no enabled transitions yet. |
| Voice intake | Gnani STT/TTS adapters, voice schemas, service contracts and disabled API routes exist with focused tests. The app wires no provider, consent adapter or workflow store; live recording and speech calls are unavailable. |
| AI structuring | Gemini adapter and structured draft schemas exist with offline tests. Gemini is not wired to the application or a patient workflow. |
| Nurse observations and safety | Database schema includes relevant clinical entities and urgent escalation storage, but no observations or safety-screen workflow is implemented. No clinical rules are configured. |
| Doctor review and approval | Doctor authentication and a placeholder page exist. There are no case queues, review actions, approval transitions or instruction authorization workflow. |
| Instruction delivery and follow-up | No approved instruction delivery, Gnani TTS workflow, follow-up or case closure workflow is implemented. |

All patient records in the browser are synthetic. Do not enter real patient data in
the demo. The doctor page has no clinical actions. AI adapters produce unverified
drafts and are not diagnostic or prescribing systems. A clinician must remain
responsible for decisions; no AI may approve treatment or independently diagnose
or prescribe.

## Target application components

Build future stages around one case lifecycle with explicit, audited transitions:

1. Patient registration, identity-safe case management, informed consent and language preference.
2. Nurse-assisted voice intake, Gnani transcription and nurse transcript confirmation.
3. Gemini symptom structuring and clarification suggestions, preserved as unverified drafts.
4. Nurse observations and clinician-approved safety screening, with urgent escalation that does not wait for messaging.
5. A doctor case queue with transcript, structured information, observations and a written/audio briefing.
6. Doctor requests for more information, treatment approval or referral. Only a doctor decision authorizes a plan.
7. Gnani speech delivery only for doctor-approved instructions, with nurse-assisted explanation.
8. Follow-up capture and explicit case closure.

FastAPI remains the API boundary; PostgreSQL remains the configured data store;
Supabase Auth remains staff identity; Gnani remains the STT/TTS provider; Gemini
remains the structuring provider. Keep provider adapters separate from workflow
services so a disconnected adapter cannot be mistaken for an enabled feature.
Consent, role, case assignment, audit and doctor-approval checks belong on the
server and in database policies where appropriate.

## Existing repository areas

- `frontend/`: authenticated nurse/doctor shell, synthetic registration and language demos, shared styling and disconnected voice demo/client.
- `backend/app/routes/`: auth, patient/consent/case APIs, and voice API routes disabled by default.
- `backend/app/services/`: authentication, patient/consent/case operations, audit and bounded voice service contracts.
- `backend/app/integrations/`: disconnected Gnani and Gemini adapters; Ollama is an isolated offline prototype, not an application provider.
- `backend/app/models/` and `database/migrations/`: PostgreSQL schema foundation and staged migrations. Migrations are not applied at startup.
- `backend/tests/` and `tests/`: Python backend tests and offline browser tests using fictional fixtures.

## Postponed or out of scope for this phase

Do not add analytics, Chart.js dashboards, WhatsApp, file uploads, extra AI agents,
alternative model providers, or autonomous clinical decisions to the workflow
foundation. The Ollama adapter and its tests are retained as an isolated experiment;
they are not part of the Gemini-centered target architecture. External notifications
and uploads require separate privacy and authorization designs. Clinical warning
signs require clinician authorship and validation before use.

## Phase boundary

The case management phase connects registration and consent to the database and
provides a first case lifecycle foundation. It does not build the clinical workflow.
The next phase can add voice intake and nurse transcript review, then AI structuring
as unverified drafts, observations, doctor review/approval and approved instruction
delivery. See `../README.md` and `../backend/README.md` for setup and current security
constraints.
