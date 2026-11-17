# Testing

Run `PYTHONPATH=backend pytest -q`. The default suite covers health, fixture discovery persistence, deterministic ranking, fail-closed rights behavior, and pipeline candidate evidence. Run `SOE_RUN_MEDIA_E2E=1 PYTHONPATH=backend pytest -q backend/tests/test_media_e2e.py` for the real local-media test; it generates original media, uses Faster-Whisper, renders captioned MP4s, and validates 1080x1920 output. Live provider and publishing credentials are not fabricated.

The media fixture generator needs a local text-to-speech engine: macOS `say`, or on Linux `espeak-ng` (`sudo apt-get install espeak-ng`). The first run downloads the Faster-Whisper `tiny.en` model. Verified on Debian 13 with espeak-ng: `SOE_RUN_MEDIA_E2E=1 PYTHONPATH=backend pytest -q backend/tests/test_media_e2e.py` → 1 passed (3 source videos, 6 rendered clips).

## Frontend

Use Node.js 22.12+ (verified with Node.js 24.22.1). Run:

```sh
cd frontend
npm install
npm run typecheck
npm test
npm run build
npm audit --omit=dev
npm audit
```

The Vitest/jsdom suite uses mocked fetch and fake timers. It covers rights evidence and platform validation, exact rights payloads and UTC expiry conversion, server 422 details, CSRF headers and credentials, and polling success, failure, and timeout (including unresponsive requests). Polling checks every two seconds and stops after five minutes. Authorization values match backend `RightsCreate`: `owned`, `licensed`, and `explicit_creator_authorization` (shown as Creator permission); the backend does not accept `permission` or `public_domain`.
