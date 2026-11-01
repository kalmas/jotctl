"""Command-line interface for Jotform administration."""

import csv
import json
import logging
import sys
import tempfile
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, ClassVar

import typer
import yaml
from dotenv import load_dotenv
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from rich.text import Text
from typer.core import TyperGroup

from jotctl import availability, backups, manifests, reports
from jotctl import conditions as conds
from jotctl import submissions as subs
from jotctl.client import FormOrderBy, FormStatus, JotformClient, JotformError, Region
from jotctl.questions import ordered_questions

load_dotenv()


def hoist_global_options(args: list[str], params: list[Any]) -> list[str]:
    """Move the root command's options to the front so they work anywhere on the command line.

    Click only parses an option at the level that declares it, so `jotctl get forms -vQ` would
    otherwise fail. Combined short flags are split: in `-vy`, `-v` is global and `-y` stays
    with the subcommand. Everything after `--` is left alone.

    Args:
        args (list[str]): Command-line arguments, without the program name.
        params (list[Any]): The root command's Click parameters.

    Returns:
        list[str]: The same arguments, with global options (and their values) first.
    """
    # Typer bundles its own copy of Click, so match options by type name, not isinstance.
    options = [p for p in params if getattr(p, "param_type_name", None) == "option"]
    takes_value = {name for o in options if not (o.is_flag or o.count) for name in o.opts + o.secondary_opts}
    flags = {name for o in options if o.is_flag or o.count for name in o.opts + o.secondary_opts}
    short_flags = {name[1] for name in flags if len(name) == 2}

    hoisted: list[str] = []
    rest: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            rest.extend(args[i:])
            break
        name = arg.split("=", 1)[0]
        if name in flags:
            hoisted.append(arg)
        elif name in takes_value:
            hoisted.append(arg)
            if "=" not in arg and i + 1 < len(args):
                i += 1
                hoisted.append(args[i])
        elif arg.startswith("-") and not arg.startswith("--") and len(arg) > 2 and arg[1:].isalpha():
            # A cluster of short flags such as -vQ or -vy.
            ours = "".join(c for c in arg[1:] if c in short_flags)
            theirs = "".join(c for c in arg[1:] if c not in short_flags)
            if ours:
                hoisted.append(f"-{ours}")
            if theirs:
                rest.append(f"-{theirs}")
        else:
            rest.append(arg)
        i += 1
    return hoisted + rest


class GlobalOptionsGroup(TyperGroup):
    """Root command group whose options may appear anywhere, e.g. after the subcommand."""

    def parse_args(self, ctx: typer.Context, args: list[str]) -> list[str]:
        return super().parse_args(ctx, hoist_global_options(args, self.params))

    COMMAND_ORDER = ("get", "describe", "apply", "diff", "delete", "report")

    def list_commands(self, ctx: typer.Context) -> list[str]:
        names = super().list_commands(ctx)
        return sorted(
            names, key=lambda n: self.COMMAND_ORDER.index(n) if n in self.COMMAND_ORDER else len(names)
        )


app = typer.Typer(help="Automate Jotform form administration.", no_args_is_help=True, cls=GlobalOptionsGroup)


class AliasGroup(TyperGroup):
    """Command group that also accepts the singular, plural and short names of its resources."""

    ALIASES: ClassVar[dict[str, tuple[str, ...]]] = {
        "form": ("forms",),
        "forms": ("form",),
        "question": ("questions",),
        "q": ("questions",),
        "condition": ("conditions",),
        "submission": ("submissions",),
        "sub": ("submissions",),
        "subs": ("submissions",),
    }

    def get_command(self, ctx: typer.Context, cmd_name: str) -> Any:
        command = super().get_command(ctx, cmd_name)
        for alias in () if command else self.ALIASES.get(cmd_name, ()):
            command = super().get_command(ctx, alias)
            if command:
                break
        return command


