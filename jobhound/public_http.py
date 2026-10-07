"""Public-page connections: validate DNS once, dial its IP, retain Host/TLS SNI.

HTTPX does not expose a network-backend constructor argument. Keep the single
HTTPcore integration here; the pool is still empty when its backend is wrapped.
Proxies/Unix sockets are deliberately unsupported at this public-input boundary.
"""
import asyncio
import ipaddress
import socket

import httpcore
import httpx


def public_address(value):
    address = ipaddress.ip_address(value)
    if getattr(address, 'scope_id', None):
        return False
    effective = getattr(address, 'ipv4_mapped', None) or address
    return effective.is_global and not effective.is_multicast


class PublicNetworkBackend:
    def __init__(self, inner):
        self.inner = inner

    async def connect_tcp(self, host, port, timeout=None, local_address=None,
                          socket_options=None):
        if port not in {80, 443}:
            raise httpcore.ConnectError('unsafe_destination_port')
        try:
            async with asyncio.timeout(timeout):
                loop = asyncio.get_running_loop()
                deadline = None if timeout is None else loop.time() + timeout
                answers = await asyncio.get_running_loop().getaddrinfo(
                    host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
                addresses = list(dict.fromkeys(answer[4][0] for answer in answers))
                if not addresses or not all(public_address(ip) for ip in addresses):
                    raise httpcore.ConnectError('dns_private_address')
                # Interleave address families and stagger attempts so an
                # unreachable IPv6 route cannot hide a working IPv4 route.
                families = {4: [], 6: []}
                for ip in addresses:
                    families[ipaddress.ip_address(ip).version].append(ip)
                first = ipaddress.ip_address(addresses[0]).version
                ordered = []
                while families[4] or families[6]:
                    for family in (first, 10 - first):
                        if families[family]:
                            ordered.append(families[family].pop(0))

                async def dial(ip, delay):
                    if delay:
                        await asyncio.sleep(delay)
                    remaining = None if deadline is None else max(0, deadline - loop.time())
                    stream = None
                    try:
                        # Numeric IPs prevent a second hostname lookup. HTTPcore
                        # retains its original origin for Host and TLS start_tls.
                        stream = await self.inner.connect_tcp(
                            ip, port, timeout=remaining, local_address=local_address,
                            socket_options=socket_options)
                        peer = stream.get_extra_info('server_addr')
                        if (not peer or not public_address(peer[0])
                                or ipaddress.ip_address(peer[0]) != ipaddress.ip_address(ip)):
                            raise httpcore.ConnectError('connected_peer_mismatch')
                    except BaseException:
                        if stream is not None:
                            await stream.aclose()
                        raise
                    return stream

                tasks = [asyncio.create_task(dial(ip, index * .25))
                         for index, ip in enumerate(ordered)]
                winner = None
                error = None
                try:
                    pending = set(tasks)
                    while pending:
                        done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                        for task in done:
                            try:
                                winner = task.result()
                            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                                error = exc
                            else:
                                return winner
                    raise error or httpcore.ConnectError('dns_resolution_failed')
                finally:
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    for stream in results:
                        if not isinstance(stream, BaseException) and stream is not winner:
                            await stream.aclose()
        except TimeoutError as exc:
            raise httpcore.ConnectTimeout('destination_connect_timeout') from exc
        except (OSError, ValueError) as exc:
            raise httpcore.ConnectError('dns_resolution_failed') from exc

    async def connect_unix_socket(self, *args, **kwargs):
        raise httpcore.ConnectError('unix_destination_forbidden')

    async def sleep(self, seconds):
        await self.inner.sleep(seconds)


class PublicHTTPTransport(httpx.AsyncHTTPTransport):
    def __init__(self):
        super().__init__(trust_env=False, retries=0)
        if type(self._pool) is not httpcore.AsyncConnectionPool:
            raise RuntimeError('unsupported_public_connection_pool')
        self._pool._network_backend = PublicNetworkBackend(self._pool._network_backend)


def require_public_transport(client, url):
    """Check the effective mounted transport, including caller-supplied proxies."""
    from .bounded_transport import BoundedTransport
    transport = client._transport_for_url(httpx.URL(url))
    if isinstance(transport, BoundedTransport):
        transport = transport.inner
    if not isinstance(transport, (PublicHTTPTransport, httpx.MockTransport)):
        raise httpx.ConnectError('public_destination_transport_required')
