"""Small durable worker loop. Temporal/Celery can replace this boundary in deployment."""
import asyncio
import logging
from pathlib import Path

from sqlalchemy import select

from .config import settings
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
    settings.validate_production()
    while True:
        try:
            await process_once()
            Path("/tmp/soe-worker-heartbeat").touch()  # noqa: S108 -- container-local healthcheck contract.
        except Exception:
            logger.error("worker_iteration_failed")
        await asyncio.sleep(interval_seconds)


if __name__ == "__main__":
    asyncio.run(run_forever())
