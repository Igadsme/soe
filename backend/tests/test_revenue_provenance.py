"""Release test H (financial integrity): provenance classes, CSV import, unavailable earnings, recommendations.

All monetary rows here are synthetic test data created by the test itself.
"""
import csv
import io
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import select

from app import main, ranking
from app.config import settings
from app.models import AuditEvent, Clip, LedgerEntry, MetricEvent, Publication, Source

HEADER = "date,kind,category,amount,currency,platform,reference\n"


def manual(client, **overrides):
    body = {"kind": "revenue", "category": "sponsorship", "cents": 50000, "reference": "Synthetic sponsorship invoice #1", **overrides}
    return client.post("/api/v1/revenue/entries", json=body)


def test_estimated_values_never_count_as_revenue(isolated_database):
    with TestClient(main.app) as client:
        response = manual(client, provenance="estimated")
        assert response.status_code == 422 and "never as revenue" in response.text
        assert manual(client, kind="expense", category="cloud_cost", cents=1200, provenance="estimated").status_code == 201
        assert manual(client, kind="expense", category="cloud_cost", cents=800).status_code == 201
        assert manual(client).status_code == 201
        with isolated_database() as db:
            # A legacy/defective row claiming estimated revenue must be excluded and surfaced, not summed.
            db.add(LedgerEntry(kind="revenue", category="other", cents=999_999, provenance="estimated", note="defective"))
            db.commit()
        summary = client.get("/api/v1/revenue/summary").json()
    assert summary["revenue_cents"] == {"verified_platform": 0, "imported": 0, "manual": 50000, "total": 50000}
    assert summary["cost_cents"] == {"actual": 800, "estimated": 1200, "total": 2000}
    assert summary["net_cents"] == 48000
    assert summary["excluded_revenue_cents"] == 999_999


def test_unavailable_platform_earnings_are_not_zero(isolated_database):
    with TestClient(main.app) as client:
        summary = client.get("/api/v1/revenue/summary").json()
        assert all(item["status"] == "unavailable" and item["cents"] is None and item["reason"]
                   for item in summary["platform_earnings"].values())
        assert manual(client, category="platform_earnings", platform="youtube", cents=1234).status_code == 201
        summary = client.get("/api/v1/revenue/summary").json()
        overview = client.get("/api/v1/overview").json()
    assert summary["platform_earnings"]["youtube"] == {"status": "available", "cents": 1234, "provenance": ["manual"]}
    assert summary["platform_earnings"]["tiktok"]["status"] == "unavailable"
    assert overview["revenue_cents"] == 1234 and overview["revenue"]["platform_earnings"]["instagram"]["cents"] is None


def test_validation_rejects_bad_categories_and_periods(isolated_database):
    with TestClient(main.app) as client:
        assert manual(client, category="processing_cost").status_code == 422
        assert manual(client, kind="expense", category="sponsorship").status_code == 422
        assert manual(client, period_start="2026-02-01", period_end="2026-01-01").status_code == 422
        assert manual(client, cents=0).status_code == 422
        assert manual(client, currency="usd").status_code == 422
        assert manual(client, extra="x").status_code == 422


def test_csv_import_reconciles_and_rejects_duplicates(isolated_database):
    body = HEADER + ("2026-10-01,revenue,platform_earnings,12.34,USD,tiktok,Synthetic statement Oct\n"
                     "2026-10-02,revenue,affiliate,5.00,USD,,Synthetic affiliate payout\n"
                     "2026-10-03,expense,cloud_cost,3.21,USD,,Synthetic hosting invoice\n")
    with TestClient(main.app) as client:
        response = client.post("/api/v1/revenue/import", files={"file": ("oct.csv", body, "text/csv")})
        assert response.status_code == 201, response.text
        assert response.json()["rows"] == 3
        duplicate = client.post("/api/v1/revenue/import", files={"file": ("again.csv", body, "text/csv")})
        assert duplicate.status_code == 409
        assert manual(client).status_code == 201
        summary = client.get("/api/v1/revenue/summary").json()
        entries = client.get("/api/v1/revenue").json()["entries"]
    assert summary["revenue_cents"] == {"verified_platform": 0, "imported": 1734, "manual": 50000, "total": 51734}
    assert summary["cost_cents"]["actual"] == 321
    assert summary["net_cents"] == 51734 - 321
    assert summary["platform_earnings"]["tiktok"] == {"status": "available", "cents": 1234, "provenance": ["imported"]}
    imported = [entry for entry in entries if entry["provenance"] == "imported"]
    assert len(imported) == 3 and all(entry["import_id"] and not entry["verified"] for entry in imported)
    with isolated_database() as db:
        assert db.scalar(select(AuditEvent).where(AuditEvent.action == "ledger.import")) is not None


