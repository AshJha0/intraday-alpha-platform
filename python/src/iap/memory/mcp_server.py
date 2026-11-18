"""``python -m iap.memory.mcp_server [--root PATH]``: the read-only MCP server
(:mod:`iap.governance.mcp_sdk_server`) plus the research-memory tools
(:mod:`iap.memory.mcp_tools`), over stdio.  Needs the optional ``mcp`` extra."""

from collections.abc import Sequence

from iap.governance.mcp_sdk_server import main as _serve
from iap.memory.mcp_tools import register

PROVIDERS = (register,)


def main(argv: Sequence[str] | None = None) -> int:
    return _serve(list(argv) if argv is not None else None, PROVIDERS, "iap.memory.mcp_server")


if __name__ == "__main__":
    raise SystemExit(main())
