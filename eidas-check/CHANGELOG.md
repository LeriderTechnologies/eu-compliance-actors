# Changelog

Routine data-only rebuilds (scheduled, see the README) are not listed here.

## 0.2.3 (2026-10-30)
- **Fix:** a signature or seal with a qualified certificate that was *not* created on a qualified signature/seal
  creation device (`qscd: false`) is no longer described as "qualified". With `require_qualified: true` it is now
  `REVIEW` ("qualified certificate, but not created on a qualified … device (QSCD)"); without it, `ALLOW` says
  "advanced signature with a qualified certificate (no QSCD, so not qualified)".

## 0.2.2 (2026-10-29)
- **Security:** a separate signature file now has to cover the document you send. Before, a valid signature over
  some other content (e.g. an unrelated signed XML file) could be reported as `ALLOW` next to an unsigned document;
  now that is `BLOCK` ("this signature does not cover the file you gave").
- Plain XML signatures with a single reference (common for signed XML files) are now validated instead of being
  reported as "could not process".
- A signature covering several files (e.g. contract + annexes) checked with only one of them gives `REVIEW` (the
  others weren't provided), no longer a false `BLOCK`. Unsupported XML transforms give `REVIEW` instead of `BLOCK`.
- Revocation downloads are capped in size; `data:` URLs are accepted for base64 inputs.

## 0.2.1 (2026-10-29)
- **Signed XML and separate signature files:** XAdES / XML-DSig signatures are validated, enveloped (a signed XML
  file) or detached (your document plus its `.xml` signature, via `signature_url` / `signature_base64`). The output
  says whether your document matches what was signed (`document_match`).
- **Authentic EU law:** `eu_act` (CELEX or ELI) + `language` checks an Official Journal act against the Publications
  Office's qualified e-seal, with the official PDF or your own copy.
- Long-term validation for XML signatures: a certificate that has expired since is validated at the signature
  timestamp, using the revocation data in the signature.
- Revocation and certificate lookups taken from a document only go to public internet addresses.

## 0.1.11 (2026-10-28)
- The EU trusted lists are now rebuilt and republished every Sunday (about 05:30 Amsterdam time); the README
  explains how current they are (`trusted_lists.built_at`) and how revocation is checked.

## 0.1.10 (2026-10-28)
- Apify Console: using batch mode or an upload no longer fails because the prefilled example value is submitted too (an example
  you didn't change is ignored; a value you typed yourself plus a batch is still reported as ambiguous).
- Downloads have a total time limit (a server sending data very slowly can no longer hold a run), and the
  connection itself is checked to be a public address (protection against DNS rebinding).
- Your "maximum charge per run" is checked before each result is delivered, so the run stops cleanly at your limit.

## 0.1.9 (2026-10-28)
- A signer certificate must allow signing (digital signature or non-repudiation key usage); certificates meant only
  for encryption no longer pass as valid signatures.

## 0.1.7 (2026-10-28)
- Contracts signed by several parties: a later signature no longer puts the earlier ones on REVIEW
  (`changed_after_signing: LATER_SIGNATURES`). A document certified as "no changes allowed" still gets BLOCK.
- Large documents: at most 25 signatures are validated per PDF, each PDF has a 3-minute budget, and every result is
  saved as soon as it is ready, so a long run never loses finished results.

## 0.1.6 (2026-10-28)
- Damaged PDFs (corrupt streams, broken structure) now always return "unreadable PDF" instead of failing the run.
- URLs that point to private, local or reserved network addresses are refused, also after a redirect.

## 0.1.5 (2026-10-28)
- Valid signatures and seals whose long-term proof is incomplete are now checked against the certificate's current
  status, so currently valid qualified seals and signatures get ALLOW (e.g. official gazette seals, Belgian eID).

## 0.1.4 (2026-10-28)
- Signatures in the legacy SHA-1 format (still used by some official gazettes) are reported with their signer and
  get REVIEW, instead of the PDF being called unsigned.
- Valid seals that use a less common but standard-compliant RSA encoding are no longer reported as invalid.

## 0.1.3 (2026-10-28)
- First public release: PDF signature and seal validation against the EU trusted lists, with signer, qualified
  status, trust provider, changes after signing, and an ALLOW / REVIEW / BLOCK verdict.
