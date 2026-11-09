# Changelog

Routine data-only rebuilds (scheduled, see the README) are not listed here.

## 0.1.30 (2026-11-09)
- Fix: more "broker" wordings were checked as a MiFID investment firm (same cause as "insurance broker" in 0.1.29):
  - Mortgage and credit intermediaries ("mortgage broker", "credit broker", "loan broker", "hypotheekadviseur",
    "hypotheekbemiddelaar", "Immobiliardarlehensvermittler", "Kreditvermittler", "courtier en crédit immobilier",
    "intermédiaire en crédit") now read as `consumer_credit`, the type already used for "mortgage" wordings. These are
    national registrations (EU mortgage / consumer credit directives), which the Actor does not cover: `not_covered`.
  - Estate agents ("real estate broker", "property broker", "makelaar", "Immobilienmakler", "agent immobilier") now
    read as `other` (no financial licence, `not_covered`).
- Fix: "beursmakelaar" (Dutch stockbroker) now reads as `investment_firm`; it read as `other` before.
- Fix: a claim that ends in "bank" ("private wealth management bank", "wealth management bank")
  reads as `bank` again; since 0.1.29 the wealth wording won and it was checked as an investment firm.

## 0.1.29 (2026-11-09)
- More claim wordings map to the right licence type (they came back `not_covered` or as the wrong type before):
  - `investment_firm`: "vermogensbeheerder" / "vermogensbeheer" (NL), "Vermögensverwalter" /
    "Finanzportfolioverwaltung" (DE), "wealth manager" / "wealth management firm" / "wealth management company"
    (the last one was read as a UCITS management company). "Wealth manager", "vermogensbeheerder" and
    "Vermögensverwalter" are soft wordings like "asset manager": a firm that holds only an AIFM / UCITS ManCo
    authorisation may stay `ALLOW`, with `matched_as` shown.
  - `ucits_manco`: "OGAW-Verwaltungsgesellschaft" / "OGAW-KVG" (DE), "beheerder (van een) icbe" (NL),
    "gestionnaire d'OPCVM" (FR). "Société de gestion (d'OPCVM)" is unchanged (`sgp`, the AMF list).
  - Insurance distribution ("insurance broker", "insurance agent/intermediary", "verzekeringstussenpersoon",
    "assurantietussenpersoon", "Versicherungsmakler/-vermittler", "courtier en assurances") now reads as
    `insurance_intermediary`, and "assureur", "compagnie d'assurance", "réassurance" as `insurance`: both are
    `not_covered`. Fix: "insurance broker" used to be checked as a MiFID investment firm (because of "broker").
  - "Property management company", "vastgoedbeheer", "Hausverwaltung", "gestion immobilière": building and
    real-estate management is not a financial licence (`other`, `not_covered`); "property management company"
    was read as a UCITS management company. "Real estate fund manager" stays `fund_manager`.
