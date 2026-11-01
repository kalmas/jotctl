"""-o output formats and resource aliases, using `get forms` and friends against a mock API."""

import csv
import json

import httpx
import pytest
import yaml
from typer.testing import CliRunner

from jotctl import cli
from jotctl.client import JotformClient

FORMS = [
    {"id": "1", "title": "Alpha", "status": "ENABLED", "count": "3", "created_at": "2026-01-01 00:00:00"},
    {
        "id": "2",
        "title": "Beta, with comma",
        "status": "DISABLED",
        "count": "0",
        "created_at": "2026-02-01 00:00:00",
    },
]
QUESTIONS = {
    "3": {"qid": "3", "name": "yourName", "text": "Your name", "type": "control_textbox", "order": "1"}
}


@pytest.fixture(autouse=True)
def mock_api(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        content = {
            "/user/forms": FORMS,
            "/form/1/questions": QUESTIONS,
            "/form/1/submissions": [],
            "/form/1": FORMS[0],
            "/form/1/properties": {"title": "Alpha", "status": "ENABLED"},
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
    return runner.invoke(cli.app, ["--api-key", "k", *args], env={"COLUMNS": "200"})


def test_table_and_wide():
    table = run("get", "forms")
    assert table.exit_code == 0, table.output
    assert "Forms (2)" in table.output
    assert "Alpha" in table.output
    assert "Created" not in table.output
    wide = run("get", "forms", "-o", "wide")
    assert "Created" in wide.output
    assert "2026-01-01 00:00:00" in wide.output


def test_json_yaml_name_csv():
    assert json.loads(run("get", "forms", "-o", "json").stdout) == FORMS
    assert yaml.safe_load(run("get", "forms", "--output", "yaml").stdout) == FORMS
    assert run("get", "forms", "-o", "name").stdout == "1\n2\n"
    rows = list(csv.reader(run("get", "forms", "-o", "csv").stdout.splitlines()))
    assert rows[0] == [
        "ID",
        "Title",
        "Status",
        "Submissions",
        "Last submission",
        "Created",
        "Updated",
    ]  # wide columns
    assert rows[2][1] == "Beta, with comma"


def test_unsupported_output_is_a_usage_error():
    result = run("describe", "form", "1", "-o", "csv")
    assert result.exit_code == 2
    assert "isn't supported here" in result.output


@pytest.mark.parametrize(
    "args",
    [
        ["get", "form", "-o", "name"],
        ["get", "question", "--form", "1", "-o", "name"],
        ["get", "q", "--form", "1", "-o", "name"],
        ["get", "sub", "--form", "1", "-o", "name"],
        ["get", "submission", "--form", "1", "-o", "name"],
        ["describe", "forms", "1", "-o", "json"],
        ["describe", "sub", "--form", "1", "-o", "json"],
        ["get", "condition", "--form", "1", "-o", "name"],
    ],
)
def test_aliases(args):
    result = run(*args)
    assert result.exit_code == 0, result.output


def test_unknown_resource():
    result = run("get", "widgets")
    assert result.exit_code == 2
    assert "No such command 'widgets'" in result.output


def test_empty_listing():
    result = run("get", "submissions", "--form", "1")
    assert result.exit_code == 0, result.output
    assert "No submissions found." in result.output
    assert run("get", "submissions", "--form", "1", "-o", "json").stdout.strip() == "[]"


def test_help_lists_verbs_in_kubectl_order():
    result = runner.invoke(cli.app, ["--help"], env={"COLUMNS": "200"})
    positions = [
        result.output.index(f"│ {verb} ") for verb in ["get", "describe", "apply", "diff", "delete", "report"]
    ]
    assert positions == sorted(positions)
