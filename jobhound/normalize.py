"""Raw provider records → canonical Job. Per-source field mapping + shared
is_remote / region_tags detection. Unknown source raises (caught upstream)."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from functools import lru_cache
from urllib.parse import unquote, urlsplit, urlunsplit

from .config import CONFIG
from .models import Job
from .text import html_to_text, normalize_text

_TITLE_PREFIX_RE = re.compile(r"^\s*(?:copy of|\*+|re:)\s*", re.IGNORECASE)
_TITLE_TRUNCATED_RE = re.compile(r"(?:\.{3}|…)\s*$")
_GREENHOUSE_JOB_HOST = re.compile(
    r"^(?:boards|job-boards)(?:\.[a-z]{2,8})?\.greenhouse\.io$",
    re.IGNORECASE,
)

# A language tag/boost only fires when a language *intent* cue co-occurs with the
# language name — so "means electricity in hindi" (etymology) doesn't trigger the
# Bengali lane, but "fluent Bengali speaker" does.
_LANG_CUE_RE = re.compile(
    r"fluent|native|speaker|bilingual|multilingual|languages?\b|"
    r"translat|locali[sz]|proficien|mother tongue",
    re.I,
)

_REMOTE_SIGNAL_RE = re.compile(
    r"\bremote\b|\bwork(?:ing)?\s+from\s+home\b|\bwfh\b|"
    r"\bwork\s+from\s+anywhere\b|\bworldwide\b|"
    r"\bglobal(?:ly)?\s+remote\b|\bremote\s+global(?:ly)?\b",
    re.IGNORECASE,
)
_WORLDWIDE_SIGNAL_RE = re.compile(
    r"\bwork\s+from\s+anywhere\b|\bworldwide\b|"
    r"\bglobal(?:ly)?\s+remote\b|\bremote\s+global(?:ly)?\b|"
    r"\bremote\s+anywhere\b",
    re.IGNORECASE,
)


def has_language_intent(text: str) -> bool:
    return _LANG_CUE_RE.search(text) is not None


@lru_cache(maxsize=256)
def _word_re(term: str) -> re.Pattern:
    return re.compile(rf"\b{re.escape(term.lower())}\b")


def _any_word(terms: list[str], text_lower: str) -> bool:
    return any(_word_re(t).search(text_lower) for t in terms)


def remote_signal(text: str, *, explicit: bool = False) -> bool:
    """Return true only for contextual remote/worldwide evidence.

    Bare ``global`` is intentionally not enough: "global challenges" and
    "global team" occurred in production descriptions for Germany-only jobs.
    """
    return explicit or _REMOTE_SIGNAL_RE.search(text or "") is not None


def _strip_html(text: str | None) -> str:
    return html_to_text(text)


def _strip_url_query(url: str | None) -> str:
    """Return a public URL without provider credentials or tracking parameters."""
    if not url:
        return ""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _display_slug(value: str) -> str:
    words = re.sub(r"[-_]+", " ", value).strip()
    return " ".join(word.upper() if len(word) <= 3 else word.title()
                    for word in words.split())


_OFFICIAL_JOB_DOMAINS = {
    "alignerr.com": "Alignerr",
    "lifeatcanva.com": "Canva",
    "rws.com": "RWS",
    "sigma.ai": "Sigma AI",
}


def company_domain_from_job_url(url: str | None) -> str | None:
    """Return a verified employer domain for known first-party job sites."""
    if not url:
        return None
    try:
        host = (urlsplit(url).hostname or "").casefold().removeprefix("www.")
    except ValueError:
        return None
    return next(
        (domain for domain in _OFFICIAL_JOB_DOMAINS
         if host == domain or host.endswith(f".{domain}")),
        None,
    )


def company_from_job_url(url: str | None) -> str | None:
    """Best-effort company hint for thin SERP records.

    ATS board slugs are stable publisher identifiers. LinkedIn job slugs are
    weaker evidence, but still preferable to treating every company-less SERP
    result as the same counterparty in the digest cap.
    """
    if not url:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    host = (parts.hostname or "").casefold()
    segments = [unquote(item) for item in parts.path.split("/") if item]
    if not segments:
        return None

    official_domain = company_domain_from_job_url(url)
    if official_domain:
        return _OFFICIAL_JOB_DOMAINS[official_domain]

    if host == "jobs.ashbyhq.com":
        slug = segments[0].casefold()
        configured = CONFIG.sources.ats.ashby.get(slug)
        if configured:
            return configured
        cleaned = re.sub(r"-(?:production|careers|jobs)$", "", slug)
        return _display_slug(cleaned)
    if host in {"jobs.lever.co", "jobs.eu.lever.co"}:
        slug = segments[0].casefold()
        return CONFIG.sources.ats.lever.get(slug) or _display_slug(slug)
    if _GREENHOUSE_JOB_HOST.fullmatch(host):
        slug = segments[0].casefold()
        return CONFIG.sources.ats.greenhouse.get(slug) or _display_slug(slug)
    if host in {"linkedin.com", "www.linkedin.com"} and "view" in segments:
        slug = segments[-1].casefold()
        slug = re.sub(r"-\d+$", "", slug)
        if "-at-" in slug:
            return _display_slug(slug.rsplit("-at-", 1)[-1])
    if host in {"upwork.com", "www.upwork.com"} and (
        len(segments) >= 2
        and segments[0].casefold() == "freelance-jobs"
        and segments[1].casefold() == "apply"
    ):
        # Upwork conceals the client name on many public/indexed listings. The
        # marketplace is still the relevant trust/economics counterparty.
        return "Upwork Client"
    if host in {"remoterocketship.com", "www.remoterocketship.com"}:
        lowered = [segment.casefold() for segment in segments]
        if "company" in lowered:
            index = lowered.index("company")
            if index + 1 < len(segments):
                return _display_slug(segments[index + 1])
    return None


def company_confidence_from_job_url(url: str | None) -> float:
    if not url:
        return 0.0
    try:
        host = (urlsplit(url).hostname or "").casefold()
    except ValueError:
        return 0.0
    if company_domain_from_job_url(url):
        return 1.0
    if host in {"jobs.ashbyhq.com", "jobs.lever.co", "jobs.eu.lever.co"}:
        return 0.85
    if _GREENHOUSE_JOB_HOST.fullmatch(host):
        return 0.85
    if host in {"remoterocketship.com", "www.remoterocketship.com"}:
        return 0.60
    if host in {"linkedin.com", "www.linkedin.com"}:
        return 0.55
    if host in {"upwork.com", "www.upwork.com"}:
        return 0.50
    return 0.0


def serp_title_state(title: str, url: str) -> tuple[str, str | None, bool, bool]:
    """Return display title plus explicit truncation/reconstruction metadata."""
    if _TITLE_TRUNCATED_RE.search(title or "") is None:
        return title, None, False, False
    original = title
    reconstructed = False
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").casefold()
        segments = [unquote(item) for item in parts.path.split("/") if item]
    except ValueError:
        return title, original, True, False
    if (
        host in {"upwork.com", "www.upwork.com"}
        and len(segments) >= 3
        and segments[0].casefold() == "freelance-jobs"
        and segments[1].casefold() == "apply"
    ):
        slug = re.sub(r"_~[A-Za-z0-9]+$", "", segments[2])
        candidate = re.sub(r"\bBOT\b", "Bot", _display_slug(slug))
        prefix = _TITLE_TRUNCATED_RE.sub("", title).strip().casefold()
        if len(candidate) > len(prefix) and (
            not prefix or candidate.casefold().startswith(prefix[:20])
        ):
            title = candidate
            reconstructed = True
    return title, original, True, reconstructed


def strip_title_prefix(title: str) -> str:
    """Remove repeated mail/template prefixes without touching title content."""
    previous = None
    while title != previous:
        previous = title
        title = _TITLE_PREFIX_RE.sub("", title).strip()
    return title


def _parse_dt(value) -> datetime | None:
    if value is None or value == "":
        return None
    # Unix timestamp (Arbeitnow created_at = seconds; Lever createdAt = ms).
    if isinstance(value, (int, float)):
        if value > 1e12:
            value /= 1000
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    # ISO-ish string.
    s = str(value).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        # Date only.
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return None


def language_signal(title: str, body: str) -> bool:
    """True if the role is genuinely language-edge (HANDOFF §9).

    A language name in the TITLE is role-defining → strong signal
    ("Internet Safety Evaluator – Bengali"). In the BODY it must co-occur with a
    language-intent cue → so company etymology ("means electricity in hindi")
    does NOT trigger the lane.
    """
    if _any_word(CONFIG.region.lang_terms, title.lower()):
        return True
    b = body.lower()
    return _any_word(CONFIG.region.lang_terms, b) and has_language_intent(b)


def _detect_region_tags(title: str, body: str, is_remote: bool) -> list[str]:
    both = f"{title}\n{body}".lower()
    tags: list[str] = []
    if _any_word(CONFIG.region.india_terms, both):
        tags.append("india")
    if language_signal(title, body):
        tags.append("bengali")  # bucket label for language-edge roles
    if _WORLDWIDE_SIGNAL_RE.search(both):
        tags.append("worldwide")
    return list(dict.fromkeys(tags))  # dedupe, preserve order


def _norm_remotive(raw: dict) -> Job:
    desc = _strip_html(raw.get("description"))
    loc = raw.get("candidate_required_location") or ""
    tags = " ".join(raw.get("tags") or [])
    title = raw.get("title", "").strip()
    region = _detect_region_tags(title, f"{loc} {tags} {desc[:2000]}", is_remote=True)
    return Job(
        source="remotive",
        title=title,
        company=(raw.get("company_name") or None),
        url=raw.get("url", ""),
        description=desc,
        location=loc or None,
        is_remote=True,
        region_tags=region,
        posted_at=_parse_dt(raw.get("publication_date")),
        pay_raw=(raw.get("salary") or None),
    )


def _norm_remoteok(raw: dict) -> Job:
    desc = _strip_html(raw.get("description"))
    loc = raw.get("location") or ""
    tags = " ".join(raw.get("tags") or [])
    title = (raw.get("position") or raw.get("title") or "").strip()
    region = _detect_region_tags(title, f"{loc} {tags} {desc[:2000]}", is_remote=True)
    # salary_min/max are annual USD integers when present.
    pay_raw = None
    smin, smax = raw.get("salary_min"), raw.get("salary_max")
    if (smin or smax):
        lo = smin or smax
        hi = smax or smin
        pay_raw = f"${lo} - ${hi} per year"
    url = raw.get("url") or ""
    return Job(
        source="remoteok",
        title=title,
        company=(raw.get("company") or None),
        url=url,
        description=desc,
        location=loc or None,
        is_remote=True,
        region_tags=region,
        posted_at=_parse_dt(raw.get("date")),
        pay_raw=pay_raw,
    )


def _provider_terms(value: object) -> list[str]:
    """Coerce inconsistent provider tag collections without losing labels."""
    if value is None:
        return []
    if isinstance(value, dict):
        values = value.values()
    elif isinstance(value, str):
        values = [value]
    elif isinstance(value, (list, tuple, set, frozenset)):
        values = value
    else:
        return []
    return [str(item).strip() for item in values if str(item).strip()]


def _norm_arbeitnow(raw: dict) -> Job:
    desc = _strip_html(raw.get("description"))
    loc = raw.get("location") or ""
    is_remote = bool(raw.get("remote"))
    tags = " ".join(
        _provider_terms(raw.get("tags"))
        + _provider_terms(raw.get("job_types"))
    )
    title = raw.get("title", "").strip()
    region = _detect_region_tags(title, f"{loc} {tags} {desc[:2000]}", is_remote=is_remote)
    return Job(
        source="arbeitnow",
        title=title,
        company=(raw.get("company_name") or None),
        url=raw.get("url", ""),
        description=desc,
        location=loc or None,
        is_remote=is_remote,
        region_tags=region,
        posted_at=_parse_dt(raw.get("created_at")),
        pay_raw=None,  # Arbeitnow has no salary field
    )


def _norm_adzuna(raw: dict) -> Job:
    desc = _strip_html(raw.get("description"))
    loc = (raw.get("location") or {}).get("display_name") or ""
    title = _strip_html(raw.get("title") or "").strip()
    # Adzuna has no remote flag — detect from text; non-remote India rows still
    # survive the relevance gate via the "india" region tag.
    is_remote = remote_signal(f"{title}\n{loc}\n{desc}")
    region = _detect_region_tags(title, f"{loc} {desc[:2000]}", is_remote=is_remote)
    # Salaries are annual, local currency (config), sometimes model-predicted.
    pay_raw = None
    smin, smax = raw.get("salary_min"), raw.get("salary_max")
    if smin or smax:
        lo, hi = (smin or smax), (smax or smin)
        cur = CONFIG.sources.adzuna.currency
        pay_raw = f"{cur} {lo:.0f} - {hi:.0f} per year"
        if str(raw.get("salary_is_predicted")) == "1":
            pay_raw += " (predicted)"
    return Job(
        source="adzuna",
        title=title,
        company=((raw.get("company") or {}).get("display_name") or None),
        # Adzuna puts the configured app id in redirect_url's utm_source.
        # The detail path is sufficient for users and canonical identity; never
        # allow provider credentials/tracking parameters into the DB or digest.
        url=_strip_url_query(raw.get("redirect_url")),
        description=desc,
        location=loc or None,
        is_remote=is_remote,
        region_tags=region,
        posted_at=_parse_dt(raw.get("created")),
        pay_raw=pay_raw,
    )


def _norm_jsearch(raw: dict) -> Job:
    desc = _strip_html(raw.get("job_description"))
    title = (raw.get("job_title") or "").strip()
    loc = ", ".join(p for p in (raw.get("job_city"), raw.get("job_state"),
                                raw.get("job_country")) if p)
    is_remote = bool(raw.get("job_is_remote"))
    region = _detect_region_tags(title, f"{loc} {desc[:2000]}", is_remote=is_remote)
    # v2 dropped job_salary_currency; assuming USD on an INR salary would inflate
    # it ~83× past the pay floor, so only build from min/max when currency is
    # explicit — otherwise fall back to the human string (₹/$ symbols parse fine).
    pay_raw = None
    smin, smax = raw.get("job_min_salary"), raw.get("job_max_salary")
    cur = raw.get("job_salary_currency")
    if (smin or smax) and cur:
        lo, hi = (smin or smax), (smax or smin)
        period = {"YEAR": "per year", "MONTH": "per month", "HOUR": "per hour"}.get(
            (raw.get("job_salary_period") or "").upper(), "")
        pay_raw = f"{cur} {lo} - {hi} {period}".strip()
    else:
        pay_raw = raw.get("job_salary_string") or raw.get("job_salary") or None
        pay_raw = str(pay_raw) if pay_raw else None
    return Job(
        source="jsearch",
        title=title,
        company=(raw.get("employer_name") or None),
        url=raw.get("job_apply_link") or raw.get("job_google_link") or "",
        description=desc,
        location=loc or None,
        is_remote=is_remote,
        region_tags=region,
        posted_at=_parse_dt(raw.get("job_posted_at_datetime_utc")),
        pay_raw=pay_raw,
    )


def _norm_greenhouse(raw: dict) -> Job:
    # content is entity-escaped HTML (&lt;p&gt;…) — unescape BEFORE tag-stripping.
    desc = _strip_html(raw.get("content") or "")
    loc = (raw.get("location") or {}).get("name") or ""
    title = (raw.get("title") or "").strip()
    text = f"{title}\n{loc}\n{desc}".lower()
    is_remote = remote_signal(text)
    region = _detect_region_tags(title, f"{loc} {desc[:2000]}", is_remote=is_remote)
    return Job(
        source="greenhouse",
        title=title,
        company=(raw.get("_company") or raw.get("company_name") or None),
        url=raw.get("absolute_url", ""),
        description=desc,
        location=loc or None,
        is_remote=is_remote,
        region_tags=region,
        posted_at=_parse_dt(raw.get("first_published") or raw.get("updated_at")),
        pay_raw=None,  # Greenhouse boards rarely expose structured salary
    )


def _norm_lever(raw: dict) -> Job:
    desc = (raw.get("descriptionPlain") or _strip_html(raw.get("description"))).strip()
    cats = raw.get("categories") or {}
    loc = ", ".join(p for p in (cats.get("location"), raw.get("country")) if p)
    title = (raw.get("text") or "").strip()
    is_remote = remote_signal(
        f"{title}\n{loc}", explicit=raw.get("workplaceType") == "remote"
    )
    body = f"{loc} {cats.get('department') or ''} {cats.get('team') or ''} {desc[:2000]}"
    region = _detect_region_tags(title, body, is_remote=is_remote)
    pay_raw = None
    sr = raw.get("salaryRange") or {}
    if sr.get("min") or sr.get("max"):
        lo, hi = sr.get("min") or sr.get("max"), sr.get("max") or sr.get("min")
        interval = str(sr.get("interval") or "")
        per = ("per hour" if "hour" in interval else
               "per month" if "month" in interval else "per year")
        pay_raw = f"{sr.get('currency') or 'USD'} {lo} - {hi} {per}"
    return Job(
        source="lever",
        title=title,
        company=(raw.get("_company") or None),
        url=raw.get("hostedUrl") or raw.get("applyUrl") or "",
        description=desc,
        location=loc or None,
        is_remote=is_remote,
        region_tags=region,
        posted_at=_parse_dt(raw.get("createdAt")),
        pay_raw=pay_raw,
    )


def _norm_ashby(raw: dict) -> Job:
    desc = (raw.get("descriptionPlain") or _strip_html(raw.get("descriptionHtml"))).strip()
    secondary = [s if isinstance(s, str) else (s.get("location") or "")
                 for s in raw.get("secondaryLocations") or []]
    loc = ", ".join(p for p in [raw.get("location"), *secondary] if p)
    title = (raw.get("title") or "").strip()
    is_remote = bool(raw.get("isRemote")) or raw.get("workplaceType") == "Remote"
    region = _detect_region_tags(title, f"{loc} {desc[:2000]}", is_remote=is_remote)
    comp = raw.get("compensation") or {}
    pay_raw = comp.get("scrapeableCompensationSalarySummary") or None
    return Job(
        source="ashby",
        title=title,
        company=(raw.get("_company") or None),
        url=raw.get("jobUrl") or raw.get("applyUrl") or "",
        description=desc,
        location=loc or None,
        is_remote=is_remote,
        region_tags=region,
        posted_at=_parse_dt(raw.get("publishedAt")),
        pay_raw=pay_raw,
    )


def _norm_serp(raw: dict) -> Job:
    # Bare SERP hit — title + snippet only. Thin by construction; the
    # confidence scorer penalises it and the digest links to the original.
    title = (raw.get("title") or "").strip()
    desc = (raw.get("snippet") or "").strip()
    link = raw.get("link", "")
    title, original_title, title_truncated, title_reconstructed = (
        serp_title_state(title, link)
    )
    text = f"{title}\n{desc}".lower()
    try:
        link_parts = urlsplit(link)
        is_upwork_listing = (
            (link_parts.hostname or "").casefold() in {"upwork.com", "www.upwork.com"}
            and link_parts.path.casefold().startswith("/freelance-jobs/apply/")
        )
    except ValueError:
        is_upwork_listing = False
    is_remote = remote_signal(text) or is_upwork_listing
    region = _detect_region_tags(title, desc, is_remote=is_remote)
    return Job(
        source="serp",
        title=title,
        company=company_from_job_url(link),
        company_domain=company_domain_from_job_url(link),
        company_confidence=company_confidence_from_job_url(link),
        url=link,
        original_title=original_title,
        title_truncated=title_truncated,
        title_reconstructed=title_reconstructed,
        description=desc,
        location=None,
        is_remote=is_remote,
        region_tags=region,
        posted_at=_parse_dt(raw.get("date")),
        pay_raw=None,
    )


def _norm_email_offer(raw: dict) -> Job:
    # Offer dicts come pre-parsed from sources/email_offers.py (v3 Patch #2):
    # platform-targeted, real rates, platform_key preset by the sender map.
    from .trust.registry import default_registry
    title = (raw.get("title") or "").strip()
    desc = (raw.get("description") or "").strip()
    platform_key = raw.get("platform_key")
    pt = default_registry().get(platform_key) if platform_key else None
    region = _detect_region_tags(title, desc[:2000], is_remote=True)
    return Job(
        source=f"email:{platform_key}",
        title=title,
        company=(pt.display_name if pt else None),
        url=raw.get("url", ""),
        description=desc,
        is_remote=True,
        region_tags=region,
        posted_at=None,
        pay_raw=raw.get("pay_raw"),
        guaranteed_hours=bool(raw.get("guaranteed_hours")),
        platform_key=platform_key,
        listing_confidence=float(raw.get("listing_confidence", 0.9)),
    )


_MAPPERS = {
    "remotive": _norm_remotive,
    "remoteok": _norm_remoteok,
    "arbeitnow": _norm_arbeitnow,
    "adzuna": _norm_adzuna,
    "jsearch": _norm_jsearch,
    "greenhouse": _norm_greenhouse,
    "lever": _norm_lever,
    "ashby": _norm_ashby,
    "serp": _norm_serp,
    "email_offers": _norm_email_offer,
}


# Human-readable fields repaired on every job, from every source (v3.5 item 1).
_TEXT_FIELDS = ("title", "company", "location", "description", "pay_raw")
# These are `str | None` on the model — whitespace-only must stay None, not "".
_OPTIONAL_TEXT_FIELDS = {"company", "location", "pay_raw"}


def apply_text_normalization(job: Job) -> Job:
    """Repair provider text ON THE CANONICAL JOB, before anything reads it.

    Fixing only the digest string would leave pay parsing, the eligibility
    gates, relevance matching and the dedupe key working from mojibake.
    """
    for field in _TEXT_FIELDS:
        value = getattr(job, field)
        if not isinstance(value, str):
            continue
        cleaned = normalize_text(value)
        if not cleaned and field in _OPTIONAL_TEXT_FIELDS:
            cleaned = None
        setattr(job, field, cleaned)
    return job


def canonicalize(job: Job) -> Job:
    """Source-independent canonicalization applied to every mapped Job.

    Text repair first, then region/language tags re-derived from the repaired
    text, then title-prefix stripping. Public so the offline regression fixtures
    can build canonical Jobs through the same path production uses.
    """
    apply_text_normalization(job)
    # Region tags were derived inside the mapper from the RAW text, so re-derive
    # them from the repaired text and union the two. Union rather than replace
    # for two reasons: mojibake can only prevent a correct word match, never
    # invent one, and the mappers feed extra context (provider tags, department,
    # job_types) that the canonical Job no longer carries.
    derived_tags = _detect_region_tags(
        job.title,
        f"{job.location or ''} {job.description[:2000]}",
        is_remote=job.is_remote,
    )
    # Preserve provider-derived language and India context, but recompute
    # worldwide authoritatively so stale/broad "global" matches cannot survive.
    preserved_tags = [tag for tag in job.region_tags if tag != "worldwide"]
    job.region_tags = list(dict.fromkeys([*preserved_tags, *derived_tags]))
    job.title = strip_title_prefix(job.title)
    if _TITLE_TRUNCATED_RE.search(job.title):
        job.original_title = job.original_title or job.title
        job.title_truncated = True
    return job


def normalize(record: dict) -> Job | None:
    """record = {"source": name, "raw": provider_dict}. Returns None if unusable."""
    mapper = _MAPPERS.get(record["source"])
    if mapper is None:
        raise ValueError(f"no normalizer for source {record['source']!r}")
    job = canonicalize(mapper(record["raw"]))
    if not job.title or not job.url:
        return None  # drop garbage rows lacking the essentials
    return job
