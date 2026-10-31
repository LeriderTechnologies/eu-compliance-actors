"""EU VAT number check: offline format/checksum pre-check, then the live EU VIES register.

Reuses the Payee Check engine (payee_check.vat for formats, payee_check.lookups for the VIES client with retries
and an in-run cache), so both Actors give the same answer for the same VAT id.

Charging: one "vat-check" event per VAT id that VIES answered (status "valid" or "invalid"). Not charged: a
malformed id (status "invalid_format", rejected offline), a prefix VIES does not cover ("not_checkable", e.g. GB,
CH, NO) and a VIES outage ("unavailable"). The caller only pays for an answer from the register.
"""
from __future__ import annotations

from difflib import SequenceMatcher

from eu_common import vat as V
from eu_common.text import InputError, names_match, normalise_name
from eu_common.lookups import Lookups, default_lookups

VERSION = "0.1"
EVENT = "vat-check"
PREFILL_VAT_ID = "IE6388047V"  # Console example in .actor/input_schema.json
MAX_BATCH = 200  # live lookups: keep a run well inside Apify's default timeout even when VIES is slow
CHARGED_STATUSES = ("valid", "invalid")


def name_score(a: str, b: str) -> float:
    """1.0 = same company name after normalising legal forms and punctuation (same rule as Payee Check)."""
    if names_match(a, b):
        return 1.0
    x, y = normalise_name(a), normalise_name(b)
    return round(SequenceMatcher(None, x, y).ratio(), 2) if x and y else 0.0


def check_vat(req, *, lookups: Lookups | None = None) -> dict:
    """One VAT id: a string, or {"vat_id": ..., "expected_name": ...}. Raises InputError when nothing is given."""
    if isinstance(req, str):
        req = {"vat_id": req}
    if not isinstance(req, dict):
        raise InputError("each item must be a VAT id string or an object with 'vat_id'")
    raw = req.get("vat_id")
    if not isinstance(raw, str) or not raw.strip():
        raise InputError("vat_id is required, e.g. 'NL123456789B01'")
    expected = req.get("expected_name")
    if expected is not None and not isinstance(expected, str):
        raise InputError("expected_name must be text")

    vid = V.normalise_vat_id(raw)
    cc, num = V.split_vat_id(vid)
    fmt = V.vat_format_ok(vid)
    out = {"vat_id": vid, "country": cc, "format_ok": fmt, "status": None, "valid": None, "name": None,
           "name_match": None, "checked_at": None, "cached": None, "detail": None, "source": None}
    if cc == "NL":
        out["nl_checksum_ok"] = V.nl_vat_checksum_ok(vid)

    if fmt is None:
        out.update(status="not_checkable",
                   detail=f"{cc!r} is not an EU VIES country prefix (EU member states, EL for Greece, XI for "
                          "Northern Ireland); this id cannot be checked in VIES")
        return out
    if fmt is False:
        out.update(status="invalid_format", valid=False,
                   detail=f"{vid} does not have the {cc} VAT id format, so VIES was not asked (not charged)")
        return out

    r = (lookups or default_lookups()).vies(cc, num)
    out.update(status=r["status"], checked_at=r["checked_at"], cached=r["cached"], source="VIES")
    if r["status"] == "valid":
        out["valid"] = True
        out["name"] = r.get("name")
        if expected and r.get("name"):
            out["name_match"] = name_score(expected, r["name"])
    elif r["status"] == "invalid":
        out["valid"] = False
        out["detail"] = r.get("detail") or f"VIES says {vid} is not a valid VAT id"
    else:
        out["detail"] = f"VIES did not answer ({r.get('detail')}); not charged, try again later"
    return out


def run_batch(actor_input, *, lookups: Lookups | None = None) -> tuple[list[dict], int]:
    """Return (dataset items, number of chargeable events). Input {"vat_id": ...} or {"vat_ids": [...]}."""
    if not isinstance(actor_input, dict):
        raise InputError("input must be an object with 'vat_id' or 'vat_ids'")
    if actor_input.get("vat_ids") not in (None, []) and actor_input.get("vat_id") == PREFILL_VAT_ID:
        actor_input = {k: v for k, v in actor_input.items() if k != "vat_id"}  # untouched Console example
    if "vat_ids" in actor_input and actor_input.get("vat_ids") not in (None, []):
        if actor_input.get("vat_id"):
            raise InputError("give either 'vat_id' or 'vat_ids', not both (clear the prefilled field)")
        items = actor_input["vat_ids"]
    elif actor_input.get("vat_id"):
        items = [actor_input]
    else:
        raise InputError("give a 'vat_id' (e.g. 'NL123456789B01') or a 'vat_ids' list")
    if not isinstance(items, list) or not items or len(items) > MAX_BATCH:
        raise InputError(f"give one 'vat_id' or a 'vat_ids' list of 1-{MAX_BATCH} items")
    lk = lookups or default_lookups()
    out, charged = [], 0
    for i, req in enumerate(items):
        try:
            item = {"index": i, **check_vat(req, lookups=lk)}
        except InputError as exc:
            out.append({"index": i, "error": str(exc)})
            continue
        out.append(item)
        charged += item["status"] in CHARGED_STATUSES
    return out, charged


def is_chargeable(item: dict) -> bool:
    return "error" not in item and item.get("status") in CHARGED_STATUSES
