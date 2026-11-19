"""State transitions shared by the publication API and durable worker."""

from sqlalchemy import select, update

from .audit import audit
from .models import Job, Publication, PublicationEvent, RightsRecord, Source


def transition(db, publication: Publication, status: str, actor: str, *, initial: bool = False) -> None:
    if initial or publication.status != status:
        db.add(PublicationEvent(publication_id=publication.id, from_status=None if initial else publication.status,
                                to_status=status, actor=actor, error_class=publication.error_class))
        if status == "running":
            audit(db, actor, "publish.attempt", "publication", publication.id, attempt=publication.attempts)
        publication.status = status


def latest_rights(db, source_id: int):
    return db.scalar(select(RightsRecord).where(RightsRecord.source_id == source_id)
                     .order_by(RightsRecord.created_at.desc(), RightsRecord.id.desc()).limit(1))


def publication_job(db, publication_id: int):
    return db.scalar(select(Job).where(Job.publication_id == publication_id, Job.kind == "publish")
                     .order_by(Job.created_at.desc(), Job.id.desc()).limit(1).with_for_update())


def recover_expired(db, job: Job, exhausted: bool) -> None:
    publication = db.get(Publication, job.publication_id)
    if publication is None:
        return
    publication.attempts = job.attempts
    publication.error_class = job.error_class
    publication.last_error = job.last_error
    publication.next_attempt_at = None if exhausted else job.run_after
    transition(db, publication, "failed" if exhausted else "queued", "worker")


def lock_source(db, source_id: int) -> None:
    # Serialize publication creation with source jobs that replace rendered clips.
    db.execute(update(Source).where(Source.id == source_id).values(status=Source.status))
