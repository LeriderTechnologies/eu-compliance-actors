"""Name, domain, LEI and country normalisation plus similarity scores (stdlib only).

Matching rules (documented in STORE_README.md):
  * LEI or national registration number equal            -> confidence 1.00 (method "lei" / "reg_number")
  * normalised full name equal                           -> 0.95 ("exact_name")
  * equal after dropping legal forms (B.V., GmbH, Ltd...) -> 0.90 ("core_name")
  * token-sorted similarity >= 0.88                      -> 0.60-0.85 ("fuzzy_name"; never enough for BLOCK/ALLOW alone)
  * registered domain equal                              -> 0.95 ("domain")
  * domain one edit away / same label, other TLD          -> "lookalike" (reported, never counted as a match)
"""
from __future__ import annotations

import difflib
import re
import unicodedata

# Legal-form tokens removed for the "core" name. Multi-word forms are joined before tokenising.
LEGAL_FORMS = {
    "bv", "nv", "vof", "cv", "gmbh", "ag", "kg", "ohg", "ug", "se", "sa", "sas", "sasu", "sarl", "eurl", "sca", "scs",
    "srl", "spa", "sapa", "scarl", "sl", "slu", "sau", "ltd", "limited", "plc", "llc", "llp", "lp", "inc", "corp",
    "corporation", "company", "co", "uab", "ab", "as", "asa", "aps", "oy", "oyj", "ou", "sia", "ad", "ead", "ood",
    "eood", "doo", "dd", "kft", "zrt", "nyrt", "rt", "sro", "as", "spzoo", "sp", "zoo", "ehf", "hf", "bvba", "sprl",
    "cvba", "scrl", "ltda", "lda", "ee", "ike", "ae", "epe", "mbh", "eg", "ev", "kgaa", "ccp", "ptyltd", "pty",
    "group", "holding", "holdings", "the",
}
_MULTI = [
    (r"\bsp\.?\s*z\s*o\.?\s*o\.?", " spzoo "), (r"\bs\.?\s*r\.?\s*o\.?\b", " sro "), (r"\bs\.?\s*a\.?\s*s\.?\b", " sas "),
    (r"\bb\.?\s*v\.?\b", " bv "), (r"\bn\.?\s*v\.?\b", " nv "), (r"\bs\.?\s*a\.?\b", " sa "),
    (r"\bs\.?\s*r\.?\s*l\.?\b", " srl "), (r"\bs\.?\s*p\.?\s*a\.?\b", " spa "), (r"\bs\.?\s*l\.?\b", " sl "),
    (r"\bd\.?\s*o\.?\s*o\.?\b", " doo "), (r"\bpty\.?\s*ltd\.?\b", " ptyltd "), (r"\bl\.?\s*t\.?\s*d\.?\b", " ltd "),
]
_TAGS = re.compile(r"\((?:[^)]*)\)")  # "(clone of ...)" and similar remarks are not part of the name


# Cyrillic and Greek letters that look like Latin ones (Unicode confusables subset used for homoglyph spoofing).
_CONFUSABLES = str.maketrans({
    "а": "a", "в": "b", "е": "e", "к": "k", "м": "m", "н": "h", "о": "o", "р": "p", "с": "c", "т": "t", "у": "y",
    "х": "x", "і": "i", "ј": "j", "ѕ": "s", "ԁ": "d", "ԛ": "q", "ԝ": "w", "ү": "y", "һ": "h", "ӏ": "l",
    "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H", "О": "O", "Р": "P", "С": "C", "Т": "T", "У": "Y",
    "Х": "X", "І": "I", "Ј": "J", "Ѕ": "S", "Ү": "Y", "Һ": "H", "Ӏ": "I",
    "α": "a", "β": "b", "ε": "e", "ι": "i", "κ": "k", "ν": "v", "ο": "o", "ρ": "p", "τ": "t", "υ": "u", "χ": "x",
    "Α": "A", "Β": "B", "Ε": "E", "Ζ": "Z", "Η": "H", "Ι": "I", "Κ": "K", "Μ": "M", "Ν": "N", "Ο": "O", "Ρ": "P",
    "Τ": "T", "Υ": "Y", "Χ": "X",
})


