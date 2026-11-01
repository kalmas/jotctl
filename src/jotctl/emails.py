"""Read a form's emails (notifications and autoresponders) from its `emails` property.

Jotform keeps a form's emails as a list in the `emails` form property; asking for
`/form/{id}/properties/emails` directly returns 404. Each email is a dict of strings: `name`,
`type` (`notification` or `autorespond`), `to`, `from`, `replyTo`, `cc`, `bcc`, `subject`, an
HTML `body`, `disabled` ("0"/"1"), `sendOnSubmit`, `sendOnEdit`, `sendDate`, `uniqueID` and
more. Addresses may be question references such as `{14}` or `{emailAddress}`.
"""

import html
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from jotctl import backups

TYPE_LABELS = {"notification": "notification", "autorespond": "autoresponder"}


def raw_emails(raw: Any) -> list[dict[str, Any]]:
    """The `emails` form property as a list, exactly as Jotform returned it.

    Args:
        raw (Any): The property's value: a list, a dict keyed by position, a JSON string of
            either, or None.
    """
    if not raw:
        return []
    if isinstance(raw, str):
        raw = json.loads(raw)
    if isinstance(raw, dict):
        raw = list(raw.values())
    return list(raw)


def find_email(emails: list[dict[str, Any]], ref: str) -> int:
    """Return the position of the email named `ref` (or with that `uniqueID`).

    Raises:
        LookupError: If no email, or more than one, matches; the message lists the names.
    """
    matches = [i for i, e in enumerate(emails) if e.get("name") == ref] or [
        i for i, e in enumerate(emails) if str(e.get("uniqueID")) == ref
    ]
    if len(matches) == 1:
        return matches[0]
    names = ", ".join(repr(e.get("name", "")) for e in emails) or "none"
    if matches:
        raise LookupError(
            f"{len(matches)} emails are named {ref!r}; use its uniqueID instead (emails: {names})"
        )
    raise LookupError(f"no email named {ref!r} (emails: {names})")


def is_enabled(email: dict[str, Any]) -> bool:
    """Whether the email is sent (its `disabled` flag isn't set)."""
    return str(email.get("disabled", "0")) not in ("1", "Yes", "true")


def type_label(email: dict[str, Any]) -> str:
    """`notification` or `autoresponder`."""
    kind = str(email.get("type", ""))
    return TYPE_LABELS.get(kind, kind)


def body_text(body: str) -> str:
    """An HTML email body as readable plain text, one paragraph per line."""
    text = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h[1-6])>", "\n", body)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def backup_emails(
    emails: list[dict[str, Any]], form_id: str, directory: Path, *, now: datetime | None = None
) -> Path:
    """Save a form's emails, exactly as read, to a new timestamped JSON file (see `backups.save_json`)."""
    return backups.save_json(emails, f"{form_id}-emails", directory, now=now)
