#!/usr/bin/env python3
"""
local_env.py
------------
Loads .env for local runs so the scripts work without `source .env` first.

Real environment variables always win, so this is a no-op in GitHub Actions
where the values come from repo secrets -- and a missing .env is fine, not an
error. It exists purely so that running a script locally does the obvious thing
instead of failing with "SUPABASE_URL is required" while the values sit in a
file two directories up.

No dependency on python-dotenv: this handles the three-line KEY=value file this
project actually has, and adding a package for that would be silly.
"""

import os
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def load(path: Path = ENV_PATH) -> int:
    """Populate os.environ from a .env file. Returns how many keys were set."""
    try:
        raw = path.read_text()
    except (FileNotFoundError, NotADirectoryError, PermissionError):
        return 0

    loaded = 0
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        # `export FOO=bar` is a common shape for a file people also source.
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip().strip('"').strip("'")
        if not key or key in os.environ:
            continue  # a real env var beats the file, always
        os.environ[key] = value
        loaded += 1
    return loaded


# Import side effect on purpose: every entrypoint wants this and nothing else.
load()
