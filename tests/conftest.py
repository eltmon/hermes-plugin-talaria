"""Socket-guard: any socket construction during import/register fails the test."""

from __future__ import annotations

import socket
import sys

import pytest

_REAL_SOCKET = socket.socket


class SocketBlockedError(RuntimeError):
    """Raised when a test-guarded socket is constructed or connected."""


def _deny(*_args, **_kwargs):
    raise SocketBlockedError(
        "socket used during plugin import or register(); "
        "Talaria must not open network connections at load time"
    )


@pytest.fixture
def socket_guard(monkeypatch):
    monkeypatch.setattr(socket, "socket", _deny)
    monkeypatch.setattr(socket, "create_connection", _deny)
    monkeypatch.setattr(_REAL_SOCKET, "connect", _deny)
    monkeypatch.setattr(_REAL_SOCKET, "connect_ex", _deny)
    monkeypatch.setattr(_REAL_SOCKET, "__init__", _deny)
    for name in list(sys.modules):
        if name == "talaria" or name.startswith("talaria."):
            del sys.modules[name]
    yield
