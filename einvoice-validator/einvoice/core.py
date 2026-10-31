"""Validate one e-invoice (UBL 2.1 Invoice/CreditNote or UN/CEFACT CII D16B) against the official rules.

Pipeline, per invoice:
  1. safe parse (lxml, no DTD/entities/network)        -> unreadable if not well-formed XML or DOCTYPE present
  2. syntax + profile from the root and CustomizationID  -> which rule sets apply
  3. XML schema (UBL 2.1 / CII D16B)                       -> schema errors are fatal
  4. Schematron rule sets (compiled XSLT, Saxon-HE):      EN 16931 always; Peppol BIS 3.0 and XRechnung 3.0 when the
                                                           invoice declares them (or the caller asks)
  5. verdict: ALLOW (no errors) / BLOCK (errors) / REVIEW (Factur-X/ZUGFeRD MINIMUM or BASIC WL: not a complete
     EN 16931 invoice, so not an e-invoice under the EU mandates), plus warnings and an invoice summary.

Saxon objects are created and used on one dedicated worker thread (SaxonC is not shared across threads).
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import re
from pathlib import Path

from lxml import etree

from . import facturx, rules

NS = {"inv": "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2",
      "cn": "urn:oasis:names:specification:ubl:schema:xsd:CreditNote-2",
      "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
      "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
      "rsm": "urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100",
      "ram": "urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100",
      "udt": "urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100",
      "svrl": "http://purl.oclc.org/dsdl/svrl"}
PEPPOL = "urn:fdc:peppol.eu:2017:poacc:billing:3.0"  # in CustomizationID (ProfileID is ...:billing:01:1.0)
XRECHNUNG = "urn:xeinkauf.de:kosit:xrechnung"
XRECHNUNG_OLD = "urn:xoev-de:kosit:standard:xrechnung"  # XRechnung 1.x/2.x, not accepted since 2024-02-01
EN16931 = "urn:cen.eu:en16931:2017"
FACTURX_PROFILES = {  # guideline id -> (profile name, complete EN 16931 invoice?)
    "urn:factur-x.eu:1p0:minimum": ("MINIMUM", False),
    "urn:factur-x.eu:1p0:basicwl": ("BASIC WL", False),
    "urn:cen.eu:en16931:2017#compliant#urn:factur-x.eu:1p0:basic": ("BASIC", True),
    "urn:cen.eu:en16931:2017#conformant#urn:factur-x.eu:1p0:extended": ("EXTENDED", True),
    "urn:zugferd.de:2p0:minimum": ("MINIMUM", False), "urn:zugferd.de:2p0:basicwl": ("BASIC WL", False),
    "urn:cen.eu:en16931:2017#compliant#urn:zugferd.de:2p0:basic": ("BASIC", True),
    "urn:cen.eu:en16931:2017#conformant#urn:zugferd.de:2p0:extended": ("EXTENDED", True),
}
SUBSET_RULE = re.compile(r"^CII-(DT|SR)-\d+$")
PROFILE_CHOICES = ("auto", "en16931", "peppol", "xrechnung", "all")
MAX_FINDINGS = 200
MAX_XML = 10 * 1024 * 1024  # ~450 MB peak while validating; real invoices are far smaller
_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False)


class NotInvoice(ValueError):
    pass


def parse(data: bytes):
    try:
        root = etree.fromstring(data, parser=_PARSER)
    except etree.XMLSyntaxError as exc:
        raise NotInvoice(f"not well-formed XML ({str(exc)[:160]})") from exc
    except ValueError as exc:  # e.g. a str with an encoding declaration
        raise NotInvoice(f"not readable XML ({str(exc)[:160]})") from exc
    if root.getroottree().docinfo.internalDTD is not None or re.search(rb"<!DOCTYPE|<!ENTITY", data[:65536], re.I):
        raise NotInvoice("XML with a DOCTYPE/ENTITY declaration is refused (e-invoices never use one)")
    return root


def syntax_of(root) -> str:
    ns = etree.QName(root).namespace
    local = etree.QName(root).localname
    if ns == NS["inv"] and local == "Invoice":
        return "ubl-invoice"
    if ns == NS["cn"] and local == "CreditNote":
        return "ubl-creditnote"
    if ns == NS["rsm"] and local == "CrossIndustryInvoice":
        return "cii"
    if local in ("Invoice", "CreditNote", "CrossIndustryInvoice"):
        raise NotInvoice(f"root element {local} has an unexpected namespace ({ns}); not UBL 2.1 or CII D16B")
    if ns and "urn:ferd:CrossIndustryDocument" in ns:
        raise NotInvoice("ZUGFeRD 1.0 invoice: an outdated format that is not EN 16931 and not accepted as an "
                         "e-invoice under the current mandates; ask for ZUGFeRD 2.x / Factur-X (EN 16931 profile)")
    if local == "StandardBusinessDocument":
        raise NotInvoice("this is a Peppol/SBDH envelope; send the invoice inside it (the UBL Invoice element)")
    raise NotInvoice(f"not an e-invoice: root element is {local} (expected UBL Invoice/CreditNote or CII "
                     "CrossIndustryInvoice)")


def _t(root, path) -> str | None:
    el = root.find(path, NS)
    if el is None or el.text is None:
        return None
    return el.text.strip() or None


def customization_id(root, syntax: str) -> str | None:
    if syntax == "cii":
        return _t(root, "rsm:ExchangedDocumentContext/ram:GuidelineSpecifiedDocumentContextParameter/ram:ID")
    return _t(root, "cbc:CustomizationID")


def standards_for(cust: str | None, syntax: str, profile: str) -> tuple[list[str], list[str], dict | None]:
    """-> (rule set ids, human labels, Factur-X profile info or None)"""
    cust = cust or ""
    fx = FACTURX_PROFILES.get(cust) if syntax == "cii" else None
    family = "ubl" if syntax.startswith("ubl") else "cii"
    sets, labels = [f"en16931-{family}"], ["EN 16931"]
    peppol = PEPPOL in cust or profile in ("peppol", "all")
    xr = XRECHNUNG in cust or XRECHNUNG_OLD in cust or profile in ("xrechnung", "all")
    if peppol:
        sets.append(f"peppol-{family}")
        labels.append("Peppol BIS Billing 3.0")
    if xr:
        sets.append(f"xrechnung-{family}")
        labels.append("XRechnung 3.0")
    fx_info = None
    if fx:
        fx_info = {"profile": fx[0], "complete_en16931": fx[1]}
        labels.append(f"Factur-X/ZUGFeRD {fx[0]}")
        if not fx[1]:
            sets, labels = [], [f"Factur-X/ZUGFeRD {fx[0]}"]  # not an EN 16931 invoice: rules don't apply
    return sets, labels, fx_info


class Engine:
    """Compiled rule sets + schemas. All calls go through `run()`, which executes on one Saxon thread."""

    def __init__(self, rules_dir: Path = rules.RULES):
        self.dir = rules_dir
        self.manifest = rules.manifest(rules_dir)
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="saxon")
        self._proc = None
        self._xslt = {}
        self._xsd = {}

    def run(self, fn, *args, **kw):
        return self._pool.submit(fn, *args, **kw)

    def _saxon(self):
        if self._proc is None:
            from saxonche import PySaxonProcessor
            self._proc = PySaxonProcessor(license=False)
            self._x30 = self._proc.new_xslt30_processor()
        return self._proc

    def _exe(self, rid: str):
        if rid not in self._xslt:
            self._saxon()
            exe = self._x30.compile_stylesheet(stylesheet_file=str(self.dir / rules.XSLT[rid]))
            if exe is None:
                raise RuntimeError(f"rule set {rid} failed to compile")
            self._xslt[rid] = exe
        return self._xslt[rid]

    def _schema(self, sid: str):
        if sid not in self._xsd:
            doc = etree.parse(str(self.dir / rules.XSD[sid]))
            self._xsd[sid] = etree.XMLSchema(doc)
        return self._xsd[sid]

    def warm(self, rule_ids=("en16931-ubl", "en16931-cii")):
        for rid in rule_ids:
            self._exe(rid)

    # --- runs on the Saxon thread ---
    def schematron(self, xml: str, rid: str) -> list[dict]:
        """xml: the invoice re-serialised by lxml as text (no declaration), so any declared encoding is honoured."""
        proc = self._saxon()
        exe = self._exe(rid)
        node = proc.parse_xml(xml_text=xml)
        svrl = exe.transform_to_string(xdm_node=node)
        if not svrl:
            raise RuntimeError(f"rule set {rid} produced no report")
        return parse_svrl(svrl.encode("utf-8"), rid)

    def xsd(self, root, syntax: str) -> list[dict]:
        schema = self._schema("cii" if syntax == "cii" else syntax)
        if schema.validate(root):
            return []
        out = []
        for e in list(schema.error_log)[:50]:
            out.append({"rule": "XSD", "severity": "error", "message": e.message[:400],
                        "location": f"line {e.line}", "rule_set": "xml-schema"})
        return out


def parse_svrl(svrl: bytes, rid: str) -> list[dict]:
    root = etree.fromstring(svrl, parser=_PARSER)
    out = []
    for el in root.iter(f"{{{NS['svrl']}}}failed-assert", f"{{{NS['svrl']}}}successful-report"):
        flag = (el.get("flag") or "error").lower()
        severity = {"fatal": "error", "error": "error", "warning": "warning", "warn": "warning",
                    "information": "info", "info": "info"}.get(flag, "error")
        text = " ".join("".join(el.find(f"{{{NS['svrl']}}}text", NS).itertext()).split()) \
            if el.find(f"{{{NS['svrl']}}}text", NS) is not None else ""
        out.append({"rule": el.get("id") or "", "severity": severity, "message": text[:600],
                    "location": (el.get("location") or "")[:300], "rule_set": rid})
    return out


def summary(root, syntax: str) -> dict:
    if syntax == "cii":
        doc = "rsm:ExchangedDocument/"
        tr = "rsm:SupplyChainTradeTransaction/"
        agr, stl = tr + "ram:ApplicableHeaderTradeAgreement/", tr + "ram:ApplicableHeaderTradeSettlement/"
        tot = stl + "ram:SpecifiedTradeSettlementHeaderMonetarySummation/"
        s = {"number": _t(root, doc + "ram:ID"), "type_code": _t(root, doc + "ram:TypeCode"),
             "issue_date": _t(root, doc + "ram:IssueDateTime/udt:DateTimeString"),
             "currency": _t(root, stl + "ram:InvoiceCurrencyCode"),
             "seller": {"name": _t(root, agr + "ram:SellerTradeParty/ram:Name"),
                        "vat_id": _t(root, agr + "ram:SellerTradeParty/ram:SpecifiedTaxRegistration/ram:ID"),
                        "country": _t(root, agr + "ram:SellerTradeParty/ram:PostalTradeAddress/ram:CountryID")},
             "buyer": {"name": _t(root, agr + "ram:BuyerTradeParty/ram:Name"),
                       "vat_id": _t(root, agr + "ram:BuyerTradeParty/ram:SpecifiedTaxRegistration/ram:ID"),
                       "reference": _t(root, agr + "ram:BuyerReference")},
             "totals": {"net": _t(root, tot + "ram:TaxBasisTotalAmount"), "tax": _t(root, tot + "ram:TaxTotalAmount"),
                        "gross": _t(root, tot + "ram:GrandTotalAmount"),
                        "payable": _t(root, tot + "ram:DuePayableAmount")},
             "due_date": _t(root, stl + "ram:SpecifiedTradePaymentTerms/ram:DueDateDateTime/udt:DateTimeString"),
             "ibans": [e.text.strip() for e in root.iterfind(
                 stl + "ram:SpecifiedTradeSettlementPaymentMeans/ram:PayeePartyCreditorFinancialAccount/ram:IBANID",
                 NS) if e.text and e.text.strip()][:5],
             "lines": len(root.findall(tr + "ram:IncludedSupplyChainTradeLineItem", NS))}
    else:
        sup, cus = "cac:AccountingSupplierParty/cac:Party/", "cac:AccountingCustomerParty/cac:Party/"
        tot = "cac:LegalMonetaryTotal/"
        s = {"number": _t(root, "cbc:ID"),
             "type_code": _t(root, "cbc:InvoiceTypeCode") or _t(root, "cbc:CreditNoteTypeCode"),
             "issue_date": _t(root, "cbc:IssueDate"), "currency": _t(root, "cbc:DocumentCurrencyCode"),
             "seller": {"name": _t(root, sup + "cac:PartyLegalEntity/cbc:RegistrationName")
                        or _t(root, sup + "cac:PartyName/cbc:Name"),
                        "vat_id": _t(root, sup + "cac:PartyTaxScheme/cbc:CompanyID"),
                        "country": _t(root, sup + "cac:PostalAddress/cac:Country/cbc:IdentificationCode")},
             "buyer": {"name": _t(root, cus + "cac:PartyLegalEntity/cbc:RegistrationName")
                       or _t(root, cus + "cac:PartyName/cbc:Name"),
                       "vat_id": _t(root, cus + "cac:PartyTaxScheme/cbc:CompanyID"),
                       "reference": _t(root, "cbc:BuyerReference")},
             "totals": {"net": _t(root, tot + "cbc:TaxExclusiveAmount"), "tax": _t(root, "cac:TaxTotal/cbc:TaxAmount"),
                        "gross": _t(root, tot + "cbc:TaxInclusiveAmount"), "payable": _t(root, tot + "cbc:PayableAmount")},
             "due_date": _t(root, "cbc:DueDate") or _t(root, "cac:PaymentMeans/cbc:PaymentDueDate"),
             "ibans": [e.text.strip() for e in root.iterfind(
                 "cac:PaymentMeans/cac:PayeeFinancialAccount/cbc:ID", NS) if e.text and e.text.strip()][:5],
             "lines": len(root.findall("cac:InvoiceLine", NS)) + len(root.findall("cac:CreditNoteLine", NS))}
    return s


def validate(engine: Engine, xml: bytes, *, profile: str = "auto") -> dict:
    """Blocking; call through engine.run(). -> result dict (status: checked | unreadable | not_invoice)."""
    doc = {"sha256": hashlib.sha256(xml).hexdigest(), "bytes": len(xml)}
    if len(xml) > MAX_XML:
        return {"verdict": "BLOCK", "valid": False, "status": "not_invoice", "document": doc,
                "reasons": [f"invoice XML is larger than {MAX_XML // (1024 * 1024)} MB; refused"],
                "errors": [], "warnings": []}
    try:
        root = parse(xml)
        syntax = syntax_of(root)
    except NotInvoice as exc:
        return {"verdict": "BLOCK", "valid": False, "status": "not_invoice", "document": doc,
                "reasons": [str(exc)], "errors": [], "warnings": []}
    cust = customization_id(root, syntax)
    sets, labels, fx = standards_for(cust, syntax, profile)
    findings = engine.xsd(root, syntax)
    schema_ok = not findings
    if schema_ok:  # Schematron on schema-invalid XML gives noise; the schema errors are the answer
        text = etree.tostring(root, encoding="unicode")
        for rid in sets:
            findings += engine.schematron(text, rid)
    if fx and fx["profile"] == "EXTENDED":
        # EXTENDED is a declared superset of EN 16931 ("conformant"): the CII syntax-subset rules that forbid
        # elements outside the EN 16931 core don't apply to it. Business rules (BR-*) still do.
        for f in findings:
            if f["rule_set"] == "en16931-cii" and SUBSET_RULE.match(f["rule"]) and f["severity"] == "error":
                f["severity"] = "warning"
                f["message"] = "(allowed in Factur-X/ZUGFeRD EXTENDED) " + f["message"]
    errors = [f for f in findings if f["severity"] == "error"]
    warnings = [f for f in findings if f["severity"] == "warning"]
    reasons = []
    if errors:
        verdict = "BLOCK"
        first = errors[0]
        reasons.append(f"{len(errors)} error(s) against {', '.join(labels) if schema_ok else 'the XML schema'}"
                       f"; first: {first['rule']} {first['message'][:200]}")
    elif XRECHNUNG_OLD in (cust or ""):
        verdict = "REVIEW"
        reasons.append("outdated XRechnung version (1.x/2.x identifier): German public-sector portals accept only "
                       "XRechnung 3.0 since 1 February 2024; the content passes EN 16931 and the XRechnung 3.0 rules, "
                       "so it is still a valid EN 16931 invoice for B2B")
    elif fx and not fx["complete_en16931"]:
        verdict = "REVIEW"
        reasons.append(f"Factur-X/ZUGFeRD profile {fx['profile']} is not a complete EN 16931 invoice: it is not "
                       "accepted as an e-invoice under the EU/German/French mandates (only as a PDF with "
                       "machine-readable extras); ask for profile EN 16931 (COMFORT), BASIC or EXTENDED")
    else:
        verdict = "ALLOW"
        reasons.append(f"valid {' + '.join(labels)} {'credit note' if syntax == 'ubl-creditnote' else 'invoice'}"
                       + (f" ({len(warnings)} warning(s))" if warnings else ""))
    if not cust:
        reasons.append("no CustomizationID / guideline identifier: validated against EN 16931 only")
    elif EN16931 not in cust and not fx:
        reasons.append(f"CustomizationID {cust[:120]} does not declare EN 16931; validated against EN 16931 anyway")
    return {"verdict": verdict, "valid": not errors and verdict != "REVIEW", "status": "checked",
            "syntax": {"ubl-invoice": "UBL 2.1 Invoice", "ubl-creditnote": "UBL 2.1 CreditNote",
                       "cii": "UN/CEFACT CII D16B"}[syntax],
            "customization_id": cust, "standards": labels, "rule_sets": sets, "facturx": fx,
            "reasons": reasons, "error_count": len(errors), "warning_count": len(warnings),
            "errors": errors[:MAX_FINDINGS], "warnings": warnings[:MAX_FINDINGS],
            "summary": summary(root, syntax), "document": doc, "rules_version": engine.manifest}


def _xml_profile(res: dict) -> str | None:
    if res.get("facturx"):
        return res["facturx"]["profile"]
    cust = res.get("customization_id") or ""
    if XRECHNUNG in cust:
        return "XRechnung"
    if cust == EN16931:
        return "EN 16931"
    return None


def extract_pdf(pdf: bytes):
    """Factur-X extraction (pypdf, no Saxon): run it outside the Saxon thread. -> extraction dict or a result dict"""
    try:
        return facturx.extract(pdf)
    except facturx.AmbiguousInvoice as exc:
        return {"verdict": "BLOCK", "valid": False, "status": "checked", "document":
                {"sha256": hashlib.sha256(pdf).hexdigest(), "bytes": len(pdf)}, "reasons": [str(exc)],
                "errors": [{"rule": "FX-AMBIGUOUS", "severity": "error", "message": str(exc), "location": "",
                            "rule_set": "factur-x"}], "warnings": [], "error_count": 1, "warning_count": 0,
                "container": {"type": "Factur-X/ZUGFeRD PDF"}}
    except facturx.NoInvoiceInPdf as exc:
        return {"verdict": "BLOCK", "valid": False, "status": "not_invoice", "document":
                {"sha256": hashlib.sha256(pdf).hexdigest(), "bytes": len(pdf)}, "reasons": [str(exc)],
                "errors": [], "warnings": [], "container": {"type": "PDF"}}


def validate_pdf(engine: Engine, pdf: bytes, *, profile: str = "auto", extracted: dict | None = None) -> dict:
    """Factur-X / ZUGFeRD PDF: validate the embedded XML, cross-check the PDF metadata. Blocking (Saxon thread)."""
    doc = {"sha256": hashlib.sha256(pdf).hexdigest(), "bytes": len(pdf)}
    ex = extracted if extracted is not None else extract_pdf(pdf)
    if "verdict" in ex:  # not an e-invoice / ambiguous: already a result
        return ex
    res = validate(engine, ex["xml"], profile=profile)
    res["container"] = {"type": "Factur-X/ZUGFeRD PDF", "embedded_file": ex["filename"], "xmp": ex["xmp"],
                        "pdfa_checked": False, "attachments": ex["attachments"]}
    res["embedded_xml"] = res.get("document")
    res["document"] = doc
    if res["status"] == "checked":
        warn = facturx.xmp_mismatch(ex["xmp"], _xml_profile(res))
        if not (ex["xmp"] or {}).get("conformance_level") and ex["filename"].lower() != "xrechnung.xml":
            warn = ("the PDF has no Factur-X/ZUGFeRD XMP metadata (fx:ConformanceLevel); some receivers use it to "
                    "detect the embedded invoice")
        if warn:
            res["warnings"].insert(0, {"rule": "FX-XMP", "severity": "warning", "message": warn, "location": "",
                                       "rule_set": "factur-x"})
            res["warning_count"] += 1
    return res
