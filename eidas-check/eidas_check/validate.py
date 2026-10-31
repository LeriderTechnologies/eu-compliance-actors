"""Validate the signatures in a PDF (PAdES / PDF signatures) against the EU trusted lists, ETSI EN 319 102-1 style.

Engine: pyHanko's AdES long-term validation (`ades_lta_validation`) with a trust manager built from the EU trusted
lists, so a signer's certificate is trusted only if it chains to a trust service listed by an EU member state, and
"qualified" comes from the trusted list plus the certificate's QC statements.

Verdict per signature, then the worst one decides the document:
  BLOCK  : signature broken (hash/crypto failure), certificate revoked, content changed after signing (other than
           allowed form filling / annotations / long-term-validation updates), unreadable signature, unsigned PDF
  REVIEW : signature intact but its trust can't be fully established (signer not under an EU trusted provider,
           validity can't be proven at signing time, revocation info unavailable, forms/annotations changed), or
           not qualified while the caller asked for a qualified signature
  ALLOW  : intact, cryptographically valid, chains to an EU trusted provider, not revoked, content unchanged
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import re
from datetime import datetime

from asn1crypto import cms as asn1_cms
from pyhanko.pdf_utils.reader import PdfFileReader
from pyhanko.sign import fields
from pyhanko.sign.validation.ades import ades_basic_validation, ades_lta_validation
from pyhanko.sign.validation.policy_decl import (PdfSignatureValidationSpec, RevocationInfoGatheringSpec,
                                                SignatureValidationSpec)
from pyhanko.sign.validation.qualified.tsp import TSPTrustManager
from pyhanko.sign.validation.settings import KeyUsageConstraints
from pyhanko_certvalidator.context import CertValidationPolicySpec
from pyhanko_certvalidator.policy_decl import CertRevTrustPolicy, RevocationCheckingPolicy, RevocationCheckingRule

from . import compat, netguard

compat.install()  # accept RFC 8017 DigestInfo without NULL params (e.g. Austrian A-Trust seals)

SIG_TIMEOUT_S = 60
MAX_SIGNATURES = 25
TIME_PROOF_ONLY = {"NO_POE", "OUT_OF_BOUNDS_NO_POE", "REVOCATION_OUT_OF_BOUNDS_NO_POE", "TRY_LATER"}
PAGE_PATH = re.compile(r"PathInRevision\('\.Root\.Pages(\.Kids\[\d+\])*'\)")  # the page tree or a page itself
PAGE_KEYS = re.compile(r"dict key /(Resources|Contents|MediaBox|CropBox|Kids|Pages|Rotate)\b")


def touches_pages(diff_text: str) -> bool:
    """True when pyHanko's change explanation shows the page tree, a page or page content/resources changed."""
    return bool(PAGE_PATH.search(diff_text) or PAGE_KEYS.search(diff_text))
RANK = {"ALLOW": 0, "REVIEW": 1, "BLOCK": 2}

# AdES sub-indications that mean "intact, but trust could not be fully established" -> REVIEW, with a plain reason
INDETERMINATE_REASONS = {
    "NO_CERTIFICATE_CHAIN_FOUND": "the signer's certificate does not chain to a trust service on an EU trusted list",
    "CERTIFICATE_CHAIN_GENERAL_FAILURE": "the signer's certificate chain could not be validated",
    "NO_POE": "long-term proof is incomplete: the signature can't be shown to predate the certificate's expiry or "
              "revocation (e.g. the certificate has expired and the document has no archive timestamp)",
    "OUT_OF_BOUNDS_NO_POE": "the certificate was expired or not yet valid, and there is no timestamp proving the "
                            "signature was made while it was valid",
    "OUT_OF_BOUNDS_NOT_REVOKED": "the certificate was expired or not yet valid at validation time",
    "TRY_LATER": "revocation information was not available yet; try again later",
    "REVOCATION_OUT_OF_BOUNDS_NO_POE": "revocation information does not cover the signing time",
    "REVOKED_NO_POE": "the certificate was revoked and the signature can't be proven to predate the revocation",
    "REVOKED_CA_NO_POE": "a CA certificate in the chain was revoked and the signature can't be proven to predate it",
    "CRYPTO_CONSTRAINTS_FAILURE_NO_POE": "the signature uses an algorithm considered too weak today, with no proof "
                                         "it was made while the algorithm was still acceptable",
    "SIG_CONSTRAINTS_FAILURE": "the signature does not meet the validation constraints",
    "CHAIN_CONSTRAINTS_FAILURE": "the certificate chain does not meet the validation constraints",
    "SIGNED_DATA_NOT_FOUND": "the signed data could not be found",
    "GENERIC": "the signature could not be fully validated",
}


