# Testing

Run `PYTHONPATH=backend pytest -q`. The default suite covers health, fixture discovery persistence, deterministic ranking, fail-closed rights behavior, and pipeline candidate evidence. Run `SOE_RUN_MEDIA_E2E=1 PYTHONPATH=backend pytest -q backend/tests/test_media_e2e.py` for the real local-media test; it generates original media, uses Faster-Whisper, renders captioned MP4s, and validates 1080x1920 output. Live provider and publishing credentials are not fabricated.