get_app = typer.Typer(
    help="List resources: forms, questions, conditions, submissions (singular names and q/sub work too).",
    no_args_is_help=True,
    cls=AliasGroup,
)
describe_app = typer.Typer(
    help="Show details about a form or its submissions.", no_args_is_help=True, cls=AliasGroup
)
delete_app = typer.Typer(help="Delete resources.", no_args_is_help=True, cls=AliasGroup)
app.add_typer(get_app, name="get")
app.add_typer(describe_app, name="describe")
app.add_typer(delete_app, name="delete")

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
            "--show-quota",
            "-Q",
            help="After the command, print how many API requests you have left today.",
        ),
    ] = False,
    verbose: Annotated[
        int,
        typer.Option(
            "--verbose",
            "-v",
            count=True,
            help="Log each API request (method, path, status) to stderr; -vv also logs request and "
            "response bodies.",
        ),
    ] = 0,
) -> None:
    """Initialize the Jotform CLI with the given API key and region.

    Args:
        ctx (typer.Context): Typer context.
        api_key (str | None): Jotform API key.
        region (Region): Jotform data region.
        show_quota (bool): Report the remaining daily API requests when the command finishes.
        verbose (int): 1 logs each request, 2 also logs bodies.
    """
    configure_logging(verbose)
    ctx.obj = Settings(api_key=api_key, region=region, show_quota=show_quota)


class ConsoleLogHandler(logging.Handler):
    """Write log lines to the stderr console as plain, unwrapped text.

    Going through the console keeps lines clean while a spinner is showing, and not
    wrapping keeps logged JSON bodies intact for copying.
    """

    def emit(self, record: logging.LogRecord) -> None:
        err_console.print(self.format(record), markup=False, highlight=False, soft_wrap=True)


def configure_logging(verbosity: int) -> None:
    """Send API request logs to stderr: none, requests (1) or requests and bodies (2+).

    Args:
        verbosity (int): Number of -v flags.
    """
    http_logger = logging.getLogger("jotctl.http")
    http_logger.handlers.clear()
    http_logger.propagate = False
    if verbosity <= 0:
        http_logger.setLevel(logging.WARNING)
        return
    handler = ConsoleLogHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    http_logger.addHandler(handler)
    http_logger.setLevel(logging.DEBUG if verbosity >= 2 else logging.INFO)


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


class Output(StrEnum):
    """Output formats for `-o`, as in kubectl."""

    TABLE = "table"
    WIDE = "wide"
    JSON = "json"
    YAML = "yaml"
    CSV = "csv"
    NAME = "name"


def output_option(*allowed: Output) -> Any:
    """A `-o/--output` option limited to `allowed` formats."""
    return typer.Option(
        "--output",
        "-o",
        help=f"Output format: {', '.join(allowed)}.",
        show_default=True,
        case_sensitive=False,
    )


def check_output(output: Output, *allowed: Output) -> None:
    """Exit with a usage error if `output` isn't one of the formats this command supports."""
    if output not in allowed:
        raise typer.BadParameter(
            f"'{output}' isn't supported here; use one of: {', '.join(allowed)}.", param_hint="'-o'"
        )


FORM_OPTION = typer.Option("--form", help="Form ID or exact title (see `jotctl get forms`).")


@dataclass
class Listing:
    """What a command shows, in every output format it supports."""

    title: str
    columns: list[str]
    rows: list[list[Any]]
    data: Any
    """Plain data for -o json and -o yaml."""
    names: list[str] = field(default_factory=list)
    """One identifier per row for -o name."""
    wide_columns: list[str] | None = None
    wide_rows: list[list[Any]] | None = None
    """Extra-detail columns for -o wide; also used for -o csv."""
    row_styles: list[str | None] | None = None
    empty: str = "No resources found."
    show_lines: bool = False


