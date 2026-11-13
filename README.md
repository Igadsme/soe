# soe

Autonomous AI content intelligence, clipping, publishing, and monetization platform.

## MVP capabilities

- **Content intelligence ingestion** via `POST /ingest`
- **Autonomous clipping** via `POST /clip`
- **Publishing workflow** via `POST /publish`
- **Monetization estimation** via `POST /monetize`

## Local development

```bash
python -m pip install -e .[dev]
pytest -q
uvicorn soe.app:app --reload
```

## API flow example

1. Ingest content
2. Generate clips from the ingested content
3. Publish clips to a destination
4. Estimate monetization revenue from impressions and CPM