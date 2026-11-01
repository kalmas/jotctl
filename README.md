# jotctl

A command-line tool for automating Jotform form administration.

## Setup

```sh
poetry install
cp .env.example .env  # Set API Key in .env file; Jotform → Settings → API (Full Access to change forms)
mkdir -p manifests && cp -n manifests-example/*.yaml manifests/  # Optional: example manifests for `forms apply` (manifests/ is gitignored)
mkdir -p reports && cp -n reports-example/*.yaml reports/        # Optional: example reports for `reports run` (reports/ is gitignored)
```

EU or HIPAA accounts also need `export JOTFORM_REGION=eu` (or `hipaa`), or pass `--region`.

## Usage

```sh
poetry run jotctl --help

# Global options (--api-key, --region, -v/-vv, -Q) can go anywhere: `jotctl forms list -vQ` works.

# List forms
poetry run jotctl forms list
poetry run jotctl forms list --limit 50 --status ENABLED --order-by created_at
poetry run jotctl forms list --json | jq '.[].title'

# Show whether a form is open: status, disable-on-date, submission limit, closed message
poetry run jotctl forms status 'Form Name'
poetry run jotctl forms status 251234567890 --json

# Show a form's conditional logic (show/hide rules, calculations, thank-you messages) as IF → THEN
poetry run jotctl forms conditions 'Form Name'
poetry run jotctl forms conditions 251234567890 --json   # decoded terms and actions

# Apply a YAML manifest: update form properties, question properties and condition terms.
# See manifests-example/weekly-reopen.yaml for a commented example. Per form:
#   properties → property (e.g. status, expireDate, limitSubmission, messageOfLimitedForm; see `forms status`)
#   questions → question → property
#   conditions → condition ID → terms → question → property
# Values may be {template: ...}: Jinja with date helpers (today, monday … sunday = the next such day,
# weeks(n), days(n), <day>.at('HH:MM'), | date('%b %-d')), e.g. "{{ (saturday + weeks(1)) | date('%Y-%m-%d') }}".
# {script: ...} runs a shell command instead.
# Shows each old → new value and asks before sending anything. --yes skips the prompt.
# Condition IDs are in the ID column of `jotctl forms conditions`. Before changing a form's conditions,
# the originals are saved to data/backups/<form ID>-conditions-<timestamp>.json (--backup-dir to change).
poetry run jotctl forms apply manifests/weekly-reopen.yaml

# List the questions (fields) on a form (QIDs are what you'll use to edit questions)
poetry run jotctl questions list 251234567890
poetry run jotctl questions list 251234567890 --inputs-only
poetry run jotctl questions list 251234567890 --json   # every property of each question

# Summarize a form's submissions, optionally exporting them all to CSV
poetry run jotctl submissions summary 251234567890
poetry run jotctl submissions summary 251234567890 --csv submissions.csv
poetry run jotctl submissions summary 251234567890 --include-deleted
poetry run jotctl submissions summary 251234567890 --csv submissions.csv --normalize-headers   # snake_case headers, max 20 chars

# Delete every submission on a form (moved to its trash, restorable from the Inbox). Shows what will be
# deleted, asks you to type the form ID, and first saves the submissions to data/backups/ as JSON.
poetry run jotctl submissions delete 'Form Name'
poetry run jotctl submissions delete 'Form Name' --purge   # also remove them from the trash for good

# Run a saved report: DuckDB SQL over a form's submissions, fetched live (see reports-example/appointments.yaml).
# Columns are the --normalize-headers names; the query may use the same {{ ... }} date templates as manifests.
poetry run jotctl reports run reports/appointments.yaml
poetry run jotctl reports run reports/appointments.yaml --csv appointments.csv
poetry run jotctl reports run reports/appointments.yaml --from-csv submissions.csv   # query a saved export instead

# Report how many API requests you have left today
poetry run jotctl --show-quota forms list   # or -Q

# Log API requests to stderr
poetry run jotctl -v forms status 'Form Name'    # GET /form/251234567890/properties 200
poetry run jotctl -vv forms status 'Form Name'   # also request and response bodies (passwords redacted)
```

## Routines

```sh
poetry run jotctl forms list

poetry run jotctl submissions summary 251234567890 --csv data/submissions$(date +%Y-%m-%d).csv --normalize-headers

poetry run jotctl reports run reports/shopper-list.yaml

poetry run jotctl reports run reports/shopper-list.yaml --csv data/shopper_list$(date +%Y-%m-%d)_clean.csv
```

## Development

```sh
make format    # ruff format + autofix lint, yamlfix
make lint      # check Python and YAML formatting and lint without changing files
make test      # pytest
```

Notes for coding agents (where the Jotform API docs are, undocumented API behavior, privacy rules)
are in `AGENTS.md`.
