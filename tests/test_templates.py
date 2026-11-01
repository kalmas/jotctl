from datetime import date, datetime

import pytest
from jinja2 import TemplateError

from jotctl.templates import format_date, render

THURSDAY = date(2026, 10, 29)
SATURDAY = date(2026, 10, 31)


@pytest.mark.parametrize(
    ("template", "today", "expected"),
    [
        ("{{ today }}", THURSDAY, "2026-10-29"),
        ("{{ saturday }}", THURSDAY, "2026-10-31"),
        ("{{ thursday }}", THURSDAY, "2026-11-05"),  # always after today
        ("{{ saturday }}", SATURDAY, "2026-11-07"),
        ("{{ monday }} {{ sunday }}", SATURDAY, "2026-11-02 2026-11-01"),
        ("{{ (monday + weeks(1)) | date('%Y-%m-%d') }}", THURSDAY, "2026-11-09"),
        ("{{ (today - days(5)) | date('%a %b %-d') }}", THURSDAY, "Sat Oct 24"),
        ("{{ saturday.at('15:00') | date('%Y-%m-%d %H:%M') }}", THURSDAY, "2026-10-31 15:00"),
        ("{{ (saturday + weeks(1)).at('9:05') | date('%-I:%M %p') }}", THURSDAY, "9:05 AM"),
        ("custom_{{ saturday | date('%Y-%m-%d') }}", THURSDAY, "custom_2026-10-31"),
        ("We're open {{ saturday | date('%b %-d') }}.", THURSDAY, "We're open Oct 31."),
    ],
)
def test_render(template, today, expected):
    assert render(template, today=today) == expected


def test_render_matches_bsd_date_scripts():
    """The conversions used in manifests/weekly-reopen.yaml, checked against `date -v` semantics."""
    for today in [date(2026, 10, d) for d in range(24, 32)]:  # every weekday, Sat 24 → Sat 31
        weekday = today.weekday()
        next_mon = today.toordinal() + ((0 - weekday - 1) % 7 + 1)
        next_sat = today.toordinal() + ((5 - weekday - 1) % 7 + 1)
        assert render("{{ monday }}", today=today) == date.fromordinal(next_mon).isoformat()
        assert render("{{ saturday }}", today=today) == date.fromordinal(next_sat).isoformat()
        assert next_sat - today.toordinal() in range(1, 8)


@pytest.mark.parametrize("template", ["{{ nope }}", "{{ today.__class__ }}", "{{ saturday.at('25:00') }}"])
def test_render_errors(template):
    with pytest.raises((TemplateError, ValueError)):
        render(template, today=THURSDAY)


def test_format_date_no_pad_codes():
    when = datetime(2026, 1, 5, 7, 3, 9)
    assert format_date(when, "%-m/%-d %-H:%-M:%-S %-I") == "1/5 7:3:9 7"
    assert format_date(when, "%m/%d 100%%") == "01/05 100%"
    assert format_date(when, "") == ""
