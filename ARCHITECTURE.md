# Soe architecture

The system is organized around auditable domain records rather than an unconstrained agent loop. Deterministic services own authorization, scheduling, budgets, retries, and publication decisions. AI providers may propose transcripts, highlights, and editorial metadata, but every proposal is evidence-backed and reviewable.

The initial vertical slice uses FastAPI + SQLAlchemy async persistence and a Next.js dashboard. PostgreSQL and Redis are provided by Docker Compose. External integrations should be added behind provider interfaces, with secrets injected through environment or a cloud secret manager.

## Workflow state

`discovered -> rights_review -> authorized -> ingested -> analyzed -> ranked -> edited -> quality_checked -> approved -> scheduled -> published -> measured`.

Any rights failure or model/provider error must stop the workflow and create an auditable failure record. No public URL is sufficient evidence of commercial rights.
