"""Offline tests for the licence check (no network). Fixtures are small slices of the real official files
(tests/fixtures/); the one natural person in fsf.xml is synthetic.
Run: python3 tests/test_licence_check.py   (or python3 -m pytest -q tests/)"""
import datetime as dt
import gzip
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FX = ROOT / "tests" / "fixtures"
sys.path.insert(0, str(ROOT))
from licence_check import actor, check, index, live, normalize as N, sources as S  # noqa: E402

TODAY = "2026-10-28"


def fx(name):
    return (FX / name).read_bytes()


def fixture_records():
    recs = []
    recs += S.esma_mifid_records(json.loads(fx("mifid.json")))
    recs += S.eba_records(json.loads(fx("eba.json")))
    recs += S.ecb_mfi_records(fx("ecb_mfi.tsv"))
    recs += S.mica_casp_records(fx("mica_casps.csv"))
    recs += S.mica_ncasp_records(fx("mica_ncasp.csv"))
    recs += S.fsf_records(fx("fsf.xml"))
    recs += S.afm_records(fx("afm.html"))
    recs += S.fsma_records(fx("fsma.csv"))
    recs += S.amf_blacklist_records(fx("amf_blacklist.csv"))
    recs += S.amf_psan_records(fx("amf_psan.csv"))
    recs += S.amf_sgp_records(fx("amf_sgp.csv"))
    recs += S.amf_biens_divers_records(fx("amf_biens_divers.csv"))
    # one synthetic MiCA issuer so every source is present
    recs += [S._rec("mica_issuer", "EMT|x", name="Test Token Issuer B.V.", country="NL", status="active",
                    type="E-money token issuer (MiCA)")]
    for r in recs:
        r["history"] = {"first_seen": "2026-10-27", "events": []}
    return recs


def make_index(recs=None, as_of_ts=TODAY + "T08:00:00+00:00", first="2026-10-27"):
    recs = fixture_records() if recs is None else recs
    meta = {s: {"data_as_of": as_of_ts, "first_recorded": first, "records": 1} for s in S.SOURCES}
    return index.Index({"sources": meta, "records": recs})


IX = make_index()


def run(q, ix=None, **kw):
    return check.check_entity(q, ix or IX, today=TODAY, **kw)


# ------------------------------------------------------------------------------------------------ normalisation
def test_name_normalisation_legal_forms_and_diacritics():
    assert N.norm_name("Mollie B.V.") == N.norm_name("MOLLIE BV") == "mollie bv"
    assert N.core_name("Mollie B.V.") == "mollie"
    assert N.core_name("Société Générale S.A.") == "societe generale"
    assert N.core_name("Revolut Payments, UAB") == "revolut payments"
    assert N.core_name("Krakowski Bank Sp. z o.o.") == "krakowski bank"
    assert N.core_name("2bfx trading (clone of 2b trading b.v.)") == "2bfx trading"


def test_domain_normalisation():
    assert N.norm_domain("https://www.Bitvavo.com/nl?x=1") == "bitvavo.com"
    assert N.norm_domain("someone@Example.FR") == "example.fr"
    assert N.norm_domain("https.//coinbase.com") == "coinbase.com"
    assert N.norm_domain("bücher.de") == "xn--bcher-kva.de"
    assert N.norm_domain("not a domain") is None
    assert N.registered_domain("platform.apex-trade.app") == "apex-trade.app"
    assert N.registered_domain("shop.example.co.uk") == "example.co.uk"


def test_lookalike_relation():
    assert N.domain_relation("bitvavo.com", "www.bitvavo.com".replace("www.", "")) == "domain"
    assert N.domain_relation("bitvav0.com", "bitvavo.com") == "lookalike"
    assert N.domain_relation("bitvavo.net", "bitvavo.com") == "lookalike"
    assert N.domain_relation("abc.com", "abd.com") is None  # labels shorter than 5 chars are not compared


def test_lei_checksum():
    assert N.norm_lei("7245002ZUXNOEJ0QPZ44") == "7245002ZUXNOEJ0QPZ44"
    assert N.norm_lei("7245002ZUXNOEJ0QPZ45") is None
    assert N.norm_lei("7245 002Z UXNO EJ0Q PZ44") == "7245002ZUXNOEJ0QPZ44"


