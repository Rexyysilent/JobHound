"""Exercise the actual HTTPcore boundary with fake DNS and socket streams."""
import asyncio
import socket
import ssl

import httpcore
import httpx
import pytest

from jobhound.public_http import PublicHTTPTransport, PublicNetworkBackend, require_public_transport


class Stream:
    def __init__(self, peer, response=None):
        self.peer, self.closed, self.sni, self.writes = peer, False, None, []
        self.response = response or b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok'
    async def read(self, max_bytes, timeout=None):
        data, self.response = self.response, b''; return data
    async def write(self, buffer, timeout=None): self.writes.append(buffer)
    async def aclose(self): self.closed = True
    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        assert ssl_context.verify_mode == ssl.CERT_REQUIRED and ssl_context.check_hostname
        self.sni = server_hostname; return self
    def get_extra_info(self, name): return (self.peer, 443) if name == 'server_addr' else None


class Backend:
    def __init__(self, peer=None, response=None): self.calls, self.streams, self.peer, self.response = [], [], peer, response
    async def connect_tcp(self, host, port, **kwargs):
        self.calls.append((host, port))
        stream = Stream(self.peer or host, self.response); self.streams.append(stream); return stream
    async def sleep(self, seconds): await asyncio.sleep(seconds)


def answers(*ips):
    return [(socket.AF_INET6 if ':' in ip else socket.AF_INET, socket.SOCK_STREAM,
             socket.IPPROTO_TCP, '', (ip, 443)) for ip in ips]


@pytest.mark.parametrize('ip', ['127.0.0.1', '10.0.0.1', '169.254.169.254',
    '100.64.0.1', '0.0.0.0', '224.0.0.1', '::1', '::ffff:127.0.0.1', 'fe80::1%1'])
def test_all_dns_answers_must_be_public(monkeypatch, ip):
    async def run():
        async def dns(*args, **kwargs): return answers('8.8.8.8', ip)
        monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', dns)
        backend = Backend()
        with pytest.raises(httpcore.ConnectError):
            await PublicNetworkBackend(backend).connect_tcp('employer.example', 443, timeout=1)
        assert backend.calls == []
    asyncio.run(run())


@pytest.mark.parametrize('host', ['127.1', '2130706433', '0x7f000001', 'localhost.',
                                'xn--employer-9za.example'])
def test_host_aliases_are_checked_after_resolution(monkeypatch, host):
    async def run():
        async def dns(name, *args, **kwargs):
            assert name == host; return answers('127.0.0.1')
        monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', dns)
        backend = Backend()
        with pytest.raises(httpcore.ConnectError):
            await PublicNetworkBackend(backend).connect_tcp(host, 80)
        assert backend.calls == []
    asyncio.run(run())


def test_actual_httpcore_connect_pins_ip_but_preserves_host_and_tls(monkeypatch):
    async def run():
        lookups = []
        async def dns(host, *args, **kwargs):
            lookups.append(host)
            return answers('8.8.8.8' if len(lookups) == 1 else '127.0.0.1')
        monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', dns)
        backend = Backend()
        transport = PublicHTTPTransport()
        transport._pool._network_backend = PublicNetworkBackend(backend)
        async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
            response = await client.get('https://employer.example/jobs/1')
        assert response.text == 'ok' and lookups == ['employer.example']
        assert backend.calls == [('8.8.8.8', 443)]
        assert backend.streams[0].sni == 'employer.example'
        assert b'Host: employer.example' in b''.join(backend.streams[0].writes)
    asyncio.run(run())


def test_preflight_to_connect_rebinding_is_blocked(monkeypatch):
    from jobhound.v41.hydration import RetrievalBudget, RetrievalPolicy, _request
    async def run():
        monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: answers('8.8.8.8'))
        async def dns(*args, **kwargs): return answers('127.0.0.1')
        monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', dns)
        backend = Backend(); transport = PublicHTTPTransport()
        transport._pool._network_backend = PublicNetworkBackend(backend)
        async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
            with pytest.raises(httpx.ConnectError):
                await _request(client, RetrievalBudget(RetrievalPolicy()), 'https://employer.example/jobs/1')
        assert backend.calls == []
    asyncio.run(run())


def test_redirect_cannot_connect_to_private_dns(monkeypatch):
    from jobhound.v41.resolve import resolve_url
    async def run():
        async def dns(host, *args, **kwargs):
            return answers('8.8.8.8' if host == 'employer.example' else '10.0.0.1')
        monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', dns)
        backend = Backend(response=b'HTTP/1.1 302 Found\r\nLocation: https://private.example/jobs/1\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
        transport = PublicHTTPTransport(); transport._pool._network_backend = PublicNetworkBackend(backend)
        async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
            outcome = await resolve_url('https://employer.example/jobs/1', client=client)
        assert backend.calls and all(host == '8.8.8.8' for host, port in backend.calls)
        assert outcome.error
    asyncio.run(run())


def test_peer_mismatch_closes_stream(monkeypatch):
    async def run():
        async def dns(*args, **kwargs): return answers('8.8.8.8')
        monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', dns)
        backend = Backend(peer='127.0.0.1')
        with pytest.raises(httpcore.ConnectError, match='connected_peer_mismatch'):
            await PublicNetworkBackend(backend).connect_tcp('employer.example', 443)
        assert backend.streams[0].closed
    asyncio.run(run())


def test_unreachable_ipv6_does_not_hide_public_ipv4(monkeypatch):
    async def run():
        async def dns(*args, **kwargs): return answers('2606:4700:4700::1111', '8.8.8.8')
        monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', dns)
        cancelled = []
        class DualBackend(Backend):
            async def connect_tcp(self, host, port, **kwargs):
                if ':' in host:
                    try: await asyncio.sleep(10)
                    finally: cancelled.append(host)
                return await super().connect_tcp(host, port, **kwargs)
        backend = DualBackend()
        stream = await PublicNetworkBackend(backend).connect_tcp('employer.example', 443, timeout=.8)
        assert stream.peer == '8.8.8.8' and cancelled
        assert not stream.closed
        await stream.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('cancel', [False, True])
def test_dns_deadline_and_cancellation_propagate(monkeypatch, cancel):
    async def run():
        started = asyncio.Event()
        async def dns(*args, **kwargs): started.set(); await asyncio.sleep(10)
        monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', dns)
        backend = Backend()
        task = asyncio.create_task(PublicNetworkBackend(backend).connect_tcp('employer.example', 443, timeout=.01))
        if cancel:
            await started.wait(); task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else httpcore.ConnectTimeout): await task
        assert backend.calls == []
    asyncio.run(run())


def test_unknown_transport_and_proxy_mount_fail_closed():
    async def run():
        async with httpx.AsyncClient(transport=PublicHTTPTransport(), trust_env=False,
                                    mounts={'https://': httpx.AsyncHTTPTransport(proxy='http://127.0.0.1:8000')}) as client:
            with pytest.raises(httpx.ConnectError, match='public_destination_transport_required'):
                require_public_transport(client, 'https://employer.example/jobs/1')
        async with httpx.AsyncClient(trust_env=False) as client:
            with pytest.raises(httpx.ConnectError): require_public_transport(client, 'https://employer.example/')
    asyncio.run(run())
