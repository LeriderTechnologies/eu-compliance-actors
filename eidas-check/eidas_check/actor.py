"""Apify Actor surface of lerider/eidas-signature-check (pay-per-event).

Input: {"url": ...} | {"pdf_base64": ...} | {"documents": [{"url"|"pdf_base64", "name"?}, ...]}, plus
"require_qualified" (default false). One dataset item per document.
Detached XML signatures (v0.2): add "signature_url" or "signature_base64" next to the document (the document is then
any file type). A signed XML file on its own is checked as an enveloped XML signature. "eu_act" (CELEX/ELI) +
"language" fetches an EU Official Journal act's seal (and its PDF, unless you give your own copy) from the EU
Publications Office.

Charging: one "signature-check" per document that was read and checked (status "checked" or "unsigned"; "no
signature" is an answer too). Not charged: a URL that could not be downloaded, a file that is not a readable PDF,
a malformed item.

Local run without the Apify SDK:  python3 -m eidas_check.actor input.json
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
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

from . import eu_oj, tl, validate, xades

EVENT = "signature-check"
MAX_DOCS = 20
PREFILL_URL = ("https://raw.githubusercontent.com/esig/dss/master/dss-pades/src/test/resources/validation/"
               "pdf-signed-added-page.pdf")  # Console example in .actor/input_schema.json
MAX_BYTES = 25 * 1024 * 1024
DOC_TIMEOUT_S = 180
UA = tl.HEADERS["User-Agent"]
CHARGED = ("checked", "unsigned")


SINGLE_KEYS = ("url", "pdf_base64", "file_base64", "name", "signature_url", "signature_base64", "eu_act", "language")


class InputError(ValueError):
    pass


def _check_public_url(url: str) -> None:
    """Only public http(s) hosts: no loopback, private, link-local (cloud metadata 169.254.169.254), multicast or
    reserved addresses, so a user-supplied URL can't make the Actor fetch from inside its own network."""
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
    """DNS-rebinding guard: the address we actually connected to must be public too (a host can resolve to a public
    IP for _check_public_url and to an internal one a moment later)."""
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
        _check_public_url(newurl)  # every hop must be public too
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_PublicRedirects, _GuardedHTTPHandler, _GuardedHTTPSHandler)
GUARD_PEER = True  # tests switch the public-address guards off to attack a local server
DOWNLOAD_DEADLINE_S = 90  # total, not per read: a server dripping one byte a minute must not hold the run


