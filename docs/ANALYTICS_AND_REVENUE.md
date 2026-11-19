# Analytics and revenue

Soe never shows a number it cannot trace back to its source. This page explains where each number comes from and what is needed to turn on the parts that are currently unavailable.

## Metric provenance

| provenance | meaning | written by |
|---|---|---|
| verified | returned by an official platform API for a published post | analytics job |
| imported | from an operator-uploaded file | (reserved) |
| manual | typed in by the operator | (reserved) |

Each `MetricEvent` also records its `source` (for example `youtube_api`) and its `window`.

### Collection windows

Each succeeded publication is collected once per window: `initial` (immediately), `1h`, `24h`, `72h`, `7d` and `28d` after publish. The scheduler only enqueues the latest window that is due. `analytics_collections` has a unique (publication, window) row, so retries and replays never double-count. If a provider cannot be queried, the attempt is stored as `unavailable` with a reason (missing scope, private post, expired token, provider refusal). No zero is written. Transient errors and rate limits retry through the job system.

### Adapters (official APIs only)

| platform | endpoint | scope | notes |
|---|---|---|---|
| YouTube | Data API v3 `videos.list?part=statistics` | youtube.readonly (already required) | views, likes, comments |
| YouTube | Analytics API v2 `reports` (ids=channel==MINE, dimensions=video, filters=video==ID) | `yt-analytics.readonly` (optional) | views, watch time, average view duration and percentage, shares, subscribers gained and lost |
| TikTok | `POST /v2/video/query/` | `video.list` (optional) | needs the public post id. `SELF_ONLY` posts have none, so they are recorded as unavailable |
| Instagram | `GET /{ig-media-id}/insights` | `instagram_manage_insights` (optional, needs Meta app review) | tries views, reach, likes, comments, shares, saved and total_interactions, then falls back to the core set on error 100 (deprecated or unsupported metric) |

Optional scopes are requested during OAuth but never required. Without them the publishing connection still works, and the analytics attempt is recorded as unavailable.

All adapters are covered by contract tests (`backend/tests/test_analytics_contract.py`) against `httpx.MockTransport`. These are **not** live API passes. Live verification is BLOCKED until real OAuth credentials, approved scopes and a published post exist.

## Ledger provenance

| provenance | allowed kinds | how it is created |
|---|---|---|
| verified_platform | revenue | official monetary API (none connected; see below) |
| imported | revenue, expense | `POST /api/v1/revenue/import`; each row is linked to the file's SHA-256, and duplicate files are rejected |
| manual | revenue, expense | `POST /api/v1/revenue/entries` with a required reference (invoice or statement id) |
| estimated | **expense only** | processing cost estimates written by the worker, or operator estimates |

Summary rules (`GET /api/v1/revenue/summary`):

- Revenue is shown separately as verified_platform, imported and manual. The total is their sum.
- Costs are shown separately as actual and estimated.
- Net is revenue total minus actual and estimated costs. It is always labeled.
- Estimated values are never revenue. The API rejects them, and any legacy row is reported as `excluded_revenue_cents` and left out of the totals.
- Platform earnings with no data are `{"status": "unavailable", "cents": null, "reason": ...}`, never 0.
- Non-USD entries are listed but never converted or totalled.

### Platform earnings availability

- **YouTube**: needs `yt-analytics-monetary.readonly`, a channel in the YouTube Partner Program, and an owner decision to request that scope. Not implemented. Shown as unavailable.
- **TikTok and Instagram**: there is no creator earnings API for this use. Import statements as CSV.

## Feedback loop

`ranking.py` keeps the transparent heuristic score. Once at least `SOE_LEARNER_MIN_SAMPLES` publications (default 30) have verified **7-day** views, it fits ridge-regression weights.

- **Features:** heuristic score, duration, and posting hour (sin/cos, UTC).
- **Target:** standardized log views, averaged with average view percentage when every sample has it.
- **Blending:** the learned predictor is blended into the score with weight `n / (n + 3·N)`, capped at 0.5.

`GET /api/v1/analytics/recommendations` returns `insufficient_data` below the threshold. The weights and suggestions are correlational. They never predict virality. The unit tests use synthetic observations that are clearly labeled as synthetic.
