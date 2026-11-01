import csv
import json

import httpx
import pytest
from typer.testing import CliRunner

from jotctl import cli
from jotctl.client import JotformClient
from jotctl.submissions import answer_columns, format_answer, normalize_headers, summarize, write_csv

QUESTIONS = {
    "1": {"qid": "1", "name": "heading", "text": "Contact Us", "type": "control_head", "order": "1"},
    "3": {"qid": "3", "name": "email", "text": "Email", "type": "control_email", "order": "3"},
    "2": {"qid": "2", "name": "fullName", "text": "Name", "type": "control_fullname", "order": "2"},
    "4": {"qid": "4", "name": "topics", "text": "Topics", "type": "control_checkbox", "order": "4"},
    "5": {"qid": "5", "name": "submit", "text": "Submit", "type": "control_button", "order": "5"},
}


def make_submission(sid, created_at, *, status="ACTIVE", new="0", flag="0", email="a@example.com"):
    return {
        "id": sid,
        "created_at": created_at,
        "status": status,
        "new": new,
        "flag": flag,
        "answers": {
            "2": {"answer": {"first": "Ada", "last": "Lovelace"}, "prettyFormat": "Ada Lovelace"},
            "3": {"answer": email},
            "4": {"answer": ["Billing", "Support"]},
        },
    }


SUBMISSIONS = [
    make_submission("101", "2026-09-03 10:00:00", new="1"),
    make_submission("102", "2026-10-01 09:30:00", flag="1"),
    make_submission("103", "2026-10-20 14:03:11", status="DELETED"),
]


def test_summarize():
    summary = summarize(SUBMISSIONS)
    assert summary.total == 3
    assert summary.unread == 1
    assert summary.flagged == 1
    assert summary.first == "2026-09-03 10:00:00"
    assert summary.last == "2026-10-20 14:03:11"
    assert summary.by_month == {"2026-09": 1, "2026-10": 2}
    assert summary.by_status == {"ACTIVE": 2, "DELETED": 1}


def test_summarize_empty():
    summary = summarize([])
    assert summary.total == 0
    assert summary.first is None


def test_answer_columns_skips_layout_and_sorts_by_order():
    assert answer_columns(QUESTIONS) == [("2", "Name"), ("3", "Email"), ("4", "Topics")]


def test_answer_columns_breaks_order_ties_by_qid():
    # Mirrors the API docs example, where two questions share order "1".
    questions = {
        "2": {"name": "fullName2", "text": "Full Name", "type": "control_fullname", "order": "1"},
        "1": {"name": "textboxExample1", "text": "Textbox Example", "type": "control_textbox", "order": "1"},
        "3": {"name": "yourMessage", "text": "Your Message", "type": "control_textarea", "order": "3"},
    }
    assert [qid for qid, _ in answer_columns(questions)] == ["1", "2", "3"]


def test_answer_columns_disambiguates_duplicate_labels():
    questions = {
        "1": {"name": "phoneHome", "text": "Phone", "type": "control_phone", "order": "1"},
        "2": {"name": "phoneWork", "text": "Phone", "type": "control_phone", "order": "2"},
    }
    assert [h for _, h in answer_columns(questions)] == ["Phone", "Phone (phoneWork)"]


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (None, ""),
        ({"answer": "hello"}, "hello"),
        ({"answer": ["a", "", "b"]}, "a; b"),
        ({"answer": {"first": "Ada", "last": "Lovelace"}, "prettyFormat": "Ada Lovelace"}, "Ada Lovelace"),
        ({"answer": {"Row 1": "Yes", "Row 2": ""}}, "Row 1: Yes"),
        ({"text": "Unanswered question"}, ""),
    ],
)
def test_format_answer(answer, expected):
    assert format_answer(answer) == expected


def test_write_csv(tmp_path):
    path = tmp_path / "out.csv"
    write_csv(path, QUESTIONS, SUBMISSIONS[:1])
    with path.open(encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    assert rows == [
        ["Submission ID", "Submitted at", "Status", "Name", "Email", "Topics"],
        ["101", "2026-09-03 10:00:00", "ACTIVE", "Ada Lovelace", "a@example.com", "Billing; Support"],
    ]


def test_write_csv_normalized_headers(tmp_path):
    path = tmp_path / "out.csv"
    write_csv(path, QUESTIONS, SUBMISSIONS[:1], normalize=True)
    with path.open(encoding="utf-8-sig") as f:
        assert next(csv.reader(f)) == ["submission_id", "submitted_at", "status", "name", "email", "topics"]


def test_normalize_headers():
    assert normalize_headers(
        [
            "Shopper's Name",
            "  Café / Résumé?  ",
            "Can we add you to our text and email lists?",
            "Can we add you to our text lists?",
            "!!!",
            "E-mail",
            "e mail",
        ]
    ) == [
        "shopper_s_name",
        "cafe_resume",
        "can_we_add_you_to_ou",
        "can_we_add_you_to_2",
        "column",
        "e_mail",
        "e_mail_2",
    ]


def api_transport(submissions, *, requests=None):
    """Mock Jotform that serves one form, its questions, and paginated submissions."""

    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        path = request.url.path
        if path == "/form/42/submissions":
            offset = int(request.url.params["offset"])
            limit = int(request.url.params["limit"])
            content = submissions[offset : offset + limit]
        elif path == "/form/42/questions":
            content = QUESTIONS
        elif path == "/form/42":
            content = {"id": "42", "title": "Contact [bold]Us", "status": "ENABLED"}
        elif path == "/user/forms":
            content = [{"id": "42", "title": "Contact [bold]Us", "status": "ENABLED"}]
        else:
            return httpx.Response(404, json={"responseCode": 404, "message": "Not found"})
        return httpx.Response(200, json={"responseCode": 200, "message": "success", "content": content})

    return httpx.MockTransport(handler)


@pytest.mark.parametrize("shape", [dict, list])
def test_get_form_accepts_object_or_list_content(shape):
    form = {"id": "42", "title": "Contact Us"}
    content = form if shape is dict else [form]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"responseCode": 200, "message": "success", "content": content})

    with JotformClient("k", transport=httpx.MockTransport(handler)) as client:
        assert client.get_form("42") == form


