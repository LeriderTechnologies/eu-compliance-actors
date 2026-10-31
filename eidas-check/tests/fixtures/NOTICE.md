These PDFs are copied unchanged from the European Commission's DSS project test resources
(https://github.com/esig/dss, dss-pades/src/test/resources/validation), licensed LGPL-2.1.
They are used only by the test suite and are not part of the Actor image.

`boe-legacy-sha1-signed.pdf` is BOE-A-2026-22453 from the Spanish Boletín Oficial del Estado (www.boe.es, 2026-10-27),
reused under the BOE's open-data reuse terms with attribution. It carries a legacy adbe.pkcs7.sha1 signature.

`at-bgbl-digestinfo-no-null.pdf` is BGBl. I Nr. 1/2024 (authentic version) from the Austrian legal information system
RIS (www.ris.bka.gv.at), open government data under CC BY 4.0. Its seal (Bundeskanzleramt, A-Trust a-sign-corporate-07)
uses an RSA PKCS#1 v1.5 DigestInfo without NULL parameters (RFC 8017 allows it; see eidas_check/compat.py).

`de-bgbl-2026-qualified-seal.pdf` is BGBl. 2026 I Nr. 250 from www.recht.bund.de (German Federal Law Gazette; an
official work, not subject to copyright under § 5 UrhG). Qualified e-seal of the Bundesamt für Justiz via D-Trust's
remote seal service; seal certificate valid until 2028-02-25.
