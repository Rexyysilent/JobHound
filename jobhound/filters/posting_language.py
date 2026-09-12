"""Detect the language of a posting and reject unsupported languages.

Patch #1 catches language names in titles.  This gate catches the language
the posting itself is written in, without adding a third-party dependency.
"""
from __future__ import annotations

import re


# German labour-market gender markers are categorical signals.
_DE_MARKERS = re.compile(
    r"\((?:m/w/d|w/m/d|m/f/d|gn|m/w/x|all genders)\)", re.IGNORECASE
)

# Distinctive stopwords only; shared English words are deliberately excluded.
_STOPWORDS: dict[str, set[str]] = {
    "german": {"und", "für", "mit", "der", "die", "das", "wir", "sie",
               "eine", "nicht", "werden", "bei", "aus", "zum", "über"},
    "french": {"et", "les", "des", "une", "vous", "nous", "dans", "pour",
               "avec", "sur", "est", "être", "aux", "notre"},
    "spanish": {"los", "las", "una", "para", "con", "por", "del", "está",
                "nuestro", "trabajo", "empresa", "buscamos", "años"},
    "italian": {"di", "il", "della", "per", "con", "una", "sono", "nel",
                "lavoro", "azienda", "siamo", "anni"},
    "portuguese": {"os", "uma", "para", "com", "não", "você", "nosso",
                   "trabalho", "empresa", "anos", "estamos", "são"},
    "dutch": {"het", "een", "van", "voor", "met", "wij", "niet", "werk",
              "bij", "onze", "jaar", "wordt"},
    "polish": {"nie", "jest", "oraz", "pracy", "firma", "nasz", "lat",
               "przez", "które", "będzie", "zespołu"},
    "turkish": {"ve", "bir", "için", "ile", "olarak", "çalışma", "şirket",
                "yıl", "bu", "en", "deneyim"},
    "indonesian": {"dan", "yang", "untuk", "dengan", "kami", "tidak",
                   "kerja", "perusahaan", "tahun", "akan", "anda"},
    "vietnamese": {"và", "của", "các", "cho", "với", "công", "không",
                   "làm", "việc", "năm", "chúng"},
    "swedish": {"och", "att", "för", "med", "vi", "inte", "arbete",
                "hos", "våra", "år", "kommer"},
    "danish": {"og", "at", "til", "med", "vi", "ikke", "arbejde",
               "hos", "vores", "år", "vil"},
}

_MIN_HITS = 3
_MARGIN = 1.5

_EN_STOPWORDS = {
    "the", "and", "for", "with", "you", "our", "are", "will", "this",
    "that", "have", "work", "team", "role", "we", "to", "of", "in",
    "is", "on", "as", "be",
}

_TOKEN_RE = re.compile(
    r"[a-zà-ÿąćęłńóśźżğışüöçãõâêîôûëïÿđ]+", re.IGNORECASE
)


def detect_language(text: str) -> str | None:
    """Return the best-supported non-English language, if there is one."""
    tokens = [token.lower() for token in _TOKEN_RE.findall(text)][:400]
    if not tokens:
        return None

    language_hits = {
        language: sum(1 for token in tokens if token in stopwords)
        for language, stopwords in _STOPWORDS.items()
    }
    english_hits = sum(1 for token in tokens if token in _EN_STOPWORDS)
    language, hits = max(language_hits.items(), key=lambda item: item[1])

    # Foreign stopword sets are smaller than the English set, hence the
    # normalized comparison against one third of the English evidence.
    if (hits >= _MIN_HITS
            and hits >= _MARGIN * max(english_hits, 1) / 3
            and hits > english_hits / 3):
        return language
    return None


def check(title: str, description: str,
          operator_languages: set[str]) -> list[str]:
    """Return rejection reasons; an empty list means the posting passes."""
    languages = {language.lower() for language in operator_languages}
    if _DE_MARKERS.search(title) and "german" not in languages:
        return ["posting_language:german_market_marker"]

    language = detect_language(f"{title}\n{description or ''}")
    if language and language not in languages:
        return [f"posting_language:{language}"]
    return []
