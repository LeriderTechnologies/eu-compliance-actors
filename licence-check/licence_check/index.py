"""Lookup index: built from the recorder snapshots (with history), loaded by the Actor, searched by check.py.

Build (on the box that runs the recorders; point --data, or env SB_RECORDER_DATA, at the recorder data root):
    python3 -m licence_check.index build [--data /path/to/recorder/data] [--out data/index.json.gz]

The index file holds every normalised record plus, per record, the history our recorders saw:
    history = {"first_seen": "YYYY-MM-DD" | None, "events": [{"date", "op": "+"|"-"|"~", "status"?}]}
and per source: data_as_of (last successful check), published (the source's own timestamp, if any),
first_recorded (start of our history) and record counts.
"""
from __future__ import annotations

import collections
import datetime as dt
import gzip
import json
import os
import re
import sys
from pathlib import Path

from . import sources as S
from .normalize import core_name, norm_domain, norm_id, norm_name, registered_domain, tokens

SCHEMA = 1
DEFAULT_DATA = Path(os.environ.get("SB_RECORDER_DATA", Path.home() / "data" / "recorders"))
PKG_INDEX = Path(__file__).resolve().parent.parent / "data" / "index.json.gz"
# Records left out of the build, one "<src> <key>" per line (# comments). Kept next to the package, never shipped:
# used when a public record trips the upload leak guard (2026-11-06: one I-SCAN firm name).
EXCLUDE_FILE = Path(__file__).resolve().parent.parent / "index_exclude.txt"


def _excluded() -> set[tuple[str, str]]:
    if not EXCLUDE_FILE.exists():
        return set()
    out = set()
    for line in EXCLUDE_FILE.read_text().splitlines():
        parts = line.split("#", 1)[0].split()
        if len(parts) == 2:
            out.add((parts[0], parts[1]))
    return out

# recorder file sources: (recorder, recorder source name) -> (our src, variant)
FILE_SOURCES = {
    ("eu-registers", "esma_mica_casps"): ("mica_casp", None),
    ("eu-registers", "esma_mica_emtwp"): ("mica_issuer", "EMT"),
    ("eu-registers", "esma_mica_artzz"): ("mica_issuer", "ART"),
    ("eu-registers", "esma_mica_ncasp"): ("mica_ncasp", None),
    ("eu-registers", "eu_fsf_sanctions"): ("eu_sanctions", None),
    ("nca-lists", "ecb_mfi"): ("ecb_mfi", None),
    ("nca-lists", "afm_warnings"): ("afm_warnings", None),
    ("nca-lists", "fsma_warnings"): ("fsma_warnings", None),
    ("nca-lists", "amf_blacklist"): ("amf_blacklist", None),
    ("nca-lists", "amf_psan"): ("amf_psan", None),
    ("nca-lists", "amf_sgp"): ("amf_sgp", None),
    ("nca-lists", "amf_biens_divers"): ("amf_biens_divers", None),
}


def run_date(stamp: str) -> str:
    """'20261027T150005Z' -> '2026-10-27'."""
    return f"{stamp[0:4]}-{stamp[4:6]}-{stamp[6:8]}"


def _health(data: Path, recorder: str):
    """Last successful check time per recorder source, from health.log."""
    out = {}
    p = data / recorder / "health.log"
    if not p.exists():
        return out
    for line in p.read_text().splitlines():
        try:
            h = json.loads(line)
        except ValueError:
            continue
        for src, st in (h.get("sources") or {}).items():
            if isinstance(st, str) and not st.startswith("error"):
                out[src] = h.get("ts")
    return out


def _state(data: Path, recorder: str):
    p = data / recorder / "state.json"
    return json.loads(p.read_text()) if p.exists() else {}


def _content_key(r):
    return json.dumps({k: r.get(k) for k in ("name", "status", "since", "until", "type", "services", "host_states",
                                               "domains", "date", "lei")}, sort_keys=True, ensure_ascii=False)


def _history_from_versions(src, variant, files):
    """files: sorted [(run, path)] of full raw versions. Returns (records of the latest version, history by key)."""
    hist: dict[str, dict] = {}
    prev: dict[str, str] = {}
    last = []
    for run, path in files:
        recs = S.parse_raw(src, gzip.decompress(path.read_bytes()), variant)
        cur = {r["key"]: _content_key(r) for r in recs}
        d = run_date(run)
        for k, c in cur.items():
            h = hist.setdefault(k, {"first_seen": d, "events": []})
            if k not in prev:
                if prev or h["events"]:  # not the baseline
                    h["events"].append({"date": d, "op": "+"})
            elif prev[k] != c:
                h["events"].append({"date": d, "op": "~", "status": json.loads(c).get("status")})
        for k in prev:
            if k not in cur:
                hist[k]["events"].append({"date": d, "op": "-"})
        prev, last = cur, recs
    return last, hist


