import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from typer.testing import CliRunner

from jotctl import cli
from jotctl.client import JotformClient
from jotctl.manifests import (
    Change,
    ConditionChange,
    FormManifest,
    ManifestError,
    PropertyChange,
    apply_condition_changes,
    load_manifest,
    plan_changes,
    plan_condition_changes,
    plan_property_changes,
    resolve_value,
    unapplied_condition_changes,
    unapplied_property_changes,
)

QUESTIONS = {
    "3": {
        "qid": "3",
        "name": "whatDate13",
        "type": "control_datetime",
        "order": "1",
        "startDate": "10/26/2026",
    },
    "5": {"qid": "5", "name": "thisSaturday", "type": "control_datetime", "order": "2", "required": "No"},
}

MANIFEST = """\
forms:
  - Contact Us:
      questions:
        whatDate13:
          startDate:
            script: |
              # comment lines are fine
              echo 11/02/2026
        thisSaturday:
          required: true
          defaultDate: custom_2026-10-31
"""


@pytest.fixture
def manifest_file(tmp_path):
    path = tmp_path / "manifest.yaml"
    path.write_text(MANIFEST)
    return path


def test_load_manifest(manifest_file):
    (manifest,) = load_manifest(manifest_file)
    assert manifest.form == "Contact Us"
    assert manifest.questions["thisSaturday"] == {"required": True, "defaultDate": "custom_2026-10-31"}


def test_load_manifest_accepts_forms_mapping(tmp_path):
    path = tmp_path / "manifest.yaml"
    path.write_text("forms:\n  '42':\n    questions:\n      a: {required: false}\n")
    assert load_manifest(path) == [FormManifest("42", {"a": {"required": False}})]


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("forms: [\n", "not valid YAML"),
        ("questions: {}\n", "top-level `forms` list"),
        ("forms:\n  - A: {}\n    B: {}\n", "each item under `forms`"),
        ("forms:\n  - A: {questions: {x: 1}}\n", "form 'A' needs `questions:"),
    ],
)
def test_load_manifest_errors(tmp_path, text, message):
    path = tmp_path / "manifest.yaml"
    path.write_text(text)
    with pytest.raises(ManifestError, match=message):
        load_manifest(path)


def test_resolve_value():
    assert resolve_value({"script": "echo hi\n"}) == "hi"
    assert resolve_value({"script": "printf 'a\\nb\\n'"}) == "a\nb"
    assert resolve_value(True) == "Yes"
    assert resolve_value(False) == "No"
    assert resolve_value(3) == "3"
    assert resolve_value({"template": "{{ 'a' ~ 'b' }}"}) == "ab"


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ({"script": "echo oops >&2; exit 3"}, r"exit 3\): oops"),
        ({"script": "true"}, "printed nothing"),
        ({"script": "x", "other": 1}, r"must be `\{template: ...\}` or `\{script: ...\}`"),
        ({"template": "{{ nope }}"}, "template failed: 'nope' is undefined"),
        ({"template": "{{ monday.__class__ }}"}, "template failed: access to attribute '__class__'"),
        ({"template": "{% if false %}x{% endif %}"}, "template produced nothing"),
        ({"template": "{{ saturday.at('25:00') }}"}, "template failed: time data '25:00'"),
        (None, "unsupported value"),
        ([1], "unsupported value"),
    ],
)
def test_resolve_value_errors(value, message):
    with pytest.raises(ManifestError, match=message):
        resolve_value(value)


def test_plan_changes_skips_unchanged_and_reports_unset(manifest_file):
    (manifest,) = load_manifest(manifest_file)
    questions = {**QUESTIONS, "5": {**QUESTIONS["5"], "required": "Yes"}}
    assert plan_changes(manifest, questions) == [
        Change("whatDate13", "3", "startDate", "10/26/2026", "11/02/2026"),
        Change("thisSaturday", "5", "defaultDate", None, "custom_2026-10-31"),
    ]


def test_plan_changes_matches_qid():
    assert plan_changes(FormManifest("F", {"5": {"required": True}}), QUESTIONS) == [
        Change("5", "5", "required", "No", "Yes")
    ]


def test_plan_changes_unknown_question():
    with pytest.raises(ManifestError, match="no question named 'nope'"):
        plan_changes(FormManifest("F", {"nope": {"required": True}}), QUESTIONS)


