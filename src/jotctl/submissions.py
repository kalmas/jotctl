"""Summarize Jotform submissions and flatten them into CSV rows."""

import csv
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jotctl.questions import ordered_questions

FIXED_COLUMNS = ["Submission ID", "Submitted at", "Status"]
MAX_NORMALIZED_LENGTH = 20


@dataclass
class SubmissionSummary:
    """Aggregate statistics for a set of submissions."""

    total: int = 0
    unread: int = 0
    flagged: int = 0
    first: str | None = None
    last: str | None = None
    by_status: Counter[str] = field(default_factory=Counter)
    by_month: Counter[str] = field(default_factory=Counter)


def summarize(submissions: list[dict[str, Any]]) -> SubmissionSummary:
    """Compute totals, date range, and status/month breakdowns.

    Args:
        submissions (list[dict[str, Any]]): Submissions as returned by the Jotform API.

    Returns:
        SubmissionSummary: Aggregated statistics.
    """
    summary = SubmissionSummary(total=len(submissions))
    dates = sorted(s["created_at"] for s in submissions if s.get("created_at"))
    if dates:
        summary.first, summary.last = dates[0], dates[-1]
    for s in submissions:
        summary.unread += s.get("new") == "1"
        summary.flagged += s.get("flag") == "1"
        summary.by_status[s.get("status", "UNKNOWN")] += 1
        if s.get("created_at"):
            summary.by_month[s["created_at"][:7]] += 1  # "YYYY-MM"
    return summary


def answer_columns(questions: dict[str, dict[str, Any]]) -> list[tuple[str, str]]:
    """Return (question ID, column header) pairs for answerable questions, in form order.

    Args:
        questions (dict[str, dict[str, Any]]): Output of `JotformClient.get_form_questions`.

    Returns:
        list[tuple[str, str]]: Question ID and a unique, human-readable header.
    """
    columns: list[tuple[str, str]] = []
    used = set(FIXED_COLUMNS)
    for q in ordered_questions(questions, inputs_only=True):
        header = q.label or q.name or q.qid
        if header in used:
            header = f"{header} ({q.name or q.qid})"
        used.add(header)
        columns.append((q.qid, header))
    return columns


def normalize_headers(headers: list[str], max_length: int = MAX_NORMALIZED_LENGTH) -> list[str]:
    """Turn headers into short snake_case names such as `submission_id`.

    Accents are stripped, letters lowercased, and each run of other characters becomes a
    single "_". Names are cut to `max_length` characters; a name that would repeat an
    earlier one gets a numeric suffix (`email_2`), still within `max_length`.

    Args:
        headers (list[str]): Original column headers.
        max_length (int): Maximum length of each normalized name.

    Returns:
        list[str]: Unique normalized names, in the same order.
    """
    result: list[str] = []
    used: set[str] = set()
    for header in headers:
        ascii_only = unicodedata.normalize("NFKD", header).encode("ascii", "ignore").decode()
        base = re.sub(r"[^a-z0-9]+", "_", ascii_only.lower()).strip("_") or "column"
        name, n = base[:max_length].rstrip("_"), 1
        while name in used:
            n += 1
            suffix = f"_{n}"
            name = base[: max_length - len(suffix)].rstrip("_") + suffix
        used.add(name)
        result.append(name)
    return result


def format_answer(answer: dict[str, Any] | None) -> str:
    """Render one answer as a single CSV cell.

    Jotform supplies `prettyFormat` for compound questions (names, addresses, dates);
    otherwise lists are joined with "; " and dicts rendered as "key: value" pairs.
    """
    if not answer:
        return ""
    if answer.get("prettyFormat"):
        return str(answer["prettyFormat"])
    return _flatten(answer.get("answer"))


def _flatten(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return "; ".join(s for s in (_flatten(v) for v in value) if s)
    if isinstance(value, dict):
        return "; ".join(f"{k}: {s}" for k, v in value.items() if (s := _flatten(v)))
    return str(value)


def write_csv(
    path: Path,
    questions: dict[str, dict[str, Any]],
    submissions: list[dict[str, Any]],
    *,
    normalize: bool = False,
) -> None:
    """Write submissions to a CSV file, one row per submission and one column per question.

    Args:
        path (Path): Destination file (overwritten if it exists).
        questions (dict[str, dict[str, Any]]): Output of `JotformClient.get_form_questions`.
        submissions (list[dict[str, Any]]): Submissions as returned by the Jotform API.
        normalize (bool): Write headers as short snake_case names (see `normalize_headers`).
    """
    headers, rows = to_rows(questions, submissions, normalize=normalize)
    # utf-8-sig adds a BOM so Excel detects UTF-8 instead of mangling non-ASCII answers.
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(rows)


def to_rows(
    questions: dict[str, dict[str, Any]],
    submissions: list[dict[str, Any]],
    *,
    normalize: bool = False,
) -> tuple[list[str], list[list[str]]]:
    """Flatten submissions into a header row and one row of strings per submission.

    Args:
        questions (dict[str, dict[str, Any]]): Output of `JotformClient.get_form_questions`.
        submissions (list[dict[str, Any]]): Submissions as returned by the Jotform API.
        normalize (bool): Use short snake_case headers (see `normalize_headers`).

    Returns:
        tuple[list[str], list[list[str]]]: Headers, then rows in submission order.
    """
    columns = answer_columns(questions)
    headers = FIXED_COLUMNS + [header for _, header in columns]
    rows = [
        [s.get("id", ""), s.get("created_at", ""), s.get("status", "")]
        + [format_answer((s.get("answers") or {}).get(qid)) for qid, _ in columns]
        for s in submissions
    ]
    return (normalize_headers(headers) if normalize else headers), rows
