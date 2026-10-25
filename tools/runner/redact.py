"""Log redaction for the registered runner (property 1: the job redacts paths
and never prints data).

:func:`redact` removes, in this order: every explicit secret string handed to
it (the cache directory, the partition root, the pipe name), Windows and
POSIX absolute paths, UNC and pipe names, the sealed / runner account names,
the local user and computer names, and e-mail addresses.  What remains is
outcome text: decisions, sequence numbers, hashes, exit codes.

The analysis command's stdout and stderr are NOT copied into the log at all
(a result-writing script that prints a row would otherwise leak it); the log
records only their byte counts and the exit code.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable

ACCOUNT_WORDS = ("iap-sealed-a", "iap-sealed-b", "iap-sealed-c", "iap-runner", "iap-sealed")
_WIN_PATH = re.compile(r"(?<![\w])[A-Za-z]:[\\/][^\s\"'<>|]*")
_UNC_OR_PIPE = re.compile(r"\\\\[^\s\"'<>|]+")
_POSIX_PATH = re.compile(r"(?<![\w.])/(?:[\w.-]+/)+[\w.-]*")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)*")


def _env_words() -> list[str]:
    words = []
    for var in ("USERNAME", "USER", "LOGNAME", "COMPUTERNAME", "HOSTNAME", "USERDOMAIN"):
        v = os.environ.get(var, "").strip()
        if len(v) >= 3:
            words.append(v)
    return words


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    out = text
    for s in sorted({s for s in secrets if s and len(s) >= 3}, key=len, reverse=True):
        out = out.replace(s, "<redacted>")
        alt = s.replace("\\", "/")
        if alt != s:
            out = out.replace(alt, "<redacted>")
    out = _UNC_OR_PIPE.sub("<pipe>", out)
    out = _WIN_PATH.sub("<path>", out)
    out = _POSIX_PATH.sub("<path>", out)
    out = _EMAIL.sub("<email>", out)
    for w in (*ACCOUNT_WORDS, *_env_words()):
        out = re.sub(re.escape(w), "<account>", out, flags=re.IGNORECASE)
    return out


class RedactingLog:
    """Append-only text log whose every line passes :func:`redact`."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        self.secrets = [s for s in secrets if s]
        self.lines: list[str] = []

    def add_secret(self, s: str) -> None:
        if s:
            self.secrets.append(s)

    def __call__(self, line: str) -> None:
        self.lines.append(redact(line, self.secrets))

    def text(self) -> str:
        return "\n".join(self.lines) + ("\n" if self.lines else "")
