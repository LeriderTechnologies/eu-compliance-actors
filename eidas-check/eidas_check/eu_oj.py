"""Fetch an EU Official Journal act's authentic PDF and its electronic seal from the Publications Office (Cellar).

Since 1 October 2023 the OJ L/C series is published act by act; each act's PDFs (all languages) are covered by one
detached XAdES seal of the Publications Office. Cellar's open REST API serves both without the bot wall that
eur-lex.europa.eu puts in front of scripts:

  1. branch notice of the work (CELEX, one language)  -> PDF manifestation item + link to the signature work
  2. tree notice of the signature work                 -> its items; the XAdES file is the plain application/xml one
                                                          (the other XML item is the Office's own ETSI report)
"""
from __future__ import annotations

import re
import urllib.error
import urllib.parse
import urllib.request

from lxml import etree

BASE = "https://publications.europa.eu/resource"
UA = "lerider-eidas-check/0.2 (+https://lerider.com)"
LANGS = {"BG": "bul", "CS": "ces", "DA": "dan", "DE": "deu", "EL": "ell", "EN": "eng", "ES": "spa", "ET": "est",
         "FI": "fin", "FR": "fra", "GA": "gle", "HR": "hrv", "HU": "hun", "IT": "ita", "LT": "lit", "LV": "lav",
         "MT": "mlt", "NL": "nld", "PL": "pol", "PT": "por", "RO": "ron", "SK": "slk", "SL": "slv", "SV": "swe"}
CELEX_RE = re.compile(r"^[0-9CE][0-9]{4}[A-Z]{1,2}[0-9]{4}(\([0-9]{1,3}\))?$")
ELI_RE = re.compile(r"(?:eli/)?(reg|dir|dec|reg_impl|reg_del|dir_impl|dir_del|dec_impl|dec_del)/(\d{4})/(\d{1,5})",
                    re.I)
ELI_SECTOR = {"reg": "R", "reg_impl": "R", "reg_del": "R", "dir": "L", "dir_impl": "L", "dir_del": "L", "dec": "D",
              "dec_impl": "D", "dec_del": "D"}
MAX_PDF = 25 * 1024 * 1024
MAX_SIG = 4 * 1024 * 1024
TIMEOUT_S = 30
_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=True)


class ActError(ValueError):
    pass


def to_celex(act: str) -> str:
    """Accept a CELEX number (32024R1689) or an ELI (http://data.europa.eu/eli/reg/2024/1689/oj, reg/2024/1689)."""
    a = (act or "").strip()
    if CELEX_RE.match(a.upper()):
        return a.upper()
    m = ELI_RE.search(a)
    if m:
        kind, year, num = m.group(1).lower(), m.group(2), int(m.group(3))
        return f"3{year}{ELI_SECTOR[kind]}{num:04d}"
    raise ActError("eu_act must be a CELEX number (e.g. 32024R1689) or an ELI (e.g. "
                   "http://data.europa.eu/eli/reg/2024/1689/oj)")


def _get(url: str, *, accept: str, lang3: str | None = None, limit: int) -> tuple[bytes, str]:
    if not url.startswith(BASE + "/"):
        raise ActError("unexpected Publications Office link")
    headers = {"User-Agent": UA, "Accept": accept}
    if lang3:
        headers["Accept-Language"] = lang3
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            host = urllib.parse.urlsplit(r.geturl()).hostname or ""
            if not (host == "publications.europa.eu" or host.endswith(".publications.europa.eu")):
                raise ActError("the Publications Office redirected to an unexpected host")
            data = r.read(limit + 1)
            ctype = r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise ActError("not found in the EU Publications Office repository") from exc
        raise ActError(f"the EU Publications Office answered HTTP {exc.code}; try again later") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ActError(f"the EU Publications Office could not be reached ({exc}); try again later") from exc
    if len(data) > limit:
        raise ActError("file from the Publications Office is larger than expected; refused")
    return data, ctype


def _cellar_items(notice: bytes) -> list[str]:
    return sorted(set(re.findall(rb"/resource/cellar/([0-9a-f-]{36}\.\d{4}\.\d{2}/DOC_\d+)", notice)),
                  key=lambda b: b)


def fetch(act: str, language: str = "EN", *, want_pdf: bool = True) -> dict:
    """-> {"celex", "language", "pdf": bytes|None, "pdf_url", "signature": bytes, "signature_url"}"""
    celex = to_celex(act)
    lang = (language or "EN").strip().upper()
    if lang not in LANGS:
        raise ActError(f"language must be one of {', '.join(LANGS)}")
    notice, _ = _get(f"{BASE}/celex/{urllib.parse.quote(celex)}", accept="application/xml;notice=branch",
                     lang3=LANGS[lang], limit=40 * 1024 * 1024)
    try:
        root = etree.fromstring(notice, parser=_PARSER)
    except etree.XMLSyntaxError as exc:
        raise ActError("the Publications Office returned an unreadable notice") from exc

    pdf_item, sig_work = None, None
    for man in root.iter("MANIFESTATION"):
        mtype = (man.findtext("MANIFESTATION_TYPE/VALUE") or "").strip().lower()
        if not mtype.startswith("pdf"):
            continue
        uri = man.findtext("URI/VALUE") or ""
        m = re.search(r"/cellar/([0-9a-f-]{36}\.\d{4}\.\d{2})$", uri)
        sw = man.findtext("MANIFESTATION_SIGNED_BY_SIGNATURE_DIGITAL/URI/IDENTIFIER")
        if m and (pdf_item is None or sw):
            pdf_item, sig_work = m.group(1), (sw or "").strip() or sig_work
    if sig_work is None:
        raise ActError(f"no electronic seal found for {celex} in {lang}: only Official Journal acts published from "
                       "October 2023 onwards carry a detached seal (older acts and consolidated texts don't)")
    if not re.fullmatch(r"[0-9a-f-]{36}", sig_work):
        raise ActError("unexpected seal identifier from the Publications Office")

    tree, _ = _get(f"{BASE}/cellar/{sig_work}", accept="application/xml;notice=tree", limit=2 * 1024 * 1024)
    signature, sig_url = None, None
    for item in _cellar_items(tree):
        url = f"{BASE}/cellar/{item.decode()}"
        data, ctype = _get(url, accept="application/xml, */*;q=0.5", limit=MAX_SIG)
        if "etsi" in ctype.lower() or b"ValidationReport" in data[:600]:
            continue  # the Office's own validation report, not the seal
        if b"Signature" in data[:2000] and b"xmldsig" in data[:2000]:
            signature, sig_url = data, url
            break
    if signature is None:
        raise ActError(f"the seal of {celex} could not be retrieved from the Publications Office")

    out = {"celex": celex, "language": lang, "signature": signature, "signature_url": sig_url, "pdf": None,
           "pdf_url": f"{BASE}/cellar/{pdf_item}/DOC_1" if pdf_item else None}
    if want_pdf:
        if not pdf_item:
            raise ActError(f"no PDF of {celex} in {lang} found at the Publications Office")
        out["pdf"], ctype = _get(out["pdf_url"], accept="application/pdf, */*;q=0.5", limit=MAX_PDF)
        if not out["pdf"].startswith(b"%PDF"):
            raise ActError(f"the Publications Office did not return a PDF for {celex} ({ctype})")
    return out
