"""JSON logs, one object per line, for the API and the worker.

Pass structured context as `extra={"fields": {...}}`. Never log tokens, Authorization
headers, query strings, or review content: identifiers and outcomes are enough to operate.
"""
import json
import logging
from datetime import UTC, datetime


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            **getattr(record, "fields", {}),
        }
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def configure(level: str = "INFO"):
    """Routes every log, including uvicorn's, through one JSON handler on stdout."""
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
    # Replaced by the API's own request log, which carries the request ID and partner.
    logging.getLogger("uvicorn.access").disabled = True
