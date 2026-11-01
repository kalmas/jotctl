"""Command-line interface for Jotform administration."""

import csv
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from jotctl import availability, backups, manifests, reports
from jotctl import conditions as conds
from jotctl import submissions as subs
from jotctl.client import FormOrderBy, FormStatus, JotformClient, JotformError, Region
from jotctl.questions import ordered_questions

load_dotenv()

app = typer.Typer(help="Automate Jotform form administration.", no_args_is_help=True)
forms_app = typer.Typer(help="Manage forms.", no_args_is_help=True)
questions_app = typer.Typer(help="Inspect a form's questions (fields).", no_args_is_help=True)
submissions_app = typer.Typer(help="Work with form submissions.", no_args_is_help=True)
reports_app = typer.Typer(help="Run saved reports: SQL over a form's submissions.", no_args_is_help=True)
app.add_typer(forms_app, name="forms")
app.add_typer(questions_app, name="questions")
app.add_typer(submissions_app, name="submissions")
app.add_typer(reports_app, name="reports")

console = Console()
err_console = Console(stderr=True)


@dataclass
class Settings:
    """Settings for the Jotform CLI."""

    api_key: str | None
    region: Region
    show_quota: bool = False


@app.callback()
def main(
    ctx: typer.Context,
    api_key: Annotated[
        str | None,
        typer.Option(
            envvar="JOTFORM_API_KEY",
            show_envvar=True,
            help="Jotform API key (Settings → API in your Jotform account).",
        ),
    ] = None,
    region: Annotated[
        Region,
        typer.Option(envvar="JOTFORM_REGION", show_envvar=True, help="Jotform data region."),
    ] = Region.US,
    show_quota: Annotated[
        bool,
        typer.Option(
            "--show-quota", help="After the command, print how many API requests you have left today."
        ),
    ] = False,
) -> None:
    """Initialize the Jotform CLI with the given API key and region.

    Args:
        ctx (typer.Context): Typer context.
        api_key (str | None): Jotform API key.
        region (Region): Jotform data region.
        show_quota (bool): Report the remaining daily API requests when the command finishes.
    """
    ctx.obj = Settings(api_key=api_key, region=region, show_quota=show_quota)


def get_client(ctx: typer.Context) -> JotformClient:
    """Get a Jotform client using the current CLI settings.

    Args:
        ctx (typer.Context): Typer context.

    Returns:
        JotformClient: Configured Jotform client.

    Raises:
        typer.Exit: If the API key is not set.
    """
    settings: Settings = ctx.obj
    if not settings.api_key:
        err_console.print(
            "[red]Error:[/red] no API key. Set [bold]JOTFORM_API_KEY[/bold] or pass [bold]--api-key[/bold]."
        )
        raise typer.Exit(1)
    client = JotformClient(settings.api_key, settings.region)
    if settings.show_quota:
        # Runs when the command finishes, including after an API error.
        ctx.call_on_close(lambda: report_quota(client))
    return client


def report_quota(client: JotformClient) -> None:
    """Print the remaining daily API requests to stderr, if any response reported it.

    Args:
        client (JotformClient): Client whose last response carried `limit-left`.
    """
    if client.limit_left is not None:
        err_console.print(f"API requests remaining today: [bold]{client.limit_left:,}[/bold]")


@forms_app.command("list")
def list_forms(
    ctx: typer.Context,
    limit: Annotated[int, typer.Option(min=1, max=1000, help="Max number of forms to return.")] = 20,
    offset: Annotated[int, typer.Option(min=0, help="Number of forms to skip.")] = 0,
    status: Annotated[FormStatus | None, typer.Option(help="Only show forms with this status.")] = None,
    order_by: Annotated[FormOrderBy | None, typer.Option(help="Field to sort by.")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print raw JSON instead of a table.")] = False,
) -> None:
    """List forms in your Jotform account."""
    try:
        with get_client(ctx) as client:
            forms = client.list_forms(limit=limit, offset=offset, status=status, order_by=order_by)
    except JotformError as e:
        err_console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1) from None

    if as_json:
        typer.echo(json.dumps(forms, indent=2))
        return

    if not forms:
        console.print("No forms found.")
        return

    table = Table(title=f"Forms ({len(forms)})")
    table.add_column("ID", no_wrap=True)
    table.add_column("Title")
    table.add_column("Status")
    table.add_column("Submissions", justify="right")
    table.add_column("Last submission")
    table.add_column("Created")
    for form in forms:
        table.add_row(
            form.get("id", ""),
            form.get("title", ""),
            form.get("status", ""),
            str(form.get("count", "")),
            form.get("last_submission") or "—",
            form.get("created_at", ""),
        )
    console.print(table)