def test_plan_changes_names_failing_script():
    with pytest.raises(ManifestError, match=r"F → whatDate13\.startDate: script failed"):
        plan_changes(FormManifest("F", {"whatDate13": {"startDate": {"script": "exit 1"}}}), QUESTIONS)


@pytest.fixture
def mock_api(monkeypatch):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path == "/user/forms":
            content = [{"id": "42", "title": "Contact Us", "status": "ENABLED"}]
        elif path == "/form/42/questions":
            content = QUESTIONS
        elif request.method == "POST" and path.startswith("/form/42/question/"):
            content = {}
        else:
            return httpx.Response(404, json={"responseCode": 404, "message": "Not found"})
        return httpx.Response(200, json={"responseCode": 200, "message": "success", "content": content})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        cli, "JotformClient", lambda key, region: JotformClient(key, region, transport=transport)
    )
    return requests


runner = CliRunner()


def posts(requests):
    return {r.url.path: parse_qs(r.content.decode()) for r in requests if r.method == "POST"}


def test_cli_apply_shows_changes_and_applies_on_confirm(mock_api, manifest_file):
    result = runner.invoke(
        cli.app, ["--api-key", "k", "apply", "-f", str(manifest_file)], input="y\n", env={"COLUMNS": "200"}
    )
    assert result.exit_code == 0, result.output
    assert "Changes from manifest.yaml (3)" in result.output
    assert "10/26/2026" in result.output
    assert "11/02/2026" in result.output
    assert "(not set)" in result.output
    assert "Apply 3 change(s)?" in result.output
    assert posts(mock_api) == {
        "/form/42/question/3": {"question[startDate]": ["11/02/2026"]},
        "/form/42/question/5": {
            "question[required]": ["Yes"],
            "question[defaultDate]": ["custom_2026-10-31"],
        },
    }
    assert "Applied 3 change(s)." in result.output


def test_cli_apply_declined_sends_nothing(mock_api, manifest_file):
    result = runner.invoke(cli.app, ["--api-key", "k", "apply", "-f", str(manifest_file)], input="n\n")
    assert result.exit_code == 0, result.output
    assert "No changes made." in result.output
    assert not posts(mock_api)


def test_cli_apply_yes_skips_prompt(mock_api, manifest_file):
    result = runner.invoke(cli.app, ["--api-key", "k", "apply", "-f", str(manifest_file), "--yes"])
    assert result.exit_code == 0, result.output
    assert "Apply" not in result.output
    assert len(posts(mock_api)) == 2


def test_cli_apply_nothing_to_change(mock_api, tmp_path):
    path = tmp_path / "manifest.yaml"
    path.write_text("forms:\n  - Contact Us:\n      questions:\n        thisSaturday: {required: false}\n")
    result = runner.invoke(cli.app, ["--api-key", "k", "apply", "-f", str(path)])
    assert result.exit_code == 0, result.output
    assert "No changes" in result.output
    assert not posts(mock_api)


def test_cli_apply_unknown_question_sends_nothing(mock_api, tmp_path):
    path = tmp_path / "manifest.yaml"
    path.write_text("forms:\n  - Contact Us:\n      questions:\n        nope: {required: true}\n")
    result = runner.invoke(cli.app, ["--api-key", "k", "apply", "-f", str(path)], input="y\n")
    assert result.exit_code == 1
    assert "no question named 'nope'" in result.stderr
    assert not posts(mock_api)


CONDITIONS = [
    {
        "id": "100",
        "type": "field",
        "link": "Any",
        "priority": "0",
        "terms": json.dumps(
            [
                {"id": "t0", "field": "45", "operator": "before", "value": "10:00 AM"},
                {"id": "t1", "field": "44", "operator": "before", "value": "2026-10-24"},
            ]
        ),
        "action": json.dumps([{"visibility": "Show", "field": "46"}]),
    },
    {
        "id": "200",
        "type": "calculation",
        "link": "Any",
        "priority": "1",
        "terms": json.dumps([{"field": "language", "operator": "equals", "value": "Español"}]),
        "action": "[]",
    },
]
COND_QUESTIONS = {
    "44": {"qid": "44", "name": "date", "order": "1"},
    "45": {"qid": "45", "name": "time", "order": "2"},
}


