"""API-key loading.

The key is read from ``ANTHROPIC_API_KEY`` or from an env file given by
``--env-file`` (for example ``C:/Work/Claude/AgenticTrader/.env``).  It is
returned to the caller only: never printed, logged, persisted or put in a
transcript.  ``.env`` and ``*.env`` are git-ignored.
"""

from __future__ import annotations

import os
from pathlib import Path

KEY_NAME = "ANTHROPIC_API_KEY"


class ApiKeyError(RuntimeError):
    pass


def parse_env_file(path: Path) -> dict[str, str]:
    """``KEY=value`` lines (``export`` prefix, quotes and ``#`` comments allowed)."""
    out: dict[str, str] = {}
    for raw in Path(path).read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[k.strip()] = v
    return out


def load_api_key(env_file: Path | None = None) -> str:
    """The key from ``env_file`` if given, else from the environment.  Error
    messages name the source, never the key."""
    if env_file is not None:
        path = Path(env_file)
        if not path.is_file():
            raise ApiKeyError(f"env file not found: {path}")
        key = parse_env_file(path).get(KEY_NAME, "")
        source = str(path)
    else:
        key = os.environ.get(KEY_NAME, "")
        source = "the environment"
    if not key.strip():
        raise ApiKeyError(f"{KEY_NAME} not set in {source}")
    return key.strip()


def redact(text: str, key: str | None) -> str:
    """``text`` with ``key`` replaced: a last line of defence for anything persisted."""
    return text.replace(key, "[REDACTED]") if key else text