def skeleton(s: str | None) -> str:
    """What a reader sees: NFKC (full-width -> ASCII), invisible format characters dropped, Cyrillic/Greek
    look-alikes mapped to Latin. Applied to register names and queries alike, so homoglyphs can't dodge a match."""
    s = unicodedata.normalize("NFKC", s or "")
    return "".join(c for c in s if unicodedata.category(c) != "Cf").translate(_CONFUSABLES)


def mixed_script(s: str | None) -> bool:
    """True when a name mixes Latin letters with Cyrillic or Greek ones (look-alike spoofing, e.g. 'Mоllie')."""
    scripts = set()
    for c in unicodedata.normalize("NFKC", s or ""):
        if c.isalpha():
            nm = unicodedata.name(c, "")
            scripts.add("latin" if "LATIN" in nm else "cyrillic" if "CYRILLIC" in nm else "greek" if "GREEK" in nm else "other")
    return "latin" in scripts and bool(scripts & {"cyrillic", "greek"})


def fold(s: str) -> str:
    """Lower case, strip diacritics, map ß/ø/æ/ł (on the homoglyph skeleton)."""
    s = skeleton(s).lower().translate(str.maketrans({"ß": "ss", "ø": "o", "æ": "ae", "ł": "l", "đ": "d", "œ": "oe"}))
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c))


