"""Offline tests against the official example invoices (tests/fixtures, see NOTICE.md there) and targeted breakages.
Needs the rule bundle: python3 -m einvoice.rules build   (once; downloads the pinned official artefacts)
Run from the folder above:  python3 -m unittest discover tests"""
import base64
import http.server
import json
import pathlib
import re
import threading
import unittest

from einvoice import actor, core

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures"
ENGINE = core.Engine()


def fx(name) -> bytes:
    return (FIX / name).read_bytes()


def check(data: bytes, profile="auto"):
    fn = core.validate_pdf if actor.is_pdf(data) else core.validate
    return ENGINE.run(fn, ENGINE, data, profile=profile).result()


def rules_of(res):
    return {f["rule"] for f in res["errors"]}


class TestOfficialExamples(unittest.TestCase):
    def test_valid_examples_are_allow(self):
        cases = {"peppol-base-example.xml": ["EN 16931", "Peppol BIS Billing 3.0"],
                 "peppol-creditnote.xml": ["EN 16931", "Peppol BIS Billing 3.0"],
                 "cen-cii-example1.xml": ["EN 16931"],
                 "xrechnung-ubl.xml": ["EN 16931", "XRechnung 3.0"],
                 "xrechnung-cii.xml": ["EN 16931", "XRechnung 3.0"]}
        for name, standards in cases.items():
            r = check(fx(name))
            self.assertEqual((r["verdict"], r["error_count"]), ("ALLOW", 0), (name, r["errors"][:3]))
            self.assertEqual(r["standards"], standards, name)
            self.assertTrue(r["valid"])

    def test_summary(self):
        s = check(fx("peppol-base-example.xml"))["summary"]
        self.assertEqual(s["currency"], "EUR")
        self.assertTrue(s["number"] and s["seller"]["name"] and s["totals"]["payable"])
        s = check(fx("xrechnung-cii.xml"))["summary"]
        self.assertTrue(s["number"] and s["buyer"]["reference"])
        self.assertEqual(check(fx("peppol-creditnote.xml"))["syntax"], "UBL 2.1 CreditNote")


class TestBreakages(unittest.TestCase):
    def test_wrong_total_is_block_br_co_16(self):
        x = re.sub(rb'(<cbc:PayableAmount currencyID="EUR">)([0-9.]+)',
                   lambda m: m.group(1) + b"9999.00", fx("peppol-base-example.xml"))
        r = check(x)
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertIn("BR-CO-16", rules_of(r))

    def test_peppol_rule(self):
        x = re.sub(rb"<cbc:BuyerReference>.*?</cbc:BuyerReference>", b"", fx("peppol-base-example.xml"))
        self.assertIn("PEPPOL-EN16931-R003", rules_of(check(x)))

    def test_xrechnung_rule_leitweg_id(self):
        x = re.sub(rb"<cbc:BuyerReference>.*?</cbc:BuyerReference>", b"", fx("xrechnung-ubl.xml"))
        r = check(x)
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertTrue(any(x.startswith("BR-DE") for x in rules_of(r)), rules_of(r))

    def test_schema_error_is_block(self):
        x = re.sub(rb"<cbc:IssueDate>.*?</cbc:IssueDate>", b"", fx("peppol-base-example.xml"))
        r = check(x)
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertEqual(r["errors"][0]["rule"], "XSD")

    def test_forced_profile_adds_rules(self):
        r = check(fx("cen-cii-example1.xml"), profile="xrechnung")
        self.assertIn("XRechnung 3.0", r["standards"])
        self.assertEqual(r["verdict"], "BLOCK")  # a plain EN 16931 invoice misses XRechnung's mandatory fields

    def test_outdated_xrechnung_id_is_checked_against_3_0(self):
        x = fx("xrechnung-ubl.xml").replace(b"urn:xeinkauf.de:kosit:xrechnung_3.0",
                                             b"urn:xoev-de:kosit:standard:xrechnung_2.0")
        r = check(x)
        self.assertIn("XRechnung 3.0", r["standards"])
        self.assertEqual(r["verdict"], "REVIEW")
        self.assertFalse(r["valid"])

    def test_latin1_encoded_invoice(self):
        x = fx("peppol-base-example.xml").decode("utf-8")
        x = x.replace('encoding="UTF-8"', 'encoding="ISO-8859-1"').replace("<cbc:Note>", "<cbc:Note>Größe ", 1)
        r = check(x.encode("latin-1"))
        self.assertEqual(r["status"], "checked")
        self.assertEqual(r["verdict"], "ALLOW", r["errors"][:2])


