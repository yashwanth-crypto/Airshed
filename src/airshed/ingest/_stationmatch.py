"""Shared station-name matching.

Two loaders need to pair a free-text station name from an external source
against our configured stations: the Kaggle historical archive
(`kaggle_history.py`) and the CPCB Advanced Search exports
(`cpcb_manual.py`). Both hit the same failure modes — a name split across
commas, a Roman numeral that distinguishes two real sites, a generic word like
"Sector" that exists in every city — so the matching logic lives once, here,
rather than drifting apart in two copies.
"""

from __future__ import annotations

import re

# Words that name a *kind* of place rather than a place. No identifying power
# on their own: "Sector 5" is a street-address fragment, not a station.
GENERIC = {
    "sector", "nagar", "colony", "road", "marg", "phase", "block", "area",
    "town", "vihar", "puram", "park", "garden", "extension", "crossing",
}
NOISE = {
    "delhi", "new", "ncr", "india", "dpcc", "cpcb", "uppcb", "hspcb", "ppcb",
    "rspcb", "imd", "iitm", "station", "the",
}
ROMAN = {"i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x"}

MIN_SCORE = 0.55


def tokens(name: str) -> set[str]:
    """Identifying words in a station name.

    Strips the operating agency after the final " - " and drops noise words,
    but keeps single-character Roman numerals: "Knowledge Park V" and
    "Knowledge Park III" are different stations 6 km apart, and the numeral is
    the only thing that tells them apart.
    """
    head = name.rsplit(" - ", 1)[0]
    cleaned = re.sub(r"[^a-z0-9 ]+", " ", head.lower())
    return {t for t in cleaned.split() if t not in NOISE and (len(t) > 1 or t in ROMAN)}


def ordinals(toks: set[str]) -> set[str]:
    """Digits and Roman numerals — the part of a name that numbers a site."""
    return {t for t in toks if t.isdigit() or t in ROMAN}


def score(ours: set[str], theirs: set[str]) -> float:
    """Match strength between two token sets, in [0, 1] or -1 if disqualified.

    Digits or numerals present on both sides but disagreeing disqualify the
    pair outright — "Sector 11" against "Sector 51" is a different station,
    not a near miss. Containment (our whole name inside theirs) scores 1.0
    when at least one shared word is a real, non-generic place name, so a
    short name like "Bahadurgarh" can still match "Arya Nagar, Bahadurgarh"
    without letting a bare "Sector 5" attach itself to any Sector 5 anywhere.
    """
    our_num, their_num = ordinals(ours), ordinals(theirs)
    if our_num and their_num and not (our_num & their_num):
        return -1.0
    overlap = ours & theirs
    if not overlap:
        return 0.0
    base = len(overlap) / len(ours | theirs)
    distinctive = {t for t in overlap if t not in GENERIC and len(t) >= 6}
    if overlap == ours and distinctive:
        base = max(base, 1.0)
    return base


def best_match(
    name: str,
    city: str,
    candidates: list[tuple[str, str]],
    used: set[str] | None = None,
) -> tuple[str, float, str] | None:
    """Best (key, score, candidate_name) for `name` among `candidates`.

    `candidates` is a list of (key, candidate_name). `city` is folded into the
    query tokens, because a config entry like "Sector 51" is meaningless
    without knowing which city's Sector 51.
    """
    used = used or set()
    ours = tokens(name) | tokens(city)
    if not ours:
        return None
    best: tuple[str, float, str] | None = None
    for key, candidate_name in candidates:
        if key in used:
            continue
        s = score(ours, tokens(candidate_name))
        if s < 0:
            continue
        if best is None or s > best[1]:
            best = (key, s, candidate_name)
    return best
