"""The licensed-or-warned check for one entity.

Input (dict): name, lei, reg_number, country, domain (or website / email), claimed_licence (text or
{"type", "country"}), as_of (YYYY-MM-DD, default today).

Verdict:
  BLOCK   strong hit (confidence >= 0.9) on a warning list or EU sanctions (legal entities); a clone warning only
          when your domain is the listed one.
  REVIEW  anything a human should look at: fuzzy or look-alike hits, withdrawn licence, claimed licence not found,
          nothing found (not_found), claimed licence type/country we do not cover (not_covered), a source down.
  ALLOW   an active authorisation matched with confidence >= 0.9, the claimed licence (if given) found, no warning
          hit, every warning list answered, and the domain (if given) confirmed by the register (a domain the register
          cannot confirm gives REVIEW, reason domain_unconfirmed).
"""
from __future__ import annotations

import datetime as dt
import re

from . import lei as LEI
from . import sources as S
from .index import Index
from .normalize import (EEA, core_name, domain_relation, fold, mixed_script, skeleton, name_similarity, norm_country, norm_domain, norm_id,
                        norm_lei, norm_name, registered_domain, tokens)

STRONG = 0.9
MAX_NAME = 200
MIN_REPORT = 0.6
FUZZY_MIN = 0.88
MAX_HITS = 5
STALE_DAYS = 7  # a warning/sanctions list older than this never yields ALLOW
DISCLAIMER = ("Indicative only; not legal, investment or compliance advice. Based on public registers and warning "
              "lists as of the data_as_of dates shown; a register can lag the authority's decisions. Confirm on the "
              "authority's own register before relying on it.")


class InputError(ValueError):
    pass


# --------------------------------------------------------------------------------------------- claimed licence types
# type -> (sources, record-type test). A claim we cannot check is "not_covered".
CLAIM_TYPES = {
    # ECB MFI list only: the ESMA MiFID register does not label which of its firms are credit institutions.
    "bank": (["ecb_mfi"], lambda r: "credit institution" in (r.get("type") or "").lower()),
    "payment_institution": (["eba_psd"], lambda r: r["type"] in (S.EBA_TYPES["PSD_PI"], S.EBA_TYPES["PSD_EPI"])),
    "emi": (["eba_psd"], lambda r: r["type"] in (S.EBA_TYPES["PSD_EMI"], S.EBA_TYPES["PSD_EEMI"])),
    "aisp": (["eba_psd"], lambda r: r["type"] == S.EBA_TYPES["PSD_AISP"] or "account information" in
             " ".join(r.get("services") or [])),
    "pisp": (["eba_psd"], lambda r: "payment initiation" in " ".join(r.get("services") or [])),
    "investment_firm": (["esma_mifid"], lambda r: (r.get("type") or "").startswith("Investment firm")),
    # AMF SGP list (FR): an SGP whose activities include managing AIFs ('Gestion de FIA') or UCITS ('OPCVM')
    "aifm": (["esma_mifid", "amf_sgp"], lambda r: (r.get("type") or "").startswith("AIFM") or (
        r["src"] == "amf_sgp" and any(s.startswith("activity: 2 - Gestion de FIA") for s in r.get("services") or []))),
    "ucits_manco": (["esma_mifid", "amf_sgp"], lambda r: (r.get("type") or "").startswith("UCITS") or (
        r["src"] == "amf_sgp" and "activity: 1 - OPCVM" in (r.get("services") or []))),
    "sgp": (["amf_sgp"], lambda r: r["src"] == "amf_sgp"),  # société de gestion de portefeuille (FR)
    "biens_divers": (["amf_biens_divers"], lambda r: r["src"] == "amf_biens_divers"),  # registered offer (FR)
    "crowdfunding": (["esma_mifid"], lambda r: "Crowdfunding" in (r.get("type") or "")),
    "trading_venue": (["esma_mifid"], lambda r: any(x in (r.get("type") or "") for x in
                                                    ("trading facility", "Regulated market"))),
    "casp": (["mica_casp", "amf_psan"], lambda r: r["src"] == "mica_casp" or r["src"] == "amf_psan"),
    "emt_issuer": (["mica_issuer"], lambda r: (r.get("type") or "").startswith("E-money token")),
    "art_issuer": (["mica_issuer"], lambda r: (r.get("type") or "").startswith("Asset-referenced")),
}
NOT_COVERED_TYPES = {"insurance", "insurer", "insurance_intermediary", "consumer_credit", "mortgage", "pension_fund",
                     "trust_office", "currency_exchange", "financial_advisor", "credit_provider", "other"}
