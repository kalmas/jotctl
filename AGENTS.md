# Notes for coding agents

`jotctl` is a Typer CLI for administering Jotform forms: listing forms and questions, exporting
and deleting submissions, applying YAML manifests (`apply -f`, `diff -f`) and running DuckDB reports
(`report -f`). Commands follow kubectl: `get`, `describe`, `apply`, `diff`, `delete`, with `-o` for output. Code is in `src/jotctl/`, tests in `tests/`.

## Jotform API reference

- **Source of truth:** the official docs at https://api.jotform.com/docs/. Fetch them when you
  need an endpoint, parameter or response shape.
- **Local copy:** `docs/jotform-openapi.yaml` may exist. It is an unofficial OpenAPI 3.0 spec
  generated from those docs in Oct 2026: handy to grep (`grep -n "^  /form/{id}" …`), but it may be
  missing or out of date, and it is gitignored. Never commit it; prefer the official docs when
  they disagree.

## API behavior the docs don't cover

Learned while building this tool; check before relying on anything marked unverified.

- **Responses** carry `limit-left` (requests left today); `JotformClient.limit_left` tracks it.
- **Debugging:** `jotctl -v …` logs every request (method, path, status) and `-vv` adds both
  bodies, to stderr. Use it to see exactly what the API returned.
- **API key permission:** changing anything needs a **Full Access** key. A Read Access key gets
  `401 You're not authorized to use (/submission-id)` (or similar) on POST/PUT/DELETE.
- **Form conditions** are the `conditions` entry of `GET /form/{id}/properties` (asking for
  `/properties/conditions` directly returns 404). Each condition's `terms` and `action` are
  JSON-encoded strings. Questions are referenced by QID; `language` is a non-question reference.
- **Updating conditions** means sending the whole list back with
  `PUT /form/{id}/properties` and `{"properties": {"conditions": [...]}}` (unverified that Jotform
  accepts it). `apply` backs up the originals and re-reads the form to check.
- **Open/closed settings** are form properties: `status`, `expireDate` + `expireDateTimeZone`
  (`"No Limit"` when unset), `limitSubmission` vs `count`, `messageOfLimitedForm`,
  `passwordProtect`. Never print `passwordProtectPass`.
- **Deleting submissions:** `DELETE /submission/{id}` moves a submission to the trash (status
  `DELETED`). Deleting it again reportedly removes it for good (unverified);
  `delete submissions --all --purge` re-reads the form to check.
- **Submission statuses** seen: `ACTIVE`, `DELETED`, and `CUSTOM` (meaning unknown).

## Working in this repo

- `make format`, `make lint` and `make test`. Tests mock HTTP with `httpx.MockTransport`; never
  call the real API from tests.
- Read-only commands against the real API are fine for checking your work. Don't run anything that
  changes forms or deletes submissions without the user's explicit go-ahead; to preview, use
  `diff -f`, or `--dry-run` on `apply` and `delete submissions`.
- **Privacy:** `manifests/`, `reports/` and `data/` are gitignored and hold real form names and
  people's personal data. Never copy their contents into tracked files, tests or examples. Use
  generic names and the placeholder form ID `251234567890`, as in `manifests-example/` and
  `reports-example/`.
