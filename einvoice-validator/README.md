# E-Invoice Validator: XRechnung, ZUGFeRD, Factur-X, Peppol

**Is this e-invoice valid, and will the receiver accept it?** This Actor checks e-invoices against the **official
validation rules**: the European standard **EN 16931** (CEN/TC 434 rules, as published by the European Commission),
**Peppol BIS Billing 3.0** and **XRechnung 3.0**, plus the UBL 2.1 and UN/CEFACT CII XML schemas. It returns one
verdict per invoice, `ALLOW`, `REVIEW` or `BLOCK`, every error and warning with its official rule ID, and a summary
of the invoice (number, dates, seller, buyer, totals, IBAN).

Built for AI agents and automations in accounts payable and receivable: check an invoice before you send it to
Peppol or a German public portal, or before you book and pay one you received; and for e-invoicing projects that
need a quick, current validator in a pipeline.

## Formats

| Format | How it is recognised | Rules applied |
|---|---|---|
| **UBL 2.1** Invoice / CreditNote | root element | XML schema + EN 16931 |
| **UN/CEFACT CII** D16B | root element | XML schema + EN 16931 |
| **Peppol BIS Billing 3.0** | `CustomizationID` …`poacc:billing:3.0` | + Peppol rules |
| **XRechnung 3.0** (UBL or CII) | `CustomizationID` …`kosit:xrechnung_3.0` | + XRechnung rules (e.g. Leitweg-ID) |
| **Factur-X / ZUGFeRD 2.x** PDF | embedded `factur-x.xml` / `zugferd-invoice.xml` / `xrechnung.xml` | XML rules for its profile; PDF metadata cross-checked |

Use `profile` to apply rules the invoice doesn't declare, e.g. check a plain EN 16931 invoice against XRechnung
before sending it to a German public authority.

## Verdicts

| Verdict | When |
|---|---|
| `ALLOW` | No errors against any applicable rule set (warnings are listed; they don't make an invoice invalid) |
| `REVIEW` | No errors, but not a full e-invoice under the mandates: Factur-X/ZUGFeRD **MINIMUM** or **BASIC WL** (no line items, so not a complete EN 16931 invoice), or an **outdated XRechnung 1.x/2.x** identifier |
| `BLOCK` | Errors: wrong totals or VAT breakdown, missing mandatory fields, invalid codes, XML schema errors; or the file is not an e-invoice (plain PDF, other XML, ZUGFeRD 1.0, Order-X) |

`valid` is `true` only for `ALLOW`.

## Pricing

| Event | Price | When |
|---|---|---|
| `invoice-validation` | **$0.004** | An e-invoice was validated (any verdict: errors found are an answer) |
| (free) | $0 | Not an e-invoice (plain PDF, other XML, unreadable file), download failed, malformed input |

## Input

```json
{"url": "https://example.com/invoices/INV-2026-001.xml"}
```

```json
{"xml": "<Invoice xmlns=\"urn:oasis:names:specification:ubl:schema:xsd:Invoice-2\" ...>...</Invoice>", "profile": "xrechnung"}
```

```json
{"invoices": [{"url": "https://example.com/a.pdf"}, {"file_base64": "PD94bWwg...", "name": "INV-7.xml"}]}
```

Up to 100 invoices per run; files up to 25 MB (invoice XML up to 10 MB). `profile`: `auto` (default), `en16931`,
`peppol`, `xrechnung`, `all`.

## Output (one item per invoice; the Peppol BIS example invoice with its amount due changed to 9999.00, shortened)

```json
{
  "index": 0,
  "source": "base-example.xml",
  "verdict": "BLOCK",
  "valid": false,
  "status": "checked",
  "syntax": "UBL 2.1 Invoice",
  "customization_id": "urn:cen.eu:en16931:2017#compliant#urn:fdc:peppol.eu:2017:poacc:billing:3.0",
  "standards": ["EN 16931", "Peppol BIS Billing 3.0"],
  "reasons": ["1 error(s) against EN 16931, Peppol BIS Billing 3.0; first: BR-CO-16 [BR-CO-16]-Amount due for payment (BT-115) = Invoice total amount with VAT (BT-112) -Paid amount (BT-113) +Rounding amount (BT-114)."],
  "error_count": 1,
  "warning_count": 0,
  "errors": [{"rule": "BR-CO-16", "severity": "error", "message": "[BR-CO-16]-Amount due for payment (BT-115) = …",
              "location": "/*:Invoice[…]/*:LegalMonetaryTotal[1]", "rule_set": "en16931-ubl"}],
  "warnings": [],
  "summary": {"number": "Snippet1", "type_code": "380", "issue_date": "2017-11-13", "currency": "EUR",
              "seller": {"name": "SupplierOfficialName Ltd", "vat_id": "GB1232434", "country": "GB"},
              "buyer": {"name": "Buyer Official Name", "vat_id": "SE4598375937", "reference": "0150abc"},
              "totals": {"net": "1325", "tax": "331.25", "gross": "1656.25", "payable": "9999.00"},
              "due_date": "2017-12-01", "ibans": ["IBAN32423940"], "lines": 2},
  "rules_version": {"en16931": "1.3.17", "xrechnung": "XRechnung 3.0.2 / config 2026-08-31", "peppol_bis_billing": "3.0.21", "ubl": "2.1", "cii": "D16B"}
}
```

For a Factur-X / ZUGFeRD PDF the item also has `facturx` (profile) and `container` (embedded file name, the PDF's
XMP `ConformanceLevel`, attachments). A mismatch between the PDF metadata and the XML profile is a warning.

## Scope and limits

- **Official rules, current versions:** EN 16931 validation artefacts 1.3.17, Peppol BIS Billing 3.0.21, XRechnung
  3.0.2 (KoSIT configuration 2026-08-31). Each result shows the versions in `rules_version`. The rules are pinned
  and checksum-verified when the Actor is built; nothing is downloaded during your run.
- **Factur-X/ZUGFeRD EXTENDED** is a declared superset of EN 16931: the CII rules that only restrict elements to
  the EN 16931 core (`CII-DT-*`, `CII-SR-*`) are shown as warnings for EXTENDED; business rules still apply.
- **PDF/A-3 conformance** of a Factur-X PDF is not checked (`container.pdfa_checked: false`), and the visible PDF
  is not compared with the XML: the XML is the legal invoice.
- **Not checked here:** whether the seller's VAT number is registered, the IBAN belongs to the seller, or the buyer
  is reachable on Peppol. [Payee Check](https://apify.com/lerider/payee-check) does that (IBAN, VIES, Peppol, invoice
  maths) in one call.
- Validation follows the published rules; it is not tax or legal advice.

Files are processed in memory for your run only; nothing is stored beyond your run's dataset.

## Open source

The full source of this Actor is public under Apache-2.0: [github.com/LeriderTechnologies/eu-compliance-actors](https://github.com/LeriderTechnologies/eu-compliance-actors/tree/main/einvoice-validator). Read exactly what it does with your data, run the tests, or host it yourself.

## Related

- [Payee Check](https://apify.com/lerider/payee-check): IBAN, VAT, Peppol and invoice checks before paying a supplier
- [eIDAS Signature Validator](https://apify.com/lerider/eidas-signature-check): signed PDFs and XML against the EU trusted lists
- [EU VAT Validator](https://apify.com/lerider/eu-vat-validator) · [EORI Validator](https://apify.com/lerider/eori-validator) · [EU TIN Validator](https://apify.com/lerider/eu-tin-validator)

Made by Lerider Technologies (lerider.com).
