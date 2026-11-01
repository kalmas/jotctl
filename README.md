# jotctl

```
     ██╗ ██████╗ ████████╗ ██████╗████████╗██╗
     ██║██╔═══██╗╚══██╔══╝██╔════╝╚══██╔══╝██║
     ██║██║   ██║   ██║   ██║        ██║   ██║
██   ██║██║   ██║   ██║   ██║        ██║   ██║
╚█████╔╝╚██████╔╝   ██║   ╚██████╗   ██║   ███████╗
 ╚════╝  ╚═════╝    ╚═╝    ╚═════╝   ╚═╝   ╚══════╝
   kubectl-style control for your Jotform forms
```

A command-line tool for automating Jotform form administration.

## Setup

```sh
poetry install
cp .env.example .env  # Set API Key in .env file; Jotform → Settings → API (Full Access to change forms)
mkdir -p manifests && cp -n manifests-example/*.yaml manifests/  # Optional: example manifests for `apply -f` (manifests/ is gitignored)
mkdir -p reports && cp -n reports-example/*.yaml reports/        # Optional: example reports for `report -f` (reports/ is gitignored)
```

EU or HIPAA accounts also need `export JOTFORM_REGION=eu` (or `hipaa`), or pass `--region`.

## Usage

Commands follow kubectl: `get`, `describe`, `apply`, `diff`, `delete`, plus `report`. Forms are named by
ID or exact title. Resources have singular and short names too (`form`, `question`/`q`, `submission`/`sub`).

```sh
poetry run jotctl --help

# Global options (--api-key, --region, -v/-vv, -Q) can go anywhere: `jotctl get forms -vQ` works.
# -o picks the output format: table (default), wide, json, yaml, csv or name, where a command supports it.

# List forms
poetry run jotctl get forms
poetry run jotctl get forms --limit 50 --status ENABLED --order-by created_at -o wide
poetry run jotctl get forms -o json | jq '.[].title'
poetry run jotctl get forms -o name                    # just the IDs

# Show whether a form is open: status, disable-on-date, submission limit, closed message
poetry run jotctl describe form 'Form Name'
poetry run jotctl describe form 251234567890 -o yaml

# List a form's questions (QIDs and names are what manifests use)
poetry run jotctl get questions --form 251234567890
poetry run jotctl get questions --form 251234567890 --inputs-only
poetry run jotctl get questions --form 251234567890 -o json   # every property of each question

# Show a form's conditional logic (show/hide rules, calculations, thank-you messages) as IF → THEN
poetry run jotctl get conditions --form 'Form Name'
poetry run jotctl get conditions --form 251234567890 -o json   # decoded terms and actions

# A form's emails: notifications and autoresponders
poetry run jotctl get emails --form 'Form Name'                      # name, type, enabled, to, from, subject
poetry run jotctl describe email Autoresponder --form 'Form Name'    # every setting, body as plain text

# Submissions: summarize, list and export
poetry run jotctl describe submissions --form 251234567890               # totals, unread, flagged, by month
poetry run jotctl get submissions --form 251234567890                    # ID, submitted at, status
poetry run jotctl get submissions --form 251234567890 -o wide            # every answer
poetry run jotctl get submissions --form 251234567890 -o csv > submissions.csv
poetry run jotctl get submissions --form 251234567890 -o csv --normalize-headers > submissions.csv   # snake_case headers, max 20 chars
poetry run jotctl get submissions --form 251234567890 --include-deleted -o json

# Apply manifests: update form properties, question properties, condition terms and emails.
# See manifests-example/weekly-reopen.yaml for a commented example. Per form:
#   properties → property (e.g. status, expireDate, limitSubmission, messageOfLimitedForm; see `describe form -o yaml`)
#   questions → question → property
#   conditions → condition ID → terms → question → property
#   emails → email name → property (e.g. subject, body, to, disabled; true/false become 1/0)
# Values may be {template: ...}: Jinja with date helpers (today, monday … sunday = the next such day,
# weeks(n), days(n), <day>.at('HH:MM') or <day>.at('HH:MM', 'America/New_York') for a time zone,
# | date('%b %-d'); %:z prints the offset, e.g. -05:00), e.g. "{{ (saturday + weeks(1)) | date('%Y-%m-%d') }}".
# {script: ...} runs a shell command instead.
# Condition IDs are in the ID column of `get conditions`. Before changing a form's conditions or emails,
# the originals are saved to data/backups/<form ID>-conditions|emails-<timestamp>.json (--backup-dir to change).
poetry run jotctl diff -f manifests/weekly-reopen.yaml    # show old → new; exits 1 if anything would change
poetry run jotctl apply -f manifests/weekly-reopen.yaml   # show old → new, ask, then apply
poetry run jotctl apply -f manifests/                     # every .yaml/.yml in the folder
poetry run jotctl apply -f manifests/weekly-reopen.yaml --dry-run   # show and exit; -y applies without asking

# Delete every submission on a form (moved to its trash, restorable from the Inbox). Shows what will be
# deleted, asks you to type the form ID, and first saves the submissions to data/backups/ as JSON.
poetry run jotctl delete submissions --form 'Form Name' --all
poetry run jotctl delete submissions --form 'Form Name' --all --purge     # also remove them from the trash for good
poetry run jotctl delete submissions --form 'Form Name' --all --dry-run   # show what would be deleted

# Run a saved report: DuckDB SQL over a form's submissions, fetched live (see reports-example/appointments.yaml).
# Columns are the --normalize-headers names; the query may use the same {{ ... }} date templates as manifests.
poetry run jotctl report -f reports/appointments.yaml
poetry run jotctl report -f reports/appointments.yaml -o csv > appointments.csv
poetry run jotctl report -f reports/appointments.yaml --from-csv submissions.csv   # query a saved export instead

# Report how many API requests you have left today
poetry run jotctl get forms -Q             # or --show-quota

# Log API requests to stderr
poetry run jotctl describe form 'Form Name' -v    # GET /form/251234567890/properties 200
poetry run jotctl describe form 'Form Name' -vv   # also request and response bodies (passwords redacted)
```

## Routines

```sh
poetry run jotctl get forms

poetry run jotctl get submissions --form 251234567890 -o csv --normalize-headers > data/submissions$(date +%Y-%m-%d).csv

poetry run jotctl report -f reports/shopper-list.yaml

poetry run jotctl report -f reports/shopper-list.yaml -o csv > data/shopper_list$(date +%Y-%m-%d)_clean.csv
```

## Development

```sh
make format    # ruff format + autofix lint, yamlfix
make lint      # check Python and YAML formatting and lint without changing files
make test      # pytest
```

Notes for coding agents (where the Jotform API docs are, undocumented API behavior, privacy rules)
are in `AGENTS.md`.