def test_dates_and_countries():
    assert N.iso_date("25/06/2025") == N.iso_date("2025-06-25T00:00:00Z") == "2025-06-25"
    assert N.iso_date("19-10-2026") == "2026-10-19"
    assert N.norm_country("NETHERLANDS") == N.norm_country("nl") == "NL"
    assert N.norm_country("EL") == "GR"


# ------------------------------------------------------------------------------------------------ parsers
def test_eba_parser_status_and_agents_skipped():
    recs = S.eba_records(json.loads(fx("eba.json")))
    assert not any("Jansen" in r["name"] for r in recs)  # PSD_AG agents never loaded
    m = {r["type"]: r for r in recs if r["name"] == "Mollie B.V." and r["type"] != "Branch"}
    assert m["Electronic money institution"]["status"] == "active"
    assert m["Electronic money institution"]["since"] == "2025-02-03"
    assert m["Payment institution"]["status"] == "withdrawn" and m["Payment institution"]["until"] == "2025-02-03"
    assert "DE" in m["Electronic money institution"]["host_states"]


def test_passporting_parse():
    emi = next(r for r in S.eba_records(json.loads(fx("eba.json"))) if r["key"] == "NL_DNB!F0038|PSD_EMI")
    assert "NL" not in emi["host_states"] and len(emi["host_states"]) >= 25
    assert any("issuing electronic money" in s for s in emi["services"])


def test_ecb_branch_parent():
    recs = S.ecb_mfi_records(fx("ecb_mfi.tsv"))
    assert recs and all(r["parent"]["lei"] == "7245002ZUXNOEJ0QPZ44" for r in recs)


def test_afm_parser_clone_and_domain():
    recs = {r["key"]: r for r in S.afm_records(fx("afm.html"))}
    assert recs["24era"]["domains"] == ["24era.com"] and recs["24era"]["date"] == "2024-07-02"
    assert recs["2bfx-trading"]["category"] == "clone"


def test_amf_email_reduced_to_domain():
    recs = S.amf_blacklist_records(fx("amf_blacklist.csv"))
    assert not any("@" in json.dumps(r) for r in recs)
    assert any(r["domains"] == ["paris-majeste.com"] for r in recs)
    assert any(r["name"] == "Uniglobe Markets Ltd" and r["domains"] == ["uniglobemarkets.com"] for r in recs)


def test_fsf_legal_entities_only():
    recs = S.fsf_records(fx("fsf.xml"))
    assert len(recs) == 1 and recs[0]["name"] == "Arab Revolutionary Brigades"


def test_mica_casp_parser():
    b = next(r for r in S.mica_casp_records(fx("mica_casps.csv")) if r["name"] == "Bitvavo B.V.")
    assert b["lei"] == "724500MX2WBKDJP9HE56" and b["since"] == "2025-06-26" and b["country"] == "NL"
    assert "bitvavo.com" in b["domains"] and "DE" in b["host_states"]


# ------------------------------------------------------------------------------------------------ verdicts
def test_licensed_emi_allow():
    r = run({"name": "Mollie B.V.", "claimed_licence": "e-money institution NL"})
    assert r["verdict"] == "ALLOW" and r["status"] == "licensed"
    assert r["claimed_licence"]["found_active"] is True
    assert r["disclaimer"] and r["attribution"]


def test_real_name_with_unconfirmed_domain_is_review():
    # exact licensed name + made-up domain must not pass clean
    r = run({"name": "Mollie B.V.", "domain": "molliepay-verify.ru"})
    assert r["verdict"] == "REVIEW"
    assert any(x.startswith("domain_unconfirmed") for x in r["reasons"])
    r = run({"name": "Mollie B.V."})               # licence-only check stays ALLOW
    assert r["verdict"] == "ALLOW"


def test_lei_match_confidence_one():
    r = run({"lei": "724500MX2WBKDJP9HE56"})
    assert r["licences"][0]["match"] == {"method": "lei", "confidence": 1.0}
    assert r["verdict"] == "ALLOW"


