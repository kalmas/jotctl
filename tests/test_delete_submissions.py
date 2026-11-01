import json

import httpx
import pytest
from typer.testing import CliRunner

from jotctl import cli
from jotctl.client import JotformClient


def make_state(*, active=3, trashed=1, purges=True, limit_left=1000):
    subs = {
        str(i): {"id": str(i), "status": "ACTIVE", "created_at": f"2026-10-{20 + i} 10:00:00", "answers": {}}
        for i in range(active)
    }
    for i in range(active, active + trashed):
        subs[str(i)] = {"id": str(i), "status": "DELETED", "created_at": f"2026-10-{20 + i} 10:00:00"}
    return {"subs": subs, "purges": purges, "limit_left": limit_left, "deletes": []}


@pytest.fixture
def api(monkeypatch):
    """Mock Jotform: DELETE trashes an active submission, and purges a trashed one if `purges`."""
    state = make_state()

    def handler(request: httpx.Request) -> httpx.Response:
        path, subs = request.url.path, state["subs"]
        if path == "/form/42":
            content = {"id": "42", "title": "Shopping"}
        elif path == "/form/42/submissions":
            offset, limit = int(request.url.params["offset"]), int(request.url.params["limit"])
            content = [dict(s) for s in subs.values()][offset : offset + limit]
        elif request.method == "DELETE" and path.startswith("/submission/"):
            sid = path.rsplit("/", 1)[1]
            state["deletes"].append(sid)
            if subs[sid]["status"] != "DELETED":
                subs[sid]["status"] = "DELETED"
            elif state["purges"]:
                del subs[sid]
            content = f"Submission #{sid} deleted successfully."
        else:
            return httpx.Response(404, json={"responseCode": 404, "message": "Not found"})
        body = {
            "responseCode": 200,
            "message": "success",
            "content": content,
            "limit-left": state["limit_left"],
        }
        return httpx.Response(200, json=body)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        cli, "JotformClient", lambda key, region: JotformClient(key, region, transport=transport)
    )
    return state


runner = CliRunner()


def delete(tmp_path, *args, input=None):
    return runner.invoke(
        cli.app,
        ["--api-key", "k", "submissions", "delete", "42", "--backup-dir", str(tmp_path / "b"), *args],
        input=input,
        env={"COLUMNS": "200"},
    )


def statuses(state):
    return {sid: s["status"] for sid, s in state["subs"].items()}


def test_wrong_confirmation_deletes_nothing(api, tmp_path):
    result = delete(tmp_path, input="4\n")
    assert result.exit_code == 0, result.output
    assert "3 → moved to the trash" in result.output
    assert "Type the form ID (42) to confirm" in result.output
    assert "No submissions deleted." in result.output
    assert not api["deletes"]
    assert not (tmp_path / "b").exists()


def test_delete_moves_active_to_trash(api, tmp_path):
    result = delete(tmp_path, input="42\n")
    assert result.exit_code == 0, result.output
    assert "left as is (use --purge to remove)" in result.output
    assert api["deletes"] == ["0", "1", "2"]  # the already-trashed one is not touched
    assert statuses(api) == {"0": "DELETED", "1": "DELETED", "2": "DELETED", "3": "DELETED"}
    (backup,) = (tmp_path / "b").iterdir()
    assert backup.name.startswith("42-submissions-")
    assert [s["id"] for s in json.loads(backup.read_text())] == ["0", "1", "2"]
    assert "Moved 3 submission(s) to the trash." in result.output


def test_purge_removes_everything(api, tmp_path):
    result = delete(tmp_path, "--purge", input="42\n")
    assert result.exit_code == 0, result.output
    assert "1 → permanently deleted" in result.output
    assert "This will permanently delete 4 submission(s)" in result.output
    assert api["subs"] == {}
    (backup,) = (tmp_path / "b").iterdir()
    assert sorted(s["id"] for s in json.loads(backup.read_text())) == ["0", "1", "2", "3"]
    assert "Removed 4 submission(s) for good; the form's trash is empty." in result.output


def test_purge_reports_submissions_left_in_trash(api, tmp_path):
    api["purges"] = False
    result = delete(tmp_path, "--purge", "--yes")
    assert result.exit_code == 1
    assert "4 submission(s) are still in the trash" in result.stderr


def test_yes_skips_prompt(api, tmp_path):
    result = delete(tmp_path, "--yes")
    assert result.exit_code == 0, result.output
    assert "Type the form ID" not in result.output
    assert len(api["deletes"]) == 3


def test_not_enough_api_requests(api, tmp_path):
    api["limit_left"] = 6  # --purge needs 3 + 4 = 7
    result = delete(tmp_path, "--purge", "--yes")
    assert result.exit_code == 1
    assert "needs 7 API requests but only 6 are left" in result.stderr
    assert not api["deletes"]
    assert not (tmp_path / "b").exists()


def test_backup_failure_deletes_nothing(api, tmp_path):
    (tmp_path / "b").write_text("")  # a file where the backup directory's parent should be
    result = runner.invoke(
        cli.app,
        [
            "--api-key",
            "k",
            "submissions",
            "delete",
            "42",
            "--yes",
            "--backup-dir",
            str(tmp_path / "b" / "sub"),
        ],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 1
    assert "couldn't save a backup, so nothing was deleted" in result.stderr
    assert not api["deletes"]


def test_nothing_to_delete(api, tmp_path):
    api["subs"] = make_state(active=0, trashed=2)["subs"]
    result = delete(tmp_path, "--yes")
    assert result.exit_code == 0, result.output
    assert "Nothing to delete on Shopping (42)." in result.output
    assert not api["deletes"]


def test_read_only_key_explains_permission(api, tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            body = {"responseCode": 401, "message": "You're not authorized to use (/submission-id)"}
            return httpx.Response(401, json=body)
        content = (
            {"id": "42", "title": "Shopping"}
            if request.url.path == "/form/42"
            else [{"id": "0", "status": "ACTIVE", "created_at": "2026-10-20 10:00:00"}]
        )
        return httpx.Response(200, json={"responseCode": 200, "message": "success", "content": content})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        cli, "JotformClient", lambda key, region: JotformClient(key, region, transport=transport)
    )
    result = runner.invoke(
        cli.app,
        ["--api-key", "k", "submissions", "delete", "42", "--yes", "--backup-dir", str(tmp_path / "b")],
        env={"COLUMNS": "400"},
    )
    assert result.exit_code == 1
    assert "needs an API key with Full Access" in result.stderr
    assert "Before the error" not in result.stderr  # nothing was deleted
