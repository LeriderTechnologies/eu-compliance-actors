"""Outbound-fetch guard for certificate validation.

Revocation checking follows URLs taken from the certificates inside the submitted document (OCSP responder, CRL
distribution point, CA issuer). Those certificates are attacker-controlled, so without a guard a crafted certificate
could make the Actor send requests into its own network (cloud metadata 169.254.169.254, localhost, private ranges).

`fetcher_backend()` gives pyhanko-certvalidator an aiohttp session whose connector drops every resolved address
that is not public, for names and IP literals alike and on every redirect hop (the check sits where aiohttp turns a
host into the addresses it will connect to, so DNS rebinding can't slip past it). A refused fetch is an ordinary
fetch failure: the signature then gets REVIEW (revocation status unavailable), never a crash.
"""
from __future__ import annotations

import contextlib
import ipaddress

import aiohttp
from pyhanko_certvalidator.fetchers.aiohttp_fetchers import AIOHttpFetcherBackend
from pyhanko_certvalidator.fetchers.api import FetchLimits

ENABLED = True  # tests switch it off to reach a local OCSP/CRL server


def is_public(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host.split("%")[0])
    except ValueError:
        return False
    return ip.is_global and not ip.is_multicast


class PublicOnlyConnector(aiohttp.TCPConnector):
    async def _resolve_host(self, host, port, traces=None):
        res = await super()._resolve_host(host, port, traces=traces)
        if not ENABLED:
            return res
        ok = [r for r in res if is_public(r["host"])]
        if not ok:
            raise aiohttp.ClientConnectionError(f"refused: {host} is not a public address")
        return ok


MAX_RESPONSE = 20 * 1024 * 1024  # a real CRL/OCSP response/certificate is far smaller; a huge one is an attack


async def _count_chunk(session, ctx, params):
    ctx.received = getattr(ctx, "received", 0) + len(params.chunk)
    if ctx.received > MAX_RESPONSE:
        raise aiohttp.ClientPayloadError(f"response larger than {MAX_RESPONSE // (1024 * 1024)} MB; refused")


def _size_limit() -> aiohttp.TraceConfig:
    tc = aiohttp.TraceConfig()
    tc.on_response_chunk_received.append(_count_chunk)
    return tc


@contextlib.asynccontextmanager
async def fetcher_backend():
    session = aiohttp.ClientSession(connector=PublicOnlyConnector(limit=8), trace_configs=[_size_limit()],
                                    headers={"User-Agent": "lerider-eidas-check/0.2 (+https://lerider.com)"})
    try:
        yield AIOHttpFetcherBackend(session=session, per_request_timeout=10,
                                    limits=FetchLimits(max_requests=50))
    finally:
        await session.close()
