# eIDAS Signature Validator: Check Signed PDFs & XML (QES, EU)

**Is this signed document genuine, and can I rely on it?** This Actor validates the digital signatures and electronic
seals in **PDFs**, in **signed XML** (XAdES / XML-DSig) and in **separate signature files** (detached XAdES) against
the **official EU trusted lists** (eIDAS), the way an EU validation service does, and returns one verdict per
document: `ALLOW`, `REVIEW` or `BLOCK`, with the reasons.

**New: authentic EU law.** Give a CELEX number or ELI (e.g. `32024R1689`, the AI Act) and the Actor fetches the
act's official PDF and the Publications Office's qualified e-seal and proves the text is the authentic Official
Journal version, in any of the 24 languages. Or give your own copy and find out whether it is exactly the
published text.

Built for AI agents and automations that receive signed contracts, mandates, invoices, certificates or official
documents and cannot "look at the blue signature bar" themselves; and for KYB/onboarding, procurement and legal ops.

## What it checks, per signature

- **Intact:** the signed bytes still match the signature (no changes inside what was signed)
- **Changed after signing:** pages, page content or resources added or replaced later (a classic way to slip a
  different text under a valid signature), versus allowed changes such as form filling, annotations and
  long-term-validation data
- **Byte-range tricks:** signatures that don't cover their whole revision
- **Who signed:** signer and issuing CA (name, organisation, country)
- **EU trust:** does the signer's certificate chain to a trust service on an **EU member state trusted list**
  (all 27 + EEA lists, via the EU List of Trusted Lists, signatures of the lists verified)?
- **Qualified or not:** qualified electronic signature (QES) or qualified seal, from the trusted list and the
  certificate's QC statements, plus whether the key is on a qualified device (QSCD), and the trust service's name
- **Revocation:** OCSP/CRL checked where the certificate points to them
- **Signing time:** claimed time and the time proven by a trusted timestamp (long-term validation, PAdES B-T/LT/LTA)

## Verdicts

| Verdict | When |
|---|---|
| `ALLOW` | Every signature is intact, cryptographically valid, chains to an EU trusted provider, not revoked, and the pages were not changed after signing. Set `require_qualified: true` to also require a qualified signature or seal |
| `REVIEW` | Intact, but trust can't be fully established: signer not under an EU trusted provider (e.g. a non-EU or self-made certificate), validity can't be proven at signing time, revocation info unavailable, forms or annotations changed, or not qualified while you asked for qualified |
| `BLOCK` | Signature broken or doesn't match the content, certificate revoked, pages or page content changed after signing, byte-range manipulation, unreadable PDF, or no signature at all |

`REVIEW` and `BLOCK` both mean "don't proceed automatically"; `BLOCK` means there is evidence the document is not
what was signed.

## Separate signature files and EU Official Journal acts

Many signed documents keep the signature in a separate `.xml` file (detached XAdES): contracts signed with national
eID tools, e-invoicing platforms, archives, and every **EU Official Journal act since October 2023** (one qualified
seal of the Publications Office covers the act in all 24 languages). The Actor:

- verifies the XML signature itself (signed info, every reference, the XAdES signing-certificate binding),
- checks that **your file matches one of the files the signature covers** (`document_match`; a changed file, or a
  different language version, is `BLOCK`),
- validates the signer's chain against the EU trusted lists and its qualified status,
- validates the **signature timestamp** (RFC 3161, timestamp authority on an EU trusted list) and, when the
  certificate has expired since, proves validity **at the timestamp** with the revocation data embedded in the
  signature (long-term validation, XAdES-LT/LTA; `sub_indication: OK_AT_TIMESTAMP`).

Signed XML on its own (e.g. a signed XML invoice or declaration) is checked as an enveloped signature; a signature
that covers only part of the XML gets `REVIEW`, so a signed fragment can't vouch for unsigned content around it.

## Pricing

| Event | Price | When |
|---|---|---|
| `signature-check` | **$0.03** | A document was read and checked: PDF, signed XML, document + separate signature, or EU act (signed or unsigned: "no signature" is an answer too) |
| (free) | $0 | URL could not be downloaded, file is not a readable PDF/XML, EU act not found or without a seal, malformed input |

## Input

```json
{"url": "https://example.com/signed-contract.pdf", "require_qualified": true}
```

```json
{"documents": [{"url": "https://example.com/a.pdf"}, {"pdf_base64": "JVBERi0xLjc...", "name": "mandate.pdf"}]}
```

```json
{"url": "https://example.com/contract.pdf", "signature_url": "https://example.com/contract.xml"}
```

```json
{"eu_act": "32024R1689", "language": "DE", "require_qualified": true}
```

```json
{"documents": [{"eu_act": "http://data.europa.eu/eli/reg/2024/1689/oj", "language": "NL",
                "pdf_base64": "JVBERi0xLjc... (your own copy)"}]}
```

Up to 20 documents per run, 25 MB each. Clear the single `url` field when you send `documents`. With `eu_act`, the
output also has `eu_act` (CELEX, language and the Publications Office links of the PDF and the seal).

