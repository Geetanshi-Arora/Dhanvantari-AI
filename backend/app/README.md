# Application modules

- `main.py`: app factory, headers, health and unchanged frontend serving.
- `core/`: validated configuration, body limits, session/CSRF and role authorization.
- `db/`: restricted runtime connection and transaction-local clinician context.
- `models/`: foundation and clinical PostgreSQL models.
- `schemas/`: strict authentication input/output schemas.
- `routes/`: versioned authentication API; clinical APIs follow in the next stage.
- `services/`: Supabase Auth boundary and privacy-conscious audit writer.
- `integrations/`: future assessment, voice and notification Protocol contracts only.
- `provision.py`: explicit operator-only clinician provisioning command.

See `../README.md` for VS Code setup and stage boundaries.