def test_load_manifest_conditions_only(tmp_path):
    path = tmp_path / "manifest.yaml"
    path.write_text(
        "forms:\n  - F:\n      conditions:\n        100:\n          terms:\n            date: {value: x}\n"
    )
    assert load_manifest(path) == [FormManifest("F", {}, {"100": {"date": {"value": "x"}}})]


@pytest.mark.parametrize(
    "settings",
    ["{}", "{other: {}}", "{conditions: {100: {terms: {date: x}}}}", "{conditions: {100: {action: {}}}}"],
)
def test_load_manifest_bad_conditions(tmp_path, settings):
    path = tmp_path / "manifest.yaml"
    path.write_text(f"forms:\n  - F: {settings}\n")
    with pytest.raises(ManifestError, match="form 'F' needs"):
        load_manifest(path)


def test_plan_condition_changes_by_name_qid_and_reference():
    manifest = FormManifest(
        "F",
        conditions={
            "100": {"date": {"value": {"script": "echo 2026-10-31"}}, "45": {"value": "10:00 AM"}},
            "200": {"language": {"value": "Spanish"}},
        },
    )
    assert plan_condition_changes(manifest, CONDITIONS, COND_QUESTIONS) == [
        ConditionChange("100", "date", 1, "value", "2026-10-24", "2026-10-31"),  # time is unchanged
        ConditionChange("200", "language", 0, "value", "Español", "Spanish"),
    ]


@pytest.mark.parametrize(
    ("conditions", "message"),
    [
        ({"999": {"date": {"value": "x"}}}, "has no condition 999"),
        ({"100": {"phone": {"value": "x"}}}, "no term tests 'phone'"),
        (
            {"100": {"date": {"value": {"script": "exit 2"}}}},
            r"condition 100, term date\.value: script failed",
        ),
    ],
)
def test_plan_condition_changes_errors(conditions, message):
    with pytest.raises(ManifestError, match=message):
        plan_condition_changes(FormManifest("F", conditions=conditions), CONDITIONS, COND_QUESTIONS)


def test_plan_condition_changes_ambiguous_term():
    doubled = [{**CONDITIONS[0], "terms": json.dumps([{"field": "44"}, {"field": "44"}])}]
    with pytest.raises(ManifestError, match="2 terms test 'date'"):
        plan_condition_changes(
            FormManifest("F", conditions={"100": {"date": {"value": "x"}}}), doubled, COND_QUESTIONS
        )


def test_apply_condition_changes_only_touches_changed_terms():
    change = ConditionChange("100", "date", 1, "value", "2026-10-24", "2026-10-31")
    updated = apply_condition_changes(CONDITIONS, [change])
    assert updated[1] == CONDITIONS[1]  # untouched conditions are sent back exactly as read
    assert {k: v for k, v in updated[0].items() if k != "terms"} == {
        k: v for k, v in CONDITIONS[0].items() if k != "terms"
    }
    terms = json.loads(updated[0]["terms"])
    assert terms[0] == json.loads(CONDITIONS[0]["terms"])[0]
    assert terms[1]["value"] == "2026-10-31"
    assert json.loads(CONDITIONS[0]["terms"])[1]["value"] == "2026-10-24"  # input not mutated


def test_unapplied_condition_changes():
    change = ConditionChange("100", "date", 1, "value", "2026-10-24", "2026-10-31")
    assert unapplied_condition_changes(apply_condition_changes(CONDITIONS, [change]), [change]) == []
    assert unapplied_condition_changes(CONDITIONS, [change]) == [change]
    assert unapplied_condition_changes([], [change]) == [change]


@pytest.fixture
def cond_api(monkeypatch):
    """Mock Jotform that stores conditions sent with PUT; `state["ignore_put"]` drops them."""
    state = {"conditions": CONDITIONS, "ignore_put": False, "requests": []}

    def handler(request: httpx.Request) -> httpx.Response:
        state["requests"].append(request)
        path = request.url.path
        if path == "/form/42/questions":
            content = COND_QUESTIONS
        elif path == "/form/42/properties" and request.method == "GET":
            content = {"conditions": state["conditions"]}
        elif path == "/form/42/properties" and request.method == "PUT":
            if not state["ignore_put"]:
                state["conditions"] = json.loads(request.content)["properties"]["conditions"]
            content = {}
        else:
            return httpx.Response(404, json={"responseCode": 404, "message": "Not found"})
        return httpx.Response(200, json={"responseCode": 200, "message": "success", "content": content})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        cli, "JotformClient", lambda key, region: JotformClient(key, region, transport=transport)
    )
    return state


