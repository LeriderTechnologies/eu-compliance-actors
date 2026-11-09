"""Source registry and parsers: raw official files -> normalised records.

Every record is a flat dict:
  src, key, kind ("licence" | "warning" | "sanction"), name, names (aliases), lei, ids (registration numbers),
  country (ISO-2, home state), authority, type, status ("active" | "withdrawn" | "inactive" | "listed" |
  "notified_exclusion"), since, until (ISO dates from the register itself), services, host_states, domains,
  category, date (warning/designation date), parent ({name, lei, id} for branches), detail_url

The parsers are pure functions (bytes or decoded rows in, records out); the recorder `nca-lists` and the index
builder share them. Live fetchers (Actor fallback) live in `live.py`.
"""
from __future__ import annotations

import csv
import html
import io
import json
import re
import xml.etree.ElementTree as ET
import zipfile

from .normalize import domains_in, iso_date, norm_country, norm_domain, norm_id, norm_lei, registered_domain

# ------------------------------------------------------------------------------------------------- source registry
# scope: ISO-2 list of home states the source covers ("EEA" = all 30); live: the Actor may refetch it at run time.
SOURCES = {
    "esma_mifid": dict(
        title="ESMA register of investment firms and credit institutions (MiFID II), incl. AIFM, UCITS ManCos, "
              "crowdfunding providers, trading venues",
        kind="licence", scope="EEA", authority="ESMA (data from all EEA national authorities)",
        url="https://registers.esma.europa.eu/publication/searchRegister?core=esma_registers_upreg",
        licence="ESMA legal notice: reproduction authorised provided the source is acknowledged",
        attribution="Source: ESMA, Register of investment firms (MiFID II)", recorder="fin-registers", live=True),
    "eba_psd": dict(
        title="EBA central register of payment and e-money institutions (PSD2/EMD), incl. AISPs, exempted "
              "institutions, branches; agents not included",
        kind="licence", scope="EEA", authority="EBA (data from all EEA national authorities)",
        url="https://euclid.eba.europa.eu/register/pir/search",
        licence="EBA legal notice: reproduction authorised provided the source is acknowledged",
        attribution="Source: European Banking Authority, Register of payment and electronic money institutions",
        recorder="fin-registers", live=True),
    "ecb_mfi": dict(
        title="ECB list of monetary financial institutions: credit institutions (banks), money market funds, "
              "central banks; EU-27 incl. branches",
        kind="licence", scope=[c for c in ("AT BE BG CY CZ DE DK EE ES FI FR GR HR HU IE IT LT LU LV MT NL PL PT "
                                           "RO SE SI SK").split()],
        authority="European Central Bank",
        url="https://www.ecb.europa.eu/stats/financial_corporations/list_of_financial_institutions/html/"
            "daily_list-MID.en.html",
        licence="ECB copyright notice: reproduction permitted provided the source is acknowledged",
        attribution="Source: European Central Bank, list of monetary financial institutions", recorder="nca-lists",
        live=True),
    "mica_casp": dict(
        title="ESMA interim MiCA register: authorised crypto-asset service providers (CASPs)",
        kind="licence", scope="EEA", authority="ESMA (data from all EEA national authorities)",
        url="https://www.esma.europa.eu/esmas-activities/digital-finance-and-innovation/"
            "markets-crypto-assets-regulation-mica",
        licence="ESMA: reproduction authorised provided the source is acknowledged",
        attribution="Source: ESMA interim MiCA register", recorder="eu-registers", live=True),
    "mica_issuer": dict(
        title="ESMA interim MiCA register: e-money token (EMT) and asset-referenced token (ART) issuers",
        kind="licence", scope="EEA", authority="ESMA (data from all EEA national authorities)",
        url="https://www.esma.europa.eu/esmas-activities/digital-finance-and-innovation/"
            "markets-crypto-assets-regulation-mica",
        licence="ESMA: reproduction authorised provided the source is acknowledged",
        attribution="Source: ESMA interim MiCA register", recorder="eu-registers", live=True),
    "amf_psan": dict(
        title="AMF list of registered digital-asset service providers (PSAN, France, pre-MiCA regime; most "
              "registrations ended 2026 with the MiCA transition)",
        kind="licence", scope=["FR"], authority="Autorité des marchés financiers (FR)",
        url="https://www.data.gouv.fr/fr/datasets/liste-blanche-des-prestataires-de-services-sur-actifs-numeriques-psan/",
        licence="Licence Ouverte / Open Licence 2.0 (Etalab)", attribution="Source: AMF, liste blanche des PSAN "
        "(data.gouv.fr, Licence Ouverte 2.0)", recorder="nca-lists", live=True),
    "amf_sgp": dict(
        title="AMF list of authorised portfolio management companies (sociétés de gestion de portefeuille, SGP; "
              "France; only companies authorised on the publication date)",
        kind="licence", scope=["FR"], authority="Autorité des marchés financiers (FR)",
        url="https://www.data.gouv.fr/fr/datasets/liste-des-societes-de-gestion-de-portefeuille-sgp-agreees-par-lamf/",
        licence="Licence Ouverte / Open Licence 2.0 (Etalab)",
        attribution="Source: AMF, liste des sociétés de gestion de portefeuille agréées (data.gouv.fr, Licence "
                    "Ouverte 2.0; last update = data_as_of)", recorder="nca-lists", live=True,
        listed_as="an authorised portfolio management company (SGP, AMF agrément)"),
    "amf_biens_divers": dict(
        title="AMF whitelist of registered 'biens divers' offers (France: investments in tangible assets such as "
              "wine, forests, livestock, sold with a return promise; Code monétaire et financier art. L. 551-3)",
        kind="licence", scope=["FR"], authority="Autorité des marchés financiers (FR)",
        url="https://www.data.gouv.fr/fr/datasets/liste-blanche-des-offres-en-biens-divers/",
        licence="Licence Ouverte / Open Licence 2.0 (Etalab)",
        attribution="Source: AMF, liste blanche des offres en biens divers (data.gouv.fr, Licence Ouverte 2.0; "
                    "last update = data_as_of)", recorder="nca-lists", live=True,
        listed_as="a registered biens-divers offer (a registration of the offer, not a licence of the firm)"),
    "mica_ncasp": dict(
        title="ESMA interim MiCA register: non-compliant entities (crypto services offered without MiCA "
              "authorisation), reported by all EEA authorities",
        kind="warning", scope="EEA", authority="ESMA (data from all EEA national authorities)",
        url="https://www.esma.europa.eu/esmas-activities/digital-finance-and-innovation/"
            "markets-crypto-assets-regulation-mica",
        licence="ESMA: reproduction authorised provided the source is acknowledged",
        attribution="Source: ESMA interim MiCA register (non-compliant entities)", recorder="eu-registers", live=True),
    "afm_warnings": dict(
        title="AFM warning list (Netherlands): firms warned against by the AFM",
        kind="warning", scope=["NL"], authority="Autoriteit Financiële Markten (NL)",
        url="https://www.afm.nl/nl-nl/consumenten/waarschuwingen/bekijk-de-waarschuwingen/boilerroom",
        licence="AFM website copyright notice: reuse and distribution allowed only with explicit attribution to the AFM",
        attribution="Bron: Autoriteit Financiële Markten (AFM), waarschuwingslijst", recorder="nca-lists", live=True),
    "fsma_warnings": dict(
        title="FSMA list of companies operating unlawfully in Belgium (fraudulent platforms, clones, "
              "unauthorised offers)",
        kind="warning", scope=["BE"], authority="Financial Services and Markets Authority (BE)",
        url="https://www.fsma.be/en/warnings/companies-operating-unlawfully-in-belgium",
        licence="CC BY 4.0 (FSMA disclaimer & copyright page)",
        attribution="Source: FSMA (Belgium), list of companies operating unlawfully, CC BY 4.0", recorder="nca-lists",
        live=True),
    "amf_blacklist": dict(
        title="AMF blacklists (France): unauthorised entities and websites (forex, binary options, crypto "
              "derivatives, miscellaneous goods, identity theft/usurpation)",
        kind="warning", scope=["FR"], authority="Autorité des marchés financiers (FR)",
        url="https://www.data.gouv.fr/fr/datasets/listes-noires-des-entites-non-autorisees-a-proposer-des-produits-"
            "ou-services-financiers-en-france/",
        licence="Licence Ouverte / Open Licence 2.0 (Etalab)",
        attribution="Source: AMF, listes noires (data.gouv.fr, Licence Ouverte 2.0)", recorder="nca-lists", live=True),
    "iosco_iscan": dict(
        title="IOSCO I-SCAN: investor warnings published by securities regulators worldwide (100+ authorities, incl. "
              "BaFin, CNMV, CONSOB, Central Bank of Ireland, FCA, FINMA, ASIC, SEC)",
        kind="warning", scope="EEA", authority="IOSCO (warnings from the national authority named in each record)",
        url="https://www.iosco.org/i-scan/",
        licence="I-SCAN API Terms of Use (non-member): reuse allowed with attribution, without distortion",
        attribution="Source: IOSCO I-SCAN (International Securities & Commodities Alerts Network); each warning was "
                    "published by the national authority named in it",
        recorder="fin-registers", live=False,  # API key stays on the recorder box; the Actor uses the daily build
        fuzzy=False),  # exact/core name, domain and LEI only: some warnings name people (e.g. a fake Facebook page)
    "eu_sanctions": dict(
        title="EU consolidated financial sanctions list (legal entities only)",
        kind="sanction", scope="EEA", authority="European Commission (FSF)",
        url="https://data.europa.eu/data/datasets/consolidated-list-of-persons-groups-and-entities-subject-to-eu-"
            "financial-sanctions",
        licence="Commission reuse notice (Decision 2011/833/EU): reuse authorised, source acknowledged",
        attribution="Source: European Commission, EU Financial Sanctions Files", recorder="eu-registers", live=True),
}