@dataclass
class FormPlan:
    """What `forms apply` will change on one form."""

    manifest: manifests.FormManifest
    form_id: str
    changes: list[manifests.Change]
    conditions: list[dict]
    """The form's conditions as read, which condition changes are applied to."""
    condition_changes: list[manifests.ConditionChange]
    property_changes: list[manifests.PropertyChange]

    @property
    def total(self) -> int:
        """Number of changes on this form."""
        return len(self.changes) + len(self.condition_changes) + len(self.property_changes)


@forms_app.command("apply")
def apply_manifest(
    ctx: typer.Context,
    manifest_path: Annotated[
        Path,
        typer.Argument(
            exists=True,
            dir_okay=False,
            readable=True,
            help="YAML manifest: forms → properties, questions and/or conditions → values.",
        ),
    ],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Apply the changes without asking.")] = False,
    backup_dir: Annotated[
        Path,
        typer.Option(
            file_okay=False,
            help="Before changing a form's conditions, save the original ones here as timestamped JSON.",
        ),
    ] = backups.DEFAULT_DIR,
) -> None:
    """Apply a YAML manifest to forms, showing every change and asking before applying it.

    A manifest sets form properties, question properties and condition terms. Values written as `{script: ...}` are produced by running the script with `sh`. Condition
    changes are sent as the form's whole condition list, then re-read to check they took.
    """
    try:
        form_manifests = manifests.load_manifest(manifest_path)
    except manifests.ManifestError as e:
        err_console.print(f"[red]Error:[/red] {escape(str(e))}")
        raise typer.Exit(1) from None

    applied = 0
    try:
        with get_client(ctx) as client:
            plans: list[FormPlan] = []
            with err_console.status("Reading forms…"):
                for manifest in form_manifests:
                    form_id = client.resolve_form_id(manifest.form)
                    questions = client.get_form_questions(form_id)
                    properties = (
                        client.get_form_properties(form_id)
                        if manifest.conditions or manifest.properties
                        else {}
                    )
                    conditions = conds.raw_conditions(properties.get("conditions"))
                    plans.append(
                        FormPlan(
                            manifest,
                            form_id,
                            manifests.plan_changes(manifest, questions),
                            conditions,
                            manifests.plan_condition_changes(manifest, conditions, questions),
                            manifests.plan_property_changes(manifest, properties),
                        )
                    )

            total = sum(plan.total for plan in plans)
            if not total:
                console.print("No changes: the forms already match the manifest.")
                return

            table = Table(title=f"Changes from {escape(manifest_path.name)} ({total})")
            table.add_column("Form")
            table.add_column("Target")
            table.add_column("Property", no_wrap=True)
            table.add_column("Old")
            table.add_column("New")
            for plan in plans:
                for c in [*plan.property_changes, *plan.changes, *plan.condition_changes]:
                    secret = isinstance(c, manifests.PropertyChange) and c.prop in manifests.SECRET_PROPERTIES
                    if c.old is None:
                        old = Text("(not set)", style="dim")
                    else:
                        old = Text("(hidden)", style="dim") if secret else Text(c.old, style="red")
                    new = Text("(hidden)", style="dim") if secret else Text(c.new, style="green")
                    table.add_row(
                        Text(f"{plan.manifest.form} ({plan.form_id})"), Text(c.target), Text(c.prop), old, new
                    )
            console.print(table)

            if not yes and not typer.confirm(f"Apply {total} change(s)?"):
                console.print("No changes made.")
                return

            for plan in plans:
                by_question: dict[tuple[str, str], dict[str, str]] = {}
                for c in plan.changes:
                    by_question.setdefault((c.qid, c.question), {})[c.prop] = c.new
                for (qid, question), props in by_question.items():
                    client.update_question(plan.form_id, qid, props)
                    applied += len(props)
                    console.print(Text(f"Updated {plan.manifest.form} → {question}: {', '.join(props)}"))

                if plan.property_changes:
                    client.update_form_properties(
                        plan.form_id, {c.prop: c.new for c in plan.property_changes}
                    )
                    reread = client.get_form_properties(plan.form_id)
                    if missing := manifests.unapplied_property_changes(reread, plan.property_changes):
                        raise JotformError(
                            f"Jotform didn't apply {', '.join(c.prop for c in missing)} on {plan.manifest.form}; "
                            "check the form's settings in the Jotform builder."
                        )
                    applied += len(plan.property_changes)
                    names = ", ".join(c.prop for c in plan.property_changes)
                    console.print(Text(f"Updated {plan.manifest.form} → properties: {names}"))

                if plan.condition_changes:
                    try:
                        backup = conds.backup_conditions(plan.conditions, plan.form_id, backup_dir)
                    except OSError as e:
                        raise JotformError(
                            f"couldn't back up the conditions on {plan.manifest.form}, so they weren't changed: {e}"
                        ) from None
                    console.print(Text(f"Backed up the conditions on {plan.manifest.form} to {backup}"))
                    updated = manifests.apply_condition_changes(plan.conditions, plan.condition_changes)
                    client.update_form_properties(plan.form_id, {"conditions": updated})
                    reread = conds.raw_conditions(client.get_form_properties(plan.form_id).get("conditions"))
                    missing = manifests.unapplied_condition_changes(reread, plan.condition_changes)
                    if missing or len(reread) != len(plan.conditions):
                        raise JotformError(
                            f"conditions on {plan.manifest.form} don't match what was sent "
                            f"({len(reread)} conditions now, {len(plan.conditions)} before; "
                            f"{len(missing)} change(s) not reflected). Check the form's conditions "
                            f"in the Jotform builder; the originals are saved in {backup}."
                        )
                    applied += len(plan.condition_changes)
                    ids = ", ".join(dict.fromkeys(c.condition_id for c in plan.condition_changes))
                    console.print(Text(f"Updated {plan.manifest.form} → condition(s) {ids}"))
    except (JotformError, manifests.ManifestError) as e:
        err_console.print(f"[red]Error:[/red] {escape(str(e))}")
        if applied:
            err_console.print(f"{applied} change(s) were applied before the error.")
        raise typer.Exit(1) from None
    console.print(f"Applied {applied} change(s).")


