"""Bounded public Discourse evidence for exact Make/n8n topics.

Post ownership is an observed forum actor, not verified employer identity.
Quotation, reply activity, thread locks and seller prices are separate facts.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urlsplit

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

HOSTS = frozenset({'community.make.com', 'community.n8n.io'})
VERSION = 'community-thread/v2'
LEGACY_VERSION = 'community-thread/v1'
_REVISION_FIELDS = {'revisions', 'current_terms_post_ids', 'current_terms_revision_id', 'terms_state'}
_SCOPE_CHANGE = re.compile(r'(?im)(?:^|(?<=[.!?])\s+)(?:(?:the|our|my)\s+)?(?:(?:scope|deliverables?|requirements?)\s+(?:now\s+(?:requires?|includes?)|(?:has|have)\s+changed|(?:is|are)\s+(?:now\s+)?(?:changed|revised|updated))|(?:new|revised|updated)\s+(?:scope|deliverables?|requirements?)\s*:)', re.I)
_BUDGET_CHANGE = re.compile(r'(?im)(?:^|(?<=[.!?])\s+)(?:(?:new|revised|updated|current)\s+)?(?:project\s+)?budget\s*(?:(?:is\s+)?now\s*|(?:is|:|=)\s*|\s+)(?:USD|EUR|GBP|INR|US\$|\$|€|£|₹)\s*[0-9]', re.I)
_NATIVE_MONEY = re.compile(r'(?:USD|EUR|GBP|INR|US\$|\$|€|£|₹)\s*[0-9]', re.I)

def _declared(pattern, text):
    # Questions need clarification; a mention cannot establish a terms revision.
    return '?' not in text and bool(pattern.search(text))
_SELLER = re.compile(r'\b(?:for\s+hire|hire\s+me|i\s+(?:offer|will\s+(?:build|fix|create|deliver))|my\s+(?:services?|packages?)|services?\s+(?:start|available))\b', re.I)
_DEMAND = re.compile(r'\b(?:hiring|looking\s+to\s+hire|paid\s+(?:help|trial|work|project)|(?:looking\s+for|seeking|need)\b.{0,70}\b(?:freelancer|developer|contractor|expert|specialist|someone))\b', re.I)
_FREE = re.compile(r'\b(?:free\s+help\s+only|not\s+hiring|no\s+(?:budget|paid\s+work)|unpaid|volunteer\s+only)\b', re.I)
_CLOSE = re.compile(r'(?im)(?:^|(?<=[.!?])\s+)(?:\[?filled\]?|(?:this|the)\s+(?:project|role|position|request|job)\s+(?:is|has\s+been|was)\s+(?:now\s+)?(?:closed|filled|completed)|(?:i|we)(?:\s+have|\x27ve)?\s+(?:found|hired)\s+(?:someone|a\s+(?:freelancer|contractor|developer))|(?:we\s+are|i\s+am)\s+no\s+longer\s+hiring)(?:\b|$)', re.I)
_REOPEN = re.compile(r'(?im)(?:^|(?<=[.!?])\s+)(?:this\s+(?:project|role|request|job)\s+(?:is|has\s+been)\s+reopened|(?:we\s+are|i\s+am)\s+(?:still|again)\s+(?:hiring|looking\s+for))\b', re.I)
_SCOPE = re.compile(r'\b(?:budget|scope|deliverables?|requirements?|deadline|workflow|integration|migration|self[- ]hosted|scenario|price|counteroffer)\b', re.I)
_HELP = re.compile(r"\b(?:i|we)\s+(?:need|am\s+looking\s+for|are\s+looking\s+for)\s+help\b", re.I)
_WORKFLOW_TOOL = re.compile(r'\b(?:n8n|zapier|make\.com|webhooks?|whatsapp(?:\s+cloud|\s+business)?\s+api|google\s+sheets|airtable)\b|\bmake\s+(?:workflow|scenario)\b', re.I)
_WORKFLOW_WORK = re.compile(r'\b(?:connect|integrat(?:e|ion)|repair|fix|debug|migrat(?:e|ion)|automate|automation|workflow|scenario|build|implement|set\s+up)\b', re.I)


class PostEvidence(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    post_id: int
    post_number: int
    actor_id: int | None
    actor_role: str
    created_at: AwareDatetime | None
    edited_at: AwareDatetime | None
    text: str
    quotes: list[str]
    cooked_sha256: str
    reply_to_post_number: int | None = None
    status_signal: str = 'unknown'


class BuyerRevision(BaseModel):
    """A native buyer post, not an agreement or reconstructed edit history."""
    model_config = ConfigDict(extra='forbid', frozen=True)
    revision_id: str
    post_id: int
    actor_id: int
    kind: str
    created_at: AwareDatetime | None
    edited_at: AwareDatetime | None
    material_text_sha256: str
    supersedes: list[str] = Field(default_factory=list)
    edit_history_available: bool = False


class ThreadEvidence(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    schema_version: str = VERSION
    topic_id: int
    url: str
    title: str
    topic_title: str
    selected_post_id: int
    buyer_actor_id: int | None
    original_at: AwareDatetime | None
    last_substantive_buyer_at: AwareDatetime | None
    observed_at: AwareDatetime
    posts: list[PostEvidence]
    selected_post_ids: list[int]
    expected_post_ids: list[int]
    complete: bool
    document_type: str
    intent: str
    description: str
    vacancy_state: str = 'unknown'
    closure_post_id: int | None = None
    thread_locked: bool = False
    issues: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    payload_sha256: str
    revisions: list[BuyerRevision] = Field(default_factory=list)
    current_terms_post_ids: list[int] = Field(default_factory=list)
    current_terms_revision_id: str | None = None
    terms_state: str = 'legacy'


def _buyer_revisions(posts, selected, topic_id, *, complete):
    """Native post order supplies sequence; missing/contradictory time stays unknown.

    One captured edited body cannot reconstruct its prior text or prove that an
    edit was substantive. Capture/edit metadata alone never refreshes demand.
    """
    revisions = []
    current_posts = [p.post_id for p in posts]
    current_id = None
    state = 'original'
    previous_time = selected.created_at
    latest_terms_time = None
    for post in posts:
        kind = ('original' if post.post_id == selected.post_id else
                'scope_changed' if _declared(_SCOPE_CHANGE, post.text) else
                'budget_changed' if _declared(_BUDGET_CHANGE, post.text) else
                post.status_signal if post.status_signal in {'closed', 'reopened', 'conflicting'} else None)
        if kind is None:
            continue
        text_hash = hashlib.sha256(' '.join(post.text.split()).casefold().encode()).hexdigest()
        identity = [topic_id, selected.post_id, post.post_id, post.actor_id, kind, text_hash]
        revision_id = hashlib.sha256(json.dumps(identity, separators=(',', ':')).encode()).hexdigest()
        terms = kind in {'original', 'scope_changed', 'budget_changed'}
        supersession_supported = (complete and terms and kind != 'original' and state != 'ambiguous'
            and post.created_at is not None and previous_time is not None and post.created_at >= previous_time)
        revisions.append(BuyerRevision(revision_id=revision_id, post_id=post.post_id,
            actor_id=post.actor_id, kind=kind, created_at=post.created_at, edited_at=post.edited_at,
            material_text_sha256=text_hash, supersedes=[current_id] if supersession_supported and current_id else []))
        if kind == 'original':
            current_id = revision_id
        elif terms:
            # Provider post numbers establish sequence even at equal timestamps;
            # dates that run backwards cannot support supersession.
            if post.created_at is None or previous_time is None or post.created_at < previous_time:
                state = 'ambiguous'
            elif state != 'ambiguous':
                state = kind
            previous_time = post.created_at
            current_id = revision_id
            latest_terms_time = post.created_at
            current_posts = [post.post_id] if _declared(_BUDGET_CHANGE, post.text) else []
    if latest_terms_time is not None and any(
        p.post_id not in current_posts and _NATIVE_MONEY.search(p.text)
        and p.edited_at is not None and p.edited_at > latest_terms_time for p in posts
    ):
        state = 'ambiguous'
    if not complete:
        state = 'incomplete'
    if state in {'ambiguous', 'incomplete'}:
        current_posts = []
        current_id = None
        revisions = [r.model_copy(update={'supersedes': []}) for r in revisions]
    return dict(revisions=revisions, current_terms_post_ids=current_posts,
                current_terms_revision_id=current_id, terms_state=state)


def topic_url(url):
    from .provenance import sanitize_url
    parts = urlsplit(sanitize_url(url))
    if parts.scheme != 'https' or parts.hostname not in HOSTS or parts.port not in (None, 443):
        raise ValueError('unsupported_community_url')
    seg = [s for s in parts.path.split('/') if s]
    if not seg or seg[0] != 't' or len(seg) not in (2, 3, 4):
        raise ValueError('exact_topic_required')
    short = re.fullmatch(r'[1-9]\d{0,15}(?:\.json)?', seg[1]) is not None
    if short and len(seg) == 4:
        raise ValueError('invalid_post_route')
    value = (seg[1] if short or len(seg) == 2 else seg[2]).removesuffix('.json')
    if not re.fullmatch(r'[1-9]\d{0,15}', value):
        raise ValueError('exact_topic_required')
    post = seg[2] if short and len(seg) == 3 else seg[3] if len(seg) == 4 else None
    if post is not None and not re.fullmatch(r'[1-9]\d{0,15}', post):
        raise ValueError('invalid_post_route')
    return f'https://{parts.hostname}/t/{value}', int(value)


def _integer(value):
    return value if type(value) is int and value > 0 else None


def _route_post_number(url):
    topic_url(url)
    segments = [part for part in urlsplit(url).path.split('/') if part]
    short = re.fullmatch(r'[1-9]\d{0,15}(?:\.json)?', segments[1]) is not None
    value = segments[2] if short and len(segments) == 3 else segments[3] if len(segments) == 4 else None
    return int(value) if value else None


def workflow_request(thread):
    return thread.intent == 'buyer' and bool(_WORKFLOW_TOOL.search(thread.title+'\n'+thread.description)) and bool(_WORKFLOW_WORK.search(thread.description))


def bounded_workflow_probe(url, title):
    """Admission to a bounded read, never acceptance of a vacancy or skill claim."""
    try:
        topic_url(url)
        return bool(re.search(r'\b(?:n8n|whatsapp|workflow|scenario|webhook|sheets|airtable)\b|make\.com', title, re.I))
    except ValueError:
        return False


def _date(value, observed_at):
    try:
        date = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return date if date.tzinfo is not None and date <= observed_at else None
    except (TypeError, ValueError):
        return None


class _PostText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.parts, self.quoted = [], [], []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        parent_skip = self.stack[-1][1] if self.stack else False
        parent_quote = self.stack[-1][2] if self.stack else False
        hidden = ('hidden' in attrs or attrs.get('aria-hidden') == 'true'
                  or re.search(r'display\s*:\s*none|visibility\s*:\s*hidden', attrs.get('style') or '', re.I))
        quote = parent_quote or tag == 'blockquote' or (tag == 'aside' and 'quote' in (attrs.get('class') or '').split())
        skip = parent_skip or hidden or tag in {'script', 'style', 'template', 'pre', 'code'}
        if tag in {'br', 'hr', 'img', 'input', 'meta', 'link', 'wbr', 'source'}:
            if not skip and not quote and tag in {'br', 'hr'}:
                self.parts.append('\n')
            return
        self.stack.append((tag, skip, quote))
        if not skip and not quote and tag in {'p', 'div', 'li', 'h1', 'h2', 'h3', 'h4'}:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        for index in range(len(self.stack)-1, -1, -1):
            if self.stack[index][0] == tag:
                _, skip, quote = self.stack[index]
                del self.stack[index:]
                if not skip and not quote and tag in {'p', 'div', 'li', 'h1', 'h2', 'h3', 'h4'}:
                    self.parts.append('\n')
                break

    def handle_data(self, data):
        skip, quote = self.stack[-1][1:] if self.stack else (False, False)
        if not skip:
            (self.quoted if quote else self.parts).append(data)

    def result(self):
        text = '\n'.join(' '.join(line.split()) for line in ''.join(self.parts).splitlines() if line.strip())
        quoted = ' '.join(' '.join(self.quoted).split())
        return text, [quoted] if quoted else []


def _thread_records(pages, url, *, max_posts, max_bytes):
    """Validate a native prefix before its required buyer/root has been fetched."""
    from .provenance import sanitize_payload
    public, topic_id = topic_url(url)
    if not isinstance(pages, list) or not 1 <= len(pages) <= 10:
        raise ValueError('invalid_thread_pages')
    pages = [sanitize_payload(page) for page in pages]
    encoded = json.dumps(pages, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    if len(encoded) > max_bytes:
        raise ValueError('thread_body_budget_reached')
    first = pages[0]
    if not isinstance(first, dict) or _integer(first.get('id')) != topic_id or first.get('archetype', 'regular') != 'regular':
        raise ValueError('thread_identity_mismatch')
    title = str(first.get('title') or '').strip()
    if not title or len(title) > 1000:
        raise ValueError('invalid_thread_title')
    stream = first.get('post_stream')
    if not isinstance(stream, dict) or not isinstance(stream.get('stream'), list):
        raise ValueError('missing_thread_stream')
    expected = stream['stream']
    if not expected or len(expected) > 5000 or any(_integer(i) is None for i in expected) or len(set(expected)) != len(expected):
        raise ValueError('invalid_thread_stream')
    records = {}
    for page in pages:
        if not isinstance(page, dict) or ('id' in page and _integer(page['id']) != topic_id):
            raise ValueError('thread_identity_mismatch')
        block = page.get('post_stream')
        batch = block.get('posts') if isinstance(block, dict) else None
        if not isinstance(batch, list):
            raise ValueError('invalid_thread_posts')
        for raw in batch:
            if not isinstance(raw, dict) or _integer(raw.get('id')) is None or raw['id'] not in expected or _integer(raw.get('topic_id')) != topic_id:
                raise ValueError('post_identity_mismatch')
            if raw['id'] in records and records[raw['id']] != raw:
                raise ValueError('post_revision_changed_during_capture')
            if _integer(raw.get('post_number')) is None or not isinstance(raw.get('cooked'), str):
                raise ValueError('invalid_post_content')
            records[raw['id']] = raw
    if len(records) > max_posts:
        raise ValueError('thread_post_budget_reached')
    if len({r['post_number'] for r in records.values()}) != len(records):
        raise ValueError('duplicate_post_number')
    return public, topic_id, title, expected, records, encoded, pages


def project_thread(pages, url, observed_at, *, selected_post_id=None, max_posts=60, max_bytes=2_000_000, schema_version=VERSION):
    """Project saved provider pages. No networking, freshness substitution or CRM."""
    if observed_at.tzinfo is None:
        raise ValueError('observation_time_requires_timezone')
    if schema_version not in {VERSION, LEGACY_VERSION}:
        raise ValueError('unsupported_thread_schema')
    if selected_post_id is not None and _integer(selected_post_id) is None:
        raise ValueError('invalid_selected_post_id')
    public, topic_id, title, expected, records, encoded, pages = _thread_records(
        pages, url, max_posts=max_posts, max_bytes=max_bytes)
    first = pages[0]
    issues = []
    if set(records) != set(expected):
        issues.append('thread_pagination_incomplete')
    root = next((r for r in records.values() if r.get('post_number') == 1), None)
    if root is None:
        raise ValueError('original_post_missing')
    route_number = _route_post_number(url)
    chosen = (records.get(selected_post_id) if selected_post_id is not None else
              next((r for r in records.values() if r['post_number'] == route_number), None) if route_number else root)
    if chosen is None:
        raise ValueError('selected_post_missing')
    if route_number and chosen['post_number'] != route_number:
        raise ValueError('selected_post_route_mismatch')
    author = _integer(root.get('user_id'))
    buyer = _integer(chosen.get('user_id'))
    selected_number = _integer(chosen.get('post_number'))
    if selected_number is None:
        raise ValueError('invalid_post_number')
    posts = []
    for raw in sorted(records.values(), key=lambda r: r.get('post_number', 0)):
        number = _integer(raw.get('post_number'))
        if number is None or not isinstance(raw.get('cooked'), str):
            raise ValueError('invalid_post_content')
        parser = _PostText(); parser.feed(raw['cooked']); parser.close()
        text, quotes = parser.result()
        uid = _integer(raw.get('user_id'))
        unavailable = raw.get('hidden') is True or bool(raw.get('deleted_at')) or raw.get('wiki') is True
        if unavailable:
            text = ''; issues.append('unattributable_post_content')
        system = raw.get('post_type', 1) != 1
        role = ('system' if system else 'unknown' if uid is None or unavailable else
                'topic_author' if uid == author else 'reply_seller' if _SELLER.search(text) else
                'other_buyer' if _DEMAND.search(text) and not _FREE.search(text) else 'reply_other')
        created, edited = _date(raw.get('created_at'), observed_at), _date(raw.get('updated_at'), observed_at)
        if created is None:
            issues.append('post_time_unknown')
        if created and edited and edited < created:
            edited = None; issues.append('post_edit_time_invalid')
        posts.append(PostEvidence(post_id=raw['id'], post_number=number, actor_id=uid, actor_role=role,
            created_at=created, edited_at=edited, text=text, quotes=quotes,
            cooked_sha256=hashlib.sha256(raw['cooked'].encode()).hexdigest(),
            reply_to_post_number=_integer(raw.get('reply_to_post_number')),
            status_signal='conflicting' if _CLOSE.search(text) and _REOPEN.search(text) else
                'closed' if _CLOSE.search(text) else 'reopened' if _REOPEN.search(text) else 'unknown'))
    if len({p.post_number for p in posts}) != len(posts):
        raise ValueError('duplicate_post_number')
    selected = next(p for p in posts if p.post_id == chosen['id'])
    primary = (title + '\n' if selected_number == 1 else '') + selected.text
    seller = bool(_SELLER.search(primary)) and not bool(_DEMAND.search(selected.text))
    hiring_category = (urlsplit(public).hostname == 'community.make.com' and first.get('category_id') == 74
                       or urlsplit(public).hostname == 'community.n8n.io' and first.get('category_id') == 13)
    category_request = hiring_category and bool(_HELP.search(selected.text))
    demand = (bool(_DEMAND.search(primary)) or category_request) and not bool(_FREE.search(primary)) and not seller
    if selected.actor_role in {'unknown', 'system'} or buyer is None:
        document, intent = 'unknown', 'unknown'
    elif seller:
        document, intent = 'seller_service', 'seller'
    elif demand:
        document, intent = 'buyer_request', 'buyer'
    else:
        document, intent = 'discussion', 'discussion'
    selected_posts = [p for p in posts if p.actor_id == buyer and p.actor_role not in {'unknown', 'system'}
        and (selected_number == 1 or p.post_id == selected.post_id or p.reply_to_post_number == selected_number)]
    intent_updates = [p for p in selected_posts if _FREE.search(p.text) or _DEMAND.search(p.text)]
    if intent == 'buyer' and intent_updates and _FREE.search(intent_updates[-1].text):
        document, intent = 'discussion', 'discussion'
    # Only substantive text owned by the selected demand actor enters the Job.
    description = '\n'.join(p.text for p in selected_posts if p.text)
    closure, state = None, 'unknown'
    for post in selected_posts if intent == 'buyer' else []:
        if post.status_signal == 'conflicting':
            state = 'unknown'; issues.append('conflicting_buyer_status')
        elif post.status_signal == 'closed':
            closure, state = post.post_id, 'explicitly_closed'
        elif post.status_signal == 'reopened':
            closure, state = None, 'unknown'
    complete = not issues
    # Partial capture may omit a later reopening; retain closure evidence without
    # claiming the current state is completely known.
    if not complete:
        state = 'unknown'
    substantive = (_DEMAND, _CLOSE, _REOPEN, _SCOPE) if schema_version == LEGACY_VERSION else (_DEMAND, _CLOSE, _REOPEN)
    last = max((p.created_at for p in selected_posts if intent == 'buyer' and p.created_at and p.text and
                (p.post_id == selected.post_id or any(pattern.search(p.text) for pattern in substantive)
                 or schema_version == VERSION and any(_declared(pattern, p.text) for pattern in (_SCOPE_CHANGE, _BUDGET_CHANGE)))), default=None)
    revision_fields = (_buyer_revisions(selected_posts, selected, topic_id, complete=complete)
        if schema_version == VERSION and intent == 'buyer' else {})
    return ThreadEvidence(schema_version=schema_version, topic_id=topic_id, url=public if selected_number == 1 else public+'/'+str(selected_number),
        title=title if selected_number == 1 else 'Buyer request: '+selected.text.split('\n')[0][:140], topic_title=title,
        selected_post_id=selected.post_id, buyer_actor_id=buyer if intent == 'buyer' else None, original_at=selected.created_at,
        last_substantive_buyer_at=last, observed_at=observed_at, posts=posts,
        selected_post_ids=[p.post_id for p in selected_posts], expected_post_ids=expected,
        complete=complete, document_type=document, intent=intent, description=description,
        vacancy_state=state, closure_post_id=closure, thread_locked=first.get('closed') is True,
        issues=sorted(set(issues)), caveats=['paid_intent_unconfirmed'] if intent == 'buyer' and category_request
            and not _DEMAND.search(primary) and not any(_DEMAND.search(p.text) for p in selected_posts) else [],
        payload_sha256=hashlib.sha256(encoded).hexdigest(), **revision_fields)


def observation_thread(observation):
    """Re-derive the actor projection from native saved pages, never trust labels."""
    if observation.source != 'community_thread' or not observation.captured_at:
        return None
    raw = observation.raw_payload
    try:
        version = raw['community_thread']['schema_version']
        result = project_thread(raw['community_pages'], observation.original_url, observation.captured_at,
                                selected_post_id=raw.get('community_selected_post_id'), max_posts=200, schema_version=version)
        if _thread_payload(result) != raw['community_thread'] or observation.job is None:
            return None
        if (observation.job.title, observation.job.description, observation.job.url) != (result.title, result.description, result.url):
            return None
        return result
    except (KeyError, ValueError, TypeError, AttributeError):
        return None


def bind_pay_actor(candidate, observation):
    """Bind normalized buyer text offsets to its original native post field."""
    thread = observation_thread(observation)
    scope_kind = 'community_benefit' if candidate.scope == 'role_benefit_unverified' else 'community_request'
    if candidate.scope in {'non_opportunity_document', 'other_assignment', 'non_pay_context', 'unattributed_amount'}:
        return
    if thread is None or thread.intent != 'buyer':
        candidate.scope = 'non_opportunity_document'
        return
    if candidate.source_field == 'title':
        selected = next(p for p in thread.posts if p.post_id == thread.selected_post_id)
        if selected.post_number == 1:
            candidate.actor = f'forum_actor:{thread.buyer_actor_id}'
            candidate.scope = f'{scope_kind}:{thread.topic_id}:{thread.selected_post_id}'
            candidate.structured_path = '/community_pages/0/title'
            candidate.structured_payload_sha256 = hashlib.sha256(observation.raw_payload['community_pages'][0]['title'].encode()).hexdigest()
            _bind_current_terms(candidate, thread, selected.post_id)
        else:
            candidate.scope = 'unattributed_thread_copy'
        return
    offset = 0
    for post in thread.posts:
        if post.post_id not in thread.selected_post_ids or not post.text:
            continue
        end = offset + len(post.text)
        if (candidate.source_span_start is not None and candidate.source_span_end is not None
                and offset <= candidate.source_span_start < candidate.source_span_end <= end):
            candidate.actor = f'forum_actor:{post.actor_id}'
            candidate.scope = f'{scope_kind}:{thread.topic_id}:{thread.selected_post_id}'
            for page_index, page in enumerate(observation.raw_payload['community_pages']):
                for post_index, native in enumerate(page['post_stream']['posts']):
                    if native['id'] == post.post_id:
                        candidate.structured_path = f'/community_pages/{page_index}/post_stream/posts/{post_index}/cooked'
                        candidate.structured_payload_sha256 = post.cooked_sha256
                        _bind_current_terms(candidate, thread, post.post_id)
                        return
        offset = end + 1
    candidate.scope = 'unattributed_thread_copy'


def _bind_current_terms(candidate, thread, post_id):
    if thread.schema_version != VERSION:
        return
    if thread.terms_state in {'ambiguous', 'incomplete'}:
        candidate.scope = 'unresolved_thread_terms'
        candidate.selection_reasons.append('buyer_terms_chronology_unresolved')
    elif post_id not in thread.current_terms_post_ids:
        candidate.scope = 'historical_thread_terms'
        candidate.selection_reasons.append('retained_superseded_buyer_terms')
    elif thread.terms_state != 'original':
        candidate.scope += ':revision:' + thread.current_terms_revision_id


def _thread_payload(projected):
    return projected.model_dump(mode='json', exclude=_REVISION_FIELDS if projected.schema_version == LEGACY_VERSION else set())


async def hydrate_community_topic(url, original, *, client, budget, selected_post_id=None):
    """Read only this public topic, through the existing send-time budget/DNS gate."""
    from datetime import timezone
    from urllib.parse import urlencode
    import httpx
    from ..bounded_transport import RequestDeferred
    from ..run_context import current_run
    from .hydration import HydrationResult, _request
    public, topic_id = topic_url(url)
    route_number = _route_post_number(url)
    if selected_post_id is not None and _integer(selected_post_id) is None:
        raise ValueError('invalid_selected_post_id')
    now = current_run().as_of if current_run() else datetime.now(timezone.utc)
    pages, request_url, total_bytes, attempted = [], public+'.json', 0, 0
    max_posts = budget.policy.community_max_posts
    max_pages = budget.policy.community_max_pages
    if type(max_posts) is not int or not 1 <= max_posts <= 200 or type(max_pages) is not int or not 1 <= max_pages <= 10:
        raise ValueError('invalid_community_budget')
    fetched = set()
    error = None
    for page_number in range(max_pages):
        try:
            attempted += 1
            response = await _request(client, budget, request_url)
            if response.status_code in {429, 503}:
                budget.note_retry_after(request_url, response.headers.get('retry-after'))
                error = f'http_{response.status_code}'; break
            if response.status_code != 200:
                error = f'http_{response.status_code}'; break
            total_bytes += len(response.content)
            if total_bytes > budget.policy.max_body_bytes:
                error = 'thread_body_budget_reached'; break
            data = response.json()
            if not isinstance(data, dict) or not isinstance(data.get('post_stream'), dict):
                error = 'invalid_thread_payload'; break
            batch = data['post_stream'].get('posts')
            if not isinstance(batch, list):
                error = 'invalid_thread_payload'; break
            remaining = max_posts - len(fetched)
            data = dict(data, post_stream=dict(data['post_stream'], posts=batch[:remaining]))
            # Validate every page before its contents can supplement the prefix.
            _, _, _, expected, records, _, validated = _thread_records(
                [*pages, data], public, max_posts=max_posts, max_bytes=budget.policy.max_body_bytes)
            if selected_post_id is not None and selected_post_id not in expected:
                raise ValueError('selected_post_not_in_stream')
            pages = validated
            fetched = set(records)
            missing = [i for i in expected if i not in fetched]
            if selected_post_id in missing:
                missing.remove(selected_post_id)
                missing.insert(0, selected_post_id)
            # A root is still necessary to distinguish a buyer reply from a
            # seller-originated topic. Stream order is only retrieval priority;
            # the returned native post number must prove which post is first.
            if not any(r['post_number'] == 1 for r in records.values()) and expected[0] in missing:
                missing.remove(expected[0]); missing.insert(0, expected[0])
            if not missing or len(fetched) >= max_posts:
                break
            take = min(20, max_posts-len(fetched))
            request_url = public+'/posts.json?'+urlencode([('post_ids[]', i) for i in missing[:take]])
        except (httpx.HTTPError, RuntimeError, RequestDeferred) as exc:
            error = str(exc) or type(exc).__name__; break
        except (ValueError, TypeError, KeyError, AttributeError):
            error = 'invalid_thread_payload'; break
    if not pages:
        return HydrationResult(state='deferred' if error and ('budget' in error or 'cooldown' in error or error in {'http_429', 'http_503'}) else 'failed',
            source='community_thread', request_url=request_url, public_url=public, error=error or 'thread_unavailable')
    try:
        projected = project_thread(pages, url, now, selected_post_id=selected_post_id, max_posts=max_posts,
                                   max_bytes=budget.policy.max_body_bytes)
    except ValueError as exc:
        return HydrationResult(state='partial', source='community_thread', request_url=request_url,
            public_url=url, captured_at=now, error=error or str(exc),
            payload={'community_pages': pages, 'community_selected_post_id': selected_post_id,
                     'community_coverage': {'fetched_posts':len(fetched), 'requests_attempted':attempted,
                                            'successful_pages':len(pages), 'bytes':total_bytes}})
    if route_number or selected_post_id is not None:
        selected_post_id = projected.selected_post_id
    return thread_outcome(projected, pages, selected_post_id=selected_post_id, error=error, request_url=request_url,
                          coverage={'fetched_posts':len(fetched), 'expected_posts':len(projected.expected_post_ids),
                                    'requests_attempted':attempted, 'successful_pages':len(pages), 'bytes':total_bytes})


def thread_outcome(projected, pages, *, selected_post_id=None, error=None, request_url='', coverage=None):
    """Same pure projection for live bounded hydration and saved-page review."""
    from ..models import Job
    from .models import SourceKind
    from .hydration import HydrationResult
    from .provenance import sanitize_payload
    kind = SourceKind.ORIGINAL_EMPLOYER if projected.intent == 'buyer' else SourceKind.REPUTABLE_BOARD
    job = Job(source='community_thread', title=projected.title, company=f'Community requester {projected.buyer_actor_id or "unknown"}',
        url=projected.url, description=projected.description, posted_at=projected.original_at,
        is_remote=bool(re.search(r'\bremote\b', projected.description, re.I)))
    return HydrationResult(state='complete' if projected.complete and not error else 'partial', job=job,
        source='community_thread', request_url=request_url, public_url=projected.url,
        payload={'community_pages': [sanitize_payload(page) for page in pages],
                 'community_selected_post_id': selected_post_id, 'community_thread': _thread_payload(projected),
                 'community_coverage': coverage or {'fetched_posts':len(projected.posts), 'expected_posts':len(projected.expected_post_ids),
                                                  'saved_pages':len(pages)}},
        error=error or (None if projected.complete else projected.issues[0] if projected.issues else 'thread_evidence_incomplete'), identity_state='exact', captured_at=projected.observed_at,
        vacancy_state=projected.vacancy_state, source_kind=kind)


def saved_thread_observation(outcome):
    from .models import ListingObservation
    thread = outcome.payload['community_thread']
    scope = f"{thread['payload_sha256']}:{thread['topic_id']}:{thread['selected_post_id']}"
    return ListingObservation(observation_id='community:'+hashlib.sha256(scope.encode()).hexdigest()[:24],
        source='community_thread', job=outcome.job, raw_payload=outcome.payload, original_url=outcome.public_url,
        normalized_url=outcome.public_url, publisher_domain=urlsplit(outcome.public_url).hostname or '',
        source_kind=outcome.source_kind, source_confidence=.9, origin='hydration',
        captured_at=outcome.captured_at, content_hash=outcome.payload['community_thread']['payload_sha256'],
        content_state=outcome.state, identity_state=outcome.identity_state, truncated=outcome.state!='complete',
        vacancy_state=outcome.vacancy_state, retrieval_error=outcome.error, extraction_version=thread['schema_version'])