def emit(listing: Listing, output: Output) -> None:
    """Print a listing in the requested format: tables to the console, everything else as plain text."""
    if output is Output.JSON:
        typer.echo(json.dumps(listing.data, indent=2, ensure_ascii=False, default=str))
    elif output is Output.YAML:
        plain = json.loads(json.dumps(listing.data, default=str))
        typer.echo(yaml.safe_dump(plain, sort_keys=False, allow_unicode=True), nl=False)
    elif output is Output.NAME:
        for name in listing.names:
            typer.echo(name)
    elif output is Output.CSV:
        columns, rows = (
            (listing.wide_columns, listing.wide_rows)
            if listing.wide_columns
            else (listing.columns, listing.rows)
        )
        writer = csv.writer(sys.stdout, lineterminator="\n")
        writer.writerow(columns)
        writer.writerows([_plain(cell) for cell in row] for row in rows)
    else:
        wide = output is Output.WIDE and listing.wide_columns is not None
        columns, rows = (listing.wide_columns, listing.wide_rows) if wide else (listing.columns, listing.rows)
        if not rows:
            console.print(listing.empty)
            return
        table = Table(title=f"{listing.title} ({len(rows)})", show_lines=listing.show_lines)
        for column in columns or []:
            table.add_column(Text(column))
        for i, row in enumerate(rows or []):
            style = listing.row_styles[i] if listing.row_styles else None
            table.add_row(*(cell if isinstance(cell, Text) else Text(str(cell)) for cell in row), style=style)
        console.print(table)


def _plain(cell: Any) -> str:
    return cell.plain if isinstance(cell, Text) else str(cell)


def fail(e: Exception) -> typer.Exit:
    """Print an error and return the Exit to raise."""
    err_console.print(f"[red]Error:[/red] {escape(str(e))}")
    return typer.Exit(1)


# ---------------------------------------------------------------------------------------------
# get


@get_app.command("forms")
def get_forms(
    ctx: typer.Context,
    limit: Annotated[int, typer.Option(min=1, max=1000, help="Max number of forms to return.")] = 20,
    offset: Annotated[int, typer.Option(min=0, help="Number of forms to skip.")] = 0,
    status: Annotated[FormStatus | None, typer.Option(help="Only show forms with this status.")] = None,
    order_by: Annotated[FormOrderBy | None, typer.Option(help="Field to sort by.")] = None,
    output: Annotated[Output, output_option(*Output)] = Output.TABLE,
) -> None:
    """List forms in your Jotform account."""
    try:
        with get_client(ctx) as client:
            forms = client.list_forms(limit=limit, offset=offset, status=status, order_by=order_by)
    except JotformError as e:
        raise fail(e) from None

    def row(f: dict[str, Any]) -> list[str]:
        return [f.get("id", ""), f.get("title", ""), f.get("status", ""), str(f.get("count", ""))]

    emit(
        Listing(
            title="Forms",
            columns=["ID", "Title", "Status", "Submissions"],
            rows=[row(f) for f in forms],
            wide_columns=["ID", "Title", "Status", "Submissions", "Last submission", "Created", "Updated"],
            wide_rows=[
                [*row(f), f.get("last_submission") or "", f.get("created_at", ""), f.get("updated_at") or ""]
                for f in forms
            ],
            data=forms,
            names=[f.get("id", "") for f in forms],
            empty="No forms found.",
        ),
        output,
    )


@get_app.command("questions")
def get_questions(
    ctx: typer.Context,
    form: Annotated[str, FORM_OPTION],
    inputs_only: Annotated[
        bool, typer.Option("--inputs-only", help="Hide layout elements such as headings, text and buttons.")
    ] = False,
    output: Annotated[Output, output_option(*Output)] = Output.TABLE,
) -> None:
    """List a form's questions (fields) in the order they appear."""
    try:
        with get_client(ctx) as client:
            form_id = client.resolve_form_id(form)
            questions = ordered_questions(client.get_form_questions(form_id), inputs_only=inputs_only)
    except JotformError as e:
        raise fail(e) from None

    emit(
        Listing(
            title=f"Questions on form {form_id}",
            columns=["QID", "Type", "Name", "Label", "Required", "Options"],
            rows=[
                [q.qid, q.short_type, q.name, q.label, "yes" if q.required else "", ", ".join(q.options)]
                for q in questions
            ],
            row_styles=[None if q.is_input else "dim" for q in questions],
            data=[q.raw for q in questions],
            names=[q.name or q.qid for q in questions],
            empty="No questions found.",
        ),
        output,
    )