def spec_for(registry, backend=None) -> PdfSignatureValidationSpec:
    rule = RevocationCheckingRule.CHECK_IF_DECLARED  # check OCSP/CRL whenever the certificate points to them
    return PdfSignatureValidationSpec(SignatureValidationSpec(
        cert_validation_policy=CertValidationPolicySpec(
            trust_manager=TSPTrustManager(registry),
            revinfo_policy=CertRevTrustPolicy(RevocationCheckingPolicy(
                ee_certificate_rule=rule, intermediate_ca_cert_rule=rule))),
        revinfo_gathering_policy=RevocationInfoGatheringSpec(fetcher_backend=backend),
        # pyHanko's default accepts any key usage; a signing certificate must allow signing (either bit suffices).
        key_usage_settings=KeyUsageConstraints(key_usage={"digital_signature", "non_repudiation"},
                                               match_all_key_usages=False)))


def _name(cert) -> dict:
    if cert is None:
        return {}
    n = cert.subject.native
    return {k: v for k, v in {"common_name": n.get("common_name"), "organization": n.get("organization_name"),
                              "country": n.get("country_name"),
                              "organization_identifier": n.get("organization_identifier")}.items() if v}


def _iso(t: datetime | None) -> str | None:
    return t.isoformat(timespec="seconds") if t else None


def _enum(v) -> str | None:
    return None if v is None else getattr(v, "name", str(v))