_CLAIM_WORDS = [  # free-text claim -> type (first match wins)
    (r"biens? divers|tangible.asset offer|miscellaneous goods", "biens_divers"),
    (r"e-?money token|\bemt\b", "emt_issuer"), (r"asset.referenced|\bart\b", "art_issuer"),
    (r"e-?money|\bemi\b|elektronisch geld|monnaie electronique|e-geld", "emi"),
    (r"account information|\baisp?\b", "aisp"), (r"payment initiation|\bpisp?\b", "pisp"),
    (r"payment|betaal|paiement|zahlungs", "payment_institution"),
    (r"crypto|casp|mica|psan|virtual asset|vasp", "casp"), (r"crowdfund", "crowdfunding"),
    (r"societe de gestion|gestion de portefeuille|\bsgp\b|portfolio management compan", "sgp"),
    (r"\baifm?\b|alternative investment", "aifm"), (r"ucits|management company|ucits", "ucits_manco"),
    (r"investment firm|broker|beleggingsonderneming|wertpapier|mifid|dealer|asset manag", "investment_firm"),
    (r"bank|credit institution|kredietinstelling|kreditinstitut|etablissement de credit", "bank"),
    (r"insur|verzeker|versicher", "insurance"), (r"consumer credit|krediet|mortgage|hypothe", "consumer_credit"),
    (r"trust office|trustkantoor", "trust_office"), (r"pension", "pension_fund"),
]


def parse_claim(claim):
    if not claim:
        return None
    if isinstance(claim, dict):
        if not all(isinstance(claim.get(k), (str, type(None))) for k in ("type", "country", "text")):
            raise InputError("claimed_licence {\"type\", \"country\"} values must be text")
        t = (claim.get("type") or "").strip().lower().replace(" ", "_").replace("-", "_")
        c = norm_country(claim.get("country") or "")
        text = claim.get("type") or ""
    elif isinstance(claim, str):
        text, t, c = claim, "", None
        m = re.search(r"\b([A-Za-z]{2})\b\s*$", claim.strip())
        if m and norm_country(m.group(1)) in EEA:
            c = norm_country(m.group(1))
        for w in claim.replace(",", " ").split():
            if norm_country(w) in EEA and len(w) > 2:
                c = norm_country(w)
    else:
        raise InputError("claimed_licence must be text or {\"type\", \"country\"}")
    if t not in CLAIM_TYPES and t not in NOT_COVERED_TYPES:
        f = fold(text)
        t = next((ty for pat, ty in _CLAIM_WORDS if re.search(pat, f)), "other")
    return {"text": text if isinstance(claim, str) else claim, "type": t, "country": c}


# --------------------------------------------------------------------------------------------- input
TEXT_FIELDS = ("name", "lei", "reg_number", "registration_number", "kvk", "domain", "website", "url", "email",
               "country", "as_of")  # claimed_licence may also be an object, handled by parse_claim


