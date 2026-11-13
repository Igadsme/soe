from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from app import auth


def test_signed_session_rejects_tampering(monkeypatch):
    monkeypatch.setattr(auth.settings, "session_secret", "test-secret")
    monkeypatch.setattr(auth.settings, "session_ttl_seconds", 60)
    token = auth.make_session("operator")
    assert auth.verify_session(token) == "operator"
    with pytest.raises(HTTPException) as error:
        auth.verify_session(token[:-2] + "xx")
    assert error.value.status_code == 401
