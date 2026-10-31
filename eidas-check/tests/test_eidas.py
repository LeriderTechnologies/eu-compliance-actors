"""Offline tests. A local test PKI (CA registered as an EU qualified CA service in a test TSP registry) gives the
positive ALLOW path; EC DSS attack fixtures (tests/fixtures, LGPL-2.1, see NOTICE) must BLOCK with an empty registry.
Run from the folder above:  python3 -m unittest discover tests"""
import asyncio
import base64
import os
import datetime as dt
import io
import json
import pathlib
import unittest

from asn1crypto import keys as a_keys, x509 as a_x509
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
from pyhanko.pdf_utils.writer import PdfFileWriter
from pyhanko.pdf_utils import generic
from pyhanko.sign import signers
from pyhanko_certvalidator.registry import SimpleCertificateStore
from pyhanko.sign.validation.qualified.tsp import (CA_QC_URI, BaseServiceInformation, CAServiceInformation,
                                                    TSPRegistry)

from eidas_check import actor, validate

try:
    import jsonschema
except ImportError:  # pragma: no cover
    jsonschema = None

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures"
NOW = dt.datetime.now(dt.timezone.utc)


def _name(cn):
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn), x509.NameAttribute(NameOID.COUNTRY_NAME, "NL"),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Lerider Test")])


