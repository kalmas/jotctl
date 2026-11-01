import json

import httpx
import pytest
from typer.testing import CliRunner

from jotctl import cli
from jotctl.client import FormOrderBy, FormStatus, JotformClient, JotformError

SAMPLE_FORMS = [
    {
        "id": "251234567890",
        "title": "Contact Us",
        "status": "ENABLED",
        "count": "42",
        "last_submission": "2026-10-20 14:03:11",
        "created_at": "2026-01-05 09:00:00",
    },
]


def make_transport(requests: list[httpx.Request], *, status: int = 200, body: dict | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = body or {
            "responseCode": 200,
            "message": "success",
            "content": SAMPLE_FORMS,
            "limit-left": 4986,
        }
        return httpx.Response(status, json=payload)

    return httpx.MockTransport(handler)


def test_list_forms_sends_auth_and_params():
    requests: list[httpx.Request] = []
    with JotformClient("secret", transport=make_transport(requests)) as client:
        forms = client.list_forms(
            limit=5, offset=10, status=FormStatus.ENABLED, order_by=FormOrderBy.CREATED_AT
        )

    assert forms == SAMPLE_FORMS
    req = requests[0]
    assert req.url.path == "/user/forms"
    assert req.headers["APIKEY"] == "secret"
    assert req.url.params["limit"] == "5"
    assert req.url.params["offset"] == "10"
    assert json.loads(req.url.params["filter"]) == {"status": "ENABLED"}
    assert req.url.params["orderby"] == "created_at"


def test_list_forms_raises_on_api_error():
    transport = make_transport([], status=401, body={"responseCode": 401, "message": "You're not authorized"})
    with (
        JotformClient("bad", transport=transport) as client,
        pytest.raises(JotformError, match="not authorized"),
    ):
        client.list_forms()


@pytest.fixture
def mock_cli(monkeypatch):
    requests: list[httpx.Request] = []
    transport = make_transport(requests)
    monkeypatch.setattr(
        cli, "JotformClient", lambda key, region: JotformClient(key, region, transport=transport)
    )
    return requests


runner = CliRunner()


def test_cli_forms_list_table(mock_cli):
    result = runner.invoke(cli.app, ["--api-key", "secret", "get", "forms"])
    assert result.exit_code == 0, result.output
    assert "Contact Us" in result.output
    assert "251234567890" in result.output


def test_cli_forms_list_json(mock_cli):
    result = runner.invoke(cli.app, ["--api-key", "secret", "get", "forms", "-o", "json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == SAMPLE_FORMS


def test_cli_requires_api_key(mock_cli, monkeypatch):
    monkeypatch.delenv("JOTFORM_API_KEY", raising=False)
    result = runner.invoke(cli.app, ["get", "forms"])
    assert result.exit_code == 1
    assert not mock_cli


def test_cli_forms_list_rejects_unknown_order_by(mock_cli):
    result = runner.invoke(cli.app, ["--api-key", "secret", "get", "forms", "--order-by", "bogus"])
    assert result.exit_code == 2
    assert not mock_cli


def test_client_tracks_limit_left():
    with JotformClient("secret", transport=make_transport([])) as client:
        assert client.limit_left is None
        client.list_forms()
        assert client.limit_left == 4986


def test_cli_show_quota(mock_cli):
    result = runner.invoke(cli.app, ["--api-key", "secret", "--show-quota", "get", "forms", "-o", "json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == SAMPLE_FORMS  # quota goes to stderr, JSON stays clean
    assert "API requests remaining today: 4,986" in result.stderr


def test_cli_quota_hidden_by_default(mock_cli):
    result = runner.invoke(cli.app, ["--api-key", "secret", "get", "forms"])
    assert result.exit_code == 0, result.output
    assert "remaining" not in result.output


def test_cli_show_quota_after_api_error(monkeypatch):
    body = {"responseCode": 401, "message": "You're not authorized", "limit-left": 12}
    transport = make_transport([], status=401, body=body)
    monkeypatch.setattr(
        cli, "JotformClient", lambda key, region: JotformClient(key, region, transport=transport)
    )
    result = runner.invoke(cli.app, ["--api-key", "bad", "--show-quota", "get", "forms"])
    assert result.exit_code == 1
    assert "not authorized" in result.stderr
    assert "API requests remaining today: 12" in result.stderr


def test_cli_show_quota_short_flag(mock_cli):
    result = runner.invoke(cli.app, ["--api-key", "secret", "-Q", "get", "forms", "-o", "json"])
    assert result.exit_code == 0, result.output
    assert "API requests remaining today: 4,986" in result.stderr