COND_MANIFEST = "forms:\n  - '42':\n      conditions:\n        100:\n          terms:\n            date: {value: 2026-10-31}\n"


def test_cli_apply_conditions(cond_api, tmp_path):
    path = tmp_path / "manifest.yaml"
    path.write_text(COND_MANIFEST)
    backups = tmp_path / "backups"
    result = runner.invoke(
        cli.app,
        ["--api-key", "k", "apply", "-f", str(path), "--backup-dir", str(backups)],
        input="y\n",
        env={"COLUMNS": "300"},
    )
    assert result.exit_code == 0, result.output
    (backup,) = backups.iterdir()
    assert backup.name.startswith("42-conditions-")
    assert json.loads(backup.read_text()) == CONDITIONS  # the originals, exactly as read
    assert f"Backed up the conditions on 42 to {backup}" in result.output
    assert "condition 100, term date" in result.output
    assert "2026-10-24" in result.output
    assert "2026-10-31" in result.output
    assert "Applied 1 change(s)." in result.output
    (put,) = [r for r in cond_api["requests"] if r.method == "PUT"]
    sent = json.loads(put.content)["properties"]["conditions"]
    assert len(sent) == 2
    assert sent[1] == CONDITIONS[1]
    assert json.loads(sent[0]["terms"])[1]["value"] == "2026-10-31"


def test_cli_apply_conditions_declined(cond_api, tmp_path):
    path = tmp_path / "manifest.yaml"
    path.write_text(COND_MANIFEST)
    backups = tmp_path / "backups"
    result = runner.invoke(
        cli.app, ["--api-key", "k", "apply", "-f", str(path), "--backup-dir", str(backups)], input="n\n"
    )
    assert result.exit_code == 0, result.output
    assert not [r for r in cond_api["requests"] if r.method == "PUT"]
    assert not backups.exists()


def test_cli_apply_conditions_not_reflected(cond_api, tmp_path):
    cond_api["ignore_put"] = True
    path = tmp_path / "manifest.yaml"
    path.write_text(COND_MANIFEST)
    result = runner.invoke(
        cli.app,
        ["--api-key", "k", "apply", "-f", str(path), "--yes", "--backup-dir", str(tmp_path / "b")],
        env={"COLUMNS": "300"},
    )
    assert result.exit_code == 1
    assert "don't match what was sent" in result.stderr
    assert f"originalsaresavedin{tmp_path / 'b'}" in "".join(result.stderr.split())  # ignore wrapping
    assert "1 change(s) not reflected" in result.stderr


def test_cli_apply_conditions_backup_failure_sends_nothing(cond_api, tmp_path):
    path = tmp_path / "manifest.yaml"
    path.write_text(COND_MANIFEST)
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    result = runner.invoke(
        cli.app,
        ["--api-key", "k", "apply", "-f", str(path), "--yes", "--backup-dir", str(blocker / "sub")],
        env={"COLUMNS": "300"},
    )
    assert result.exit_code == 1
    assert "couldn't back up the conditions on 42, so they weren't changed" in result.stderr
    assert not [r for r in cond_api["requests"] if r.method == "PUT"]


def test_load_manifest_properties(tmp_path):
    path = tmp_path / "manifest.yaml"
    path.write_text(
        "forms:\n  - F:\n      properties:\n        status: ENABLED\n        limitSubmission: 84\n"
    )
    assert load_manifest(path) == [FormManifest("F", properties={"status": "ENABLED", "limitSubmission": 84})]


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        ("{properties: [status]}", "needs `properties:"),
        ("{properties: {conditions: x}}", "can't set conditions under `properties:`"),
    ],
)
def test_load_manifest_bad_properties(tmp_path, settings, message):
    path = tmp_path / "manifest.yaml"
    path.write_text(f"forms:\n  - F: {settings}\n")
    with pytest.raises(ManifestError, match=message):
        load_manifest(path)


