"""Append audit events in the same transaction as the action."""
from .models import AuditEvent


def audit(db, actor: str, action: str, entity: str, identifier, **details) -> None:
    # Callers supply structured status, IDs and flags only.
    db.add(AuditEvent(actor=actor, action=action, entity=entity,
                      entity_id=str(identifier), details=details))
