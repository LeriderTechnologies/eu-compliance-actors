"""XML signatures (v0.2): enveloped XAdES, detached via ds:Manifest, attacks, Actor routing, outbound-fetch guard.
A local test PKI (CA registered as an EU qualified CA service in a test TSP registry) gives the ALLOW path.
Live checks against real EU Official Journal seals run only with EIDAS_LIVE=1.
Run from the folder above:  python3 -m unittest discover tests"""
import asyncio
import base64
import datetime as dt
import hashlib
import http.server
import os
import threading
import unittest

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from lxml import etree
from pyhanko.sign.validation.qualified.tsp import TSPRegistry
from signxml import methods
from signxml.xades import XAdESSigner

from eidas_check import actor, netguard, xades
from test_eidas import registry_with

DS = "http://www.w3.org/2000/09/xmldsig#"
NOW = dt.datetime.now(dt.timezone.utc)
DOC = b"%PDF-1.4 a contract nobody can read without a PDF viewer"


def _name(cn):
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn), x509.NameAttribute(NameOID.COUNTRY_NAME, "NL"),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Lerider Test")])


def make_pki(crl_url=None, key_usage=(True, True)):
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca = (x509.CertificateBuilder().subject_name(_name("Test Qualified CA")).issuer_name(_name("Test Qualified CA"))
          .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(NOW - dt.timedelta(days=30)).not_valid_after(NOW + dt.timedelta(days=3650))
          .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
          .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
          .sign(ca_key, hashes.SHA256()))
    key = ec.generate_private_key(ec.SECP256R1())
    b = (x509.CertificateBuilder().subject_name(_name("Alice Signer")).issuer_name(ca.subject)
         .public_key(key.public_key()).serial_number(x509.random_serial_number())
         .not_valid_before(NOW - dt.timedelta(days=1)).not_valid_after(NOW + dt.timedelta(days=365))
         .add_extension(x509.KeyUsage(key_usage[0], key_usage[1], False, False, False, False, False, False, False),
                        critical=True)
         .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False))
    if crl_url:
        b = b.add_extension(x509.CRLDistributionPoints([x509.DistributionPoint(
            [x509.UniformResourceIdentifier(crl_url)], None, None, None)]), critical=False)
    cert = b.sign(ca_key, hashes.SHA256())
    from asn1crypto import x509 as a_x509
    a_ca = a_x509.Certificate.load(ca.public_bytes(serialization.Encoding.DER))
    return a_ca, key, cert


def sign_xml(root, key, cert, reference_uri=None):
    signer = XAdESSigner(method=methods.enveloped, signature_algorithm="ecdsa-sha256", digest_algorithm="sha256")
    signed = signer.sign(root, key=key, cert=[cert], reference_uri=reference_uri)
    return etree.tostring(signed)


def invoice_xml():
    return etree.fromstring(b"<Invoice><ID>INV-1</ID><Amount currency='EUR'>100.00</Amount></Invoice>")


def manifest_xml(files: dict[str, bytes]):
    root = etree.Element("Bundle")
    man = etree.SubElement(root, f"{{{DS}}}Manifest", Id="manifest", nsmap={"ds": DS})
    for name, data in files.items():
        ref = etree.SubElement(man, f"{{{DS}}}Reference", URI=name)
        etree.SubElement(ref, f"{{{DS}}}DigestMethod", Algorithm="http://www.w3.org/2001/04/xmlenc#sha256")
        etree.SubElement(ref, f"{{{DS}}}DigestValue").text = base64.b64encode(hashlib.sha256(data).digest()).decode()
    return root


def direct_detached(files: dict[str, bytes], key, cert) -> bytes:
    """Plain XML-DSig with direct external references (URI="contract.pdf"), built by hand: signxml only signs XML."""
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    sig = etree.Element(f"{{{DS}}}Signature", nsmap={"ds": DS})
    si = etree.SubElement(sig, f"{{{DS}}}SignedInfo")
    etree.SubElement(si, f"{{{DS}}}CanonicalizationMethod", Algorithm="http://www.w3.org/2001/10/xml-exc-c14n#")
    etree.SubElement(si, f"{{{DS}}}SignatureMethod", Algorithm="http://www.w3.org/2001/04/xmldsig-more#ecdsa-sha256")
    for name, data in files.items():
        ref = etree.SubElement(si, f"{{{DS}}}Reference", URI=name)
        etree.SubElement(ref, f"{{{DS}}}DigestMethod", Algorithm="http://www.w3.org/2001/04/xmlenc#sha256")
        etree.SubElement(ref, f"{{{DS}}}DigestValue").text = base64.b64encode(hashlib.sha256(data).digest()).decode()
    r, s_ = decode_dss_signature(key.sign(etree.tostring(si, method="c14n", exclusive=True), ec.ECDSA(hashes.SHA256())))
    etree.SubElement(sig, f"{{{DS}}}SignatureValue").text = base64.b64encode(
        r.to_bytes(32, "big") + s_.to_bytes(32, "big")).decode()
    ki = etree.SubElement(etree.SubElement(sig, f"{{{DS}}}KeyInfo"), f"{{{DS}}}X509Data")
    etree.SubElement(ki, f"{{{DS}}}X509Certificate").text = base64.b64encode(
        cert.public_bytes(serialization.Encoding.DER)).decode()
    return etree.tostring(sig)