def test_parent_lei_matches_branches():
    r = run({"lei": "7245002ZUXNOEJ0QPZ44", "claimed_licence": "bank"})
    assert r["licences"] and all(l["match"]["method"] == "parent_lei" for l in r["licences"])


def test_reg_number_match():
    r = run({"reg_number": "F0038"})
    assert any(l["match"]["method"] == "reg_number" and l["match"]["confidence"] == 1.0 for l in r["licences"])


def test_warning_exact_name_block():
    r = run({"name": "24era"})
    assert r["verdict"] == "BLOCK" and r["status"] == "warned"


def test_warning_domain_block():
    r = run({"name": "Some Other Name", "domain": "https://www.uniglobemarkets.com/fr"})
    assert r["verdict"] == "BLOCK" and any(w["match"]["method"] == "domain" for w in r["warnings"])


def test_lookalike_flagged_not_matched():
    r = run({"name": "Totally Unrelated Ltd", "domain": "uniglobemarket.com"})
    assert r["lookalike_domains"] and not r["warnings"]
    assert r["verdict"] == "REVIEW"


def test_clone_warning_is_review_not_block():
    r = run({"name": "2bfx trading"})
    assert r["verdict"] == "REVIEW"


def test_impersonated_licensed_name():
    recs = fixture_records() + [S._rec("ecb_mfi", "NLX", name="ING Bank N.V.", country="NL", status="active",
                                       type="Credit Institution")]
    for r in recs:
        r.setdefault("history", {"first_seen": "2026-10-27", "events": []})
    ix = make_index(recs)
    r = run({"name": "ING Bank N.V."}, ix)
    assert r["verdict"] == "REVIEW" and "impersonation" in " ".join(r["reasons"])
    r = run({"name": "ING Bank N.V.", "domain": "ing-sparen.example"}, ix)
    assert r["verdict"] == "REVIEW"


def test_sanctions_block():
    r = run({"name": "Arab Revolutionary Brigades"})
    assert r["verdict"] == "BLOCK" and r["status"] == "sanctioned"


def test_mica_non_compliant_block():
    r = run({"name": "Bank Bit", "domain": "bank-bit.com"})
    assert r["verdict"] == "BLOCK"


def test_not_found_is_review():
    r = run({"name": "Jansen Bouw Holding B.V.", "country": "NL"})
    assert r["verdict"] == "REVIEW" and r["status"] == "not_found"
    assert "not_found" in r["reasons"][0]


def test_not_covered_claim():
    r = run({"name": "Mollie B.V.", "claimed_licence": "insurance intermediary NL"})
    assert r["claimed_licence"]["covered"] is False and r["verdict"] == "REVIEW"
    r = run({"name": "Bitvavo B.V.", "claimed_licence": {"type": "bank", "country": "NO"}})
    assert r["claimed_licence"]["covered"] is False  # ECB list has no NO/IS/LI


def test_withdrawn_licence_review():
    r = run({"name": "Revolut Payments UAB"})
    assert r["verdict"] == "REVIEW" and r["status"] == "licence_withdrawn"


def test_as_of_uses_register_dates():
    r = run({"name": "Mollie B.V.", "claimed_licence": "payment institution", "as_of": "2022-06-01"})
    pi = next(l for l in r["licences"] if l["type"] == "Payment institution")
    emi = next(l for l in r["licences"] if l["type"] == "Electronic money institution")
    assert pi["status_as_of"] == "active" and emi["status_as_of"] == "not_yet_authorised"
    assert r["claimed_licence"]["found_active"] is True


def test_as_of_before_history_unknown():
    r = run({"name": "Bunq B.V., Sucursal en España", "as_of": "2025-01-01"})
    l = next(l for l in r["licences"] if l["source"] == "ecb_mfi")
    assert l["status_as_of"] == "unknown" and "before our history" in l["status_note"]
    assert r["verdict"] == "REVIEW"


def test_warning_listed_after_as_of_ignored():
    r = run({"name": "24era", "as_of": "2024-01-01"})
    assert r["verdict"] != "BLOCK"


