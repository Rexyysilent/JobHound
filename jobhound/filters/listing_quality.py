"""Listing-quality, source-integrity, and location gates."""
from __future__ import annotations

import re
from urllib.parse import urlparse

from ..config import CONFIG, ListingQualityCfg


_COUNT_TITLE = re.compile(r"^\s*\d[\d,.]*\s+.{0,80}\b(?:jobs?|vacancies)\b", re.IGNORECASE)
_SEARCH_URL = re.compile(
    r"(SRCH_|/jobs\?|[?&](?:q|query|search|keywords?)=|/search[/?]|-jobs-SRCH|"
    r"indeed\.[^/]+/q-[^?#]+-jobs(?:\.html)?(?:[?#]|$))",
    re.IGNORECASE,
)
_MARKETPLACE_INDEX_URL = re.compile(
    r"(?:upwork\.com/freelance-jobs/(?!apply(?:/|$))[^/?#]+/?$|"
    r"linkedin\.com/jobs/(?!view(?:/|$))[^?#]+(?:-jobs)?/?$|"
    r"outlier\.ai/(?:languages?|opportunities?)(?:/|$)|"
    r"aigigjobs\.com/(?:languages?|jobs?)(?:/|$)|"
    r"telusinternational\.ai/opportunities(?:/|$))",
    re.IGNORECASE,
)
_CATEGORY_TITLE = re.compile(
    r"^\s*(?:\d+[,+]?\s*)?(?:freelance\s+)?jobs?\b|"
    r"\b(?:freelance\s+jobs?|jobs?)\s+in\s+[^|]{2,60}$",
    re.IGNORECASE,
)
_ABSURD_LANGUAGE = re.compile(
    r"\b(sumerian|akkadian|klingon|elvish|quenya|sindarin|dothraki|"
    r"valyrian|na['’]?vi)\b",
    re.IGNORECASE,
)
_REGION_PAREN = re.compile(
    r"[([]\s*(usa|us|uk|united states|united kingdom|canada|australia|"
    r"eu|emea|latam|europe)(?:[\s-]+only)?\s*[)\]]",
    re.IGNORECASE,
)
_REGION_PHRASE = re.compile(
    r"\b(?:located|based)\s+in\s+(?:the\s+)?"
    r"(usa|us|uk|united states|united kingdom|canada|australia|germany|"
    r"eu|emea|europe)\b|"
    r"\b(usa|uk|us|canada|australia|germany|eu|emea|europe)"
    r"[\s-]+(?:only|residents?|citizens?)\b|"
    r"\b(?:location\s*:\s*)"
    r"(eu|emea|europe|germany)\b(?:\s*\(\s*remote\s*\))?|"
    r"\b(?:work(?:ing)?\s+from\s+anywhere\s+in|work(?:ing)?\s+from|"
    r"remote\s+(?:within|across|in))\s+(?:the\s+)?"
    r"(eu|emea|europe|germany)\b|"
    r"\b(eu|emea|europe|germany)[\s-]+remote\b",
    re.IGNORECASE,
)
_REGION_CITY_COUNTRY = re.compile(
    r"\b(?:located|based)\s+in\s+[^,.;\n]{1,60},\s*"
    r"(usa|us|united states|uk|united kingdom|canada|australia|germany|"
    r"eu|emea|europe)\b",
    re.IGNORECASE,
)
_REGION_LINE = re.compile(
    r"(?im)^\s*(?:location\s*:\s*)?"
    r"(usa|us|united states|uk|united kingdom|canada|australia|germany|"
    r"eu|emea|europe)\s*(?:[-,/|]\s*remote|\s+remote)?\s*$|"
    r"\s[-–—]\s*(usa|us|united states|uk|united kingdom|canada|australia|"
    r"germany|eu|emea|europe)\s*$"
)
_CONTENT_PATH = re.compile(r"/(blog|guide|article|news|resources)(?:/|$)", re.IGNORECASE)
_CONTENT_TITLE = re.compile(r"^(?:how to|guide|tips)\b|\bhow to (?:find|get)\b", re.IGNORECASE)
_NON_JOB_PUBLISHERS = {"ai supermarket"}

FREE_HOSTS = (
    "railway.app", "unaux.com", "epizy.com", "rf.gd",
    "infinityfreeapp.com", "vercel.app", "netlify.app", "github.io",
    "web.app", "firebaseapp.com", "glitch.me", "repl.co",
    "liveblog365.com",
)

