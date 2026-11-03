# Backend application modules

- `main.py`: FastAPI app factory, security headers, health checks, middleware and frontend serving. It wires auth and patient services; voice bindings remain disabled.
- `core/`: strict runtime configuration, protected credential loading, request limits, session/CSRF and role authorization.
- `db/`: restricted runtime connection, transaction-local clinician context and database-role checks.
- `models/`: staff, patient and clinical PostgreSQL ORM models.
- `schemas/`: strict authentication, patient/consent, voice and AI draft contracts.
- `routes/`: active authentication and patient/consent APIs; voice routes fail closed without explicit workflow bindings.
- `services/`: Supabase Auth boundary, patient/consent operations, audit writing and disconnected voice service contracts.
- `integrations/`: disconnected Gnani STT/TTS and Gemini adapters. Ollama is an isolated offline prototype and is not used by the application.
- `provision.py`: explicit operator-only clinician provisioning command.

Patient APIs do not mean the browser demo is production registration: its forms and
patient state remain synthetic. No provider is configured or called at application
startup. See [`backend/README.md`](../README.md) for setup and phase constraints, and
[`docs/architecture.md`](../../docs/architecture.md) for the workflow status.