def test_plan_property_changes():
    manifest = FormManifest(
        "F",
        properties={
            "status": "ENABLED",
            "limitSubmission": 84,  # unchanged: numbers compare as Jotform's strings
            "messageOfLimitedForm": {"script": "echo Opens Oct 31"},
            "newThing": True,
        },
    )
    current = {"status": "DISABLED", "limitSubmission": "84", "messageOfLimitedForm": "Opens Sep 7"}
    assert plan_property_changes(manifest, current) == [
        PropertyChange("status", "DISABLED", "ENABLED"),
        PropertyChange("messageOfLimitedForm", "Opens Sep 7", "Opens Oct 31"),
        PropertyChange("newThing", None, "Yes"),
    ]


def test_plan_property_changes_names_failing_script():
    with pytest.raises(ManifestError, match=r"F → property status: script failed"):
        plan_property_changes(FormManifest("F", properties={"status": {"script": "exit 1"}}), {})


def test_unapplied_property_changes():
    change = PropertyChange("status", "DISABLED", "ENABLED")
    assert unapplied_property_changes({"status": "ENABLED"}, [change]) == []
    assert unapplied_property_changes({"status": "DISABLED"}, [change]) == [change]
    assert unapplied_property_changes({}, [change]) == [change]


@pytest.fixture
def props_api(monkeypatch):
    """Mock Jotform that stores properties sent with PUT; `state["ignore"]` lists keys it drops."""
    state = {
        "properties": {
            "status": "DISABLED",
            "messageOfLimitedForm": "Opens Sep 7",
            "passwordProtectPass": "old-secret",
            "conditions": CONDITIONS,
        },
        "ignore": set(),
        "requests": [],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        state["requests"].append(request)
        path = request.url.path
        if path == "/form/42/questions":
            content = COND_QUESTIONS
        elif path == "/form/42/properties" and request.method == "GET":
            content = state["properties"]
        elif path == "/form/42/properties" and request.method == "PUT":
            sent = json.loads(request.content)["properties"]
            state["properties"] = {
                **state["properties"],
                **{k: v for k, v in sent.items() if k not in state["ignore"]},
            }
            content = {}
        else:
            return httpx.Response(404, json={"responseCode": 404, "message": "Not found"})
        return httpx.Response(200, json={"responseCode": 200, "message": "success", "content": content})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        cli, "JotformClient", lambda key, region: JotformClient(key, region, transport=transport)
    )
    return state


PROPS_MANIFEST = """\
forms:
  - '42':
      properties:
        status: ENABLED
        messageOfLimitedForm: Opens Oct 31
"""


def puts(state):
    return [json.loads(r.content)["properties"] for r in state["requests"] if r.method == "PUT"]


def test_cli_apply_properties(props_api, tmp_path):
    path = tmp_path / "manifest.yaml"
    path.write_text(PROPS_MANIFEST)
    result = runner.invoke(
        cli.app, ["--api-key", "k", "apply", "-f", str(path)], input="y\n", env={"COLUMNS": "200"}
    )
    assert result.exit_code == 0, result.output
    assert "Changes from manifest.yaml (2)" in result.output
    assert "DISABLED" in result.output
    assert "Opens Sep 7" in result.output
    assert puts(props_api) == [
        {"status": "ENABLED", "messageOfLimitedForm": "Opens Oct 31"}
    ]  # one PUT, no conditions
    assert "Updated 42 → properties: status, messageOfLimitedForm" in result.output
    assert "Applied 2 change(s)." in result.output


