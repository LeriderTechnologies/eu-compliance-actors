# EU TIN Validator: Tax ID Number Check for 27 EU Countries

Check Tax Identification Numbers (TINs) for every EU member state with the European Commission's official **TIN
service**: is the structure right for that country, and is the check digit right? Built for DAC7 seller onboarding on
platforms and marketplaces, KYC/KYB forms, payroll and invoice checks, and AI agents that collect tax IDs.

- **All 27 EU member states**, for both people and companies, using each country's official rules
- **Two answers per TIN:** `valid_structure` (length and characters) and `valid_syntax` (check digit / algorithm)
- **Batches of up to 200** per run; a TIN repeated in the same run is checked and charged once
- **Clear limits:** the EU publishes no register of issued TINs, so a `valid` TIN has the right format and check
  digit; it does not prove the number was issued to that person or company

## Pricing: you pay only when the EU service answers

| Event | Price | When |
|---|---|---|
| `tin-check` | **$0.002** | The EU TIN service answered: `valid` or `invalid` |
| (free) | $0 | Non-EU country, service down, a repeat in the same run, malformed item |

That is $2 per 1,000 answered checks.

## Input

```json
{"country": "DE", "tin": "86095742719"}
```

```json
{"tins": [{"country": "FR", "tin": "30 23 217 600 053"}, {"country": "IT", "tin": "RSSMRA85T10A562S"},
          {"country": "NL", "tin": "123456782"}, {"country": "GR", "tin": "123456789"}]}
```

Give the TIN without a country prefix. Spaces, dots, dashes and slashes are ignored. `GR` is read as `EL` (Greece).
Clear the single `tin` field when you send `tins`.

## Output

```json
{
  "index": 0,
  "country": "DE",
  "tin": "86095742719",
  "status": "valid",
  "valid": true,
  "valid_structure": true,
  "valid_syntax": true,
  "checked_at": "2026-10-28T19:30:00+00:00",
  "detail": "valid DE TIN format (structure and check digits only: the EU has no public register of issued TINs)",
  "source": "EU TIN service"
}
```

| `status` | Meaning | Charged |
|---|---|---|
| `valid` | Right structure and (where the country has one) the right check digit | yes |
| `invalid` | Wrong structure, wrong check digit, or rejected as malformed for that country | yes |
| `not_checkable` | Not an EU member state code (e.g. GB, CH, NO, US) | no |
| `unavailable` | The EU service did not answer; try again later | no |

Some countries have no check digit; `valid_syntax` is then `null` and the structure decides.

## Privacy

TINs of natural persons are personal data. They are sent only to the European Commission's TIN service and returned
in your own run's dataset. The Actor logs and stores nothing else.

## Open source

The full source of this Actor is public under Apache-2.0: [github.com/LeriderTechnologies/eu-compliance-actors](https://github.com/LeriderTechnologies/eu-compliance-actors/tree/main/eu-id-validators). Read exactly what it does with your data, run the tests, or host it yourself.

## Related

- [EU VAT Validator (VIES)](https://apify.com/lerider/eu-vat-validator): VAT numbers, live registration check
- [EORI Number Validator](https://apify.com/lerider/eori-validator): EU customs IDs
- [Payee Check](https://apify.com/lerider/payee-check): IBAN, VAT, Peppol and invoice checks before you pay a supplier

Not tax or legal advice. Made by Lerider Technologies (lerider.com).