def test_fuzzy_name_never_block():
    r = run({"name": "24 erra"})
    assert r["verdict"] != "BLOCK"


def test_other_country_lowers_confidence():
    r = run({"name": "Mollie B.V.", "country": "PL"})
    assert all(l["match"]["confidence"] < 0.9 for l in r["licences"] if l["source"] == "eba_psd"
               and "PL" not in (l.get("passported_to") or []))


def test_missing_source_never_allow():
    recs = [r for r in fixture_records() if r["src"] != "afm_warnings"]
    ix = make_index(recs)
    r = run({"name": "Mollie B.V."}, ix)
    assert r["verdict"] == "REVIEW" and r["coverage"]["afm_warnings"]["status"] == "unavailable"


def test_stale_warning_list_never_allow():
    ix = make_index(as_of_ts="2026-10-01T08:00:00+00:00")
    r = run({"name": "Mollie B.V."}, ix)
    assert r["verdict"] == "REVIEW" and "older than" in " ".join(r["reasons"])


def test_input_errors():
    for bad in [{}, {"lei": "X"}, {"name": "Foo", "country": "Narnia"}, {"name": "Foo", "as_of": "28-10-2026"},
                {"domain": "not a domain"}]:
        try:
            run(bad)
        except check.InputError:
            continue
        raise AssertionError(f"expected InputError for {bad}")


def test_batch_errors_not_charged():
    items, n = actor.run_batch({"entities": [{"name": "Mollie B.V."}, {"lei": "bad"}]}, IX, today=TODAY)
    assert n == 1 and "error" in items[1] and items[0]["verdict"]


def test_refresh_live_fallback_with_fake_get():
    """A stale source is refetched through `get`; a failing fetch leaves the snapshot, marked stale."""
    ix = make_index(as_of_ts="2026-09-01T00:00:00+00:00")
    afm = fx("afm.html")

    def get(url, params=None, timeout=60):
        if "afm.nl" in url:
            return afm
        raise OSError("offline")

    failed = actor.refresh(ix, [{"name": "x"}], "auto", 48, get,
                           now=dt.datetime(2026, 10, 28, tzinfo=dt.timezone.utc))
    assert "afm_warnings" not in failed and ix.meta["afm_warnings"]["refreshed"] == "live"
    assert "fsma_warnings" in failed
    r = run({"name": "Mollie B.V."}, ix, unavailable=failed)
    assert r["coverage"]["fsma_warnings"]["status"] == "stale" and r["verdict"] == "REVIEW"


def test_gleif_names_used():
    r = run({"lei": "7245002ZUXNOEJ0QPZ44"}, gleif=lambda lei: {"legal_name": "Mollie B.V.", "other_names": []})
    assert any(l["name"] == "Mollie B.V." for l in r["licences"])


def test_index_build_from_recorder_layout():
    """build() reads fin-registers current files + versioned raw files and derives first_seen / removals."""
    tmp = Path(tempfile.mkdtemp())
    try:
        fin = tmp / "fin-registers"
        fin.mkdir()
        ents = json.loads(fx("eba.json"))
        with gzip.open(fin / "current.eba_psd.jsonl.gz", "wt") as f:
            for e in ents:
                f.write(f"{e['EntityCode']}|{e['EntityType']}\t{json.dumps(e)}\n")
        (fin / "health.log").write_text(json.dumps({"ts": "2026-10-27T10:00:00+00:00",
                                                    "sources": {"eba_psd": "unchanged"}}) + "\n")
        v = tmp / "nca-lists" / "versions" / "afm_warnings"
        v.mkdir(parents=True)
        full = fx("afm.html").decode()
        first = re.sub(r"<li>\s*<a href=\"[^\"]*/24era\".*?</li>", "", full, flags=re.S)
        (v / "20261020T080000Z__aaaa.html.gz").write_bytes(gzip.compress(first.encode()))
        (v / "20261025T080000Z__bbbb.html.gz").write_bytes(gzip.compress(full.encode()))
        idx = index.build(tmp)
        afm = {r["key"]: r for r in idx["records"] if r["src"] == "afm_warnings"}
        assert afm["24era"]["history"]["first_seen"] == "2026-10-25"
        assert afm["24era"]["history"]["events"] == [{"date": "2026-10-25", "op": "+"}]
        assert afm["12-ventures"]["history"]["first_seen"] == "2026-10-20"
        assert idx["sources"]["afm_warnings"]["first_recorded"] == "2026-10-20"
        assert idx["sources"]["eba_psd"]["data_as_of"] == "2026-10-27T10:00:00+00:00"
        out = tmp / "i.json.gz"
        index.save(idx, out)
        assert index.Index(index.load(out)).sources()
    finally:
        shutil.rmtree(tmp)