def norm_name(s: str) -> str:
    """Full normalised name: folded, legal forms canonicalised, punctuation removed."""
    s = _TAGS.sub(" ", fold(s))
    s = s.replace("&", " and ")
    for pat, rep in _MULTI:
        s = re.sub(pat, rep, s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def core_name(s: str) -> str:
    """Name without legal-form tokens ("Mollie B.V." -> "mollie")."""
    toks = [t for t in norm_name(s).split() if t not in LEGAL_FORMS]
    return " ".join(toks) if toks else norm_name(s)


def tokens(s: str) -> list[str]:
    return [t for t in core_name(s).split() if len(t) > 1]


def name_similarity(a: str, b: str) -> float:
    """Token-sort similarity of the core names, 0..1."""
    x, y = " ".join(sorted(core_name(a).split())), " ".join(sorted(core_name(b).split()))
    if not x or not y:
        return 0.0
    return difflib.SequenceMatcher(None, x, y).ratio()


# ---------------------------------------------------------------------------------------------------------- domains
_MULTI_TLD = {"co.uk", "org.uk", "com.au", "co.za", "com.cy", "com.mt", "com.pl", "co.nz", "com.br", "co.il", "com.tr",
              "com.cn", "com.hk", "co.jp", "com.sg", "ltd.uk", "net.pl", "com.ua", "co.in", "com.es", "com.gr"}
_URL = re.compile(r"(?i)\b(?:https?://)?(?:[a-z0-9-]+@)?((?:[a-z0-9¡-￿-]+\.)+[a-z¡-￿]{2,})(?::\d+)?(?:/\S*)?")


def norm_domain(s: str) -> str | None:
    """'https://www.Example.COM/path' -> 'example.com'; e-mail -> its domain; IDN -> punycode. None if no domain."""
    if not s:
        return None
    s = s.strip().lower()
    s = re.sub(r"^https?\.//", "", s)  # e.g. 'https.//coinbase.com' typo in the ESMA file
    s = re.sub(r"^[a-z]+://", "", s)
    if "@" in s:
        s = s.rsplit("@", 1)[1]
    s = s.split("/")[0].split("?")[0].split("#")[0].split(":")[0].strip(" .,;")
    if s.startswith("www.") or re.match(r"^www\d\.", s):
        s = s.split(".", 1)[1]
    if "." not in s or " " in s:
        return None
    try:
        s = s.encode("idna").decode("ascii")
    except UnicodeError:
        pass
    if not re.fullmatch(r"[a-z0-9.-]+\.[a-z0-9-]{2,}", s):
        return None
    return s


def registered_domain(d: str) -> str:
    """Last two labels (three for co.uk-style suffixes): 'platform.apex-trade.app' -> 'apex-trade.app'."""
    parts = d.split(".")
    if len(parts) >= 3 and ".".join(parts[-2:]) in _MULTI_TLD:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def domains_in(text: str) -> list[str]:
    """All distinct domains in a free-text field (websites lists, warning titles)."""
    out = []
    for m in _URL.finditer(text or ""):
        d = norm_domain(m.group(0))
        if d and d not in out and not re.fullmatch(r"[\d.]+", d):
            out.append(d)
    return out


def edit_distance_le1(a: str, b: str) -> bool:
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        diff = [i for i in range(len(a)) if a[i] != b[i]]
        return len(diff) == 1 or (len(diff) == 2 and diff[1] == diff[0] + 1 and a[diff[0]] == b[diff[1]]
                                  and a[diff[1]] == b[diff[0]])
    if len(a) > len(b):
        a, b = b, a
    for i in range(len(b)):
        if b[:i] + b[i + 1:] == a:
            return True
    return False


def domain_relation(query: str, listed: str) -> str | None:
    """'domain' (same registered domain), 'lookalike' (one edit away or same label with another TLD), else None."""
    q, l = registered_domain(query), registered_domain(listed)
    if q == l:
        return "domain"
    ql, ll = q.split(".")[0], l.split(".")[0]
    if len(ql) >= 5 and (ql == ll or edit_distance_le1(q, l)):
        return "lookalike"
    return None


# ---------------------------------------------------------------------------------------------------------- ids
_LEI = re.compile(r"^[A-Z0-9]{18}[0-9]{2}$")


def norm_lei(s: str) -> str | None:
    s = unicodedata.normalize("NFKC", s or "")
    s = re.sub(r"\s+", "", "".join(c for c in s if unicodedata.category(c) != "Cf")).upper()
    return s if _LEI.match(s) and s.isascii() and lei_checksum_ok(s) else None


def lei_checksum_ok(lei: str) -> bool:
    """ISO 17442 (ISO 7064 MOD 97-10)."""
    try:
        return int("".join(str(int(c, 36)) for c in lei)) % 97 == 1
    except ValueError:
        return False


def norm_id(s: str) -> str:
    """Registration numbers: upper case, only letters and digits ('LB000484 ' -> 'LB000484')."""
    return re.sub(r"[^A-Z0-9]", "", fold(s or "").upper())


# ---------------------------------------------------------------------------------------------------------- countries
EEA = ["AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "ES", "FI", "FR", "GR", "HR", "HU", "IE", "IT", "LT", "LU", "LV",
       "MT", "NL", "PL", "PT", "RO", "SE", "SI", "SK", "IS", "LI", "NO"]
COUNTRY_NAMES = {
    "austria": "AT", "belgium": "BE", "bulgaria": "BG", "cyprus": "CY", "czech republic": "CZ", "czechia": "CZ",
    "germany": "DE", "denmark": "DK", "estonia": "EE", "spain": "ES", "finland": "FI", "france": "FR", "greece": "GR",
    "croatia": "HR", "hungary": "HU", "ireland": "IE", "italy": "IT", "lithuania": "LT", "luxembourg": "LU",
    "latvia": "LV", "malta": "MT", "netherlands": "NL", "the netherlands": "NL", "poland": "PL", "portugal": "PT",
    "romania": "RO", "sweden": "SE", "slovenia": "SI", "slovakia": "SK", "slovak republic": "SK", "iceland": "IS",
    "liechtenstein": "LI", "norway": "NO", "united kingdom": "GB", "switzerland": "CH",
}


def norm_country(s: str) -> str | None:
    """'NETHERLANDS' / 'nl' / 'EL' (ESMA's Greece) -> ISO alpha-2; None if unknown."""
    if not s:
        return None
    t = fold(s).strip().strip(".")
    if len(t) == 2:
        t = t.upper()
        return {"EL": "GR", "UK": "GB"}.get(t, t)
    return COUNTRY_NAMES.get(t)


def iso_date(s: str) -> str | None:
    """'25/06/2025', '2025-06-25', '2025-06-25T00:00:00Z', '19-10-2026' -> '2025-06-25'."""
    if not s:
        return None
    s = s.strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return "-".join(m.groups())
    m = re.match(r"^(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})$", s)
    if m:
        d, mo, y = m.groups()
        return f"{y}-{int(mo):02d}-{int(d):02d}"
    return None
