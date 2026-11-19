import json
import logging
import re
import sys

AUTH = re.compile(r"(?i)\b(Bearer|OAuth|Basic)\s+[^\s\"',;&}\])]+")
FIELDS = re.compile(
    r"""(?ix) (\b(?:access_token|refresh_token|upload_token|upload_id|authorization|client_secret|fb_exchange_token)["']?\s*[:=]\s*)
    (?:"[^"]*"|'[^']*'|[^\s&,;\]}]+)"""
)


def redact(value: str) -> str:
    value = AUTH.sub("[REDACTED]", value)
    return FIELDS.sub(r"\1[REDACTED]", value)


class RedactCredentials(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg, record.args = redact(record.getMessage()), ()
        if isinstance(getattr(record, "path", None), str):
            record.path = redact(record.path)
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps({"level": record.levelname, "logger": record.name, "message": record.getMessage(), **{key: getattr(record, key) for key in ("request_id", "method", "path", "status", "duration_ms") if hasattr(record, key)}})


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(RedactCredentials())
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    # HTTP client URLs and request traces may carry provider credentials.
    for name in ("httpx", "httpx2", "httpcore", "uvicorn.access"):
        logging.getLogger(name).disabled = True