class TestNotInvoices(unittest.TestCase):
    def test_refusals(self):
        cases = [b"<html><body>hi</body></html>", b"not xml", b"",
                 b'<?xml version="1.0"?><!DOCTYPE z [<!ENTITY a "aaaa">]><Invoice>&a;</Invoice>',
                 b'<?xml version="1.0"?><!--' + b"x" * 70000 + b'--><!DOCTYPE z [<!ENTITY a "b">]><z>&a;</z>',
                 b'<StandardBusinessDocument xmlns="http://www.unece.org/cefact/namespaces/'
                 b'StandardBusinessDocumentHeader"/>',
                 b'<Invoice xmlns="urn:example:not-ubl"/>']
        for c in cases:
            r = check(c)
            self.assertEqual((r["status"], r["verdict"]), ("not_invoice", "BLOCK"), c[:60])

    def test_deep_nesting_is_refused(self):
        r = check(b"<Invoice>" + b"<a>" * 5000 + b"</a>" * 5000 + b"</Invoice>")
        self.assertEqual(r["status"], "not_invoice")


class TestFacturX(unittest.TestCase):
    def test_profiles(self):
        r = check(fx("E05_01_Einfach_fx.pdf"))
        self.assertEqual(r["verdict"], "ALLOW")
        self.assertEqual(r["container"]["embedded_file"], "factur-x.xml")
        r = check(fx("MINIMUM_Rechnung_fx.pdf"))
        self.assertEqual((r["verdict"], r["facturx"]["profile"]), ("REVIEW", "MINIMUM"))
        self.assertFalse(r["valid"])
        r = check(fx("X04_01_Sammelrechnung_3_Bestellungen_fx.pdf"))
        self.assertEqual((r["verdict"], r["facturx"]["profile"]), ("ALLOW", "EXTENDED"))
        self.assertTrue(any("allowed in Factur-X" in w["message"] for w in r["warnings"]))

    def test_extended_still_enforces_business_rules(self):
        from pypdf import PdfReader, PdfWriter
        import io
        reader = PdfReader(io.BytesIO(fx("X04_01_Sammelrechnung_3_Bestellungen_fx.pdf")))
        xml = reader.attachments["factur-x.xml"][0]
        xml = re.sub(rb"(<ram:DuePayableAmount>)([0-9.]+)", rb"\g<1>123456.78", xml, count=1)
        w = PdfWriter()
        w.append(reader)
        w.add_attachment("factur-x.xml", xml)
        out = io.BytesIO()
        w.write(out)
        r = check(out.getvalue())
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertTrue(any(x.startswith("BR-") for x in rules_of(r)), rules_of(r))

    def test_not_einvoices(self):
        for name, needle in [("EmptyPDFA1.pdf", "no embedded e-invoice"), ("zugferd1.pdf", "ZUGFeRD 1.0"),
                             ("corrupt-factur-x-waytoosmall.pdf", "")]:
            r = check(fx(name)) if actor.is_pdf(fx(name)) else ENGINE.run(core.validate_pdf, ENGINE,
                                                                              fx(name)).result()
            self.assertEqual(r["status"], "not_invoice", name)
            self.assertIn(needle, r["reasons"][0], name)


class TestRound2Attacks(unittest.TestCase):
    """Second break round (2026-10-29)."""

    def _pdf_with(self, files: dict):
        from pypdf import PdfReader, PdfWriter
        import io
        w = PdfWriter(clone_from=PdfReader(io.BytesIO(fx("E05_01_Einfach_fx.pdf"))))  # keeps factur-x.xml
        for n, d in files.items():
            w.add_attachment(n, d)
        out = io.BytesIO()
        w.write(out)
        return out.getvalue()

    def test_two_different_embedded_invoices_are_block(self):
        from pypdf import PdfReader
        import io
        good = PdfReader(io.BytesIO(fx("E05_01_Einfach_fx.pdf"))).attachments["factur-x.xml"][0]
        evil = re.sub(rb"(<ram:DuePayableAmount>)[^<]+", rb"\g<1>1.00", good, count=1)
        self.assertNotEqual(evil, good)
        for extra in ({"zugferd-invoice.xml": evil}, {"Factur-X.xml": evil}):
            r = check(self._pdf_with(extra))
            self.assertEqual(r["verdict"], "BLOCK", extra.keys())
            self.assertIn("more than one", r["reasons"][0])
        r = check(self._pdf_with({"zugferd-invoice.xml": good}))  # identical copies are fine
        self.assertEqual(r["verdict"], "ALLOW")

    def test_polyglot_xml_mentioning_pdf_is_validated_as_xml(self):
        x = fx("peppol-base-example.xml").replace(b"<Invoice ", b"<!-- %PDF-1.7 --><Invoice ", 1)
        self.assertFalse(actor.is_pdf(x))
        self.assertEqual(check(x)["verdict"], "ALLOW")

    def test_odd_inputs_never_crash_the_run(self):
        import asyncio
        x = fx("peppol-base-example.xml").decode()
        out = asyncio.run(actor.run({"invoices": [
            {"xml": x.replace("<cbc:Note>", "<cbc:Note>\ud800", 1)},
            {"file_base64": "data:application/xml;base64," + base64.b64encode(x.encode()).decode(), "name": {"a": 1}},
            {"file_base64": "!!!not base64!!!"}, {"url": 7}, "string item", None]}, engine=ENGINE))
        self.assertEqual(len(out), 6)
        self.assertIn("error", out[0])
        self.assertEqual((out[1]["verdict"], out[1]["source"]), ("ALLOW", "{'a': 1}"))
        for o in out[3:]:
            self.assertIn("error", o)


