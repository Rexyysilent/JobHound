"""Conservative document/opportunity classification for V5.5.

This module only classifies evidence.  Routing remains the engine's job.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from ..text import normalize_match


@dataclass(frozen=True)
class DocumentClassification:
    document_type: str
    opportunity_type: str
    intent: str
    actionable: bool
    reasons: tuple[str, ...] = ()


_INDEX = re.compile(r"\b(?:browse|view|search|explore)\s+(?:(?:all|our|current|open|remote)\s+){0,3}(?:jobs|roles|openings)\b|\bjob\s+openings\b", re.I)
_SELLER = re.compile(r"\b(?:i\s+will|i\s+offer|buy\s+my|my\s+(?:service|gig|package)|hire\s+me|order\s+now|purchase\s+my|packages?\s+start(?:ing)?\s+at)\b", re.I)
_BUYER = re.compile(r"\b(?:buyer\s+seeks?|looking\s+(?:for|to\s+hire)|seeking|need(?:ed)?|hiring)\b.{0,80}\b(?:freelancer|contractor|developer|annotator|transcriber|specialist|someone)\b", re.I)
_RECRUITMENT = re.compile(
    r"\b(?:actively\s+)?(?:hiring|recruiting|seeking)\b.{0,100}\b"
    r"(?:speakers?|evaluators?|raters?|annotators?|transcribers?|contributors?|"
    r"freelancers?|contractors?|developers?|specialists?|candidates?|applicants?)\b",
    re.I,
)
# Titles that ask for advice or announce a discussion. "Discussion" followed by
# a role noun is a job title ("Discussion Moderator"); "how do we ..." and
# "tips for ..." are too common in real job titles to count.
_DISCUSSION_TITLE = re.compile(
    r"\b(?:how\s+(?:do|can|did|does|should)\s+(?:i|you|people|anyone|one)\b"
    r"|where\s+(?:can|do|should)\s+(?:i|you|people)\s+(?:find|get|look)\b"
    r"|is\s+.{1,60}?\s+(?:legit|legitimate|a\s+scam|worth\s+it)\b"
    r"|anyone\s+(?:else\s+)?(?:know|tried|using|working)\b"
    r"|(?:need|seeking|looking\s+for)\s+(?:some\s+)?advice\b|advice\s+needed\b|career\s+advice\b"
    r"|get(?:ting)?\s+(?:a\s+)?job\b"
    r"|discussion\b(?!\s+(?:moderator|facilitator|lead|leader|manager|specialist|analyst|host)))",
    re.I,
)
# Explicit role structure: section headings or application instructions.
# Casual mentions ("heard the pay is ...", "what are the requirements?") are
# what advice threads say, so they do not count.
_ROLE_STRUCTURE = re.compile(
    r"\b(?:requirements?|responsibilit(?:y|ies)|qualifications?|duties|compensation|salary|pay"
    r"|about\s+the\s+role|what\s+you(?:'ll|\s+will)\s+do|job\s+description)\s*:"
    r"|\b(?:how\s+to\s+apply|apply\s+(?:now|here|today|through|via|for\s+(?:this|the|job))"
    r"|we\s+are\s+hiring|we're\s+hiring)\b",
    re.I,
)
# A body only marks a discussion when it describes itself as one. The bare
# word "discussion" is ordinary in postings ("discussion channels", "a good
# discussion on technical design", "case and discussion" interview steps).
_DISCUSSION_BODY = re.compile(
    r"\b(?:discussion\s+(?:threads?|and\s+advice|posts?|topics?)|general\s+discussion"
    r"|advice\s+(?:threads?|wanted|needed)"
    r"|no\s+buyer\s+request|not\s+a\s+job\s+(?:posts?|postings?|offers?|listings?))\b",
    re.I,
)
_MODERATION = re.compile(r"\bmoderat(?:e|es|ed|ing|ion|or|ors)\b", re.I)
_POOL = re.compile(r"\b(?:talent\s+(?:network|community|pool)|join\s+our\s+network|future\s+opportunities|waitlist|expression\s+of\s+interest)\b", re.I)


def classify_document(title: str, text: str, url: str = "") -> DocumentClassification:
    combined = normalize_match(f"{title}\n{text}")
    try:
        split = urlsplit(url or "")
        host = (split.hostname or "").casefold().removeprefix("www.")
        path = (split.path or "/").casefold().rstrip("/") or "/"
    except ValueError:
        host, path = "", "/"
    # A document classification is not proof of a live vacancy or eligibility.
    if not title.strip() and not text.strip():
        return DocumentClassification("unknown", "unknown", "unknown", False, ("empty_document",))
    try:
        query = parse_qs(split.query)
    except (UnboundLocalError, ValueError):
        query = {}
    individual_query = any(query.get(key) for key in ("gh_jid", "jobId", "job_id", "postingId"))
    index_path = bool(re.search(r"/(?:jobs|careers|openings)$", path))
    index_heading = bool(_INDEX.search(title) or re.search(r"\b(?:remote\s+)?(?:ai\s+)?jobs\b", title, re.I))
    if index_path and not individual_query and (index_heading or _INDEX.search(combined)):
        return DocumentClassification("job_index", "index", "employer", False, ("multi_job_index",))
    # Footer recruitment copy and a role's discussion-moderation duties do not
    # determine the type of the main document. Keep that source text untouched.
    primary_text = re.split(r"\b(?:not\s+(?:right|ready)\s+for\s+you|not\s+ready\s+to\s+apply|other\s+opportunities)\b|(?:^|[.\n])\s*footer\s*:", text, maxsplit=1, flags=re.I)[0]
    primary = normalize_match(f"{title}\n{primary_text}")

    # Route semantics are stronger than keyword similarity in a search-result
    # snippet.  These are provider-independent page types, with Upwork's
    # public route vocabulary handled explicitly because a buyer post and a
    # seller catalogue live on the same host.
    if (host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com")) and (
        path == "/watch" or path.startswith("/shorts/") or host == "youtu.be"
    ):
        return DocumentClassification("article", "non_opportunity", "content", False, ("video_content_route",))
    if host == "upwork.com" or host.endswith(".upwork.com"):
        if path == "/services/product" or path.startswith("/services/product/"):
            return DocumentClassification("seller_service", "non_opportunity", "seller", False, ("seller_catalogue_route",))
        if path == "/hire" or path.startswith("/hire/"):
            return DocumentClassification("talent_directory", "index", "buyer_browsing", False, ("talent_directory_route",))
        if path.startswith("/freelance-jobs/apply/") or re.fullmatch(r"/jobs/~[0-9]+", path):
            return DocumentClassification("buyer_request", "individual_request", "buyer", True, ("individual_buyer_route",))
    if host in {"reddit.com", "old.reddit.com"} and "/comments/" in path:
        if _BUYER.search(combined):
            return DocumentClassification("buyer_request", "individual_request", "buyer", True, ("explicit_buyer_demand",))
        if _RECRUITMENT.search(combined):
            return DocumentClassification("individual_job", "individual_role", "employer", True, ("explicit_recruitment_demand",))
        return DocumentClassification("discussion", "non_opportunity", "discussion", False, ("discussion_route",))
    if _SELLER.search(combined):
        return DocumentClassification("seller_service", "non_opportunity", "seller", False, ("seller_language",))
    if _POOL.search(primary):
        return DocumentClassification("talent_pool", "pool", "employer", False, ("unallocated_pool",))
    body = normalize_match(primary_text)
    # On a moderation job, discussion phrases describe the work, not the page.
    body_signal = _DISCUSSION_BODY.search(body) and not _MODERATION.search(primary)
    discussion_signal = _DISCUSSION_TITLE.search(title) or body_signal
    if discussion_signal and not _ROLE_STRUCTURE.search(primary) and not _BUYER.search(combined):
        return DocumentClassification("discussion", "non_opportunity", "discussion", False, ("no_paid_demand",))
    if _INDEX.search(combined) and index_path and not individual_query:
        return DocumentClassification("job_index", "index", "employer", False, ("multi_job_index",))
    if _BUYER.search(combined):
        return DocumentClassification("buyer_request", "individual_request", "buyer", True, ("explicit_buyer_demand",))
    return DocumentClassification("individual_job", "individual_role", "employer", True)


def classify_offer(canonical: object) -> DocumentClassification:
    """Classify a CanonicalJob without coupling this focused helper to models."""
    job = getattr(canonical, "job", canonical)
    return classify_document(
        str(getattr(job, "title", "") or ""),
        str(getattr(job, "description", "") or ""),
        str(getattr(job, "url", "") or ""),
    )
