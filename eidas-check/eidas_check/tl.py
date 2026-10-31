"""EU trusted lists (eIDAS) as a pyHanko TSP registry.

Build: download the EU List Of Trusted Lists (LOTL) and every national trusted list, verify their signatures (pyHanko
does this inside `lotl_to_registry`) and build the registry. The Docker build bakes the downloaded list files into
the image (`python -m eidas_check.tl build`); a run rebuilds the registry from those files offline (~4 s, signatures
re-verified) when they are younger than MAX_AGE, else downloads fresh lists (~15-30 s). The parsed registry itself is
not pickled: its certificate objects can't be unpickled reliably across Python versions.

National lists are fetched by us into pyHanko's cache first, with an honest client name and a broad Accept header:
pyHanko's own fetcher sends a narrow Accept header that some member states answer with 406. The cache only holds
raw XML; pyHanko still verifies every list's signature when it builds the registry.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import sys
import tempfile
from pathlib import Path

import aiohttp
from pyhanko.sign.validation.qualified import eutl_fetch, eutl_parse

HEADERS = {"User-Agent": "lerider-eidas-check/0.1 (+https://lerider.com)",
           "Accept": "application/xml, text/xml, application/vnd.etsi.tsl+xml, */*"}
BAKED = Path(__file__).resolve().parent / "data"
MAX_AGE = dt.timedelta(days=7)
MAX_MISSED = 2  # national lists that may be unreachable at build time; their signers then get REVIEW, never ALLOW
log = logging.getLogger("eidas_check.tl")


async def _prewarm(cache, client) -> list[str]:
    lotl = await eutl_fetch.fetch_lotl(client, cache)
    missed = []
    for ref in eutl_parse.parse_lotl_unsafe(lotl).references:
        try:
            cache[ref.location_uri]
            continue
        except KeyError:
            pass
        last = None
        for attempt in range(3):
            try:
                async with client.get(ref.location_uri, timeout=aiohttp.ClientTimeout(total=40)) as r:
                    r.raise_for_status()
                    cache[ref.location_uri] = await r.text()
                last = None
                break
            except Exception as exc:  # noqa: BLE001 - one country down must not stop the rest
                last = exc
                await asyncio.sleep(2 * (attempt + 1))
        if last is not None:
            missed.append(f"{ref.territory}: {last}"[:160])
    return missed


async def build(cache_dir: Path | None = None, *, offline: bool = False) -> tuple[object, dict]:
    """Return (TSPRegistry, meta). offline=True: use only the files already in cache_dir (no downloads)."""
    cache_dir = cache_dir or Path(tempfile.mkdtemp(prefix="tlcache-"))
    cache = eutl_fetch.FileSystemTLCache(cache_dir, expire_after=MAX_AGE * 2 if offline else dt.timedelta(hours=12))
    async with aiohttp.ClientSession(headers=HEADERS) as client:
        missed = [] if offline else await _prewarm(cache, client)
        registry, errors = await eutl_fetch.lotl_to_registry(None, client, cache=cache)
    meta = {"built_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "certificate_authorities": len(list(registry.known_certificate_authorities)),
            "timestamp_authorities": len(list(registry.known_timestamp_authorities)),
            "services_skipped": len(errors), "lists_missed": missed}
    return registry, meta


async def load_baked() -> tuple[object, dict] | None:
    try:
        meta = json.loads((BAKED / "meta.json").read_text())
        if dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(meta["built_at"]) > MAX_AGE:
            return None
        registry, _ = await build(BAKED / "tlcache", offline=True)
        if len(list(registry.known_certificate_authorities)) < 500:
            return None
        return registry, {**meta, "source": "baked"}
    except Exception as exc:  # noqa: BLE001 - any problem with the baked copy: fall back to a live download
        log.warning("baked trusted lists unusable (%s); downloading fresh lists", exc)
        return None


async def get_registry() -> tuple[object, dict]:
    baked = await load_baked()
    if baked:
        return baked
    registry, meta = await build()
    return registry, {**meta, "source": "live"}


def main() -> None:  # python -m eidas_check.tl build
    if sys.argv[1:] != ["build"]:
        sys.exit("usage: python -m eidas_check.tl build")
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("pyhanko").setLevel(logging.CRITICAL)  # ~50 known unparseable historic services log tracebacks
    BAKED.mkdir(exist_ok=True)
    registry, meta = asyncio.run(build(BAKED / "tlcache"))
    if len(meta["lists_missed"]) > MAX_MISSED or meta["certificate_authorities"] < 500:
        sys.exit(f"trusted-list build incomplete, refusing to bake: {meta}")
    (BAKED / "meta.json").write_text(json.dumps(meta, indent=1))
    print(json.dumps(meta, indent=1))


if __name__ == "__main__":
    main()
