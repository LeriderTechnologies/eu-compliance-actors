"""Apify Actor surface of lerider/einvoice-validator (pay-per-event).

Input: {"url": ...} | {"file_base64": ...} | {"xml": "<Invoice ...>"} | {"invoices": [{"url"|"file_base64"|"xml",
"name"?}, ...]}, plus "profile" (auto | en16931 | peppol | xrechnung | all). Files: UBL 2.1 / CII XML, or a
Factur-X / ZUGFeRD PDF. One dataset item per invoice.

Charging: one "invoice-validation" per e-invoice that was validated (status "checked": ALLOW, REVIEW or BLOCK on rule
errors are all answers). Not charged: a URL that could not be downloaded, a file that is not an e-invoice (plain PDF,
other XML, unreadable), a malformed item.

Local run without the Apify SDK:  python3 -m einvoice.actor input.json
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import http.client
import ipaddress
import json
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from . import core

EVENT = "invoice-validation"
MAX_INVOICES = 100
MAX_BYTES = 25 * 1024 * 1024
INVOICE_TIMEOUT_S = 60
PREFILL_URL = ("https://raw.githubusercontent.com/OpenPEPPOL/peppol-bis-invoice-3/master/rules/examples/"
               "base-example.xml")  # Console example in .actor/input_schema.json
UA = "lerider-einvoice-validator/0.1 (+https://lerider.com)"
SINGLE_KEYS = ("url", "file_base64", "xml", "name")


class InputError(ValueError):
    pass


# --- downloads: public addresses only (no loopback/private/link-local/metadata), on every hop and at connect time
def _check_public_url(url: str) -> None:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise InputError("url must be an http:// or https:// address")
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80),
                                   type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, ValueError) as exc:
        raise InputError(f"could not resolve the host of the url ({exc})") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global or ip.is_multicast:
            raise InputError("url points to a private, local or reserved address; only public URLs are fetched")


def _check_peer(sock) -> None:
    ip = ipaddress.ip_address(sock.getpeername()[0])
    if not ip.is_global or ip.is_multicast:
        sock.close()
        raise InputError("url resolved to a private, local or reserved address at connect time; refused")


class _GuardedHTTPConnection(http.client.HTTPConnection):
    def connect(self):
        super().connect()
        if GUARD_PEER:
            _check_peer(self.sock)


class _GuardedHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        super().connect()
        if GUARD_PEER:
            _check_peer(self.sock)


class _GuardedHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_GuardedHTTPConnection, req)


class _GuardedHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_GuardedHTTPSConnection, req, context=self._context)


class _PublicRedirects(urllib.request.HTTPRedirectHandler):
    max_redirections = 5

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if GUARD_PEER:
            _check_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_PublicRedirects, _GuardedHTTPHandler, _GuardedHTTPSHandler)
GUARD_PEER = True  # tests switch the public-address guards off to reach a local server
DOWNLOAD_DEADLINE_S = 60


def _download(url: str) -> bytes:
    if GUARD_PEER:
        _check_public_url(url)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/xml, application/pdf, */*"})
    limit = time.monotonic() + DOWNLOAD_DEADLINE_S
    try:
        with _OPENER.open(req, timeout=30) as r:
            parts, size = [], 0
            while size <= MAX_BYTES:
                if time.monotonic() > limit:
                    raise InputError(f"download took longer than {DOWNLOAD_DEADLINE_S} s; refused")
                chunk = r.read1(65536) if hasattr(r, "read1") else r.read(65536)
                if not chunk:
                    break
                parts.append(chunk)
                size += len(chunk)
            data = b"".join(parts)
    except InputError:
        raise
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, http.client.HTTPException) as exc:
        raise InputError(f"could not download the file ({exc})") from exc
    if len(data) > MAX_BYTES:
        raise InputError(f"file is larger than {MAX_BYTES // (1024 * 1024)} MB")
    return data


def _str(doc, key) -> str | None:
    v = doc.get(key)
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if not isinstance(v, str):
        raise InputError(f"'{key}' must be a string")
    return v


def _load(doc) -> tuple[str, bytes]:
    if not isinstance(doc, dict):
        raise InputError("each invoice must be an object with 'url', 'file_base64' or 'xml'")
    given = [k for k in ("url", "file_base64", "xml") if _str(doc, k)]
    if len(given) > 1:
        raise InputError(f"give one of 'url', 'file_base64' or 'xml' per invoice, not {' + '.join(given)}")
    if not given:
        raise InputError("each invoice needs a 'url', a 'file_base64' or an 'xml'")
    k = given[0]
    name = doc.get("name")
    name = str(name)[:300] if name not in (None, "") else None
    if k == "url":
        url = doc["url"].strip()
        return name or url, _download(url)
    if k == "file_base64":
        b64 = re.sub(r"^\s*data:[^,]{0,100},", "", doc["file_base64"])  # accept data: URLs
        try:
            data = base64.b64decode(b64.strip(), validate=False)
        except (binascii.Error, ValueError) as exc:
            raise InputError("file_base64 is not valid base64") from exc
    else:
        try:
            data = doc["xml"].encode("utf-8")
        except UnicodeEncodeError as exc:  # e.g. a lone surrogate from JSON "\ud800"
            raise InputError(f"xml contains characters that are not valid Unicode ({exc.reason})") from exc
        if data.lstrip()[:5].lower() == b"<?xml":  # the text is already Unicode: drop a declared other encoding
            end = data.find(b"?>")
            data = data[end + 2:] if end > 0 else data
    if len(data) > MAX_BYTES:
        raise InputError(f"file is larger than {MAX_BYTES // (1024 * 1024)} MB")
    return name or k, data


def pick_invoices(actor_input) -> list:
    if not isinstance(actor_input, dict):
        raise InputError("input must be an object")
    if actor_input.get("url") == PREFILL_URL and (actor_input.get("invoices") or actor_input.get("file_base64")
                                                   or actor_input.get("xml")):
        actor_input = {k: v for k, v in actor_input.items() if k != "url"}  # untouched Console example
    docs = actor_input.get("invoices")
    single = [k for k in SINGLE_KEYS if k != "name" and actor_input.get(k)]
    if docs not in (None, []):
        if single:
            raise InputError(f"give either {'/'.join(single)} or 'invoices', not both (clear the prefilled field)")
    elif single:
        docs = [{k: actor_input.get(k) for k in SINGLE_KEYS if actor_input.get(k)}]
    else:
        raise InputError("give a 'url', a 'file_base64', an 'xml', or an 'invoices' list")
    if not isinstance(docs, list) or not docs or len(docs) > MAX_INVOICES:
        raise InputError(f"give 1-{MAX_INVOICES} invoices per run")
    profile = actor_input.get("profile", "auto") or "auto"
    if profile not in core.PROFILE_CHOICES:
        raise InputError(f"profile must be one of {', '.join(core.PROFILE_CHOICES)}")
    return docs


def is_pdf(data: bytes) -> bool:
    """PDF unless it plainly starts as XML (an XML invoice may mention %PDF- in a comment or note)."""
    head = data[:1024].lstrip(b"\xef\xbb\xbf\xff\xfe\xfe\xff \t\r\n\x00")
    if head.startswith(b"<"):
        return False
    return b"%PDF-" in data[:1024]


async def iter_run(actor_input, *, engine: core.Engine | None = None):
    docs = pick_invoices(actor_input)
    profile = actor_input.get("profile", "auto") or "auto"
    engine = engine or core.Engine()
    for i, doc in enumerate(docs):
        try:
            name, data = await asyncio.to_thread(_load, doc)
        except InputError as exc:
            yield {"index": i, "error": str(exc)}
            continue
        except Exception as exc:  # noqa: BLE001 - one odd item must never end the run
            yield {"index": i, "error": f"this item could not be read ({type(exc).__name__}); not charged"}
            continue
        try:
            if is_pdf(data):  # PDF parsing stays off the Saxon thread, so a pathological PDF can't stall the batch
                ex = await asyncio.wait_for(asyncio.to_thread(core.extract_pdf, data), INVOICE_TIMEOUT_S)
                fut = engine.run(core.validate_pdf, engine, data, profile=profile, extracted=ex)
            else:
                fut = engine.run(core.validate, engine, data, profile=profile)
            res = await asyncio.wait_for(asyncio.wrap_future(fut), INVOICE_TIMEOUT_S)
        except asyncio.TimeoutError:
            yield {"index": i, "source": name, "error": f"validation took longer than {INVOICE_TIMEOUT_S} s "
                                                         "(not charged)"}
            engine = core.Engine()  # the old Saxon thread may still be busy: later invoices get a fresh one
            continue
        except Exception as exc:  # noqa: BLE001 - one bad file must not end the batch
            yield {"index": i, "source": name, "error": f"the validator could not process this file "
                                                         f"({type(exc).__name__}); not charged"}
            continue
        yield {"index": i, "source": name, **res, "profile_requested": profile,
               "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


async def run(actor_input, *, engine=None) -> list[dict]:
    return [it async for it in iter_run(actor_input, engine=engine)]


def is_chargeable(item: dict) -> bool:
    return "error" not in item and item.get("status") == "checked"


def _budget_covers(Actor, n: int) -> bool:
    if not n:
        return True
    left = Actor.get_charging_manager().calculate_max_event_charge_count_within_limit(EVENT)
    return left is None or left >= n


async def main() -> None:  # pragma: no cover - needs the Apify platform/SDK
    from apify import Actor

    async with Actor:
        actor_input = await Actor.get_input() or {}
        try:
            n = len(pick_invoices(actor_input))
        except InputError as exc:
            await Actor.fail(status_message=str(exc))
            return
        done = 0
        async for item in iter_run(actor_input):
            done += 1
            if not _budget_covers(Actor, 1 if is_chargeable(item) else 0):
                await Actor.set_status_message(
                    f"Stopped before invoice {done} of {n}: your maximum charge for this run was reached.")
                break
            await Actor.push_data(item)
            if is_chargeable(item):
                res = await Actor.charge(event_name=EVENT, count=1)
                if getattr(res, "event_charge_limit_reached", False):
                    await Actor.set_status_message(
                        f"Stopped after {done} of {n} invoices: your maximum charge for this run was reached.")
                    break


if __name__ == "__main__":
    if len(sys.argv) == 2:
        with open(sys.argv[1], encoding="utf-8") as fh:
            result = asyncio.run(run(json.load(fh)))
        print(json.dumps({"items": result, "event": EVENT,
                          "chargeable_events": sum(map(is_chargeable, result))}, indent=2, ensure_ascii=False))
    else:
        asyncio.run(main())
