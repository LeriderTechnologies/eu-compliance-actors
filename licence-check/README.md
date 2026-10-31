# EU Financial Licence & Warning List Check

> **Open source (Apache-2.0):** [read the code on GitHub](https://github.com/LeriderTechnologies/eu-compliance-actors/tree/main/licence-check): see exactly what this Actor does with your data, run the tests, or host it yourself.

Before you pay, deposit with, invest through or onboard a financial firm or crypto platform, ask one question:
**is it authorised in the EU, and is it on a regulator's warning list?**

This Actor checks a firm by name, LEI, registration number and/or website against the EU-level public registers
(ESMA, EBA, ECB, the MiCA register) and national warning lists (France, Belgium, the Netherlands, plus the EU-wide
MiCA list of non-compliant crypto firms). It returns a verdict, **ALLOW**, **REVIEW** or **BLOCK**, with the reasons,
the matching register entries (what the firm is authorised for, since when, withdrawn when, where it is passported),
and for every source the date of the data used.

It is built for KYB and onboarding checks, payment and treasury agents, and AI agents that move money. It does not
replace a compliance officer. **Not legal advice**, not investment advice: see "Limits" below.

## What you get per firm

| Field | Meaning |
|---|---|
| `verdict` | `ALLOW` (active authorisation found, nothing against it), `REVIEW` (a human checks first), `BLOCK` (warning list or EU sanctions hit) |
| `status` | `licensed`, `licensed_and_warned`, `warned`, `sanctioned`, `licence_withdrawn`, `registered_offer` (only an AMF biens-divers offer registration matched, see Coverage), `possible_match`, `not_found`, `not_covered` |
| `reasons` | One line per rule that fired |
| `licences` | Up to 5 register entries: register, authority, type, status, `authorised_since`, `withdrawn_on`, `passported_to`, services, match method and confidence, history |
| `warnings`, `sanctions` | Up to 5 warning-list / EU-sanctions entries that match, with date and match confidence |
| `lookalike_domains` | Warning-list websites that look like yours (one character off, or same name on another extension). Shown, not counted as a match |
| `claimed_licence` | Whether the licence the firm claims is `covered` by our sources and `found` (active) |
| `coverage` | Per source: checked / stale / unavailable, and `data_as_of` |
| `history` | When our daily records first saw the entry and what changed since |
| `lei` | LEI status from GLEIF (see below); `null` when no LEI is known for the firm and none was searched for. If you search by KVK number and none is on file, `lei` is an object with `found: false` |
| `attribution`, `disclaimer` | Source acknowledgements required by the publishers, and the limits |

`not_found` and `not_covered` are different answers:

- **not_found**: we searched every source that covers this kind of firm and found nothing. For a firm that claims a
  bank, payment, e-money, investment or MiCA licence in the EU, that is a red flag.
- **not_covered**: the licence the firm claims (for example insurance, consumer credit or a purely national licence)
  or its country is not in our sources. We cannot say anything about it; check the national register.

## Input

```json
{"name": "Mollie B.V.", "country": "NL",
 "claimed_licence": "e-money institution NL", "as_of": "2026-10-28"}
```

Give at least one of `name`, `lei`, `reg_number`, `domain`. Batch: `{"entities": [{...}, {...}]}` (up to 500).

- `claimed_licence`: free text ("payment institution NL", "MiCA CASP", "bank") or `{"type": "emi", "country": "NL"}`.
  Types: `bank`, `payment_institution`, `emi`, `aisp`, `pisp`, `investment_firm`, `aifm`, `ucits_manco`,
  `crowdfunding`, `trading_venue`, `casp`, `emt_issuer`, `art_issuer`, `sgp` (French portfolio management company),
  `biens_divers` (French registered tangible-asset offer). For a French firm, `aifm` and `ucits_manco` are also
  checked against the AMF list of portfolio management companies.
- `as_of`: the status on a past date, from the registers' own authorisation and withdrawal dates where they publish
  them, otherwise from our recorded history. Before our history starts for a register without dates, the answer is
  `unknown` (never a guess).
- `refresh`: `auto` (default) refetches any source older than `max_age_hours` (default 48) from the official file at
  the start of the run; `never` uses the bundled data only; `always` refetches everything.
- `resolve_lei` (default on): looks up the LEI's legal name at GLEIF, so registers that list the firm without its LEI
  still match.

## LEI status (GLEIF)

When the firm has an LEI, the result has a `lei` section. The LEI is the one you gave, else the one registered with
the Dutch KVK number you gave (`kvk`, or an 8-digit `reg_number` with `country` NL), else the one on the matched
register entry (`lei_from`: `input`, `kvk`, `register`). It shows:

- `registration_status` (`ISSUED`, `LAPSED`, `RETIRED`, `ANNULLED`, ...), `entity_status`, `next_renewal`,
  `successor_lei`, and `legal_name` per GLEIF with `name_matches` against the name you gave;
- `recent_changes`: renames, status changes, country moves and successor changes in the last 90 days, from our daily
  copy of the GLEIF data (this history starts on 2026-10-30 and grows every day), plus `renamed_on` and `lapsed_on`;
- `lei_source`: `snapshot` (bundled GLEIF data, rebuilt daily, date in `data_as_of`), `gleif_api` (an LEI not in the
  bundle, looked up live at GLEIF), `not_in_snapshot` (not bundled and `resolve_lei` off) or `unavailable`.

The bundle holds every LEI that appears in our registers, every LEI registered with the Dutch KVK, and every EEA LEI
that changed in the last 90 days.

What it does to the verdict: a **LAPSED** LEI (or a retired or annulled one) on a firm for which you give a
`claimed_licence` makes the verdict `REVIEW` (reason `lei_lapsed` or `lei_inactive`). It never makes a `BLOCK` on its
own: about a third of all LEIs worldwide are lapsed, and a lapsed LEI means the yearly renewal of the LEI data was
missed, not that a licence was lost. Without a claimed licence, a lapsed LEI, a recent rename or a name that differs
from GLEIF's is shown in `notes` only.

If you give both a `name` and an `lei`, and the name does not resemble any name known for that LEI (GLEIF's legal
name and other names, names from our rename history, the names of register entries found through that LEI;
similarity below 0.5), the verdict is `REVIEW` with reason `lei_name_mismatch`: the LEI may belong to another firm, so
a licence found through it is not taken as yours. Renamed firms are covered by the history and other names; a firm
known only by a translated or local-language name may still get this REVIEW.

## Example output (shortened)

```json
{"verdict": "BLOCK", "status": "warned",
 "reasons": ["On a warning list: AFM (NL) lists '24era' (exact_name, 2024-07-02)",
             "On a warning list: FSMA (BE) lists '24era.com' (domain, 2024-08-29)"],
 "warnings": [{"source": "afm_warnings", "name": "24era", "date": "2024-07-02",
               "match": {"method": "exact_name", "confidence": 0.95}, "data_as_of": "2026-10-28T07:21:11+00:00"}]}
```

## How matching works

| Method | Confidence |
|---|---|
| LEI or registration number equal | 1.00 |
| Same name after normalising case, accents, punctuation, legal form spelling | 0.95 |
| Same name without the legal form (B.V., GmbH, Ltd, S.A., UAB...) | 0.90 |
| Similar name (token-sorted similarity of 0.88 or more) | 0.60 to 0.85 |
| Same website domain | 0.95 |
| Name matches but the firm's home country differs from yours (and it is not passported there) | minus 0.10 |

Only matches of 0.90 or more can lead to ALLOW or BLOCK. Weaker matches lead to REVIEW.

Rules worth knowing:

- **Clones and impersonation.** Fraudsters copy the names of real banks and brokers, and warning lists then carry
  that name too. When a licensed firm's name also appears on a warning list, the result is REVIEW unless you give the
  domain you deal with and the register confirms it. If your domain is the one on the warning list, it is BLOCK.
- A REVIEW is also given when only an AMF biens-divers offer registration matches, when a licence is withdrawn, when only a similar name matches, when the domain you give
  differs from the domain in the register, when a source could not be read, or when a warning list is more than
  7 days old. ALLOW is never given while a warning list is missing.
- **Domain confirmation needs the register to publish a domain.** The MiCA registers and the AMF PSAN list carry a
  website for almost every entry (the AMF portfolio-manager list for about 60% of firms, and the biens-divers list for
  its offers), so a domain you give is actively checked against them. The ECB bank list and the
  EBA payment/e-money register currently publish none, and the ESMA MiFID register has one for only a small share of
  firms. When you give a domain that the register cannot confirm, the verdict is **REVIEW** with the reason
  `domain_unconfirmed` (a real firm's name with a made-up domain is exactly what impersonators send). The domain is
  still checked against warning lists and sanctions. If you only want the licence status, leave `domain` out; to
  confirm a bank's or payment/e-money institution's website, use the regulator's own register page.
- Warning lists can name natural persons. We return only the entries that match what you send, and there is no
  search or bulk export. E-mail addresses on the AMF list are reduced to their domain.

## Coverage

Licence registers (who is authorised):

| Source | Covers | Countries | Dates in the source |
|---|---|---|---|
| ESMA register of investment firms (MiFID II) | Investment firms, AIFMs, UCITS management companies, crowdfunding providers, trading venues, branches | EU + NO, IS, LI | Authorisation and withdrawal dates |
| EBA register of payment and e-money institutions | Payment institutions, e-money institutions, account information providers, exempted institutions, branches, firms notified as outside PSD2 scope | EU + NO, IS, LI | Authorisation and withdrawal dates |
| ECB list of monetary financial institutions | Credit institutions (banks) and their branches, money market funds | EU-27 (not NO, IS, LI) | None: we record entries and exits from 2026-10-28 |
| ESMA interim MiCA register | Authorised crypto-asset service providers (CASPs), e-money and asset-referenced token issuers | EU + NO, IS, LI | Authorisation date |
| AMF list of PSAN | French digital-asset providers under the pre-MiCA regime (most registrations ended in 2026) | FR | Registration and end dates |
| AMF list of authorised portfolio management companies (SGP) | French asset managers with an AMF agrément, with their activities (UCITS, AIF management, discretionary mandates, advice) and instruments | FR | Authorisation date; the list holds only firms authorised on the day, so exits show in our history from 2026-10-30 |
| AMF whitelist of biens-divers offers | Registered offers to invest in tangible assets with a return promise (wine, forests, livestock, precious metals). This is the registration of an **offer**, not a licence of the firm, and the AMF stresses it is not an incentive to invest: these products remain very risky | FR | Registration date |

Warning lists and sanctions:

| Source | Covers | Countries |
|---|---|---|
| ESMA interim MiCA register, non-compliant entities | Crypto services offered without MiCA authorisation, reported by every national authority | EU + NO, IS, LI |
| AMF listes noires | Unauthorised forex, binary options, crypto derivatives, miscellaneous goods schemes, identity theft | FR |
| FSMA list of companies operating unlawfully | Fraudulent platforms, clones, unauthorised offers | BE |
| AFM warning list | Firms the AFM warns against | NL |
| EU consolidated financial sanctions list | Legal entities under EU asset freezes (no natural persons) | EU |

What the AMF lists say is what we say: a result reads "listed by AMF as an authorised portfolio management company"
or "listed by AMF as a registered biens-divers offer", never more. A biens-divers match gives `status`
`registered_offer` (not `licensed`), verdict **REVIEW** (never ALLOW on an offer registration alone) and a note repeating the AMF's risk warning. A French firm missing from these lists
is not proof that it is unlicensed: the lists lag AMF decisions, and firms authorised elsewhere in the EEA can operate
in France under a passport (see the ESMA and EBA registers in the same result).

Per country, this means:

| Country | Banks | Payment / e-money | Investment firms | Crypto (MiCA) | National warning list |
|---|---|---|---|---|---|
| FR | yes | yes | yes, plus AMF portfolio management companies and biens-divers offers | yes, plus PSAN | yes (AMF) |
| BE | yes | yes | yes | yes | yes (FSMA) |
| NL | yes | yes | yes | yes | yes (AFM) |
| Other EU-27 | yes | yes | yes | yes | only the EU-wide MiCA non-compliant list |
| NO, IS, LI | no | yes | yes | yes | only the EU-wide MiCA non-compliant list |
| UK, CH, rest of world | no | no | no | no | no |

Not covered yet: IOSCO I-SCAN, BaFin (DE), CONSOB (IT) and CNMV (ES) warnings, the UK FCA, insurance companies and
intermediaries, and purely national licences (consumer credit, mortgage advice, trust offices, financial service
providers registered only nationally). The `not_covered` field in every result lists them.

## How up to date is the data?

| What | How often | Where you see it |
|---|---|---|
| Our copy of each register and warning list | Read from the official file **every day** (ESMA MiFID and EBA at 10:25, ECB MFI list and AFM / FSMA / AMF lists at 10:40, MiCA registers and EU sanctions at 20:05, Amsterdam time) | `history` in each result |
| The data bundled in this Actor | **Rebuilt and republished every day at about 11:15 Amsterdam time** from the latest daily copies, after the full test suite passes | `coverage[].data_as_of` per source, in every result |
| Live refresh during your run | `refresh: auto` (default): any source older than `max_age_hours` (48 h) is fetched live from the official file at the start of your run. `always`: every source. `never`: bundled data only | the same `data_as_of` (then the time of your run) |

So with the defaults, an answer is never based on a source copy older than 48 hours, and normally on one from the
same or the previous day. `data_as_of` is when we last read the official file; each authority updates its own
register on its own schedule, so a decision made today may appear in its file later.

**History.** On top of the registers' own dates we keep every change we see, so answers can say "first seen on",
"left the list on" or "first warned on". That history starts on 2026-10-26/28 (per source, shown in each result as
`our_history_starts`) and grows every day.

## Pricing

Pay per event: one `licence-check` event per firm checked. Items rejected for bad input are not charged. A check
where a source was down still returns a verdict (never ALLOW in that case) and is charged.

## Limits

- Indicative only. **Not legal advice**, not investment or compliance advice. Registers can lag the authority's
  decisions by days or weeks; confirm on the authority's own register before you rely on a result.
- A firm missing from a list is not proof it is safe: warning lists only contain firms an authority has noticed.
- Names are matched, not identities. Without an LEI, registration number or domain, two firms with the same name can
  be confused; the confidence score shows how the match was made.
- The ECB list is a statistical list of monetary financial institutions; it is the EU-wide public list of credit
  institutions, but a national banking register is authoritative.

## Sources and licences

- ESMA, Register of investment firms (MiFID II) and interim MiCA register: reproduction authorised, source acknowledged.
- European Banking Authority, Register of payment and electronic money institutions: reproduction authorised, source acknowledged.
- European Central Bank, list of monetary financial institutions: reproduction permitted, source acknowledged.
- AMF (France), listes noires, liste blanche des PSAN, liste des sociétés de gestion de portefeuille agréées and liste
  blanche des offres en biens divers, via data.gouv.fr: Licence Ouverte / Open Licence 2.0 (source and date of last
  update: `attribution` and `data_as_of` in each result).
- FSMA (Belgium), list of companies operating unlawfully in Belgium: CC BY 4.0.
- Autoriteit Financiële Markten (AFM), waarschuwingslijst: reuse with explicit attribution to the AFM.
- European Commission, EU Financial Sanctions Files: reuse authorised, source acknowledged.
- GLEIF LEI records (LEI look-up and the bundled LEI status data): CC0.

Every result carries the attribution lines for the sources it used.
