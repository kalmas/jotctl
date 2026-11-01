import json
from datetime import datetime

import httpx
import pytest
from typer.testing import CliRunner

from jotctl import cli
from jotctl.client import JotformClient
from jotctl.emails import backup_emails, body_text, find_email, is_enabled, raw_emails, type_label
from jotctl.manifests import (
    EmailChange,
    FormManifest,
    ManifestError,
    apply_email_changes,
    load_manifest,
    plan_email_changes,
    unapplied_email_changes,
)

EMAILS = [
    {
        "name": "Notification 1",
        "type": "notification",
        "to": "staff@example.com",
        "from": "default|default",
        "replyTo": "{email}",
        "subject": "Re: {form_title}",
        "body": "<p>New submission</p>",
        "disabled": "1",
        "uniqueID": "111",
    },
    {
        "name": "Autoresponder",
        "type": "autorespond",
        "to": "{email}",
        "from": "none|Example Org",
        "replyTo": "help@example.com",
        "subject": "Thanks!",
        "body": "<html><body><p>Hello &amp; welcome,</p><p>see you <b>{apptTime}</b><br>Bye</p></body></html>",
        "disabled": "0",
        "pdfPassword": "hunter2",
        "uniqueID": "222",
    },
]


def test_raw_emails_shapes():
    assert raw_emails(None) == []
    assert raw_emails(EMAILS) == EMAILS
    assert raw_emails(json.dumps(EMAILS)) == EMAILS
    assert raw_emails({"0": EMAILS[0], "1": EMAILS[1]}) == EMAILS


def test_find_email():
    assert find_email(EMAILS, "Autoresponder") == 1
    assert find_email(EMAILS, "111") == 0  # by uniqueID
    with pytest.raises(LookupError, match="no email named 'nope'"):
        find_email(EMAILS, "nope")
    with pytest.raises(LookupError, match="2 emails are named 'X'"):
        find_email([{"name": "X"}, {"name": "X"}], "X")


def test_helpers():
    assert [is_enabled(e) for e in EMAILS] == [False, True]
    assert [type_label(e) for e in EMAILS] == ["notification", "autoresponder"]
    assert body_text(EMAILS[1]["body"]) == "Hello & welcome,\nsee you {apptTime}\nBye"


def test_backup_emails(tmp_path):
    path = backup_emails(EMAILS, "42", tmp_path, now=datetime(2026, 11, 1, 9, 0, 0))
    assert path.name == "42-emails-20261101-090000.json"
    assert json.loads(path.read_text()) == EMAILS


def test_load_manifest_emails(tmp_path):
    path = tmp_path / "m.yaml"
    path.write_text("forms:\n  - F:\n      emails:\n        Autoresponder:\n          subject: Hi\n")
    assert load_manifest(path) == [FormManifest("F", emails={"Autoresponder": {"subject": "Hi"}})]


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        ("{emails: [x]}", "needs `emails:"),
        ("{emails: {A: x}}", "needs `emails:"),
        ("{properties: {emails: []}}", "can't set emails under `properties:`"),
    ],
)
def test_load_manifest_bad_emails(tmp_path, settings, message):
    path = tmp_path / "m.yaml"
    path.write_text(f"forms:\n  - F: {settings}\n")
    with pytest.raises(ManifestError, match=message):
        load_manifest(path)


def test_plan_email_changes():
    manifest = FormManifest(
        "F",
        emails={
            "Autoresponder": {"subject": {"template": "{{ 'Thanks' ~ '!' }}"}, "sendOnEdit": True},
            "111": {"disabled": False},
        },
    )
    assert plan_email_changes(manifest, EMAILS) == [
        EmailChange("Autoresponder", 1, "sendOnEdit", None, "1"),  # subject is unchanged
        EmailChange("111", 0, "disabled", "1", "0"),
    ]


def test_plan_email_changes_errors():
    with pytest.raises(ManifestError, match="no email named 'Nope'"):
        plan_email_changes(FormManifest("F", emails={"Nope": {"subject": "x"}}), EMAILS)
    with pytest.raises(ManifestError, match=r"F → email Autoresponder\.subject: script failed"):
        plan_email_changes(
            FormManifest("F", emails={"Autoresponder": {"subject": {"script": "exit 1"}}}), EMAILS
        )


def test_apply_and_unapplied_email_changes():
    change = EmailChange("Autoresponder", 1, "subject", "Thanks!", "See you")
    updated = apply_email_changes(EMAILS, [change])
    assert updated[1]["subject"] == "See you"
    assert updated[0] == EMAILS[0]
    assert EMAILS[1]["subject"] == "Thanks!"  # input not mutated
    assert unapplied_email_changes(updated, [change]) == []
    assert unapplied_email_changes(EMAILS, [change]) == [change]
    assert unapplied_email_changes(EMAILS[:1], [change]) == [change]


