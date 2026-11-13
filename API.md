# API

- `GET /health` — service health.
- `GET /api/v1/overview` — persisted counts and explicitly verified financial totals.
- `POST /api/v1/sources` — register a YouTube/Twitch metadata source.
- `GET /api/v1/sources` — list registered sources.
- `POST /api/v1/sources/{id}/rights` — attach an authorization record; fails closed for non-commercial or non-publishing permissions.
- `GET /api/v1/clips` — list ranked clip candidates.
- `POST /api/v1/demo/pipeline` — create clearly labeled local fixture records; performs no fake media processing.
