"""Tiny SOAP-over-HTTP client with retries inside a time budget (same policy as payee_check.lookups).

Standard library only. `post` is injectable so tests never touch the network.
"""
from __future__ import annotations

import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from typing import Callable

Post = Callable[[str, bytes, float], tuple[int, str]]


class Unavailable(Exception):
    """The service did not give a usable answer within the budget (never charged)."""


def http_post(url: str, body: bytes, timeout: float) -> tuple[int, str]:
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "text/xml; charset=utf-8", "SOAPAction": '""', "User-Agent": "lerider-eu-id-validators/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:  # SOAP faults come back as HTTP 500 with a body
        return exc.code, exc.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise Unavailable(str(exc) or "network error") from exc


def call(url: str, body: str, answer: Callable[[int, ET.Element | None, str], object], *, post: Post = http_post,
         sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
         timeout_s: float = 8.0, budget_s: float = 20.0, backoff_s: tuple[float, ...] = (0.5, 1.5)):
    """POST `body`; `answer(status, root, text)` returns a final value or None (transient -> retry).
    Raises Unavailable when no attempt gave an answer."""
    start, last = clock(), "no answer"
    for attempt in range(len(backoff_s) + 1):
        remaining = budget_s - (clock() - start)
        if remaining <= 0.5:
            break
        try:
            status, text = post(url, body.encode("utf-8"), min(timeout_s, remaining))
            try:
                root = ET.fromstring(text)
            except ET.ParseError:
                root = None
            out = answer(status, root, text)
            if out is not None:
                return out
            last = f"HTTP {status}"
        except Unavailable as exc:
            last = str(exc)
        if attempt < len(backoff_s):
            sleep(backoff_s[attempt])
    raise Unavailable(f"no answer after retries ({last})")


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def children(el: ET.Element) -> dict[str, str]:
    """Direct children as {local-name: text} (first occurrence wins)."""
    out: dict[str, str] = {}
    for c in el:
        out.setdefault(local(c.tag), (c.text or "").strip())
    return out


def find_all(root: ET.Element, name: str) -> list[ET.Element]:
    return [e for e in root.iter() if local(e.tag) == name]
