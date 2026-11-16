"""Each test uses a disposable database, including server-side auth sessions."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import auth
from app.db import Base, session
from app.main import app, _login_windows, _rate_windows


@pytest.fixture(autouse=True)
def isolated_database(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(auth, "SessionLocal", factory)
    monkeypatch.setattr(auth.settings, "media_root", str(tmp_path / "media"))

    def db_session():
        with factory() as db:
            yield db

    app.dependency_overrides[session] = db_session
    _login_windows.clear()
    _rate_windows.clear()
    yield factory
    app.dependency_overrides.clear()
    engine.dispose()
