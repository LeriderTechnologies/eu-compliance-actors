"""EU VAT id formats and invoice VAT arithmetic: offline, standard library only.

Indicative checks, not tax advice. Rates are only checked for Dutch suppliers (the three Dutch rates); other
countries' rates are reported as not checked rather than guessed.
"""
from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from .text import InputError, fold_id, skeleton

# Dutch VAT rates: 21 % (general), 9 % (reduced), 0 % (zero rate). Source: Belastingdienst, "Btw-tarieven"
# https://www.belastingdienst.nl/wps/wcm/connect/bldcontentnl/belastingdienst/zakelijk/btw/tarieven_en_vrijstellingen/
# (verified 2026-10-26). Verify before each release.
NL_VAT_RATES = (Decimal("0"), Decimal("9"), Decimal("21"))

# VIES member-state codes (Greece is EL, Northern Ireland goods trade is XI) and the shape of the number after the
# prefix. Used only as an offline pre-check; VIES itself is the authority when it answers.
VAT_FORMATS = {
    "AT": r"U\d{8}", "BE": r"[01]\d{9}", "BG": r"\d{9,10}", "CY": r"\d{8}[A-Z]", "CZ": r"\d{8,10}", "DE": r"\d{9}",
    "DK": r"\d{8}", "EE": r"\d{9}", "EL": r"\d{9}", "ES": r"[A-Z0-9]\d{7}[A-Z0-9]", "FI": r"\d{8}",
    "FR": r"[A-HJ-NP-Z0-9]{2}\d{9}", "HR": r"\d{11}", "HU": r"\d{8}", "IE": r"\d{7}[A-W][A-I]?|\d[A-Z+*]\d{5}[A-W]",
    "IT": r"\d{11}", "LT": r"\d{9}|\d{12}", "LU": r"\d{8}", "LV": r"\d{11}", "MT": r"\d{8}", "NL": r"\d{9}B\d{2}",
    "PL": r"\d{10}", "PT": r"\d{9}", "RO": r"[1-9]\d{1,9}", "SE": r"\d{10}01", "SI": r"\d{8}", "SK": r"\d{10}",
    "XI": r"\d{9}|\d{12}|GD\d{3}|HA\d{3}",
}
VIES_COUNTRIES = frozenset(VAT_FORMATS)

# Wording that tells the buyer it owes the VAT (reverse charge, art. 196 VAT Directive) or that the supply is an
# exempt intra-Community supply of goods (art. 138). Several EU languages; matched case-insensitively.
REVERSE_CHARGE_RE = re.compile(
    r"(?i)(btw[ -]?verlegd|verlegde btw|verleggingsregeling|omgekeerde heffing|reverse[ -]?charge|"
    r"vat (?:is )?reverse[ -]?charged|art(?:icle|\.|ikel)?\s*196|art(?:icle|\.|ikel)?\s*138|"
    r"intra[ -]?community (?:supply|delivery)|intracommunautaire (?:levering|dienst)|"
    r"autoliquidation|steuerschuldnerschaft des leistungsempf[aä]ngers|reverse-charge-verfahren|"
    r"inversi[oó]n del sujeto pasivo|inversione contabile|autoliquida[cç][aã]o|odwrotne obci[aą][zż]enie|"
    r"omv[aä]nd (?:skattskyldighet|betalningsskyldighet)|omvendt betalingspligt|k[aä][aä]nteinen verovelvollisuus|"
    r"tax exempt intra|vrijgestelde intracommunautaire)")

CENT = Decimal("0.01")


def normalise_vat_id(value: str | None) -> str:
    s = re.sub(r"[\s.\-]+", "", fold_id(value)).upper()
    return "EL" + s[2:] if s.startswith("GR") else s


def split_vat_id(vat_id: str) -> tuple[str, str]:
    return vat_id[:2], vat_id[2:]


def vat_format_ok(vat_id: str) -> bool | None:
    """True/False for a VIES country, None for a prefix VIES does not cover (e.g. GB, CH, NO)."""
    cc, num = split_vat_id(vat_id)
    pattern = VAT_FORMATS.get(cc)
    if pattern is None:
        return None
    return bool(re.fullmatch(pattern, num, re.ASCII))


