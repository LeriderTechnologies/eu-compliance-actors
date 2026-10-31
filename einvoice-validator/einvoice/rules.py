"""Official validation artefacts, pinned by version and SHA-256, baked into the Docker image at build time.

  EN 16931 (CEN/TC 434 rules, maintained by the European Commission)  ConnectingEurope/eInvoicing-EN16931  EUPL-1.2
  XRechnung 3.0 (KoSIT) + UBL 2.1 / CII D16B XML schemas              itplr-kosit/validator-configuration-xrechnung  Apache-2.0
  Peppol BIS Billing 3.0 (OpenPeppol)                                  OpenPEPPOL/peppol-bis-invoice-3  MPL-2.0
  SchXslt (Schematron -> XSLT compiler, for the Peppol rules)          name.dmaus.schxslt  MIT

`python -m einvoice.rules build [DIR]` downloads, verifies and unpacks them, compiles the Peppol Schematron to XSLT,
and writes DIR/manifest.json. Nothing is fetched at run time.
"""
from __future__ import annotations

import hashlib
import io
import json
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

RULES = Path(__file__).resolve().parent / "rules_data"
ARTEFACTS = {
    "en16931_ubl": ("1.3.17", "https://github.com/ConnectingEurope/eInvoicing-EN16931/releases/download/"
                    "validation-1.3.17/en16931-ubl-1.3.17.zip",
                    "bf02a0e3f6cbe692a4a28e70be7bdcaded5612b4169b543e13d6cb4f33c29e92"),
    "en16931_cii": ("1.3.17", "https://github.com/ConnectingEurope/eInvoicing-EN16931/releases/download/"
                    "validation-1.3.17/en16931-cii-1.3.17.zip",
                    "e5f3235824b6b38454b1d54cf3f14e01efb5b33971fbefc4e64997d0ef619e89"),
    "kosit": ("XRechnung 3.0.2 / config 2026-08-31", "https://github.com/itplr-kosit/validator-configuration-xrechnung/"
              "releases/download/v2026-08-31/xrechnung-3.0.2-validator-configuration-2026-08-31.zip",
              "2530cd107c414511c5d0462ec10f886910395abfca820db82e83d70bf01221a8"),
    "peppol": ("3.0.21", "https://github.com/OpenPEPPOL/peppol-bis-invoice-3/archive/refs/tags/3.0.21.zip",
               "3196ca37e9281ffd33c60682583ebb1e279177f29af64cf68544bd03c77fcfdb"),
    "schxslt": ("1.10.1", "https://repo1.maven.org/maven2/name/dmaus/schxslt/schxslt/1.10.1/schxslt-1.10.1.jar",
                "4f4f21edab7b37f96ad59ae12a344d3510f1092ac46b6d81a4efa0120b73cb58"),
}
# ruleset id -> file inside RULES
XSLT = {"en16931-ubl": "en16931-ubl.xslt", "en16931-cii": "en16931-cii.xslt", "peppol-ubl": "peppol-ubl.xslt",
        "peppol-cii": "peppol-cii.xslt", "xrechnung-ubl": "xrechnung-ubl.xsl", "xrechnung-cii": "xrechnung-cii.xsl"}
XSD = {"ubl-invoice": "xsd/ubl/maindoc/UBL-Invoice-2.1.xsd", "ubl-creditnote": "xsd/ubl/maindoc/UBL-CreditNote-2.1.xsd",
       "cii": "xsd/cii/CrossIndustryInvoice_100pD16B.xsd"}


def _fetch(name: str, cache: Path | None) -> bytes:
    version, url, sha = ARTEFACTS[name]
    path = cache / f"{name}.bin" if cache else None
    if path and path.exists():
        data = path.read_bytes()
    else:
        req = urllib.request.Request(url, headers={"User-Agent": "lerider-einvoice-build/0.1 (+https://lerider.com)"})
        with urllib.request.urlopen(req, timeout=120) as r:
            data = r.read()
        if path:
            path.write_bytes(data)
    if hashlib.sha256(data).hexdigest() != sha:
        raise SystemExit(f"{name}: SHA-256 mismatch, refusing to build ({url})")
    return data


