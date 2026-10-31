# Changelog

## 0.1.5 (2026-10-28)
- Apify Console: using batch mode no longer fails because the prefilled example value is submitted too (an example
  you didn't change is ignored; a value you typed yourself plus a batch is still reported as ambiguous).
- Your "maximum charge per run" is checked before each result is delivered, so the run stops cleanly at your limit.

## 0.1.3 (2026-10-28)
- When the EU EOS service is down, a batch now finishes in about a minute instead of retrying every group of
  numbers (unanswered numbers stay free).

## 0.1.2 (2026-10-28)
- EORI numbers pasted with full-width digits or invisible characters are checked as the real number.

## 0.1.1 (2026-10-28)
- First public release: live EU EOS check with trader name and address where published, batches of up to 500,
  GB numbers flagged as not checkable, pay only when EOS answers.
