# API

- `GET /health` — service health.
- `GET /api/v1/overview` — persisted counts and explicitly verified financial totals.
- `POST /api/v1/sources` — register a YouTube/Twitch metadata source.
- `GET /api/v1/sources` — list registered sources.
- `POST /api/v1/sources/{id}/rights` — attach an authorization record; fails closed for non-commercial or non-publishing permissions.
- `GET /api/v1/clips` — list ranked clip candidates.
- `POST /api/v1/demo/pipeline` — create clearly labeled local fixture records; performs no fake media processing.
- `POST /api/v1/discovery/search?platform=youtube|twitch&query=...` — use an official adapter when credentials exist, otherwise an explicitly labeled fixture in development mode.
- `GET /api/v1/publications` — list publication queue/history records.
- `POST /api/v1/clips/{id}/publish?platform=...` — rights-gated queue operation; disabled unless `SOE_PUBLISHING_ENABLED=true`.
- `GET /api/v1/analytics` — separate verified and unverified metric observations.