ATS_DOMAINS = {
    "greenhouse.io", "lever.co", "ashbyhq.com", "workable.com",
    "smartrecruiters.com", "bamboohr.com", "recruitee.com",
}
BOARD_DOMAINS = {
    "remotive.com", "remoteok.com", "arbeitnow.com", "wellfound.com",
    "linkedin.com", "naukri.com", "indeed.com", "glassdoor.co.in",
    "glassdoor.com", "internshala.com", "adzuna.in", "adzuna.com",
    "upwork.com",
}
EMPLOYER_JOB_DOMAINS = {
    "alignerr.com", "lifeatcanva.com", "rws.com",
}
_SECOND_LEVEL = {"co", "com", "org", "net", "gov", "ac", "edu"}

_FOREIGN_LOCATION = re.compile(
    r"\b(?:bangladesh|pakistan|sri lanka|nepal|bhutan|maldives|"
    r"united states|usa|u\.s\.|canada|mexico|brazil|argentina|"
    r"united kingdom|uk|ireland|germany|france|spain|italy|portugal|"
    r"netherlands|belgium|switzerland|austria|poland|romania|"
    r"latvia|lithuania|estonia|europe|eu|emea|"
    r"algeria|south africa|nigeria|kenya|morocco|egypt|"
    r"china|taiwan|hong kong|japan|south korea|singapore|malaysia|"
    r"indonesia|philippines|vietnam|thailand|australia|new zealand|"
    r"united arab emirates|uae|dubai|abu dhabi|chile|israel|norway|peru)\b",
    re.IGNORECASE,
)
_INDIA_LOCATION = re.compile(
    r"\b(?:india|indian|bengaluru|bangalore|gurugram|gurgaon|delhi|noida|"
    r"mumbai|pune|hyderabad|chennai|kolkata|ahmedabad|jaipur|kochi)\b",
    re.IGNORECASE,
)
_EXPLICIT_ONSITE = re.compile(
    r"\b(?:on[ -]?site|in[ -]?office|office[ -]?based|work\s+from\s+office|wfo|hybrid)\b",
    re.IGNORECASE,
)
_GENERIC_REMOTE_LOCATION = re.compile(
    r"^\s*(?:remote|anywhere|worldwide|global|work\s+from\s+home)\s*$",
    re.IGNORECASE,
)


def junk_check(title: str, url: str, company: str | None,
               has_rate: bool, company_domain: str | None = None,
               cfg: ListingQualityCfg = CONFIG.listing_quality) -> list[str]:
    """Identify search/index pages masquerading as individual job listings."""
    reasons: list[str] = []
    if _COUNT_TITLE.search(title):
        reasons.append("junk:count_title_search_page")
    if _SEARCH_URL.search(url):
        reasons.append("junk:search_results_url")
    if _MARKETPLACE_INDEX_URL.search(url):
        reasons.append("junk:marketplace_index_page")
    if _CATEGORY_TITLE.search(title) and not re.search(
        r"\b(?:engineer|developer|specialist|evaluator|annotator|trainer|"
        r"reviewer|intern|contractor|consultant)\b",
        title,
        re.IGNORECASE,
    ):
        reasons.append("junk:category_title")
    if not company and not has_rate and len(title) < 25:
        reasons.append("junk:no_company_no_rate")
    if (company or "").strip().casefold() in _NON_JOB_PUBLISHERS:
        reasons.append("junk:non_job_publisher")
    reasons.extend(content_page_check(title, url))
    reasons.extend(farm_source_check(url, company_domain, cfg))
    return reasons


def farm_source_check(url: str, company_domain: str | None = None,
                      cfg: ListingQualityCfg = CONFIG.listing_quality) -> list[str]:
    """Categorically reject known content-farm copies (v3.5 item 3).

    A farm confidence prior of 0.35 was too weak to matter: the EV confidence
    multiplier is 0.7 + 0.3·confidence, so a keyword-stuffed farm page still
    reached fit 100 and outranked real work. Source quality is therefore a
    listing-quality REJECTION, not a score nudge — no keyword scaling, no fit
    cap, no change to compute_ev.

    Only the retained canonical URL is examined. If dedupe already picked a
    direct ATS/employer link for this record, a farm entry in `seen_on` must not
    reject it — the canonical URL is assessed on its own merits.
    """
    tier, _ = domain_tier(url, company_domain, cfg)
    if tier == "farm":
        return [f"junk:source_farm:{_base_domain(url)}"]
    # Path-level tell: an unrelated host section carrying job detail pages, e.g.
    # mysmartpros.com/tuition/job/... — a tuition site is not a job board.
    path = (urlparse(url).path or "/").lower()
    return [f"junk:farm_path:{marker}" for marker in cfg.farm_paths
            if marker.lower() in path]


