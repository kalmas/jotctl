import json

import httpx
import pytest
from typer.testing import CliRunner

from jotctl import cli
from jotctl.client import JotformClient
from jotctl.questions import ordered_questions

QUESTIONS = {
    "4": {"qid": "4", "name": "submit", "text": "Submit", "type": "control_button", "order": "5"},
    "1": {"qid": "1", "name": "heading", "text": "Contact Us", "type": "control_head", "order": "1"},
    "3": {
        "qid": "3",
        "name": "topic",
        "text": "Topic",
        "type": "control_dropdown",
        "order": "3",
        "required": "Yes",
        "options": "Billing|Support|Other",
    },
    "2": {"qid": "2", "name": "fullName", "text": "Name [bold]", "type": "control_fullname", "order": "2"},
}


def test_ordered_questions_sorts_and_parses():
    questions = ordered_questions(QUESTIONS)
    assert [q.qid for q in questions] == ["1", "2", "3", "4"]
    topic = questions[2]
    assert topic.short_type == "dropdown"
    assert topic.required is True
    assert topic.options == ["Billing", "Support", "Other"]
    assert topic.raw is QUESTIONS["3"]
    assert questions[0].is_input is False


def test_ordered_questions_inputs_only():
    assert [f.qid for f in ordered_questions(QUESTIONS, inputs_only=True)] == ["2", "3"]


def test_ordered_questions_tolerates_missing_properties():
    (f,) = ordered_questions({"7": {"type": "control_textbox"}})
    assert (f.order, f.label, f.required, f.options) == (0, "", False, [])


FORMS = [
    {"id": "42", "title": "Contact Us", "status": "ENABLED"},
    {"id": "43", "title": "Signup", "status": "ENABLED"},
    {"id": "44", "title": "signup", "status": "DISABLED"},
    {"id": "45", "title": "Old Form", "status": "DELETED"},
]


@pytest.fixture
def mock_api(monkeypatch):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/form/42/questions":
            return httpx.Response(200, json={"responseCode": 200, "message": "success", "content": QUESTIONS})
        if request.url.path == "/user/forms":
            return httpx.Response(200, json={"responseCode": 200, "message": "success", "content": FORMS})
        return httpx.Response(404, json={"responseCode": 404, "message": "Form not found"})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        cli, "JotformClient", lambda key, region: JotformClient(key, region, transport=transport)
    )
    return requests


runner = CliRunner()


def test_cli_questions_list_table(mock_api):
    result = runner.invoke(cli.app, ["--api-key", "k", "get", "questions", "--form", "42"])
    assert result.exit_code == 0, result.output
    assert mock_api[0].url.path == "/form/42/questions"
    assert "Questions on form 42 (4)" in result.output
    assert "Name [bold]" in result.output  # labels are not parsed as Rich markup
    assert "Billing, Support, Other" in result.output
    assert "dropdown" in result.output


def test_cli_questions_list_inputs_only(mock_api):
    result = runner.invoke(cli.app, ["--api-key", "k", "get", "questions", "--form", "42", "--inputs-only"])
    assert result.exit_code == 0, result.output
    assert "(2)" in result.output
    assert "Contact Us" not in result.output


def test_cli_questions_list_json(mock_api):
    result = runner.invoke(cli.app, ["--api-key", "k", "get", "questions", "--form", "42", "-o", "json"])
    assert result.exit_code == 0, result.output
    assert [q["qid"] for q in json.loads(result.stdout)] == ["1", "2", "3", "4"]


def test_cli_questions_list_unknown_form(mock_api):
    result = runner.invoke(cli.app, ["--api-key", "k", "get", "questions", "--form", "999"])
    assert result.exit_code == 1
    assert "Form not found" in result.stderr


@pytest.mark.parametrize("title", ["Contact Us", "contact us"])
def test_cli_questions_list_by_title(mock_api, title):
    result = runner.invoke(cli.app, ["--api-key", "k", "get", "questions", "--form", title])
    assert result.exit_code == 0, result.output
    assert [r.url.path for r in mock_api] == ["/user/forms", "/form/42/questions"]
    assert "Questions on form 42 (4)" in result.output


def test_cli_questions_list_by_title_prefers_exact_case(mock_api):
    runner.invoke(cli.app, ["--api-key", "k", "get", "questions", "--form", "signup"])
    assert mock_api[-1].url.path == "/form/44/questions"  # not 43, titled "Signup"


@pytest.mark.parametrize(
    ("title", "message"),
    [
        ("Nope", 'No form titled "Nope"'),
        ("Old Form", 'No form titled "Old Form"'),  # deleted forms are ignored
        ("SIGNUP", '2 forms are titled "SIGNUP" (43, 44)'),
    ],
)
def test_cli_questions_list_bad_title(mock_api, title, message):
    result = runner.invoke(cli.app, ["--api-key", "k", "get", "questions", "--form", title])
    assert result.exit_code == 1
    assert message in result.stderr