# Not covered, and why (shown in the Actor's coverage block and in the README).
NOT_COVERED = {
    "BaFin (DE), CONSOB (IT), CNMV (ES) warnings outside I-SCAN": "only the warnings these authorities also post to "
        "IOSCO I-SCAN are covered; no bulk export of their own lists",
    "UK FCA register (licences)": "UK, outside EU scope; FCA warnings are covered through IOSCO I-SCAN",
    "National-only licences (consumer credit, insurance distribution, AFM FD register, DNB trust offices...)":
        "not in the EU-level registers; national registers not yet added",
    "Insurance undertakings (EIOPA register)": "not yet added",
}

EBA_SERVICES = {
    "PS_010": "cash placement on a payment account", "PS_020": "cash withdrawal from a payment account",
    "PS_03A": "direct debits", "PS_03B": "card payments", "PS_03C": "credit transfers",
    "PS_04A": "direct debits (credit line)", "PS_04B": "card payments (credit line)",
    "PS_04C": "credit transfers (credit line)", "PS_05A": "issuing payment instruments",
    "PS_05B": "acquiring payment transactions", "PS_060": "money remittance", "PS_070": "payment initiation services",
    "PS_080": "account information services", "ES_010": "issuing electronic money",
}

EBA_TYPES = {
    "PSD_PI": "Payment institution", "PSD_EPI": "Exempted payment institution (small PI)",
    "PSD_EMI": "Electronic money institution", "PSD_EEMI": "Exempted electronic money institution",
    "PSD_AISP": "Account information service provider", "PSD_ENL": "Institution entitled under national law",
    "PSD_EXC": "Service provider excluded from PSD2 scope (notified, not licensed)", "PSD_BR": "Branch",
}


