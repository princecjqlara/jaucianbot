"""Load local, Git-ignored environment values for maintenance scripts."""

from __future__ import annotations

import os
from pathlib import Path


def load_local_env() -> None:
    path = Path(__file__).resolve().parent / ".env.local"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        os.environ.setdefault(name, value)
