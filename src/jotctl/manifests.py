"""Load form manifests (YAML) and work out the changes they ask for.

A manifest lists forms by title or ID. Under each, `properties` sets form properties (see
`jotctl describe form`), `questions` sets question properties by question name, and
`conditions` sets properties of a condition's terms, by condition ID (see
`jotctl get conditions --form ...`) and the question the term tests:

    forms:
      - My Form:
          properties:
            status: ENABLED
            messageOfLimitedForm:
              template: "This form will open again on {{ saturday | date('%b %-d') }} at 10 AM."
          questions:
            whatDate13:
              startDate:
                template: "{{ monday | date('%Y-%m-%d') }}"
            thisSaturday:
              required: true
          conditions:
            '1234567890123':
              terms:
                date:
                  value:
                    template: "{{ saturday | date('%Y-%m-%d') }}"

A value is used as-is, or written as `{template: ...}` (a Jinja template with date helpers
such as `saturday` and `weeks(1)`; see `jotctl.templates`) or `{script: ...}` (a shell
script whose output is used).
"""

import copy
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from jinja2 import TemplateError

from jotctl import emails as form_emails
from jotctl import templates
from jotctl.client import SECRET_FIELDS
from jotctl.conditions import decode_json_list
from jotctl.questions import ordered_questions

# Form properties a manifest may not set: `conditions` must go through the `conditions` section,
# which edits individual terms instead of replacing the whole list.
RESERVED_PROPERTIES = {"conditions", "emails"}
# Form properties whose values are never displayed.
SECRET_PROPERTIES = SECRET_FIELDS


SECTIONS = {"properties", "questions", "conditions", "emails"}


class ManifestError(Exception):
    """Raised when a manifest file is malformed or a value can't be produced."""


@dataclass
class FormManifest:
    """The changes a manifest asks for on one form."""

    form: str
    """Form title or ID, as written in the manifest."""
    questions: dict[str, dict[str, Any]] = field(default_factory=dict)
    """Question name → property name → raw manifest value (a scalar or `{script: ...}`)."""
    conditions: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    """Condition ID → question tested by a term → term property → raw manifest value."""
    properties: dict[str, Any] = field(default_factory=dict)
    """Form property name → raw manifest value."""
    emails: dict[str, dict[str, Any]] = field(default_factory=dict)
    """Email name (or uniqueID) → email property → raw manifest value."""


@dataclass
class Change:
    """One question property whose value will change."""

    question: str
    qid: str
    prop: str
    old: str | None
    new: str

    @property
    def target(self) -> str:
        """What is being changed, for display."""
        return self.question


@dataclass
class PropertyChange:
    """One form property whose value will change."""

    prop: str
    old: str | None
    new: str

    @property
    def target(self) -> str:
        """What is being changed, for display."""
        return "form"


@dataclass
class EmailChange:
    """One property of one form email whose value will change."""

    email: str
    """The email's name (or uniqueID), as written in the manifest."""
    index: int
    """Position of the email in the form's `emails` list."""
    prop: str
    old: str | None
    new: str

    @property
    def target(self) -> str:
        """What is being changed, for display."""
        return f"email {self.email}"


@dataclass
class ConditionChange:
    """One property of one condition term whose value will change."""

    condition_id: str
    term: str
    """The term's question, as written in the manifest."""
    index: int
    """Position of the term within the condition's terms."""
    prop: str
    old: str | None
    new: str

    @property
    def target(self) -> str:
        """What is being changed, for display."""
        return f"condition {self.condition_id}, term {self.term}"