@pytest.fixture
def api(monkeypatch):
    """Mock Jotform whose form 42 stores `emails` sent with PUT, unless `ignore_put`."""
    state = {"emails": [dict(e) for e in EMAILS], "ignore_put": False, "puts": []}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/form/42/questions":
            content = {}
        elif path == "/form/42/properties" and request.method == "GET":
            content = {"title": "Contact Us", "emails": state["emails"]}
        elif path == "/form/42/properties" and request.method == "PUT":
            sent = json.loads(request.content)["properties"]
            state["puts"].append(sent)
            if not state["ignore_put"]:
                state["emails"] = sent["emails"]
            content = {}
        else:
            return httpx.Response(404, json={"responseCode": 404, "message": "Not found"})
        return httpx.Response(200, json={"responseCode": 200, "message": "success", "content": content})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        cli, "JotformClient", lambda key, region: JotformClient(key, region, transport=transport)
    )
    return state


runner = CliRunner()


def run(*args, input=None):
    return runner.invoke(cli.app, ["--api-key", "k", *args], input=input, env={"COLUMNS": "250"})


def test_cli_get_emails(api):
    result = run("get", "emails", "--form", "42")
    assert result.exit_code == 0, result.output
    assert "Emails on form 42 (2)" in result.output
    assert "Notification 1" in result.output
    assert "autoresponder" in result.output
    assert "hunter2" not in result.output
    assert run("get", "email", "--form", "42", "-o", "name").stdout == "Notification 1\nAutoresponder\n"


def test_cli_get_emails_json_redacts_secrets(api):
    data = json.loads(run("get", "emails", "--form", "42", "-o", "json").stdout)
    assert data[1]["pdfPassword"] == "[redacted]"
    assert data[1]["subject"] == "Thanks!"


def test_cli_describe_email(api):
    result = run("describe", "email", "Autoresponder", "--form", "42")
    assert result.exit_code == 0, result.output
    assert "autoresponder" in result.output
    assert "see you {apptTime}" in result.output  # body as plain text
    assert "<p>" not in result.output
    assert "hunter2" not in result.output
    assert (
        json.loads(run("describe", "email", "222", "--form", "42", "-o", "json").stdout)["name"]
        == "Autoresponder"
    )


def test_cli_describe_unknown_email(api):
    result = run("describe", "email", "Nope", "--form", "42")
    assert result.exit_code == 1
    assert "no email named 'Nope'" in result.stderr


MANIFEST = """\
forms:
  - '42':
      emails:
        Autoresponder:
          subject: See you soon
          body: "<p>New body</p>"
        Notification 1:
          disabled: false
"""


def test_cli_apply_emails(api, tmp_path):
    path = tmp_path / "m.yaml"
    path.write_text(MANIFEST)
    backups_dir = tmp_path / "b"
    result = run("apply", "-f", str(path), "--backup-dir", str(backups_dir), input="y\n")
    assert result.exit_code == 0, result.output
    assert "email Autoresponder" in result.output
    assert "Applied 3 change(s)." in result.output
    (sent,) = api["puts"]
    assert set(sent) == {"emails"}
    assert len(sent["emails"]) == 2
    assert sent["emails"][1]["subject"] == "See you soon"
    assert sent["emails"][1]["pdfPassword"] == "hunter2"  # untouched fields are sent back as read
    assert sent["emails"][0]["disabled"] == "0"
    (backup,) = backups_dir.iterdir()
    assert backup.name.startswith("42-emails-")
    assert json.loads(backup.read_text()) == EMAILS


def test_cli_apply_emails_not_reflected(api, tmp_path):
    api["ignore_put"] = True
    path = tmp_path / "m.yaml"
    path.write_text(MANIFEST)
    result = run("apply", "-f", str(path), "--yes", "--backup-dir", str(tmp_path / "b"))
    assert result.exit_code == 1
    assert "emails on 42 don't match what was sent" in result.stderr


def test_cli_diff_shortens_long_values_and_hides_secrets(api, tmp_path):
    long_body = "x" * 500
    path = tmp_path / "m.yaml"
    path.write_text(
        f"forms:\n  - '42':\n      emails:\n        Autoresponder:\n          body: {long_body}\n"
        "          pdfPassword: new-secret\n"
    )
    result = run("diff", "-f", str(path))
    assert result.exit_code == 1, result.output
    assert "(500 characters)" in result.output
    assert "(hidden)" in result.output
    assert "new-secret" not in result.output
    assert "hunter2" not in result.output
    assert not api["puts"]
