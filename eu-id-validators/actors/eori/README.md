# EORI Number Validator: EU Customs EORI Check (EOS)

Check EORI numbers live in **EOS**, the European Commission's official register of Economic Operators Registration
and Identification numbers. Built for import/export and customs workflows, freight and marketplace onboarding, and
AI agents that must confirm a trading partner's customs ID before they book, ship or pay.

- **Live EOS lookup** for EORI numbers issued by all 27 EU member states and Northern Ireland (XI)
- **Trader name and address** when the trader agreed to publish them in EOS
- **Batches of up to 500** per run (sent to EOS in groups of 10, its limit); a number repeated in the same run is
  checked and charged once
- **Honest edge cases:** GB numbers are UK-issued (HMRC) and not in EOS since Brexit, so they come back as
  `not_checkable` instead of a misleading "not valid"; an EOS outage comes back as `unavailable`

## Pricing: you pay only when EOS answers

| Event | Price | When |
|---|---|---|
| `eori-check` | **$0.002** | EOS answered: the number is `valid` or `invalid` |
| (free) | $0 | Wrong format, GB or non-EU prefix, EOS down, a repeat in the same run, malformed item |

That is $2 per 1,000 answered checks.

## Input

```json
{"eori": "IE6388047V"}
```

```json
{"eoris": ["DE123456789012345", "nl 123.456.789", "XI123456789000", "GB123456789000"]}
```

Spaces, dots, dashes and slashes are ignored, and `GR` is read as `EL` (Greece). Clear the single `eori` field when
you send `eoris`.

## Output

One dataset item per number, in input order:

```json
{
  "index": 0,
  "eori": "IE6388047V",
  "country": "IE",
  "format_ok": true,
  "status": "valid",
  "valid": true,
  "trader": null,
  "checked_at": "2026-10-28T19:30:00+00:00",
  "detail": "registered; the trader did not agree to publish name and address",
  "source": "EU EOS"
}
```

| `status` | Meaning | Charged |
|---|---|---|
| `valid` | Registered in an EU customs system | yes |
| `invalid` | EOS says the number is not registered | yes |
| `invalid_format` | Not 2 letters + 1–15 letters/digits; EOS was not asked | no |
| `not_checkable` | GB (check with HMRC) or a non-EU prefix | no |
| `unavailable` | EOS did not answer; try again later | no |

`trader` holds whatever EOS publishes for that number (typically name, street, postal code, city, country), or
`null` when the trader did not consent to publication.

## Open source

The full source of this Actor is public under Apache-2.0: [github.com/LeriderTechnologies/eu-compliance-actors](https://github.com/LeriderTechnologies/eu-compliance-actors/tree/main/eu-id-validators). Read exactly what it does with your data, run the tests, or host it yourself.

## Related

- [EU VAT Validator (VIES)](https://apify.com/lerider/eu-vat-validator): VAT numbers, same pay-per-answer model
- [EU TIN Validator](https://apify.com/lerider/eu-tin-validator): tax identification numbers, 27 EU countries
- [Payee Check](https://apify.com/lerider/payee-check): IBAN, VAT, Peppol and invoice checks before you pay a supplier

Results come straight from EOS at the time of the run. Nothing is stored beyond your run's dataset. Not legal or
customs advice. Made by Lerider Technologies (lerider.com).
