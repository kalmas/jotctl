import csv
import json
from pathlib import Path

import httpx
import pytest
import yaml
from typer.testing import CliRunner

from jotctl import cli, templates
from jotctl.client import JotformClient
from jotctl.reports import Report, ReportError, format_value, load_report, render_query, run_query

HEADERS = ["submission_id", "submitted_at", "status", "your_name", "folks", "opt_in_answer", "email_address"]


def write_export(path: Path, rows: list[list[str]], headers: list[str] = HEADERS) -> Path:
    # utf-8-sig, like `submissions summary --csv`.
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(rows)
    return path


@pytest.fixture
def export(tmp_path):
    return write_export(
        tmp_path / "export.csv",
        [
            ["1", "2026-10-24 10:00:00", "ACTIVE", "Ada", "2", "Yes!", "ada@example.com"],
            ["2", "2026-10-24 11:00:00", "ACTIVE", "Grace", "4", "Not right now", ""],
        ],
    )


def test_load_report(tmp_path):
    path = tmp_path / "r.yaml"
    path.write_text("form: 42\nquery: SELECT 1\n")
    assert load_report(path) == Report("42", "SELECT 1")


@pytest.mark.parametrize(
    "text",
    [
        "form: F\n",
        "query: SELECT 1\n",
        "form: F\nquery: SELECT 1\nextra: 1\n",
        "form: F\nquery: [1]\n",
        "[\n",
    ],
)
def test_load_report_errors(tmp_path, text):
    path = tmp_path / "r.yaml"
    path.write_text(text)
    with pytest.raises(ReportError):
        load_report(path)


def test_render_query_templates():
    saturday = templates.render("{{ saturday | date('%m/%d/%Y') }}")
    assert (
        render_query(Report("F", "WHERE d LIKE '{{ saturday | date('%m/%d/%Y') }}%'"))
        == f"WHERE d LIKE '{saturday}%'"
    )
    with pytest.raises(ReportError, match="query template failed"):
        render_query(Report("F", "{{ nope }}"))


def test_run_query_detects_types_and_nulls(export):
    result = run_query(
        "SELECT your_name, folks, opt_in_answer = 'Yes!' AS opt_in, NULLIF(email_address, '') AS email "
        "FROM submissions ORDER BY folks",
        export,
    )
    assert result.columns == ["your_name", "folks", "opt_in", "email"]
    assert result.rows == [("Ada", 2, True, "ada@example.com"), ("Grace", 4, False, None)]


def test_run_query_errors(export, tmp_path):
    with pytest.raises(ReportError, match="query failed"):
        run_query("SELECT nope FROM submissions", export)
    with pytest.raises(ReportError, match="query failed"):
        run_query("SELECT 1 FROM submissions", tmp_path / "missing.csv")


def test_run_query_path_with_quote(tmp_path):
    path = write_export(
        tmp_path / "it's.csv", [["1", "2026-10-24 10:00:00", "ACTIVE", "Ada", "2", "Yes!", ""]]
    )
    assert run_query("SELECT count(*) FROM submissions", path).rows == [(1,)]


def test_format_value():
    assert [format_value(v) for v in [None, True, False, 3, "x"]] == ["", "true", "false", "3", "x"]


def test_example_report_runs(tmp_path):
    """reports-example/ is documentation; make sure its report still loads and runs."""
    report = load_report(Path(__file__).parent.parent / "reports-example" / "appointments.yaml")
    saturday = templates.render("{{ saturday | date('%m/%d/%Y') }}")
    headers = ["submission_id", "submitted_at", "status", "your_name", "phone_number", "email_address",
               "can_we_contact_you", "appointment_time"]  # fmt: skip
    export = write_export(
        tmp_path / "export.csv",
        [
            ["1", "2026-10-24 10:00:00", "ACTIVE", "Ada", "555-0100", "", "Yes", f"{saturday} 2:30 PM"],
            ["2", "2026-10-24 11:00:00", "ACTIVE", "Grace", "555-0101", "", "No", f"{saturday} 10:00 AM"],
            ["3", "2026-10-24 12:00:00", "ACTIVE", "Alan", "555-0102", "", "Yes", "01/01/2020 10:00 AM"],
        ],
        headers,
    )
    result = run_query(render_query(report), export)
    assert result.columns == ["name", "phone_number", "email_address", "opt_in", "appointment"]
    assert [(r[0], r[3]) for r in result.rows] == [("Grace", False), ("Ada", True)]  # Saturday only, by time
    assert result.rows[1][4].endswith("at 2:30 PM")