def test_iter_submissions_paginates_and_dedupes():
    # 5 unique submissions plus a repeat of "s2" that straddles the page boundary.
    data = [make_submission(f"s{i}", f"2026-10-0{i} 00:00:00") for i in range(1, 6)]
    data.insert(2, data[1])
    requests: list[httpx.Request] = []
    with JotformClient("k", transport=api_transport(data, requests=requests)) as client:
        ids = [s["id"] for s in client.iter_submissions("42", page_size=2)]
    assert ids == ["s1", "s2", "s3", "s4", "s5"]
    assert [r.url.params["offset"] for r in requests] == [
        "0",
        "2",
        "4",
        "6",
    ]  # last page empty: 6 items fill 3 pages


@pytest.fixture
def mock_api(monkeypatch):
    transport = api_transport(SUBMISSIONS)
    monkeypatch.setattr(
        cli, "JotformClient", lambda key, region: JotformClient(key, region, transport=transport)
    )


runner = CliRunner()


def test_cli_summary_excludes_deleted_by_default(mock_api):
    result = runner.invoke(cli.app, ["--api-key", "k", "describe", "submissions", "--form", "42"])
    assert result.exit_code == 0, result.output
    assert "Contact [bold]Us (42)" in result.output  # titles are not parsed as Rich markup
    assert "Submissions  2" in result.output
    assert "By status" not in result.output  # only shown when something isn't ACTIVE
    assert "2026-09" in result.output


def test_cli_summary_include_deleted(mock_api):
    result = runner.invoke(
        cli.app, ["--api-key", "k", "describe", "submissions", "--form", "42", "--include-deleted"]
    )
    assert result.exit_code == 0, result.output
    assert "Submissions  3" in result.output
    assert "ACTIVE: 2, DELETED: 1" in result.output


def test_cli_get_submissions_csv(mock_api):
    result = runner.invoke(cli.app, ["--api-key", "k", "get", "submissions", "--form", "42", "-o", "csv"])
    assert result.exit_code == 0, result.output
    rows = list(csv.reader(result.stdout.splitlines()))
    assert rows[0][:3] == ["Submission ID", "Submitted at", "Status"]
    assert [r[0] for r in rows] == ["Submission ID", "101", "102"]  # deleted ones left out
    assert len(rows[0]) > 3  # every answer column


def test_cli_get_submissions_normalized_and_deleted(mock_api):
    args = ["--api-key", "k", "get", "submissions", "--form", "42", "-o", "csv", "--normalize-headers"]
    result = runner.invoke(cli.app, [*args, "--include-deleted"])
    assert result.exit_code == 0, result.output
    rows = list(csv.reader(result.stdout.splitlines()))
    assert rows[0][:3] == ["submission_id", "submitted_at", "status"]
    assert len(rows) == 4


@pytest.mark.parametrize(
    ("output", "expected"), [("name", "101\n102\n"), ("table", "Submissions on form 42 (2)")]
)
def test_cli_get_submissions_other_outputs(mock_api, output, expected):
    result = runner.invoke(
        cli.app,
        ["--api-key", "k", "get", "submissions", "--form", "42", "-o", output],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.output
    assert expected in result.stdout


def test_cli_describe_submissions_json(mock_api):
    result = runner.invoke(
        cli.app, ["--api-key", "k", "describe", "submissions", "--form", "42", "-o", "json"]
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert (data["id"], data["total"], data["byStatus"]) == ("42", 2, {"ACTIVE": 2})


def test_cli_summary_by_title(mock_api):
    result = runner.invoke(
        cli.app, ["--api-key", "k", "describe", "submissions", "--form", "contact [bold]us"]
    )
    assert result.exit_code == 0, result.output
    assert "Contact [bold]Us (42)" in result.output
    assert "Submissions  2" in result.output


def test_cli_summary_unknown_title(mock_api):
    result = runner.invoke(cli.app, ["--api-key", "k", "describe", "submissions", "--form", "Nope"])
    assert result.exit_code == 1
    assert 'No form titled "Nope"' in result.output


def test_cli_summary_unknown_form(mock_api):
    result = runner.invoke(cli.app, ["--api-key", "k", "describe", "submissions", "--form", "999"])
    assert result.exit_code == 1
    assert "Not found" in result.output
