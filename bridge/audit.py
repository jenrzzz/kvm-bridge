"""One JSON line per session opened, action taken, and session closed. No
frames, ever, and typed text only by its length."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path


class Audit:
    def __init__(self, path: str | os.PathLike | None):
        self.path = Path(path) if path else None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: str, **fields) -> None:
        if not self.path:
            return
        line = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"), "event": event, **fields}
        with self.path.open("a") as f:
            f.write(json.dumps(line, default=str) + "\n")