def nl_vat_checksum_ok(vat_id: str) -> bool | None:
    """Dutch btw-id check: the classic mod-11 test on the 9 digits (legal entities), or, for the btw-id issued to
    sole traders since 2020, mod-97 over the whole id with letters as numbers (like an IBAN). None if not NL-shaped."""
    if not re.fullmatch(r"NL\d{9}B\d{2}", vat_id):
        return None
    d = [int(c) for c in vat_id[2:11]]
    s = sum(w * x for w, x in zip(range(9, 1, -1), d[:8])) - d[8]
    if s % 11 == 0:
        return True
    digits = "".join(str(int(c, 36)) for c in vat_id)
    return int(digits) % 97 == 1


def to_amount(value, field: str, *, allow_negative: bool = True) -> Decimal:
    """Accept a JSON number or a decimal string; at most 2 decimals (floats within float noise of a cent)."""
    if isinstance(value, bool) or value is None:
        raise InputError(f"{field} must be a number or a decimal string")
    try:
        d = Decimal(str(value).strip().replace(",", ".")) if isinstance(value, str) else Decimal(repr(value))
    except (InvalidOperation, AttributeError, ValueError):
        raise InputError(f"{field} is not a number: {value!r}") from None
    if not d.is_finite():
        raise InputError(f"{field} is not a finite number")
    if abs(d) >= Decimal("1e12"):
        raise InputError(f"{field} is too large")
    q = d.quantize(CENT, rounding=ROUND_HALF_UP)
    if abs(q - d) > Decimal("0.000001"):
        raise InputError(f"{field} has more than 2 decimals: {value!r}")
    if not allow_negative and q < 0:
        raise InputError(f"{field} must not be negative")
    if abs(q) >= Decimal("1e12"):
        raise InputError(f"{field} is too large")
    return q


