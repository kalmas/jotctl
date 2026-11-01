"""Helpers for working with a form's questions (its fields, including layout elements)."""

from dataclasses import dataclass
from typing import Any

# Question types that are layout/decoration and never hold an answer.
NON_INPUT_TYPES = {
    "control_head",
    "control_button",
    "control_text",
    "control_image",
    "control_divider",
    "control_pagebreak",
    "control_collapse",
    "control_separator",
}


@dataclass
class Question:
    """One question (field) on a form, with the properties most useful for listing and editing."""

    qid: str
    order: int
    type: str
    name: str
    label: str
    required: bool
    options: list[str]
    raw: dict[str, Any]

    @property
    def is_input(self) -> bool:
        """True if the question collects an answer (not a heading, button, divider, ...)."""
        return self.type not in NON_INPUT_TYPES

    @property
    def short_type(self) -> str:
        """Type without the `control_` prefix, e.g. `textbox`."""
        return self.type.removeprefix("control_")


def ordered_questions(questions: dict[str, dict[str, Any]], *, inputs_only: bool = False) -> list[Question]:
    """Return a form's questions in the order they appear on the form.

    Args:
        questions (dict[str, dict[str, Any]]): Output of `JotformClient.get_form_questions`.
        inputs_only (bool, optional): Skip layout elements such as headings and buttons. Defaults to False.

    Returns:
        list[Question]: Questions sorted by form order.
    """
    result = [
        Question(
            qid=qid,
            order=int(q.get("order") or 0),
            type=q.get("type", ""),
            name=q.get("name", ""),
            label=(q.get("text") or "").strip(),
            required=q.get("required") == "Yes",
            # Choice questions (dropdown, radio, checkbox) store their options pipe-separated.
            options=[o for o in (q.get("options") or "").split("|") if o],
            raw=q,
        )
        for qid, q in questions.items()
    ]
    if inputs_only:
        result = [q for q in result if q.is_input]
    # `order` can repeat (the API docs example has two questions at order 1); tie-break on qid.
    result.sort(key=lambda q: (q.order, int(q.qid) if q.qid.isdigit() else 0))
    return result
