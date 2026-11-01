import json

import httpx
import pytest
from typer.testing import CliRunner

from jotctl import cli
from jotctl.client import JotformClient, redact

FORMS = [{"id": "42", "title": "Contact Us", "status": "ENABLED"}]
PROPERTIES = {
    "title": "Contact Us",
    "status": "ENABLED",
    "passwordProtect": "Yes",
    "passwordProtectPass": "hunter2",
}


@pytest.fixture
def mock_api(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        content = {
            "/user/forms": FORMS,
            "/form/42/properties": PROPERTIES if request.method == "GET" else {"status": "DISABLED"},
            "/form/42/question/3": {"text": "New"},
        }.get(request.url.path)
        if content is None:
            return httpx.Response(404, json={"responseCode": 404, "message": "Not found"})
        return httpx.Response(200, json={"responseCode": 200, "message": "success", "content": content})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        cli, "JotformClient", lambda key, region: JotformClient(key, region, transport=transport)
    )


runner = CliRunner()


def run(*args):
    return runner.invoke(cli.app, ["--api-key", "k", *args], env={"COLUMNS": "100"})


def test_quiet_by_default(mock_api):
    result = run("get", "forms", "-o", "json")
    assert result.exit_code == 0, result.output
    assert result.stderr == ""


def test_verbose_logs_requests_to_stderr(mock_api):
    result = run("-v", "get", "forms", "-o", "json")
    assert result.exit_code == 0, result.output
    assert result.stderr.splitlines() == ["GET /user/forms?limit=20&offset=0 200"]
    assert json.loads(result.stdout) == FORMS  # stdout stays clean
    assert "response body" not in result.stderr


def test_verbose_logs_errors(mock_api):
    result = run("-v", "describe", "form", "999")
    assert result.exit_code == 1
    assert "GET /form/999/properties 404" in result.stderr


def test_very_verbose_logs_bodies_without_wrapping_or_secrets(mock_api):
    result = run("-vv", "describe", "form", "42")
    assert result.exit_code == 0, result.output
    lines = result.stderr.splitlines()
    assert lines[0] == "GET /form/42/properties 200"
    assert lines[1].startswith("response body: ")
    body = json.loads(lines[1].removeprefix("response body: "))  # one unwrapped line of valid JSON
    assert body["content"]["passwordProtectPass"] == "[redacted]"
    assert "hunter2" not in result.output


def test_very_verbose_logs_request_bodies(monkeypatch, tmp_path):
    state = dict(PROPERTIES)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            state.update(json.loads(request.content)["properties"])
        content = {} if request.url.path == "/form/42/questions" else state
        return httpx.Response(200, json={"responseCode": 200, "message": "success", "content": content})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        cli, "JotformClient", lambda key, region: JotformClient(key, region, transport=transport)
    )
    manifest = tmp_path / "m.yaml"
    manifest.write_text(
        "forms:\n  - '42':\n      properties:\n        status: DISABLED\n        passwordProtectPass: s3cret\n"
    )
    result = run("-vv", "apply", "-f", str(manifest), "--yes")
    assert result.exit_code == 0, result.output
    lines = result.stderr.splitlines()
    put = lines.index("PUT /form/42/properties 200")
    sent = json.loads(lines[put + 1].removeprefix("request body: "))
    assert sent == {"properties": {"status": "DISABLED", "passwordProtectPass": "[redacted]"}}
    assert "s3cret" not in result.output and "hunter2" not in result.output


def test_redact():
    assert json.loads(redact('{"a": [{"passwordProtectPass": "x", "b": 1}]}')) == {
        "a": [{"passwordProtectPass": "[redacted]", "b": 1}]
    }
    assert (
        redact("properties%5BpasswordProtectPass%5D=x&a=1")
        == "properties%5BpasswordProtectPass%5D=%5Bredacted%5D&a=1"
    )
    assert redact("plain text") == "plain text"
    assert redact("") == ""


def test_flag_can_be_repeated_as_separate_options(mock_api):
    result = run("-v", "-v", "describe", "form", "42")
    assert result.exit_code == 0, result.output
    assert "response body: " in result.stderr