# ------------------------------------------------------------------------------------------------ LEI section
SNAP_LINES = [
    '#{"schema":1,"data_as_of":"2026-10-28T00:00:00+00:00","changes_since":"2026-08-01","rows":4}',
    "L\t98450064AC9753611E09\tMollie B.V.\tNL\tLAPSED\tACTIVE\t\t30204462\t2026-10-20",
    "L\t724500MX2WBKDJP9HE56\tSome Investment Firm\tDE\tLAPSED\tACTIVE\t\t\t2025-01-01",
    "L\t7245002ZUXNOEJ0QPZ44\tNew Name AG\tDE\tRETIRED\tINACTIVE\t529900T8BM49AURSDO55\t\t",
    "C\t98450064AC9753611E09\t2026-10-21\treg_status\tISSUED\tLAPSED",
    "C\t7245002ZUXNOEJ0QPZ44\t2026-10-01\tname\tOld Name AG\tNew Name AG",
    "C\t7245002ZUXNOEJ0QPZ44\t2026-05-01\tname\tAncient AG\tOld Name AG",
]
SNAP = check.LEI.Snapshot.parse(SNAP_LINES)


def test_lei_kvk_lapsed_with_claim_is_review_never_block():
    r = run({"reg_number": "30204462", "country": "NL", "claimed_licence": "e-money NL"}, lei_snap=SNAP)
    s = r["lei"]
    assert s["lei"] == "98450064AC9753611E09" and s["lei_from"] == "kvk" and s["lei_source"] == "snapshot"
    assert s["registration_status"] == "LAPSED" and s["lapsed_on"] == "2026-10-21" and s["kvk"] == "30204462"
    assert r["status"] == "licensed" and any(l["name"] == "Mollie B.V." for l in r["licences"])  # found via KVK LEI
    assert r["verdict"] == "REVIEW" and any(x.startswith("lei_lapsed") for x in r["reasons"])
    r = run({"kvk": "3020 4462", "name": "Mollie B.V."}, lei_snap=SNAP)  # no claim: note only
    assert r["verdict"] == "ALLOW" and any(x.startswith("lei_lapsed") for x in r["notes"])


def test_lei_from_register_and_unknown_kvk():
    r = run({"lei": "724500MX2WBKDJP9HE56", "claimed_licence": "investment firm"}, lei_snap=SNAP)
    assert r["lei"]["lei_from"] == "input" and r["verdict"] == "REVIEW"
    r = run({"name": "Mollie B.V.", "kvk": "11111111"}, lei_snap=SNAP)
    assert r["lei"]["found"] is False and r["lei"]["lei"] is None and r["verdict"] == "ALLOW"
    assert run({"name": "Mollie B.V."}, lei_snap=SNAP)["lei"] is None  # no LEI known: section null
    try:
        run({"kvk": "123"})
        raise AssertionError("short kvk accepted")
    except check.InputError:
        pass


def test_lei_retired_rename_successor():
    r = run({"lei": "7245002ZUXNOEJ0QPZ44", "name": "Old Name AG", "claimed_licence": "bank"}, lei_snap=SNAP)
    s = r["lei"]
    assert s["registration_status"] == "RETIRED" and s["successor_lei"] == "529900T8BM49AURSDO55"
    assert s["renamed_on"] == "2026-10-01" and [c["date"] for c in s["recent_changes"]] == ["2026-10-01"]  # 90 d
    assert any(x.startswith("lei_inactive") for x in r["reasons"]) and r["verdict"] in ("REVIEW",)
    assert any(x.startswith("lei_renamed") for x in r["notes"]) and s["name_matches"] is False


