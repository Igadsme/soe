"""Bounded HTTP metrics and opt-in error reporting."""
import hmac
import os
import time
from ipaddress import ip_address, ip_network

from fastapi import HTTPException, Request
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    multiprocess,
)
from prometheus_client.core import GaugeMetricFamily
from sqlalchemy import func, select
from starlette.responses import Response

from .config import secret_value, settings
from .models import Job

REQUESTS = Counter("soe_http_requests_total", "HTTP requests", ["method", "route", "status"])
LATENCY = Histogram("soe_http_request_duration_seconds", "HTTP request latency", ["method", "route", "status"])
IN_PROGRESS = Gauge("soe_http_requests_in_progress", "Active HTTP requests", multiprocess_mode="livesum")


def scrub_event(event, hint=None):
    """Drop request secrets, bodies, query strings, breadcrumbs and local variables."""
    request = event.get("request", {})
    for key in ("cookies", "data", "query_string"):
        request.pop(key, None)
    if "url" in request:
        request["url"] = request["url"].split("?", 1)[0].split("#", 1)[0]
    headers = request.get("headers", {})
    request["headers"] = {key: value for key, value in headers.items() if key.lower() not in {"authorization", "cookie", "set-cookie", "proxy-authorization", "x-csrf-token"}}
    event.pop("breadcrumbs", None)
    event.pop("user", None)
    for exception in event.get("exception", {}).get("values", []):
        for frame in exception.get("stacktrace", {}).get("frames", []):
            frame.pop("vars", None)
    return event


def init_sentry():
    if not secret_value(settings.sentry_dsn).strip():
        return
    import sentry_sdk
    from sentry_sdk.integrations.fastapi import FastApiIntegration
    sentry_sdk.init(
        dsn=secret_value(settings.sentry_dsn), integrations=[FastApiIntegration()],
        send_default_pii=False, traces_sample_rate=settings.sentry_traces_sample_rate,
        environment=settings.env, before_send=scrub_event,
        before_send_transaction=scrub_event, include_local_variables=False,
        max_request_body_size="never",
    )


class MetricsMiddleware:
    """Wrap the whole ASGI stack, including rejected bodies and streaming responses."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not settings.metrics_enabled:
            return await self.app(scope, receive, send)
        started, status = time.monotonic(), 500
        IN_PROGRESS.inc()

        async def record_send(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, record_send)
        finally:
            route = getattr(scope.get("route"), "path", "unmatched")
            method = scope["method"] if scope["method"] in {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"} else "OTHER"
            labels = (method, route, str(status))
            REQUESTS.labels(*labels).inc()
            LATENCY.labels(*labels).observe(time.monotonic() - started)
            IN_PROGRESS.dec()


def metrics_response(request: Request, db):
    if not settings.metrics_enabled:
        raise HTTPException(404, "not found")
    token = secret_value(settings.metrics_token)
    if token:
        if not hmac.compare_digest(request.headers.get("authorization", "").encode(), ("Bearer " + token).encode()):
            raise HTTPException(401, "invalid metrics credentials")
    else:
        # Only the socket peer counts. Never trust arbitrary forwarded headers.
        try:
            peer = ip_address(request.client.host if request.client else "")
            networks = [ip_network(n) for n in ("127.0.0.0/8", "::1/128", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")]
            networks += [ip_network(n.strip()) for n in settings.trusted_proxies.split(",") if n.strip()]
            allowed = any(peer in network for network in networks)
        except ValueError:
            allowed = False
        if not allowed:
            raise HTTPException(403, "private metrics access required")
    registry = REGISTRY
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
    # Query at scrape time instead of summing duplicate queue snapshots per worker.
    jobs = GaugeMetricFamily("soe_jobs", "Persisted jobs by status", labels=["status"])
    counts = dict(db.execute(select(Job.status, func.count()).group_by(Job.status)).all())
    for status in ("queued", "running", "succeeded", "failed"):
        jobs.add_metric([status], counts.get(status, 0))
    queue_registry = CollectorRegistry()
    class QueueCollector:
        def collect(self):
            yield jobs
    queue_registry.register(QueueCollector())
    return Response(generate_latest(registry) + generate_latest(queue_registry), media_type=CONTENT_TYPE_LATEST)
