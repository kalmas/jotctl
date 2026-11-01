"""Save data to timestamped JSON files before changing or deleting it in Jotform."""

import json
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_DIR = Path("data/backups")


def save_json(data: Any, name: str, directory: Path, *, now: datetime | None = None) -> Path:
    """Write `data` to a new file named `<name>-<YYYYMMDD-HHMMSS>.json`.

    Args:
        data (Any): JSON-serializable data, saved exactly as given.
        name (str): File name prefix, e.g. `251234567890-conditions`.
        directory (Path): Where to write; created if missing.
        now (datetime | None): Timestamp for the file name. Defaults to the current time.

    Returns:
        Path: The new file. An existing file is never overwritten; a numeric suffix is
            added instead.

    Raises:
        OSError: If the directory or file can't be written.
    """
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"{name}-{(now or datetime.now()):%Y%m%d-%H%M%S}"
    n = 1
    while True:
        path = directory / (f"{stem}.json" if n == 1 else f"{stem}-{n}.json")
        try:
            with path.open("x", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.write("\n")
            return path
        except FileExistsError:
            n += 1
