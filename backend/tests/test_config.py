import pytest
from fastapi.testclient import TestClient

from app.config import Settings


def production_settings(**overrides):
    values = dict(
        env="production",
        redis_url="redis://redis:6379/0",
        session_secret="test-placeholder-" * 3,
        admin_password="test-only-password",
        admin_password_hash="",
        demo_mode=False,
        cors_origins="https://app.example.com",
        database_url="postgresql+psycopg://test:test@db/test",
    )
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_valid_production_settings():
    production_settings().validate_production()
    production_settings(admin_password="", admin_password_hash="test-hash").validate_production()


@pytest.mark.parametrize(("overrides", "variable"), [
    ({"redis_url": ""}, "SOE_REDIS_URL"),
    ({"redis_url": "   "}, "SOE_REDIS_URL"),
    ({"session_secret": ""}, "SOE_SESSION_SECRET"),
    ({"session_secret": "short"}, "SOE_SESSION_SECRET"),
    ({"admin_password": "", "admin_password_hash": ""}, "SOE_ADMIN_PASSWORD"),
    ({"demo_mode": True}, "SOE_DEMO_MODE"),
    ({"cors_origins": ""}, "SOE_CORS_ORIGINS"),
    ({"cors_origins": " , "}, "SOE_CORS_ORIGINS"),
    ({"cors_origins": "https://app.example.com, *"}, "SOE_CORS_ORIGINS"),
    ({"cors_origins": "https://*.example.com"}, "SOE_CORS_ORIGINS"),
    ({"database_url": "sqlite:///./test.db"}, "SOE_DATABASE_URL"),
    ({"database_url": "sqlite+aiosqlite:///:memory:"}, "SOE_DATABASE_URL"),
])
def test_rejects_unsafe_production_settings(overrides, variable):
    with pytest.raises(RuntimeError, match=variable):
        production_settings(**overrides).validate_production()


def test_validation_does_not_disclose_values():
    with pytest.raises(RuntimeError) as exc:
        production_settings(session_secret="private-test-value", database_url="sqlite:///private-path").validate_production()
    assert "private-test-value" not in str(exc.value)
    assert "private-path" not in str(exc.value)


def test_development_retains_defaults():
    Settings(_env_file=None, env="development").validate_production()


def test_lifespan_validates_production(monkeypatch):
    from app import main

    monkeypatch.setattr(main, "settings", production_settings(demo_mode=True))
    with pytest.raises(RuntimeError, match="SOE_DEMO_MODE"):
        with TestClient(main.app):
            pass


def test_tests_ignore_working_directory_dotenv(tmp_path, monkeypatch):
    (tmp_path / '.env').write_text('SOE_ADMIN_PASSWORD=fixture-only-password\nSOE_DATABASE_URL=sqlite:///wrong-test-db.db\n')
    monkeypatch.chdir(tmp_path)
    config = Settings()
    assert config.database_url == 'sqlite://'
    assert not config.admin_password
    assert not (tmp_path / 'wrong-test-db.db').exists()
