"""Generate actors/<eori|tin>/.actor/* and listing.json (title, SEO, pricing) from one place. Run after edits."""
import json
import pathlib

HERE = pathlib.Path(__file__).resolve().parent


def n(t):
    return {"type": [t, "null"]}


COMMON = {
    "index": {**n("integer"), "description": "Position in your input (0-based)"},
    "status": {**n("string"), "description": "valid / invalid = the EU service answered (charged once per distinct number); invalid_format, not_checkable, unavailable = not charged"},
    "valid": {**n("boolean"), "description": "true / false, null = not known"},
    "checked_at": {**n("string"), "description": "When the EU service was asked (UTC, ISO 8601)"},
    "detail": {**n("string"), "description": "Why the status is what it is"},
    "source": {**n("string"), "description": "Which official EU service answered"},
    "duplicate_of": {**n("integer"), "description": "Index of the same number earlier in this batch (not charged again)"},
    "error": {**n("string"), "description": "Malformed item (not charged)"},
}
CFG = {
    "eori": dict(
        name="eori-validator", title="EORI Number Validator: EU Customs EORI Check (EOS)", event="eori-check", price=0.002,
        event_title="EORI number checked in EU EOS",
        event_desc="One EORI number the EU EOS service answered (valid or invalid). Bad formats, GB/non-EU numbers, repeats in the same run, EOS outages and malformed items are free.",
        description="Check EU EORI numbers live in the European Commission's EOS customs register: valid or not, plus the trader's name and address where published. Batches of up to 500, GB numbers flagged, $0.002 per answered check; bad formats and outages are free.",
        seoTitle="EORI Number Validator API: Bulk EU EORI Check (EOS)",
        seoDescription="Validate EU EORI numbers live in the EC EOS register, with trader name and address where published. Bulk up to 500. $0.002 per answered check; errors free.",
        module="eu_ids.eori_actor", memory=256,
        fields={"eori": {**n("string"), "description": "EORI as checked (no spaces, GR -> EL)"},
                "country": {**n("string"), "description": "Country prefix"},
                "format_ok": {**n("boolean"), "description": "Offline format check (2 letters + 1-15 letters/digits)"},
                "trader": {"type": ["object", "null"], "description": "Name and address from EOS, only if the trader agreed to publish them"}},
        view=["index", "eori", "status", "valid", "trader", "detail", "error"],
        input={"title": "EORI Validator input", "type": "object", "schemaVersion": 1, "properties": {
            "eori": {"title": "EORI number", "type": "string", "editor": "textfield", "prefill": "IE6388047V",
                     "description": "One EORI number with its country prefix, e.g. DE123456789012345 or NL123456789. Spaces, dots and dashes are ignored. Leave empty when you use 'EORI numbers (batch)'."},
            "eoris": {"title": "EORI numbers (batch)", "type": "array", "editor": "stringList",
                      "description": "Up to 500 EORI numbers per run. Clear the single 'EORI number' field when you use this."}}}),
    "tin": dict(
        name="eu-tin-validator", title="EU TIN Validator: Tax ID Number Check for 27 EU Countries", event="tin-check", price=0.002,
        event_title="TIN checked by the EU TIN service",
        event_desc="One TIN the European Commission's TIN service answered (valid or invalid structure/check digit). Non-EU countries, repeats in the same run, outages and malformed items are free.",
        description="Check Tax Identification Numbers (TINs) of all 27 EU member states with the European Commission's official TIN service: structure and check digit, for DAC7 seller onboarding, KYC and invoices. Batches of up to 200, $0.002 per answered check; outages are free.",
        seoTitle="EU TIN Validator API: Tax ID Number Check (DAC7, KYC)",
        seoDescription="Validate tax ID numbers for all 27 EU countries with the EC TIN service: structure and check digit. Bulk up to 200, for DAC7 and KYC. $0.002 per check.",
        module="eu_ids.tin_actor", memory=256,
        fields={"country": {**n("string"), "description": "Issuing country (EU code, Greece = EL)"},
                "tin": {**n("string"), "description": "TIN as checked (no spaces, dots, dashes or slashes)"},
                "valid_structure": {**n("boolean"), "description": "Length and character pattern are right for that country"},
                "valid_syntax": {**n("boolean"), "description": "Check digit / algorithm is right (null where the country has none)"}},
        view=["index", "country", "tin", "status", "valid", "valid_structure", "valid_syntax", "detail", "error"],
        input={"title": "EU TIN Validator input", "type": "object", "schemaVersion": 1, "properties": {
            "country": {"title": "Country", "type": "string", "editor": "textfield", "prefill": "DE",
                        "description": "2-letter code of the EU country that issued the TIN, e.g. DE, FR, IT, NL (Greece: EL or GR)."},
            "tin": {"title": "TIN", "type": "string", "editor": "textfield", "prefill": "86095742719",
                    "description": "The tax identification number, without a country prefix. Spaces, dots, dashes and slashes are ignored. Leave empty when you use 'TINs (batch)'."},
            "tins": {"title": "TINs (batch)", "type": "array", "editor": "json",
                     "description": "Up to 200 items per run: [{\"country\": \"DE\", \"tin\": \"...\"}, ...]. Clear the single 'TIN' field when you use this."}}}),
}