def _extract(z: zipfile.ZipFile, member_suffix: str, dest: Path) -> None:
    hits = [n for n in z.namelist() if n.endswith(member_suffix)]
    if len(hits) != 1:
        raise SystemExit(f"expected one {member_suffix} in archive, found {hits}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(z.read(hits[0]))


def _extract_dir(z: zipfile.ZipFile, prefix: str, dest: Path) -> int:
    n = 0
    for name in z.namelist():
        if name.startswith(prefix) and not name.endswith("/"):
            out = dest / name[len(prefix):]
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(z.read(name))
            n += 1
    return n


def _compile_schematron(sch: Path, schxslt_dir: Path, out: Path) -> None:
    from saxonche import PySaxonProcessor
    with PySaxonProcessor(license=False) as proc:
        x = proc.new_xslt30_processor()
        pipeline = x.compile_stylesheet(stylesheet_file=str(schxslt_dir / "xslt/2.0/pipeline-for-svrl.xsl"))
        pipeline.set_cwd(str(sch.parent))
        result = pipeline.transform_to_string(source_file=str(sch))
    if not result or "<xsl:stylesheet" not in result and "<xsl:transform" not in result:
        raise SystemExit(f"Schematron compilation of {sch.name} produced no stylesheet")
    out.write_text(result, encoding="utf-8")


def build(dest: Path = RULES, cache: Path | None = None) -> dict:
    tmp = dest.with_name(dest.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    with zipfile.ZipFile(io.BytesIO(_fetch("en16931_ubl", cache))) as z:
        _extract(z, "xslt/EN16931-UBL-validation.xslt", tmp / XSLT["en16931-ubl"])
    with zipfile.ZipFile(io.BytesIO(_fetch("en16931_cii", cache))) as z:
        _extract(z, "xslt/EN16931-CII-validation.xslt", tmp / XSLT["en16931-cii"])
    with zipfile.ZipFile(io.BytesIO(_fetch("kosit", cache))) as z:
        _extract(z, "xrechnung/3.0.2/xsl/XRechnung-UBL-validation.xsl", tmp / XSLT["xrechnung-ubl"])
        _extract(z, "xrechnung/3.0.2/xsl/XRechnung-CII-validation.xsl", tmp / XSLT["xrechnung-cii"])
        if _extract_dir(z, "resources/ubl/2.1/xsd/", tmp / "xsd/ubl") < 10 or \
                _extract_dir(z, "resources/cii/16b/xsd/", tmp / "xsd/cii") < 4:
            raise SystemExit("XML schemas missing from the KoSIT configuration")
    sx = tmp / "_schxslt"
    with zipfile.ZipFile(io.BytesIO(_fetch("schxslt", cache))) as z:
        _extract_dir(z, "xslt/", sx / "xslt")
    with zipfile.ZipFile(io.BytesIO(_fetch("peppol", cache))) as z:
        sch = tmp / "_peppol"
        _extract_dir(z, "peppol-bis-invoice-3-3.0.21/rules/sch/", sch)
    _compile_schematron(sch / "PEPPOL-EN16931-UBL.sch", sx, tmp / XSLT["peppol-ubl"])
    _compile_schematron(sch / "PEPPOL-EN16931-CII.sch", sx, tmp / XSLT["peppol-cii"])
    shutil.rmtree(sx)
    shutil.rmtree(sch)
    for f in list(XSLT.values()) + list(XSD.values()):
        if not (tmp / f).exists():
            raise SystemExit(f"rules build incomplete: {f} missing")
    manifest = {"en16931": ARTEFACTS["en16931_ubl"][0], "xrechnung": ARTEFACTS["kosit"][0],
                "peppol_bis_billing": ARTEFACTS["peppol"][0], "ubl": "2.1", "cii": "D16B"}
    (tmp / "manifest.json").write_text(json.dumps(manifest, indent=1))
    shutil.rmtree(dest, ignore_errors=True)
    tmp.rename(dest)
    return manifest


def manifest(rules: Path = RULES) -> dict:
    return json.loads((rules / "manifest.json").read_text())


if __name__ == "__main__":
    if sys.argv[1:2] != ["build"]:
        sys.exit("usage: python -m einvoice.rules build [DEST] [--cache DIR]")
    args = sys.argv[2:]
    cache = None
    if "--cache" in args:
        i = args.index("--cache")
        cache = Path(args[i + 1])
        cache.mkdir(parents=True, exist_ok=True)
        del args[i:i + 2]
    print(json.dumps(build(Path(args[0]) if args else RULES, cache), indent=1))
