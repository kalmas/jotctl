import httpx
import pytest
import typer.main
from typer.testing import CliRunner

from jotctl import cli
from jotctl.client import JotformClient

ROOT = typer.main.get_command(cli.app)


def hoist(*args):
    return cli.hoist_global_options(list(args), ROOT.params)


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["forms", "list", "-vQ"], ["-vQ", "forms", "list"]),
        (["forms", "list", "-v", "--json", "-Q"], ["-v", "-Q", "forms", "list", "--json"]),
        (["forms", "list", "--api-key", "k"], ["--api-key", "k", "forms", "list"]),
        (["forms", "list", "--region=eu", "--verbose"], ["--region=eu", "--verbose", "forms", "list"]),
        # -v is global, -y belongs to `forms apply`.
        (["forms", "apply", "m.yaml", "-vy"], ["-v", "forms", "apply", "m.yaml", "-y"]),
        (["forms", "apply", "m.yaml", "-y"], ["forms", "apply", "m.yaml", "-y"]),
        # Nothing after -- is touched.
        (["forms", "status", "--", "-v"], ["forms", "status", "--", "-v"]),
        # Global options already in front stay there.
        (["-vv", "forms", "list"], ["-vv", "forms", "list"]),
    ],
)
def test_hoist_global_options(args, expected):
    assert hoist(*args) == expected


def test_no_subcommand_option_clashes_with_a_global_one():
    """Hoisting would steal a subcommand's option if it reused a global option's name."""
    global_names = {n for p in ROOT.params for n in getattr(p, "opts", []) + getattr(p, "secondary_opts", [])}
    global_shorts = {n[1] for n in global_names if len(n) == 2}

    def walk(command, path):
        for sub_name, sub in getattr(command, "commands", {}).items():
            yield from walk(sub, [*path, sub_name])
        if path:
            for p in command.params:
                for name in getattr(p, "opts", []) + getattr(p, "secondary_opts", []):
                    yield " ".join(path), name

    clashes = [
        (cmd, name)
        for cmd, name in walk(ROOT, [])
        if name in global_names or (len(name) == 2 and name[1] in global_shorts)
    ]
    assert clashes == []


@pytest.fixture
def mock_api(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        body = {"responseCode": 200, "message": "success", "content": [], "limit-left": 1234}
        return httpx.Response(200, json=body)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        cli, "JotformClient", lambda key, region: JotformClient(key, region, transport=transport)
    )


runner = CliRunner()


@pytest.mark.parametrize(
    "args",
    [
        ["forms", "list", "-vQ", "--api-key", "k"],
        ["forms", "-v", "list", "--api-key=k", "-Q"],
        ["--api-key", "k", "-Q", "forms", "list", "-v"],
    ],
)
def test_cli_global_options_anywhere(mock_api, args):
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    assert "GET /user/forms?limit=20&offset=0 200" in result.stderr
    assert "API requests remaining today: 1,234" in result.stderr
