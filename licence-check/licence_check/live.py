"""Live fetchers: the official files behind each source (shared by the `nca-lists` recorder and the Actor's live
fallback) plus two targeted look-ups (ESMA MiFID Solr query, GLEIF LEI record).

Every fetcher takes `get(url, params=None, timeout=60) -> bytes`; the default uses urllib (stdlib only).
"""
from __future__ import annotations

import gzip
import json
import re
import urllib.parse
import urllib.request

from . import sources as S

UA = "LeriderTechnologies-licence-check/1.0 (public register look-up)"
ESMA_SOLR = "https://registers.esma.europa.eu/solr/esma_registers_upreg/select"
EBA_META = "https://euclid.eba.europa.eu/register/api/filemetadata"
ESMA_MICA = "https://www.esma.europa.eu/sites/default/files/2024-12/{}.csv"
FSF_URL = ("https://webgate.ec.europa.eu/fsd/fsf/public/files/xmlFullSanctionsList_1_1/content"
           "?token=dG9rZW4tMjAxNw")
ECB_PAGE = ("https://www.ecb.europa.eu/stats/financial_corporations/list_of_financial_institutions/html/"
            "daily_list-MID.en.html")
AFM_URL = "https://www.afm.nl/nl-nl/consumenten/waarschuwingen/bekijk-de-waarschuwingen/boilerroom"
FSMA_CSV = "https://www.fsma.be/en/warnings/de-companies-operating-unlawfully-in-belgium?page&_format=csv"
DATAGOUV = "https://www.data.gouv.fr/api/1/datasets/{}/"
AMF_DATASETS = {"amf_blacklist": "641d5dab1d84b530f7720b08",
                "amf_psan": "liste-blanche-des-prestataires-de-services-sur-actifs-numeriques-psan",
                "amf_sgp": "651427eaf6eab90fa3db2da3",           # SGP agréées par l'AMF (Licence Ouverte 2.0)
                "amf_biens_divers": "65709c9cae162fbc34e7ec05"}  # liste blanche des offres en biens divers (LO 2.0)
GLEIF = "https://api.gleif.org/api/v1/lei-records/{}"


def urllib_get(url, params=None, timeout=60):
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# ------------------------------------------------------------------------------------------------- raw files
def fetch_raw(src: str, get=urllib_get, variant: str | None = None):
    """-> (raw bytes, meta dict with 'url' and 'published'). Raw = what the recorder stores and parse_raw reads."""
    if src == "ecb_mfi":
        page = get(ECB_PAGE).decode("utf-8", "replace")
        m = re.search(r'href="([^"]*mfi_csv_(\d{6})\.csv\.gz)"', page)
        if not m:
            raise RuntimeError("ECB MFI download link not found")
        url = urllib.parse.urljoin(ECB_PAGE, m.group(1))
        return gzip.decompress(get(url, timeout=120)), {"url": url, "published": "20" + m.group(2)}
    if src == "afm_warnings":
        return get(AFM_URL), {"url": AFM_URL, "published": None}
    if src == "fsma_warnings":
        return get(FSMA_CSV), {"url": FSMA_CSV, "published": None}
    if src in AMF_DATASETS:
        meta = json.loads(get(DATAGOUV.format(AMF_DATASETS[src])))
        res = sorted((r for r in meta.get("resources", []) if (r.get("format") or "").lower() == "csv"),
                     key=lambda r: r.get("last_modified") or "", reverse=True)
        if not res:
            raise RuntimeError(f"{src}: no CSV resource on data.gouv.fr")
        return get(res[0]["url"], timeout=120), {"url": res[0]["url"], "published": res[0].get("last_modified")}
    if src == "mica_casp":
        u = ESMA_MICA.format("CASPS")
        return get(u), {"url": u, "published": None}
    if src == "mica_ncasp":
        u = ESMA_MICA.format("NCASP")
        return get(u), {"url": u, "published": None}
    if src == "mica_issuer":
        u = ESMA_MICA.format("EMTWP" if (variant or "EMT") == "EMT" else "ARTZZ")
        return get(u), {"url": u, "published": None}
    if src == "eu_sanctions":
        return get(FSF_URL, timeout=180), {"url": FSF_URL, "published": None}
    if src == "eba_psd":
        meta = json.loads(get(EBA_META))
        url = meta["golden_copy_path_context"] + meta["latest_version_relative_zip_path"]
        return get(url, timeout=600), {"url": url, "published": meta["latest_version_relative_zip_path"],
                                       "sha256": meta.get("sha256_hash")}
    raise ValueError(f"no raw fetcher for {src}")


def eba_published_sha(get=urllib_get):
    return json.loads(get(EBA_META)).get("sha256_hash")


def fetch_records(src: str, get=urllib_get):
    """Live records for a whole source (Actor fallback). mica_issuer = EMT + ART files."""
    if src == "mica_issuer":
        out, metas = [], []
        for v in ("EMT", "ART"):
            b, m = fetch_raw(src, get, v)
            out += S.parse_raw(src, b, v)
            metas.append(m)
        return out, {"url": metas[0]["url"], "published": None}
    b, m = fetch_raw(src, get)
    return S.parse_raw(src, b), m


# ------------------------------------------------------------------------------------------------- targeted look-ups
def _solr_quote(s):
    return '"' + re.sub(r'(["\\])', r"\\\1", s) + '"'


def esma_mifid_query(name: str | None = None, lei: str | None = None, get=urllib_get, rows=50):
    """Targeted live query of the ESMA MiFID register (parents by LEI or name phrase, then their child documents)."""
    clauses = []
    if lei:
        clauses += [f"ae_lei:{_solr_quote(lei)}", f"ae_headOfficeLei:{_solr_quote(lei)}"]
    if name:
        clauses.append(f"ae_entityName:{_solr_quote(name)}")
    if not clauses:
        return []
    q = "entity_type:ae AND (" + " OR ".join(clauses) + ")"
    d = json.loads(get(ESMA_SOLR, {"q": q, "rows": rows, "wt": "json"}))
    parents = d["response"]["docs"]
    if not parents:
        return []
    roots = " OR ".join(_solr_quote(p["id"]) for p in parents)
    kids = json.loads(get(ESMA_SOLR, {"q": f"_root_:({roots}) AND -entity_type:ae", "rows": 2000, "wt": "json"}))
    return S.esma_mifid_records(parents + kids["response"]["docs"])


def gleif_record(lei: str, get=urllib_get):
    """GLEIF LEI record (CC0): legal name, other names, country, registration number."""
    d = json.loads(get(GLEIF.format(lei), timeout=20))["data"]["attributes"]
    ent = d.get("entity", {})
    return {
        "lei": lei, "legal_name": (ent.get("legalName") or {}).get("name"),
        "other_names": [n.get("name") for n in ent.get("otherNames") or [] if n.get("name")],
        "country": (ent.get("legalAddress") or {}).get("country"),
        "registered_as": ent.get("registeredAs"), "status": ent.get("status"),
        "registration_status": (d.get("registration") or {}).get("status"),
        "next_renewal": (d.get("registration") or {}).get("nextRenewalDate"),
        "successor_lei": (ent.get("successorEntity") or {}).get("lei")
        or next((x.get("lei") for x in ent.get("successorEntities") or [] if x.get("lei")), None),
    }
