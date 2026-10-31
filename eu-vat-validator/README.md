# EU VAT Validator (VIES): Check VAT Numbers, Pay Per Answer

Check EU VAT numbers live in **VIES**, the European Commission's official register, and get the registered company
name back. Built for supplier onboarding, invoice checks, checkout VAT exemption and AI agents that need to know
"is this VAT number real, and whose is it?"

- **Live VIES lookup** for all 27 EU member states plus Northern Ireland (XI)
- **Offline pre-check** of the number format per country (and the checksum for Dutch numbers), so typos are caught
  without a register call
- **Name match** (optional): pass the name you expect and get a 0–1 score; legal forms and punctuation are ignored
- **Batches** of up to 200 numbers per run; a number repeated in the same run is looked up once
- **Retries** when a member state's VIES service is slow, and an honest `unavailable` status when it stays down

## Pricing: you pay only when VIES answers

| Event | Price | When |
|---|---|---|
| `vat-check` | **$0.003** | VIES answered: the number is `valid` or `invalid` |
| (free) | $0 | Wrong format (`invalid_format`), non-EU prefix (`not_checkable`), VIES down (`unavailable`), malformed item |

That is $3 per 1,000 answered checks. A batch full of typos or a VIES outage costs nothing.

## Input

One number:

```json
{"vat_id": "IE6388047V", "expected_name": "Google Ireland Limited"}
```

A batch (strings or objects, up to 200):

```json
{"vat_ids": ["NL123456789B01", "DE 123.456.789", {"vat_id": "FR40303265045", "expected_name": "Sodimas"}]}
```

Spaces, dots and dashes are ignored, and `GR` is read as `EL` (VIES uses EL for Greece). Clear the single `vat_id`
field when you send `vat_ids`.

## Output

One dataset item per number, in input order:

```json
{
  "index": 0,
  "vat_id": "IE6388047V",
  "country": "IE",
  "format_ok": true,
  "status": "valid",
  "valid": true,
  "name": "GOOGLE IRELAND LIMITED",
  "name_match": 1.0,
  "checked_at": "2026-10-28T12:00:00+00:00",
  "cached": false,
  "detail": null,
  "source": "VIES"
}
```

| `status` | Meaning | Charged |
|---|---|---|
| `valid` | Registered in VIES for intra-EU trade | yes |
| `invalid` | VIES says the number is not registered | yes |
| `invalid_format` | Not the format of that country's VAT numbers; VIES was not asked | no |
| `not_checkable` | Prefix is not a VIES country (e.g. GB, CH, NO) | no |
| `unavailable` | VIES or the member state's service did not answer; try again later | no |

Dutch numbers also get `nl_checksum_ok`. Some member states (for example Germany) never return a name through
VIES; `name` is then `null` and `name_match` stays `null`.

## Good to know

- VIES confirms that a number is registered for **intra-EU** transactions. A business can have a domestic VAT number
  that is not active in VIES.
- Results come straight from VIES at the time of the run (`checked_at`). Nothing is stored beyond your run's dataset.
- Need more than a VAT number, for example the IBAN, Peppol registration and invoice VAT maths before you pay a
  supplier? Use [Payee Check](https://apify.com/lerider/payee-check), built on the same engine.

Not tax or legal advice. Made by Lerider Technologies (lerider.com).

## Open source

The full source of this Actor is public under Apache-2.0: [github.com/LeriderTechnologies/eu-compliance-actors](https://github.com/LeriderTechnologies/eu-compliance-actors/tree/main/eu-vat-validator). Read exactly what it does with your data, run the tests, or host it yourself.