def free_host_check(url: str) -> list[str]:
    """Flag purported job boards hosted on disposable/free app platforms."""
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    for suffix in FREE_HOSTS:
        if host == suffix or host.endswith(f".{suffix}"):
            return [f"scam:free_host_board:{suffix}"]
    return []


def absurd_language_check(title: str) -> list[str]:
    """Detect implausible language requirements used in fake listing floods."""
    match = _ABSURD_LANGUAGE.search(title)
    if match is None:
        return []
    return [f"scam:absurd_language:{match.group(1).lower()}"]


def region_lock_check(title: str, description: str = "") -> list[str]:
    """Reject explicit non-India country locks even when a job says remote."""
    text = f"{title}\n{(description or '')[:600]}"
    for pattern in (
        _REGION_PAREN, _REGION_PHRASE, _REGION_CITY_COUNTRY, _REGION_LINE
    ):
        match = pattern.search(text)
        if match:
            country = next(group for group in match.groups() if group)
            return [f"region_locked:{country.lower()}"]
    return []


def content_page_check(title: str, url: str) -> list[str]:
    """Identify editorial pages and bare homepages, not job detail pages."""
    parsed = urlparse(url)
    reasons: list[str] = []
    if _CONTENT_PATH.search(parsed.path or "/"):
        reasons.append("junk:content_page_url")
    if _CONTENT_TITLE.search(title.strip()):
        reasons.append("junk:content_page_title")
    if (parsed.path or "/") == "/" and not parsed.query:
        reasons.append("junk:root_domain")
    return reasons


def _base_domain(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in _SECOND_LEVEL:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def domain_tier(url: str, company_domain: str | None = None,
                cfg: ListingQualityCfg = CONFIG.listing_quality) -> tuple[str, float]:
    """Return the source-domain tier and its baseline confidence."""
    domain = _base_domain(url)
    confidence = cfg.tier_confidence
    if domain in ATS_DOMAINS:
        return "ats", confidence["ats"]
    if domain in EMPLOYER_JOB_DOMAINS:
        return "employer", confidence["ats"]
    if domain in BOARD_DOMAINS:
        return "board", confidence["board"]
    if domain in set(cfg.farm_domains):
        return "farm", confidence["farm"]

    # A posting on the employer's own domain is canonical like an ATS posting.
    if company_domain:
        normalized_company_domain = _base_domain(
            company_domain if "://" in company_domain else f"https://{company_domain}"
        )
        if domain and domain == normalized_company_domain:
            return "ats", confidence["ats"]
    return "unknown", confidence["unknown"]


def location_gate(is_remote: bool, region_tags: list[str],
                  location: str | None, description: str = "",
                  title: str = "") -> list[str]:
    """Reject foreign-locked or explicitly on-site work before remote boosts.

    A provider's ``is_remote`` flag means remote *within the advertised
    country*, not globally remote.  V4.1 returned early on that flag, which let
    Bangladesh/Ireland/Algeria/etc. listings pass as India-accessible.  Explicit
    location evidence therefore wins over broad remote/worldwide tags.
    """
    tags = {tag.lower() for tag in region_tags}
    normalized_location = (location or "").lower()
    normalized_title = (title or "").lower()
    location_lines = "\n".join(
        line.strip() for line in (description or "")[:1200].splitlines()
        if re.search(r"\b(?:location|workplace|work location)\s*:", line, re.I)
    )
    explicit_text = f"{normalized_location}\n{normalized_title}\n{location_lines}"
    if _EXPLICIT_ONSITE.search(explicit_text):
        return ["rejected_location:onsite_or_hybrid"]
    if _FOREIGN_LOCATION.search(explicit_text) and not _INDIA_LOCATION.search(explicit_text):
        match = _FOREIGN_LOCATION.search(explicit_text)
        return [f"rejected_location:{match.group(0).lower()}"]
    # A concrete provider location on a non-remote listing outranks noisy
    # company-copy tags such as "worldwide team" or "remote-first culture".
    if (
        not is_remote
        and normalized_location.strip()
        and not _GENERIC_REMOTE_LOCATION.fullmatch(normalized_location)
        and not _INDIA_LOCATION.search(normalized_location)
        and "india" not in tags
    ):
        return [f"rejected_location:{location}"]
    if is_remote or tags & {"india", "worldwide", "apac", "asia"}:
        return []
    if _INDIA_LOCATION.search(normalized_location) or "remote" in normalized_location:
        return []
    return [f"rejected_location:{location or 'unknown'}"]
