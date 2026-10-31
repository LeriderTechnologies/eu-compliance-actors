"""Offline tests: VIES is faked through Lookups(fetch=...). Run from this folder with payee_check on the path:
PYTHONPATH=.:../payee-check python3 -m unittest discover tests"""
import json
import pathlib
import unittest

from eu_common.text import InputError
from eu_common.lookups import LookupFailed, Lookups
from vat_validator import actor  # noqa: F401  (import check)
from vat_validator.check import check_vat, is_chargeable, run_batch

try:
    import jsonschema
except ImportError:  # pragma: no cover
    jsonschema = None

ROOT = pathlib.Path(__file__).resolve().parents[1]


def fake(answers):
    """answers: {number: (status, body)} or LookupFailed; records the numbers asked."""
    asked = []

    def fetch(url, timeout):
        num = url.rsplit("/", 1)[1]
        asked.append(num)
        a = answers.get(num, (200, {"isValid": False, "userError": "INVALID"}))
        if isinstance(a, Exception):
            raise a
        return a
    lk = Lookups(fetch=fetch, sleep=lambda s: None)
    return lk, asked


VALID = (200, {"isValid": True, "userError": "VALID", "name": "ACME B.V."})


class TestCheck(unittest.TestCase):
    def test_valid_with_name_match(self):
        lk, _ = fake({"004495445B01": VALID})
        r = check_vat({"vat_id": "nl 0044.95445-B01", "expected_name": "Acme BV"}, lookups=lk)
        self.assertEqual((r["vat_id"], r["status"], r["valid"], r["name"], r["name_match"]),
                         ("NL004495445B01", "valid", True, "ACME B.V.", 1.0))
        self.assertTrue(is_chargeable(r))

    def test_invalid_is_charged(self):
        lk, _ = fake({})
        r = check_vat("DE123456789", lookups=lk)
        self.assertEqual((r["status"], r["valid"]), ("invalid", False))
        self.assertTrue(is_chargeable(r))

    def test_bad_format_not_asked_not_charged(self):
        lk, asked = fake({})
        r = check_vat("DE12345", lookups=lk)
        self.assertEqual((r["status"], r["valid"], asked), ("invalid_format", False, []))
        self.assertFalse(is_chargeable(r))

    def test_non_eu_prefix_not_charged(self):
        lk, asked = fake({})
        r = check_vat("GB123456789", lookups=lk)
        self.assertEqual((r["status"], r["format_ok"], asked), ("not_checkable", None, []))
        self.assertFalse(is_chargeable(r))

    def test_greece_gr_becomes_el(self):
        lk, asked = fake({"123456789": VALID})
        self.assertEqual(check_vat("GR123456789", lookups=lk)["vat_id"], "EL123456789")

    def test_vies_down_not_charged(self):
        lk, _ = fake({"123456789": LookupFailed("timeout")})
        r = check_vat("DE123456789", lookups=lk)
        self.assertEqual((r["status"], r["valid"]), ("unavailable", None))
        self.assertFalse(is_chargeable(r))

    def test_batch_counts_and_errors(self):
        lk, asked = fake({"004495445B01": VALID})
        items, n = run_batch({"vat_ids": ["NL004495445B01", "DE123456789", "DE1", "", {"vat_id": "NL004495445B01"}]},
                             lookups=lk)
        self.assertEqual([it.get("status") for it in items], ["valid", "invalid", "invalid_format", None, "valid"])
        self.assertIn("error", items[3])
        self.assertEqual(n, 3)
        self.assertTrue(items[4]["cached"])
        self.assertEqual(asked, ["004495445B01", "123456789"])

    def test_bad_inputs(self):
        for bad in ({}, {"vat_ids": "NL1"}, {"vat_ids": ["X"] * 201}, {"vat_id": "NL1", "vat_ids": ["NL2"]}, []):
            with self.assertRaises(InputError, msg=bad):
                run_batch(bad, lookups=fake({})[0])

    def test_prefill_plus_empty_batch_runs_single(self):
        items, n = run_batch({"vat_id": "NL004495445B01", "vat_ids": []}, lookups=fake({"004495445B01": VALID})[0])
        self.assertEqual((len(items), n), (1, 1))


@unittest.skipUnless(jsonschema, "jsonschema not installed")
class TestSchemas(unittest.TestCase):
    def test_every_item_shape_matches_dataset_schema(self):
        schema = json.loads((ROOT / ".actor" / "dataset_schema.json").read_text())["fields"]
        v = jsonschema.Draft7Validator(schema)
        lk, _ = fake({"004495445B01": VALID, "999999999": LookupFailed("down")})
        items, _ = run_batch({"vat_ids": ["NL004495445B01", "DE123456789", "DE1", "GB123456789", "DE999999999",
                                          {"vat_id": "NL004495445B01", "expected_name": "Other NV"}, {"x": 1}]},
                             lookups=lk)
        for it in items:
            self.assertEqual([f"{list(e.path)}: {e.message}" for e in v.iter_errors(it)], [], it)

    def test_input_schema_prefill_is_a_valid_run(self):
        props = json.loads((ROOT / ".actor" / "input_schema.json").read_text())["properties"]
        prefill = {k: p["prefill"] for k, p in props.items() if "prefill" in p}
        items, n = run_batch(prefill, lookups=fake({"6388047V": VALID})[0])
        self.assertEqual(n, 1)


if __name__ == "__main__":
    unittest.main()
