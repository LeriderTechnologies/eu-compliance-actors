"""Offline tests: the EU services are faked through `post=`. Run from the folder above: python3 -m unittest discover tests"""
import json
import pathlib
import re
import unittest

from eu_ids import eori, tin
from eu_ids.common import InputError, is_chargeable

try:
    import jsonschema
except ImportError:  # pragma: no cover
    jsonschema = None

ROOT = pathlib.Path(__file__).resolve().parents[1]
FAST = {"sleep": lambda s: None}


def eos(valid: set[str], *, down: bool = False, calls: list | None = None, trader: dict | None = None):
    """Fake EOS: answers every <eori> in the request; status 0 if in `valid`."""
    def post(url, body, timeout):
        nums = re.findall(r"<ev:eori>([^<]*)</ev:eori>", body.decode())
        if calls is not None:
            calls.append(nums)
        if down:
            return 503, "busy"
        if len(nums) > 10:
            return 200, ("<S:Envelope xmlns:S='http://schemas.xmlsoap.org/soap/envelope/'><S:Body><r><return>"
                         "<errorDescription>Maximum allowed number of EORI in one request is 10.</errorDescription>"
                         "</return></r></S:Body></S:Envelope>")
        res = ""
        for n in nums:
            extra = "".join(f"<{k}>{v}</{k}>" for k, v in (trader or {}).items()) if n in valid else ""
            res += (f"<result><eori>{n}</eori><status>{0 if n in valid else 1}</status>"
                    f"<statusDescr>{'Valid' if n in valid else 'Not valid'}</statusDescr>{extra}</result>")
        return 200, ("<S:Envelope xmlns:S='http://schemas.xmlsoap.org/soap/envelope/'><S:Body>"
                     f"<ns0:validateEORIResponse xmlns:ns0='http://eori.ws.eos.dds.s/'><return>{res}</return>"
                     "</ns0:validateEORIResponse></S:Body></S:Envelope>")
    return post


def tinsvc(answers: dict, *, calls: list | None = None):
    """Fake TIN service: answers[(cc, tin)] = (structure, syntax) | 'fault' | 'down'; default = invalid structure."""
    def post(url, body, timeout):
        b = body.decode()
        cc = re.search(r"<urn:countryCode>([^<]*)<", b).group(1)
        t = re.search(r"<urn:tinNumber>([^<]*)<", b).group(1)
        if calls is not None:
            calls.append((cc, t))
        a = answers.get((cc, t), ("false", "false"))
        if a == "down":
            return 500, "<env:Envelope xmlns:env='http://schemas.xmlsoap.org/soap/envelope/'><env:Body><env:Fault><faultstring>SERVER_BUSY</faultstring></env:Fault></env:Body></env:Envelope>"
        if a == "fault":
            return 500, "<env:Envelope xmlns:env='http://schemas.xmlsoap.org/soap/envelope/'><env:Body><env:Fault><faultstring>INVALID_INPUT</faultstring></env:Fault></env:Body></env:Envelope>"
        st, sy = a
        syn = "" if sy is None else f"<ns2:validSyntax>{sy}</ns2:validSyntax>"
        return 200, ("<env:Envelope xmlns:env='http://schemas.xmlsoap.org/soap/envelope/'><env:Body>"
                     "<ns2:checkTinResponse xmlns:ns2='urn:ec.europa.eu:taxud:tin:services:checkTin:types'>"
                     f"<ns2:countryCode>{cc}</ns2:countryCode><ns2:tinNumber>{t}</ns2:tinNumber>"
                     f"<ns2:validStructure>{st}</ns2:validStructure>{syn}</ns2:checkTinResponse></env:Body></env:Envelope>")
    return post


class TestEori(unittest.TestCase):
    def run_(self, inp, **kw):
        return eori.run_batch(inp, **FAST, **kw)

    def test_statuses_and_charging(self):
        items, n = self.run_({"eoris": ["ie 638.8047-v", "NL004495445", "GB123456789000", "US123", "D", "", "IE6388047V"]},
                             post=eos({"IE6388047V"}))
        self.assertEqual([it.get("status") for it in items],
                         ["valid", "invalid", "not_checkable", "not_checkable", "invalid_format", None, "valid"])
        self.assertIn("error", items[5])
        self.assertEqual(items[0]["eori"], "IE6388047V")
        self.assertEqual(items[6]["duplicate_of"], 0)
        self.assertEqual(n, 2)  # IE once + NL; the repeat, GB, US, bad format and error are free
        self.assertIn("HMRC", items[2]["detail"])

    def test_trader_details_passed_through(self):
        items, _ = self.run_({"eori": "DE123456789012345"},
                             post=eos({"DE123456789012345"}, trader={"name": "ACME GMBH", "city": "BERLIN"}))
        self.assertEqual(items[0]["trader"], {"name": "ACME GMBH", "city": "BERLIN"})

    def test_chunks_of_ten(self):
        calls = []
        nums = [f"DE{i:012d}" for i in range(23)]
        items, n = self.run_({"eoris": nums}, post=eos(set(nums), calls=calls))
        self.assertEqual([len(c) for c in calls], [10, 10, 3])
        self.assertEqual(n, 23)

    def test_outage_is_free(self):
        items, n = self.run_({"eoris": ["IE6388047V", "BE0403170701"]}, post=eos(set(), down=True))
        self.assertEqual({it["status"] for it in items}, {"unavailable"})
        self.assertEqual(n, 0)

    def test_greece_and_xi(self):
        items, _ = self.run_({"eoris": ["GR123456789", "XI123456789000"]}, post=eos({"EL123456789", "XI123456789000"}))
        self.assertEqual([(it["eori"], it["status"]) for it in items],
                         [("EL123456789", "valid"), ("XI123456789000", "valid")])

    def test_bad_inputs(self):
        for bad in ({}, {"eoris": "IE1"}, {"eoris": ["IE1"] * 501}, {"eori": "IE1", "eoris": ["IE2"]}, []):
            with self.assertRaises(InputError, msg=bad):
                self.run_(bad, post=eos(set()))


