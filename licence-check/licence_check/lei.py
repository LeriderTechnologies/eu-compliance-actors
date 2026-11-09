"""LEI section of the verdict: registration status, GLEIF legal name, successor LEI and recent renames / lapses.

Data (GLEIF Level-1 golden copy, CC0): the full file is 3.5 M LEIs (~790 MB as a database), too big for the image.
We bake a compact snapshot, data/lei.tsv.gz, built daily from our GLEIF recorder (see build()):
  - every LEI that appears in a bundled register record (licences, warnings, sanctions);
  - every Dutch LEI registered with the KVK (registration authority RA000463), keyed by the 8-digit KVK number;
  - every LEI with a recorded change in the last 90 days, if its legal address is in the EEA;
  - change rows (name, registration status, entity status, country, successor) of those LEIs, last 90 days.
An LEI that is not in the snapshot is looked up at the free GLEIF API (api.gleif.org) at run time when allowed
("lei_source": "gleif_api"); otherwise the section says "not_in_snapshot".

File format (gzip text, tab separated, no tabs/newlines inside values):
  first line   "#" + JSON meta {schema, built_at, data_as_of, gleif_publish, rows, change_rows, changes_since}
  "L" rows     lei, legal_name, country, registration_status, entity_status, successor_lei, kvk, next_renewal
  "C" rows     lei, date (YYYY-MM-DD of the GLEIF publish), field, old, new

Build:  python3 -m licence_check.lei build --db <recorder current.sqlite> [--index data/index.json.gz]
                                           [--out data/lei.tsv.gz] [--days 90]
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import os
import re
import sys
from pathlib import Path

from .normalize import EEA, name_similarity, norm_name

SCHEMA = 1
PKG_LEI = Path(__file__).resolve().parent.parent / "data" / "lei.tsv.gz"
KVK_RA = "RA000463"
CHANGE_FIELDS = ("name", "reg_status", "entity_status", "country", "successor_lei")
RECENT_DAYS = 90
NAME_MATCH = 0.85
_BAD = re.compile(r"[\t\r\n]")


def _clean(v) -> str:
    return _BAD.sub(" ", v or "")


# --------------------------------------------------------------------------------------------- build (recorder box)
def build(db: Path, index_path: Path | None, out: Path, days: int = RECENT_DAYS) -> dict:
    import sqlite3

    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    pub = dict(con.execute("SELECT k, v FROM meta").fetchall()).get("pub")
    want: set[str] = set()
    if index_path and Path(index_path).exists():
        with gzip.open(index_path, "rt", encoding="utf-8") as f:
            for r in json.load(f).get("records", []):
                for k in ("lei", "parent_lei"):
                    if r.get(k):
                        want.add(r[k])
    since = (dt.datetime.fromisoformat(pub[:10]) - dt.timedelta(days=days)).date().isoformat() if pub else "0000"
    ph = ",".join("?" * len(CHANGE_FIELDS))
    changes = con.execute(f"SELECT lei, substr(pub, 1, 10), field, old, new FROM changes WHERE pub >= ? AND field IN "
                          f"({ph}) ORDER BY lei, pub", (since, *CHANGE_FIELDS)).fetchall()
    changed = {c[0] for c in changes}
    rows, keep = [], set()
    eea = set(EEA)
    q = ("SELECT lei, name, country, reg_status, entity_status, successor_lei, ra_id, ra_entity_id, next_renewal "
         "FROM current")
    for lei, name, country, rs, es, succ, ra, raid, nr in con.execute(q):
        kvk = re.sub(r"\D", "", raid or "") if ra == KVK_RA else ""
        if lei in want or kvk or (lei in changed and country in eea):
            rows.append((lei, name, country, rs, es, succ, kvk if len(kvk) == 8 else "", nr))
            keep.add(lei)
    changes = [c for c in changes if c[0] in keep]
    meta = {"schema": SCHEMA, "built_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "gleif_publish": pub, "data_as_of": (pub.replace(" ", "T") + "+00:00") if pub else None,
            "rows": len(rows), "change_rows": len(changes), "changes_since": since,
            "register_leis": len(want), "register_leis_found": len(want & keep)}
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".part")
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=9) as f:
        f.write("#" + json.dumps(meta, separators=(",", ":")) + "\n")
        for r in rows:
            f.write("L\t" + "\t".join(_clean(v) for v in r) + "\n")
        for c in changes:
            f.write("C\t" + "\t".join(_clean(v) for v in c) + "\n")
    tmp.replace(out)
    meta["bytes"] = out.stat().st_size
    return meta


# --------------------------------------------------------------------------------------------- load (Actor)
class Snapshot:
    def __init__(self, meta: dict, rows: dict, kvk: dict, changes: dict):
        self.meta, self.rows, self.kvk, self.changes = meta, rows, kvk, changes

    @classmethod
    def parse(cls, lines) -> "Snapshot":
        meta, rows, kvk, changes = {}, {}, {}, {}
        for line in lines:
            line = line.rstrip("\n")
            if line.startswith("#"):
                meta = json.loads(line[1:])
                continue
            if line.startswith("L\t"):  # keep the raw line (split on use): ~3x less memory than lists
                p = line.split("\t", 8)
                if len(p) == 9:
                    rows[p[1]] = line
                    if p[7]:
                        kvk.setdefault(p[7], []).append(p[1])
                continue
            p = line.split("\t")
            if p[0] == "C" and len(p) == 6:
                changes.setdefault(p[1], []).append({"date": p[2], "field": p[3], "old": p[4] or None,
                                                     "new": p[5] or None})
        return cls(meta, rows, kvk, changes)

    def record(self, lei: str) -> dict | None:
        r = self.rows.get(lei)
        if not r:
            return None
        name, country, rs, es, succ, kvk, nr = r.split("\t")[2:9]
        return {"legal_name": name or None, "country": country or None, "registration_status": rs or None,
                "entity_status": es or None, "successor_lei": succ or None, "kvk": kvk or None,
                "next_renewal": nr or None}


_CACHE: dict = {}


def load(path: Path | str | None = None) -> Snapshot | None:
    """Snapshot from data/lei.tsv.gz (or env LICENCE_LEI_FILE); None if the file is missing or unreadable."""
    p = Path(path or os.environ.get("LICENCE_LEI_FILE") or PKG_LEI)
    if str(p) in _CACHE:
        return _CACHE[str(p)]
    snap = None
    try:
        with gzip.open(p, "rt", encoding="utf-8") as f:
            snap = Snapshot.parse(f)
    except (OSError, ValueError) as exc:
        print(f"LEI snapshot not loaded: {type(exc).__name__}", file=sys.stderr)
    _CACHE[str(p)] = snap
    return snap


class Lazy:
    """Loads the snapshot on first use (~0.4 s, ~90 MB), so runs that never meet an LEI or KVK number skip it."""

    def __init__(self, path=None):
        self._path, self._snap, self._done = path, None, False

    def _get(self):
        if not self._done:
            self._snap, self._done = load(self._path), True
        return self._snap

    def __bool__(self):
        return self._get() is not None

    def __getattr__(self, name):
        return getattr(self._get(), name)


# --------------------------------------------------------------------------------------------- the section
def section(*, snap: Snapshot | None, q: dict, register_lei: str | None, gleif_info: dict | None, live=None,
            today: str) -> dict | None:
    """-> the "lei" output section, or None when no LEI is known for this firm.

    LEI choice: the LEI you gave > the LEI registered with the KVK number you gave (NL) > the LEI on the matched
    register entry. Status comes from the snapshot; an LEI not in it comes from the live GLEIF look-up (the one
    already made for resolve_lei, or `live(lei)`).
    """
    lei, lei_from, kvk_leis = None, None, []
    if q.get("lei"):
        lei, lei_from = q["lei"], "input"
    elif q.get("kvk"):
        kvk_leis = sorted((snap.kvk.get(q["kvk"]) or []) if snap else [])
        if kvk_leis:
            # several LEIs on one KVK number (re-registrations): prefer an ISSUED one
            kvk_leis.sort(key=lambda x: (snap.record(x)["registration_status"] != "ISSUED", x))
            lei, lei_from = kvk_leis[0], "kvk"
    if not lei and register_lei:
        lei, lei_from = register_lei, "register"
    if not lei:
        if q.get("kvk"):
            return {"lei": None, "lei_from": "kvk", "kvk": q["kvk"], "lei_source": "snapshot" if snap else "unavailable",
                    "found": False, "data_as_of": (snap.meta.get("data_as_of") if snap else None),
                    "note": "no LEI registered with this KVK number in the GLEIF data" if snap else
                    "LEI data not available in this run"}
        return None
    out: dict = {"lei": lei, "lei_from": lei_from}
    if q.get("kvk"):
        out["kvk"] = q["kvk"]
    rec = snap.record(lei) if snap else None
    if rec:
        out.update({"lei_source": "snapshot", "data_as_of": snap.meta.get("data_as_of"), **rec})
    else:
        g = gleif_info if gleif_info and not gleif_info.get("error") and gleif_info.get("lei") == lei else None
        if g is None and live:
            try:
                g = live(lei)
            except Exception as exc:  # GLEIF API down or slow: say so, never guess
                out.update({"lei_source": "unavailable", "error": type(exc).__name__})
        if g:
            out.update({"lei_source": "gleif_api", "data_as_of": dt.datetime.now(dt.timezone.utc).isoformat(
                timespec="seconds"), "legal_name": g.get("legal_name"), "country": g.get("country"),
                "registration_status": g.get("registration_status"), "entity_status": g.get("status"),
                "successor_lei": g.get("successor_lei"), "next_renewal": (g.get("next_renewal") or "")[:10] or None})
        elif "lei_source" not in out:
            out["lei_source"] = "not_in_snapshot" if snap else "unavailable"
    if len(kvk_leis) > 1:
        out["other_leis_for_kvk"] = kvk_leis[1:5]
    if out.get("legal_name") and q.get("name"):
        sim = 1.0 if norm_name(out["legal_name"]) == norm_name(q["name"]) else round(
            name_similarity(out["legal_name"], q["name"]), 2)
        out["name_matches"] = sim >= NAME_MATCH
        out["name_similarity"] = sim
    else:
        out["name_matches"] = None
    since = (dt.date.fromisoformat(today) - dt.timedelta(days=RECENT_DAYS)).isoformat()
    ch = [c for c in (snap.changes.get(lei, []) if snap else []) if c["date"] >= since]
    out["recent_changes"] = ch
    out["recent_changes_since"] = max(since, (snap.meta.get("changes_since") or since)) if snap else None
    out["renamed_on"] = next((c["date"] for c in reversed(ch) if c["field"] == "name"), None)
    out["lapsed_on"] = next((c["date"] for c in reversed(ch) if c["field"] == "reg_status" and c["new"] == "LAPSED"),
                            None)
    return out


INACTIVE = ("RETIRED", "ANNULLED", "DUPLICATE", "MERGED")
# GLEIF statuses of an LEI that is being moved from one LEI issuer (LOU) to another: the LEI stays valid. Shown as a
# plain note so a buyer does not read the unfamiliar status as a problem; never changes the verdict.
PENDING = ("PENDING_ARCHIVAL", "PENDING_TRANSFER")


def licence_fields(snap: Snapshot | None, lei: str | None) -> dict:
    """-> {"lei_status", "successor_lei"} for one matched register entry's own LEI, from the baked snapshot (which
    holds every LEI found in our registers). lei_status None = no LEI data in this run."""
    rec = snap.record(lei) if snap and lei else None
    if not rec:
        return {"lei_status": None}
    out = {"lei_status": rec["registration_status"]}
    if rec["successor_lei"]:
        out["successor_lei"] = rec["successor_lei"]
    return out


def _lapsed_msg(lei: str, when: str, review: bool, who: str = "") -> str:
    msg = f"lei_lapsed: {who}LEI {lei}{', which' if who else ''} is LAPSED at GLEIF{when}"
    if review:
        msg += (": the firm did not renew its LEI. This alone does not mean the licence was withdrawn, but a "
                "licensed firm normally keeps its LEI current; ask the firm or check the register")
    return msg


def _inactive_msg(lei: str, status: str, successor: str | None, review: bool, who: str = "") -> str:
    msg = f"lei_inactive: {who}LEI {lei}{', which' if who else ''} is {status} at GLEIF" + (f", successor LEI {successor}" if successor else "")
    if review:
        msg += ("; the firm may have merged into another firm or closed while the register still lists it: check "
                "which firm you deal with and whether the licence moved" + (" to the successor" if successor else ""))
    return msg


def _pending_msg(lei: str, status: str, who: str = "") -> str:
    return (f"lei_pending_transfer: {who}LEI {lei}{', which' if who else ''} is {status} at GLEIF: the LEI is being "
            "moved to another LEI issuer (LOU) and stays valid; this says nothing about the licence and does not "
            "change the verdict")


def verdict_effects(sec: dict | None, *, claims_licence: bool) -> tuple[list[str], list[str]]:
    """-> (reasons that make the verdict REVIEW, notes). claims_licence: you gave a claimed_licence, or this LEI is the
    one of a matched authorised register entry (then the LEI status is about a licensed firm). Never BLOCK: about 35%
    of all LEIs are lapsed (3.2% of licensed EU financial firms'), and a lapsed LEI means the yearly data
    renewal was missed, not that the firm lost a licence."""
    reasons, notes = [], []
    if not sec or not sec.get("lei"):
        if sec and sec.get("note"):
            notes.append(f"LEI: {sec['note']} ({sec.get('kvk')})")
        return reasons, notes
    lei, rs, es = sec["lei"], sec.get("registration_status"), sec.get("entity_status")
    when = f" (recorded {sec['lapsed_on']})" if sec.get("lapsed_on") else (
        f" (renewal was due {sec['next_renewal']})" if sec.get("next_renewal") else "")
    if rs == "LAPSED":
        (reasons if claims_licence else notes).append(_lapsed_msg(lei, when, claims_licence))
    elif rs in INACTIVE or es == "INACTIVE":
        (reasons if claims_licence else notes).append(
            _inactive_msg(lei, rs if rs in INACTIVE else es, sec.get("successor_lei"), claims_licence))
    elif rs in PENDING:
        notes.append(_pending_msg(lei, rs))
    elif sec.get("successor_lei"):
        notes.append(f"LEI {lei} names successor LEI {sec['successor_lei']}")
    if sec.get("renamed_on"):
        c = next(c for c in reversed(sec["recent_changes"]) if c["field"] == "name")
        notes.append(f"lei_renamed: GLEIF legal name changed on {c['date']} from '{c['old']}' to '{c['new']}'")
    if sec.get("name_matches") is False:
        notes.append(f"lei_name_differs: GLEIF legal name for {lei} is '{sec['legal_name']}'")
    if sec.get("lei_source") in ("unavailable", "not_in_snapshot") and sec.get("lei_from") == "input":
        notes.append(f"LEI {lei}: status not checked ({sec['lei_source']})")
    return reasons, notes


def licence_reasons(active: list[dict], skip: str | None, shown: list[dict] | None = None,
                    queried: str | None = None) -> list[str]:
    """REVIEW reasons for authorised register entries whose own LEI (lei_status from licence_fields) is LAPSED or
    RETIRED etc., other than the LEI already judged in the lei section (`skip`). One reason per LEI. Not judged
    here: entries matched only through a parent LEI (a branch's own LEI says nothing about the firm you asked
    for), entries found through the LEI link whose successor LEI is the LEI you asked for / the lei section's LEI
    (the firm itself), and entries not shown in the output (`shown`, the capped licences list). An entry found by
    name that still carries an old duplicate/lapsed LEI keeps its REVIEW (Newrails UAB)."""
    out, seen = [], {skip}
    selves = {x for x in (skip, queried) if x}
    shown_ids = None if shown is None else {id(o) for o in shown}
    for o in active:
        lei, st = o.get("lei"), o.get("lei_status")
        if not lei or lei in seen or not st:
            continue
        how = (o.get("match") or {}).get("method")
        if how == "parent_lei":
            continue
        if shown_ids is not None and id(o) not in shown_ids:
            continue
        if o.get("successor_lei") in selves and how in ("lei", "parent_lei"):
            continue  # found through the LEI link and retired into the firm itself (UBS Europe SE branches)
        seen.add(lei)
        who = f"the register entry '{o['name']}' carries "
        if st == "LAPSED":
            out.append(_lapsed_msg(lei, "", True, who))
        elif st in INACTIVE:
            out.append(_inactive_msg(lei, st, o.get("successor_lei"), True, who))
    return out


def licence_notes(entries: list[dict], skip: str | None) -> list[str]:
    """Notes for shown register entries whose own LEI is PENDING_ARCHIVAL / PENDING_TRANSFER at GLEIF (an LEI moving
    to another issuer, still valid), other than the LEI already described in the lei section (`skip`). One note per
    LEI. Informative only: the verdict is not touched. Entries matched only through a parent LEI are left out, as in
    licence_reasons."""
    out, seen = [], {skip}
    for o in entries:
        lei, st = o.get("lei"), o.get("lei_status")
        if not lei or lei in seen or st not in PENDING or (o.get("match") or {}).get("method") == "parent_lei":
            continue
        seen.add(lei)
        out.append(_pending_msg(lei, st, f"the register entry '{o['name']}' carries "))
    return out


MISMATCH = 0.5


def name_mismatch(sec: dict | None, asked: str | None, other_names=()) -> str | None:
    """Reason when the LEI you gave belongs, per GLEIF, to a differently named firm: best similarity of the asked name
    against the GLEIF legal name, GLEIF other names, names from our rename history and the names of register entries
    matched by that LEI stays < 0.5. Covers legitimately renamed firms (old name in history or other names)."""
    if not sec or sec.get("lei_from") != "input" or not asked or not sec.get("legal_name"):
        return None
    names = [sec["legal_name"], *other_names]
    names += [c[k] for c in sec.get("recent_changes") or [] if c["field"] == "name" for k in ("old", "new") if c[k]]
    best = max((1.0 if norm_name(n) == norm_name(asked) else name_similarity(n, asked) for n in names if n),
               default=0.0)
    sec["name_best_similarity"] = round(best, 2)
    if best >= MISMATCH:
        return None
    return (f"lei_name_mismatch: LEI {sec['lei']} is registered at GLEIF to '{sec['legal_name']}', not '{asked}'; "
            "a licence found through this LEI may belong to another firm")


def main(argv=None):
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--db", required=True, type=Path)
    b.add_argument("--index", type=Path, default=PKG_LEI.parent / "index.json.gz")
    b.add_argument("--out", type=Path, default=PKG_LEI)
    b.add_argument("--days", type=int, default=RECENT_DAYS)
    a = ap.parse_args(argv)
    print(json.dumps(build(a.db, a.index, a.out, a.days), indent=1))


if __name__ == "__main__":
    main()