@get_app.command("conditions")
def get_conditions(
    ctx: typer.Context,
    form: Annotated[str, FORM_OPTION],
    output: Annotated[Output, output_option(*Output)] = Output.TABLE,
) -> None:
    """List a form's conditional logic as IF → THEN: show/hide rules, calculations, thank-you messages."""
    try:
        with get_client(ctx) as client:
            form_id = client.resolve_form_id(form)
            conditions = conds.parse_conditions(client.get_form_properties(form_id).get("conditions"))
            readable = output in (Output.TABLE, Output.WIDE, Output.CSV)
            questions = client.get_form_questions(form_id) if conditions and readable else {}
    except JotformError as e:
        raise fail(e) from None

    names = {q.qid: q.name or f"#{q.qid}" for q in ordered_questions(questions)}
    rows = [
        [
            str(i),
            c.id,
            c.type,
            conds.describe_terms(c, names),
            "\n".join(conds.describe_action(a, c.type, names) for a in c.actions),
        ]
        for i, c in enumerate(conditions, 1)
    ]
    emit(
        Listing(
            title=f"Conditions on form {form_id}",
            columns=["#", "ID", "Type", "If", "Then"],
            rows=rows,
            data=[c.to_json() for c in conditions],
            names=[c.id for c in conditions],
            empty="No conditions found.",
            show_lines=True,
        ),
        output,
    )


@get_app.command("submissions")
def get_submissions(
    ctx: typer.Context,
    form: Annotated[str, FORM_OPTION],
    include_deleted: Annotated[
        bool, typer.Option("--include-deleted", help="Include submissions marked as deleted.")
    ] = False,
    normalize_headers: Annotated[
        bool,
        typer.Option(
            "--normalize-headers",
            help="Use short snake_case column names (lowercase, no special characters, at most 20 "
            "characters), e.g. `submission_id`, as reports do.",
        ),
    ] = False,
    output: Annotated[Output, output_option(*Output)] = Output.TABLE,
) -> None:
    """List a form's submissions; -o wide or -o csv include every answer.

    Export to a file with `-o csv > submissions.csv`.
    """
    try:
        with get_client(ctx) as client, err_console.status("Fetching submissions…"):
            form_id = client.resolve_form_id(form)
            questions = client.get_form_questions(form_id)
            submissions = list(client.iter_submissions(form_id))
    except JotformError as e:
        raise fail(e) from None

    if not include_deleted:
        submissions = [s for s in submissions if s.get("status") != "DELETED"]
    headers, rows = subs.to_rows(questions, submissions, normalize=normalize_headers)
    emit(
        Listing(
            title=f"Submissions on form {form_id}",
            columns=headers[:3],
            rows=[row[:3] for row in rows],
            wide_columns=headers,
            wide_rows=rows,
            data=submissions,
            names=[s.get("id", "") for s in submissions],
            empty="No submissions found.",
        ),
        output,
    )


# ---------------------------------------------------------------------------------------------
# describe


def _grid(rows: list[tuple[str, Any]]) -> Table:
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold")
    grid.add_column()
    for label, value in rows:
        grid.add_row(label, value if isinstance(value, Text) else Text(str(value)))
    return grid


