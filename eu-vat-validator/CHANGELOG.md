# Changelog

## 0.1.5 (2026-10-28)
- Apify Console: using batch mode no longer fails because the prefilled example value is submitted too (an example
  you didn't change is ignored; a value you typed yourself plus a batch is still reported as ambiguous).
- Your "maximum charge per run" is checked before each result is delivered, so the run stops cleanly at your limit.

## 0.1.3 (2026-10-28)
- When a member state's VIES service is down, a batch now finishes in about a minute instead of retrying every
  number; numbers from other countries are still checked, and unanswered ones stay free.

## 0.1.2 (2026-10-28)
- VAT numbers pasted with full-width digits or invisible characters are checked as the real number; digits from
  other scripts are rejected before VIES is asked (not charged).
- More robust handling of unusual VIES answers.

## 0.1.1 (2026-10-28)
- First public release: live EU VIES check with registered name, offline format pre-check, optional name match,
  batches of up to 200, pay only when VIES answers.
