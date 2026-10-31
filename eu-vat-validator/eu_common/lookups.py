"""Live lookups for the supplier pre-flight: EU VIES (VAT ids) and the Peppol Directory.

Rules for every lookup:
- short timeouts and a hard time budget, a few retries with backoff on transient errors;
- on failure the result is status "unavailable" (never "valid"/"registered"), so the caller can never be told ALLOW
  because a register did not answer;
- a small in-memory TTL cache (24 h) keyed on the VAT id / Peppol id, so a batch never asks twice. Nothing is
  written to disk or logged: VIES returns trader names, and for sole traders those are personal data.

Sources (both free public services, no account):
- VIES REST API, European Commission: https://ec.europa.eu/taxation_customs/vies/rest-api/ms/{CC}/vat/{NUMBER}
  (fair-use limits; the service reports member-state outages as MS_UNAVAILABLE etc.)
- Peppol Directory search API, OpenPeppol: https://directory.peppol.eu/search/1.0/json?participant=...
  (the Directory lists participants that chose to publish business cards; absence does not prove absence from
  the Peppol network itself).
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Callable

USER_AGENT = "payee-check-supplier-preflight/0.2 (+stateless pre-payment check)"
VIES_URL = "https://ec.europa.eu/taxation_customs/vies/rest-api/ms/{cc}/vat/{num}"
PEPPOL_URL = "https://directory.peppol.eu/search/1.0/json?participant={pid}"
CACHE_TTL_S = 24 * 3600

# VIES userError values. VALID / INVALID are answers; these mean "ask again later".
VIES_TRANSIENT = {"MS_UNAVAILABLE", "TIMEOUT", "SERVICE_UNAVAILABLE", "MS_MAX_CONCURRENT_REQ",
                  "GLOBAL_MAX_CONCURRENT_REQ", "SERVER_BUSY", "IP_BLOCKED", "VAT_BLOCKED"}

FetchJson = Callable[[str, float], tuple[int, object]]


BREAKER = 3


class LookupFailed(Exception):
    """Transport failure: timeout, DNS, connection reset, non-JSON body."""


def http_get_json(url: str, timeout: float) -> tuple[int, object]:
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status, body = r.status, r.read(2_000_000)
    except urllib.error.HTTPError as exc:
        status, body = exc.code, exc.read(200_000) if exc.fp else b""
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise LookupFailed(type(exc).__name__) from None
    try:
        return status, json.loads(body.decode("utf-8")) if body else None
    except (ValueError, UnicodeDecodeError):
        return status, None


class TTLCache:
    def __init__(self, ttl_s: float = CACHE_TTL_S, max_items: int = 10_000, clock: Callable[[], float] = time.time):
        self.ttl, self.max, self.clock, self._d = ttl_s, max_items, clock, {}

    def get(self, key):
        hit = self._d.get(key)
        if hit and self.clock() - hit[0] < self.ttl:
            return hit[1]
        self._d.pop(key, None)
        return None

    def put(self, key, value) -> None:
        if len(self._d) >= self.max:
            self._d.pop(next(iter(self._d)))
        self._d[key] = (self.clock(), value)


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class Lookups:
    """VIES + Peppol client. Inject `fetch`, `sleep` and `clock` in tests; defaults hit the real services."""

    def __init__(self, fetch: FetchJson = http_get_json, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic, timeout_s: float = 6.0, budget_s: float = 15.0,
                 backoff_s: tuple[float, ...] = (0.5, 1.5), cache: TTLCache | None = None):
        self.fetch, self.sleep, self.clock = fetch, sleep, clock
        self.timeout, self.budget, self.backoff = timeout_s, budget_s, backoff_s
        self.cache = cache if cache is not None else TTLCache()
        self.calls = 0  # network requests made (for tests and run stats)
        self.failures: dict[tuple, int] = {}  # consecutive 'unavailable' answers per service (+ country)

    def _open(self, key: tuple) -> dict | None:
        """Circuit breaker: after BREAKER consecutive failures, answer 'unavailable' at once for the rest of the run
        instead of spending the full retry budget on every item (an outage would otherwise cost minutes per item)."""
        if self.failures.get(key, 0) >= BREAKER:
            return {"status": "unavailable", "detail": f"skipped: {key[0]}{' ' + key[1] if len(key) > 1 else ''} "
                                                       f"did not answer {BREAKER} times in a row in this run"}
        return None

    def _record(self, key: tuple, status: str) -> None:
        self.failures[key] = self.failures.get(key, 0) + 1 if status == "unavailable" else 0

    def _attempts(self, url: str, answer: Callable[[int, object], dict | None]) -> dict | None:
        """Try up to len(backoff)+1 times within the budget. `answer` returns a final dict or None (transient)."""
        start = self.clock()
        last = "no answer"
        for attempt in range(len(self.backoff) + 1):
            remaining = self.budget - (self.clock() - start)
            if remaining <= 0.5:
                break
            try:
                self.calls += 1
                status, body = self.fetch(url, min(self.timeout, remaining))
                out = answer(status, body)
                if out is not None:
                    return out
                last = f"HTTP {status}" + (f" {body.get('userError') or ''}".rstrip()
                                            if isinstance(body, dict) else "")
            except LookupFailed as exc:
                last = str(exc) or "network error"
            if attempt < len(self.backoff):
                self.sleep(self.backoff[attempt])
        return {"status": "unavailable", "detail": f"no answer after retries ({last})"}

    # ------------------------------------------------------------------ VIES

    def vies(self, cc: str, number: str) -> dict:
        """{"status": "valid"|"invalid"|"unavailable", "name": str|None, "checked_at": iso, "cached": bool}"""
        key = ("vies", cc, number)
        hit = self.cache.get(key)
        if hit:
            return {**hit, "cached": True}

        def answer(status: int, body: object) -> dict | None:
            if not isinstance(body, dict):
                return None
            err = str(body.get("userError") or "").upper()
            if not err:  # error envelope: {"actionSucceed": false, "errorWrappers": [{"error": "..."}]}
                wrappers = body.get("errorWrappers") or []
                err = str(wrappers[0].get("error") or "").upper() if isinstance(wrappers, list) and wrappers \
                    and isinstance(wrappers[0], dict) else ""
            if status == 200 and err == "VALID" and body.get("isValid") is True:
                name = body.get("name")
                return {"status": "valid", "name": None if name in (None, "", "---") else str(name).strip()}
            if status == 200 and err == "INVALID" and body.get("isValid") is False:
                return {"status": "invalid", "name": None}
            if err == "INVALID_INPUT":
                return {"status": "invalid", "name": None, "detail": "VIES: invalid input (format)"}
            return None  # transient or unknown: retry

        out = self._open(("VIES", cc)) or self._attempts(
            VIES_URL.format(cc=urllib.parse.quote(cc), num=urllib.parse.quote(number)), answer)
        if not out.get("detail", "").startswith("skipped:"):
            self._record(("VIES", cc), out["status"])
        out.setdefault("name", None)
        out["checked_at"] = _now_iso()
        if out["status"] != "unavailable":
            self.cache.put(key, out)
        return {**out, "cached": False}

    # ------------------------------------------------------------------ Peppol Directory

    def peppol(self, participant_id: str) -> dict:
        """{"status": "listed"|"not_listed"|"unavailable", "country", "entity_name", "receives_invoices"}"""
        key = ("peppol", participant_id.lower())
        hit = self.cache.get(key)
        if hit:
            return {**hit, "cached": True}

        def answer(status: int, body: object) -> dict | None:
            if status == 400:
                return {"status": "invalid_id", "detail": "the Peppol Directory rejected this participant id"}
            if status != 200 or not isinstance(body, dict) or "matches" not in body:
                return None
            # Upstream JSON is not trusted to have the documented shape: every level is type-checked.
            seq = lambda v: v if isinstance(v, list) else []  # noqa: E731
            text = lambda v: v if isinstance(v, str) and v.strip() else None  # noqa: E731
            matches = [m for m in seq(body.get("matches")) if isinstance(m, dict)]
            if not matches:
                return {"status": "not_listed"}
            m = matches[0]
            ent = next((e for e in seq(m.get("entities")) if isinstance(e, dict)), {})
            nm = next((n for n in seq(ent.get("name")) if isinstance(n, dict)), {})
            docs = m.get("docTypes")
            recv = None if not isinstance(docs, list) else any(
                "Invoice-2::Invoice" in (text(d.get("value")) or "") for d in docs if isinstance(d, dict))
            return {"status": "listed", "country": text(ent.get("countryCode")), "entity_name": text(nm.get("name")),
                    "receives_invoices": recv}

        pid = urllib.parse.quote(f"iso6523-actorid-upis::{participant_id}", safe=":")
        out = self._open(("Peppol Directory",)) or self._attempts(PEPPOL_URL.format(pid=pid), answer)
        if not out.get("detail", "").startswith("skipped:"):
            self._record(("Peppol Directory",), out["status"])
        out["checked_at"] = _now_iso()
        if out["status"] != "unavailable":
            self.cache.put(key, out)
        return {**out, "cached": False}


_default: Lookups | None = None


def default_lookups() -> Lookups:
    """One process-wide client so the cache spans a whole batch / server lifetime."""
    global _default
    if _default is None:
        _default = Lookups()
    return _default