def test_cli_apply_properties_with_conditions(props_api, tmp_path):
    path = tmp_path / "manifest.yaml"
    path.write_text(
        PROPS_MANIFEST
        + "      conditions:\n        100:\n          terms:\n            date: {value: 2026-10-31}\n"
    )
    result = runner.invoke(
        cli.app,
        ["--api-key", "k", "apply", "-f", str(path), "--yes", "--backup-dir", str(tmp_path / "b")],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.output
    props_put, conditions_put = puts(props_api)
    assert set(props_put) == {"status", "messageOfLimitedForm"}
    assert set(conditions_put) == {"conditions"}
    assert "Applied 3 change(s)." in result.output


def test_cli_apply_properties_not_applied(props_api, tmp_path):
    props_api["ignore"] = {"status"}
    path = tmp_path / "manifest.yaml"
    path.write_text(PROPS_MANIFEST)
    result = runner.invoke(
        cli.app, ["--api-key", "k", "apply", "-f", str(path), "--yes"], env={"COLUMNS": "300"}
    )
    assert result.exit_code == 1
    assert "Jotform didn't apply status on 42" in result.stderr


def test_cli_apply_hides_secret_properties(props_api, tmp_path):
    path = tmp_path / "manifest.yaml"
    path.write_text("forms:\n  - '42':\n      properties:\n        passwordProtectPass: new-secret\n")
    result = runner.invoke(
        cli.app, ["--api-key", "k", "apply", "-f", str(path)], input="n\n", env={"COLUMNS": "200"}
    )
    assert result.exit_code == 0, result.output
    assert "passwordProtectPass" in result.output
    assert "(hidden)" in result.output
    assert "old-secret" not in result.output
    assert "new-secret" not in result.output
    assert not puts(props_api)


def test_example_manifest_is_valid():
    """manifests-example/ is documentation; make sure it keeps loading and every value resolves."""
    path = Path(__file__).parent.parent / "manifests-example" / "weekly-reopen.yaml"
    forms = load_manifest(path)
    assert [f.form for f in forms] == ["Weekly Appointment Booking", "251234567890"]
    values = [
        *(v for f in forms for v in f.properties.values()),
        *(v for f in forms for props in f.questions.values() for v in props.values()),
        *(
            v
            for f in forms
            for terms in f.conditions.values()
            for props in terms.values()
            for v in props.values()
        ),
    ]
    assert len(values) == 9
    for value in values:
        assert resolve_value(value)


def test_cli_apply_dry_run(props_api, tmp_path):
    path = tmp_path / "manifest.yaml"
    path.write_text(PROPS_MANIFEST)
    result = runner.invoke(
        cli.app, ["--api-key", "k", "apply", "-f", str(path), "--dry-run"], env={"COLUMNS": "200"}
    )
    assert result.exit_code == 0, result.output
    assert "Changes from manifest.yaml (2)" in result.output
    assert "Dry run: no changes made." in result.output
    assert "Apply 2 change(s)?" not in result.output
    assert not puts(props_api)


@pytest.mark.parametrize(
    ("manifest_text", "exit_code", "expected"),
    [
        (PROPS_MANIFEST, 1, "Changes from manifest.yaml (2)"),
        ("forms:\n  - '42':\n      properties:\n        status: DISABLED\n", 0, "No changes"),
        (
            "forms:\n  - '42':\n      questions:\n        nope: {required: true}\n",
            2,
            "no question named 'nope'",
        ),
    ],
)
def test_cli_diff_exit_codes(props_api, tmp_path, manifest_text, exit_code, expected):
    path = tmp_path / "manifest.yaml"
    path.write_text(manifest_text)
    result = runner.invoke(cli.app, ["--api-key", "k", "diff", "-f", str(path)], env={"COLUMNS": "200"})
    assert result.exit_code == exit_code, result.output
    assert expected in result.output
    assert not puts(props_api)


def test_cli_apply_folder_and_repeated_f(props_api, tmp_path):
    folder = tmp_path / "manifests"
    folder.mkdir()
    (folder / "a.yaml").write_text("forms:\n  - '42':\n      properties:\n        status: ENABLED\n")
    (folder / "b.yml").write_text("forms:\n  - '42':\n      properties:\n        messageOfLimitedForm: Hi\n")
    (folder / "notes.txt").write_text("ignored")
    extra = tmp_path / "c.yaml"
    extra.write_text("forms:\n  - '42':\n      properties:\n        limitSubmission: 5\n")
    result = runner.invoke(
        cli.app, ["--api-key", "k", "diff", "-f", str(folder), "-f", str(extra)], env={"COLUMNS": "200"}
    )
    assert result.exit_code == 1, result.output
    assert "Changes from 3 files (3)" in result.output


def test_cli_apply_empty_folder(props_api, tmp_path):
    result = runner.invoke(cli.app, ["--api-key", "k", "apply", "-f", str(tmp_path)])
    assert result.exit_code == 1
    assert "has no .yaml or .yml files" in result.stderr