def normalise_request(req: dict) -> dict:
    if not isinstance(req, dict):
        raise InputError("each entity must be an object")
    e = req.get("entity") if isinstance(req.get("entity"), dict) else req
    for key in TEXT_FIELDS:
        v = e.get(key)
        if v is not None and not isinstance(v, str):
            raise InputError(f"{key} must be text, got {type(v).__name__}")
    name = (e.get("name") or "").strip()
    lei_raw = (e.get("lei") or "").strip()
    lei = norm_lei(lei_raw) if lei_raw else None
    if lei_raw and not lei:
        raise InputError(f"lei '{lei_raw[:30]}' is not a valid LEI (20 characters, ISO 17442 check digits)")
    reg = norm_id(e.get("reg_number") or e.get("registration_number") or e.get("kvk") or "")
    dom_in = e.get("domain") or e.get("website") or e.get("url") or e.get("email") or ""
    domain = norm_domain(dom_in) if dom_in else None
    if dom_in and not domain:
        raise InputError(f"domain '{str(dom_in)[:60]}' is not a domain or URL")
    country = None
    if e.get("country"):
        country = norm_country(e["country"])
        if not country:
            raise InputError(f"country '{e['country']}' not recognised (use ISO 3166 alpha-2, e.g. NL)")
    kvk = None  # Dutch Chamber of Commerce number: 'kvk' given, or an 8-digit reg_number with country NL
    if e.get("kvk"):
        kvk = re.sub(r"\D", "", e["kvk"])
        if len(kvk) != 8:
            raise InputError(f"kvk '{e['kvk'][:30]}' is not an 8-digit KVK number")
    elif country == "NL" and re.fullmatch(r"\d{8}", reg):
        kvk = reg
    if not (name or lei or reg or domain):
        raise InputError("give at least one of name, lei, reg_number, domain")
    if name and len(norm_name(name)) < 2:
        raise InputError("name too short")
    if len(name) > MAX_NAME:  # fuzzy matching cost grows with length; real legal names are far shorter
        raise InputError(f"name too long (max {MAX_NAME} characters)")
    as_of = req.get("as_of") or e.get("as_of")
    if as_of:
        try:
            as_of = dt.date.fromisoformat(str(as_of)).isoformat()
        except ValueError:
            raise InputError("as_of must be YYYY-MM-DD") from None
        if as_of > dt.date.today().isoformat():  # the registers can't say who will be licensed in the future
            raise InputError("as_of cannot be in the future")
    skel = norm_domain(skeleton(dom_in)) if dom_in else None  # homoglyph domain -> the domain it imitates
    return {"name": name or None, "lei": lei, "reg_number": reg or None, "kvk": kvk, "domain": domain,
            "country": country,
            "domain_skeleton": skel if skel and skel != domain else None,
            "claimed_licence": parse_claim(e.get("claimed_licence") or req.get("claimed_licence")),
            "as_of": as_of}