@describe_app.command("form")
def describe_form(
    ctx: typer.Context,
    form: Annotated[str, typer.Argument(help="Form ID or exact title (see `jotctl get forms`).")],
    output: Annotated[
        Output, output_option(Output.TABLE, Output.WIDE, Output.JSON, Output.YAML)
    ] = Output.TABLE,
) -> None:
    """Show whether a form is open: its status, disable date, submission limit and closed message."""
    check_output(output, Output.TABLE, Output.WIDE, Output.JSON, Output.YAML)
    try:
        with get_client(ctx) as client:
            form_id = client.resolve_form_id(form)
            properties = client.get_form_properties(form_id)
    except JotformError as e:
        raise fail(e) from None

    info = availability.read_availability(properties)
    if output in (Output.JSON, Output.YAML):
        emit(
            Listing("", [], [], {"id": form_id, "title": properties.get("title", ""), **info.to_json()}),
            output,
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

    console.print(
        _grid(
            [
                ("Form", f"{properties.get('title', '')} ({form_id})"),
                ("Status", Text(info.status, style="green" if info.status == "ENABLED" else "red")),
                ("Disable on date", expires),
                ("Submission limit", limit),
                ("Password protected", "yes" if info.password_protected else "no"),
                ("Closed message", Text(info.message) if info.message else Text("not set", style="dim")),
            ]
        )
    )


@describe_app.command("submissions")
def describe_submissions(
    ctx: typer.Context,
    form: Annotated[str, FORM_OPTION],
    include_deleted: Annotated[
        bool, typer.Option("--include-deleted", help="Include submissions marked as deleted.")
    ] = False,
    output: Annotated[
        Output, output_option(Output.TABLE, Output.WIDE, Output.JSON, Output.YAML)
    ] = Output.TABLE,
) -> None:
    """Summarize a form's submissions: totals, unread, flagged, date range and counts by month."""
    check_output(output, Output.TABLE, Output.WIDE, Output.JSON, Output.YAML)
    try:
        with get_client(ctx) as client, err_console.status("Fetching submissions…"):
            form_id = client.resolve_form_id(form)
            details = client.get_form(form_id)
            submissions = list(client.iter_submissions(form_id))
    except JotformError as e:
        raise fail(e) from None

    if not include_deleted:
        submissions = [s for s in submissions if s.get("status") != "DELETED"]
    summary = subs.summarize(submissions)
    if output in (Output.JSON, Output.YAML):
        data = {
            "id": form_id,
            "title": details.get("title", ""),
            "formStatus": details.get("status", ""),
            "total": summary.total,
            "unread": summary.unread,
            "flagged": summary.flagged,
            "first": summary.first,
            "last": summary.last,
            "byStatus": dict(sorted(summary.by_status.items())),
            "byMonth": dict(sorted(summary.by_month.items())),
        }
        emit(Listing("", [], [], data), output)
        return

    rows: list[tuple[str, Any]] = [
        ("Form", f"{details.get('title', '')} ({form_id})"),
        ("Form status", details.get("status", "")),
        ("Submissions", summary.total),
        ("Unread", summary.unread),
        ("Flagged", summary.flagged),
        ("First", summary.first or "—"),
        ("Latest", summary.last or "—"),
    ]
    # Documented statuses are ACTIVE and OVERQUOTA (over the plan's submission limit).
    if set(summary.by_status) - {"ACTIVE"}:
        rows.append(("By status", ", ".join(f"{k}: {v}" for k, v in sorted(summary.by_status.items()))))
    console.print(_grid(rows))

    if summary.by_month:
        months = Table(title="Last 12 months")
        months.add_column("Month")
        months.add_column("Count", justify="right")
        for month, count in sorted(summary.by_month.items())[-12:]:
            months.add_row(month, str(count))
        console.print(months)


# ---------------------------------------------------------------------------------------------
# apply / diff


FILENAME_HELP = "Manifest file, or a folder of .yaml/.yml manifests. Repeat -f for several."


def manifest_files(paths: list[Path]) -> list[Path]:
    """Expand folders into the YAML files directly inside them, in name order."""
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            found = sorted(p for p in path.iterdir() if p.suffix in (".yaml", ".yml") and p.is_file())
            if not found:
                raise manifests.ManifestError(f"{path} has no .yaml or .yml files")
            files.extend(found)
        else:
            files.append(path)
    return files


@dataclass
class FormPlan:
    """What `apply` will change on one form."""

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


def plan_manifests(client: JotformClient, paths: list[Path]) -> tuple[str, list[FormPlan]]:
    """Load manifests and compare them with the forms; returns a description of the source and the plans."""
    files = manifest_files(paths)
    form_manifests = [m for f in files for m in manifests.load_manifest(f)]
    plans = []
    with err_console.status("Reading forms…"):
        for manifest in form_manifests:
            form_id = client.resolve_form_id(manifest.form)
            questions = client.get_form_questions(form_id)
            properties = (
                client.get_form_properties(form_id) if manifest.conditions or manifest.properties else {}
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
    source = files[0].name if len(files) == 1 else f"{len(files)} files"
    return source, plans


def show_plans(source: str, plans: list[FormPlan]) -> int:
    """Print the old → new table for every planned change; returns the number of changes."""
    total = sum(plan.total for plan in plans)
    if not total:
        console.print("No changes: the forms already match the manifest.")
        return 0
    table = Table(title=f"Changes from {escape(source)} ({total})")
    for column in ["Form", "Target", "Property", "Old", "New"]:
        table.add_column(column, no_wrap=column == "Property")
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
    return total


def apply_plans(client: JotformClient, plans: list[FormPlan], backup_dir: Path) -> int:
    """Send every planned change, checking property and condition changes took; returns how many were applied.

    Raises:
        JotformError: On an API error or a change Jotform didn't apply; `.applied` on the
            exception says how many changes went through first.
    """
    applied = 0
    try:
        for plan in plans:
            by_question: dict[tuple[str, str], dict[str, str]] = {}
            for c in plan.changes:
                by_question.setdefault((c.qid, c.question), {})[c.prop] = c.new
            for (qid, question), props in by_question.items():
                client.update_question(plan.form_id, qid, props)
                applied += len(props)
                console.print(Text(f"Updated {plan.manifest.form} → {question}: {', '.join(props)}"))

            if plan.property_changes:
                client.update_form_properties(plan.form_id, {c.prop: c.new for c in plan.property_changes})
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
    except JotformError as e:
        e.applied = applied  # type: ignore[attr-defined]
        raise
    return applied


FilenamesOption = Annotated[
    list[Path],
    typer.Option("--filename", "-f", exists=True, readable=True, help=FILENAME_HELP),
]


@app.command("apply")
def apply(
    ctx: typer.Context,
    filenames: FilenamesOption,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show what would change and exit without changing anything.")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Apply the changes without asking.")] = False,
    backup_dir: Annotated[
        Path,
        typer.Option(
            file_okay=False,
            help="Before changing a form's conditions, save the original ones here as timestamped JSON.",
        ),
    ] = backups.DEFAULT_DIR,
) -> None:
    """Apply manifests to forms: show every old → new value, ask, then change the forms.

    A manifest sets form properties, question properties and condition terms; values may be
    `{template: ...}` or `{script: ...}`. Condition changes are sent as the form's whole
    condition list, backed up first and re-read afterwards to check they took.
    """
    try:
        with get_client(ctx) as client:
            source, plans = plan_manifests(client, filenames)
            total = show_plans(source, plans)
            if not total:
                return
            if dry_run:
                console.print("Dry run: no changes made.")
                return
            if not yes and not typer.confirm(f"Apply {total} change(s)?"):
                console.print("No changes made.")
                return
            applied = apply_plans(client, plans, backup_dir)
    except manifests.ManifestError as e:
        raise fail(e) from None
    except JotformError as e:
        err_console.print(f"[red]Error:[/red] {escape(str(e))}")
        if applied := getattr(e, "applied", 0):
            err_console.print(f"{applied} change(s) were applied before the error.")
        raise typer.Exit(1) from None
    console.print(f"Applied {applied} change(s).")