def test_lei_name_mismatch_review():
    # LEI of one firm with a made-up name: never ALLOW through that LEI's licence
    snap = check.LEI.Snapshot.parse(SNAP_LINES[:1] + ["L\t7245002ZUXNOEJ0QPZ44\tbunq B.V.\tNL\tISSUED\tACTIVE\t\t\t"])
    base = run({"lei": "7245002ZUXNOEJ0QPZ44"}, lei_snap=snap)
    r = run({"lei": "7245002ZUXNOEJ0QPZ44", "name": "Acme Widgets Trading Ltd"}, lei_snap=snap)
    assert r["verdict"] == "REVIEW" and any(x.startswith("lei_name_mismatch") for x in r["reasons"])
    assert r["lei"]["name_best_similarity"] < 0.5 and r["verdict"] != "BLOCK"
    assert not any(x.startswith("lei_name_mismatch") for x in base["reasons"])  # no name given: no rule
    # legit cases do not regress: same name, GLEIF other name, old name from our rename history
    r = run({"lei": "7245002ZUXNOEJ0QPZ44", "name": "BUNQ BV"}, lei_snap=snap)
    assert not any(x.startswith("lei_name_mismatch") for x in r["reasons"])
    r = run({"lei": "7245002ZUXNOEJ0QPZ44", "name": "Totally Different Holding"}, lei_snap=snap,
            gleif=lambda lei: {"lei": lei, "legal_name": "bunq B.V.", "other_names": ["Totally Different Holding"]})
    assert not any(x.startswith("lei_name_mismatch") for x in r["reasons"])
    r = run({"lei": "7245002ZUXNOEJ0QPZ44", "name": "Ancient AG"}, lei_snap=SNAP)  # renamed twice, in history
    assert not any(x.startswith("lei_name_mismatch") for x in r["reasons"])
    for q in ({"lei": "724500MX2WBKDJP9HE56"}, {"name": "Mollie B.V."}):  # existing ALLOW cases stay ALLOW
        assert run(q, lei_snap=snap)["verdict"] == "ALLOW"


def test_lei_live_fallback_and_down():
    g = {"lei": "529900T8BM49AURSDO55", "legal_name": "X AG", "registration_status": "ISSUED", "status": "ACTIVE"}
    r = run({"lei": "529900T8BM49AURSDO55"}, lei_snap=SNAP, gleif=lambda lei: g)
    assert r["lei"]["lei_source"] == "gleif_api" and r["lei"]["registration_status"] == "ISSUED"
    r = run({"lei": "529900T8BM49AURSDO55"}, lei_snap=SNAP)  # resolve_lei off, not baked
    assert r["lei"]["lei_source"] == "not_in_snapshot"

    def down(lei):
        raise TimeoutError
    r = run({"lei": "529900T8BM49AURSDO55"}, lei_snap=None, gleif=down)
    assert r["lei"]["lei_source"] == "unavailable" and r["verdict"] != "BLOCK"


