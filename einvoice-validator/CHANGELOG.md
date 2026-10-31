# Changelog

## 0.1.2 (2026-10-29)
- **Security:** a Factur-X/ZUGFeRD PDF that embeds more than one different invoice XML is `BLOCK` (a receiver could
  book a different one than the one validated).
- An XML invoice that mentions "%PDF" near the top is validated as XML; `data:` URLs are accepted for
  `file_base64`; an unusual item (e.g. invalid Unicode) gives an error for that item instead of stopping the run.

## 0.1.1 (2026-10-29)
- First release: UBL 2.1 and CII invoices and credit notes, Factur-X / ZUGFeRD 2.x PDFs; EN 16931 (1.3.17),
  Peppol BIS Billing 3.0 (3.0.21) and XRechnung 3.0 (3.0.2) rules plus the UBL/CII XML schemas.
