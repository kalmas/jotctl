import json
from datetime import datetime

import httpx
import pytest
from typer.testing import CliRunner

from jotctl import cli
from jotctl.client import JotformClient
from jotctl.conditions import backup_conditions, describe_action, describe_terms, parse_conditions

NAMES = {"13": "whatDate13", "45": "time", "46": "formFull", "47": "apptTime", "1": "heading"}

RAW = [
    {
        "id": "2",
        "type": "calculation",
        "link": "Any",
        "priority": "1",
        "terms": json.dumps([{"id": "t", "field": "13", "operator": "isFilled", "value": "x"}]),
        "action": json.dumps(
            [{"id": "a", "resultField": "47", "equation": "{13} at {99}", "isError": False}]
        ),
    },
    {
        "id": "1",
        "type": "field",
        "link": "All",
        "priority": "0",
        "terms": json.dumps(
            [
                {"field": "45", "operator": "before", "value": "10:00 AM"},
                {"field": "language", "operator": "equals", "value": "Español"},
            ]
        ),
        "action": json.dumps(
            [
                {"visibility": "Show", "field": "46"},
                {"visibility": "HideMultiple", "fields": ["1", "13"]},
            ]
        ),
    },
]


def test_parse_conditions_decodes_and_sorts_by_priority():
    first, second = parse_conditions(RAW)
    assert (first.id, first.type, first.link, first.priority) == ("1", "field", "All", 0)
    assert first.actions[1]["fields"] == ["1", "13"]
    assert second.terms[0]["operator"] == "isFilled"


def test_parse_conditions_empty_and_json_string():
    assert parse_conditions(None) == []
    assert parse_conditions("") == []
    assert [c.id for c in parse_conditions(json.dumps(RAW))] == ["1", "2"]


def test_describe_terms():
    field_rule, calc = parse_conditions(RAW)
    assert describe_terms(field_rule, NAMES) == 'time before "10:00 AM" and language equals "Español"'
    assert describe_terms(calc, NAMES) == "whatDate13 is filled"  # unary: value omitted


def test_describe_actions():
    field_rule, calc = parse_conditions(RAW)
    assert [describe_action(a, "field", NAMES) for a in field_rule.actions] == [
        "show formFull",
        "hide heading, whatDate13",
    ]
    assert describe_action(calc.actions[0], "calculation", NAMES) == "apptTime = {whatDate13} at {#99}"


def test_describe_message_and_fallback():
    message = {"message": "<p>Thanks &amp; see you <b>{whatDate13}</b></p>"}
    assert describe_action(message, "message", {}) == 'show thank-you message "Thanks & see you {whatDate13}"'
    other = {"id": "x", "isError": False, "redirect": "https://example.com", "empty": ""}
    assert describe_action(other, "url", {}) == "redirect=https://example.com"


@pytest.fixture
def mock_api(monkeypatch):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        content = {
            "/form/42/properties": {"title": "Contact Us", "conditions": RAW},
            "/form/7/properties": {"title": "No logic"},
            "/form/42/questions": {qid: {"qid": qid, "name": name} for qid, name in NAMES.items()},
        }.get(request.url.path)
        if content is None:
            return httpx.Response(404, json={"responseCode": 404, "message": "Not found"})
        return httpx.Response(200, json={"responseCode": 200, "message": "success", "content": content})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        cli, "JotformClient", lambda key, region: JotformClient(key, region, transport=transport)
    )
    return requests


runner = CliRunner()


def test_cli_conditions_table(mock_api):
    result = runner.invoke(cli.app, ["--api-key", "k", "forms", "conditions", "42"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    assert "Conditions on form 42 (2)" in result.output
    assert 'time before "10:00 AM" and language equals "Español"' in result.output
    assert "hide heading, whatDate13" in result.output
    assert result.output.index("show formFull") < result.output.index("apptTime =")


def test_cli_conditions_json(mock_api):
    result = runner.invoke(cli.app, ["--api-key", "k", "forms", "conditions", "42", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert [c["id"] for c in data] == ["1", "2"]
    assert data[0]["actions"][0] == {"visibility": "Show", "field": "46"}
    assert "/form/42/questions" not in [r.url.path for r in mock_api]  # names aren't needed for JSON


def test_cli_conditions_none(mock_api):
    result = runner.invoke(cli.app, ["--api-key", "k", "forms", "conditions", "7"])
    assert result.exit_code == 0, result.output
    assert "No conditions found." in result.output


def test_cli_conditions_unknown_form(mock_api):
    result = runner.invoke(cli.app, ["--api-key", "k", "forms", "conditions", "999"])
    assert result.exit_code == 1
    assert "Not found" in result.stderr


def test_backup_conditions_never_overwrites(tmp_path):
    now = datetime(2026, 10, 29, 15, 30, 0)
    first = backup_conditions(RAW, "42", tmp_path / "b", now=now)
    second = backup_conditions([], "42", tmp_path / "b", now=now)
    assert first.name == "42-conditions-20261029-153000.json"
    assert second.name == "42-conditions-20261029-153000-2.json"
    assert json.loads(first.read_text()) == RAW
    assert json.loads(second.read_text()) == []