- New note `lei_pending_transfer`: a matched LEI (yours, or a register entry's own LEI) that is `PENDING_ARCHIVAL` or
  `PENDING_TRANSFER` at GLEIF is being moved to another LEI issuer and stays valid. The output now says so in
  `notes`; the verdict is unchanged (before, the unfamiliar status was shown in `lei_status` without explanation).

## 0.1.28 (2026-11-08)
- Fix: a claim worded "asset management company" (e.g. "asset management
  company NL") misparsed to `ucits_manco` because the bare `"management company"` wording rule matched before the
  `"asset manag..."` wording rule that should catch it. It now correctly parses to `investment_firm`, per the
  Actor's own type mapping for asset managers ("asset manager" alone was already correct). Fixed by reordering the
  two free-text rules (`_CLAIM_WORDS` in `licence_check/check.py`); no other wording is affected -- a 531-phrase
  sweep of every claim-wording literal in the tests/README/CHANGELOG found exactly this one change, nothing else.
  "UCITS management company", "fund management company" and a bare "management company" are unaffected (still
  `ucits_manco`/`fund_manager`/`ucits_manco` respectively).

## 0.1.27 (2026-11-08)
- New claim type `fund_manager`: a claim worded "fund manager", "fund management company", "investment fund
  manager", "ManCo", "fondsbeheerder" / "beheerder van beleggingsinstellingen", "Fondsmanager" /
  "Kapitalverwaltungsgesellschaft" / "KVG" or "gestionnaire de fonds" is now checked against the fund-manager
  registers (ESMA AIFM and UCITS management company entries; for France also the AMF list of portfolio management
  companies) and is satisfied by either authorisation. Previously "fund manager" came back `not_covered`, and
  "fund management company" was checked as a UCITS management company only (an AIFM-only firm read as not found).
- Precise wordings are unchanged: "AIFM" / "AIF manager" / "alternative investment fund manager" stay `aifm`,
  anything with "UCITS" (and a bare "management company") stays `ucits_manco`, "asset manager" stays
  `investment_firm`. The word "fund" on its own ("investment fund", "hedge fund", "fund administrator") is not a
  licence claim and still comes back `not_covered`.
- "AIFMD" (e.g. "AIFMD manager") is now read as `aifm`; it came back `not_covered` before.
- A claim that names a fund manager together with another type ("investment firm / fund manager", "fund manager or
  broker") is checked as `fund_manager`: a firm holding only a MiFID investment-firm licence now gets `REVIEW`
  (fund-manager licence not found) instead of `ALLOW`. Claim `investment_firm` on its own to check that licence.

## 0.1.26 (2026-11-08)
- Fix: a withdrawn strict licence entry is no longer hidden behind a soft-worded
  claim's ALLOW. When the claim's own strict licence type is found but WITHDRAWN (not merely absent) and an
  active sibling authorisation also exists, the verdict is now REVIEW even for soft wording ("asset manager NL"),
  with a reason naming the strict entry's status and withdrawal date and the sibling type it is active under
  (e.g. "its MiFID investment firm entry is withdrawn, withdrawn 2020-06-15; active only as AIFM"). Real: 15 firms
  in the live index, e.g. Providence Capital N.V. (NL), previously ALLOW on "asset manager". The same logic
  applies to `payment_institution` (a withdrawn Payment-institution entry behind an active EMI, e.g. Mollie B.V.).
  A firm with no strict entry at all and only a withdrawn sibling entry now also gets `matched_as` filled in
  (previously silent "found but not active"/not-found).
- Removed "fund manager" from the claim-wording examples in STORE_README and the CHANGELOG (that wording parses
  to claim type `other` -> `not_covered`, never reaches the `investment_firm` path, so it never kept ALLOW).

## 0.1.25 (2026-11-08)
- Fix: a generic `investment_firm` claim ("investment firm", "MiFID", "broker", "dealer",
  "beleggingsonderneming", "wertpapier"...) that only matches a firm's AIFM or UCITS management company entry no
  longer comes back ALLOW as if the firm held the precise MiFID licence it claimed. It now comes back `found`, with
  a new field `matched_as` (the licence type actually held, e.g. `["AIFM"]`), a plain note, and verdict `REVIEW`
  with the reason "found only as AIFM, not as a MiFID investment firm". A firm that really holds an active MiFID
  "Investment firm" entry is unaffected (ALLOW, as before 0.1.24); claiming `aifm`/`ucits_manco` directly is also
  unaffected. A claim worded only "asset manager" may still come back ALLOW (matched_as is still shown), since
  that wording does not promise the precise licence type.
- Same fix for a generic `payment_institution` claim: a firm that only holds an active electronic money
  institution (EMI/EEMI) authorisation — e.g. Mollie B.V., whose Payment-institution entry was withdrawn when its
  EMI entry took over — now comes back `found`/`found_active` via the EMI entry, with `matched_as` and verdict
  `REVIEW` ("found only as Electronic money institution, not as a payment institution"), never ALLOW. Claiming
  `emi` directly is unaffected.
- `matched_as` is absent unless this sibling-type fallback applies (null/absent-safe in the dataset schema).
- Fixed a wrong sentence in STORE_README that equated a fund manager with a MiFID investment firm.

## 0.1.24 (2026-11-08)
- Fix: a claimed "investment firm" no longer comes back not-found for a firm that is really an AIFM or a UCITS
  management company (both authorised in the same ESMA MiFID register, under their own entry type). About a third
  of that register (an estimated 4,500+ active entries) was affected, e.g. Seawind Capital Partners B.V. (NL, active
  AIFM since 2014) and many well-known fund managers (BlackRock Luxembourg, Robeco, Amundi, Candriam entities...).
  Claiming `aifm` or `ucits_manco` directly still checks that type only, unaffected.

## 0.1.23 (2026-11-07)
- Fix: a branch found only through its parent firm's LEI no longer turns the verdict to REVIEW because of the branch's
  own lapsed or retired LEI (affected 22 of about 9,900 checks by LEI, e.g. large banks with many branches). The same
  holds for a register entry whose LEI was retired into the firm you asked for, and for entries beyond the 5 shown in
  `licences`. A register entry found by name that carries an old lapsed or duplicate LEI still gives REVIEW.
- Clearer wording: "the register entry 'X' carries LEI Y, which is LAPSED at GLEIF".

## 0.1.22 (2026-11-07)
- Each matched register entry with an LEI now shows `lei_status` (the GLEIF registration status of that LEI:
  ISSUED, LAPSED, RETIRED ...) and `successor_lei` when GLEIF names one.
- A lapsed or retired LEI on an authorised firm we matched now gives REVIEW with a plain reason, also when you do
  not give a `claimed_licence` (before, it was a note). It never gives BLOCK on its own: a lapsed LEI means the yearly
  LEI renewal was missed, not that the licence was withdrawn. A retired LEI with a successor usually means a merger.

## 0.1.11 (2026-11-06)
- New warning source: **IOSCO I-SCAN**, about 48,000 investor warnings from 100+ securities regulators worldwide
  (BaFin, CNMV, CONSOB, Central Bank of Ireland, CSSF, FCA, FINMA, ASIC, SEC, CFTC and more), updated daily. Matches on
  website, e-mail domain, LEI, exact name or legal core name; each warning names the authority, its date and a link to
  its notice. Impersonators of licensed firms count as clones (REVIEW, not BLOCK, when your domain is the licensed
  firm's).
- Social networks, app stores, free website builders and free e-mail providers listed in a warning are never used as
  match keys. Fixes a bug where any check with a free e-mail address returned BLOCK, because the AMF
  blacklist lists scammers' addresses on those providers.
- A warning that lists a domain the register confirms for an authorised firm now gives REVIEW with status
  `licensed_and_warned` instead of BLOCK (old warnings from before a licence, one-country warnings on passported
  firms).
- Domains under second-level registries (e.g. `.com.mx`, `.it.com`, `.co.th`) are now compared as whole names.

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
