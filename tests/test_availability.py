import json
from datetime import UTC, datetime

import httpx
import pytest
from typer.testing import CliRunner

from jotctl import cli
from jotctl.availability import read_availability
from jotctl.client import JotformClient

CLOSED = {
    "title": "Shopping",
    "status": "DISABLED",
    "expireDate": "2026-10-28 10:00",
    "expireDateTimeZone": "America/New_York",
    "limitSubmission": "84",
    "count": "84",
    "messageOfLimitedForm": "Opens again Saturday.",
}
OPEN = {
    "title": "Sign Up",
    "status": "ENABLED",
    "expireDate": "No Limit",
    "expireDateTimeZone": "",
    "limitSubmission": "48",
    "count": "3",
    "messageOfLimitedForm": "",
    "passwordProtect": "Yes",
    "passwordProtectPass": "hunter2",
}


def test_read_availability_closed():
    info = read_availability(CLOSED, now=datetime(2026, 10, 29, tzinfo=UTC))
    assert (info.status, info.expire_date, info.timezone, info.expired) == (
        "DISABLED",
        "2026-10-28 10:00",
        "America/New_York",
        True,
    )
    assert (info.limit, info.count, info.remaining) == (84, 84, 0)
    assert info.message == "Opens again Saturday."
    assert info.password_protected is False


def test_read_availability_respects_timezone():
    # 10:00 in New York is 14:00 UTC (EDT), so 13:59 UTC is still before it.
    assert read_availability(CLOSED, now=datetime(2026, 10, 28, 13, 59, tzinfo=UTC)).expired is False
    assert read_availability(CLOSED, now=datetime(2026, 10, 28, 14, 0, tzinfo=UTC)).expired is True


def test_read_availability_unset_and_unreadable():
    info = read_availability(OPEN)
    assert (info.expire_date, info.timezone, info.expired) == (None, None, None)
    assert info.remaining == 45
    assert info.password_protected is True
    assert "hunter2" not in json.dumps(info.to_json())
    assert read_availability({"expireDate": "someday", "limitSubmission": "No Limit"}).expired is None
    assert read_availability({"limitSubmission": "No Limit"}).remaining is None


@pytest.fixture
def mock_api(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        content = {"/form/42/properties": CLOSED, "/form/43/properties": OPEN}.get(request.url.path)
        if content is None:
            return httpx.Response(404, json={"responseCode": 404, "message": "Not found"})
        return httpx.Response(200, json={"responseCode": 200, "message": "success", "content": content})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        cli, "JotformClient", lambda key, region: JotformClient(key, region, transport=transport)
    )


runner = CliRunner()


def test_cli_status_closed(mock_api):
    result = runner.invoke(cli.app, ["--api-key", "k", "describe", "form", "42"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    assert "Shopping (42)" in result.output
    assert "DISABLED" in result.output
    assert "2026-10-28 10:00 America/New_York (passed)" in result.output  # the real clock is past 2026-10-28
    assert "84 (84 submitted, 0 left) reached" in result.output
    assert "Opens again Saturday." in result.output


def test_cli_status_open_json_hides_password(mock_api):
    result = runner.invoke(cli.app, ["--api-key", "k", "describe", "form", "43", "-o", "json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["id"] == "43" and data["title"] == "Sign Up"
    assert data["expireDate"] is None and data["remaining"] == 45 and data["passwordProtected"] is True
    assert "hunter2" not in result.output


def test_cli_status_table_hides_password(mock_api):
    result = runner.invoke(cli.app, ["--api-key", "k", "describe", "form", "43"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    assert "not set" in result.output
    assert "48 (3 submitted, 45 left)" in result.output
    assert "hunter2" not in result.output


def test_cli_status_unknown_form(mock_api):
    result = runner.invoke(cli.app, ["--api-key", "k", "describe", "form", "999"])
    assert result.exit_code == 1
    assert "Not found" in result.stderr
