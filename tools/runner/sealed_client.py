"""The runner's side of the sealed service (D2 transport, CR1 client).

One function, :func:`request`, sends one request document and returns
``(response, files)``.  The default transport is the Windows named pipe of
the partition (``\\\\.\\pipe\\iap-sealed-<partition>``): opening it is a
``CreateFile`` on which the service reads OUR identity from the kernel, so
the client carries no token.  Tests inject an in-memory transport.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

TOOLS = Path(__file__).resolve().parents[1]

Transport = Callable[[str, Mapping[str, Any]], tuple[dict[str, Any], list[tuple[dict, bytes]]]]


def sealed_service_module():
    """``tools/sealed_service.py`` loaded by path (the service and the client share
    the frame protocol and the checkpoint rules)."""
    name = "sealed_service"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, TOOLS / "sealed_service.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def pipe_transport(partition: str, request: Mapping[str, Any]):
    ss = sealed_service_module()
    return ss.request_over_pipe(ss.pipe_name(partition), request)


def request(
    partition: str, req: Mapping[str, Any], transport: Transport | None = None
) -> tuple[dict[str, Any], list[tuple[dict, bytes]]]:
    return (transport or pipe_transport)(partition, req)