def test_csv_import_validation(isolated_database):
    cases = {
        "wrong,header\n1,2\n": "header must be exactly",
        HEADER: "no rows",
        HEADER + "2026-10-01,revenue,platform_earnings,-1,USD,youtube,x\n": "row 2",
        HEADER + "2026-10-01,revenue,platform_earnings,1.234,USD,youtube,x\n": "row 2",
        HEADER + "not-a-date,revenue,affiliate,1,USD,,x\n": "row 2",
        HEADER + "2026-10-01,revenue,affiliate,1,USD,myspace,x\n": "row 2",
        HEADER + "2026-10-01,revenue,affiliate,1,USD\n": "row 2",
        HEADER + "2026-10-01,revenue,processing_cost,1,USD,,x\n": "row 2",
    }
    with TestClient(main.app) as client:
        for body, expected in cases.items():
            response = client.post("/api/v1/revenue/import", files={"file": ("bad.csv", body, "text/csv")})
            assert response.status_code == 422 and expected in response.text, (body, response.text)
        big = HEADER + "2026-10-01,revenue,affiliate,1,USD,,x\n" * 40000
        assert client.post("/api/v1/revenue/import", files={"file": ("big.csv", big, "text/csv")}).status_code == 413
        latin = (HEADER + "2026-10-01,revenue,affiliate,1,USD,,caf\xe9\n").encode("latin-1")
        assert client.post("/api/v1/revenue/import", files={"file": ("l.csv", latin, "text/csv")}).status_code == 422


def test_export_neutralizes_formula_injection(isolated_database):
    with TestClient(main.app) as client:
        assert manual(client, reference="=HYPERLINK(\"http://evil.test\")").status_code == 201
        assert manual(client, reference="+cmd|' /C calc'!A0").status_code == 201
        response = client.get("/api/v1/revenue/export.csv")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    rows = list(csv.reader(io.StringIO(response.text)))
    references = [row[-1] for row in rows[1:]]
    assert references == ["'=HYPERLINK(\"http://evil.test\")", "'+cmd|' /C calc'!A0"]


def test_processing_estimates_are_estimated_costs(isolated_database):
    with isolated_database() as db:
        db.add(LedgerEntry(kind="expense", category="processing_estimate", cents=40, provenance="estimated", note="estimate"))
        db.commit()
    with TestClient(main.app) as client:
        summary = client.get("/api/v1/revenue/summary").json()
    assert summary["revenue_cents"]["total"] == 0 and summary["cost_cents"] == {"actual": 0, "estimated": 40, "total": 40}


def synthetic(n, signal=True):
    """Synthetic observations (test data only): longer clips and evening posts perform better when signal=True."""
    rows = []
    for index in range(n):
        duration = 15 + (index % 6) * 9
        hour = (index * 5) % 24
        evening = 1.0 if 17 <= hour <= 22 else 0.0
        views = 100 * (1 + (duration / 60 if signal else 0) + evening) * (1 + (index % 3) / 10)
        rows.append(ranking.Observation(60 + index % 30, duration, hour, views, 40 + (index % 5)))
    return rows


def test_learner_threshold_weights_and_blend():
    insufficient = ranking.recommendations(synthetic(29), 30)
    assert insufficient["state"] == "insufficient_data" and insufficient["weights"] is None and insufficient["blend_weight"] == 0
    learned = ranking.recommendations(synthetic(60), 30)
    assert learned["state"] == "learned" and learned["weights"]["duration_minutes"] > 0
    assert learned["suggestions"]["duration"] == "longer"
    assert 17 <= learned["suggestions"]["best_posting_hour_utc"] <= 23
    assert "virality" in learned["disclaimer"]
    assert ranking.blend_weight(29, 30) == 0 and 0 < ranking.blend_weight(30, 30) < ranking.blend_weight(300, 30) <= ranking.BLEND_CAP
    assert ranking.blended_score(70, 30, 18, None, 100, 30) == 70
    blended = ranking.blended_score(70, 50, 19, learned["weights"], 60, 30)
    assert 0 <= blended <= 100 and blended != 70


def test_recommendations_endpoint_uses_only_verified_7d_metrics(isolated_database, monkeypatch):
    monkeypatch.setattr(settings, "learner_min_samples", 5)
    published = datetime(2026, 10, 1, 18, tzinfo=timezone.utc)
    with isolated_database() as db:
        source = Source(platform="upload", external_id="learn", title="t", url="https://example.test/l")
        db.add(source)
        db.flush()
        for index in range(6):
            clip = Clip(source_id=source.id, start_seconds=0, end_seconds=20 + index * 5, description="d", hook="h",
                        score=70, status="approved", evidence={})
            db.add(clip)
            db.flush()
            publication = Publication(clip_id=clip.id, platform="youtube", status="succeeded",
                                      published_at=published + timedelta(hours=index))
            db.add(publication)
            db.flush()
            db.add(MetricEvent(publication_id=publication.id, metric="views", value=100 + index * 50, verified=True,
                               provenance="verified", window="7d"))
            # Unverified/imported and non-7d rows must be ignored.
            db.add(MetricEvent(publication_id=publication.id, metric="views", value=10**9, provenance="imported", window="7d"))
            db.add(MetricEvent(publication_id=publication.id, metric="views", value=10**9, provenance="verified", window="1h"))
        db.commit()
    with TestClient(main.app) as client:
        result = client.get("/api/v1/analytics/recommendations").json()
        monkeypatch.setattr(settings, "learner_min_samples", 7)
        insufficient = client.get("/api/v1/analytics/recommendations").json()
    assert result["state"] == "learned" and result["sample_size"] == 6 and result["weights"]["duration_minutes"] > 0
    assert insufficient["state"] == "insufficient_data" and insufficient["required_samples"] == 7