class _Counter(http.server.BaseHTTPRequestHandler):
    hits = 0

    def do_GET(self):  # noqa: N802
        type(self).hits += 1
        self.send_response(200)
        self.end_headers()
        self.wfile.write(fx("peppol-base-example.xml"))

    def log_message(self, *a):
        pass


class TestActor(unittest.TestCase):
    def items(self, inp):
        import asyncio
        return asyncio.run(actor.run(inp, engine=ENGINE))

    def test_batch_and_charging(self):
        b = lambda d: base64.b64encode(d).decode()  # noqa: E731
        out = self.items({"invoices": [
            {"file_base64": b(fx("peppol-base-example.xml")), "name": "a.xml"},
            {"xml": fx("xrechnung-cii.xml").decode("utf-8")},
            {"file_base64": b(fx("MINIMUM_Rechnung_fx.pdf"))},
            {"file_base64": b(fx("EmptyPDFA1.pdf"))},
            {"xml": "<x/>", "url": "https://example.com/a.xml"},
            {},
        ]})
        self.assertEqual([o.get("verdict") for o in out[:4]], ["ALLOW", "ALLOW", "REVIEW", "BLOCK"])
        self.assertEqual([actor.is_chargeable(o) for o in out], [True, True, True, False, False, False])
        self.assertIn("error", out[4])
        self.assertIn("error", out[5])

    def test_xml_text_with_foreign_encoding_declaration(self):
        x = fx("peppol-base-example.xml").decode("utf-8").replace('encoding="UTF-8"', 'encoding="ISO-8859-1"')
        out = self.items({"xml": x.replace("<cbc:Note>", "<cbc:Note>Größe ", 1)})
        self.assertEqual(out[0]["verdict"], "ALLOW", out[0].get("errors", out[0])[:2] if "errors" in out[0] else out)

    def test_bad_inputs(self):
        for bad in ({}, {"invoices": [{}] * 101}, {"url": "x", "invoices": [{"xml": "<a/>"}]},
                    {"xml": "<a/>", "profile": "nope"}, []):
            with self.assertRaises(actor.InputError):
                actor.pick_invoices(bad)

    def test_console_prefill_with_batch(self):
        docs = actor.pick_invoices({"url": actor.PREFILL_URL, "invoices": [{"xml": "<a/>"}]})
        self.assertEqual(docs, [{"xml": "<a/>"}])

    def test_private_urls_refused_and_guard_proven(self):
        srv = http.server.HTTPServer(("127.0.0.1", 0), _Counter)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{srv.server_address[1]}/inv.xml"
        try:
            _Counter.hits = 0
            for u in (url, "http://169.254.169.254/latest/meta-data", "file:///etc/passwd", "ftp://x/y"):
                out = self.items({"url": u})
                self.assertIn("error", out[0], u)
            self.assertEqual(_Counter.hits, 0)
            actor.GUARD_PEER = False
            try:
                out = self.items({"url": url})
            finally:
                actor.GUARD_PEER = True
            self.assertEqual(out[0]["verdict"], "ALLOW")
            self.assertEqual(_Counter.hits, 1)
        finally:
            srv.shutdown()

    def test_items_match_dataset_schema(self):
        import jsonschema
        schema = json.loads((ROOT / ".actor" / "dataset_schema.json").read_text())["fields"]
        v = jsonschema.Draft7Validator(schema)
        b = lambda d: base64.b64encode(d).decode()  # noqa: E731
        for it in self.items({"invoices": [{"file_base64": b(fx("peppol-base-example.xml"))},
                                           {"file_base64": b(fx("X04_01_Sammelrechnung_3_Bestellungen_fx.pdf"))},
                                           {"xml": "<a/>"}, {}]}):
            self.assertEqual([f"{list(e.path)}: {e.message}" for e in v.iter_errors(it)], [], it)

    def test_input_schema_is_valid_json_schema(self):
        s = json.loads((ROOT / ".actor" / "input_schema.json").read_text())
        self.assertEqual(s["properties"]["url"]["prefill"], actor.PREFILL_URL)
        self.assertEqual(set(s["properties"]["profile"]["enum"]), set(core.PROFILE_CHOICES))


if __name__ == "__main__":
    unittest.main()
