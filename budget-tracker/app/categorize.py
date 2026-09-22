"""Turn a raw bank description into a clean merchant name and a category."""

import re
from dataclasses import dataclass

from app.seed_data import CONDITIONAL_ONEOFFS, FIXED, ONEOFF

# A trailing token that contains a digit, e.g. " #1662", " 0356091",
# "*HQ1U88TO3", " X3", " CA2". Separator is a space, '*' or '#'.
_TRAILING_CODE = re.compile(r"[\s*#]+[^\s*#]*\d[^\s*#]*$")
# Digits glued straight onto a word: "AMERICAN AIR0012318120817".
_GLUED_DIGITS = re.compile(r"(?<=[A-Z])\d{4,}$")


def clean(description: str) -> str:
    """Uppercase and collapse runs of spaces ("UBER   *EATS" -> "UBER *EATS")."""
    return " ".join(description.upper().split())


def normalize_merchant(description: str) -> str:
    """Strip reference numbers so the same merchant always looks the same.

    "AMAZON MKTPL*GK3V04R63" -> "AMAZON MKTPL"
    "PUBLIX #1662"           -> "PUBLIX"
    "AMERICAN AIR0012318120817" -> "AMERICAN AIR"
    """
    s = clean(description)
    previous = None
    while s != previous:  # repeat: some descriptions have several codes
        previous = s
        s = _GLUED_DIGITS.sub("", s)
        s = _TRAILING_CODE.sub("", s).strip()
    return s or clean(description)


def _squash(s: str) -> str:
    # Chase spaces things inconsistently around '*' ("UBER *EATS",
    # "UBER* EATS", "UBER   *EATS"), so we drop spaces next to '*'. We keep
    # the other spaces: removing them all would make "JUAN VALDEZ ARP INTERN"
    # contain "ARPIN" (the movers).
    return re.sub(r"\s*\*\s*", "*", clean(s))


@dataclass
class Match:
    category: str | None
    is_oneoff: bool
    is_fixed: bool


def categorize(description: str, amount: float, merchant_map: dict[str, str]) -> Match:
    """merchant_map is {PATTERN: category}, loaded from the merchant_map table."""
    haystack = _squash(description)
    hits = [p for p in merchant_map if _squash(p) in haystack]
    if not hits:
        return Match(None, False, False)

    best = max(hits, key=len)  # most specific pattern wins
    category = merchant_map[best]

    is_oneoff = category == ONEOFF or any(
        _squash(p) in haystack and amount > limit
        for p, limit in CONDITIONAL_ONEOFFS.items()
    )
    return Match(category, is_oneoff, category == FIXED)