def load_manifest(path: Path) -> list[FormManifest]:
    """Read a manifest file.

    `forms` may be a list of single-key mappings (as in the module example) or one mapping
    of form → settings.

    Args:
        path (Path): YAML manifest file.

    Returns:
        list[FormManifest]: One entry per form, in file order.

    Raises:
        ManifestError: If the file isn't valid YAML or doesn't follow the manifest layout.
    """
    try:
        doc = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        raise ManifestError(f"{path} is not valid YAML: {e}") from None

    forms = doc.get("forms") if isinstance(doc, dict) else None
    if isinstance(forms, dict):
        forms = [{k: v} for k, v in forms.items()]
    if not isinstance(forms, list):
        raise ManifestError(f"{path}: expected a top-level `forms` list")

    manifests = []
    for entry in forms:
        if not (isinstance(entry, dict) and len(entry) == 1):
            raise ManifestError(
                f"{path}: each item under `forms` must be `<form title or ID>: {{questions: ...}}`"
            )
        ((form, settings),) = entry.items()
        if not isinstance(settings, dict) or not settings or set(settings) - SECTIONS:
            raise ManifestError(
                f"{path}: form {form!r} needs one or more of `properties:`, `questions:`, "
                "`conditions:` and `emails:`, and nothing else"
            )
        properties = settings.get("properties") or {}
        if not isinstance(properties, dict):
            raise ManifestError(f"{path}: form {form!r} needs `properties: {{<property>: <value>}}`")
        if reserved := RESERVED_PROPERTIES & set(properties):
            raise ManifestError(
                f"{path}: form {form!r} can't set {', '.join(sorted(reserved))} under `properties:`; "
                "use the `conditions:` or `emails:` section, which change single values"
            )
        questions = settings.get("questions") or {}
        if not isinstance(questions, dict) or not all(isinstance(p, dict) for p in questions.values()):
            raise ManifestError(
                f"{path}: form {form!r} needs `questions: {{<question name>: {{<property>: <value>}}}}`"
            )
        conditions = settings.get("conditions") or {}
        if not isinstance(conditions, dict) or not all(
            isinstance(c, dict)
            and set(c) == {"terms"}
            and isinstance(c["terms"], dict)
            and all(isinstance(p, dict) for p in c["terms"].values())
            for c in conditions.values()
        ):
            raise ManifestError(
                f"{path}: form {form!r} needs `conditions: {{<condition ID>: {{terms: "
                "{{<question name>: {{<property>: <value>}}}}}}}}`"
            )
        emails = settings.get("emails") or {}
        if not isinstance(emails, dict) or not all(isinstance(p, dict) for p in emails.values()):
            raise ManifestError(
                f"{path}: form {form!r} needs `emails: {{<email name>: {{<property>: <value>}}}}`"
            )
        manifests.append(
            FormManifest(
                str(form),
                questions,
                {str(cid): c["terms"] for cid, c in conditions.items()},
                {str(k): v for k, v in properties.items()},
                {str(name): props for name, props in emails.items()},
            )
        )
    return manifests


def resolve_value(value: Any) -> str:
    """Turn a manifest value into the string Jotform stores.

    Booleans become "Yes"/"No" (Jotform's convention, e.g. for `required`). A mapping with
    a `template` renders it with the date helpers in `jotctl.templates`; one with a
    `script` runs it with `sh` and uses its output, minus the trailing newline.

    Raises:
        ManifestError: If the template or script fails or produces nothing, or the value
            is a list or a mapping other than `{template: ...}` or `{script: ...}`.
    """
    if isinstance(value, dict) and set(value) == {"template"}:
        try:
            output = templates.render(str(value["template"]))
        except (TemplateError, ValueError, LookupError) as e:  # e.g. `.at('25:00')`, an unknown time zone
            raise ManifestError(f"template failed: {e}") from None
        if not output:
            raise ManifestError("template produced nothing")
        return output
    if isinstance(value, dict):
        if set(value) != {"script"}:
            raise ManifestError(
                f"a mapping value must be `{{template: ...}}` or `{{script: ...}}`, got keys {sorted(value)}"
            )
        result = subprocess.run(["sh", "-c", value["script"]], capture_output=True, text=True)
        output = result.stdout.rstrip("\n")
        if result.returncode != 0:
            raise ManifestError(f"script failed (exit {result.returncode}): {result.stderr.strip()}")
        if not output:
            raise ManifestError("script printed nothing")
        return output
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if value is None or isinstance(value, list):
        raise ManifestError(
            f"unsupported value {value!r}; use a string, number, boolean, `template` or `script`"
        )
    return str(value)