def test_lei_build_and_output_schema():
    import sqlite3
    tmp = Path(tempfile.mkdtemp())
    try:
        db = tmp / "current.sqlite"
        con = sqlite3.connect(db)
        cols = ["lei", "name", "country", "city", "jurisdiction", "legal_form", "category", "entity_status",
                "expiration_reason", "successor_lei", "ra_id", "ra_entity_id", "initial_reg", "last_update",
                "reg_status", "next_renewal", "managing_lou", "validation_sources"]
        con.execute(f"CREATE TABLE current ({', '.join(c + ' TEXT' for c in cols)})")
        con.execute("CREATE TABLE changes (pub TEXT, lei TEXT, field TEXT, old TEXT, new TEXT)")
        con.execute("CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT)")
        con.execute("INSERT INTO meta VALUES ('pub', '2026-10-30 00:00:00')")
        row = lambda lei, c, ra, raid: [lei, "N\t" + lei, c, "", "", "", "", "ACTIVE", "", "", ra, raid, "", "",  # noqa
                                        "ISSUED", "2027-01-01", "", ""]
        for r in (row("A" * 20, "US", "RA1", "1"), row("B" * 20, "NL", "RA000463", "30204462"),
                  row("C" * 20, "DE", "RA2", "x"), row("D" * 20, "US", "RA2", "y"), row("E" * 20, "FR", "", "")):
            con.execute(f"INSERT INTO current VALUES ({','.join('?' * len(cols))})", r)
        con.executemany("INSERT INTO changes VALUES (?,?,?,?,?)", [
            ("2026-10-29 00:00:00", "C" * 20, "name", "Old", "New"),      # EEA + recent -> kept
            ("2026-10-29 00:00:00", "D" * 20, "name", "Old", "New"),      # non-EEA -> dropped
            ("2026-01-01 00:00:00", "E" * 20, "reg_status", "ISSUED", "LAPSED"),  # older than 90 d -> dropped
            ("2026-10-29 00:00:00", "C" * 20, "next_renewal", "a", "b")])  # untracked field
        con.commit()
        ixf = tmp / "index.json.gz"
        with gzip.open(ixf, "wt") as f:
            json.dump({"records": [{"lei": "A" * 20}]}, f)
        meta = check.LEI.build(db, ixf, tmp / "lei.tsv.gz")
        assert meta["rows"] == 3 and meta["change_rows"] == 1 and meta["register_leis_found"] == 1
        snap = check.LEI.load(tmp / "lei.tsv.gz")
        assert set(snap.rows) == {"A" * 20, "B" * 20, "C" * 20} and snap.kvk == {"30204462": ["B" * 20]}
        assert snap.record("A" * 20)["legal_name"] == "N " + "A" * 20  # tab cleaned
    finally:
        shutil.rmtree(tmp)
    # output: JSON-serialisable, KVK/ids as strings, lei field declared nullable in the dataset schema
    r = run({"reg_number": "30204462", "country": "NL"}, lei_snap=SNAP)
    json.dumps(r)
    assert isinstance(r["lei"]["kvk"], str) and isinstance(r["query"]["kvk"], str)
    ds = json.loads((ROOT / ".actor" / "dataset_schema.json").read_text())["fields"]["properties"]
    assert "null" in ds["lei"]["type"] and "null" in ds["gleif"]["type"]
    items, n = actor.route({"entities": [{"name": "Mollie B.V."}, {"kvk": "30204462"}, {"lei": "bad"}],
                            "refresh": "never"}, ix=IX, live=False, lei_snap=SNAP, today=TODAY)
    assert n == 2 and items[1]["lei"]["lei_from"] == "kvk" and "error" in items[2]




def test_every_source_has_licence_and_attribution():
    for s, m in S.SOURCES.items():
        assert m["licence"] and m["attribution"] and m["url"].startswith("https://"), s


# ------------------------------------------------------------------------------------------------ AMF SGP + biens divers
def test_amf_sgp_parser():
    recs = {r["key"]: r for r in S.amf_sgp_records(fx("amf_sgp.csv"))}
    assert set(recs) == {"GP-08000008", "GP-15000006", "GS-95000032"}  # one record per AMF number, not per row
    m = recs["GP-08000008"]
    assert m["name"] == "MANDARINE GESTION" and m["lei"] == "969500EWBJ0A1W6UTO35" and m["status"] == "active"
    assert {"GP08000008", "50122120400016", "501221204"} <= set(m["ids"])  # AMF no., SIRET, SIREN from SIRET
    assert m["domains"] == ["mandarine-gestion.com"] and m["since"] and m["country"] == "FR"
    assert "activity: 1 - OPCVM" in m["services"] and m["services"][0].startswith("activity: ")
    assert recs["GS-95000032"]["services"] == [] and "809672165" in recs["GP-15000006"]["ids"]
    assert not any(k == "telephone" or "phone" in json.dumps(r).lower() for r in recs.values() for k in r)


