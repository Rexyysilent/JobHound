"""Offline tests use fixed synthetic settings instead of personal policy."""
import sys
from pathlib import Path
import socket
import ipaddress
import pytest

sys.path.insert(0, str(Path(__file__).parent / "tests"))
from public_test_context import install

install()


@pytest.fixture(autouse=True)
def no_external_network(monkeypatch):
    """Tests may use synthetic transports, never actual remote services."""
    connect, connect_ex = socket.socket.connect, socket.socket.connect_ex
    def guard(original):
        def checked(sock, address):
            # Windows asyncio builds its wakeup socketpair over loopback.
            if isinstance(address, tuple):
                try:
                    if ipaddress.ip_address(address[0]).is_loopback:
                        return original(sock, address)
                except ValueError:
                    pass
            elif sock.family == getattr(socket, "AF_UNIX", object()):
                return original(sock, address)
            raise AssertionError("External network is disabled during unit tests")
        return checked
    monkeypatch.setattr(socket.socket, "connect", guard(connect))
    monkeypatch.setattr(socket.socket, "connect_ex", guard(connect_ex))


@pytest.fixture(autouse=True)
def _pin_rollout_switches(monkeypatch):
    """Each test explicitly opts into the rollout policy it exercises."""
    from jobhound.config import CONFIG
    monkeypatch.setattr(CONFIG.v55, 'enabled', False)
    monkeypatch.setattr(CONFIG.v55, 'production_approved', False)
    monkeypatch.setattr(CONFIG.delivery, 'enabled', False)
    monkeypatch.setattr(CONFIG.delivery, 'production_approved', False)