## Output (one item per document; illustrative values, shortened)

```json
{
  "index": 0,
  "source": "https://example.com/signed-contract.pdf",
  "verdict": "ALLOW",
  "status": "checked",
  "reasons": ["Signature1: valid qualified signature, signer chains to an EU trusted provider (Example Qualified CA)"],
  "signatures": [{
    "field": "Signature1", "verdict": "ALLOW", "indication": "PASSED", "sub_indication": "OK",
    "signer": {"common_name": "…", "organization": "…", "country": "BE"},
    "issuer": {"common_name": "Example Qualified CA", "country": "BE"},
    "claimed_signing_time": "…", "proven_signing_time": "…",
    "intact": true, "crypto_valid": true, "trusted": true, "revoked": false,
    "qualified": true, "qualified_type": "qualified_signature", "qscd": true,
    "trust_service": "Example Qualified CA service",
    "coverage": "ENTIRE_FILE", "changed_after_signing": null, "timestamped": true
  }],
  "document": {"sha256": "…", "bytes": 61234, "signature_count": 1, "document_timestamps": 0},
  "trusted_lists": {"built_at": "2026-10-28T19:00:00+00:00", "source": "baked"}
}
```

`indication` / `sub_indication` follow ETSI EN 319 102-1 (`PASSED`, `INDETERMINATE`, `FAILURE`). A valid signature
without a signature timestamp passes with `sub_indication: OK_NO_TIMESTAMP`: its validity is proven as of the check,
not as of signing.

## Scope and limits (v0.2)

- **PDF signatures** (PAdES and Adobe-style PDF signatures, incl. document timestamps), **XML signatures**
  (XAdES-B/T/LT/LTA and plain XML-DSig, enveloped or detached, direct references or a signed `ds:Manifest`). ASiC
  containers and CAdES (.p7m) are not supported yet. For an XML file with several signatures, the first is validated
  and the others are listed as not validated (`REVIEW`).
- **XML archive timestamps** (XAdES-LTA) are counted (`archive_timestamps`) but not verified; validity rests on the
  signature timestamp and the embedded revocation data.
- **EU acts:** only Official Journal acts published act-by-act from 1 October 2023 carry a detached seal. Older acts
  and consolidated versions have none; the Actor says so and doesn't charge.
- **Legacy formats:** signatures in formats the engine can't validate (e.g. the SHA-1 based `adbe.pkcs7.sha1` some
  official gazettes still use) are reported as present, with signer and issuer, and get `REVIEW`, never "unsigned".
- **Advanced vs. qualified:** many official e-seals (e.g. Austria's Amtssignatur) come from CAs that are not on an EU
  trusted list; they are intact and cryptographically valid but get `REVIEW` because they are not EU-qualified.
- **EU trust only:** a signature under a non-EU CA (e.g. a US e-signature platform's own certificate) gets `REVIEW`,
  not `BLOCK`: it may be fine, it just isn't EU-trusted.
- Some historic services on national lists use extensions the engine can't read; signatures under them get
  `REVIEW`, never a false `ALLOW`.
- **How current are the trusted lists?** The EU List of Trusted Lists and every national list are downloaded,
  signature-checked and bundled into this Actor **every Sunday at about 05:30 Amsterdam time**, after the test
  suite passes. Every result shows when (`trusted_lists.built_at`). If the bundled copy is ever older than 7 days,
  the run downloads fresh lists itself (+15–30 s), so a result never relies on trusted lists older than a week.
  Revocation of the signer's certificate (OCSP/CRL) is not taken from the bundle: it is checked during your run,
  from the revocation data in the document and, where needed, the certificate authority's live service.
- A technical validation, not legal advice on what a signature means for your contract.

Engine: [pyHanko](https://github.com/MatthiasValvekens/pyHanko) (MIT) and, for XML,
[signxml](https://github.com/XML-Security/signxml) (Apache-2.0), tested against attack and edge-case files from the
European Commission's DSS test suite, signature-wrapping and entity-expansion attacks, and real Official Journal
seals. Revocation and issuer lookups only go to public internet addresses. Documents are processed in memory for your run only; nothing is stored
beyond your run's dataset.

## Open source

The full source of this Actor is public under Apache-2.0: [github.com/LeriderTechnologies/eu-compliance-actors](https://github.com/LeriderTechnologies/eu-compliance-actors/tree/main/eidas-check). Read exactly what it does with your data, run the tests, or host it yourself.

## Related

- [Payee Check](https://apify.com/lerider/payee-check): IBAN, VAT, Peppol and invoice checks before paying a supplier
- [EU Financial License Check](https://apify.com/lerider/eu-licence-check): is this firm authorised or on a warning list?
- [EU VAT Validator](https://apify.com/lerider/eu-vat-validator) · [EORI Validator](https://apify.com/lerider/eori-validator) · [EU TIN Validator](https://apify.com/lerider/eu-tin-validator)

Made by Lerider Technologies (lerider.com).