# --------------------------------------------------------------------------------------------- matching
def _candidates(ix: Index, q: dict, extra_names: list[str]):
    """-> {record index: (confidence, method)} over all sources."""
    ix._build()
    hits: dict[int, tuple[float, str]] = {}

    def put(i, conf, how):
        if i not in hits or hits[i][0] < conf:
            hits[i] = (conf, how)

    if q["lei"]:
        for i in ix.by_lei.get(q["lei"], ()):
            r = ix.records[i]
            put(i, 1.0 if r.get("lei") == q["lei"] else 0.9, "lei" if r.get("lei") == q["lei"] else "parent_lei")
    if q["reg_number"]:
        for i in ix.by_id.get(q["reg_number"], ()):
            r = ix.records[i]
            same = not q["country"] or r.get("country") in (None, q["country"])
            put(i, 1.0 if same else 0.8, "reg_number")
    names = [n for n in [q["name"]] + extra_names if n]
    for n in names:
        nn, cn = norm_name(n), core_name(n)
        for i in ix.by_norm.get(nn, ()):
            put(i, 0.95, "exact_name")
        if len(cn) >= 3:
            for i in ix.by_core.get(cn, ()):
                put(i, 0.9, "core_name")
        toks = tokens(n)
        if toks:
            # block on the rarest token, then score
            rare = min(toks, key=lambda t: len(ix.by_token.get(t, ())) or 10 ** 9)
            pool = ix.by_token.get(rare, set())
            if len(pool) <= 3000:
                for i in pool:
                    if i in hits:
                        continue
                    r = ix.records[i]
                    best = max(name_similarity(n, x) for x in [r["name"]] + (r.get("names") or []) if x)
                    if best >= FUZZY_MIN:
                        put(i, round(min(0.85, 0.6 + (best - FUZZY_MIN) * 2.0), 2), "fuzzy_name")
    lookalikes = []
    if q["domain"]:
        rd = registered_domain(q["domain"])
        for i in ix.by_domain.get(rd, ()):
            put(i, 0.95, "domain")
        label = rd.split(".")[0]
        near = set(ix.domain_labels.get(label, set()))
        if len(label) >= 5:
            for d in ix.by_domain:
                if d != rd and abs(len(d) - len(rd)) <= 1 and domain_relation(rd, d) == "lookalike":
                    near.add(d)
        if q.get("domain_skeleton"):  # e.g. Cyrillic 'е' in '24еra.com' imitates the warned '24era.com'
            near.add(registered_domain(q["domain_skeleton"]))
        for d in near - {rd}:
            for i in ix.by_domain.get(d, ()):
                if ix.records[i]["kind"] != "licence":
                    lookalikes.append((i, d))
    # country: a home-state mismatch lowers name-based confidence (branches and passports excepted)
    if q["country"]:
        for i, (conf, how) in list(hits.items()):
            r = ix.records[i]
            if how in ("exact_name", "core_name", "fuzzy_name") and r.get("country") and \
                    r["country"] != q["country"] and q["country"] not in (r.get("host_states") or []):
                hits[i] = (round(conf - 0.1, 2), how + "+other_country")
    return {i: h for i, h in hits.items() if h[0] >= MIN_REPORT}, lookalikes


def _state_at(r: dict, as_of: str | None, first_recorded: str | None):
    """Status of a record on as_of. Register dates first, our history second."""
    if not as_of:
        return r.get("status"), None
    h = r.get("history") or {}
    if r["kind"] == "licence":
        since, until = r.get("since"), r.get("until")
        if since:
            if as_of < since:
                return "not_yet_authorised", None
            if until and as_of >= until:
                return "withdrawn", None
            if until or r.get("status") == "active":
                return "active", None
            return r.get("status"), "withdrawal date not published; status is today's"
        fs = h.get("first_seen")
        if first_recorded and as_of < first_recorded:
            return "unknown", "as_of is before our history for this register"
        if fs and as_of < fs:
            return "not_listed", None
        return r.get("status"), None
    d = r.get("date") or h.get("first_seen")
    if d and as_of < d:
        return "not_yet_listed", None
    if not r.get("date") and first_recorded and as_of < first_recorded:
        return "unknown", "as_of is before our history for this list"
    return "listed", None


def _out(r, conf, how, ix, as_of):
    meta = ix.meta.get(r["src"], {})
    st, note = _state_at(r, as_of, meta.get("first_recorded"))
    o = {"source": r["src"], "register": S.SOURCES[r["src"]]["title"].split(":")[0].split(" (")[0],
         "authority": r.get("authority"), "name": r["name"], "type": r.get("type"), "status": r.get("status"),
         "match": {"method": how, "confidence": conf}}
    if as_of:
        o["status_as_of"] = st
        if note:
            o["status_note"] = note
    for k, kk in (("lei", "lei"), ("ids", "reg_numbers"), ("country", "home_state"), ("since", "authorised_since"),
                  ("until", "withdrawn_on"), ("host_states", "passported_to"), ("domains", "domains"),
                  ("names", "other_names"), ("date", "date"), ("category", "category"), ("parent", "parent"),
                  ("detail_url", "detail_url")):
        if r.get(k):
            o[kk] = r[k]
    if r.get("status") == "notified_exclusion" and "authorised_since" in o:
        o["notified_on"] = o.pop("authorised_since")  # an exclusion notice is not an authorisation
    if r.get("services"):
        o["services"] = r["services"][:20]
    h = r.get("history") or {}
    o["history"] = {"first_seen_by_us": h.get("first_seen"), "changes": h.get("events", [])[-20:],
                    "our_history_starts": meta.get("first_recorded")}
    if h.get("new_since_snapshot"):
        o["history"]["new_since_snapshot"] = True
    o["source_url"] = S.SOURCES[r["src"]]["url"]
    o["data_as_of"] = meta.get("data_as_of")
    return o


