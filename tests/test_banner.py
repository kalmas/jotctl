from typer.testing import CliRunner

from jotctl import cli
from jotctl.banner import ROWS, TAGLINE, WIDTH

runner = CliRunner()


def test_rows_line_up():
    assert {len(row) for row in ROWS} == {WIDTH}


def test_root_help_shows_banner():
    result = runner.invoke(cli.app, ["--help"], env={"COLUMNS": "100"})
    assert result.exit_code == 0, result.output
    assert ROWS[0] in result.output
    assert TAGLINE in result.output
    assert result.output.index(TAGLINE) < result.output.index("Usage:")


def test_no_args_shows_banner():
    result = runner.invoke(cli.app, [], env={"COLUMNS": "100"})
    assert TAGLINE in result.output


def test_subcommand_help_has_no_banner():
    result = runner.invoke(cli.app, ["get", "--help"], env={"COLUMNS": "100"})
    assert result.exit_code == 0, result.output
    assert TAGLINE not in result.output


def test_narrow_terminal_skips_banner():
    result = runner.invoke(cli.app, ["--help"], env={"COLUMNS": str(WIDTH)})
    assert result.exit_code == 0, result.output
    assert TAGLINE not in result.output
