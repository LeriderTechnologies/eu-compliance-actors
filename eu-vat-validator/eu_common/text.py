"""Text and identifier normalisation: Unicode folding, look-alike (homoglyph) mapping, company-name matching.

Standard library only."""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

LEGAL_FORMS = {"bv", "nv", "vof", "cv", "ltd", "llc", "inc", "gmbh", "bvba", "sarl", "srl", "sa", "ag", "plc",
               "eenmanszaak", "holding"}


class InputError(ValueError):
    """Bad request: the caller gets a 400 / failed run and is not charged."""


def fold_id(value) -> str:
    """Identifier text as the user meant it: NFKC (full-width -> ASCII) and no invisible format characters
    (zero-width space, BOM, bidi controls), so look-alikes are checked as, and reported as, the real ASCII number."""
    s = unicodedata.normalize("NFKC", value or "")
    return "".join(c for c in s if unicodedata.category(c) != "Cf")


# Cyrillic and Greek letters that look like Latin ones (the usual homoglyph-spoofing set, Unicode confusables subset).
_CONFUSABLES = str.maketrans({
    "а": "a", "в": "b", "е": "e", "к": "k", "м": "m", "н": "h", "о": "o", "р": "p", "с": "c", "т": "t", "у": "y",
    "х": "x", "і": "i", "ј": "j", "ѕ": "s", "ԁ": "d", "ԛ": "q", "ԝ": "w", "ү": "y", "һ": "h", "ӏ": "l",
    "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H", "О": "O", "Р": "P", "С": "C", "Т": "T", "У": "Y",
    "Х": "X", "І": "I", "Ј": "J", "Ѕ": "S", "Ү": "Y", "Һ": "H", "Ӏ": "I",
    "α": "a", "β": "b", "ε": "e", "ι": "i", "κ": "k", "ν": "v", "ο": "o", "ρ": "p", "τ": "t", "υ": "u", "χ": "x",
    "Α": "A", "Β": "B", "Ε": "E", "Ζ": "Z", "Η": "H", "Ι": "I", "Κ": "K", "Μ": "M", "Ν": "N", "Ο": "O", "Ρ": "P",
    "Τ": "T", "Υ": "Y", "Χ": "X",
})


def skeleton(text: str | None) -> str:
    """What a human sees: NFKC, invisible format characters removed, Cyrillic/Greek look-alikes mapped to Latin,
    accents stripped. Used for name matching and for scanning message text, so homoglyphs can't dodge a rule."""
    s = fold_id(text).translate(_CONFUSABLES)
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c))


def normalise_name(s: str | None) -> str:
    """"Jansen Bouw B.V." -> "jansen bouw": no case, diacritics, punctuation or legal form."""
    s = skeleton(s).lower()
    s = re.sub(r"(?<=\b[a-z])\.(?=[a-z]\.)|(?<=\b[a-z]\.[a-z])\.", "", s)  # b.v. -> bv, v.o.f. -> vof
    s = s.replace(".", "")
    words = [w for w in re.split(r"[^a-z0-9]+", s) if w]
    return " ".join(w for w in words if w not in LEGAL_FORMS)


def names_match(a: str | None, b: str | None) -> bool:
    """Fuzzy match that tolerates case, legal forms, small typos and bank-truncated names."""
    x, y = normalise_name(a), normalise_name(b)
    if not x or not y:
        return False
    if x == y or SequenceMatcher(None, x, y).ratio() >= 0.85:
        return True
    tx, ty = set(x.split()), set(y.split())
    if tx <= ty or ty <= tx:
        return True
    short, long_ = sorted((x, y), key=len)
    return len(short) >= 8 and long_.startswith(short)