runner = CliRunner()

REPORT = "form: Contact Us\nquery: |\n  SELECT your_name, status FROM submissions ORDER BY submission_id\n"


def test_cli_from_csv_to_csv(export, tmp_path):
    report = tmp_path / "r.yaml"
    report.write_text(
        "form: unused\nquery: SELECT your_name, NULLIF(email_address, '') AS email FROM submissions\n"
    )
    result = runner.invoke(cli.app, ["report", "-f", str(report), "--from-csv", str(export), "-o", "csv"])
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == ["your_name,email", "Ada,ada@example.com", "Grace,"]


@pytest.mark.parametrize(("output", "parse"), [("json", json.loads), ("yaml", yaml.safe_load)])
def test_cli_json_and_yaml(export, tmp_path, output, parse):
    report = tmp_path / "r.yaml"
    report.write_text(
        "form: F\nquery: SELECT your_name, folks, NULLIF(email_address, '') AS email FROM submissions\n"
    )
    result = runner.invoke(cli.app, ["report", "-f", str(report), "--from-csv", str(export), "-o", output])
    assert result.exit_code == 0, result.output
    assert parse(result.stdout) == [
        {"your_name": "Ada", "folks": 2, "email": "ada@example.com"},
        {"your_name": "Grace", "folks": 4, "email": None},
    ]


def test_cli_rejects_name_output(export, tmp_path):
    report = tmp_path / "r.yaml"
    report.write_text("form: F\nquery: SELECT 1\n")
    result = runner.invoke(cli.app, ["report", "-f", str(report), "--from-csv", str(export), "-o", "name"])
    assert result.exit_code == 2
    assert "isn't supported here" in result.output


def test_cli_query_error(export, tmp_path):
    report = tmp_path / "r.yaml"
    report.write_text("form: F\nquery: SELECT nope FROM submissions\n")
    result = runner.invoke(cli.app, ["report", "-f", str(report), "--from-csv", str(export)])
    assert result.exit_code == 1
    assert "query failed" in result.stderr


QUESTIONS = {
    "3": {"qid": "3", "name": "yourName", "text": "Your name", "type": "control_textbox", "order": "1"}
}
SUBMISSIONS = [
    {"id": "1", "created_at": "2026-10-24 10:00:00", "status": "ACTIVE", "answers": {"3": {"answer": "Ada"}}},
    {
        "id": "2",
        "created_at": "2026-10-24 11:00:00",
        "status": "DELETED",
        "answers": {"3": {"answer": "Old"}},
    },
]


@pytest.fixture
def mock_api(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        content = {
            "/user/forms": [{"id": "42", "title": "Contact Us", "status": "ENABLED"}],
            "/form/42/questions": QUESTIONS,
            "/form/42/submissions": SUBMISSIONS,
        }.get(request.url.path)
        if content is None:
            return httpx.Response(404, json={"responseCode": 404, "message": "Not found"})
        return httpx.Response(200, json={"responseCode": 200, "message": "success", "content": content})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        cli, "JotformClient", lambda key, region: JotformClient(key, region, transport=transport)
    )


@pytest.mark.parametrize(("args", "names"), [([], ["Ada"]), (["--include-deleted"], ["Ada", "Old"])])
def test_cli_fetches_submissions(mock_api, tmp_path, args, names):
    report = tmp_path / "r.yaml"
    report.write_text(REPORT)
    result = runner.invoke(cli.app, ["--api-key", "k", "report", "-f", str(report), "-o", "csv", *args])
    assert result.exit_code == 0, result.output
    rows = list(csv.reader(result.stdout.splitlines()))
    assert rows[0] == ["your_name", "status"]
    assert [r[0] for r in rows[1:]] == names


def test_cli_table_output(mock_api, tmp_path):
    report = tmp_path / "r.yaml"
    report.write_text(REPORT)
    result = runner.invoke(cli.app, ["--api-key", "k", "report", "-f", str(report)], env={"COLUMNS": "120"})
    assert result.exit_code == 0, result.output
    assert "r.yaml (1)" in result.output
    assert "Ada" in result.output and "ACTIVE" in result.output
