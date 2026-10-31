# Changelog

Routine data-only rebuilds (scheduled, see the README) are not listed here.

## 0.1.10 (2026-10-30)
- A match on an AMF biens-divers offer alone now returns REVIEW, not ALLOW: an offer registration is not a licence of the firm.
- Two new French sources from the AMF: the list of authorised portfolio management companies (sociétés de gestion
  de portefeuille, about 670 firms, with their activities) and the whitelist of registered biens-divers offers
  (wine, forests, livestock and other tangible-asset investments).
- New `claimed_licence` types `sgp` and `biens_divers`; for French firms, `aifm` and `ucits_manco` claims are also
  checked against the AMF list.
- New `status` value `registered_offer`: only a registered biens-divers offer matched. Results say "listed by AMF as
  ..." and repeat the AMF's warning that such investments remain very risky.
- French firms can be found by AMF number (e.g. GP-08000008), SIREN or SIRET.
- New `lei` section per firm: LEI registration status (ISSUED / LAPSED / RETIRED ...), legal name per GLEIF and
  whether it matches the name you gave, successor LEI, next renewal date, and renames or lapses in the last 90 days.
- Dutch firms can be checked by KVK number (`kvk`, or an 8-digit `reg_number` with country NL): we find the LEI
  registered with that number and, through it, the firm's register entries.
- A lapsed, retired or annulled LEI on a firm with a `claimed_licence` gives REVIEW (never BLOCK on its own).
- A `name` that does not resemble any known name of the `lei` you gave gives REVIEW (`lei_name_mismatch`), so a
  licence found through another firm's LEI no longer gives ALLOW.

## 0.1.7 (2026-10-28)
- The bundled register data is now rebuilt and republished every day (about 11:15 Amsterdam time); the README
  explains how current each answer is and where `data_as_of` shows it.

## 0.1.6 (2026-10-28)
- Live register refreshes use about 16x less memory (the EBA register is now read entity by entity), so
  refreshing runs are faster and far from the memory limit.
- Your "maximum charge per run" is checked before each result is delivered, so the run stops cleanly at your limit.

## 0.1.5 (2026-10-28)
- `as_of` in the future is rejected (the registers can't say who will be licensed then).
- `claimed_licence` objects with non-text values return a clear per-item error instead of failing the run.

## 0.1.3 (2026-10-28)
- Firm names on warning lists are now matched even when written with look-alike letters or invisible characters.
- A domain that imitates a warned domain with look-alike letters is reported as a look-alike.
- A name that mixes Latin with Cyrillic or Greek look-alike letters ("Mоllie") gets REVIEW as possible
  impersonation, even if it matches a licensed firm.
- Names longer than 200 characters are rejected per item.

## 0.1.2 (2026-10-28)
- One malformed entity in a batch no longer fails the whole run: it is returned as an error item (not charged).
- LEIs pasted with full-width digits or invisible characters are read correctly.

## 0.1.1 (2026-10-28)
- First public release: EU licence and warning-list check (ECB, EBA, ESMA MiFID, MiCA, AFM, FSMA, AMF, EU sanctions)
  with ALLOW / REVIEW / BLOCK. A domain the register cannot confirm gives REVIEW, not ALLOW.
