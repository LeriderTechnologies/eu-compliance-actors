# EU Compliance Actors

Source code of the EU compliance checks that [Lerider Technologies](https://lerider.com) runs on the
[Apify Store](https://apify.com/lerider). You can read exactly what each check does with your data, run the
tests, or host it yourself. The hosted versions are pay-per-answer: no subscription, and failed or unanswered
checks are free.

| Folder | Store page | What it checks | Source |
|---|---|---|---|
| [`eu-vat-validator`](eu-vat-validator) | [EU VAT Validator](https://apify.com/lerider/eu-vat-validator) | EU VAT numbers live in VIES, name match | open |
| [`eu-id-validators`](eu-id-validators) | [EORI Validator](https://apify.com/lerider/eori-validator) · [EU TIN Validator](https://apify.com/lerider/eu-tin-validator) | EORI numbers (EU EOS) and tax ids (EU TIN service) | open |
| [`eidas-check`](eidas-check) | [eIDAS Signature Validator](https://apify.com/lerider/eidas-signature-check) | Signed PDFs and XML against the EU trusted lists | open |
| [`einvoice-validator`](einvoice-validator) | [E-Invoice Validator](https://apify.com/lerider/einvoice-validator) | XRechnung, ZUGFeRD / Factur-X, Peppol BIS against the official rule sets | open |
| [`licence-check`](licence-check) | [EU Financial License Check](https://apify.com/lerider/eu-licence-check) | Firms in ESMA, EBA, MiCA and ECB registers and on national warning lists | open (data snapshot not included) |
| — | [Payee Check](https://apify.com/lerider/payee-check) | Invoice and payment-fraud check before paying an IBAN | **closed**, see below |

## Why Payee Check is closed

Payee Check looks for the signs of invoice and CEO fraud: a changed bank account, pressure wording, look-alike
names. Publishing the exact rules would show fraudsters what to avoid. So that one stays closed. Everything else
we run is here, so you can judge how we build before trusting the one you can't read. The parts it shares with
the VAT validator (VAT number formats, VIES and Peppol Directory lookups, name matching) are open, in
`eu-vat-validator/eu_common/`.

## Principles

- **No data kept.** Inputs are checked and returned, never stored or logged. VIES answers can contain personal data
  (sole traders); they live only in a 24-hour in-memory cache inside one run.
- **Fail closed.** If an official register or service doesn't answer, the result says so (`unavailable` or
  `REVIEW`), never a pass. Each Store page lists exactly what is and isn't charged.
- **Official sources only.** Every answer names the register that gave it and when.

## Running a check yourself

Each folder is a self-contained [Apify Actor](https://docs.apify.com/platform/actors): `.actor/` holds the
Dockerfile and input/output schemas, and the Python package next to it holds the logic. Tests run offline:

```bash
cd eu-vat-validator && python3 -m unittest discover tests
```

Some folders need extra packages (`requirements-actor.txt`) or a one-off build step: the e-invoice rule sets and the
eIDAS trusted lists are downloaded and verified at build time. The licence check needs a register snapshot in
`data/` (built with `python3 -m licence_check.index build` from the register history we record every day). That
history is not part of this repo, so the licence check can be read and tested here, but not run as is.

## Licence

Apache-2.0 for our code (see `LICENSE` and `NOTICE`). Test fixtures and downloaded rule sets keep their own
licences, listed next to them.

Found a way to fool a check? See [SECURITY.md](SECURITY.md). Questions: hello@lerider.com.