def plan_changes(manifest: FormManifest, questions: dict[str, dict[str, Any]]) -> list[Change]:
    """Compare a form manifest with the form's current questions.

    Questions are matched by name (falling back to question ID). Properties whose new value
    equals the current one are left out.

    Args:
        manifest (FormManifest): Changes requested for this form.
        questions (dict[str, dict[str, Any]]): Output of `JotformClient.get_form_questions`.

    Returns:
        list[Change]: The properties that would change, in manifest order.

    Raises:
        ManifestError: If a question isn't on the form or a value can't be produced; the message
            names the question and property.
    """
    by_name = {q.name: q for q in ordered_questions(questions)}
    changes = []
    for name, props in manifest.questions.items():
        question = by_name.get(name) or next((q for q in by_name.values() if q.qid == str(name)), None)
        if question is None:
            raise ManifestError(
                f"form {manifest.form!r} has no question named {name!r} (see `jotctl get questions --form {manifest.form!r}`)"
            )
        for prop, value in props.items():
            try:
                new = resolve_value(value)
            except ManifestError as e:
                raise ManifestError(f"{manifest.form} → {name}.{prop}: {e}") from None
            old = question.raw.get(prop)
            old = None if old is None else str(old)
            if old != new:
                changes.append(Change(name, question.qid, str(prop), old, new))
    return changes


def plan_condition_changes(
    manifest: FormManifest, conditions: list[dict[str, Any]], questions: dict[str, dict[str, Any]]
) -> list[ConditionChange]:
    """Compare a form manifest's condition settings with the form's current conditions.

    A term is found by the question it tests, given by name or question ID (or a
    non-question reference such as `language`, as it appears in the term).

    Args:
        manifest (FormManifest): Changes requested for this form.
        conditions (list[dict[str, Any]]): The form's `conditions` property, as returned.
        questions (dict[str, dict[str, Any]]): Output of `JotformClient.get_form_questions`.

    Returns:
        list[ConditionChange]: The term properties that would change, in manifest order.

    Raises:
        ManifestError: If a condition or term can't be found, a term is ambiguous, or a value
            can't be produced.
    """
    by_id = {str(c.get("id")): c for c in conditions}
    qids = {q.name: q.qid for q in ordered_questions(questions)}
    changes = []
    for cid, term_settings in manifest.conditions.items():
        if cid not in by_id:
            raise ManifestError(
                f"form {manifest.form!r} has no condition {cid} (see `jotctl get conditions --form {manifest.form!r}`)"
            )
        terms = decode_json_list(by_id[cid].get("terms"))
        for ref, props in term_settings.items():
            target = qids.get(ref, str(ref))
            matches = [i for i, t in enumerate(terms) if str(t.get("field")) == target]
            if len(matches) != 1:
                problem = "no term tests" if not matches else f"{len(matches)} terms test"
                raise ManifestError(f"{manifest.form} → condition {cid}: {problem} {ref!r}")
            (index,) = matches
            for prop, value in props.items():
                try:
                    new = resolve_value(value)
                except ManifestError as e:
                    raise ManifestError(
                        f"{manifest.form} → condition {cid}, term {ref}.{prop}: {e}"
                    ) from None
                old = terms[index].get(prop)
                old = None if old is None else str(old)
                if old != new:
                    changes.append(ConditionChange(cid, str(ref), index, str(prop), old, new))
    return changes


def apply_condition_changes(
    conditions: list[dict[str, Any]], changes: list[ConditionChange]
) -> list[dict[str, Any]]:
    """Return a copy of a form's conditions with the changes made.

    Conditions that aren't changed are returned exactly as given; changed ones have their
    `terms` re-encoded as the compact JSON string Jotform stores.
    """
    updated = copy.deepcopy(conditions)
    by_id = {str(c.get("id")): c for c in updated}
    terms_by_id: dict[str, list[dict[str, Any]]] = {}
    for change in changes:
        terms = terms_by_id.setdefault(
            change.condition_id, decode_json_list(by_id[change.condition_id]["terms"])
        )
        terms[change.index][change.prop] = change.new
    for cid, terms in terms_by_id.items():
        by_id[cid]["terms"] = json.dumps(terms, separators=(",", ":"), ensure_ascii=False)
    return updated