@forms_app.command("conditions")
def form_conditions(
    ctx: typer.Context,
    form: Annotated[
        str, typer.Argument(help="Jotform form ID or exact form title (see `jotctl forms list`).")
    ],
    as_json: Annotated[
        bool, typer.Option("--json", help="Print the decoded conditions as JSON instead of a table.")
    ] = False,
) -> None:
    """List a form's conditional logic: show/hide rules, calculations, thank-you messages, ..."""
    try:
        with get_client(ctx) as client:
            form_id = client.resolve_form_id(form)
            conditions = conds.parse_conditions(client.get_form_properties(form_id).get("conditions"))
            questions = client.get_form_questions(form_id) if conditions and not as_json else {}
    except JotformError as e:
        err_console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1) from None

    if as_json:
        typer.echo(json.dumps([c.to_json() for c in conditions], indent=2))
        return

    if not conditions:
        console.print("No conditions found.")
        return

    names = {q.qid: q.name or f"#{q.qid}" for q in ordered_questions(questions)}
    table = Table(title=f"Conditions on form {form_id} ({len(conditions)})", show_lines=True)
    table.add_column("#", justify="right", no_wrap=True)
    table.add_column("ID", style="dim", no_wrap=True)
    table.add_column("Type", no_wrap=True)
    table.add_column("If")
    table.add_column("Then")
    for i, c in enumerate(conditions, 1):
        then = "\n".join(conds.describe_action(a, c.type, names) for a in c.actions)
        table.add_row(str(i), c.id, c.type, Text(conds.describe_terms(c, names)), Text(then))
    console.print(table)


