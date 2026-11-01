"""Read the form settings that decide whether a form accepts submissions.

These are form properties: `status` (ENABLED/DISABLED), `expireDate` with
`expireDateTimeZone` ("disable on date"), `limitSubmission` against the form's `count`
("disable on submission limit"), `messageOfLimitedForm` (shown when the form is closed),
and `passwordProtect`.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Jotform's value for "not set" on the date and limit properties.
NO_LIMIT = "No Limit"


@dataclass
class Availability:
    """A form's open/closed settings."""

    status: str
    expire_date: str | None
    """Local date and time the form is disabled, e.g. "2026-10-28 10:00", if set."""
    timezone: str | None
    expired: bool | None
    """Whether `expire_date` has passed; None when there's no date or it can't be read."""
    limit: int | None
    count: int | None
    message: str
    password_protected: bool

    @property
    def remaining(self) -> int | None:
        """Submissions left before the limit, if there is one."""
        if self.limit is None or self.count is None:
            return None
        return max(self.limit - self.count, 0)

    def to_json(self) -> dict[str, Any]:
        """The settings as plain data, for `-o json` and `-o yaml` output."""
        return {
            "status": self.status,
            "expireDate": self.expire_date,
            "timezone": self.timezone,
            "expired": self.expired,
            "limit": self.limit,
            "count": self.count,
            "remaining": self.remaining,
            "message": self.message,
            "passwordProtected": self.password_protected,
        }


def read_availability(properties: dict[str, Any], *, now: datetime | None = None) -> Availability:
    """Extract the open/closed settings from a form's properties.

    Args:
        properties (dict[str, Any]): Output of `JotformClient.get_form_properties`.
        now (datetime | None): Aware "current" time to compare the disable date with.
            Defaults to the current time.

    Returns:
        Availability: The settings. The password itself is never included.
    """
    expire = _setting(properties.get("expireDate"))
    timezone = _setting(properties.get("expireDateTimeZone"))
    return Availability(
        status=str(properties.get("status") or ""),
        expire_date=expire,
        timezone=timezone,
        expired=_has_passed(expire, timezone, now) if expire else None,
        limit=_int(properties.get("limitSubmission")),
        count=_int(properties.get("count")),
        message=str(properties.get("messageOfLimitedForm") or ""),
        password_protected=properties.get("passwordProtect") == "Yes",
    )


def _setting(value: Any) -> str | None:
    value = str(value or "").strip()
    return None if not value or value == NO_LIMIT else value


def _int(value: Any) -> int | None:
    try:
        return int(str(value))
    except ValueError:
        return None


def _has_passed(expire: str, timezone: str | None, now: datetime | None) -> bool | None:
    try:
        tz = ZoneInfo(timezone) if timezone else None
        when = datetime.strptime(expire, "%Y-%m-%d %H:%M").replace(tzinfo=tz)
    except (ValueError, ZoneInfoNotFoundError):
        return None
    if tz is None:  # no zone given: compare as local time
        return when <= (now.astimezone().replace(tzinfo=None) if now else datetime.now())
    return when <= (now or datetime.now(tz))
