"""Import policy (PLATFORM_CONVENTIONS.md §13.7): nothing on the trading
path imports a network client or an LLM client.

An AST scan, not an import: it reads every module under the guarded
packages and inspects each ``import`` / ``from ... import`` statement and
each ``importlib.import_module("...")`` / ``__import__("...")`` call with a
literal argument, wherever it sits in the file (module level, inside a
function, under ``if TYPE_CHECKING``).  Relative imports stay inside the
package and are followed no further — the rule is about what a guarded
module names, and every guarded package is scanned in full.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterator, List, Tuple

import pytest

from conftest import REPO_ROOT

IAP_DIR = REPO_ROOT / "python" / "src" / "iap"

#: Packages on the trading / data path (§13.7 plus the MVP and alpha layers).
GUARDED_PACKAGES = (
    "risk", "execution", "orderbook", "portfolio", "mvp", "core",
    "marketdata", "features", "alpha",
)

#: Top-level module names that are network clients / servers or transports.
NETWORK_MODULES = frozenset({
    "socket", "socketserver", "ssl", "select", "selectors", "asyncio",
    "http", "urllib", "urllib2", "urllib3", "ftplib", "smtplib", "poplib",
    "imaplib", "telnetlib", "xmlrpc", "webbrowser",
    "requests", "httpx", "aiohttp", "websocket", "websockets", "grpc",
    "pycurl", "tornado", "twisted", "zmq", "paramiko", "boto3", "botocore",
    "flask", "fastapi", "starlette", "uvicorn", "django",
})

#: Top-level module names of LLM / agent clients and inference SDKs.
LLM_MODULES = frozenset({
    "anthropic", "openai", "cohere", "mistralai", "groq", "replicate",
    "google", "vertexai", "langchain", "langchain_core", "langchain_openai",
    "langgraph", "llama_index", "litellm", "ollama", "transformers",
    "huggingface_hub", "mcp", "claude_agent_sdk",
})

FORBIDDEN = NETWORK_MODULES | LLM_MODULES


def _literal(node: ast.AST) -> str:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else ""


def imported_modules(tree: ast.AST) -> Iterator[Tuple[int, str]]:
    """``(line, dotted module name)`` of every absolute import in ``tree``,
    including dynamic imports with a literal name."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                yield node.lineno, node.module
        elif isinstance(node, ast.Call) and node.args:
            func = node.func
            name = (func.attr if isinstance(func, ast.Attribute)
                    else func.id if isinstance(func, ast.Name) else "")
            if name in ("import_module", "__import__") and _literal(node.args[0]):
                yield node.lineno, _literal(node.args[0])


def violations(source: str) -> List[Tuple[int, str]]:
    return [(line, module) for line, module in imported_modules(ast.parse(source))
            if module.split(".")[0] in FORBIDDEN]


def _modules(package: str) -> List[Path]:
    return sorted((IAP_DIR / package).rglob("*.py"))


def test_guarded_packages_exist_and_are_not_empty():
    for package in GUARDED_PACKAGES:
        assert (IAP_DIR / package / "__init__.py").is_file(), package
        assert _modules(package), package


@pytest.mark.parametrize("package", GUARDED_PACKAGES)
def test_no_network_or_llm_import_on_the_trading_path(package):
    found = []
    for path in _modules(package):
        for line, module in violations(path.read_text(encoding="utf-8")):
            found.append(f"{path.relative_to(REPO_ROOT).as_posix()}:{line}: imports {module}")
    assert not found, (
        "network / LLM client imported on the trading path "
        "(PLATFORM_CONVENTIONS.md §13.7):\n" + "\n".join(found))


@pytest.mark.parametrize("source, expected", [
    ("import socket\n", [(1, "socket")]),
    ("import os, http.client\n", [(1, "http.client")]),
    ("from urllib.request import urlopen\n", [(1, "urllib.request")]),
    ("def f():\n    import requests\n", [(2, "requests")]),
    ("from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import httpx\n", [(3, "httpx")]),
    ("try:\n    import anthropic\nexcept ImportError:\n    anthropic = None\n",
     [(2, "anthropic")]),
    ("import importlib\nm = importlib.import_module('openai')\n", [(2, "openai")]),
    ("m = __import__('aiohttp')\n", [(1, "aiohttp")]),
    ("from google.generativeai import GenerativeModel\n", [(1, "google.generativeai")]),
])
def test_scanner_catches_every_import_form(source, expected):
    assert violations(source) == expected


@pytest.mark.parametrize("source", [
    "import json, math, sqlite3\n",
    "from . import socket\n",                  # a sibling module, not the stdlib one
    "from .http import thing\n",
    "import numpy as np\nfrom iap.core.rng import SplitMix64\n",
    "name = 'requests'\n",                     # a string, not an import
    "import socketlike\nimport httptools2\n",   # prefix of a name is not the name
])
def test_scanner_does_not_flag_clean_modules(source):
    assert violations(source) == []
