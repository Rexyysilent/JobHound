"""jobhound/filters/matching.py

Phrase matching done right.

The bug this replaces: rapidfuzz partial_ratio("prompt engineer", "QA Engineer")
scores high because ONE shared token dominates. Result: a 2-word boost keyword
awarded its full points for a job that matched half of it.

New contract:
  phrase_in_text(phrase, text) is True iff EVERY token of the phrase appears
  (whole-word, typo-tolerant) in `text`, in order, within a tight window.
  Single-token phrases match whole words only, never substrings.
"""
from __future__ import annotations

import re
from functools import lru_cache

from rapidfuzz import fuzz

_TOKEN_RE = re.compile(r"[a-z0-9+#]+")   # keeps c++, c#, k8s-ish tokens intact
_TOKEN_SIM_THRESHOLD = 88                 # per-token typo tolerance ("promt"~"prompt")
_SHORT_TOKEN_EXACT = 4                    # tokens this short must match exactly (qa, ai, sql)


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _token_match(want: str, have: str) -> bool:
    if len(want) <= _SHORT_TOKEN_EXACT or len(have) <= _SHORT_TOKEN_EXACT:
        return want == have  # 'ai' must never match 'air'
    # stemming-lite: engineer↔engineering, annotate↔annotation
    if (have.startswith(want) or want.startswith(have)) \
            and abs(len(want) - len(have)) <= 4:
        return True
    return fuzz.ratio(want, have) >= _TOKEN_SIM_THRESHOLD


@lru_cache(maxsize=4096)
def _phrase_tokens(phrase: str) -> tuple[str, ...]:
    return tuple(_tokens(phrase))


def phrase_in_text(phrase: str, text: str, *, window_slack: int = 2) -> bool:
    """True iff all phrase tokens appear in order within a tight window.

    window_slack: extra tokens allowed between first and last match beyond the
    phrase's own length ("prompt engineering and design" still matches
    "prompt engineering"; "prompt ... 40 words ... engineer" does not).
    """
    want = _phrase_tokens(phrase)
    if not want:
        return False
    have = _tokens(text)
    if len(want) == 1:
        return any(_token_match(want[0], h) for h in have)

    max_span = len(want) + window_slack
    # find ordered occurrence with bounded span
    for start in range(len(have)):
        if not _token_match(want[0], have[start]):
            continue
        pos = start
        ok = True
        for w in want[1:]:
            nxt = None
            for j in range(pos + 1, min(start + max_span, len(have))):
                if _token_match(w, have[j]):
                    nxt = j
                    break
            if nxt is None:
                ok = False
                break
            pos = nxt
        if ok:
            return True
    return False


def matched_keywords(keywords: list[str], text: str) -> list[str]:
    """All keywords (phrases) that legitimately occur in text."""
    return [kw for kw in keywords if phrase_in_text(kw, text)]
