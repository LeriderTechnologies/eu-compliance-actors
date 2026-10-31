"""Apify Actor surface (pay-per-event): one "vat-check" event per VAT id that VIES answered (see check.py).

Local run without the Apify SDK:  python3 -m vat_validator.actor input.json
"""
from __future__ import annotations

import json
import sys

from eu_common.text import InputError

from .check import EVENT, is_chargeable, run_batch


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
            items, _ = run_batch(actor_input)
        except InputError as exc:
            await Actor.fail(status_message=str(exc))
            return
        for done, item in enumerate(items):
            if not _budget_covers(Actor, [EVENT] if is_chargeable(item) else []):
                await Actor.set_status_message(
                    f"Stopped before VAT id {done + 1} of {len(items)}: your maximum charge for this run was reached.")
                break
            await Actor.push_data(item)
            if is_chargeable(item):
                res = await Actor.charge(event_name=EVENT, count=1)
                # Honour the user's "max charge per run": past it we'd keep paying platform costs for no revenue.
                if getattr(res, "event_charge_limit_reached", False):
                    await Actor.set_status_message(
                        f"Stopped after {done + 1} of {len(items)} VAT ids: your maximum charge for this run was reached.")
                    break


if __name__ == "__main__":
    if len(sys.argv) == 2:
        with open(sys.argv[1], encoding="utf-8") as fh:
            result, n = run_batch(json.load(fh))
        print(json.dumps({"items": result, "event": EVENT, "chargeable_events": n}, indent=2, ensure_ascii=False))
    else:
        import asyncio

        asyncio.run(main())