def to_rate(value, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise InputError(f"{field} must be a percentage number, e.g. 21")
    try:
        d = Decimal(str(value).strip()) if isinstance(value, str) else Decimal(repr(value))
    except (InvalidOperation, ValueError):
        raise InputError(f"{field} is not a number: {value!r}") from None
    if not d.is_finite() or d < 0 or d > 100:
        raise InputError(f"{field} must be a percentage between 0 and 100")
    d = d.normalize()
    return d.quantize(Decimal(1)) if d.as_tuple().exponent > 0 else d  # 2E+1 -> 20


def _vat(net: Decimal, rate: Decimal) -> Decimal:
    return (net * rate / 100).quantize(CENT, rounding=ROUND_HALF_UP)


def _s(d: Decimal | None) -> str | None:
    return None if d is None else str(d.quantize(CENT))


def parse_invoice(inv: dict) -> dict:
    """Validate the invoice block's types (InputError on a malformed request); return parsed Decimals."""
    if not isinstance(inv, dict):
        raise InputError("invoice must be a JSON object")
    lines_raw = inv.get("lines") or []
    if not isinstance(lines_raw, list) or len(lines_raw) > 1000:
        raise InputError("invoice.lines must be a list of at most 1,000 {net, vat_rate} objects")
    lines = []
    for i, ln in enumerate(lines_raw):
        if not isinstance(ln, dict) or "net" not in ln or "vat_rate" not in ln:
            raise InputError(f"invoice.lines[{i}] must be an object with net and vat_rate")
        lines.append({"net": to_amount(ln["net"], f"invoice.lines[{i}].net"),
                      "vat_rate": to_rate(ln["vat_rate"], f"invoice.lines[{i}].vat_rate"),
                      "vat": to_amount(ln["vat"], f"invoice.lines[{i}].vat") if ln.get("vat") is not None else None})
    totals = {k: to_amount(inv[k], f"invoice.{k}") if inv.get(k) is not None else None
              for k in ("total_net", "total_vat", "total_gross")}
    text = inv.get("text") or ""
    if not isinstance(text, str) or len(text) > 50_000:
        raise InputError("invoice.text must be a string of at most 50,000 characters")
    buyer = inv.get("buyer_vat_id")
    if buyer is not None and not isinstance(buyer, str):
        raise InputError("invoice.buyer_vat_id must be a string")
    return {"lines": lines, **totals, "text": text, "buyer_vat_id": normalise_vat_id(buyer) if buyer else None}


def vat_math(inv: dict, supplier_cc: str | None) -> tuple[dict, list[tuple[str, str, str]]]:
    """Arithmetic and reverse-charge checks on a parsed invoice. Returns (result, findings), where each finding is
    (rule, severity, detail) for a check that failed."""
    findings: list[tuple[str, str, str]] = []
    lines = inv["lines"]
    tn, tv, tg = inv["total_net"], inv["total_vat"], inv["total_gross"]
    res: dict = {"ok": True, "rates_checked": None, "computed": {}, "issues": []}

    def issue(rule: str, detail: str, severity: str = "warn") -> None:
        findings.append((rule, severity, detail))
        res["issues"].append(detail)

    if lines:
        bad_lines = [i for i, ln in enumerate(lines) if ln["vat"] is not None and ln["vat"] != _vat(ln["net"], ln["vat_rate"])]
        for i in bad_lines[:5]:
            ln = lines[i]
            issue("M1 VAT arithmetic", f"line {i}: VAT {_s(ln['vat'])} on net {_s(ln['net'])} at {ln['vat_rate']} % "
                                       f"should be {_s(_vat(ln['net'], ln['vat_rate']))}")
        sum_net = sum((ln["net"] for ln in lines), Decimal("0"))
        by_line = sum((_vat(ln["net"], ln["vat_rate"]) for ln in lines), Decimal("0"))
        groups: dict[Decimal, Decimal] = {}
        for ln in lines:
            groups[ln["vat_rate"]] = groups.get(ln["vat_rate"], Decimal("0")) + ln["net"]
        by_rate = sum((_vat(n, r) for r, n in groups.items()), Decimal("0"))
        res["computed"] = {"total_net": _s(sum_net), "total_vat_line_rounding": _s(by_line),
                           "total_vat_total_rounding": _s(by_rate)}
        if tn is not None and tn != sum_net:
            issue("M1 VAT arithmetic", f"lines add up to net {_s(sum_net)}, total_net says {_s(tn)}")
        if tv is not None and tv not in (by_line, by_rate):
            issue("M1 VAT arithmetic", f"total_vat {_s(tv)} matches neither rounding per line ({_s(by_line)}) "
                                       f"nor per rate on totals ({_s(by_rate)})")
        net_for_gross = tn if tn is not None else sum_net
        vat_for_gross = tv if tv is not None else by_rate
        res["computed"]["total_gross"] = _s(net_for_gross + vat_for_gross)
        if supplier_cc == "NL":
            res["rates_checked"] = "NL"
            odd = sorted({ln["vat_rate"] for ln in lines if ln["vat_rate"] not in NL_VAT_RATES})
            if odd:
                issue("M2 Dutch VAT rate", f"rate(s) {', '.join(f'{r} %' for r in odd)} are not a Dutch VAT rate "
                                           "(21, 9 or 0 %)")
        charged = any(ln["vat_rate"] > 0 for ln in lines) or (tv is not None and tv != 0)
    else:
        if tn is not None and tv is not None and tn != 0:
            res["computed"] = {"implied_vat_rate": str((tv / tn * 100).quantize(Decimal("0.01")))}
        charged = tv is not None and tv != 0
        net_for_gross, vat_for_gross = tn, tv
    if tg is not None and net_for_gross is not None and vat_for_gross is not None \
            and tg != net_for_gross + vat_for_gross:
        issue("M1 VAT arithmetic", f"total_gross {_s(tg)} is not net {_s(net_for_gross)} + VAT {_s(vat_for_gross)} "
                                   f"= {_s(net_for_gross + vat_for_gross)}")

    # Reverse charge: a B2B supply between two different EU member states (both VAT ids given).
    buyer_cc = inv["buyer_vat_id"][:2] if inv["buyer_vat_id"] else None
    if supplier_cc in VIES_COUNTRIES and buyer_cc in VIES_COUNTRIES:
        expected = supplier_cc != buyer_cc
    else:
        expected = None
    found = bool(REVERSE_CHARGE_RE.search(skeleton(inv["text"]))) if inv["text"].strip() else None
    res["reverse_charge_expected"] = expected
    res["reverse_charge_text_found"] = found
    res["vat_charged"] = charged
    if expected:
        if charged:
            issue("M3 cross-border B2B VAT", f"{supplier_cc} supplier to {buyer_cc} business buyer with VAT charged: "
                                             "usually 0 % with reverse charge (services) or as an intra-Community "
                                             "supply (goods); some supplies are exceptions")
        if found is False:
            issue("M4 reverse-charge wording", f"{supplier_cc} to {buyer_cc} B2B: no 'btw verlegd' / reverse-charge "
                                               "or intra-Community supply wording found in the invoice text")
    res["ok"] = not res["issues"]
    return res, findings