def coverage(ix: Index, unavailable: dict | None = None):
    out = {}
    for src, m in S.SOURCES.items():
        meta = ix.meta.get(src, {})
        st = "checked" if src in ix.sources() else "unavailable"
        if unavailable and src in unavailable:
            st = "unavailable" if src not in ix.sources() else "stale"
        c = {"status": st, "kind": m["kind"], "scope": m["scope"], "data_as_of": meta.get("data_as_of"),
             "refreshed": meta.get("refreshed", "snapshot"), "records": meta.get("records")}
        if unavailable and src in unavailable:
            c["error"] = unavailable[src]
        out[src] = c
    return out


def check_entity(req: dict, ix: Index, *, today: str | None = None, unavailable: dict | None = None,
                 gleif=None, lei_snap=None) -> dict:
    """lei_snap: the baked GLEIF snapshot (lei.Snapshot) or None. The live `gleif` look-up (if given) also serves LEIs
    that are not in the snapshot."""
    q = normalise_request(req)
    today = today or dt.date.today().isoformat()
    as_of = q["as_of"]
    extra, gleif_info = [], None
    if gleif and q["lei"]:
        try:
            gleif_info = gleif(q["lei"])
            extra = [gleif_info.get("legal_name")] + (gleif_info.get("other_names") or [])
            extra = [x for x in extra if x and (not q["name"] or norm_name(x) != norm_name(q["name"]))]
        except Exception as exc:  # GLEIF down: name/LEI matching still works on the register's own LEIs
            gleif_info = {"error": f"{type(exc).__name__}"}
    kvk_lei = None  # NL: the LEI registered with this KVK number also finds the register entry, by LEI and name
    if q["kvk"] and not q["lei"] and lei_snap:
        found = sorted(lei_snap.kvk.get(q["kvk"]) or [],
                       key=lambda x: (lei_snap.record(x)["registration_status"] != "ISSUED", x))
        if found:
            kvk_lei = found[0]
            nm = lei_snap.record(kvk_lei)["legal_name"]
            if nm and (not q["name"] or norm_name(nm) != norm_name(q["name"])):
                extra.append(nm)
    hits, lookalikes = _candidates(ix, {**q, "lei": q["lei"] or kvk_lei}, extra)
    ranked = sorted(hits.items(), key=lambda kv: (-kv[1][0], ix.records[kv[0]]["src"], ix.records[kv[0]]["name"]))
    lic = [_out(ix.records[i], c, h, ix, as_of) for i, (c, h) in ranked if ix.records[i]["kind"] == "licence"]
    warn = [_out(ix.records[i], c, h, ix, as_of) for i, (c, h) in ranked if ix.records[i]["kind"] == "warning"]
    sanc = [_out(ix.records[i], c, h, ix, as_of) for i, (c, h) in ranked if ix.records[i]["kind"] == "sanction"]
    if as_of:  # entries listed only after as_of do not count for that date
        warn = [w for w in warn if w.get("status_as_of") not in ("not_yet_listed",)]
        sanc = [s for s in sanc if s.get("status_as_of") not in ("not_yet_listed",)]
    look = []
    for i, d in lookalikes:
        r = ix.records[i]
        look.append({"source": r["src"], "listed_name": r["name"], "listed_domain": d, "your_domain": q["domain"],
                     "date": r.get("date"), "note": "look-alike domain on a warning list (not counted as a match)"})

    reasons, verdict = [], "ALLOW"

    def worse(v):
        nonlocal verdict
        order = {"ALLOW": 0, "REVIEW": 1, "BLOCK": 2}
        if order[v] > order[verdict]:
            verdict = v

    eff = lambda o: o.get("status_as_of", o["status"])  # noqa: E731
    strong_lic = [o for o in lic if o["match"]["confidence"] >= STRONG]
    active_lic = [o for o in strong_lic if eff(o) == "active"]
    strong_lic_ids = any(o["match"]["method"] in ("lei", "reg_number") for o in strong_lic)

    for s in sanc:
        if s["match"]["confidence"] >= STRONG:
            worse("BLOCK")
            reasons.append(f"EU sanctions: '{s['name']}' ({s['match']['method']}, programme {s.get('category')})")
        else:
            worse("REVIEW")
            reasons.append(f"Possible EU sanctions match: '{s['name']}' ({s['match']['method']} "
                           f"{s['match']['confidence']})")
    notes, warn_block = [], False
    qd = registered_domain(q["domain"]) if q["domain"] else None
    for w in warn:
        r_is_clone = "clone" in fold(" ".join(filter(None, [w["name"], w.get("category") or "",
                                                             str(w.get("parent") or "")])))
        conf, how = w["match"]["confidence"], w["match"]["method"]
        where = f"{w['authority']} ({w.get('home_state') or 'EU'})"
        on_listed_domain = bool(qd and qd in {registered_domain(d) for d in w.get("domains") or []})
        name_based = how not in ("domain", "lei", "reg_number")
        # A licensed firm whose name also appears on a warning list: usually impersonation or a clone.
        same_name_as_licensed = name_based and (bool(active_lic) or strong_lic_ids)
        if on_listed_domain:
            worse("BLOCK"); warn_block = True
            reasons.append(f"On a warning list: {where} lists '{w['name']}' with your domain {q['domain']}"
                           + (f" ({w['date']})" if w.get("date") else ""))
        elif r_is_clone or same_name_as_licensed:
            lic_domains = {registered_domain(d) for o in active_lic for d in o.get("domains") or []}
            if qd and qd in lic_domains:
                notes.append(f"{where} warns about '{w['name']}'"
                             + (" (clone)" if r_is_clone else " (impersonation of a licensed name is common)")
                             + f"; your domain {q['domain']} is the licensed firm's registered domain")
            elif qd:
                worse("REVIEW")
                reasons.append(f"A warning by {where} names '{w['name']}'"
                               + (" (clone of a licensed firm)" if r_is_clone else
                                  ", the same name as a licensed firm (often impersonation)")
                               + f"; the register does not confirm {q['domain']} as the licensed firm's domain")
            else:
                worse("REVIEW")
                reasons.append(f"A warning by {where} names '{w['name']}'"
                               + (" (clone of a licensed firm)" if r_is_clone else
                                  ", the same name as a licensed firm (often impersonation)")
                               + ": give the domain you deal with to rule it out")
        elif conf >= STRONG:
            worse("BLOCK"); warn_block = True
            reasons.append(f"On a warning list: {where} lists '{w['name']}' ({how}"
                           + (f", {w['date']}" if w.get("date") else "") + ")")
        else:
            worse("REVIEW")
            reasons.append(f"Possible warning-list match: {where} '{w['name']}' ({how} {conf})")
    if look:
        worse("REVIEW")
        reasons.append(f"Your domain resembles {len(look)} domain(s) on warning lists (e.g. {look[0]['listed_domain']})")

    # licences
    claim = q["claimed_licence"]
    claim_out = None
    if claim:
        t, c = claim["type"], claim["country"] or q["country"]
        if t in CLAIM_TYPES:
            srcs, test = CLAIM_TYPES[t]
            covered_srcs = [s for s in srcs if not c or S.SOURCES[s]["scope"] == "EEA" or c in S.SOURCES[s]["scope"]]
            covered = bool(covered_srcs) and (not c or c in EEA)
        else:
            covered_srcs, covered, test = [], False, None
        found = [o for o in strong_lic if covered and o["source"] in covered_srcs and test({"src": o["source"], "type": o.get("type"), "services": o.get("services") or []})
                 and (not c or o.get("home_state") == c or c in (o.get("passported_to") or []))]
        claim_out = {"claimed": claim["text"], "type": t, "country": c, "covered": covered,
                     "found": bool(found) if covered else None,
                     "found_active": any(eff(o) == "active" for o in found) if covered else None}
        if not covered:
            worse("REVIEW")
            claim_out["note"] = ("not_covered: this licence type/country is not in our registers "
                                 "(see coverage); check the national register")
            reasons.append(f"Claimed licence '{claim['text']}' is not covered by our registers (not_covered)")
        elif not found:
            worse("REVIEW")
            reasons.append(f"Claimed licence '{claim['text']}' not found in {', '.join(covered_srcs)}")
            if any(s.startswith("amf_") for s in covered_srcs):
                claim_out["note"] = ("not on the AMF list as of data_as_of; absence from a list is not proof that "
                                     "the firm is unlicensed (lists lag decisions; EEA firms may operate in France "
                                     "under a passport): check the AMF's own register")
        elif not claim_out["found_active"]:
            worse("REVIEW")
            reasons.append(f"Claimed licence '{claim['text']}' found but not active"
                           + (f" on {as_of}" if as_of else ""))
    if not active_lic:
        worse("REVIEW")
        if strong_lic:
            o = strong_lic[0]
            reasons.append(f"Licence found but {eff(o)}: {o['register']} '{o['name']}'"
                           + (f", withdrawn {o['withdrawn_on']}" if o.get("withdrawn_on") else ""))
        elif lic:
            reasons.append(f"Only a possible licence match: '{lic[0]['name']}' ({lic[0]['match']['method']} "
                           f"{lic[0]['match']['confidence']})")
        elif not warn and not sanc:
            reasons.append("not_found: no authorisation and no warning found in the covered registers")
    elif q["domain"]:
        reg_domains = {registered_domain(d) for o in active_lic for d in o.get("domains") or []}
        if reg_domains and registered_domain(q["domain"]) not in reg_domains:
            worse("REVIEW")
            reasons.append(f"Your domain {q['domain']} differs from the register's ({', '.join(sorted(reg_domains))})")
        elif not reg_domains:
            # a real firm's exact name + any made-up domain must not pass clean (impersonation)
            worse("REVIEW")
            reasons.append(f"domain_unconfirmed: the register lists no website for this firm, so {q['domain']} "
                           "cannot be confirmed as theirs (impersonators reuse real names); verify the domain "
                           "another way, or leave domain out to check licence status only")
    reg_lei = next((o["lei"] for o in active_lic + strong_lic if o.get("lei")), None)
    lei_sec = LEI.section(snap=lei_snap, q=q, register_lei=reg_lei, gleif_info=gleif_info,
                          live=(lambda x: gleif(x)) if gleif and not q["lei"] else None, today=today)
    lei_reasons, lei_notes = LEI.verdict_effects(lei_sec, claims_licence=bool(claim))
    by_lei = [o for o in lic if o["match"]["method"] in ("lei", "parent_lei")]
    mm = LEI.name_mismatch(lei_sec, q["name"], [*((gleif_info or {}).get("other_names") or []),
                                               *(o["name"] for o in by_lei),
                                               *(n for o in by_lei for n in o.get("other_names") or []),
                                               *(c[k] for c in ((lei_snap.changes.get(q["lei"]) or []) if q["lei"]
                                                                and lei_snap else [])
                                                 if c["field"] == "name" for k in ("old", "new") if c[k])])
    if mm:
        lei_reasons.append(mm)
        lei_notes = [n for n in lei_notes if not n.startswith("lei_name_differs")]
    if lei_reasons:
        worse("REVIEW")
        reasons += lei_reasons
    notes += lei_notes
    if q["name"] and mixed_script(q["name"]):
        worse("REVIEW")
        reasons.append(f"name_lookalike: '{q['name']}' mixes Latin with Cyrillic or Greek look-alike letters, a common "
                       "impersonation trick; the match was made on what the name looks like")
    old_lists = []
    for src, m in S.SOURCES.items():
        ts = (ix.meta.get(src) or {}).get("data_as_of")
        if m["kind"] != "licence" and src in ix.sources() and ts and (
                dt.date.fromisoformat(today) - dt.date.fromisoformat(ts[:10])).days > STALE_DAYS:
            old_lists.append(f"{src} ({ts[:10]})")
    if old_lists:
        worse("REVIEW")
        reasons.append(f"Warning/sanctions data older than {STALE_DAYS} days: " + ", ".join(old_lists))
    down = [s for s in S.SOURCES if s not in ix.sources()]
    if down:
        worse("REVIEW")
        reasons.append("Not answered (source unavailable): " + ", ".join(down))

    if any(o["source"] == "amf_biens_divers" for o in lic):
        notes.append("AMF: registering a biens-divers offer is not an incentive to invest; these investments remain "
                     "very risky by nature. The registration covers the named offer only, not the firm's other "
                     "products.")
    if strong_lic and active_lic and not warn and not sanc and not reasons:
        a = active_lic[0]
        listed_as = S.SOURCES[a["source"]].get("listed_as")  # plain wording: what the list says, nothing more
        reasons.append((f"Listed by {a['authority']} as {listed_as}: '{a['name']}'" if listed_as else
                        f"Authorised: {a['register']} '{a['name']}'")
                       + (f" since {a['authorised_since']}" if a.get("authorised_since") else ""))
    status = ("sanctioned" if any(s["match"]["confidence"] >= STRONG for s in sanc) else
              "warned" if warn_block and not active_lic else
              "licensed_and_warned" if warn_block and active_lic else
              "registered_offer" if active_lic and all(o["source"] == "amf_biens_divers" for o in active_lic) else
              "licensed" if active_lic else
              "licence_withdrawn" if strong_lic else
              "possible_match" if (lic or warn or sanc) else
              "not_covered" if claim_out and not claim_out["covered"] else "not_found")
    if status == "registered_offer":
        # a registered biens-divers offer is not a licence of the firm: never ALLOW on it alone
        worse("REVIEW")
        reasons.append("registered_offer: AMF registered this offer, which is not an authorisation of the firm; "
                       "a human should check the offer documents before paying")
    used = sorted({o["source"] for o in lic + warn + sanc})
    out = {
        "verdict": verdict, "status": status, "reasons": reasons, "query": {k: v for k, v in q.items() if v},
        "licences": lic[:MAX_HITS], "warnings": warn[:MAX_HITS], "sanctions": sanc[:MAX_HITS],
        "lookalike_domains": look[:MAX_HITS], "notes": notes,
        "counts": {"licences": len(lic), "warnings": len(warn), "sanctions": len(sanc)},
        "claimed_licence": claim_out, "as_of": as_of or today, "coverage": coverage(ix, unavailable),
        "not_covered": S.NOT_COVERED,
        "attribution": [S.SOURCES[s]["attribution"] for s in used],
        "disclaimer": DISCLAIMER,
    }
    if gleif_info:
        out["gleif"] = gleif_info
    out["lei"] = lei_sec
    if lei_sec and lei_sec.get("lei_source") in ("snapshot", "gleif_api"):
        out["attribution"].append("LEI data: Global Legal Entity Identifier Foundation (GLEIF), CC0")
    return out
