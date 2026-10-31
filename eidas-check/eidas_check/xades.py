"""Validate XML signatures (XAdES / XML-DSig) against the EU trusted lists, ETSI EN 319 102-1 style.

Two shapes:
  detached  : a signature file (.xml) over separate files, directly (Reference URI="contract.pdf") or through a signed
              ds:Manifest (every EU Official Journal act since 2023 is sealed this way: one XAdES file covering the
              act in all 24 languages). The caller's document must match one of the signed digests.
  enveloped : the signature sits inside the signed XML document itself (URI="" or an Id reference).

Engine: signxml checks the XML-DSig core (SignedInfo signature, reference digests, XAdES signing-certificate binding;
it resolves Id references strictly, so signature-wrapping tricks fail); we then match the caller's file against
the *verified* references only, check the signature timestamp, build the signer's chain to an EU trusted list with
pyhanko-certvalidator, and get "qualified" from pyHanko's trusted-list assessor.

Time: the chain is validated now first. If that fails only because the certificate has since expired (or its
revocation status can no longer be fetched) and a trusted signature timestamp exists, it is validated at the
timestamp time with the revocation data embedded in the signature (long-term validation, XAdES-LT/LTA). Archive
timestamps are reported but not verified.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import datetime as dt
import hashlib
import re

from asn1crypto import cms as a_cms, crl as a_crl, ocsp as a_ocsp, x509 as a_x509
from lxml import etree
from pyhanko.sign.validation.generic_cms import validate_tst_signed_data
from pyhanko.sign.validation.qualified.assess import QualificationAssessor
from pyhanko.sign.validation.qualified.tsp import TSPTrustManager
from . import netguard
from pyhanko_certvalidator import CertificateValidator, ValidationContext, errors as cv_errors
from pyhanko_certvalidator.policy_decl import CertRevTrustPolicy, RevocationCheckingPolicy, RevocationCheckingRule
from pyhanko_certvalidator.authority import AuthorityWithCert, TrustAnchor, TrustedServiceType, TrustQualifiers
from signxml import SignatureConfiguration, XMLVerifier
from signxml import exceptions as sx_errors
from signxml.algorithms import DigestAlgorithm, SignatureMethod
from signxml.xades import XAdESVerifier

DS = "http://www.w3.org/2000/09/xmldsig#"
XADES_NS = ("http://uri.etsi.org/01903/v1.3.2#", "http://uri.etsi.org/01903/v1.4.1#")
NS = {"ds": DS, "x": XADES_NS[0], "x141": XADES_NS[1]}
SIG_TIMEOUT_S = 60
MAX_SIGNATURES = 25
MAX_KEY_CANDIDATES = 10
MAX_MANIFEST_REFS = 10000
MANIFEST_TYPE = "http://www.w3.org/2000/09/xmldsig#Manifest"
DIGESTS = {"http://www.w3.org/2001/04/xmlenc#sha256": "sha256", "http://www.w3.org/2001/04/xmlenc#sha512": "sha512",
           "http://www.w3.org/2001/04/xmldsig-more#sha384": "sha384", "http://www.w3.org/2001/04/xmldsig-more#sha224":
           "sha224", "http://www.w3.org/2000/09/xmldsig#sha1": "sha1", "http://www.w3.org/2007/05/xmldsig-more#sha3-256":
           "sha3_256", "http://www.w3.org/2007/05/xmldsig-more#sha3-512": "sha3_512"}
C14N = {"http://www.w3.org/2001/10/xml-exc-c14n#": (True, False),
        "http://www.w3.org/2001/10/xml-exc-c14n#WithComments": (True, True),
        "http://www.w3.org/TR/2001/REC-xml-c14n-20010315": (False, False),
        "http://www.w3.org/TR/2001/REC-xml-c14n-20010315#WithComments": (False, True),
        "http://www.w3.org/2006/12/xml-c14n11": (False, False),  # = 1.0 unless xml:id/xml:base fixups apply
        "http://www.w3.org/2006/12/xml-c14n11#WithComments": (False, True)}
WEAK = {"sha1"}


def signer_revinfo(retroactive: bool) -> CertRevTrustPolicy:
    """Same as the PDF path: OCSP/CRL are checked whenever the certificate points to them, and must then succeed.
    retroactive: at a past moment (timestamp), accept revocation data issued after it (e.g. the OCSP response the
    signer embedded minutes after signing)."""
    return CertRevTrustPolicy(RevocationCheckingPolicy(
        ee_certificate_rule=RevocationCheckingRule.CHECK_IF_DECLARED,
        intermediate_ca_cert_rule=RevocationCheckingRule.CHECK_IF_DECLARED), retroactive_revinfo=retroactive)

_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False,
                          remove_blank_text=False)


class NotXML(ValueError):
    pass


def looks_like_xml(data: bytes) -> bool:
    head = data[:512].lstrip(b"\xef\xbb\xbf \t\r\n")
    return head.startswith(b"<")


def parse(data: bytes):
    try:
        root = etree.fromstring(data, parser=_PARSER)
    except etree.XMLSyntaxError as exc:
        raise NotXML(f"not well-formed XML ({str(exc)[:120]})") from exc
    if root.getroottree().docinfo.internalDTD is not None or re.search(rb"<!DOCTYPE|<!ENTITY", data[:65536], re.I):
        raise NotXML("XML with a DOCTYPE/ENTITY declaration is refused (entity-expansion protection)")
    return root


def _b64(text: str | None) -> bytes:
    return base64.b64decode(re.sub(r"\s+", "", text or ""), validate=True)


def _q(el, path):
    """findall over both XAdES namespaces."""
    return el.findall(path.replace("x:", "{%s}" % XADES_NS[0]), NS) + el.findall(
        path.replace("x:", "{%s}" % XADES_NS[1]), NS)


def _name(cert) -> dict:
    if cert is None:
        return {}
    n = cert.subject.native
    return {k: v for k, v in {"common_name": n.get("common_name"), "organization": n.get("organization_name"),
                              "country": n.get("country_name"),
                              "organization_identifier": n.get("organization_identifier")}.items() if v}


def _issuer(cert) -> dict:
    if cert is None:
        return {}
    n = cert.issuer.native
    return {k: v for k, v in {"common_name": n.get("common_name"), "organization": n.get("organization_name"),
                              "country": n.get("country_name")}.items() if v}


def blank(field: str) -> dict:
    return {"field": field, "verdict": None, "reasons": [], "indication": None, "sub_indication": None, "signer": {},
            "issuer": {}, "claimed_signing_time": None, "proven_signing_time": None, "intact": None,
            "crypto_valid": None, "trusted": None, "revoked": None, "qualified": None, "qualified_type": None,
            "qscd": None, "trust_service": None, "coverage": None, "changed_after_signing": None,
            "change_detail": None, "timestamped": None}


class TrustManager(TSPTrustManager):
    """EU trusted lists, plus qualified timestamping units (QTST services list the TSA's own certificate)."""

    def find_potential_issuers(self, cert):
        yield from super().find_potential_issuers(cert)
        for a in self.tsp_registry.known_timestamp_authorities:
            if a.is_potential_issuer_of(cert):
                anchor = self.as_trust_anchor(a)
                if anchor is not None:
                    yield anchor


class TsaTrustManager(TSPTrustManager):
    """For timestamp tokens only: every service on an EU trusted list (QTST units/CAs and CAs) is an anchor without a
    service-type restriction (pyHanko tags QTST anchors time-stamping-only; the TSA unit's timeStamping key usage is
    still enforced by pyHanko).

    A listed QTST CA that is not self-signed often signs the CRL for its own units, and certvalidator can only
    validate that CRL when the CA sits *inside* the path. So when path building reaches a listed certificate, its
    own issuer (from the certificates in the token/signature) is offered as an anchor too. That issuer is an anchor
    only on paths that run through the listed certificate; it gains no trust anywhere else."""

    def __init__(self, tsp_registry, certs=()):
        super().__init__(tsp_registry)
        self._certs = list(certs)
        self._parent = {}  # sha256 of a listed, non-self-signed cert -> its self-signed issuer from the token
        for c in self._certs:
            if self._listed(AuthorityWithCert(c)) and c.self_signed == "no":
                for p in self._certs:
                    if p.self_signed in ("maybe", "yes") and AuthorityWithCert(p).is_potential_issuer_of(c):
                        self._parent[c.sha256] = p
                        break

    def _listed(self, authority):
        return next(iter(self.tsp_registry.applicable_service_definitions(authority, moment=None)), None)

    def as_trust_anchor(self, authority):
        cert = getattr(authority, "certificate", None)
        if cert is not None and cert.sha256 in self._parent:
            return None  # lifted: it becomes an intermediate under its own issuer (see find_potential_issuers)
        sd = self._listed(authority)
        if sd is None:
            return None
        return TrustAnchor(authority, quals=TrustQualifiers(trusted_service_type=TrustedServiceType.UNSPECIFIED,
                                                             valid_from=sd.base_info.valid_from,
                                                             valid_until=sd.base_info.valid_until))

    def find_potential_issuers(self, cert):
        for a in set(self.tsp_registry.known_timestamp_authorities) | set(
                self.tsp_registry.known_certificate_authorities):
            if a.is_potential_issuer_of(cert):
                anchor = self.as_trust_anchor(a)
                if anchor is not None:
                    yield anchor
        parent = self._parent.get(cert.sha256)
        if parent is not None:  # only paths through the listed certificate reach this anchor
            yield TrustAnchor(AuthorityWithCert(parent),
                              quals=TrustQualifiers(trusted_service_type=TrustedServiceType.UNSPECIFIED))


def _embedded(sig_el):
    """Certificates and revocation data carried in the signature (KeyInfo + XAdES CertificateValues/RevocationValues)."""
    certs, ocsps, crls = [], [], []
    for e in sig_el.iterfind(".//ds:KeyInfo//ds:X509Certificate", NS):
        try:
            certs.append(a_x509.Certificate.load(_b64(e.text)))
        except (ValueError, binascii.Error):
            pass
    for e in _q(sig_el, ".//x:EncapsulatedX509Certificate"):
        try:
            certs.append(a_x509.Certificate.load(_b64(e.text)))
        except (ValueError, binascii.Error):
            pass
    for e in _q(sig_el, ".//x:EncapsulatedOCSPValue"):
        try:
            ocsps.append(a_ocsp.OCSPResponse.load(_b64(e.text)))
        except (ValueError, binascii.Error):
            pass
    for e in _q(sig_el, ".//x:EncapsulatedCRLValue"):
        try:
            crls.append(a_crl.CertificateList.load(_b64(e.text)))
        except (ValueError, binascii.Error):
            pass
    return certs, ocsps, crls


def _is_ca(cert) -> bool:
    bc = cert.basic_constraints_value
    return bool(bc and bc["ca"].native)


def _signing_cert_candidates(sig_el, certs):
    """Leaf candidates: KeyInfo certificates that are not CAs, KeyInfo order first."""
    keyinfo = []
    for e in sig_el.iterfind("ds:KeyInfo//ds:X509Certificate", NS):
        try:
            keyinfo.append(a_x509.Certificate.load(_b64(e.text)))
        except (ValueError, binascii.Error):
            pass
    seen, out = set(), []
    for c in keyinfo + certs:
        if c.sha256 in seen or _is_ca(c):
            continue
        seen.add(c.sha256)
        out.append(c)
    return out[:MAX_KEY_CANDIDATES]  # each candidate costs a full verification


def _pem(cert) -> bytes:
    b = base64.encodebytes(cert.dump()).decode()
    return ("-----BEGIN CERTIFICATE-----\n" + b + "-----END CERTIFICATE-----\n").encode()


def _crypto_verify(xml: bytes, sig_el, leaf, resolver, *, allow_sha1: bool):
    is_xades = bool(_q(sig_el, "ds:Object/x:QualifyingProperties"))
    n_refs = len(sig_el.findall("ds:SignedInfo/ds:Reference", NS))
    cfg = SignatureConfiguration(
        expect_references=n_refs,
        verification_time=leaf.not_valid_before + dt.timedelta(seconds=1),  # crypto only; time/trust checked later
        signature_methods=frozenset(SignatureMethod) if allow_sha1 else SignatureConfiguration().signature_methods,
        digest_algorithms=frozenset(DigestAlgorithm) if allow_sha1 else SignatureConfiguration().digest_algorithms)
    verifier = XAdESVerifier() if is_xades else XMLVerifier()
    res = verifier.verify(xml, x509_cert=_pem(leaf), expect_config=cfg, uri_resolver=resolver)
    return (res if isinstance(res, list) else [res]), is_xades  # signxml returns a bare result for one reference


SUPPORTED_TRANSFORMS = set(C14N) | {"http://www.w3.org/2000/09/xmldsig#enveloped-signature",
                                    "http://www.w3.org/2000/09/xmldsig#base64"}


def _unsupported_transforms(sig_el) -> list[str]:
    return sorted({t.get("Algorithm") or "(none)" for t in sig_el.iterfind("ds:SignedInfo/ds:Reference/ds:Transforms/"
                                                                           "ds:Transform", NS)
                   if t.get("Algorithm") not in SUPPORTED_TRANSFORMS})


def _uses_sha1(sig_el) -> bool:
    si = sig_el.find("ds:SignedInfo", NS)
    if si is None:
        return False
    algs = [e.get("Algorithm") or "" for e in si.iterfind("ds:SignatureMethod", NS)]
    algs += [e.get("Algorithm") or "" for e in si.iterfind("ds:Reference/ds:DigestMethod", NS)]
    return any("sha1" in a.lower() for a in algs)


def _crypto_failure(exc) -> dict:
    """Map a signxml failure to verdict fields."""
    msg = str(exc or "")
    if isinstance(exc, sx_errors.InvalidDigest) and "certificate digest" in msg:
        return dict(verdict="BLOCK", indication="FAILURE", sub_indication="SIG_CONSTRAINTS_FAILURE",
                    reasons=["the signer certificate in the signature is not the one the signer committed to "
                             "(XAdES signing-certificate mismatch)"])
    if isinstance(exc, sx_errors.InvalidDigest):
        m = re.search(r"\((.*)\)\s*$", msg)
        uri = m.group(1) if m else ""
        external = bool(uri) and not uri.startswith("#")
        return dict(verdict="BLOCK", indication="FAILURE", sub_indication="HASH_FAILURE", intact=False,
                    reasons=[f"the file you gave does not match the file this signature covers ({uri[:120]}): "
                             "changed, or a different file" if external else
                             "the signed content was changed after signing (digest mismatch)"])
    if isinstance(exc, sx_errors.InvalidSignature):
        return dict(verdict="BLOCK", indication="FAILURE", sub_indication="SIG_CRYPTO_FAILURE", intact=True,
                    crypto_valid=False, reasons=["the signature is cryptographically invalid"])
    if isinstance(exc, sx_errors.InvalidInput) and "Ambiguous reference" in msg:
        return dict(verdict="BLOCK", indication="FAILURE", sub_indication="FORMAT_FAILURE",
                    reasons=["the signature points at an Id that occurs more than once in the document "
                             "(possible signature-wrapping attack)"])
    m = re.search(r"(?:Unable to resolve reference URI|External URI dereferencing is not configured): (.*)$", msg)
    if isinstance(exc, sx_errors.InvalidInput) and m and not m.group(1).startswith("#"):
        return dict(verdict="REVIEW", indication="INDETERMINATE", sub_indication="SIGNED_DATA_NOT_FOUND",
                    reasons=[f"this signature covers a separate file that you did not provide ({m.group(1)[:120]}); "
                             "give it as the document to check it"])
    return dict(verdict="REVIEW", indication="INDETERMINATE", sub_indication="FORMAT_FAILURE",
                reasons=[f"the signature could not be validated: unsupported or malformed "
                         f"({type(exc).__name__}: {msg[:160]})"])


def _c14n(el, alg: str | None) -> bytes:
    exclusive, comments = C14N.get(alg or "http://www.w3.org/TR/2001/REC-xml-c14n-20010315", (False, False))
    return etree.tostring(el, method="c14n", exclusive=exclusive, with_comments=comments)


def _digest(name: str, data: bytes) -> bytes:
    return hashlib.new(name, data).digest()


def _match_document(doc: bytes, refs: list[tuple[str, str, bytes, bool]]):
    """refs: (uri, digest name, digest value, has_transforms). Return the matching uri or None. The document is
    hashed once per algorithm, so a manifest with thousands of entries can't multiply the work."""
    digests = {}
    for uri, alg, value, has_tf in refs:
        if has_tf:
            continue
        if alg not in digests:
            digests[alg] = _digest(alg, doc)
        if digests[alg] == value:
            return uri
    return None


async def _timestamp(sig_el, vc_kwargs):
    """Validate the first XAdES SignatureTimeStamp. Returns (time or None, status dict, problem or None)."""
    sts = _q(sig_el, "ds:Object/x:QualifyingProperties/x:UnsignedProperties/x:UnsignedSignatureProperties/"
                     "x:SignatureTimeStamp")
    if not sts:
        return None, {"present": False}, None
    sv = sig_el.find("ds:SignatureValue", NS)
    problem, last = None, {"present": True}
    for ts in sts:
        try:
            enc = _q(ts, "x:EncapsulatedTimeStamp")[0]
            cm = ts.find("ds:CanonicalizationMethod", NS)
            imprint_src = _c14n(sv, cm.get("Algorithm") if cm is not None else None)
            tsd = a_cms.ContentInfo.load(_b64(enc.text))["content"]
            certs = [c.chosen for c in (tsd["certificates"] or []) if c.name == "certificate"]
            vc = ValidationContext(**{**vc_kwargs, "other_certs": vc_kwargs["other_certs"] + certs,
                                      "trust_manager": TsaTrustManager(vc_kwargs["trust_manager"].tsp_registry,
                                                                      vc_kwargs["other_certs"] + certs)})
            st = await asyncio.wait_for(validate_tst_signed_data(
                tsd, vc, lambda algo: hashlib.new(algo, imprint_src).digest()), SIG_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 - a broken token must give a verdict, not crash the run
            problem = f"the signature timestamp could not be processed ({type(exc).__name__})"
            continue
        tsa = st.get("signing_cert")
        info = {"present": True, "time": st["timestamp"].isoformat(timespec="seconds"), "tsa": _name(tsa),
                "intact": bool(st.get("intact")), "valid": bool(st.get("valid")),
                "trusted": st.get("trust_problem_indic") is None and st.get("validation_path") is not None}
        if info["intact"] and info["valid"] and info["trusted"]:
            return st["timestamp"], info, None
        problem = ("the signature timestamp does not match this signature" if not info["intact"] else
                   "the signature timestamp is cryptographically invalid" if not info["valid"] else
                   "the timestamp authority is not on an EU trusted list")
        last = info
    return None, last, problem


async def _chain(leaf, registry, certs, ocsps, crls, moment, backend):
    tm = TrustManager(registry)
    vc = ValidationContext(trust_manager=tm, other_certs=certs, ocsps=ocsps, crls=crls, moment=moment,
                           allow_fetching=True, fetcher_backend=backend,
                           revinfo_policy=signer_revinfo(moment is not None))
    return await asyncio.wait_for(CertificateValidator(leaf, validation_context=vc).async_validate_path(),
                                  SIG_TIMEOUT_S)


def _signature_elements(root):
    if etree.QName(root).namespace == DS and etree.QName(root).localname == "Signature":
        return [root]
    return root.findall(".//ds:Signature", NS)


async def check_signature(xml: bytes, root, sig_el, registry, *, documents: dict[str, bytes], require_qualified: bool,
                          field: str, backend=None) -> dict:
    out = {**blank(field), "signed_files": [], "document_match": None, "archive_timestamps": 0}
    certs, ocsps, crls = _embedded(sig_el)
    candidates = _signing_cert_candidates(sig_el, certs)
    if not candidates:
        out.update(verdict="REVIEW", indication="INDETERMINATE", sub_indication="NO_SIGNING_CERTIFICATE_FOUND",
                   reasons=["the signature carries no signer certificate, so the signer can't be identified or "
                            "checked against the EU trusted lists"])
        return out

    used: dict[str, str] = {}

    ext = {}  # external reference URI -> (digest name, digest value) from SignedInfo
    for r in sig_el.findall("ds:SignedInfo/ds:Reference", NS):
        u = r.get("URI") or ""
        dm = r.find("ds:DigestMethod", NS)
        if u and not u.startswith("#") and dm is not None and DIGESTS.get(dm.get("Algorithm")):
            try:
                ext[u] = (DIGESTS[dm.get("Algorithm")], _b64(r.findtext("ds:DigestValue", namespaces=NS)))
            except (ValueError, binascii.Error):
                pass

    def resolver(uri):  # direct detached references: hand over the caller's file
        data = documents.get(uri)
        if data is None and len(documents) == 1:
            only = next(iter(documents.values()))
            alg_val = ext.get(uri)
            # one external reference: it must be this file. Several (contract + annexes): give the file only to the
            # reference it matches; the others are "not provided" (REVIEW), not a false "changed" (BLOCK).
            if len(ext) <= 1 or (alg_val and _digest(alg_val[0], only) == alg_val[1]):
                data = only
        if data is None:
            return None
        used[uri] = hashlib.sha256(data).hexdigest()
        return data

    res, leaf, last_exc, is_xades = None, None, None, False
    for allow_sha1 in (False, True):
        for cand in candidates:
            try:
                res, is_xades = await asyncio.to_thread(_crypto_verify, xml, sig_el, cand, resolver,
                                                        allow_sha1=allow_sha1)
                leaf = cand
                break
            except sx_errors.InvalidSignature as exc:
                last_exc = exc
                continue  # maybe another KeyInfo certificate is the signer's
            except Exception as exc:  # noqa: BLE001 - digest/format errors don't depend on the key
                last_exc = exc
                break
        if res is not None or "forbidden by configuration" not in str(last_exc):
            break
    weak = _uses_sha1(sig_el)
    odd = _unsupported_transforms(sig_el)
    if odd:  # signxml skips unknown transforms, which would turn a valid filtered signature into a false BLOCK
        out.update(verdict="REVIEW", indication="INDETERMINATE", sub_indication="FORMAT_FAILURE",
                   reasons=["the signature uses a transform this validator does not support (" + ", ".join(odd[:3])
                            + "), so what exactly was signed can't be determined; it is not proven good or bad"])
        return out

    out.update(signer=_name(candidates[0]), issuer=_issuer(candidates[0]))
    if res is None:
        out.update(_crypto_failure(last_exc))
        return out

    out.update(intact=True, crypto_valid=True, signer=_name(leaf), issuer=_issuer(leaf))
    out["archive_timestamps"] = len(_q(sig_el, ".//x:ArchiveTimeStamp"))

    # --- what exactly is signed, and does the caller's file match it ---
    signed_refs, block, review = [], [], []
    root_signed = False
    for vr, ref in zip(res, sig_el.findall("ds:SignedInfo/ds:Reference", NS)):
        uri, rtype = ref.get("URI"), ref.get("Type")
        is_manifest = vr.signed_xml is not None and (rtype == MANIFEST_TYPE or (
            etree.QName(vr.signed_xml).namespace == DS and etree.QName(vr.signed_xml).localname == "Manifest"))
        if is_manifest:
            for mref in vr.signed_xml.findall("ds:Reference", NS)[:MAX_MANIFEST_REFS]:
                dm = mref.find("ds:DigestMethod", NS)
                alg = DIGESTS.get(dm.get("Algorithm") if dm is not None else "")
                try:
                    val = _b64(mref.findtext("ds:DigestValue", namespaces=NS))
                except (ValueError, binascii.Error):
                    continue
                if alg:
                    signed_refs.append((mref.get("URI") or "", alg, val, mref.find("ds:Transforms", NS) is not None))
        elif rtype and rtype.endswith("#SignedProperties"):
            continue
        elif uri == "":
            root_signed = True
        elif uri and not uri.startswith("#"):
            signed_refs.append((uri, "direct", b"", True))
    out["signed_files"] = [u for u, *_ in signed_refs][:50]

    doc_match = None
    if used:  # direct detached reference(s) verified by signxml against the caller's file
        doc_match = {"matched": True, "reference": next(iter(used)), "via": "reference"}
    elif any(a != "direct" for _, a, _, _ in signed_refs):
        if documents:
            for name, data in documents.items():
                hit = _match_document(data, [r for r in signed_refs if r[1] != "direct"])
                doc_match = {"matched": bool(hit), "reference": hit, "via": "manifest",
                             "sha256": hashlib.sha256(data).hexdigest()}
                if not hit:
                    block.append("the file you gave does not match any file this signature covers: it was changed, "
                                 "or it is a different file/language version")
                elif next(a for u, a, _, _ in signed_refs if u == hit) in WEAK:
                    weak = True
        else:
            review.append("the signature is valid, but you did not provide the signed file, so it was not compared; "
                          f"it covers {len(signed_refs)} file(s): {', '.join(out['signed_files'][:5])}")
    elif root_signed:
        doc_match = {"matched": True, "reference": "", "via": "enveloped (whole document)"}
    else:
        ids = [r.get("URI") for r in sig_el.findall("ds:SignedInfo/ds:Reference", NS)
               if (r.get("URI") or "").startswith("#") and not (r.get("Type") or "").endswith("#SignedProperties")]
        review.append("the signature covers only part of the XML document (" + ", ".join(ids[:5]) + "), not the "
                      "whole document; check that the part you rely on is the signed part")
    if documents and not (doc_match and doc_match.get("via") in ("reference", "manifest")):
        # A separate file was given, so the signature must cover THAT file (a direct reference or a signed manifest
        # entry matching its digest). A signature over something else (e.g. an unrelated signed XML) proves nothing
        # about it.
        doc_match = {"matched": False, "reference": None, "via": None,
                     "sha256": hashlib.sha256(next(iter(documents.values()))).hexdigest()}
        if not any("does not match" in b for b in block):
            block.append("this signature does not cover the file you gave (it signs other content); it proves "
                         "nothing about your file")
    out["document_match"] = doc_match
    out["coverage"] = ("ENTIRE_DOCUMENT" if (doc_match and doc_match.get("matched")) or (root_signed and not documents)
                       else "PARTIAL" if not signed_refs else None)

    # claimed signing time (from the verified SignedProperties, if XAdES)
    for vr in res:
        if vr.signed_xml is not None and etree.QName(vr.signed_xml).localname == "SignedProperties":
            st = vr.signed_xml.find(".//{%s}SigningTime" % etree.QName(vr.signed_xml).namespace)
            if st is not None and st.text:
                out["claimed_signing_time"] = st.text.strip()

    # --- timestamp, chain, revocation, qualification ---
    tm = TrustManager(registry)
    base_vc = {"trust_manager": tm, "other_certs": certs, "ocsps": ocsps, "crls": crls, "allow_fetching": True,
               "fetcher_backend": backend}
    ts_time, ts_info, ts_problem = await _timestamp(sig_el, base_vc)
    out["timestamped"] = ts_time is not None
    out["timestamp"] = ts_info
    if ts_problem and ts_time is None:
        review.append(ts_problem)

    path, moment_used, chain_problem, sub = None, None, None, None
    try:
        path = await _chain(leaf, registry, certs, ocsps, crls, None, backend)
    except (cv_errors.ExpiredError, cv_errors.InsufficientRevinfoError, cv_errors.StaleRevinfoError,
            cv_errors.NotYetValidError) as exc:
        if ts_time is not None:
            try:
                path = await _chain(leaf, registry, certs, ocsps, crls, ts_time, backend)
                moment_used = ts_time
            except Exception as exc2:  # noqa: BLE001
                chain_problem = exc2
        else:
            chain_problem = exc
    except Exception as exc:  # noqa: BLE001
        chain_problem = exc

    revoked_now = False
    if isinstance(chain_problem, cv_errors.RevokedError) and ts_time is not None:
        try:  # revoked today, but was it valid when the trusted timestamp was made?
            path = await _chain(leaf, registry, certs, ocsps, crls, ts_time, backend)
            moment_used, chain_problem, revoked_now = ts_time, None, True
        except Exception as exc2:  # noqa: BLE001
            chain_problem = exc2

    if path is not None:
        out.update(trusted=True, revoked=False)
        out["proven_signing_time"] = ts_time.isoformat(timespec="seconds") if ts_time else None
        ku = leaf.key_usage_value
        if ku is not None and not ({"digital_signature", "non_repudiation"} & set(ku.native)):
            review.append("the signer's certificate is not meant for signing (no digital-signature or "
                          "non-repudiation key usage)")
        try:
            q = QualificationAssessor(registry).check_entity_cert_qualified(path, moment=moment_used or ts_time)
            out["qualified"] = bool(q.status.qualified)
            qt = getattr(q.status.qc_type, "name", None)
            out["qualified_type"] = {"QC_ESIGN": "qualified_signature", "QC_ESEAL": "qualified_seal",
                                     "QC_WEB": "qualified_website"}.get(qt, qt)
            out["qscd"] = bool(q.status.qc_key_security.is_qscd)
            if q.service_definition is not None:
                out["trust_service"] = q.service_definition.base_info.service_name
        except Exception:  # noqa: BLE001 - qualification unknown, not a validity problem
            pass
        if revoked_now:
            review.append("the signer's certificate has been revoked since; the signature was made before the "
                          "revocation (proven by the timestamp), but check why it was revoked")
        sub = "OK" if moment_used is None and ts_time else "OK_AT_TIMESTAMP" if moment_used else "OK_NO_TIMESTAMP"
    else:
        e = chain_problem
        if isinstance(e, cv_errors.RevokedError):
            out["revoked"] = True
            block.append("the signer's certificate is revoked" + (" (and was already revoked at the timestamp)"
                                                                  if ts_time else ""))
            sub = "REVOKED_NO_POE" if not ts_time else "REVOKED"
        elif isinstance(e, cv_errors.PathBuildingError):
            out["trusted"] = False
            review.append("the signer's certificate does not chain to a trust service on an EU trusted list")
            sub = "NO_CERTIFICATE_CHAIN_FOUND"
        elif isinstance(e, (cv_errors.ExpiredError, cv_errors.NotYetValidError)):
            review.append("the certificate is expired (or not yet valid) and there is no trusted timestamp proving "
                          "the signature was made while it was valid")
            sub = "OUT_OF_BOUNDS_NO_POE"
        elif isinstance(e, (cv_errors.InsufficientRevinfoError, cv_errors.StaleRevinfoError)):
            review.append("the certificate's revocation status could not be established (no usable OCSP/CRL)")
            sub = "TRY_LATER"
        elif isinstance(e, asyncio.TimeoutError):
            review.append("validation timed out (revocation services slow); try again later")
            sub = "TRY_LATER"
        else:
            review.append(f"the signer's certificate chain could not be validated ({type(e).__name__})")
            sub = "CERTIFICATE_CHAIN_GENERAL_FAILURE"

    if weak:
        review.append("the signature or the signed-file digest uses SHA-1, which is no longer considered secure")
    if require_qualified and not out["qualified"]:
        review.append("not a qualified signature or seal (you asked for qualified)")
    elif require_qualified and not out["qscd"]:
        review.append("qualified certificate, but not created on a qualified signature/seal creation device (QSCD), "
                      "so not a qualified signature or seal (you asked for qualified)")

    out["reasons"] = block + review
    out["verdict"] = "BLOCK" if block else "REVIEW" if review else "ALLOW"
    out["indication"] = "FAILURE" if block else "PASSED" if path is not None and not review else (
        "PASSED" if path is not None else "INDETERMINATE")
    out["sub_indication"] = "HASH_FAILURE" if doc_match and doc_match.get("matched") is False else sub
    if out["verdict"] == "ALLOW":
        kind = (out["qualified_type"] if out["qualified"] and out["qscd"]
                else "advanced signature with a qualified certificate (no QSCD, so not qualified)" if out["qualified"]
                else "advanced (not qualified) signature")
        what = (f"; matches signed file {doc_match['reference']}" if doc_match and doc_match.get("reference") else "")
        out["reasons"] = [f"valid {kind.replace('_', ' ')}, signer chains to an EU trusted provider"
                          + (f" ({out['trust_service']})" if out["trust_service"] else "") + what
                          + ("; certificate has expired since, validity proven at the trusted timestamp "
                             f"({out['proven_signing_time']})" if moment_used else "")
                          + ("; no signature timestamp, so validity is proven as of this check, not as of signing"
                             if not ts_time else "")]
    return out


async def check_xml_signature(sig_xml: bytes, registry, *, documents: dict[str, bytes] | None = None,
                              require_qualified: bool = False) -> dict:
    """sig_xml: a detached signature file, or a signed XML document. documents: {name: bytes} the signature covers
    (detached case). Returns the same document-level shape as validate.check_pdf."""
    documents = documents or {}
    doc = {"sha256": hashlib.sha256(sig_xml).hexdigest(), "bytes": len(sig_xml), "signature_count": 0,
           "document_timestamps": 0, "format": "xml"}
    try:
        root = parse(sig_xml)
    except NotXML as exc:
        return {"verdict": "BLOCK", "status": "unreadable", "document": doc, "signatures": [],
                "reasons": [f"not a readable signature file: {exc}"]}
    sigs = _signature_elements(root)
    doc["signature_count"] = len(sigs)
    if not sigs:
        return {"verdict": "BLOCK", "status": "unsigned", "document": doc, "signatures": [],
                "reasons": ["the XML file contains no XML signature (ds:Signature)"]}
    results = []
    first = sigs[0]
    try:
        async with netguard.fetcher_backend() as backend:  # OCSP/CRL/issuer URLs come from the file: public only
            results.append(await check_signature(sig_xml, root, first, registry, documents=documents,
                                                 require_qualified=require_qualified,
                                                 field=first.get("Id") or "Signature1", backend=backend))
    except Exception as exc:  # noqa: BLE001 - never crash the run on a hostile file
        results.append({**blank(first.get("Id") or "Signature1"), "verdict": "REVIEW",
                        "reasons": [f"the validation engine could not process this signature ({type(exc).__name__}); "
                                    "it is not proven good or bad"]})
    for s in sigs[1:MAX_SIGNATURES + 1]:
        results.append({**blank(s.get("Id") or "Signature"), "verdict": "REVIEW", "sub_indication": "NOT_VALIDATED",
                        "reasons": ["not validated: only the first XML signature in a file is validated"]})
    rank = {"ALLOW": 0, "REVIEW": 1, "BLOCK": 2}
    verdict = max((r["verdict"] for r in results), key=rank.__getitem__)
    reasons = [f"{r['field']}: {x}" for r in results for x in r["reasons"] if r["verdict"] == verdict or
               verdict == "ALLOW"]
    return {"verdict": verdict, "status": "checked", "document": doc, "signatures": results, "reasons": reasons}