def _fin_history(data: Path, src: str, parent_of=lambda k, rec: k):
    """History from fin-registers: baseline/weekly versions + daily keyed diffs."""
    base = data / "fin-registers"
    hist: dict[str, dict] = {}
    vdir, ddir = base / "versions" / src, base / "diffs" / src
    first = sorted(vdir.glob("*.jsonl.gz"))
    if first:
        d = run_date(first[0].name)
        with gzip.open(first[0], "rt", encoding="utf-8") as f:
            for line in f:
                x = json.loads(line)
                k = parent_of(x["key"], x["rec"])
                hist.setdefault(k, {"first_seen": d, "events": []})
    for p in sorted(ddir.glob("*.jsonl.gz")) if ddir.exists() else []:
        d = run_date(p.name)
        with gzip.open(p, "rt", encoding="utf-8") as f:
            for line in f:
                x = json.loads(line)
                k = parent_of(x["key"], x.get("rec") or {})
                h = hist.setdefault(k, {"first_seen": d if x["op"] == "+" else None, "events": []})
                ev = {"date": d, "op": x["op"] if k == x["key"] else "~"}
                rec = x.get("rec") or {}
                st = rec.get("ae_status") or (rec.get("Properties") and "updated")
                if ev["op"] == "~" and st:
                    ev["status"] = st
                if not h["events"] or h["events"][-1] != ev:
                    h["events"].append(ev)
    return hist, (run_date(first[0].name) if first else None)


def _load_fin_current(data: Path, src: str):
    p = data / "fin-registers" / f"current.{src}.jsonl.gz"
    with gzip.open(p, "rt", encoding="utf-8") as f:
        return [json.loads(line.split("\t", 1)[1]) for line in f]


def build(data: Path = DEFAULT_DATA) -> dict:
    srcmeta: dict[str, dict] = {}
    records: list[dict] = []

    # --- fin-registers: ESMA MiFID + EBA PSD2/EMD
    fh, fst = _health(data, "fin-registers"), _state(data, "fin-registers")
    if (data / "fin-registers" / "current.esma_mifid.jsonl.gz").exists():
        docs = _load_fin_current(data, "esma_mifid")
        recs = S.esma_mifid_records(docs)
        hist, first = _fin_history(data, "esma_mifid", lambda k, rec: rec.get("_root_") or k)
        for r in recs:
            r["history"] = hist.get(r["key"], {"first_seen": None, "events": []})
        records += recs
        srcmeta["esma_mifid"] = {"data_as_of": fh.get("esma_mifid"), "first_recorded": first,
                                 "published": (fst.get("esma_mifid") or {}).get("last_modified"), "records": len(recs)}
    if (data / "fin-registers" / "current.eba_psd.jsonl.gz").exists():
        ents = _load_fin_current(data, "eba_psd")
        recs = S.eba_records(ents)
        hist, first = _fin_history(data, "eba_psd")
        for r in recs:
            r["history"] = hist.get(r["key"], {"first_seen": None, "events": []})
        records += recs
        st = fst.get("eba_psd") or {}
        srcmeta["eba_psd"] = {"data_as_of": fh.get("eba_psd"), "first_recorded": first,
                              "published": st.get("file"), "published_sha256": st.get("sha256"), "records": len(recs)}

    if (data / "fin-registers" / "current.iosco_iscan.jsonl.gz").exists():
        recs = S.iscan_records(_load_fin_current(data, "iosco_iscan"))
        hist, first = _fin_history(data, "iosco_iscan")
        for r in recs:
            r["history"] = hist.get(r["key"], {"first_seen": None, "events": []})
        records += recs
        st = fst.get("iosco_iscan") or {}
        srcmeta["iosco_iscan"] = {"data_as_of": fh.get("iosco_iscan"), "first_recorded": first,
                                  "published": st.get("last_run"), "records": len(recs)}

    # --- file sources (eu-registers, nca-lists): full raw versions
    healths = {rec: _health(data, rec) for rec in ("eu-registers", "nca-lists")}
    states = {rec: _state(data, rec) for rec in ("eu-registers", "nca-lists")}
    for (recorder, rsrc), (src, variant) in FILE_SOURCES.items():
        vdir = data / recorder / "versions" / rsrc
        files = sorted((p.name.split("__")[0], p) for p in vdir.glob("*.gz")) if vdir.exists() else []
        if not files:
            continue
        recs, hist = _history_from_versions(src, variant, files)
        for r in recs:
            r["history"] = hist.get(r["key"], {"first_seen": None, "events": []})
        records += recs
        m = srcmeta.setdefault(src, {"records": 0, "first_recorded": None, "data_as_of": None, "parts": {}})
        m["records"] += len(recs)
        fr = run_date(files[0][0])
        m["first_recorded"] = min(filter(None, [m["first_recorded"], fr]))
        asof = healths[recorder].get(rsrc)
        m["data_as_of"] = min(filter(None, [m["data_as_of"], asof])) if m["data_as_of"] and asof else (asof or
                                                                                                        m["data_as_of"])
        st = states[recorder].get(rsrc) or {}
        m["parts"][rsrc] = {"published": st.get("generationDate") or st.get("published") or st.get("head"),
                            "sha256": st.get("sha256"), "records": len(recs)}
    ex = _excluded()
    if ex:
        records = [r for r in records if (r["src"], r["key"]) not in ex]
        for src in {s for s, _ in ex} & set(srcmeta):
            srcmeta[src]["records"] = sum(1 for r in records if r["src"] == src)
    for src, m in srcmeta.items():
        m.update({k: S.SOURCES[src][k] for k in ("title", "kind", "authority", "url", "licence", "attribution")})
    return {"schema": SCHEMA, "built_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "sources": srcmeta, "records": records}


def save(idx: dict, out: Path):
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".part")
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=9) as f:
        json.dump(idx, f, ensure_ascii=False, separators=(",", ":"))
    tmp.replace(out)


