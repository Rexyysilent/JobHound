"""Human-text normalization (v3.5 item 1) — run BEFORE anything interprets text.

Provider feeds carry real mojibake: the 2026-07-22 digest surfaced
"AI Training Contributor â€“ Remote", which is a UTF-8 en dash (E2 80 93) that
was decoded as cp1252 somewhere upstream. Repairing that only in the digest
string is not enough — region tagging, pay parsing, the eligibility gates,
relevance matching and the dedupe key all read the same fields, so the canonical
`Job` itself must hold the repaired text.

Two forms are exposed:

* `normalize_text` — the DISPLAY form. Legitimate Unicode punctuation survives
  (HumaniTapp's "$120-$170/hr" keeps its real en dash); only mojibake,
  zero-width junk, control characters and repeated whitespace are touched.
* `normalize_match` — the MATCHING/PARSING form. Same repairs, plus hyphen /
  en dash / em dash folded to ASCII "-" so one pattern covers every dash a
  provider might emit. Never store this back on the Job as display text.

Both are idempotent: normalizing an already-normalized string is a no-op.
"""
from __future__ import annotations

import html as html_lib
import re
import unicodedata

from ftfy import fix_text

# Codepoints below are spelled as hex ints, not literals: the characters are
# invisible or trivially confused, and a character class full of them is
# unreviewable in a diff.

# Zero-width / formatting characters that carry no display meaning.
# U+200C ZWNJ and U+200D ZWJ are deliberately absent: Bengali and Devanagari
# use them to control conjunct forms, and Bengali work is this agent's primary
# lane — stripping them would corrupt real titles.
_ZERO_WIDTH = dict.fromkeys((
    0x200B,                       # zero-width space
    0x2060,                       # word joiner
    0xFEFF,                       # zero-width no-break space / BOM
    0x00AD,                       # soft hyphen
))

# Unicode space separators, folded to a plain space.
_SPACE_CODEPOINTS = (
    0x00A0,                       # no-break space
    0x1680,                       # ogham space mark
    *range(0x2000, 0x200B),       # en quad .. hair space
    0x202F,                       # narrow no-break space
    0x205F,                       # medium mathematical space
    0x3000,                       # ideographic space
)
_UNICODE_SPACE = re.compile("[" + "".join(map(chr, _SPACE_CODEPOINTS)) + "]")

_HORIZONTAL_WS = re.compile(r"[ \t]+")
_BLANK_LINES = re.compile(r"\n{3,}")

# HTML feeds often encode section boundaries only in markup. Converting every
# tag to a space destroys headings and list-item boundaries.
_SCRIPT_STYLE = re.compile(r"(?is)<(?:script|style)\b[^>]*>.*?</(?:script|style)\s*>")
_HTML_COMMENT = re.compile(r"(?s)<!--.*?-->")
_HTML_BREAK = re.compile(r"(?i)<br\s*/?>")
_HTML_LIST_ITEM = re.compile(r"(?i)<li\b[^>]*>")
_HTML_BLOCK_OPEN = re.compile(
    r"(?i)<(?:address|article|aside|blockquote|dd|div|dl|dt|fieldset|figcaption|"
    r"figure|footer|form|h[1-6]|header|hr|main|nav|ol|p|pre|section|table|tbody|"
    r"td|tfoot|th|thead|tr|ul)\b[^>]*>"
)
_HTML_BLOCK_CLOSE = re.compile(
    r"(?i)</(?:address|article|aside|blockquote|dd|div|dl|dt|fieldset|figcaption|"
    r"figure|footer|form|h[1-6]|header|main|nav|ol|p|pre|section|table|tbody|td|"
    r"tfoot|th|thead|tr|ul)\s*>"
)
_HTML_TAG = re.compile(r"(?s)<[^>]+>")

# Every dash a provider might emit, folded to ASCII "-" for matching only.
_DASH_CODEPOINTS = (
    0x2010, 0x2011,               # hyphen, non-breaking hyphen
    0x2012, 0x2013, 0x2014,       # figure dash, en dash, em dash
    0x2015,                       # horizontal bar
    0x2212,                       # minus sign
    0x2043,                       # hyphen bullet
)
_DASHES = re.compile("[" + "".join(map(chr, _DASH_CODEPOINTS)) + "]")


def _drop_controls(value: str) -> str:
    """Remove C0/C1 control characters, keeping newline and tab."""
    return "".join(
        ch for ch in value
        if ch in "\n\t" or unicodedata.category(ch) != "Cc"
    )


def normalize_text(value: str | None) -> str | None:
    """Repair and canonicalize a human-readable provider string for display."""
    if value is None:
        return None
    if not value:
        return value
    text = fix_text(value)
    text = unicodedata.normalize("NFC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.translate(_ZERO_WIDTH)
    text = _UNICODE_SPACE.sub(" ", text)
    text = _drop_controls(text)
    text = _HORIZONTAL_WS.sub(" ", text)
    # Trim each line before collapsing blank runs, so "\n \n \n" folds too.
    text = "\n".join(line.strip() for line in text.split("\n"))
    text = _BLANK_LINES.sub("\n\n", text)
    return text.strip()


def fold_dashes(value: str) -> str:
    """Collapse every dash variant to ASCII "-" (matching form only)."""
    return _DASHES.sub("-", value)


def normalize_match(value: str | None) -> str:
    """Normalized + dash-folded form used for matching and parsing."""
    return fold_dashes(normalize_text(value) or "")


def normalize_lines(value: str | None) -> str:
    """Normalize display text while retaining meaningful line boundaries."""
    return normalize_text(value) or ""


def html_to_text(value: str | None) -> str:
    """Convert provider HTML without flattening its semantic structure.

    Block elements become line boundaries and list items receive a stable
    bullet. The result is plain text, so a second call is intentionally a
    no-op.
    """
    if value is None:
        return ""
    decoded = html_lib.unescape(value)
    if "<" not in decoded or ">" not in decoded:
        return normalize_lines(decoded)
    text = _SCRIPT_STYLE.sub("\n", decoded)
    text = _HTML_COMMENT.sub("\n", text)
    text = _HTML_BREAK.sub("\n", text)
    text = _HTML_LIST_ITEM.sub("\n• ", text)
    text = _HTML_BLOCK_OPEN.sub("\n", text)
    text = _HTML_BLOCK_CLOSE.sub("\n", text)
    text = _HTML_TAG.sub("", text)
    return normalize_lines(text)
