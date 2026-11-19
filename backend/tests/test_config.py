import pytest
from app.config import Settings
from fastapi.testclient import TestClient


def production_settings(**overrides):
    values = dict(
        env="production",
        redis_url="redis://redis:6379/0",
        session_secret="test-placeholder-" * 3,
        admin_password="",
        admin_password_hash="test-hash",
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


def test_processing_deadline_settings():
    config = Settings(_env_file=None, processing_deadline_floor_seconds=300,
                      processing_deadline_duration_multiplier=5)
    assert config.processing_deadline_floor_seconds == 300
    assert config.processing_deadline_duration_multiplier == 5


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


@pytest.mark.parametrize('env', ['production', 'staging'])
@pytest.mark.parametrize('token', ['', '   '])
def test_metrics_require_token_in_production(env, token):
    with pytest.raises(RuntimeError, match='SOE_METRICS_TOKEN is required'):
        production_settings(env=env, metrics_enabled=True, metrics_token=token).validate_production()
    production_settings(env=env, metrics_enabled=True, metrics_token='test-token').validate_production()


def test_production_warns_when_proxy_trust_is_empty(monkeypatch, caplog):
    from app import main
    monkeypatch.setattr(main, 'settings', production_settings(trusted_proxies=''))
    monkeypatch.setattr(main, 'configure_logging', lambda: None)
    with TestClient(main.app):
        pass
    assert 'SOE_TRUSTED_PROXIES is empty' in caplog.text


def test_production_publishing_requires_credentials_keys_and_https_origin():
    from cryptography.fernet import Fernet
    key = Fernet.generate_key().decode()
    configured = dict(google_client_id="fixture-id", google_client_secret="fixture-secret",
                      oauth_redirect_base="https://app.example.com")
    with pytest.raises(RuntimeError, match="SOE_TOKEN_ENCRYPTION_KEYS"):
        production_settings(**configured).validate_production()
    with pytest.raises(RuntimeError, match="SOE_TOKEN_ENCRYPTION_KEYS"):
        production_settings(**configured, token_encryption_keys=f"{key},invalid").validate_production()
    with pytest.raises(RuntimeError, match="SOE_PUBLISHING_ENABLED"):
        production_settings(publishing_enabled=True, token_encryption_keys=key).validate_production()
    production_settings(**configured, publishing_enabled=True, token_encryption_keys=key).validate_production()
    configured["oauth_redirect_base"] = "http://app.example.com"
    with pytest.raises(RuntimeError, match="SOE_OAUTH_REDIRECT_BASE"):
        production_settings(**configured, publishing_enabled=True, token_encryption_keys=key).validate_production()
