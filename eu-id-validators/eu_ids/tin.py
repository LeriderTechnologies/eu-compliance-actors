"""EU Tax Identification Number (TIN) check against the European Commission's TIN service (official, free).

The service checks the structure (length, character pattern) and the syntax (check digit / algorithm) of a TIN for
each EU member state. It does NOT say whether the number was issued to anyone: the EU publishes no TIN register.
Results say so in `detail`, and the README too.

TINs of natural persons are personal data: they are passed to the EU service and returned in the caller's own run
dataset, never logged or stored elsewhere.

Charging: one "tin-check" per TIN the service answered (valid / invalid). Non-EU countries, empty numbers and
outages are free.
"""
from __future__ import annotations

import re
from xml.sax.saxutils import escape

from . import soap
from .common import BREAKER, fold_id, EU27, InputError, count_charged, country, now_iso, pick_items

EVENT = "tin-check"
URL = "https://ec.europa.eu/taxation_customs/tin/services/checkTinService"
PREFILL = {"country": "DE", "tin": "86095742719"}  # Console example in .actor/input_schema.json
MAX_BATCH = 200
NOTE = "structure and check digits only: the EU has no public register of issued TINs"


def normalise(value: str) -> str:
    return re.sub(r"[\s.\-/]+", "", fold_id(value)).upper()


def _envelope(cc: str, tin: str) -> str:
    return ('<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
            'xmlns:urn="urn:ec.europa.eu:taxud:tin:services:checkTin:types"><soapenv:Body><urn:checkTin>'
            f'<urn:countryCode>{escape(cc)}</urn:countryCode><urn:tinNumber>{escape(tin)}</urn:tinNumber>'
            '</urn:checkTin></soapenv:Body></soapenv:Envelope>')


def _flag(v: str | None) -> bool | None:
    return {"true": True, "false": False}.get((v or "").strip().lower())


def lookup(cc: str, tin: str, **kw) -> dict:
    """{"valid_structure": bool|None, "valid_syntax": bool|None}. Raises soap.Unavailable."""
    def answer(status, root, text):
        if root is None:
            return None
        resp = soap.find_all(root, "checkTinResponse")
        if status == 200 and resp:
            f = soap.children(resp[0])
            return {"valid_structure": _flag(f.get("validStructure")), "valid_syntax": _flag(f.get("validSyntax"))}
        fault = soap.find_all(root, "faultstring")
        if fault and (fault[0].text or "").strip() == "INVALID_INPUT":
            return {"valid_structure": False, "valid_syntax": False, "input_rejected": True}
        return None  # server busy / technical error: retry
    return soap.call(URL, _envelope(cc, tin), answer, **kw)


def check_tin(i: int, req, *, breaker: dict | None = None, **kw) -> dict:
    if not isinstance(req, dict):
        raise InputError("each item must be an object {'country': 'BE', 'tin': '...'}")
    cc = req.get("country")
    raw = req.get("tin")
    if not isinstance(cc, str) or not cc.strip():
        raise InputError("country is required: the 2-letter code of the country that issued the TIN, e.g. 'DE'")
    if not isinstance(raw, str) or not normalise(raw):
        raise InputError("tin is required, e.g. '86095742719'")
    cc = country(cc)
    tin = normalise(raw)
    try:
        (cc + tin).encode("utf-8")
    except UnicodeEncodeError:  # e.g. a lone surrogate from "\ud800" in the JSON input
        raise InputError("country/tin contain characters that are not valid text") from None
    item = {"index": i, "country": cc, "tin": tin, "status": None, "valid": None, "valid_structure": None,
            "valid_syntax": None, "checked_at": None, "detail": None, "source": None}
    if cc not in EU27:
        item.update(status="not_checkable", detail=f"{cc!r} is not an EU member state code (Greece is EL); the EU "
                                                   "TIN service only covers the 27 member states (not charged)")
        return item
    if breaker is not None and breaker.get(cc, 0) >= BREAKER:
        item.update(status="unavailable", source="EU TIN service",
                    detail=f"skipped: the EU TIN service did not answer for {cc} {BREAKER} times in a row in this run; "
                           "not charged, try again later")
        return item
    try:
        r = lookup(cc, tin, **kw)
    except soap.Unavailable as exc:
        if breaker is not None:
            breaker[cc] = breaker.get(cc, 0) + 1
        item.update(status="unavailable", source="EU TIN service",
                    detail=f"the EU TIN service did not answer ({exc}); not charged, try again later")
        return item
    if breaker is not None:
        breaker[cc] = 0
    ok = r["valid_structure"] is True and r["valid_syntax"] is not False
    item.update(status="valid" if ok else "invalid", valid=ok, valid_structure=r["valid_structure"],
                valid_syntax=r["valid_syntax"], checked_at=now_iso(), source="EU TIN service")
    if ok:
        item["detail"] = f"valid {cc} TIN format ({NOTE})"
    elif r.get("input_rejected"):
        item["detail"] = f"the EU TIN service rejected this input as malformed for {cc}"
    elif r["valid_structure"] is False:
        item["detail"] = f"not the structure of a {cc} TIN (length or characters)"
    else:
        item["detail"] = f"right structure but the check digit is wrong for a {cc} TIN"
    return item


def run_batch(actor_input, **kw) -> tuple[list[dict], int]:
    """Input {"country": ..., "tin": ...} or {"tins": [{"country", "tin"}, ...]}."""
    raw_items = pick_items(actor_input, "tin", "tins", MAX_BATCH, "{'country': 'DE', 'tin': '86095742719'}", prefill=PREFILL)
    items: list[dict] = []
    seen: dict[tuple, dict] = {}  # same country + TIN twice in one batch: ask the service once
    breaker: dict[str, int] = {}
    for i, req in enumerate(raw_items):
        try:
            key = (country(req.get("country")), normalise(req["tin"])) \
                if isinstance(req, dict) and isinstance(req.get("tin"), str) else None
            if key in seen:
                items.append({**seen[key], "index": i, "duplicate_of": seen[key]["index"]})
                continue
            item = check_tin(i, req, breaker=breaker, **kw)
            if item["status"] in ("valid", "invalid"):
                seen[key] = item
            items.append(item)
        except InputError as exc:
            items.append({"index": i, "error": str(exc)})
    return items, count_charged(items)
