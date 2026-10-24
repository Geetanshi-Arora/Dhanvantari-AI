# Dhanvantari AI architecture and phase boundary

## Phase 1: implemented

A static, dependency-free multipage frontend using HTML5, CSS3 Grid/Flexbox, and vanilla JavaScript ES modules. `frontend/assets/css/styles.css` provides shared design tokens and responsive components. `app.js` wires page interactions; `store.js` owns synthetic samples and tab-scoped session storage. The site makes no external API requests and uses no third-party fonts or assets.

Navigation: landing → role selection → nurse dashboard → language preference → registration → dashboard. Users can also navigate directly between demo pages. This is intentionally a public demo, with no authorization claim. Doctor role selection displays its future-phase roadmap.

Language options are illustrative UI preferences. No option claims verified Gnani.ai support; no translation, speech recording, playback, or patient-language conversation is implemented. English interface copy is staff-facing. No clinical analysis, consultation, prescription, referral, or doctor authorization occurs.

Synthetic registrations remain in sessionStorage for the current tab. Exit demo clears its state; normal browser tab closure ends its lifetime (browser session restoration may restore it). This mechanism is neither encrypted medical storage nor offline-first clinical support. Text entered by users is rendered through textContent, not HTML. The app uses native input validation and never requests credentials.

## Planned architecture: not implemented

- Phase 2: FastAPI, Pydantic, Uvicorn, Supabase Auth/PostgreSQL, registration/session APIs and role-based access. Supabase RLS must restrict access to assigned cases.
- Phase 3: verified Gnani.ai STT/TTS behind modular backend voice adapters, a labeled development mock if provider details are unavailable. Enable only provider-verified languages.
- Phase 4: server-side Gemini, validated structured JSON, selected-language questions, original transcript retained separately from English clinical summaries.
- Phase 5: evidence requests and consented uploads to private Supabase Storage. Requests for available evidence must not become autonomous test orders.
- Phase 6: dedicated continuous safety engine, initially with labeled simulated rules and synthetic scenarios. Clinical rules need clinician design and validation; uncertain cases escalate. Emergency referral must not wait for WhatsApp.
- Phase 7: English doctor reports, authorized case review, clinician decisions, WhatsApp Business notifications with non-sensitive secure case links.
- Phase 8: translated clinician-approved instructions, nurse verification, Gnani.ai speech in the selected patient language.
- Phase 9: longitudinal records, doctor dashboard, aggregate Chart.js analytics without patient identifiers.
- Phase 10: comprehensive functional/security/integration tests and Vercel frontend plus compatible Python backend deployment.

Future database entities: users, patients, consultations, transcripts, symptoms, observations, medical_files, ai_assessments, triage_results, doctor_notifications, doctor_reviews, care_plans, referrals, follow_ups, audit_logs. Relationships, indexes, timestamps and authorization policies belong in migrations. No empty executable APIs or pretend security policies are created in Phase 1.

All privileged credentials remain on the backend. Gnani.ai will not be substituted with a different provider. AI must not diagnose autonomously, prescribe, authorize treatment, or impersonate a doctor. The system is not clinically validated.