def unapplied_condition_changes(
    conditions: list[dict[str, Any]], changes: list[ConditionChange]
) -> list[ConditionChange]:
    """Return the changes that a re-read of the form's conditions doesn't reflect."""
    by_id = {str(c.get("id")): decode_json_list(c.get("terms")) for c in conditions}
    missing = []
    for change in changes:
        terms = by_id.get(change.condition_id, [])
        value = terms[change.index].get(change.prop) if change.index < len(terms) else None
        if value is None or str(value) != change.new:
            missing.append(change)
    return missing


def plan_property_changes(manifest: FormManifest, properties: dict[str, Any]) -> list[PropertyChange]:
    """Compare a form manifest's `properties` with the form's current properties.

    Args:
        manifest (FormManifest): Changes requested for this form.
        properties (dict[str, Any]): Output of `JotformClient.get_form_properties`.

    Returns:
        list[PropertyChange]: The properties that would change, in manifest order.

    Raises:
        ManifestError: If a value can't be produced; the message names the property.
    """
    changes = []
    for prop, value in manifest.properties.items():
        try:
            new = resolve_value(value)
        except ManifestError as e:
            raise ManifestError(f"{manifest.form} → property {prop}: {e}") from None
        old = properties.get(prop)
        old = None if old is None else str(old)
        if old != new:
            changes.append(PropertyChange(prop, old, new))
    return changes


def unapplied_property_changes(
    properties: dict[str, Any], changes: list[PropertyChange]
) -> list[PropertyChange]:
    """Return the changes that a re-read of the form's properties doesn't reflect."""
    return [c for c in changes if properties.get(c.prop) is None or str(properties[c.prop]) != c.new]


def plan_email_changes(manifest: FormManifest, emails: list[dict[str, Any]]) -> list[EmailChange]:
    """Compare a form manifest's `emails` with the form's current emails.

    Emails are found by name, then by uniqueID. Booleans become "1"/"0", as Jotform stores
    email flags such as `disabled` and `sendOnSubmit`.

    Args:
        manifest (FormManifest): Changes requested for this form.
        emails (list[dict[str, Any]]): The form's `emails` property, as returned.

    Returns:
        list[EmailChange]: The email properties that would change, in manifest order.

    Raises:
        ManifestError: If an email can't be found or a value can't be produced.
    """
    changes = []
    for ref, props in manifest.emails.items():
        try:
            index = form_emails.find_email(emails, ref)
        except LookupError as e:
            raise ManifestError(f"form {manifest.form!r}: {e} (see `jotctl get emails --form ...`)") from None
        for prop, value in props.items():
            try:
                new = ("1" if value else "0") if isinstance(value, bool) else resolve_value(value)
            except ManifestError as e:
                raise ManifestError(f"{manifest.form} → email {ref}.{prop}: {e}") from None
            old = emails[index].get(prop)
            old = None if old is None else str(old)
            if old != new:
                changes.append(EmailChange(ref, index, str(prop), old, new))
    return changes


def apply_email_changes(emails: list[dict[str, Any]], changes: list[EmailChange]) -> list[dict[str, Any]]:
    """Return a copy of a form's emails with the changes made; other emails are returned as given."""
    updated = copy.deepcopy(emails)
    for change in changes:
        updated[change.index][change.prop] = change.new
    return updated


def unapplied_email_changes(emails: list[dict[str, Any]], changes: list[EmailChange]) -> list[EmailChange]:
    """Return the changes that a re-read of the form's emails doesn't reflect."""
    return [
        c
        for c in changes
        if c.index >= len(emails)
        or emails[c.index].get(c.prop) is None
        or str(emails[c.index][c.prop]) != c.new
    ]
