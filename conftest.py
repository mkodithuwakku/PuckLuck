"""Global pytest configuration.

SRS §8 gate 1: unit tests MUST prohibit network access. Any attempt to
open a socket, create a connection, or resolve a hostname raises
``NetworkAccessBlocked`` so accidental live calls fail loudly and
immediately.
"""

from __future__ import annotations

import socket
from typing import Any

import pytest


class NetworkAccessBlocked(RuntimeError):
    """Raised when a test attempts any outbound network activity."""


def _blocked(*args: Any, **kwargs: Any) -> Any:
    raise NetworkAccessBlocked(
        "Network access is disabled during unit tests (SRS §8). "
        "Inject a fake fetcher/transport instead."
    )


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "socket", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(socket, "getaddrinfo", _blocked)