def test_amf_sgp_listed_wording_and_claims():
    r = run({"name": "Mandarine Gestion", "country": "FR", "claimed_licence": "UCITS management company FR"})
    assert r["verdict"] == "ALLOW" and r["status"] == "licensed", r["reasons"]
    assert r["reasons"][0].startswith("Listed by AMF as an authorised portfolio management company (SGP")
    assert r["claimed_licence"]["type"] == "ucits_manco" and r["claimed_licence"]["found_active"] is True
    r = run({"lei": "969500EWBJ0A1W6UTO35", "claimed_licence": "société de gestion de portefeuille FR"})
    assert r["claimed_licence"]["type"] == "sgp" and r["verdict"] == "ALLOW"
    assert any(a.startswith("Source: AMF, liste des sociétés de gestion") for a in r["attribution"])
    r = run({"name": "Elevation Capital Partners", "country": "FR", "claimed_licence": "AIFM FR"})
    assert r["claimed_licence"]["found_active"] is True  # 'Gestion de FIA' activity
    r = run({"name": "Elevation Capital Partners", "country": "FR", "claimed_licence": {"type": "ucits_manco",
                                                                                       "country": "FR"}})
    assert r["verdict"] == "REVIEW" and r["claimed_licence"]["found"] is False  # no OPCVM activity on the list
    assert "not proof that the firm is unlicensed" in r["claimed_licence"]["note"]
    r = run({"reg_number": "809672165", "country": "FR"})  # SIREN
    assert r["licences"][0]["source"] == "amf_sgp" and r["licences"][0]["match"]["method"] == "reg_number"


def test_amf_absent_is_not_unlicensed():
    r = run({"name": "Gestion Imaginaire SAS", "country": "FR", "claimed_licence": "SGP FR"})
    assert r["verdict"] == "REVIEW" and r["status"] == "not_found" and r["claimed_licence"]["covered"] is True
    assert "not proof that the firm is unlicensed" in r["claimed_licence"]["note"]
    assert not any("unlicensed" in x.lower() and "not proof" not in x for x in r["reasons"])


def test_amf_biens_divers_offer_wording():
    recs = {r["key"]: r for r in S.amf_biens_divers_records(fx("amf_biens_divers.csv"))}
    assert len(recs) == 12 and recs["D-19-01"]["names"] == ["CAVISSIMA SAS"]
    assert recs["D-26-01"]["names"] == ["LA MONNAIE DE PARIS"]  # bracket part ('BULLION') is not an alias
    assert recs["D-18-01"]["domains"] == ["ecotree.green"]
    r = run({"name": "Cavissima SAS", "domain": "cavissima.com", "claimed_licence": "biens divers FR"})
    assert r["verdict"] == "REVIEW" and r["status"] == "registered_offer", r["reasons"]
    assert r["reasons"][0].startswith("Listed by AMF as a registered biens-divers offer (a registration of the offer")
    assert any("very risky" in n for n in r["notes"]) and r["claimed_licence"]["type"] == "biens_divers"
    assert "licensed" not in r["status"] and not any("Authorised" in x for x in r["reasons"])
    r = run({"reg_number": "D-25-02", "country": "FR"})
    assert r["licences"][0]["name"] == "ADAE FRANCE (TREESITION)" and r["status"] == "registered_offer"
    r = run({"name": "Bullion"})
    assert not any(l["source"] == "amf_biens_divers" and l["match"]["confidence"] >= check.STRONG
                   for l in r["licences"])


def test_amf_new_sources_in_recorder_index_and_live():
    for s in ("amf_sgp", "amf_biens_divers"):
        assert S.SOURCES[s]["recorder"] == "nca-lists" and "Licence Ouverte" in S.SOURCES[s]["licence"]
        assert ("nca-lists", s) in index.FILE_SOURCES and s in live.AMF_DATASETS


if __name__ == "__main__":
    fails = 0
    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for name, fn in tests:
        try:
            fn()
            print("ok  ", name)
        except Exception as e:  # noqa: BLE001
            fails += 1
            print("FAIL", name, type(e).__name__, e)
    print(f"{len(tests) - fails}/{len(tests)} passed")
    sys.exit(1 if fails else 0)