def run(coro):
    return asyncio.run(coro)


class TestEnveloped(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ca, cls.key, cls.cert = make_pki()
        cls.reg = registry_with(cls.ca)
        cls.signed = sign_xml(invoice_xml(), cls.key, cls.cert)

    def check(self, data, reg=None, **kw):
        return run(xades.check_xml_signature(data, reg or self.reg, **kw))

    def test_valid_enveloped_signature_is_allow(self):
        r = self.check(self.signed)
        self.assertEqual(r["verdict"], "ALLOW", r["reasons"])
        s = r["signatures"][0]
        self.assertEqual(s["sub_indication"], "OK_NO_TIMESTAMP")
        self.assertEqual(s["coverage"], "ENTIRE_DOCUMENT")
        self.assertEqual(s["signer"]["common_name"], "Alice Signer")
        self.assertFalse(s["qualified"])

    def test_require_qualified_is_review(self):
        r = self.check(self.signed, require_qualified=True)
        self.assertEqual(r["verdict"], "REVIEW")

    def test_untrusted_signer_is_review(self):
        r = self.check(self.signed, reg=TSPRegistry())
        self.assertEqual(r["verdict"], "REVIEW")
        self.assertEqual(r["signatures"][0]["sub_indication"], "NO_CERTIFICATE_CHAIN_FOUND")

    def test_changed_content_is_block(self):
        r = self.check(self.signed.replace(b"100.00", b"900.00"))
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertEqual(r["signatures"][0]["sub_indication"], "HASH_FAILURE")

    def test_changed_signature_value_is_block(self):
        root = etree.fromstring(self.signed)
        sv = root.find(f".//{{{DS}}}SignatureValue")
        raw = bytearray(base64.b64decode(sv.text))
        raw[10] ^= 0xFF
        sv.text = base64.b64encode(bytes(raw)).decode()
        r = self.check(etree.tostring(root))
        self.assertEqual(r["verdict"], "BLOCK")

    def test_swapped_certificate_is_not_allow(self):
        _, _, other = make_pki()
        root = etree.fromstring(self.signed)
        el = root.find(f".//{{{DS}}}X509Certificate")
        el.text = base64.b64encode(other.public_bytes(serialization.Encoding.DER)).decode()
        r = self.check(etree.tostring(root))
        self.assertEqual(r["verdict"], "BLOCK", r["reasons"])

    def test_no_signature_and_garbage(self):
        self.assertEqual(self.check(b"<Invoice/>")["status"], "unsigned")
        self.assertEqual(self.check(b"not xml at all <<<")["status"], "unreadable")

    def test_entity_expansion_is_refused(self):
        bomb = (b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;">]>'
                b"<lolz>&lol2;</lolz>")
        r = self.check(bomb)
        self.assertEqual(r["status"], "unreadable")
        self.assertIn("DOCTYPE", r["reasons"][0])

    def test_doctype_hidden_behind_long_comment_is_refused(self):
        bomb = (b'<?xml version="1.0"?><!--' + b"x" * 70000 + b'--><!DOCTYPE z [<!ENTITY a "aaaa">]><z>&a;</z>')
        self.assertEqual(self.check(bomb)["status"], "unreadable")

    def test_signature_wrapping_with_duplicate_id_is_block(self):
        _, key, cert = self.ca, self.key, self.cert
        root = invoice_xml()
        root.find("Amount").set("Id", "amt")
        signed = etree.fromstring(sign_xml(root, key, cert, reference_uri="#amt"))
        evil = etree.SubElement(signed, "Amount", Id="amt")  # attacker adds a second element with the same Id
        evil.text = "1.00"
        r = self.check(etree.tostring(signed))
        self.assertEqual(r["verdict"], "BLOCK", r["reasons"])

    def test_partial_signature_is_not_allow(self):
        root = invoice_xml()
        root.find("Amount").set("Id", "amt")
        signed = sign_xml(root, self.key, self.cert, reference_uri="#amt")
        r = self.check(signed, documents={})
        self.assertNotEqual(r["verdict"], "ALLOW")

    def test_certificate_without_signing_usage_is_review(self):
        ca, key, cert = make_pki(key_usage=(False, False))
        r = run(xades.check_xml_signature(sign_xml(invoice_xml(), key, cert), registry_with(ca)))
        self.assertNotEqual(r["verdict"], "ALLOW")


class TestDetachedManifest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ca, cls.key, cls.cert = make_pki()
        cls.reg = registry_with(cls.ca)
        cls.sig = sign_xml(manifest_xml({"contract_EN.pdf": DOC, "contract_NL.pdf": DOC + b" NL"}), cls.key,
                           cls.cert, reference_uri="#manifest")

    def test_matching_document_is_allow(self):
        r = run(xades.check_xml_signature(self.sig, self.reg, documents={"mine.pdf": DOC}))
        self.assertEqual(r["verdict"], "ALLOW", r["reasons"])
        self.assertEqual(r["signatures"][0]["document_match"]["reference"], "contract_EN.pdf")
        self.assertIn("contract_NL.pdf", r["signatures"][0]["signed_files"])

    def test_changed_document_is_block(self):
        r = run(xades.check_xml_signature(self.sig, self.reg, documents={"mine.pdf": DOC + b"!"}))
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertEqual(r["signatures"][0]["sub_indication"], "HASH_FAILURE")

    def test_no_document_is_review(self):
        r = run(xades.check_xml_signature(self.sig, self.reg, documents={}))
        self.assertEqual(r["verdict"], "REVIEW")

    def test_huge_manifest_hashes_the_document_once(self):
        import time
        files = {f"f{i}.pdf": str(i).encode() for i in range(3000)}
        sig = sign_xml(manifest_xml(files), self.key, self.cert, reference_uri="#manifest")
        big = b"%PDF" + b"0" * (8 * 1024 * 1024)
        t = time.monotonic()
        r = run(xades.check_xml_signature(sig, self.reg, documents={"big.pdf": big}))
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertLess(time.monotonic() - t, 20)

    def test_edited_manifest_digest_is_block(self):
        evil_digest = base64.b64encode(hashlib.sha256(b"evil").digest()).decode()
        root = etree.fromstring(self.sig)
        root.find(f".//{{{DS}}}Manifest/{{{DS}}}Reference/{{{DS}}}DigestValue").text = evil_digest
        r = run(xades.check_xml_signature(etree.tostring(root), self.reg, documents={"x": b"evil"}))
        self.assertEqual(r["verdict"], "BLOCK")


class TestActorRouting(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ca, cls.key, cls.cert = make_pki()
        cls.reg = registry_with(cls.ca)
        cls.sig = sign_xml(manifest_xml({"c.pdf": DOC}), cls.key, cls.cert, reference_uri="#manifest")
        cls.signed_xml = sign_xml(invoice_xml(), cls.key, cls.cert)

    def items(self, inp):
        return run(actor.run(inp, registry=self.reg, tl_meta={}))

    def test_detached_pair_and_signed_xml_and_charging(self):
        b = lambda d: base64.b64encode(d).decode()  # noqa: E731
        out = self.items({"documents": [
            {"pdf_base64": b(DOC), "signature_base64": b(self.sig), "name": "c.pdf"},
            {"pdf_base64": b(DOC + b"x"), "signature_base64": b(self.sig)},
            {"file_base64": b(self.signed_xml), "name": "invoice.xml"},
            {"signature_base64": b(self.sig)},
            {"pdf_base64": b(b"just bytes")},
            {"eu_act": "not-an-act"},
        ]})
        self.assertEqual([o.get("verdict") for o in out[:5]], ["ALLOW", "BLOCK", "ALLOW", "REVIEW", "BLOCK"])
        self.assertEqual(out[0]["document"]["kind"], "detached_xml_signature")
        self.assertEqual(out[2]["document"]["kind"], "signed_xml")
        self.assertEqual(out[4]["status"], "unreadable")
        self.assertIn("error", out[5])
        self.assertEqual([actor.is_chargeable(o) for o in out], [True, True, True, True, False, False])

    def test_items_match_dataset_schema(self):
        import json
        import jsonschema
        from test_eidas import ROOT
        schema = json.loads((ROOT / ".actor" / "dataset_schema.json").read_text())["fields"]
        v = jsonschema.Draft7Validator(schema)
        b = lambda d: base64.b64encode(d).decode()  # noqa: E731
        for it in self.items({"documents": [{"pdf_base64": b(DOC), "signature_base64": b(self.sig)},
                                            {"file_base64": b(self.signed_xml)}, {"signature_base64": b(b"<x/>")},
                                            {"eu_act": "nope"}]}):
            self.assertEqual([f"{list(e.path)}: {e.message}" for e in v.iter_errors(it)], [], it)

    def test_eu_act_with_signature_is_refused(self):
        out = self.items({"eu_act": "32024R1689", "signature_base64": "PGEvPg=="})
        self.assertIn("error", out[0])


class _Counter(http.server.BaseHTTPRequestHandler):
    hits = 0

    def do_GET(self):  # noqa: N802
        type(self).hits += 1
        self.send_response(404)
        self.end_headers()

    def log_message(self, *a):
        pass


class TestOutboundGuard(unittest.TestCase):
    """A certificate pointing its CRL at an internal address must not make the Actor fetch it."""

    def setUp(self):
        _Counter.hits = 0
        self.srv = http.server.HTTPServer(("127.0.0.1", 0), _Counter)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/x.crl"

    def tearDown(self):
        self.srv.shutdown()

    def test_xml_signature_crl_on_localhost_is_not_fetched(self):
        ca, key, cert = make_pki(crl_url=self.url)
        r = run(xades.check_xml_signature(sign_xml(invoice_xml(), key, cert), registry_with(ca)))
        self.assertEqual(_Counter.hits, 0)
        self.assertEqual(r["verdict"], "REVIEW")

    def test_pdf_signature_crl_on_localhost_is_not_fetched(self):
        from test_eidas import blank_pdf
        from asn1crypto import keys as a_keys, x509 as a_x509
        from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
        from pyhanko.sign import signers
        from pyhanko_certvalidator.registry import SimpleCertificateStore
        import io
        from eidas_check import validate
        ca, key, cert = make_pki(crl_url=self.url)
        a_key = a_keys.PrivateKeyInfo.load(key.private_bytes(serialization.Encoding.DER,
                                                             serialization.PrivateFormat.PKCS8,
                                                             serialization.NoEncryption()))
        signer = signers.SimpleSigner(signing_cert=a_x509.Certificate.load(cert.public_bytes(
            serialization.Encoding.DER)), signing_key=a_key, cert_registry=SimpleCertificateStore.from_certs([ca]))
        out = io.BytesIO()
        signers.sign_pdf(IncrementalPdfFileWriter(io.BytesIO(blank_pdf())),
                         signers.PdfSignatureMetadata(field_name="Sig1"), signer=signer, output=out)
        r = run(validate.check_pdf(out.getvalue(), registry_with(ca)))
        self.assertEqual(_Counter.hits, 0)
        self.assertNotEqual(r["verdict"], "ALLOW")

    def test_guard_off_reaches_server(self):  # proves the test server would have been hit without the guard
        netguard.ENABLED = False
        try:
            ca, key, cert = make_pki(crl_url=self.url)
            run(xades.check_xml_signature(sign_xml(invoice_xml(), key, cert), registry_with(ca)))
        finally:
            netguard.ENABLED = True
        self.assertGreater(_Counter.hits, 0)

    def test_is_public(self):
        for h in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "192.168.1.1", "fd00::1", "0.0.0.0"):
            self.assertFalse(netguard.is_public(h), h)
        self.assertTrue(netguard.is_public("8.8.8.8"))


class TestRound2Attacks(unittest.TestCase):
    """Second break round (2026-10-29)."""

    @classmethod
    def setUpClass(cls):
        cls.ca, cls.key, cls.cert = make_pki()
        cls.reg = registry_with(cls.ca)

    def test_unrelated_signature_does_not_vouch_for_a_supplied_file(self):
        signed = sign_xml(invoice_xml(), self.key, self.cert)  # valid, but over an invoice XML
        r = run(xades.check_xml_signature(signed, self.reg, documents={"contract.pdf": DOC}))
        self.assertEqual(r["verdict"], "BLOCK", r["reasons"])
        self.assertFalse(r["signatures"][0]["document_match"]["matched"])
        self.assertEqual(r["signatures"][0]["sub_indication"], "HASH_FAILURE")

    def test_direct_detached_reference(self):
        sig = direct_detached({"contract.pdf": DOC}, self.key, self.cert)
        r = run(xades.check_xml_signature(sig, self.reg, documents={"x.pdf": DOC}))
        self.assertEqual(r["verdict"], "ALLOW", r["reasons"])
        self.assertEqual(r["signatures"][0]["document_match"]["via"], "reference")
        r = run(xades.check_xml_signature(sig, self.reg, documents={"x.pdf": DOC + b" changed"}))
        self.assertEqual(r["verdict"], "BLOCK")
        r = run(xades.check_xml_signature(sig, self.reg, documents={}))
        self.assertEqual(r["verdict"], "REVIEW")
        self.assertEqual(r["signatures"][0]["sub_indication"], "SIGNED_DATA_NOT_FOUND")

    def test_multi_file_signature_with_one_file_given_is_review(self):
        sig = direct_detached({"contract.pdf": DOC, "annex.pdf": b"annex"}, self.key, self.cert)
        r = run(xades.check_xml_signature(sig, self.reg, documents={"contract.pdf": DOC}))
        self.assertEqual(r["verdict"], "REVIEW", r["reasons"])
        r = run(xades.check_xml_signature(sig, self.reg, documents={"x.pdf": b"something else"}))
        self.assertNotEqual(r["verdict"], "ALLOW")

    def test_unsupported_transform_is_review_not_block(self):
        root = etree.fromstring(sign_xml(invoice_xml(), self.key, self.cert))
        tf = root.find(f".//{{{DS}}}Transforms")
        etree.SubElement(tf, f"{{{DS}}}Transform", Algorithm="http://www.w3.org/TR/1999/REC-xpath-19991116")
        r = run(xades.check_xml_signature(etree.tostring(root), self.reg))
        self.assertEqual(r["verdict"], "REVIEW")
        self.assertIn("transform", r["reasons"][0])

    def test_oversized_crl_is_not_read_into_memory(self):
        big = b"0" * (netguard.MAX_RESPONSE + 5 * 1024 * 1024)

        class Big(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Length", str(len(big)))
                self.end_headers()
                try:
                    self.wfile.write(big)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), Big)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        netguard.ENABLED = False
        try:
            ca, key, cert = make_pki(crl_url=f"http://127.0.0.1:{srv.server_address[1]}/big.crl")
            r = run(xades.check_xml_signature(sign_xml(invoice_xml(), key, cert), registry_with(ca)))
        finally:
            netguard.ENABLED = True
            srv.shutdown()
        self.assertEqual(r["verdict"], "REVIEW")

    def test_odd_inputs(self):
        b = base64.b64encode(sign_xml(invoice_xml(), self.key, self.cert)).decode()
        out = run(actor.run({"documents": [{"file_base64": "data:application/xml;base64," + b, "name": {"x": 1}},
                                           {"pdf_base64": "\ud800"}, {"url": 5}]},
                            registry=self.reg, tl_meta={}))
        self.assertEqual(out[0]["verdict"], "ALLOW")
        self.assertEqual(out[0]["source"], "{'x': 1}")
        self.assertIn("error", out[1])
        self.assertIn("error", out[2])


@unittest.skipUnless(os.environ.get("EIDAS_LIVE") == "1", "live EU Publications Office test (EIDAS_LIVE=1)")
class TestLiveOfficialJournal(unittest.TestCase):
    """Real EU Official Journal acts: authentic PDF + detached XAdES-LTA seal of the Publications Office."""

    def test_ai_act_is_allow_and_tampered_copy_is_block(self):
        out = run(actor.run({"documents": [{"eu_act": "32024R1689", "language": "EN"},
                                           {"eu_act": "http://data.europa.eu/eli/reg/2024/1689/oj", "language": "NL",
                                            "pdf_base64": base64.b64encode(DOC).decode()}],
                             "require_qualified": True}))
        self.assertEqual(out[0]["verdict"], "ALLOW", out[0].get("reasons") or out[0])
        s = out[0]["signatures"][0]
        self.assertEqual(s["qualified_type"], "qualified_seal")
        self.assertEqual(s["document_match"]["reference"], "L_202401689EN.pdf")
        self.assertEqual(out[0]["eu_act"]["celex"], "32024R1689")
        self.assertEqual(out[1]["verdict"], "BLOCK")
        self.assertTrue(out[1]["eu_act"]["checked_your_copy"])


if __name__ == "__main__":
    unittest.main()