async def check_signature(sig, spec, *, require_qualified: bool, sig_fields: frozenset = frozenset()) -> dict:
    out = {"field": sig.field_name, "verdict": None, "reasons": [], "indication": None, "sub_indication": None,
           "signer": {}, "issuer": {}, "claimed_signing_time": None, "proven_signing_time": None,
           "intact": None, "crypto_valid": None, "trusted": None, "revoked": None, "qualified": None,
           "qualified_type": None, "qscd": None, "trust_service": None, "coverage": None,
           "changed_after_signing": None, "change_detail": None, "timestamped": None}
    try:
        res = await asyncio.wait_for(ades_lta_validation(sig, spec), SIG_TIMEOUT_S)
    except asyncio.TimeoutError:
        out.update(verdict="REVIEW", reasons=["validation timed out (revocation services slow); try again later"])
        return out
    except Exception as exc:  # noqa: BLE001 - a malformed signature must give a verdict, not crash the run
        out.update(verdict="REVIEW", reasons=[f"the validation engine could not process this signature "
                                              f"({type(exc).__name__}); it is not proven good or bad"])
        return out
    checked_now = False
    basic_q = None
    sub = res.ades_subindic
    out["indication"] = type(sub).__name__.replace("AdES", "").upper()  # PASSED / INDETERMINATE / FAILURE
    out["sub_indication"] = _enum(sub)
    out["proven_signing_time"] = _iso(getattr(res, "best_signature_time", None))
    st = res.api_status
    if (out["indication"] == "INDETERMINATE" and out["sub_indication"] in TIME_PROOF_ONLY and st is not None
            and st.intact and st.valid and not st.revoked):
        # Long-term validation could not build a past-time proof. ETSI EN 319 102-1 validates at the current time
        # first: if the signature passes basic validation now (certificate valid, trusted, not revoked), it is valid.
        try:
            basic = await asyncio.wait_for(ades_basic_validation(
                sig.signed_data, spec.signature_validation_spec, raw_digest=sig.compute_digest()), SIG_TIMEOUT_S)
            if type(basic.ades_subindic).__name__ == "AdESPassed" and basic.api_status and basic.api_status.trusted:
                out.update(indication="PASSED", sub_indication="OK_AT_CHECK_TIME")
                checked_now = True
                basic_q = getattr(basic.api_status, "qualification_result", None)
        except Exception:  # noqa: BLE001 - keep the long-term result
            pass
    no_ts = (out["sub_indication"] == "SIG_CONSTRAINTS_FAILURE" and st is not None and st.bottom_line
             and getattr(st, "timestamp_validity", None) is None)
    if no_ts:  # only "no timestamp" may be waived: basic validation (same constraints) must pass on its own
        try:
            basic = await asyncio.wait_for(ades_basic_validation(
                sig.signed_data, spec.signature_validation_spec, raw_digest=sig.compute_digest()), SIG_TIMEOUT_S)
            no_ts = type(basic.ades_subindic).__name__ == "AdESPassed"
        except Exception:  # noqa: BLE001
            no_ts = False
    if no_ts:
        # pyHanko's time-aware validation insists on a signature timestamp; a valid B-B signature without one is
        # still valid now. Report it as passed, with validity proven at check time instead of at signing time.
        out.update(indication="PASSED", sub_indication="OK_NO_TIMESTAMP")
    if st is None:
        out.update(verdict="REVIEW", reasons=[f"signature format is malformed or not supported, so it could not be "
                                              f"validated ({out['sub_indication']})"])
        return out
    out.update(signer=_name(st.signing_cert),
               claimed_signing_time=_iso(getattr(st, "signer_reported_dt", None)),
               intact=st.intact, crypto_valid=st.valid, trusted=st.trusted, revoked=st.revoked,
               coverage=_enum(st.coverage), timestamped=getattr(st, "timestamp_validity", None) is not None)
    if st.signing_cert is not None:
        out["issuer"] = {k: v for k, v in {"common_name": st.signing_cert.issuer.native.get("common_name"),
                                           "organization": st.signing_cert.issuer.native.get("organization_name"),
                                           "country": st.signing_cert.issuer.native.get("country_name")}.items() if v}
    level = st.modification_level
    out["changed_after_signing"] = None if level is None else _enum(level)
    q = getattr(st, "qualification_result", None) or basic_q
    if q is not None:
        out["qualified"] = bool(q.status.qualified)
        out["qualified_type"] = {"QC_ESIGN": "qualified_signature", "QC_ESEAL": "qualified_seal",
                                 "QC_WEB": "qualified_website"}.get(_enum(q.status.qc_type), _enum(q.status.qc_type))
        out["qscd"] = bool(q.status.qc_key_security.is_qscd)
        if q.service_definition is not None:
            out["trust_service"] = q.service_definition.base_info.service_name

    block, review = [], []
    if not st.intact:
        block.append("the document content does not match the signature (changed or corrupted)")
    elif not st.valid:
        block.append("the signature is cryptographically invalid")
    if st.revoked:
        block.append("the signer's certificate is revoked")
    diff = getattr(st, "diff_result", None)
    coverage = _enum(st.coverage)
    if coverage not in (None, "ENTIRE_FILE", "ENTIRE_REVISION"):
        block.append("the signature does not cover its whole document revision (possible byte-range manipulation)")
    if level is not None and _enum(level) == "OTHER":
        detail = str(diff) if diff is not None else ""
        out["change_detail"] = detail[:400] or None
        if touches_pages(detail):
            block.append("pages or page content were changed after signing: what you see may not be what was signed")
        else:
            review.append("the document was changed after signing in a way the validator cannot classify as allowed "
                          "(e.g. signing-tool housekeeping, validation data, annotations); see change_detail")
    elif (level is not None and _enum(level) == "FORM_FILLING" and st.docmdp_ok is not False and diff is not None
          and set(getattr(diff, "changed_form_fields", ()) or ()) <= sig_fields):
        out["changed_after_signing"] = "LATER_SIGNATURES"  # countersigning: only other signature fields were filled
    elif level is not None and _enum(level) in ("FORM_FILLING", "ANNOTATIONS"):
        (review if st.docmdp_ok is not False else block).append(
            f"{_enum(level).lower().replace('_', ' ')} changed after signing"
            + ("" if st.docmdp_ok is not False else ", which the signer did not permit"))
    if out["indication"] == "FAILURE" and not block:
        block.append(f"signature validation failed ({out['sub_indication']})")
    elif out["indication"] == "INDETERMINATE":
        review.append(INDETERMINATE_REASONS.get(out["sub_indication"], INDETERMINATE_REASONS["GENERIC"]))
    if require_qualified and not out["qualified"]:
        review.append("not a qualified signature or seal (you asked for qualified)")
    elif require_qualified and not out["qscd"]:
        review.append("qualified certificate, but not created on a qualified signature/seal creation device (QSCD), "
                      "so not a qualified signature or seal (you asked for qualified)")
    out["reasons"] = block + review
    out["verdict"] = "BLOCK" if block else "REVIEW" if review else "ALLOW"
    if out["verdict"] == "ALLOW":
        kind = (out["qualified_type"] if out["qualified"] and out["qscd"]
                else "advanced signature with a qualified certificate (no QSCD, so not qualified)" if out["qualified"]
                else "advanced (not qualified) signature")
        out["reasons"] = [f"valid {kind.replace('_', ' ')}, signer chains to an EU trusted provider"
                          + (f" ({out['trust_service']})" if out["trust_service"] else "")
                          + ("; no signature timestamp, so validity is proven as of this check, not as of signing"
                             if no_ts else "")
                          + ("; validated with the certificate's current status (long-term proof not available)"
                             if checked_now else "")]
    return out


def _blank_sig(field: str) -> dict:
    return {"field": field, "verdict": None, "reasons": [], "indication": None, "sub_indication": None, "signer": {},
            "issuer": {}, "claimed_signing_time": None, "proven_signing_time": None, "intact": None,
            "crypto_valid": None, "trusted": None, "revoked": None, "qualified": None, "qualified_type": None,
            "qscd": None, "trust_service": None, "coverage": None, "changed_after_signing": None,
            "change_detail": None, "timestamped": None}