def main():
    for key, c in CFG.items():
        assert len(c["title"]) <= 63 and len(c["description"]) <= 300 and len(c["seoDescription"]) <= 160, \
            (key, len(c["title"]), len(c["description"]), len(c["seoDescription"]))
        d = HERE / "actors" / key / ".actor"
        d.mkdir(parents=True, exist_ok=True)
        dump = lambda obj, name: (d / name).write_text(json.dumps(obj, indent=2) + "\n")  # noqa: E731
        dump({"actorSpecification": 1, "name": c["name"], "title": c["title"], "description": c["description"],
              "version": "0.1", "buildTag": "latest", "defaultMemoryMbytes": c["memory"], "input": "./input_schema.json",
              "dockerfile": "./Dockerfile", "storages": {"dataset": "./dataset_schema.json"}, "output": "./output_schema.json"},
             "actor.json")
        dump(c["input"], "input_schema.json")
        props = {"index": COMMON["index"], **c["fields"], **{k: v for k, v in COMMON.items() if k != "index"}}
        dump({"actorSpecification": 1,
              "fields": {"$schema": "http://json-schema.org/draft-07/schema#", "type": "object",
                         "description": "One item per number. Malformed items carry only index + error and are not charged.",
                         "properties": props, "required": ["index"]},
              "views": {"overview": {"title": "Results", "transformation": {"fields": c["view"]},
                                     "display": {"component": "table", "properties": {
                                         f: {"label": f.replace("_", " ").capitalize()} for f in c["view"]}}}}},
             "dataset_schema.json")
        dump({"actorOutputSchemaVersion": 1, "title": f"{c['title'].split(':')[0]} results",
              "description": "One dataset item per number.",
              "properties": {"results": {"type": "string", "title": "Results (dataset items)",
                                         "template": "{{links.apiDefaultDatasetUrl}}/items"},
                             "overview": {"type": "string", "title": "Results table",
                                          "template": "{{links.apiDefaultDatasetUrl}}/items?view=overview"}}},
             "output_schema.json")
        (d / "Dockerfile").write_text("FROM apify/actor-python:3.12\nCOPY requirements-actor.txt ./\n"
                                      "RUN pip install --no-cache-dir -r requirements-actor.txt\nCOPY eu_ids ./eu_ids\n"
                                      f'CMD ["python3", "-m", "{c["module"]}"]\n')
        (HERE / "actors" / key / "listing.json").write_text(json.dumps(
            {k: c[k] for k in ("name", "event", "price", "event_title", "event_desc", "seoTitle", "seoDescription")},
            indent=2) + "\n")
        print(key, "ok")


if __name__ == "__main__":
    main()