@app.command("diff")
def diff(ctx: typer.Context, filenames: FilenamesOption) -> None:
    """Show what `apply` would change, without changing anything.

    Exits 0 when the forms already match, 1 when there are differences (as kubectl diff does).
    """
    try:
        with get_client(ctx) as client:
            source, plans = plan_manifests(client, filenames)
    except (manifests.ManifestError, JotformError) as e:
        err_console.print(f"[red]Error:[/red] {escape(str(e))}")
        raise typer.Exit(2) from None
    if show_plans(source, plans):
        raise typer.Exit(1)


# ---------------------------------------------------------------------------------------------
# delete


@delete_app.command("submissions")
def delete_submissions(
    ctx: typer.Context,
    form: Annotated[str, FORM_OPTION],
    all_: Annotated[
        bool, typer.Option("--all", help="Required: delete every submission on the form.")
    ] = False,
    purge: Annotated[
        bool,
        typer.Option(
            "--purge",
            help="Also remove the form's trashed submissions for good, including the ones just deleted.",
        ),
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show what would be deleted and exit without deleting anything.")
    ] = False,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Skip typing the form ID to confirm. Use with care.")
    ] = False,
    backup_dir: Annotated[
        Path,
        typer.Option(file_okay=False, help="Before deleting, save the affected submissions here as JSON."),
    ] = backups.DEFAULT_DIR,
) -> None:
    """Delete every submission on a form (--all), moving them to its trash; --purge empties the trash too.

    Shows what will be deleted and asks you to type the form ID before doing anything, and
    saves every affected submission to a JSON backup first.
    """
    if not all_:
        err_console.print("[red]Error:[/red] pass --all to delete every submission on the form.")
        raise typer.Exit(1)
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
            rows: list[tuple[str, Any]] = [
                ("Form", f"{title} ({form_id})"),
                ("Submissions", f"{len(active)} → moved to the trash"),
                (
                    "Already in trash",
                    f"{len(trashed)} → "
                    + ("permanently deleted" if purge else "left as is (use --purge to remove)"),
                ),
            ]
            if purge:
                rows.append(("Then", f"{len(active)} just moved → permanently deleted"))
            if dates:
                rows.append(("Submitted", f"{dates[0]} to {dates[-1]}"))
            left = f" of {client.limit_left:,} left today" if client.limit_left is not None else ""
            rows.append(("API requests", f"{needed}{left}"))
            console.print(_grid(rows))

            if client.limit_left is not None and client.limit_left < needed:
                err_console.print(
                    f"[red]Error:[/red] this needs {needed} API requests but only {client.limit_left} are left "
                    "today. Nothing was deleted."
                )
                raise typer.Exit(1)

            if dry_run:
                console.print("Dry run: no submissions deleted.")
                return

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