def make_pki():
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca = (x509.CertificateBuilder().subject_name(_name("Test Qualified CA")).issuer_name(_name("Test Qualified CA"))
          .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(NOW - dt.timedelta(days=30)).not_valid_after(NOW + dt.timedelta(days=3650))
          .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
          .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
          .sign(ca_key, hashes.SHA256()))
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (x509.CertificateBuilder().subject_name(_name("Alice Signer")).issuer_name(ca.subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(NOW - dt.timedelta(days=1)).not_valid_after(NOW + dt.timedelta(days=365))
            .add_extension(x509.KeyUsage(True, True, False, False, False, False, False, False, False), critical=True)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    a = lambda c: a_x509.Certificate.load(c.public_bytes(serialization.Encoding.DER))  # noqa: E731
    a_key = a_keys.PrivateKeyInfo.load(key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                                         serialization.NoEncryption()))
    signer = signers.SimpleSigner(signing_cert=a(cert), signing_key=a_key,
                                  cert_registry=SimpleCertificateStore.from_certs([a(ca)]))
    return a(ca), signer


def registry_with(ca_cert) -> TSPRegistry:
    reg = TSPRegistry()
    reg.register_ca(CAServiceInformation(
        base_info=BaseServiceInformation(service_type=CA_QC_URI, service_name="Test Qualified CA service",
                                         valid_from=NOW - dt.timedelta(days=60), valid_until=None,
                                         provider_certs=(ca_cert,), additional_info_certificate_type=frozenset(),
                                         other_additional_info=frozenset()),
        qualifications=frozenset(), expired_certs_revocation_info=None))
    return reg


def blank_pdf() -> bytes:
    w = PdfFileWriter()
    page = generic.DictionaryObject({generic.NameObject("/Type"): generic.NameObject("/Page"),
                                     generic.NameObject("/MediaBox"): generic.ArrayObject(
                                         [generic.NumberObject(x) for x in (0, 0, 595, 842)])})
    w.insert_page(page)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def sign(pdf: bytes, signer) -> bytes:
    out = io.BytesIO()
    signers.sign_pdf(IncrementalPdfFileWriter(io.BytesIO(pdf)), signers.PdfSignatureMetadata(field_name="Sig1"),
                     signer=signer, output=out)
    return out.getvalue()


def add_page_after(pdf: bytes) -> bytes:
    w = IncrementalPdfFileWriter(io.BytesIO(pdf))
    page = generic.DictionaryObject({generic.NameObject("/Type"): generic.NameObject("/Page"),
                                     generic.NameObject("/MediaBox"): generic.ArrayObject(
                                         [generic.NumberObject(x) for x in (0, 0, 595, 842)])})
    w.insert_page(page)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def check(pdf, reg, **kw):
    return asyncio.run(validate.check_pdf(pdf, reg, **kw))


class TestValidate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ca, cls.signer = make_pki()
        cls.reg = registry_with(cls.ca)
        cls.signed = sign(blank_pdf(), cls.signer)

    def test_allow_when_signer_chains_to_trusted_service(self):
        r = check(self.signed, self.reg)
        self.assertEqual((r["verdict"], r["status"]), ("ALLOW", "checked"), r["reasons"])
        s = r["signatures"][0]
        self.assertEqual((s["intact"], s["crypto_valid"], s["trusted"], s["indication"]), (True, True, True, "PASSED"))
        self.assertEqual(s["signer"]["common_name"], "Alice Signer")
        self.assertFalse(s["qualified"])  # no QC statements in the test cert: advanced, not qualified
        self.assertIn("advanced", r["reasons"][0])

    def test_require_qualified_downgrades_to_review(self):
        r = check(self.signed, self.reg, require_qualified=True)
        self.assertEqual(r["verdict"], "REVIEW")
        self.assertTrue(any("asked for qualified" in x for x in r["reasons"]))

    def test_untrusted_signer_is_review(self):
        r = check(self.signed, TSPRegistry())
        self.assertEqual(r["verdict"], "REVIEW")
        self.assertEqual(r["signatures"][0]["sub_indication"], "NO_CERTIFICATE_CHAIN_FOUND")

    def test_page_added_after_signing_is_block(self):
        r = check(add_page_after(self.signed), self.reg)
        self.assertEqual(r["verdict"], "BLOCK", r["reasons"])
        self.assertTrue(any("pages" in x for x in r["reasons"]))

    def test_flipped_byte_inside_signed_range_is_block(self):
        b = bytearray(self.signed)
        i = b.index(b"/MediaBox")
        b[i + 1] = ord("N")  # corrupt a byte the signature covers
        r = check(bytes(b), self.reg)
        self.assertEqual(r["verdict"], "BLOCK", r["reasons"])

    def test_unsigned_and_unreadable(self):
        self.assertEqual(check(blank_pdf(), self.reg)["status"], "unsigned")
        r = check(b"not a pdf", self.reg)
        self.assertEqual((r["verdict"], r["status"]), ("BLOCK", "unreadable"))

    def test_parser_crashes_become_unreadable(self):
        # fuzz 2026-10-28: corrupt Flate data, misspelled filters and broken xref escaped as exceptions (failed run)
        b = bytearray(self.signed)
        i = b.index(b"xref")
        b[i:i + 4] = b"xreX"
        for bad in (bytes(b), self.signed.replace(b"/Type /Page", b"/Type /Page /Filter /FlateDecodX", 1), b"%PDF-1.7\n" + bytes(range(256)) * 10):
            r = check(bad, self.reg)
            self.assertIn(r["verdict"], ("BLOCK", "REVIEW"))
            self.assertNotEqual(r["verdict"], "ALLOW")

    def test_countersigned_document_is_allow(self):
        # a second party signing later fills only a signature field: ordinary countersigning, not a REVIEW
        from pyhanko.sign import signers as S
        out = io.BytesIO()
        S.sign_pdf(IncrementalPdfFileWriter(io.BytesIO(self.signed)), S.PdfSignatureMetadata(field_name="Party2"),
                   signer=self.signer, output=out)
        r = check(out.getvalue(), self.reg)
        self.assertEqual(r["verdict"], "ALLOW", r["reasons"])
        self.assertEqual(r["signatures"][0]["changed_after_signing"], "LATER_SIGNATURES")

    def test_signature_cap(self):
        from eidas_check import validate as V
        old, V.MAX_SIGNATURES = V.MAX_SIGNATURES, 0
        try:
            r = check(self.signed, self.reg)
        finally:
            V.MAX_SIGNATURES = old
        self.assertEqual((r["verdict"], r["signatures"][0]["sub_indication"]), ("REVIEW", "NOT_VALIDATED_LIMIT"))

    def test_certificate_without_signing_key_usage_is_not_allow(self):
        # pyHanko accepts any key usage by default; ours requires digital_signature or non_repudiation
        ca_key = ec.generate_private_key(ec.SECP256R1())
        ca = (x509.CertificateBuilder().subject_name(_name("Test Qualified CA")).issuer_name(_name("Test Qualified CA"))
              .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
              .not_valid_before(NOW - dt.timedelta(days=30)).not_valid_after(NOW + dt.timedelta(days=3650))
              .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
              .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
              .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
              .sign(ca_key, hashes.SHA256()))
        key = ec.generate_private_key(ec.SECP256R1())
        cert = (x509.CertificateBuilder().subject_name(_name("Mallory")).issuer_name(ca.subject).public_key(key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(NOW - dt.timedelta(days=1))
                .not_valid_after(NOW + dt.timedelta(days=365))
                .add_extension(x509.KeyUsage(False, False, True, False, False, False, False, False, False), critical=True)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
                .sign(ca_key, hashes.SHA256()))
        a = lambda c: a_x509.Certificate.load(c.public_bytes(serialization.Encoding.DER))  # noqa: E731
        pk = a_keys.PrivateKeyInfo.load(key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                                          serialization.NoEncryption()))
        s = signers.SimpleSigner(signing_cert=a(cert), signing_key=pk, cert_registry=SimpleCertificateStore.from_certs([a(ca)]))
        r = check(sign(blank_pdf(), s), registry_with(a(ca)))
        self.assertNotEqual(r["verdict"], "ALLOW", r["reasons"])

    def test_touches_pages_rule(self):
        self.assertTrue(validate.touches_pages("x AbsoluteContext(path=PathInRevision('.Root.Pages')) y"))
        self.assertTrue(validate.touches_pages("PathInRevision('.Root.Pages.Kids[0]')"))
        self.assertTrue(validate.touches_pages("Values for dict key /Resources differ"))
        self.assertFalse(validate.touches_pages("PathInRevision('.Root.Pages.Kids[0].Annots[1]')"))
        self.assertFalse(validate.touches_pages("VRI key /B013 was modified or deleted."))
        self.assertFalse(validate.touches_pages("Values for dict key /I differ"))


@unittest.skipUnless(FIX.exists(), "EC DSS fixtures not present")
class TestDssAttackFixtures(unittest.TestCase):
    """Real attack files from the EC DSS test suite: must never get ALLOW; tampering must BLOCK."""
    EXPECT = {"pdf-signed-added-page.pdf": "BLOCK", "modified_after_signature.pdf": "BLOCK",
              "pades-spoofing-replaced-reason.pdf": "BLOCK", "hello_signed_INCSAVE_signed_EDITED.pdf": "BLOCK",
              "pdf-signed-corrupted.pdf": "BLOCK", "pdf-spoofing-attack.pdf": "BLOCK",
              "self-issued-qesig.pdf": "REVIEW", "pdf-signed-original.pdf": "REVIEW"}

    def test_legacy_sha1_signature_is_review_not_unsigned(self):
        # pyHanko silently skips adbe.pkcs7.sha1 signatures; a signed gazette must never come out as "unsigned"
        r = check((FIX / "boe-legacy-sha1-signed.pdf").read_bytes(), TSPRegistry())
        self.assertEqual((r["verdict"], r["status"], r["document"]["signature_count"]), ("REVIEW", "checked", 1))
        s = r["signatures"][0]
        self.assertEqual(s["sub_indication"], "FORMAT_NOT_SUPPORTED")
        self.assertEqual(s["signer"]["country"], "ES")
        self.assertIn("adbe.pkcs7.sha1", s["reasons"][0])

    def test_digestinfo_without_null_params_is_crypto_valid(self):
        # RFC 8017 allows absent NULL params; strict pyca rejected this genuine Austrian seal as "invalid" (BLOCK)
        r = check((FIX / "at-bgbl-digestinfo-no-null.pdf").read_bytes(), TSPRegistry())
        s = r["signatures"][0]
        self.assertEqual((s["intact"], s["crypto_valid"]), (True, True), s["reasons"])
        self.assertEqual(r["verdict"], "REVIEW")  # CA a-sign-corporate-07 is not on an EU trusted list
        self.assertEqual(s["signer"]["common_name"], "Bundeskanzleramt")

    def test_tampered_signature_still_fails_with_compat(self):
        b = bytearray((FIX / "at-bgbl-digestinfo-no-null.pdf").read_bytes())
        i = b.index(b"/Contents <") + 11 + 2000  # inside the hex CMS blob, in the signature value region
        b[i] = ord("0") if b[i] != ord("0") else ord("1")
        r = check(bytes(b), TSPRegistry())
        self.assertNotEqual(r["verdict"], "ALLOW")
        self.assertFalse(r["signatures"] and r["signatures"][0]["crypto_valid"] is True and r["verdict"] == "ALLOW")

    def test_expected_verdicts(self):
        reg = TSPRegistry()
        for name, want in self.EXPECT.items():
            r = check((FIX / name).read_bytes(), reg)
            self.assertEqual(r["verdict"], want, f"{name}: {r['reasons']}")


@unittest.skipUnless(os.environ.get("EIDAS_LIVE") == "1", "set EIDAS_LIVE=1: downloads EU trusted lists + OCSP")
class TestLiveRealWorld(unittest.TestCase):
    """Real documents against the real EU trusted lists (network). The German seal certificate is valid until
    2028-02-25; after that this test must move to a newer gazette issue."""

    def test_german_gazette_qualified_seal_is_allow(self):
        from eidas_check import tl
        reg, _ = asyncio.run(tl.build())
        r = check((FIX / "de-bgbl-2026-qualified-seal.pdf").read_bytes(), reg)
        s = r["signatures"][0]
        self.assertEqual(r["verdict"], "ALLOW", r["reasons"])
        self.assertEqual((s["qualified"], s["qualified_type"]), (True, "qualified_seal"))
        self.assertIn("D-Trust", s["trust_service"])


class TestActor(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ca, signer = make_pki()
        cls.reg = registry_with(cls.ca)
        cls.signed = sign(blank_pdf(), signer)

    def run_(self, inp):
        return asyncio.run(actor.run(inp, registry=self.reg, tl_meta={"built_at": "x", "source": "test"}))

    def test_base64_batch_and_charging(self):
        b64 = base64.b64encode(self.signed).decode()
        items = self.run_({"documents": [{"pdf_base64": b64, "name": "a.pdf"},
                                         {"pdf_base64": base64.b64encode(blank_pdf()).decode()},
                                         {"pdf_base64": base64.b64encode(b"junk").decode()},
                                         {"url": "ftp://x/y.pdf"}, {}]})
        self.assertEqual([it.get("status") for it in items], ["checked", "unsigned", "unreadable", None, None])
        self.assertEqual([actor.is_chargeable(it) for it in items], [True, True, False, False, False])
        self.assertEqual(items[0]["verdict"], "ALLOW")
        self.assertEqual(items[0]["source"], "a.pdf")

    def test_private_and_local_urls_are_refused(self):
        for u in ("http://127.0.0.1/a.pdf", "http://localhost/a.pdf", "http://169.254.169.254/latest/meta-data/",
                  "http://10.1.2.3/a.pdf", "http://[::1]/a.pdf", "http://2130706433/a.pdf", "file:///etc/passwd"):
            items = self.run_({"url": u})
            self.assertIn("error", items[0], u)
            self.assertFalse(actor.is_chargeable(items[0]))
        import urllib.request
        with self.assertRaises(actor.InputError):  # a redirect hop to the metadata service is refused too
            actor._PublicRedirects().redirect_request(urllib.request.Request("https://example.com/a.pdf"), None, 302,
                                                      "Found", {}, "http://169.254.169.254/")

    def test_download_deadline_and_size_cap(self):
        import http.server, socketserver, threading, time as _t

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a): pass

            def do_GET(self):
                self.send_response(200)
                if self.path == "/drip":
                    self.send_header("Content-Length", "1000"); self.end_headers()
                    try:
                        for _ in range(1000):
                            self.wfile.write(b"x"); self.wfile.flush(); _t.sleep(0.2)
                    except OSError:
                        pass
                else:
                    self.end_headers()
                    try:
                        while True:
                            self.wfile.write(b"0" * 65536)
                    except OSError:
                        pass

        class TS(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True
        srv = TS(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        actor.GUARD_PEER = False
        try:
            t0 = _t.time()
            with self.assertRaises(actor.InputError):
                actor._download(f"http://127.0.0.1:{srv.server_address[1]}/drip", deadline_s=2)
            self.assertLess(_t.time() - t0, 10)
            with self.assertRaises(actor.InputError):
                actor._download(f"http://127.0.0.1:{srv.server_address[1]}/endless", deadline_s=30)
        finally:
            actor.GUARD_PEER = True
            srv.shutdown()

    def test_connect_time_peer_check(self):
        import socket
        srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1)
        s = socket.create_connection(srv.getsockname())
        with self.assertRaises(actor.InputError):  # what a DNS-rebinding host would end up connecting to
            actor._check_peer(s)
        srv.close()

    def test_console_prefill_url_with_batch_or_upload(self):
        self.assertEqual(actor.pick_documents({"url": actor.PREFILL_URL, "documents": [{"url": "https://e.com/a.pdf"}]}),
                         [{"url": "https://e.com/a.pdf"}])
        self.assertEqual(actor.pick_documents({"url": actor.PREFILL_URL, "pdf_base64": "JVBERi0="}), [{"pdf_base64": "JVBERi0="}])
        with self.assertRaises(actor.InputError):
            actor.pick_documents({"url": "https://e.com/x.pdf", "documents": [{"url": "https://e.com/a.pdf"}]})

    def test_bad_inputs(self):
        for bad in ({}, {"documents": "x"}, {"documents": [{}] * 21}, {"url": "https://a/b.pdf", "documents": [{}]},
                    {"url": "https://a/b.pdf", "require_qualified": "yes"}, []):
            with self.assertRaises(actor.InputError, msg=bad):
                self.run_(bad)

    @unittest.skipUnless(jsonschema, "jsonschema not installed")
    def test_items_match_dataset_schema(self):
        schema = json.loads((ROOT / ".actor" / "dataset_schema.json").read_text())["fields"]
        v = jsonschema.Draft7Validator(schema)
        items = self.run_({"documents": [{"pdf_base64": base64.b64encode(self.signed).decode()},
                                         {"pdf_base64": base64.b64encode(add_page_after(self.signed)).decode()},
                                         {"pdf_base64": base64.b64encode(blank_pdf()).decode()},
                                         {"pdf_base64": base64.b64encode(b"junk").decode()}, {}]})
        for it in items:
            self.assertEqual([f"{list(e.path)}: {e.message}" for e in v.iter_errors(it)], [], it)


if __name__ == "__main__":
    unittest.main()
