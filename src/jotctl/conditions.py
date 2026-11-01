"""Decode a form's conditional logic (Jotform "conditions") and describe it readably.

Conditions come from the form's `conditions` property. Each has a `type` (`field` for
show/hide, `calculation`, `message`, `require`, ...), `terms` (the IF part) joined by
`link` ("Any" or "All"), and `action` (the THEN part). Jotform stores `terms` and `action`
as JSON-encoded strings, and refers to questions by question ID.
"""

import html
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from jotctl import backups

# Operators that don't compare against a value.
UNARY_OPERATORS = {"isFilled", "isEmpty"}
# Action bookkeeping that adds nothing to a description.
NOISE_KEYS = {"id", "isError"}


@dataclass
class Condition:
    """One condition with its terms and actions decoded."""

    id: str
    type: str
    link: str
    priority: int
    terms: list[dict[str, Any]]
    actions: list[dict[str, Any]]

    def to_json(self) -> dict[str, Any]:
        """The condition as plain data, for `-o json` and `-o yaml` output."""
        return {
            "id": self.id,
            "type": self.type,
            "link": self.link,
            "priority": self.priority,
            "terms": self.terms,
            "actions": self.actions,
        }


def raw_conditions(raw: Any) -> list[dict[str, Any]]:
    """The `conditions` form property as a list, exactly as Jotform returned it.

    Args:
        raw (Any): The property's value: a list, a JSON string of one, or None.
    """
    if not raw:
        return []
    return json.loads(raw) if isinstance(raw, str) else list(raw)


def backup_conditions(
    conditions: list[dict[str, Any]], form_id: str, directory: Path, *, now: datetime | None = None
) -> Path:
    """Save a form's conditions, exactly as read, to a new timestamped JSON file.

    Args:
        conditions (list[dict[str, Any]]): The form's `conditions` property, as returned.
        form_id (str): Form ID, used in the file name.
        directory (Path): Where to write; created if missing.
        now (datetime | None): Timestamp for the file name. Defaults to the current time.

    Returns:
        Path: The new file, e.g. `<directory>/251234567890-conditions-20261029-153000.json`.
            An existing file is never overwritten; a numeric suffix is added instead.

    Raises:
        OSError: If the directory or file can't be written.
    """
    return backups.save_json(conditions, f"{form_id}-conditions", directory, now=now)


def parse_conditions(raw: Any) -> list[Condition]:
    """Decode the `conditions` form property, sorted by priority.

    Args:
        raw (Any): The property's value: a list of conditions, a JSON string of one, or
            None when the form has no conditions.

    Returns:
        list[Condition]: Conditions in the order Jotform evaluates them.
    """
    conditions = [
        Condition(
            id=str(c.get("id", "")),
            type=c.get("type", ""),
            link=c.get("link", "Any"),
            priority=int(c.get("priority") or 0),
            terms=decode_json_list(c.get("terms")),
            actions=decode_json_list(c.get("action")),
        )
        for c in raw_conditions(raw)
    ]
    conditions.sort(key=lambda c: c.priority)
    return conditions


def decode_json_list(value: Any) -> list[dict[str, Any]]:
    """Decode a condition's `terms` or `action`, which Jotform stores as JSON strings."""
    if isinstance(value, str):
        value = json.loads(value) if value else []
    if isinstance(value, dict):
        value = [value]
    return value or []


def describe_terms(condition: Condition, names: dict[str, str]) -> str:
    """The IF part, e.g. `whatDate13 is before "2026-10-24" or time45 is filled`.

    Args:
        condition (Condition): The condition.
        names (dict[str, str]): Question ID → question name, for readable references.
    """
    joiner = " and " if condition.link == "All" else " or "
    parts = []
    for t in condition.terms:
        text = f"{_question(t.get('field'), names)} {_words(t.get('operator', ''))}"
        if t.get("operator") not in UNARY_OPERATORS:
            text += f' "{t.get("value", "")}"'
        parts.append(text)
    return joiner.join(parts)


def describe_action(action: dict[str, Any], condition_type: str, names: dict[str, str]) -> str:
    """The THEN part for one action, e.g. `show time46` or `total47 = {count13}`.

    Unrecognized action shapes fall back to their properties as `key=value` pairs.

    Args:
        action (dict[str, Any]): One decoded action.
        condition_type (str): The condition's `type`.
        names (dict[str, str]): Question ID → question name.
    """
    if "visibility" in action:
        targets = action.get("fields") or [action.get("field")]
        verb = _words(action["visibility"].removesuffix("Multiple"))
        return f"{verb} {', '.join(_question(q, names) for q in targets)}"
    if condition_type == "calculation" and "resultField" in action:
        equation = re.sub(
            r"\{(\w+)\}", lambda m: "{" + _question(m.group(1), names) + "}", action.get("equation", "")
        )
        return f"{_question(action['resultField'], names)} = {equation}"
    if condition_type == "message" and "message" in action:
        return f'show thank-you message "{_shorten(_strip_html(action["message"]))}"'
    details = {k: v for k, v in action.items() if k not in NOISE_KEYS and v not in ("", None, False)}
    return ", ".join(f"{k}={_shorten(str(v))}" for k, v in details.items())


def _question(qid: Any, names: dict[str, str]) -> str:
    """Question name for an ID; unknown IDs show as `#ID`, and non-numeric references such
    as `language` (the form's language setting) are shown as-is."""
    qid = str(qid)
    return names.get(qid) or (f"#{qid}" if qid.isdigit() else qid)


def _words(camel: str) -> str:
    """`isFilled` → `is filled`, `HideMultiple` → `hide multiple`."""
    return re.sub(r"(?<!^)(?=[A-Z])", " ", camel).lower()


def _strip_html(text: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", text)).split())


def _shorten(text: str, limit: int = 60) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"
