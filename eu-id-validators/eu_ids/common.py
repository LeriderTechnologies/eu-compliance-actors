"""Shared pieces of the EORI and TIN Actors: input errors, country codes, batch runner and charging rule."""
from __future__ import annotations

import unicodedata
from datetime import datetime, timezone
from typing import Callable

# EU member states as the EU services spell them (Greece = EL). XI = Northern Ireland (EORI only).
EU27 = frozenset("AT BE BG CY CZ DE DK EE EL ES FI FR HR HU IE IT LT LU LV MT NL PL PT RO SE SI SK".split())
CHARGED_STATUSES = ("valid", "invalid")
BREAKER = 3  # consecutive service failures after which the rest of the run is answered 'unavailable' at once


def fold_id(value) -> str:
    """Identifier text as the user meant it: NFKC (full-width -> ASCII) and no invisible format characters
    (zero-width space, BOM, bidi controls), so look-alikes are checked as, and reported as, the real ASCII number."""
    s = unicodedata.normalize("NFKC", value or "")
    return "".join(c for c in s if unicodedata.category(c) != "Cf")


class InputError(ValueError):
    """Malformed input: the run fails (whole input) or the item is reported with an error (not charged)."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def country(code) -> str:
    c = (code or "").strip().upper() if isinstance(code, str) else ""
    return "EL" if c == "GR" else c


def is_chargeable(item: dict) -> bool:
    """Charged once per number the EU service answered; a repeat of an earlier item in the same batch is free."""
    return "error" not in item and item.get("status") in CHARGED_STATUSES and item.get("duplicate_of") is None


def drop_untouched_prefill(actor_input: dict, prefill: dict) -> dict:
    """The Apify Console submits prefilled example values with whatever the user adds. When the user picked another
    mode (batch etc.), single-mode fields that still hold the untouched example are not user input: drop them."""
    return {k: v for k, v in actor_input.items() if not (k in prefill and v == prefill[k])}


def pick_items(actor_input, single_key: str, batch_key: str, max_batch: int, example: str, prefill: dict | None = None) -> list:
    """{single_key: ...} -> [input]; {batch_key: [...]} -> that list. A batch plus the untouched Console example in
    the single field runs the batch; a batch plus a single value the user typed is ambiguous (error)."""
    if not isinstance(actor_input, dict):
        raise InputError(f"input must be an object with '{single_key}' or '{batch_key}'")
    batch = actor_input.get(batch_key)
    if batch not in (None, []) and prefill:
        actor_input = drop_untouched_prefill(actor_input, prefill)
    if batch not in (None, []):
        if actor_input.get(single_key):
            raise InputError(f"give either '{single_key}' or '{batch_key}', not both (clear the prefilled field)")
        items = batch
    elif actor_input.get(single_key):
        items = [actor_input]
    else:
        raise InputError(f"give a '{single_key}' (e.g. {example}) or a '{batch_key}' list")
    if not isinstance(items, list) or not items or len(items) > max_batch:
        raise InputError(f"give one '{single_key}' or a '{batch_key}' list of 1-{max_batch} items")
    return items


def count_charged(items: list[dict]) -> int:
    return sum(is_chargeable(it) for it in items)


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


async def run_actor(run_batch: Callable[[dict], tuple[list[dict], int]], event: str, noun: str) -> None:
    """Apify main loop: push every item, charge `event` once per answered item, stop at the user's charge limit."""
    from apify import Actor  # pragma: no cover - needs the Apify platform/SDK

    async with Actor:
        actor_input = await Actor.get_input() or {}
        try:
            items, _ = run_batch(actor_input)
        except InputError as exc:
            await Actor.fail(status_message=str(exc))
            return
        for done, item in enumerate(items):
            if not _budget_covers(Actor, [event] if is_chargeable(item) else []):
                await Actor.set_status_message(
                    f"Stopped before item {done + 1} of {len(items)} {noun}: your maximum charge for this run was reached.")
                break
            await Actor.push_data(item)
            if is_chargeable(item):
                res = await Actor.charge(event_name=event, count=1)
                # Honour the user's "max charge per run": past it we'd keep paying platform costs for no revenue.
                if getattr(res, "event_charge_limit_reached", False):
                    await Actor.set_status_message(
                        f"Stopped after {done + 1} of {len(items)} {noun}: your maximum charge for this run was reached.")
                    break
