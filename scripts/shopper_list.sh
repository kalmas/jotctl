#!/usr/bin/env bash
# Run shopper_list.sql against a submissions CSV exported with --normalize-headers.
#
# Usage:
#     scripts/shopper_list.sh [CSV] [DUCKDB_OPTIONS...]
#
# CSV defaults to submissions1027.csv. Extra options go to duckdb, e.g. -csv to print
# CSV instead of a table:
#     scripts/shopper_list.sh submissions1027.csv -csv > shoppers.csv
set -euo pipefail

csv="${1:-submissions1027.csv}"
shift || true
[[ -f "$csv" ]] || { echo "error: no such file: $csv" >&2; exit 1; }

sql_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
{
    printf "CREATE VIEW submissions AS FROM read_csv('%s');\n" "${csv//\'/\'\'}"
    cat "$sql_dir/shopper_list.sql"
} | duckdb "$@"
