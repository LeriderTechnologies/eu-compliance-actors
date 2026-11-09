"""Apify Actor surface (pay-per-event). One event:
- "licence-check": one per entity checked (single input or each item of "entities"). Malformed items are reported
  with an error and not charged. A check whose source was down still returns a verdict (never ALLOW on a missing
  warning list) and is charged.

The lookup index ships inside the image (data/index.json.gz, built from our daily recorder snapshots, with history).
At run time, sources older than `max_age_hours` are refreshed live from the official files (refresh "auto"), so a
stale image still answers on current data; history then comes from the snapshot. Optional env LICENCE_INDEX_URL
points to a newer index file (e.g. a public key-value-store record) that replaces the bundled one.

No attestation here: evidence records/timestamps belong to Payee Check's attest feature (not duplicated).

Local run without the Apify SDK:  python3 -m licence_check.actor input.json [--no-live]
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import os
import sys
import urllib.request

from . import index as I
from . import lei as LEI
from . import live as L
from . import sources as S
from .check import InputError, check_entity

EVENT = "licence-check"
MAX_BATCH = 500
MAX_TARGETED = 50  # ESMA MiFID live queries per run (2 requests each)


def load_index() -> I.Index:
    url = os.environ.get("LICENCE_INDEX_URL")
    if url:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": L.UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                body = r.read()
            remote = json.loads(gzip.decompress(body) if body[:2] == b"\x1f\x8b" else body)
            local = I.load()
            if remote.get("schema") == I.SCHEMA and (remote.get("built_at") or "") > (local.get("built_at") or ""):
                return I.Index(remote)
            return I.Index(local)
        except Exception as exc:  # fall back to the bundled index
            print(f"LICENCE_INDEX_URL not used: {type(exc).__name__}: {exc}", file=sys.stderr)
    return I.Index(I.load())


def _age_hours(ts: str | None, now: dt.datetime) -> float:
    if not ts:
        return 1e9
    try:
        t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return 1e9
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return (now - t).total_seconds() / 3600


def refresh(ix: I.Index, items: list, mode: str = "auto", max_age_hours: float = 48, get=L.urllib_get,
            now: dt.datetime | None = None) -> dict:
    """Refresh stale sources live. Returns {src: error} for sources that could not be refreshed."""
    if mode == "never":
        return {}
    now = now or dt.datetime.now(dt.timezone.utc)
    stamp = now.isoformat(timespec="seconds")
    failed = {}
    for src, m in S.SOURCES.items():
        meta = ix.meta.get(src, {})
        if not m.get("live", True):  # e.g. IOSCO I-SCAN: API key stays on the recorder box; daily build only
            continue
        if mode == "auto" and src in ix.sources() and _age_hours(meta.get("data_as_of"), now) < max_age_hours:
            continue
        try:
            if src == "esma_mifid":
                qs = [it for it in items if isinstance(it, dict)][:MAX_TARGETED]
                if len(items) > MAX_TARGETED:
                    failed[src] = f"live query limited to the first {MAX_TARGETED} entities; rest on snapshot"
                for it in qs:
                    e = it.get("entity") if isinstance(it.get("entity"), dict) else it
                    if e.get("name") or e.get("lei"):
                        ix.upsert(src, L.esma_mifid_query(name=e.get("name"), lei=e.get("lei"), get=get))
                ix.meta.setdefault(src, {}).update({"refreshed": "live_targeted", "data_as_of": stamp})
                continue
            if src == "eba_psd" and meta.get("published_sha256"):
                if L.eba_published_sha(get) == meta["published_sha256"]:
                    ix.meta[src].update({"refreshed": "live_verified_unchanged", "data_as_of": stamp})
                    continue
            recs, fm = L.fetch_records(src, get)
            if not recs:
                raise RuntimeError("empty file")
            ix.replace_source(src, recs, {"refreshed": "live", "data_as_of": stamp, "records": len(recs),
                                          "published": fm.get("published"), "first_recorded":
                                              meta.get("first_recorded")})
        except Exception as exc:
            failed[src] = f"{type(exc).__name__}: {str(exc)[:120]}"
    return failed


def run_batch(actor_input: dict, ix: I.Index, *, unavailable=None, gleif=None, today=None, lei_snap=None):
    items = actor_input.get("entities") if isinstance(actor_input, dict) else None
    if items is None:
        items = [actor_input]
    if not isinstance(items, list) or not items or len(items) > MAX_BATCH:
        raise InputError(f"give one entity or an 'entities' list of 1-{MAX_BATCH} items")
    shared = {k: actor_input[k] for k in ("as_of",) if isinstance(actor_input, dict) and actor_input.get(k)}
    out, charged = [], 0
    for i, req in enumerate(items):
        if isinstance(req, dict):
            req = {**shared, **req}
        try:
            out.append({"index": i, **check_entity(req, ix, unavailable=unavailable, gleif=gleif, today=today,
                                                          lei_snap=lei_snap)})
            charged += 1
        except InputError as exc:
            out.append({"index": i, "error": str(exc)})
    return out, charged


def route(actor_input: dict, *, ix: I.Index | None = None, live: bool = True, get=L.urllib_get, today=None,
          lei_snap=None):
    if not isinstance(actor_input, dict):
        raise InputError("input must be a JSON object")
    mode = actor_input.get("refresh", "auto")
    if mode not in ("auto", "never", "always"):
        raise InputError("refresh must be auto, never or always")
    max_age = actor_input.get("max_age_hours", 48)
    if not isinstance(max_age, (int, float)) or max_age < 1:
        raise InputError("max_age_hours must be a number >= 1")
    ix = ix or load_index()
    items = actor_input.get("entities") or [actor_input]
    unavailable = refresh(ix, items if isinstance(items, list) else [], mode if live else "never", max_age, get)
    gleif = None
    if live and actor_input.get("resolve_lei", True):
        gleif = lambda lei: L.gleif_record(lei, get)  # noqa: E731
    lei_snap = lei_snap if lei_snap is not None else LEI.Lazy()
    return run_batch(actor_input, ix, unavailable=unavailable, gleif=gleif, today=today, lei_snap=lei_snap)


def _budget_covers(Actor, events: list[str]) -> bool:
    """True if the user's "max charge per run" still covers these events. Checked BEFORE pushing a result, so a
    result is never handed out after the limit is reached (the SDK only reports the limit after a charge)."""
    if not events:
        return True
    cm = Actor.get_charging_manager()
    for ev in set(events):
        left = cm.calculate_max_event_charge_count_within_limit(ev)
        if left is not None and left < events.count(ev):
            return False
    return True


async def main() -> None:  # pragma: no cover - needs the Apify platform/SDK
    from apify import Actor

    async with Actor:
        actor_input = await Actor.get_input() or {}
        try:
            items, _ = route(actor_input)
        except InputError as exc:
            await Actor.fail(status_message=str(exc))
            return
        for done, item in enumerate(items):
            if not _budget_covers(Actor, [] if "error" in item else [EVENT]):
                await Actor.set_status_message(
                    f"Stopped before entity {done + 1} of {len(items)}: your maximum charge for this run was reached.")
                break
            await Actor.push_data(item)
            if "error" in item:
                continue
            res = await Actor.charge(event_name=EVENT, count=1)
            # Honour the user's "max charge per run": past it we'd keep paying platform costs for no revenue.
            if getattr(res, "event_charge_limit_reached", False):
                await Actor.set_status_message(
                    f"Stopped after {done + 1} of {len(items)} entities: your maximum charge for this run was reached.")
                break


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) == 1:
        with open(args[0], encoding="utf-8") as fh:
            result, n = route(json.load(fh), live="--no-live" not in sys.argv)
        print(json.dumps({"items": result, "event": EVENT, "chargeable_events": n}, indent=2, ensure_ascii=False))
    else:
        import asyncio

        asyncio.run(main())