@forms_app.command("status")
def form_status(
    ctx: typer.Context,
    form: Annotated[
        str, typer.Argument(help="Jotform form ID or exact form title (see `jotctl forms list`).")
    ],
    as_json: Annotated[bool, typer.Option("--json", help="Print the settings as JSON instead.")] = False,
) -> None:
    """Show whether a form is open: its status, disable date, submission limit and closed message."""
    try:
        with get_client(ctx) as client:
            form_id = client.resolve_form_id(form)
            properties = client.get_form_properties(form_id)
    except JotformError as e:
        err_console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1) from None

    info = availability.read_availability(properties)
    if as_json:
        typer.echo(
            json.dumps({"id": form_id, "title": properties.get("title", ""), **info.to_json()}, indent=2)
        )
        return

    if info.expire_date is None:
        expires = Text("not set", style="dim")
    else:
        expires = Text(f"{info.expire_date} {info.timezone or ''}".strip())
        if info.expired:
            expires.append(" (passed)", style="red")
        elif info.expired is False:
            expires.append(" (upcoming)", style="green")

    if info.limit is None:
        limit = Text("not set", style="dim")
    else:
        limit = Text(f"{info.limit}")
        if info.count is not None:
            limit.append(f" ({info.count} submitted, {info.remaining} left)")
            if info.remaining == 0:
                limit.append(" reached", style="red")

    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold")
    grid.add_column()
    grid.add_row("Form", Text(f"{properties.get('title', '')} ({form_id})"))
    grid.add_row("Status", Text(info.status, style="green" if info.status == "ENABLED" else "red"))
    grid.add_row("Disable on date", expires)
    grid.add_row("Submission limit", limit)
    grid.add_row("Password protected", "yes" if info.password_protected else "no")
    grid.add_row("Closed message", Text(info.message) if info.message else Text("not set", style="dim"))
    console.print(grid)


@submissions_app.command("summary")
def submissions_summary(
    ctx: typer.Context,
    form: Annotated[
        str, typer.Argument(help="Jotform form ID or exact form title (see `jotctl forms list`).")
    ],
    csv_path: Annotated[
        Path | None,
        typer.Option(
            "--csv", dir_okay=False, writable=True, help="Also save all submissions to this CSV file."
        ),
    ] = None,
    include_deleted: Annotated[
        bool, typer.Option("--include-deleted", help="Include submissions marked as deleted.")
    ] = False,
    normalize_headers: Annotated[
        bool,
        typer.Option(
            "--normalize-headers",
            help="Write CSV headers as short snake_case names (lowercase, no special characters, "
            "at most 20 characters), e.g. `submission_id`.",
        ),
    ] = False,
) -> None:
    """Show a summary of a form's submissions, optionally exporting them to CSV."""
    try:
        with get_client(ctx) as client, err_console.status("Fetching submissions…"):
            form_id = client.resolve_form_id(form)
            details = client.get_form(form_id)
            submissions = list(client.iter_submissions(form_id))
            questions = client.get_form_questions(form_id) if csv_path else {}
    except JotformError as e:
        err_console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1) from None

    if not include_deleted:
        submissions = [s for s in submissions if s.get("status") != "DELETED"]
    summary = subs.summarize(submissions)

    overview = Table.grid(padding=(0, 2))
    overview.add_column(style="bold")
    overview.add_column()
    overview.add_row("Form", Text(f"{details.get('title', '')} ({form_id})"))
    overview.add_row("Form status", Text(details.get("status", "")))
    overview.add_row("Submissions", str(summary.total))
    overview.add_row("Unread", str(summary.unread))
    overview.add_row("Flagged", str(summary.flagged))
    overview.add_row("First", summary.first or "—")
    overview.add_row("Latest", summary.last or "—")
    # Documented statuses are ACTIVE and OVERQUOTA (over the plan's submission limit).
    if set(summary.by_status) - {"ACTIVE"}:
        overview.add_row("By status", ", ".join(f"{k}: {v}" for k, v in sorted(summary.by_status.items())))
    console.print(overview)

    if summary.by_month:
        months = Table(title="Last 12 months")
        months.add_column("Month")
        months.add_column("Count", justify="right")
        for month, count in sorted(summary.by_month.items())[-12:]:
            months.add_row(month, str(count))
        console.print(months)

    if csv_path:
        subs.write_csv(csv_path, questions, submissions, normalize=normalize_headers)
        console.print(f"Saved {summary.total} submissions to [bold]{escape(str(csv_path))}[/bold]")


