"""Factur-X / ZUGFeRD 2.x: a PDF (PDF/A-3) with the invoice as embedded CII XML.

extract(pdf) -> {"xml", "filename", "xmp": {"conformance_level", "document_file_name", "version"} | None,
                 "attachments": [...]}
The embedded XML is then validated like any CII invoice. PDF/A-3 conformance itself is not checked (that needs a
full PDF/A validator); the XMP metadata is cross-checked with the XML profile.
"""
from __future__ import annotations

import io
import re

INVOICE_NAMES = ("factur-x.xml", "zugferd-invoice.xml", "xrechnung.xml")
ORDER_NAMES = ("order-x.xml",)
MAX_XML = 10 * 1024 * 1024
# XMP ConformanceLevel -> profile name used by core.FACTURX_PROFILES
XMP_LEVELS = {"MINIMUM": "MINIMUM", "BASIC WL": "BASIC WL", "BASIC": "BASIC", "EN 16931": "EN 16931",
              "COMFORT": "EN 16931", "EXTENDED": "EXTENDED", "XRECHNUNG": "XRECHNUNG"}


class NoInvoiceInPdf(ValueError):
    pass


class AmbiguousInvoice(NoInvoiceInPdf):
    pass


def _xmp_field(xmp: str, name: str) -> str | None:
    m = re.search(rf"<(?:\w+:)?{name}>\s*([^<]+?)\s*</(?:\w+:)?{name}>", xmp) or \
        re.search(rf"(?:\w+:)?{name}\s*=\s*\"([^\"]+)\"", xmp)
    return m.group(1).strip() if m else None


def extract(pdf: bytes) -> dict:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError
    try:
        reader = PdfReader(io.BytesIO(pdf), strict=False)
        attachments = reader.attachments  # {name: [bytes, ...]}
        names = list(attachments.keys())
    except (PdfReadError, ValueError, KeyError, TypeError, AttributeError, RecursionError, OSError) as exc:
        raise NoInvoiceInPdf(f"not a readable PDF ({type(exc).__name__}: {str(exc)[:120]})") from exc
    except Exception as exc:  # noqa: BLE001 - hostile PDFs trip all kinds of parser errors
        raise NoInvoiceInPdf(f"not a readable PDF ({type(exc).__name__})") from exc
    lower = {n.lower(): n for n in names}
    hit = next((lower[n] for n in INVOICE_NAMES if n in lower), None)
    if hit is None:
        if any(n in lower for n in ORDER_NAMES):
            raise NoInvoiceInPdf("this PDF carries an Order-X purchase order, not an invoice")
        xmls = [n for n in names if n.lower().endswith(".xml")]
        if len(xmls) == 1:
            hit = xmls[0]  # e.g. a ZUGFeRD 1.0 "ZUGFeRD-invoice.xml" variant or a vendor-named file
        else:
            raise NoInvoiceInPdf("no embedded e-invoice XML found in this PDF (not Factur-X/ZUGFeRD; a plain PDF "
                                 "invoice is not an e-invoice)" + (f"; attachments: {', '.join(names[:5])}"
                                                                   if names else ""))
    # Ambiguity attack: several embedded invoices (same name twice, or factur-x.xml + zugferd-invoice.xml) with
    # different content. We could validate one while the receiver's software books the other.
    inv_names = [n for n in names if n.lower() in INVOICE_NAMES]  # all spellings, e.g. factur-x.xml + Factur-X.xml
    variants = {bytes(d) for n in inv_names for d in attachments[n]}
    if len(variants) > 1:
        raise AmbiguousInvoice("this PDF embeds more than one different invoice XML (" + ", ".join(inv_names[:5])
                               + "); a receiver may read a different one than the one validated, so it can't be "
                               "trusted")
    data = attachments[hit][0]
    if len(data) > MAX_XML:
        raise NoInvoiceInPdf("embedded invoice XML is larger than 10 MB; refused")
    xmp = None
    try:
        meta = reader.trailer["/Root"].get("/Metadata")
        if meta is not None:
            raw = meta.get_object().get_data()[:1_000_000].decode("utf-8", "replace")
            xmp = {"conformance_level": _xmp_field(raw, "ConformanceLevel"),
                   "document_file_name": _xmp_field(raw, "DocumentFileName"),
                   "version": _xmp_field(raw, "Version")}
    except Exception:  # noqa: BLE001 - metadata is informative only
        xmp = None
    return {"xml": data, "filename": hit, "xmp": xmp, "attachments": names[:20]}


def xmp_mismatch(xmp: dict | None, profile: str | None) -> str | None:
    """A warning text when the PDF metadata declares a different profile than the XML, else None."""
    if not xmp or not xmp.get("conformance_level") or not profile:
        return None
    declared = XMP_LEVELS.get(xmp["conformance_level"].strip().upper())
    if declared and declared != profile and not (declared == "XRECHNUNG" and profile == "XRechnung"):
        return (f"PDF metadata declares profile {xmp['conformance_level']}, but the embedded XML is {profile}; "
                "receivers may reject the mismatch")
    return None