# Hosts shared by many unrelated sites (social networks, app stores, link shorteners, free hosting): a warning that
# points there must not turn the whole platform into a match key.
SHARED_DOMAINS = {
    "facebook.com", "fb.com", "instagram.com", "line.me", "lin.ee", "t.me", "telegram.me", "telegram.org", "wa.me",
    "whatsapp.com", "google.com", "apple.com", "linkedin.com", "x.com", "twitter.com", "youtube.com", "youtu.be",
    "tiktok.com", "discord.gg", "discord.com", "reddit.com", "medium.com", "linktr.ee", "bit.ly", "pages.dev",
    "vercel.app", "netlify.app", "github.io", "herokuapp.com", "wixsite.com", "weebly.com", "blogspot.com",
    "wordpress.com", "webflow.io", "firebaseapp.com", "web.app", "carrd.co", "godaddysites.com", "square.site",
    "glitch.me", "onrender.com", "azurewebsites.net", "cloudfront.net", "amazonaws.com", "wechat.com", "weibo.com",
    "qq.com", "pinterest.com", "tumblr.com", "snapchat.com", "twitch.tv", "quora.com", "iosco.org", "webs.com",
    "jimdo.com", "jimdosite.com", "site123.me", "mystrikingly.com", "framer.website", "notion.site"}
# Free e-mail providers: a fraudster's gmail address says nothing about gmail.com.
FREEMAIL = {
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "msn.com", "yahoo.com", "ymail.com",
    "aol.com", "icloud.com", "me.com", "mail.com", "consultant.com", "protonmail.com", "proton.me", "pm.me",
    "tuta.com", "tutanota.com", "gmx.com", "gmx.net", "gmx.de", "web.de", "yandex.com", "yandex.ru", "mail.ru",
    "zoho.com", "qq.com", "163.com", "126.com", "naver.com", "hushmail.com", "inbox.com", "usa.com", "email.com"}
def _rec(src, key, **kw):
    r = {"src": src, "key": key, "kind": SOURCES[src]["kind"], "name": "", "names": [], "lei": None, "ids": [],
         "country": None, "authority": None, "type": None, "status": None, "since": None, "until": None,
         "services": [], "host_states": [], "domains": [], "category": None, "date": None, "parent": None,
         "detail_url": None}
    r.update({k: v for k, v in kw.items() if v not in (None, "", [])})
    flat = []
    for n in ([r["name"]] if isinstance(r["name"], list) else []) + list(r["names"]):
        flat.extend(n if isinstance(n, list) else [n])
    if isinstance(r["name"], list):
        r["name"] = next((x for x in r["name"] if x), "")
    r["names"] = flat
    r["names"] =sorted({n.strip() for n in r["names"] if n and n.strip() and n.strip() != r["name"]})
    if r["kind"] != "licence":  # a fraudster's gmail address or Facebook page must not make gmail.com/facebook.com warned
        r["domains"] = [d for d in r["domains"] if registered_domain(d) not in SHARED_DOMAINS | FREEMAIL]
    return r


