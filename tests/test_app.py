from fastapi.testclient import TestClient

from soe.app import app


client = TestClient(app)


def test_health() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_end_to_end_flow_ingest_clip_publish_monetize() -> None:
    ingest_response = client.post(
        "/ingest",
        json={"content": "Headline\nKey insight\nCall to action"},
    )
    assert ingest_response.status_code == 200
    content_id = ingest_response.json()["content_id"]

    clip_response = client.post("/clip", json={"content_id": content_id, "max_clips": 2})
    assert clip_response.status_code == 200
    assert clip_response.json()["clips"] == ["Headline", "Key insight"]

    publish_response = client.post(
        "/publish",
        json={"content_id": content_id, "destination": "social"},
    )
    assert publish_response.status_code == 200
    assert publish_response.json()["status"] == "published"

    monetize_response = client.post(
        "/monetize",
        json={"content_id": content_id, "cpm": 12.5, "impressions": 2400},
    )
    assert monetize_response.status_code == 200
    assert monetize_response.json()["estimated_revenue"] == 30.0