class TestTin(unittest.TestCase):
    def run_(self, inp, **kw):
        return tin.run_batch(inp, **FAST, **kw)

    def test_statuses(self):
        svc = tinsvc({("DE", "86095742719"): ("true", "true"), ("BE", "00012511118"): ("true", "false"),
                      ("EL", "123456789"): ("true", "false"), ("CY", "12345678L"): ("true", None),
                      ("FR", "X"): "fault"})
        items, n = self.run_({"tins": [
            {"country": "de", "tin": "860 957 427-19"}, {"country": "BE", "tin": "00.012511/118"},
            {"country": "GR", "tin": "123456789"}, {"country": "CY", "tin": "12345678L"}, {"country": "FR", "tin": "X"},
            {"country": "GB", "tin": "1234567890"}, {"country": "DE", "tin": "86095742719"}, {"tin": "1"}, "DE1",
            {"country": "NL", "tin": "1"}]}, post=svc)
        self.assertEqual([it.get("status") for it in items],
                         ["valid", "invalid", "invalid", "valid", "invalid", "not_checkable", "valid", None, None, "invalid"])
        self.assertEqual(items[0]["tin"], "86095742719")
        self.assertEqual(items[2]["country"], "EL")
        self.assertIsNone(items[3]["valid_syntax"])  # no check digit for that country: structure decides
        self.assertIn("check digit", items[1]["detail"])
        self.assertIn("rejected", items[4]["detail"])
        self.assertEqual(items[6]["duplicate_of"], 0)
        self.assertIn("no public register", items[0]["detail"])
        self.assertEqual(n, 6)  # 0,1,2,3,4,9; GB, the repeat and the 2 errors are free

    def test_outage_is_free(self):
        items, n = self.run_({"country": "DE", "tin": "86095742719"}, post=tinsvc({("DE", "86095742719"): "down"}))
        self.assertEqual((items[0]["status"], n), ("unavailable", 0))
        self.assertFalse(is_chargeable(items[0]))

    def test_bad_inputs(self):
        for bad in ({}, {"country": "DE"}, {"tins": {}}, {"tins": [{}] * 201}, {"tin": "1", "tins": [{"country": "DE", "tin": "2"}]}):
            with self.assertRaises(InputError, msg=bad):
                self.run_(bad, post=tinsvc({}))


@unittest.skipUnless(jsonschema, "jsonschema not installed")
class TestSchemas(unittest.TestCase):
    def check(self, key, items):
        schema = json.loads((ROOT / "actors" / key / ".actor" / "dataset_schema.json").read_text())["fields"]
        v = jsonschema.Draft7Validator(schema)
        for it in items:
            self.assertEqual([f"{list(e.path)}: {e.message}" for e in v.iter_errors(it)], [], it)

    def test_eori_items_match_schema(self):
        items, _ = eori.run_batch({"eoris": ["IE6388047V", "NL1", "GB1", "X", "", "IE6388047V", "DE123"]}, **FAST,
                                  post=eos({"IE6388047V"}, trader={"name": "X"}))
        self.check("eori", items)
        items, _ = eori.run_batch({"eori": "IE6388047V"}, **FAST, post=eos(set(), down=True))
        self.check("eori", items)

    def test_tin_items_match_schema(self):
        items, _ = tin.run_batch({"tins": [{"country": "DE", "tin": "1"}, {"country": "GB", "tin": "1"}, {"tin": "1"},
                                           {"country": "DE", "tin": "1"}, {"country": "CY", "tin": "2"}]}, **FAST,
                                 post=tinsvc({("DE", "1"): ("true", "true"), ("CY", "2"): ("true", None)}))
        self.check("tin", items)

    def test_prefills_are_valid_runs(self):
        for key, mod, post in (("eori", eori, eos({"IE6388047V"})), ("tin", tin, tinsvc({("DE", "86095742719"): ("true", "true")}))):
            props = json.loads((ROOT / "actors" / key / ".actor" / "input_schema.json").read_text())["properties"]
            prefill = {k: p["prefill"] for k, p in props.items() if "prefill" in p}
            items, n = mod.run_batch(prefill, **FAST, post=post)
            self.assertEqual((n, items[0]["status"]), (1, "valid"), key)


if __name__ == "__main__":
    unittest.main()