def _download(url: str, *, deadline_s: float | None = None) -> bytes:
    if GUARD_PEER:
        _check_public_url(url)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/pdf, */*"})
    limit = time.monotonic() + (deadline_s or DOWNLOAD_DEADLINE_S)
    try:
        with _OPENER.open(req, timeout=30) as r:
            parts, size = [], 0
            while size <= MAX_BYTES:
                if time.monotonic() > limit:
                    raise InputError(f"download took longer than {deadline_s or DOWNLOAD_DEADLINE_S} s; refused")
                chunk = r.read1(65536) if hasattr(r, "read1") else r.read(65536)
                if not chunk:
                    break
                parts.append(chunk)
                size += len(chunk)
            data = b"".join(parts)
    except InputError:
        raise
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise InputError(f"could not download the PDF ({exc})") from exc
    if len(data) > MAX_BYTES:
        raise InputError(f"file is larger than {MAX_BYTES // (1024 * 1024)} MB")
    return data


def _b64file(value: str, what: str) -> bytes:
    value = re.sub(r"^\s*data:[^,]{0,100},", "", value)  # accept data: URLs ("data:application/pdf;base64,...")
    try:
        data = base64.b64decode(value.strip(), validate=False)
    except (binascii.Error, ValueError) as exc:
        raise InputError(f"{what} is not valid base64") from exc
    if len(data) > MAX_BYTES:
        raise InputError(f"file is larger than {MAX_BYTES // (1024 * 1024)} MB")
    return data


def _str(doc, key) -> str | None:
    v = doc.get(key)
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if not isinstance(v, str):
        raise InputError(f"'{key}' must be a string")
    return v.strip()


def _load(doc) -> tuple[str, bytes | None]:
    if not isinstance(doc, dict):
        raise InputError("each document must be an object with 'url' or 'pdf_base64'")
    url, b64 = _str(doc, "url"), _str(doc, "pdf_base64") or _str(doc, "file_base64")
    if url and b64:
        raise InputError("give either 'url' or 'pdf_base64' for a document, not both")
    name = doc.get("name")
    name = str(name)[:300] if name not in (None, "") else None
    if url:
        return name or url, _download(url)
    if b64:
        return name or "pdf_base64", _b64file(b64, "pdf_base64")
    if _str(doc, "eu_act") or _str(doc, "signature_url") or _str(doc, "signature_base64"):
        return name or "", None  # the signature (or the EU act) says what was signed
    raise InputError("each document needs a 'url' or a 'pdf_base64'")


def _load_signature(doc) -> bytes | None:
    url, b64 = _str(doc, "signature_url"), _str(doc, "signature_base64")
    if url and b64:
        raise InputError("give either 'signature_url' or 'signature_base64', not both")
    if url:
        return _download(url)
    if b64:
        return _b64file(b64, "signature_base64")
    return None


def pick_documents(actor_input) -> list:
    if not isinstance(actor_input, dict):
        raise InputError("input must be an object")
    if actor_input.get("url") == PREFILL_URL and (actor_input.get("documents") or actor_input.get("pdf_base64")):
        actor_input = {k: v for k, v in actor_input.items() if k != "url"}  # untouched Console example
    docs = actor_input.get("documents")
    single = [k for k in SINGLE_KEYS if k not in ("name", "language") and actor_input.get(k)]
    if docs not in (None, []):
        if single:
            raise InputError(f"give either {'/'.join(single)} or 'documents', not both (clear the prefilled field)")
    elif single:
        docs = [{k: actor_input.get(k) for k in SINGLE_KEYS if actor_input.get(k)}]
    else:
        raise InputError("give a 'url' of a signed PDF, a 'pdf_base64', an 'eu_act', or a 'documents' list")
    if not isinstance(docs, list) or not docs or len(docs) > MAX_DOCS:
        raise InputError(f"give 1-{MAX_DOCS} documents per run")
    rq = actor_input.get("require_qualified", False)
    if not isinstance(rq, bool):
        raise InputError("require_qualified must be true or false")
    return docs


async def iter_run(actor_input, *, registry=None, tl_meta=None):
    """Yield one dataset item per document as soon as it is done, so a run that hits its timeout keeps (and
    pushes) every finished result."""
    docs = pick_documents(actor_input)
    require_qualified = actor_input.get("require_qualified", False)
    if registry is None:
        registry, tl_meta = await tl.get_registry()
    tl_info = {k: (tl_meta or {}).get(k) for k in ("built_at", "source", "certificate_authorities", "services_skipped",
                                                "lists_missed")}
    for i, doc in enumerate(docs):
        try:
            item = await _check_one(doc, registry, require_qualified)
        except InputError as exc:
            yield {"index": i, "error": str(exc)}
            continue
        except asyncio.TimeoutError:
            yield {"index": i, "source": str(doc.get("name"))[:300] if isinstance(doc, dict) and doc.get("name") else None,
                   "error": f"validation took longer than {DOC_TIMEOUT_S} s (not charged)"}
            continue
        yield {"index": i, **item, "require_qualified": require_qualified, "trusted_lists": tl_info,
               "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


async def _check_one(doc, registry, require_qualified) -> dict:
    """Route one input item: EU act / detached XML signature / signed XML / PDF."""
    name, data = await asyncio.to_thread(_load, doc)
    act = _str(doc, "eu_act")
    sig = None if act else await asyncio.to_thread(_load_signature, doc)
    extra = {}
    if act:
        if _str(doc, "signature_url") or _str(doc, "signature_base64"):
            raise InputError("give either 'eu_act' or a signature file, not both")
        try:
            fetched = await asyncio.to_thread(eu_oj.fetch, act, _str(doc, "language") or "EN", want_pdf=data is None)
        except eu_oj.ActError as exc:
            raise InputError(str(exc)) from exc
        sig = fetched["signature"]
        if data is None:
            data, name = fetched["pdf"], fetched["pdf_url"]
        extra["eu_act"] = {k: fetched[k] for k in ("celex", "language", "pdf_url", "signature_url")}
        extra["eu_act"]["checked_your_copy"] = fetched["pdf"] is None
    if sig is not None:
        docs = {(name or "document"): data} if data is not None else {}
        res = await asyncio.wait_for(xades.check_xml_signature(sig, registry, documents=docs,
                                                               require_qualified=require_qualified), DOC_TIMEOUT_S)
        res["document"] = {**res["document"], "kind": "detached_xml_signature",
                           "signed_file": ({"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
                                           if data is not None else None)}
        return {"source": name or _str(doc, "signature_url") or "signature", **res, **extra}
    if data[:1024].lstrip().startswith(b"%PDF") or b"%PDF-" in data[:1024]:
        res = await asyncio.wait_for(validate.check_pdf(data, registry, require_qualified=require_qualified),
                                     DOC_TIMEOUT_S)
        return {"source": name, **res}
    if xades.looks_like_xml(data):
        res = await asyncio.wait_for(xades.check_xml_signature(data, registry, require_qualified=require_qualified),
                                     DOC_TIMEOUT_S)
        res["document"] = {**res["document"], "kind": "signed_xml"}
        return {"source": name, **res}
    return {"source": name, "verdict": "BLOCK", "status": "unreadable", "signatures": [],
            "document": {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)},
            "reasons": ["not a PDF or a signed XML file; for a file with a separate signature, also give "
                        "'signature_url' or 'signature_base64'"]}


async def run(actor_input, *, registry=None, tl_meta=None) -> list[dict]:
    return [it async for it in iter_run(actor_input, registry=registry, tl_meta=tl_meta)]


def is_chargeable(item: dict) -> bool:
    return "error" not in item and item.get("status") in CHARGED


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
            n_docs = len(pick_documents(actor_input))
        except InputError as exc:
            await Actor.fail(status_message=str(exc))
            return
        done = 0
        async for item in iter_run(actor_input):
            done += 1
            if not _budget_covers(Actor, [EVENT] if is_chargeable(item) else []):
                await Actor.set_status_message(
                    f"Stopped before document {done} of {n_docs}: your maximum charge for this run was reached.")
                break
            await Actor.push_data(item)
            if is_chargeable(item):
                res = await Actor.charge(event_name=EVENT, count=1)
                if getattr(res, "event_charge_limit_reached", False):
                    await Actor.set_status_message(
                        f"Stopped after {done} of {n_docs} documents: your maximum charge for this run was reached.")
                    break


if __name__ == "__main__":
    if len(sys.argv) == 2:
        with open(sys.argv[1], encoding="utf-8") as fh:
            result = asyncio.run(run(json.load(fh)))
        print(json.dumps({"items": result, "event": EVENT,
                          "chargeable_events": sum(map(is_chargeable, result))}, indent=2, ensure_ascii=False))
    else:
        asyncio.run(main())