@submissions_app.command("delete")
def delete_submissions(
    ctx: typer.Context,
    form: Annotated[
        str, typer.Argument(help="Jotform form ID or exact form title (see `jotctl forms list`).")
    ],
    purge: Annotated[
        bool,
        typer.Option(
            "--purge",
            help="Also remove the form's trashed submissions for good, including the ones just deleted.",
        ),
    ] = False,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Skip typing the form ID to confirm. Use with care.")
    ] = False,
    backup_dir: Annotated[
        Path,
        typer.Option(file_okay=False, help="Before deleting, save the affected submissions here as JSON."),
    ] = backups.DEFAULT_DIR,
) -> None:
    """Delete every submission on a form, moving them to its trash; --purge empties the trash too.

    Shows what will be deleted and asks you to type the form ID before doing anything, and
    saves every affected submission to a JSON backup first.
    """
    moved = purged = 0
    try:
        with get_client(ctx) as client:
            with err_console.status("Fetching submissions…"):
                form_id = client.resolve_form_id(form)
                title = client.get_form(form_id).get("title", "")
                submissions = list(client.iter_submissions(form_id))
            active = [s for s in submissions if s.get("status") != "DELETED"]
            trashed = [s for s in submissions if s.get("status") == "DELETED"]
            affected = active + trashed if purge else active
            # One DELETE per active submission, plus one per trashed one (including those just moved).
            needed = len(active) + (len(affected) if purge else 0)
            if not needed:
                console.print(Text(f"Nothing to delete on {title} ({form_id})."))
                return

            dates = sorted(s["created_at"] for s in affected if s.get("created_at"))
            overview = Table.grid(padding=(0, 2))
            overview.add_column(style="bold")
            overview.add_column()
            overview.add_row("Form", Text(f"{title} ({form_id})"))
            overview.add_row("Submissions", f"{len(active)} → moved to the trash")
            overview.add_row(
                "Already in trash",
                f"{len(trashed)} → "
                + ("permanently deleted" if purge else "left as is (use --purge to remove)"),
            )
            if purge:
                overview.add_row("Then", f"{len(active)} just moved → permanently deleted")
            if dates:
                overview.add_row("Submitted", f"{dates[0]} to {dates[-1]}")
            overview.add_row(
                "API requests",
                f"{needed}"
                + (f" of {client.limit_left:,} left today" if client.limit_left is not None else ""),
            )
            console.print(overview)

            if client.limit_left is not None and client.limit_left < needed:
                err_console.print(
                    f"[red]Error:[/red] this needs {needed} API requests but only {client.limit_left} are left "
                    "today. Nothing was deleted."
                )
                raise typer.Exit(1)

            if not yes:
                what = "permanently delete" if purge else "delete"
                answer = typer.prompt(
                    f"This will {what} {len(affected)} submission(s). Type the form ID ({form_id}) to confirm",
                    default="",
                    show_default=False,
                )
                if answer.strip() != form_id:
                    console.print("No submissions deleted.")
                    return

            try:
                backup = backups.save_json(affected, f"{form_id}-submissions", backup_dir)
            except OSError as e:
                raise JotformError(f"couldn't save a backup, so nothing was deleted: {e}") from None
            console.print(Text(f"Backed up {len(affected)} submission(s) to {backup}"))

            with err_console.status("Deleting…") as status:
                for s in active:
                    client.delete_submission(s["id"])
                    moved += 1
                    status.update(f"Moving to the trash… {moved}/{len(active)}")
                if purge:
                    trash = [s for s in client.iter_submissions(form_id) if s.get("status") == "DELETED"]
                    for s in trash:
                        client.delete_submission(s["id"])
                        purged += 1
                        status.update(f"Removing from the trash… {purged}/{len(trash)}")
                    remaining = [s for s in client.iter_submissions(form_id) if s.get("status") == "DELETED"]
    except JotformError as e:
        err_console.print(f"[red]Error:[/red] {escape(str(e))}")
        if moved or purged:
            err_console.print(
                f"Before the error: {moved} moved to the trash, {purged} removed from the trash."
            )
        raise typer.Exit(1) from None

    if not purge:
        console.print(
            f"Moved {moved} submission(s) to the trash. They can be restored from the form's Inbox."
        )
        return
    if remaining:
        err_console.print(
            f"[yellow]Warning:[/yellow] {len(remaining)} submission(s) are still in the trash, so Jotform didn't "
            "remove them for good. Empty the trash from the form's Inbox in Jotform."
        )
        raise typer.Exit(1)
    console.print(f"Removed {purged} submission(s) for good; the form's trash is empty.")


