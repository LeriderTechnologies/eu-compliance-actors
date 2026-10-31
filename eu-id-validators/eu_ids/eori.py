"""EORI number check against the European Commission's EOS validation service (official, free).

EOS answers whether an EORI number is registered in an EU customs system, and returns the trader's name and address
when the trader agreed to publish them. GB numbers are UK-issued (HMRC) and not in EOS since Brexit, so they are
reported as not_checkable instead of a misleading "not valid"; XI (Northern Ireland) numbers are in EOS.

Charging: one "eori-check" per number EOS answered (valid / invalid). Bad formats, non-EU prefixes and outages are
free. EOS takes at most 10 numbers per request, so batches are sent in chunks of 10.
"""
from __future__ import annotations

import re
from xml.sax.saxutils import escape

from . import soap
from .common import BREAKER, fold_id, EU27, InputError, count_charged, now_iso, pick_items

EVENT = "eori-check"
URL = "https://ec.europa.eu/taxation_customs/dds2/eos/validation/services/validation"
PREFILL = {"eori": "IE6388047V"}  # Console example in .actor/input_schema.json
MAX_BATCH = 500
CHUNK = 10  # EOS limit per request
EORI_COUNTRIES = EU27 | {"XI"}
FORMAT = re.compile(r"[A-Z]{2}[A-Z0-9]{1,15}")
_BASE = {"eori", "status", "statusDescr"}


def normalise(value: str) -> str:
    s = re.sub(r"[\s.\-/]+", "", fold_id(value)).upper()
    return "EL" + s[2:] if s.startswith("GR") else s


def _envelope(numbers: list[str]) -> str:
    inner = "".join(f"<ev:eori>{escape(n)}</ev:eori>" for n in numbers)
    return ('<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body>'
            f'<ev:validateEORI xmlns:ev="http://eori.ws.eos.dds.s/">{inner}</ev:validateEORI></soap:Body></soap:Envelope>')


def lookup(numbers: list[str], **kw) -> dict[str, dict]:
    """{eori: {"status": "valid"|"invalid", "trader": {...}|None}} for up to 10 numbers. Raises soap.Unavailable."""
    def answer(status, root, text):
        if root is None or status != 200:
            return None
        if soap.find_all(root, "errorDescription"):
            return None  # e.g. too many numbers: our bug or a service hiccup; retry, then unavailable
        out = {}
        for res in soap.find_all(root, "result"):
            f = soap.children(res)
            if f.get("status") not in ("0", "1"):
                continue
            trader = {k: v for k, v in f.items() if k not in _BASE and v} or None
            out[f.get("eori", "")] = {"status": "valid" if f["status"] == "0" else "invalid", "trader": trader}
        return out if all(n in out for n in numbers) else None
    return soap.call(URL, _envelope(numbers), answer, **kw)


def _item(i: int, raw) -> dict:
    if isinstance(raw, dict):
        raw = raw.get("eori")
    if not isinstance(raw, str) or not raw.strip():
        raise InputError("eori is required, e.g. 'DE123456789012345'")
    e = normalise(raw)
    cc = e[:2]
    item = {"index": i, "eori": e, "country": cc, "format_ok": bool(FORMAT.fullmatch(e)), "status": None,
            "valid": None, "trader": None, "checked_at": None, "detail": None, "source": None}
    if not item["format_ok"]:
        item.update(status="invalid_format", valid=False,
                    detail="an EORI is a 2-letter country code plus 1-15 letters/digits; EOS was not asked (not charged)")
    elif cc == "GB":
        item.update(status="not_checkable", detail="GB EORI numbers are issued by HMRC (UK) and are not in the EU EOS "
                                                    "system since Brexit; check them with HMRC (not charged)")
    elif cc not in EORI_COUNTRIES:
        item.update(status="not_checkable", detail=f"{cc!r} is not an EU country code (or XI); EOS only holds "
                                                   "EU-issued EORI numbers (not charged)")
    return item


def run_batch(actor_input, **kw) -> tuple[list[dict], int]:
    """Input {"eori": ...} or {"eoris": [...]} (strings or {"eori": ...}). Returns (dataset items, chargeable)."""
    raw_items = pick_items(actor_input, "eori", "eoris", MAX_BATCH, "'DE123456789012345'", prefill=PREFILL)
    items: list[dict] = []
    for i, raw in enumerate(raw_items):
        try:
            items.append(_item(i, raw))
        except InputError as exc:
            items.append({"index": i, "error": str(exc)})
    todo = sorted({it["eori"] for it in items if "error" not in it and it["status"] is None})
    answers: dict[str, dict] = {}
    failed: dict[str, str] = {}
    fails = 0
    for k in range(0, len(todo), CHUNK):
        chunk = todo[k:k + CHUNK]
        if fails >= BREAKER:  # EOS is down: don't spend the retry budget on every remaining chunk
            failed.update({n: f"skipped: EOS did not answer {BREAKER} times in a row in this run" for n in chunk})
            continue
        try:
            answers.update(lookup(chunk, **kw))
            fails = 0
        except soap.Unavailable as exc:
            fails += 1
            failed.update({n: str(exc) for n in chunk})
    checked_at = now_iso()
    first: dict[str, int] = {}
    for it in items:
        if "error" in it or it["status"] is not None:
            continue
        e = it["eori"]
        if e in first:
            it["duplicate_of"] = first[e]  # same number earlier in this batch: answered once, charged once
        else:
            first[e] = it["index"]
        if e in answers:
            a = answers[e]
            it.update(status=a["status"], valid=a["status"] == "valid", trader=a["trader"], checked_at=checked_at,
                      source="EU EOS")
            if a["status"] == "invalid":
                it["detail"] = "EOS says this EORI number is not registered"
            elif a["trader"] is None:
                it["detail"] = "registered; the trader did not agree to publish name and address"
        else:
            it.update(status="unavailable", source="EU EOS",
                      detail=f"EOS did not answer ({failed.get(e, 'no answer')}); not charged, try again later")
    return items, count_charged(items)
