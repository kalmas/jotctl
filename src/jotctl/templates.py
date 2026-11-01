"""Render manifest values written as Jinja templates, with date helpers.

Templates run in Jinja's sandbox with these names available:

- `today`: the current date.
- `monday` … `sunday`: the next such day, always after today (on a Saturday, `saturday`
  is a week away).
- `days(n)`, `weeks(n)`: durations to add or subtract, e.g. `saturday + weeks(1)`.
- `<day>.at('15:00')`: that day at a time of day; `<day>.at('15:00', 'America/New_York')` adds
  that time zone, so `%:z` prints its UTC offset (e.g. `-05:00`).
- `| date('%Y-%m-%d')`: format a date or date-time with strftime codes. `%-d`, `%-m`,
  `%-H`, `%-I`, `%-M` and `%-S` drop the leading zero on every platform.

Filters bind tighter than `+`, so wrap arithmetic in parentheses before formatting:
`{{ (saturday + weeks(1)) | date('%b %-d') }}`.
"""

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from jinja2 import StrictUndefined
from jinja2.sandbox import SandboxedEnvironment

NO_PAD = re.compile(r"%-[dmHIMS]")
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


class Day(date):
    """A date that can be combined with a time of day in templates."""

    def at(self, hhmm: str, timezone: str | None = None) -> datetime:
        """This day at `hhmm` (24-hour "H:MM" or "HH:MM"), e.g. `saturday.at('15:00')`.

        With a time zone name such as "America/New_York", the result carries that zone's UTC
        offset for this date (daylight saving included), which `%z` / `%:z` print.
        """
        when = datetime.combine(self, datetime.strptime(hhmm, "%H:%M").time())
        return when.replace(tzinfo=ZoneInfo(timezone)) if timezone else when


def format_date(value: date | datetime, fmt: str) -> str:
    """strftime, plus portable `%-X` codes that drop the leading zero (e.g. `%-d` → "7")."""
    parts = re.split(f"({NO_PAD.pattern})", fmt)
    return "".join(
        str(int(value.strftime("%" + part[2]))) if NO_PAD.fullmatch(part) else value.strftime(part)
        for part in parts
    )


def render(template: str, *, today: date | None = None) -> str:
    """Render a template string with the date helpers.

    Args:
        template (str): Jinja template, e.g. `"{{ monday | date('%Y-%m-%d') }}"`.
        today (date | None): The date to treat as today. Defaults to the current date.

    Returns:
        str: The rendered text.

    Raises:
        jinja2.TemplateError: If the template is invalid or uses an unknown name.
    """
    today = Day.fromordinal((today or date.today()).toordinal())
    env = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False, keep_trailing_newline=False)
    env.filters["date"] = format_date
    context = {
        "today": today,
        "days": lambda n: timedelta(days=n),
        "weeks": lambda n: timedelta(weeks=n),
    }
    for i, name in enumerate(WEEKDAYS):
        ahead = (i - today.weekday() - 1) % 7 + 1  # 1..7: always after today
        context[name] = today + timedelta(days=ahead)
    return env.from_string(template).render(context)
