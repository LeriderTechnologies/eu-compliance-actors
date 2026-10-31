# Changelog

## 0.1.5 (2026-10-28)
- Apify Console: using batch mode no longer fails because the prefilled example value is submitted too (an example
  you didn't change is ignored; a value you typed yourself plus a batch is still reported as ambiguous).
- Your "maximum charge per run" is checked before each result is delivered, so the run stops cleanly at your limit.

## 0.1.3 (2026-10-28)
- When the EU TIN service is down for a country, a batch now finishes in about a minute instead of retrying every
  TIN; other countries are still checked, and unanswered TINs stay free.

## 0.1.2 (2026-10-28)
- One malformed item in a batch no longer fails the whole run: it is returned as an error item (not charged).
- TINs pasted with full-width digits or invisible characters are checked as the real number.

## 0.1.1 (2026-10-28)
- First public release: EU TIN check (structure and check digit) for all 27 member states, batches of up to 200,
  pay only when the EU service answers.
