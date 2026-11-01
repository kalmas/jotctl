"""Load report files (YAML) and run their SQL over a form's submissions with DuckDB.

A report names a form and a DuckDB query over a `submissions` table:

    form: My Form
    query: |
      SELECT shopper_name, phone_number
      FROM submissions
      WHERE appt_time LIKE '{{ saturday | date('%m/%d/%Y') }}%'

The table's columns are the snake_case names written by `jotctl submissions summary --csv
... --normalize-headers`, and DuckDB detects their types (numbers, booleans, timestamps) as
it would reading that CSV. The query may use the same `{{ ... }}` date templates as
manifests (see `jotctl.templates`).
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb
import yaml
from jinja2 import TemplateError

from jotctl import templates


class ReportError(Exception):
    """Raised when a report file is malformed or its query fails."""


@dataclass
class Report:
    """A saved query over one form's submissions."""

    form: str
    """Form title or ID."""
    query: str
    """DuckDB SQL over `submissions`, possibly with `{{ ... }}` templates."""


@dataclass
class Result:
    """A query's column names and rows."""

    columns: list[str]
    rows: list[tuple[Any, ...]]


def load_report(path: Path) -> Report:
    """Read a report file.

    Raises:
        ReportError: If the file isn't valid YAML or doesn't have exactly `form` and `query`.
    """
    try:
        doc = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        raise ReportError(f"{path} is not valid YAML: {e}") from None
    if not (isinstance(doc, dict) and set(doc) == {"form", "query"} and isinstance(doc["query"], str)):
        raise ReportError(f"{path}: a report needs exactly `form: <title or ID>` and `query: <SQL>`")
    return Report(str(doc["form"]), doc["query"])


def render_query(report: Report) -> str:
    """The report's query with its `{{ ... }}` templates filled in.

    Raises:
        ReportError: If a template fails.
    """
    try:
        return templates.render(report.query)
    except (TemplateError, ValueError) as e:
        raise ReportError(f"query template failed: {e}") from None


def run_query(sql: str, csv_path: Path) -> Result:
    """Run `sql` with a `submissions` view over a submissions CSV.

    Args:
        sql (str): One DuckDB statement.
        csv_path (Path): A submissions export written with normalized headers.

    Returns:
        Result: Column names and rows.

    Raises:
        ReportError: If DuckDB rejects the query or can't read the file.
    """
    with duckdb.connect() as con:
        try:
            quoted = str(csv_path).replace("'", "''")
            con.execute(f"CREATE VIEW submissions AS FROM read_csv('{quoted}')")
            cursor = con.execute(sql)
            columns = [d[0] for d in cursor.description or []]
            return Result(columns, cursor.fetchall())
        except duckdb.Error as e:
            raise ReportError(f"query failed: {e}") from None


def format_value(value: Any) -> str:
    """Render a result value the way DuckDB's CSV output does (NULL → "", true/false)."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
