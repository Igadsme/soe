"""Small durable worker loop. Temporal/Celery can replace this boundary in deployment."""
import asyncio
import logging
from sqlalchemy import select
from .db import SessionLocal
from .models import WorkflowRun

logger = logging.getLogger(__name__)


async def process_once() -> int:
    processed = 0
    with SessionLocal() as db:
        runs = list(db.scalars(select(WorkflowRun).where(WorkflowRun.state == "queued").limit(10)).all())
        for run in runs:
            run.state = "rights_review"
            processed += 1
        db.commit()
    return processed


async def run_forever(interval_seconds: int = 10) -> None:
    while True:
        try:
            await process_once()
        except Exception:
            logger.exception("worker_iteration_failed")
        await asyncio.sleep(interval_seconds)


if __name__ == "__main__":
    asyncio.run(run_forever())
