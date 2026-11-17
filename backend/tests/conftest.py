"""Each test uses a disposable database, including server-side auth sessions."""
import os

# Set before importing application modules: never load developer dotenv or DB.
os.environ["SOE_ENV_FILE"] = "/nonexistent/soe-test-env"
os.environ["SOE_DATABASE_URL"] = "sqlite://"
for name, value in {
    "DEV_AUTH_BYPASS": "true", "ENV": "development", "DEMO_MODE": "true", "ADMIN_PASSWORD": "",
    "ADMIN_PASSWORD_HASH": "", "SESSION_SECRET": "", "REDIS_URL": "",
    "YOUTUBE_API_KEY": "", "TWITCH_CLIENT_ID": "", "TWITCH_CLIENT_SECRET": "",
    "PUBLISHING_ENABLED": "false", "CORS_ORIGINS": "http://localhost:3000",
    "RATE_LIMIT_PER_MINUTE": "120", "TRUSTED_PROXIES": "",
}.items():
    os.environ["SOE_" + name] = value

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import auth, worker
from app.db import Base, session
from app.main import app, _login_windows, _rate_windows


@pytest.fixture(autouse=True)
def isolated_database(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    original_init = TestClient.__init__
    def loopback_client(self, *args, **kwargs):
        kwargs.setdefault("client", ("127.0.0.1", 50000))
        original_init(self, *args, **kwargs)
    monkeypatch.setattr(TestClient, "__init__", loopback_client)
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(auth, "SessionLocal", factory)
    monkeypatch.setattr(worker, "SessionLocal", factory)
    monkeypatch.setattr(auth.settings, "media_root", str(tmp_path / "media"))

    def db_session():
        with factory() as db:
            yield db

    app.dependency_overrides[session] = db_session
    from app import main
    monkeypatch.setattr(main, "development_bypass", auth.development_bypass)
    main._login_backoffs.clear()
    _login_windows.clear()
    _rate_windows.clear()
    from app import rate_limit
    monkeypatch.setattr(rate_limit, "_failures", 0)
    monkeypatch.setattr(rate_limit, "_open_until", 0.0)
    yield factory
    app.dependency_overrides.clear()
    engine.dispose()