def decode(b: bytes) -> str:
    if b[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return b.decode("utf-16")
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("utf-8", "replace")


# ------------------------------------------------------------------------------------------------- ESMA MiFID (Solr)
def esma_mifid_records(docs):
    """docs: Solr documents (parents type 'ae' + children). Returns one record per parent (head office or branch)."""
    kids: dict[str, list] = {}
    parents = []
    for d in docs:
        if d.get("entity_type") == "ae":
            parents.append(d)
        elif d.get("_root_"):
            kids.setdefault(d["_root_"], []).append(d)
    out = []
    for p in parents:
        ch = kids.get(p["id"], [])
        services = sorted({c.get("ac_serviceName") for c in ch if c.get("entity_type") == "aeActivity"
                           and c.get("ac_serviceName") and (c.get("ac_status") or "Active") == "Active"})
        hosts = sorted({norm_country(c.get("hms_notifiedHostMemberState")) or c.get("hms_notifiedHostMemberState")
                        for c in ch if c.get("entity_type") == "aeNotHostMmbSt" and c.get("hms_notifiedHostMemberState")})
        for h in p.get("ae_hostMemberStates") or []:
            hosts.append(norm_country(h) or h)
        st = (p.get("ae_status") or "").lower()
        status = {"active": "active", "inactive": "withdrawn"}.get(st, "unknown")
        branch = p.get("ae_officeType") == "Branch"
        out.append(_rec(
            "esma_mifid", p["id"], name=p.get("ae_entityName"), names=[p.get("ae_commercialName")],
            lei=norm_lei(p.get("ae_lei") or p.get("ae_headOfficeLei") or ""),
            ids=[x for x in [p.get("ae_micLeiEsmaId")] if x],
            country=norm_country(p.get("ae_homeMemberState")), authority=p.get("ae_competentAuthority") or None,
            type=(p.get("ae_entityTypeLabel") or "") + (" (branch in " + (p.get("ae_hostMemberState") or "?").title()
                                                        + ")" if branch else ""),
            status=status, since=iso_date(p.get("ae_authorisationNotificationDate") or ""),
            until=iso_date(p.get("ae_authorisationWithdrawalEndDate") or p.get("ae_authorisationWithdrawalEndDateStr")
                           or ""),
            services=services, host_states=sorted(set(h for h in hosts if h)),
            domains=[d for d in [norm_domain(p.get("ae_website") or "")] if d],
        ))
    return out


# ------------------------------------------------------------------------------------------------- EBA PSD2/EMD
def _props(e):
    out = {}
    for p in e.get("Properties", []) or []:
        out.update(p)
    return out


def eba_records(entities):
    """entities: EBA golden-copy entity dicts (agents PSD_AG are skipped: often natural persons)."""
    out = []
    for e in entities:
        if not isinstance(e, dict) or e.get("EntityType") in (None, "PSD_AG"):
            continue
        p = _props(e)
        et = e["EntityType"]
        aut = [iso_date(x) for x in (p.get("ENT_AUT") or []) if iso_date(x)]
        if et == "PSD_BR":
            status = "active" if (p.get("DER_CHI_ENT_AUT") or "").lower() == "active" else "inactive"
        elif et == "PSD_EXC":
            status = "notified_exclusion"
        elif aut:
            # EBA ENT_AUT = [authorised, withdrawn, re-authorised, ...]: an even count means currently withdrawn.
            status = "withdrawn" if len(aut) % 2 == 0 else "active"
        else:
            status = "active"
        hosts, services = [], set()
        for s in e.get("Services", []) or []:
            for cc, codes in s.items():
                hosts.append(cc)
                services.update([codes] if isinstance(codes, str) else codes)
        home = norm_country(p.get("ENT_COU_RES") or "")
        parent = None
        if et == "PSD_BR":
            parent = {"id": p.get("ENT_COD_PAR_ENT"), "type": EBA_TYPES.get(p.get("ENT_TYP_PAR_ENT"))}
        out.append(_rec(
            "eba_psd", f"{e['EntityCode']}|{et}", name=p.get("ENT_NAM"), names=[p.get("ENT_NAM_COM")],
            ids=sorted({norm_id(x) for x in [e.get("EntityCode", "").split("!")[-1], p.get("ENT_NAT_REF_COD")] if x}),
            lei=norm_lei(e.get("EntityCode", "")),
            country=home, authority=e.get("CA_OwnerID"), type=EBA_TYPES.get(et, et), status=status,
            since=aut[0] if aut else None, until=aut[-1] if status == "withdrawn" else None,
            services=sorted(f"{EBA_SERVICES.get(c, c)} ({c})" for c in services),
            host_states=sorted({h for h in hosts if h != home}),
            category=p.get("ENT_DES_ACT_EXC_SCP"), parent=parent,
        ))
    return out


def eba_entities_from_zip(zip_bytes, *, skip_types=("PSD_AG",), chunk=1 << 22):
    """Entities of the EBA golden copy ([[disclaimer], [entity, ...]]), streamed one object at a time.
    The file is ~220 MB of JSON, mostly payment agents (PSD_AG) that eba_records skips anyway; json.loads on the
    whole file peaked at ~1.4 GB (a 2 GB Actor). Streaming keeps only the wanted entities in memory."""
    z = zipfile.ZipFile(io.BytesIO(zip_bytes))
    name = next(n for n in z.namelist() if n.endswith(".json"))
    dec = json.JSONDecoder()
    with z.open(name) as raw:
        f = io.TextIOWrapper(raw, encoding="utf-8")
        buf, pos, eof = "", 0, False

        def fill() -> bool:
            nonlocal buf, pos, eof
            if eof:
                return False
            more = f.read(chunk)
            eof = not more
            buf, pos = buf[pos:] + more, 0
            return bool(more)

        def skip_ws() -> str:
            nonlocal pos
            while True:
                while pos < len(buf) and buf[pos] in " \t\r\n":
                    pos += 1
                if pos < len(buf) or not fill():
                    return buf[pos] if pos < len(buf) else ""

        def expect(ch: str) -> None:
            nonlocal pos
            if skip_ws() != ch:
                raise ValueError(f"EBA file: expected {ch!r}")
            pos += 1

        def value():
            nonlocal pos
            while True:
                skip_ws()
                try:
                    obj, end = dec.raw_decode(buf, pos)
                    if end == len(buf) and not eof:  # a number/literal could continue in the next chunk
                        raise json.JSONDecodeError("need more", buf, end)
                    pos = end
                    return obj
                except json.JSONDecodeError:
                    if not fill():
                        raise

        expect("[")
        value()  # [disclaimer]
        if skip_ws() == "]":
            return []
        expect(",")
        expect("[")
        out = []
        if skip_ws() == "]":
            return out
        while True:
            e = value()
            if not (isinstance(e, dict) and e.get("EntityType") in skip_types):
                out.append(e)
            c = skip_ws()
            pos += 1
            if c == "]":
                return out
            if c != ",":
                raise ValueError("EBA file: malformed entity list")


# ------------------------------------------------------------------------------------------------- ECB MFI list
def ecb_mfi_records(b: bytes):
    rows = list(csv.reader(io.StringIO(decode(b)), delimiter="\t"))
    if not rows:
        return []
    h = [c.strip() for c in rows[0]]
    out = []
    for r in rows[1:]:
        if len(r) < len(h):
            continue
        d = dict(zip(h, (c.strip() for c in r)))
        if not d.get("RIAD_CODE") or not d.get("NAME"):
            continue
        cat = d.get("CATEGORY") or ""
        parent = None
        if d.get("HEAD_NAME"):
            parent = {"name": d["HEAD_NAME"], "lei": norm_lei(d.get("HEAD_LEI", "")), "id": d.get("HEAD_RIAD_CODE"),
                      "country": norm_country(d.get("HEAD_COUNTRY_OF_REGISTRATION", ""))}
        out.append(_rec(
            "ecb_mfi", d["RIAD_CODE"], name=d["NAME"], lei=norm_lei(d.get("LEI", "")), ids=[norm_id(d["RIAD_CODE"])],
            country=norm_country(d.get("COUNTRY_OF_REGISTRATION", "")), authority="European Central Bank (MFI list)",
            type=cat + (" (branch of " + parent["name"] + ")" if parent else ""), status="active", parent=parent,
            category=cat))
    return out


# ------------------------------------------------------------------------------------------------- ESMA MiCA CSVs
def csv_dicts(b: bytes, delimiter=","):
    rows = list(csv.reader(io.StringIO(decode(b)), delimiter=delimiter))
    if not rows:
        return []
    h = [c.strip().lstrip("﻿") for c in rows[0]]
    return [{h[i]: (r[i].strip() if i < len(r) else "") for i in range(len(h)) if h[i]}
            for r in rows[1:] if any(c.strip() for c in r)]


def _cc_list(s):
    return sorted({norm_country(x) for x in re.split(r"[|,;\s]+", s or "") if norm_country(x)})


def mica_casp_records(b: bytes):
    out = []
    for i, d in enumerate(csv_dicts(b)):
        home = norm_country(d.get("ae_homeMemberState", ""))
        key = d.get("ae_lei") or f"{home}|{d.get('ae_lei_name')}"
        services = [s.strip() for s in (d.get("ac_serviceCode") or "").split("|") if s.strip()]
        end = iso_date(d.get("ac_authorisationEndDate", ""))
        out.append(_rec(
            "mica_casp", key, name=d.get("ae_lei_name"), names=re.split(r"\s*,\s*", d.get("ae_commercial_name") or ""),
            lei=norm_lei(d.get("ae_lei", "")), country=home, authority=d.get("ae_competentAuthority"),
            type="Crypto-asset service provider (MiCA, Art. 59/63)" if "62" not in (d.get("ac_legal_basis") or "")
            else "Crypto-asset services by a financial entity (MiCA Art. 60 notification)",
            status="withdrawn" if end else "active", since=iso_date(d.get("ac_authorisationNotificationDate", "")),
            until=end, services=services, host_states=[c for c in _cc_list(d.get("ac_serviceCode_cou")) if c != home],
            domains=domains_in(" ".join([d.get("ae_website") or "", d.get("ae_website_platform") or ""]))))
    return out


def mica_issuer_records(b: bytes, kind: str):
    """kind 'EMT' or 'ART'. The issuer of a notified EMT/ART white paper (EMT issuers are banks or EMIs)."""
    out = []
    for d in csv_dicts(b):
        home = norm_country(d.get("ae_homeMemberState", ""))
        other = d.get("ae_authorisation_other_emt") or ("Credit institution" if d.get("ae_credit_institution") ==
                                                        "YES" else "")
        end = iso_date(d.get("ac_authorisationEndDate", ""))
        out.append(_rec(
            "mica_issuer", f"{kind}|{d.get('ae_lei') or d.get('ae_lei_name')}|{d.get('wp_url', '')[:80]}",
            name=d.get("ae_lei_name"), names=[d.get("ae_commercial_name")], lei=norm_lei(d.get("ae_lei", "")),
            country=home, authority=d.get("ae_competentAuthority"),
            type=f"{'E-money token' if kind == 'EMT' else 'Asset-referenced token'} issuer (MiCA)"
                 + (f"; authorised as {other}" if other else ""),
            status="withdrawn" if end else "active", since=iso_date(d.get("ac_authorisationNotificationDate", "")),
            until=end, domains=domains_in(d.get("ae_website") or ""), detail_url=d.get("wp_url") or None))
    return out


def mica_ncasp_records(b: bytes):
    out = []
    for d in csv_dicts(b):
        home = norm_country(d.get("ae_homeMemberState", ""))
        name = d.get("ae_lei_name") or d.get("ae_commercial_name")
        out.append(_rec(
            "mica_ncasp", f"{home}|{name}|{d.get('ae_website', '')}", name=name, names=[d.get("ae_commercial_name")],
            lei=norm_lei(d.get("ae_lei", "")), country=home, authority=d.get("ae_competentAuthority"),
            type="Non-compliant entity (crypto-asset services without MiCA authorisation)", status="listed",
            date=iso_date(d.get("ae_decision_date", "")), domains=domains_in(d.get("ae_website") or ""),
            category=(d.get("ae_reason") or "")[:300] or None, detail_url=_first_url(d.get("ae_comments"))))
    return out


def _first_url(s):
    m = re.search(r"https?://[^\s,;\"]+", s or "")
    return m.group(0) if m else None


# ------------------------------------------------------------------------------------------------- EU FSF sanctions
_FSF = "{http://eu.europa.ec/fpi/fsd/export}"


def fsf_records(b: bytes):
    """Legal entities only (subjectType 'enterprise'); natural persons are never loaded."""
    root = ET.fromstring(b)
    out = []
    for e in root.iter(_FSF + "sanctionEntity"):
        st = e.find(_FSF + "subjectType")
        if st is None or st.get("code") != "enterprise":
            continue
        names = [a.get("wholeName") for a in e.findall(_FSF + "nameAlias") if a.get("wholeName")]
        if not names:
            continue
        reg = e.find(_FSF + "regulation")
        ids = [norm_id(i.get("number")) for i in e.findall(_FSF + "identification") if i.get("number")]
        out.append(_rec(
            "eu_sanctions", e.get("logicalId"), name=names[0], names=names[1:], ids=ids,
            authority="Council of the EU", type="EU financial sanctions (asset freeze)", status="listed",
            date=e.get("designationDate") or None, category=reg.get("programme") if reg is not None else None,
            detail_url=(reg.findtext(_FSF + "publicationUrl") if reg is not None else None)))
    return out


# ------------------------------------------------------------------------------------------------- AFM warnings (NL)
_AFM_LI = re.compile(r'<li>\s*<a href="(?P<href>/[^"]*/bekijk-de-waarschuwingen/[^"]+)"[^>]*>(?P<text>.*?)<span'
                     r'[^>]*>\(opens[^<]*</span></a>\s*(?:<span class="cc-link-data">\((?P<date>[^)]*)\)</span>)?',
                     re.S)


def afm_records(b: bytes):
    t = decode(b)
    out = []
    for m in _AFM_LI.finditer(t):
        text = html.unescape(re.sub(r"<[^>]+>", "", m.group("text"))).strip()
        parts = [x.strip() for x in text.split("|")]
        name = parts[0]
        doms = domains_in(" ".join(parts[1:]))
        if not doms and norm_domain(name) and "." in name and " " not in name:
            doms = [norm_domain(name)]
        clone = re.search(r"\(\s*clone of ([^)]*)\)", name, re.I)
        out.append(_rec(
            "afm_warnings", m.group("href").rsplit("/", 1)[-1], name=name, domains=doms,
            country="NL", authority="AFM", type="AFM warning (no licence / unauthorised offer)", status="listed",
            date=iso_date(m.group("date") or ""), category="clone" if clone else None,
            parent={"clone_of": clone.group(1).strip()} if clone else None,
            detail_url="https://www.afm.nl" + m.group("href")))
    return out


# ------------------------------------------------------------------------------------------------- FSMA warnings (BE)
def fsma_records(b: bytes):
    """One FSMA warning row lists several names and websites (not aligned). One record per name; when a row has a
    single name, the row's websites belong to it; otherwise each website also gets its own domain-only record."""
    out = []
    for i, d in enumerate(csv_dicts(b, delimiter=";")):
        names = [n.strip() for n in re.split(r",\s*(?![^()]*\))", d.get("Company name") or "") if n.strip()]
        doms = domains_in(d.get("Website") or "")
        date = iso_date(d.get("Date") or "")
        title = d.get("Title") or ""
        common = dict(country="BE", authority="FSMA", status="listed", date=date,
                      type="FSMA warning: " + (d.get("Type of fraude") or "unlawful activity"),
                      category=title[:200] or None)
        if len(names) == 1:
            clone = "clone" in names[0].lower()
            out.append(_rec("fsma_warnings", f"{i}|{names[0]}", name=names[0], domains=doms,
                            **{**common, "category": ("clone; " if clone else "") + (title[:180] or "")}))
            continue
        for n in names:
            out.append(_rec("fsma_warnings", f"{i}|{n}", name=n, **{
                **common, "category": ("clone; " if "clone" in n.lower() else "") + (title[:180] or "")}))
        for dm in doms:
            out.append(_rec("fsma_warnings", f"{i}|@{dm}", name=dm, domains=[dm], **common))
    # identical keys can occur when the same warning lists a name twice: keep the first
    seen, uniq = set(), []
    for r in out:
        if r["key"] not in seen:
            seen.add(r["key"]); uniq.append(r)
    return uniq


# ------------------------------------------------------------------------------------------------- AMF (FR)
def amf_blacklist_records(b: bytes):
    """'nom' holds a name, a website or an e-mail address. E-mails are reduced to their domain (personal data)."""
    out = []
    for d in csv_dicts(b, delimiter=";"):
        nom = (d.get("nom") or "").strip()
        if not nom:
            continue
        cat = (d.get("categorie") or "").replace("_", " ")
        parts = [x.strip() for x in re.split(r"\s+/\s+|\s*;\s*", nom) if x.strip()]
        doms, names = [], []
        for x in parts:
            dm = norm_domain(x) if (" " not in x and "." in x) else None
            (doms.append(dm) if dm else names.append(x))
        doms = [x for x in doms if registered_domain(x) not in SHARED_DOMAINS | FREEMAIL]
        if not names and not doms:
            continue  # e.g. only a gmail address: nothing safe to match on
        if "@" in nom and not names:
            how = "e-mail domain"
        elif not names:
            how = "website"
        else:
            how = "name"
        name = names[0] if names else doms[0]
        doms = sorted(set(doms))
        out.append(_rec(
            "amf_blacklist", f"{how}|{name.lower()}|{','.join(doms)}|{d.get('date_inscription')}", name=name,
            names=names[1:], domains=doms,
            country="FR", authority="AMF", type=f"AMF blacklist ({how})", status="listed",
            date=iso_date(d.get("date_inscription") or ""), category=cat or None))
    return out


def amf_psan_records(b: bytes):
    groups: dict[str, list] = {}
    for d in csv_dicts(b, delimiter=";"):
        if d.get("no_amf"):
            groups.setdefault(d["no_amf"], []).append(d)
    out = []
    for no, rows in groups.items():
        d = rows[0]
        statut = (d.get("statut") or "").lower()
        status = "withdrawn" if ("radi" in statut or d.get("date_fin_autorisation")) else "active"
        out.append(_rec(
            "amf_psan", no, name=d.get("entite_nom"), lei=norm_lei(d.get("lei", "")),
            ids=[norm_id(x) for x in (d.get("no_registre_national"), no) if x], country="FR", authority="AMF",
            type=f"PSAN {(d.get('nature_autorisation') or 'registration').lower()} (France, pre-MiCA)",
            status=status, since=iso_date(d.get("date_debut_autorisation") or ""),
            until=iso_date(d.get("date_fin_autorisation") or ""),
            services=sorted({r.get("libelle_activite") for r in rows if r.get("libelle_activite")}),
            domains=domains_in(d.get("site_internet") or ""),
            category=d.get("motif_fin_autorisation") or None))
    return out


def amf_sgp_records(b: bytes):
    """One record per AMF number. The file has one row per activity / instrument / restriction of each SGP and lists
    only SGPs authorised on the publication date (statut 'Vivant'): a firm leaving the list shows up in our history
    as a removal, not as a 'withdrawn' row. Phone numbers are not kept."""
    groups: dict[str, list] = {}
    for d in csv_dicts(b, delimiter=";"):
        if d.get("no_amf"):
            groups.setdefault(d["no_amf"], []).append(d)
    pre = {"activité": "activity", "instrument financier": "instrument", "restrictions": "restriction"}
    order = {"activity": 0, "instrument": 1, "restriction": 2, "other": 3}
    out = []
    for no, rows in groups.items():
        d = rows[0]
        services = set()
        for r in rows:
            lab = (r.get("libelle_activite") or "").strip()
            if not lab:
                continue
            sub = (r.get("libelle_sous_activite") or "").strip()
            kind = pre.get((r.get("libelle_type_activite") or "").strip().lower(), "other")
            services.add(f"{kind}: {lab}" + (f" ({sub})" if sub else ""))
        siret = re.sub(r"\D", "", d.get("siret") or "")
        ids = [no, d.get("no_registre_national"), siret, siret[:9] if len(siret) == 14 else None]
        statut = (d.get("statut") or "").strip().lower()
        form = d.get("forme_juridique") or ""
        out.append(_rec(
            "amf_sgp", no, name=d.get("entite_nom"), lei=norm_lei(d.get("lei", "")),
            ids=sorted({norm_id(x) for x in ids if x and norm_id(x)}),
            country=norm_country(d.get("pays_siege") or "") or "FR", authority="AMF",
            type="Portfolio management company (SGP), AMF agrément",
            status="active" if statut in ("vivant", "") else "unknown",
            since=iso_date(d.get("date_debut_autorisation") or ""),
            services=sorted(services, key=lambda s: (order[s.split(":")[0]], s)),
            domains=domains_in(d.get("site_internet") or ""),
            category=form if form and form != "A renseigner" else None))
    return out


def amf_biens_divers_records(b: bytes):
    """One record per registered offer. The list names offers, not firms ('CAVISSIMA SAS : INVESTIR DANS LE VIN ...',
    'ADAE FRANCE (TREESITION)'): the part before ':' or '(' is kept as another name, so a firm name finds its
    offer. The part in brackets is not (often a generic word such as 'BULLION', which must not give an exact-name
    match). Phone numbers are not kept."""
    out = []
    for d in csv_dicts(b, delimiter=";"):
        no = (d.get("no_amf") or "").strip()
        name = (d.get("offre_nom") or "").strip()
        if not no or not name:
            continue
        aliases = []
        head = re.split(r"\s*[:(]\s*", name, maxsplit=1)[0].strip()
        if head and head != name:
            aliases.append(head)
        out.append(_rec(
            "amf_biens_divers", no, name=name, names=aliases, ids=[norm_id(no)], country="FR", authority="AMF",
            type="Registered biens-divers offer (France, CMF art. L. 551-3): a registration of the offer, not an "
                 "authorisation of the firm and not an AMF recommendation",
            status="active", since=iso_date(d.get("date_enregistrement") or ""),
            domains=domains_in(d.get("url_offre") or ""), detail_url=d.get("url_offre") or None,
            category="biens divers (tangible-asset investment)"))
    return out


# ------------------------------------------------------------------------------------------------- IOSCO I-SCAN
ISCAN_JURIS = {
    "united kingdom": "GB", "switzerland": "CH", "new zealand": "NZ", "thailand": "TH", "australia": "AU",
    "hong kong": "HK", "united states of america": "US", "british columbia": "CA", "ontario": "CA", "quebec": "CA",
    "alberta": "CA", "singapore": "SG", "malaysia": "MY", "ukraine": "UA", "japan": "JP",
    "british virgin islands": "VG", "brazil": "BR", "oman, sultanate of": "OM", "jersey": "JE", "argentina": "AR",
    "egypt": "EG", "chile": "CL", "south africa": "ZA", "panama": "PA", "gibraltar": "GI", "mauritius": "MU",
    "united arab emirates": "AE", "difc, dubai": "AE", "abu dhabi": "AE", "mexico": "MX", "chinese taipei": "TW",
    "isle of man": "IM", "bermuda": "BM", "seychelles": "SC", "nigeria": "NG", "montenegro": "ME",
    "aifc, astana": "KZ", "palestine": "PS", "dominican republic": "DO", "guernsey": "GG", "uruguay": "UY",
    "sri lanka": "LK", "algeria": "DZ", "bahamas": "BS", "kuwait": "KW", "jordan": "JO", "pakistan": "PK",
    "israel": "IL", "belize": "BZ", "qatar": "QA", "armenia": "AM", "paraguay": "PY", "trinidad and tobago": "TT",
    "bangladesh": "BD", "india": "IN", "andorra": "AD", "angola": "AO", "croatia, republic of": "HR",
    "tunisia": "TN", "the netherlands": "NL"}
_ISCAN_PREFIX = re.compile(r"(?i)^\s*(?:warning|advarsel)\s+(?:against|mod)\s+(?:investing\s+with\s+|investering\s+i\s+)?")


def _iscan_names(*fields):
    out = []
    for f in fields:
        for v in (f if isinstance(f, list) else [f]):
            for n in re.split(r"\s*;\s*", v or ""):
                n = _ISCAN_PREFIX.sub("", n).strip()
                if n and n not in out:
                    out.append(n)
    return out


def iscan_records(items):
    """IOSCO I-SCAN warnings (API JSON objects) -> warning records. Domains from the entity's URLs and e-mail
    addresses (domain only: e-mail addresses can be personal data); shared platforms, free-mail providers and the
    warning authority's own site are dropped. Impersonators of licensed firms get category 'clone'."""
    out = []
    for d in items:
        if not isinstance(d, dict) or d.get("id") is None:
            continue
        own = {registered_domain(x) for x in [norm_domain(d.get("nca_url") or "")] if x}
        doms = []
        for u in [d.get("url"), d.get("domain_name"), d.get("fqdn"), *(d.get("other_urls") or [])]:
            x = norm_domain(u or "")
            if x and registered_domain(x) not in SHARED_DOMAINS | FREEMAIL | own and x not in doms:
                doms.append(x)
        for e in d.get("email") or []:
            x = norm_domain(e or "")
            if x and registered_domain(x) not in FREEMAIL | SHARED_DOMAINS | own and x not in doms:
                doms.append(x)
        names = _iscan_names(d.get("commercial_name"), d.get("other_commercial_names") or [],
                             d.get("corporate_names") or [])
        for x in domains_in(" ".join(names)):  # e.g. "VT Markets / www.vtmarkets.com"
            if registered_domain(x) not in SHARED_DOMAINS | own and x not in doms:
                doms.append(x)
        lei = norm_lei(d.get("lei") or "")
        if not names and not doms and not lei:
            continue  # nothing to match on (e.g. a social-media handle only)
        cats = d.get("categories_detailed") or []
        clone = 2 in (d.get("categories") or []) or bool(re.search(r"(?i)\bclone|imperson", " ".join(names)))
        juris = d.get("nca_jurisdiction") or ""
        out.append(_rec(
            "iosco_iscan", str(d["id"]), name=names[0] if names else doms[0], names=names[1:], lei=lei, domains=doms,
            country=norm_country(juris) or ISCAN_JURIS.get(juris.strip().lower()),
            authority=f"{d.get('nca_name') or 'unknown authority'} ({juris}) via IOSCO I-SCAN",
            type="Investor warning (" + ", ".join(cats or ["category not given"]) + ")", status="listed",
            date=iso_date(d.get("nca_registration_date") or "") or iso_date(d.get("validation_date") or ""),
            category="; ".join(["clone"] * clone + list(d.get("products_detailed") or [])) or None,
            detail_url=d.get("nca_url") or None))
    return out


# ------------------------------------------------------------------------------------------------- dispatch
def parse_raw(src: str, b: bytes, variant: str | None = None):
    """Raw official file bytes -> records, for the sources stored as files."""
    if src == "ecb_mfi":
        return ecb_mfi_records(b)
    if src == "mica_casp":
        return mica_casp_records(b)
    if src == "mica_issuer":
        return mica_issuer_records(b, variant or "EMT")
    if src == "mica_ncasp":
        return mica_ncasp_records(b)
    if src == "eu_sanctions":
        return fsf_records(b)
    if src == "afm_warnings":
        return afm_records(b)
    if src == "fsma_warnings":
        return fsma_records(b)
    if src == "amf_blacklist":
        return amf_blacklist_records(b)
    if src == "amf_psan":
        return amf_psan_records(b)
    if src == "amf_sgp":
        return amf_sgp_records(b)
    if src == "amf_biens_divers":
        return amf_biens_divers_records(b)
    if src == "eba_psd":
        return eba_records(eba_entities_from_zip(b))
    raise ValueError(f"no raw parser for {src}")