def load(path: Path | str | None = None) -> dict:
    p = Path(path or os.environ.get("LICENCE_CHECK_INDEX") or PKG_INDEX)
    with gzip.open(p, "rt", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------------------------------- search index
class Index:
    """In-memory lookup over the records of one or more sources."""

    def __init__(self, idx: dict):
        self.meta = idx.get("sources", {})
        self.built_at = idx.get("built_at")
        self.records: list[dict] = []
        self._by_src: dict[str, list[int]] = collections.defaultdict(list)
        self.add(idx.get("records", []))

    def add(self, recs):
        start = len(self.records)
        self.records.extend(recs)
        for i in range(start, len(self.records)):
            self._by_src[self.records[i]["src"]].append(i)
        self._built = False

    def replace_source(self, src: str, recs: list[dict], meta: dict):
        """Swap a source's records for a live copy; history is carried over by key."""
        old = {self.records[i]["key"]: self.records[i].get("history") for i in self._by_src.get(src, [])}
        keep = [r for r in self.records if r["src"] != src]
        for r in recs:
            r["history"] = old.get(r["key"]) or {"first_seen": None, "events": [], "new_since_snapshot": True}
        self.records = []
        self._by_src = collections.defaultdict(list)
        self.add(keep + recs)
        self.meta[src] = {**self.meta.get(src, {}), **meta}

    def upsert(self, src: str, recs: list[dict]):
        """Targeted live results (ESMA MiFID query): replace records with the same key, add new ones."""
        keys = {r["key"] for r in recs}
        old = {self.records[i]["key"]: self.records[i].get("history") for i in self._by_src.get(src, [])}
        keep = [r for r in self.records if not (r["src"] == src and r["key"] in keys)]
        for r in recs:
            r["history"] = old.get(r["key"]) or {"first_seen": None, "events": [], "new_since_snapshot": True}
        self.records = []
        self._by_src = collections.defaultdict(list)
        self.add(keep + recs)

    def _build(self):
        if self._built:
            return
        self.by_lei, self.by_id, self.by_norm, self.by_core = (collections.defaultdict(set) for _ in range(4))
        self.by_token, self.by_domain = collections.defaultdict(set), collections.defaultdict(set)
        for i, r in enumerate(self.records):
            if r.get("lei"):
                self.by_lei[r["lei"]].add(i)
            par = r.get("parent") or {}
            if par.get("lei"):
                self.by_lei[par["lei"]].add(i)
            for x in r.get("ids") or []:
                if x:
                    self.by_id[norm_id(x)].add(i)
            for n in [r.get("name")] + (r.get("names") or []):
                if not n:
                    continue
                self.by_norm[norm_name(n)].add(i)
                self.by_core[core_name(n)].add(i)
                for t in set(tokens(n)):
                    self.by_token[t].add(i)
            for d in r.get("domains") or []:
                self.by_domain[registered_domain(d)].add(i)
        self.domain_labels = collections.defaultdict(set)
        for d in self.by_domain:
            self.domain_labels[d.split(".")[0]].add(d)
        self._built = True

    def sources(self):
        return sorted(self._by_src)


def main(argv=None):
    import argparse

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--data", type=Path, default=DEFAULT_DATA)
    b.add_argument("--out", type=Path, default=PKG_INDEX)
    a = ap.parse_args(argv)
    idx = build(a.data)
    save(idx, a.out)
    c = collections.Counter(r["src"] for r in idx["records"])
    print(json.dumps({"out": str(a.out), "bytes": a.out.stat().st_size, "records": dict(c),
                      "sources": {k: {kk: v.get(kk) for kk in ("data_as_of", "first_recorded", "records")}
                                  for k, v in idx["sources"].items()}}, indent=1))


if __name__ == "__main__":
    sys.exit(main())