@reports_app.command("run")
def run_report(
    ctx: typer.Context,
    report_path: Annotated[
        Path,
        typer.Argument(exists=True, dir_okay=False, readable=True, help="YAML report: `form` and `query`."),
    ],
    csv_out: Annotated[
        Path | None,
        typer.Option(
            "--csv", dir_okay=False, writable=True, help="Write the result to this CSV file instead."
        ),
    ] = None,
    from_csv: Annotated[
        Path | None,
        typer.Option(
            "--from-csv",
            exists=True,
            dir_okay=False,
            readable=True,
            help="Query this saved export (written with --normalize-headers) instead of fetching submissions.",
        ),
    ] = None,
    include_deleted: Annotated[
        bool, typer.Option("--include-deleted", help="Include submissions marked as deleted.")
    ] = False,
) -> None:
    """Run a saved report: a DuckDB query over a form's submissions, fetched from Jotform.

    The query sees a `submissions` table with the same snake_case columns as
    `submissions summary --csv FILE --normalize-headers`, and may use `{{ ... }}` date templates.
    """
    try:
        report = reports.load_report(report_path)
        sql = reports.render_query(report)
        if from_csv:
            result = reports.run_query(sql, from_csv)
        else:
            with get_client(ctx) as client, err_console.status("Fetching submissions…"):
                form_id = client.resolve_form_id(report.form)
                questions = client.get_form_questions(form_id)
                submissions = list(client.iter_submissions(form_id))
            if not include_deleted:
                submissions = [s for s in submissions if s.get("status") != "DELETED"]
            with tempfile.TemporaryDirectory() as tmp:
                export = Path(tmp) / "submissions.csv"
                subs.write_csv(export, questions, submissions, normalize=True)
                result = reports.run_query(sql, export)
    except (JotformError, reports.ReportError) as e:
        err_console.print(f"[red]Error:[/red] {escape(str(e))}")
        raise typer.Exit(1) from None

    if csv_out:
        with csv_out.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(result.columns)
            writer.writerows([reports.format_value(v) for v in row] for row in result.rows)
        console.print(Text(f"Wrote {len(result.rows)} row(s) to {csv_out}"))
        return

    table = Table(title=f"{escape(report_path.name)} ({len(result.rows)} rows)")
    for column in result.columns:
        table.add_column(Text(column))
    for row in result.rows:
        table.add_row(*(Text(reports.format_value(v)) for v in row))
    console.print(table)


@questions_app.command("list")
def list_questions(
    ctx: typer.Context,
    form: Annotated[
        str, typer.Argument(help="Jotform form ID or exact form title (see `jotctl forms list`).")
    ],
    inputs_only: Annotated[
        bool, typer.Option("--inputs-only", help="Hide layout elements such as headings, text and buttons.")
    ] = False,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print every property of each question as JSON instead of a table.")
    ] = False,
) -> None:
    """List the questions on a form, in the order they appear."""
    try:
        with get_client(ctx) as client:
            form_id = client.resolve_form_id(form)
            questions = ordered_questions(client.get_form_questions(form_id), inputs_only=inputs_only)
    except JotformError as e:
        err_console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1) from None

    if as_json:
        typer.echo(json.dumps([q.raw for q in questions], indent=2))
        return

    if not questions:
        console.print("No questions found.")
        return

    table = Table(title=f"Questions on form {form_id} ({len(questions)})")
    table.add_column("QID", justify="right", no_wrap=True)
    table.add_column("Type", no_wrap=True)
    table.add_column("Name", no_wrap=True)
    table.add_column("Label")
    table.add_column("Required", justify="center")
    table.add_column("Options")
    for q in questions:
        table.add_row(
            q.qid,
            q.short_type,
            Text(q.name),
            Text(q.label),
            "yes" if q.required else "",
            Text(", ".join(q.options)),
            style=None if q.is_input else "dim",
        )
    console.print(table)