def _unsupported_signatures(reader, seen: set[str]) -> list[dict]:
    """Filled signature fields pyHanko skipped (it silently ignores SubFilters it doesn't support, e.g. the legacy
    SHA-1 based adbe.pkcs7.sha1 that some official gazettes still use). Report them as present but not validated
    (REVIEW), with the signer read from the embedded certificate, instead of calling the PDF unsigned."""
    out = []
    try:
        filled = list(fields.enumerate_sig_fields(reader, filled_status=True))
    except Exception:  # noqa: BLE001 - a broken AcroForm: nothing more we can say
        return out
    for name, sig_ref, _ in filled:
        if name in seen:
            continue
        sig = sig_ref.get_object()
        sub = str(sig.get("/SubFilter", "")).lstrip("/") or "unknown"
        signer, issuer = {}, {}
        try:
            cms = asn1_cms.ContentInfo.load(bytes(sig["/Contents"]))
            certs = [c.chosen for c in cms["content"]["certificates"]]
            si = cms["content"]["signer_infos"][0]["sid"].native
            leaf = next((c for c in certs if c.serial_number == si.get("serial_number")), certs[0] if certs else None)
            if leaf is not None:
                signer = _name(leaf)
                issuer = {k: v for k, v in {"common_name": leaf.issuer.native.get("common_name"),
                                            "organization": leaf.issuer.native.get("organization_name"),
                                            "country": leaf.issuer.native.get("country_name")}.items() if v}
        except Exception:  # noqa: BLE001 - unreadable CMS: still report the signature exists
            pass
        legacy = " (legacy SHA-1 based format, deprecated)" if sub == "adbe.pkcs7.sha1" else ""
        out.append({"field": name, "verdict": "REVIEW",
                    "reasons": [f"signature present but in an unsupported format {sub}{legacy}; it was not validated"],
                    "indication": "INDETERMINATE", "sub_indication": "FORMAT_NOT_SUPPORTED", "signer": signer,
                    "issuer": issuer, "claimed_signing_time": None, "proven_signing_time": None, "intact": None,
                    "crypto_valid": None, "trusted": None, "revoked": None, "qualified": None, "qualified_type": None,
                    "qscd": None, "trust_service": None, "coverage": None, "changed_after_signing": None,
                    "change_detail": None, "timestamped": None})
    return out


async def check_pdf(data: bytes, registry, *, require_qualified: bool = False) -> dict:
    """One document -> {"verdict", "status", "reasons", "signatures": [...], "document": {...}}.
    status: "checked" (charged), "unsigned" (charged: that is an answer), "unreadable" (not charged)."""
    doc = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data), "signature_count": 0,
           "document_timestamps": 0}
    try:
        reader = PdfFileReader(io.BytesIO(data), strict=False)
        sigs = list(reader.embedded_regular_signatures)
        doc["document_timestamps"] = len(list(reader.embedded_timestamp_signatures))
    except Exception as exc:  # noqa: BLE001 - any parser failure (corrupt streams, broken xref, unknown filter)
        return {"verdict": "BLOCK", "status": "unreadable", "document": doc, "signatures": [],
                "reasons": [f"not a readable PDF ({type(exc).__name__}: {str(exc)[:120]})"]}
    skipped = _unsupported_signatures(reader, {s.field_name for s in sigs})
    doc["signature_count"] = len(sigs) + len(skipped)
    if not sigs and not skipped:
        return {"verdict": "BLOCK", "status": "unsigned", "document": doc, "signatures": [],
                "reasons": ["the PDF has no signature"]}
    sig_fields = frozenset(s.field_name for s in sigs) | frozenset(s["field"] for s in skipped)
    async with netguard.fetcher_backend() as backend:  # OCSP/CRL/issuer URLs come from the document: public only
        spec = spec_for(registry, backend)
        results = [await check_signature(s, spec, require_qualified=require_qualified, sig_fields=sig_fields)
                   for s in sigs[:MAX_SIGNATURES]]
    for s in sigs[MAX_SIGNATURES:]:  # each one re-analyses later revisions: keep a run's time bounded
        results.append({**_blank_sig(s.field_name), "verdict": "REVIEW", "sub_indication": "NOT_VALIDATED_LIMIT",
                        "reasons": [f"not validated: the document has more than {MAX_SIGNATURES} signatures"]})
    results += skipped
    verdict = max((r["verdict"] for r in results), key=RANK.__getitem__)
    reasons = [f"{r['field']}: {x}" for r in results for x in r["reasons"]
               if r["verdict"] == verdict or verdict == "ALLOW"]
    return {"verdict": verdict, "status": "checked", "document": doc, "signatures": results, "reasons": reasons}
