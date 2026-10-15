import json
import logging
import sys

from test_connections import headers
from test_reviews import FakeYelp, connected, work

from app.logs import JsonFormatter


def fields(caplog, message):
    return [record.fields for record in caplog.records if record.getMessage() == message]


def test_json_formatter():
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        record = logging.getLogger("t").makeRecord(
            "t", logging.ERROR, __file__, 1, "failed %s", ("job",), sys.exc_info(),
            extra={"fields": {"job_id": "j1"}})
    entry = json.loads(JsonFormatter().format(record))
    assert (entry["level"], entry["message"], entry["job_id"]) == ("ERROR", "failed job", "j1")
    assert "RuntimeError: boom" in entry["exception"]


def test_request_log_identifies_the_caller_but_never_the_token(client, tenants, caplog):
    caplog.set_level(logging.INFO)
    partner, token, secret = tenants[0]
    response = client.get("/v1/partner?ignored=secret-looking", headers=headers(tenants[0]))
    client.get("/v1/partner")
    authorized, anonymous = fields(caplog, "request")
    assert authorized["request_id"] == response.headers["x-request-id"]
    assert (authorized["status"], authorized["route"], authorized["path"]) == (
        200, "/v1/partner", "/v1/partner")
    assert (authorized["partner_id"], authorized["token_id"]) == (partner.id, token.id)
    assert (anonymous["status"], anonymous["partner_id"]) == (401, None)
    # The application's own logs only; httpx here is the test client, not the API.
    formatted = "\n".join(JsonFormatter().format(record) for record in caplog.records
                          if record.name.startswith(("app", "uvicorn")))
    assert secret not in formatted
    assert "secret-looking" not in formatted


def test_health_checks_log_at_debug(client, caplog):
    caplog.set_level(logging.INFO)
    client.get("/health/ready")
    assert fields(caplog, "request") == []


def test_sync_attempt_log(client, session, tenants, caplog):
    caplog.set_level(logging.INFO)
    connection = connected(client, tenants[0])
    work(session, FakeYelp())
    (entry,) = fields(caplog, "sync attempt finished")
    assert str(entry["connection_id"]) == connection["id"]
    assert (entry["trigger"], entry["outcome"], entry["inserted"], entry["error_code"]) == (
        "connect", "succeeded", 3, None)
    assert entry["duration_ms"] >= 0
