from uuid import uuid4

from sqlalchemy.orm import Session

from app.models.foundation import AuditLog


def audit(engine, action, outcome, request_id, actor_id=None, resource_id=None):
    # Do not include email, password, cookies, provider tokens or clinical text.
    with Session(engine) as db, db.begin():
        db.add(
            AuditLog(
                id=uuid4(),
                actor_id=actor_id,
                action=action,
                outcome=outcome,
                request_id=request_id,
                resource_id=resource_id,
            )
        )