# ---------------------------------------------------------------------------------------------
# report


@app.command("report")
def report(
    ctx: typer.Context,
    filename: Annotated[
        Path,
        typer.Option(
            "--filename",
            "-f",
            exists=True,
            dir_okay=False,
            readable=True,
            help="YAML report: `form` and `query`.",
        ),
    ],
    output: Annotated[
        Output, output_option(Output.TABLE, Output.WIDE, Output.CSV, Output.JSON, Output.YAML)
    ] = Output.TABLE,
    from_csv: Annotated[
        Path | None,
        typer.Option(
            "--from-csv",
            exists=True,
            dir_okay=False,
            readable=True,
            help="Query this saved export (`get submissions -o csv --normalize-headers`) instead of fetching.",
        ),
    ] = None,
    include_deleted: Annotated[
        bool, typer.Option("--include-deleted", help="Include submissions marked as deleted.")
    ] = False,
) -> None:
    """Run a saved report: a DuckDB query over a form's submissions, fetched from Jotform.

    The query sees a `submissions` table with the same snake_case columns as
    `get submissions -o csv --normalize-headers`, and may use `{{ ... }}` date templates.
    Save the result with `-o csv > file.csv`.
    """
    check_output(output, Output.TABLE, Output.WIDE, Output.CSV, Output.JSON, Output.YAML)
    try:
        saved = reports.load_report(filename)
        sql = reports.render_query(saved)
        if from_csv:
            result = reports.run_query(sql, from_csv)
        else:
            with get_client(ctx) as client, err_console.status("Fetching submissions…"):
                form_id = client.resolve_form_id(saved.form)
                questions = client.get_form_questions(form_id)
                submissions = list(client.iter_submissions(form_id))
            if not include_deleted:
                submissions = [s for s in submissions if s.get("status") != "DELETED"]
            with tempfile.TemporaryDirectory() as tmp:
                export = Path(tmp) / "submissions.csv"
                subs.write_csv(export, questions, submissions, normalize=True)
                result = reports.run_query(sql, export)
    except (JotformError, reports.ReportError) as e:
        raise fail(e) from None

    rows = [[reports.format_value(v) for v in row] for row in result.rows]
    emit(
        Listing(
            title=filename.name,
            columns=result.columns,
            rows=rows,
            data=[dict(zip(result.columns, row, strict=True)) for row in result.rows],
            empty="No rows.",
        ),
        output,
    )
