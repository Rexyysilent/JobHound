"""Narrow native contracts for public Turing and micro1 role leaves.

No JavaScript execution, invented endpoint, application form or account access.
Only complete role-scoped native data can supplement a discovery observation.
"""
from dataclasses import dataclass,field
from datetime import datetime,timezone
from decimal import Decimal,InvalidOperation
import hashlib
from html.parser import HTMLParser
import json
import re
from urllib.parse import urlsplit

from ..models import Job
from ..document_regions import role_control
from .models import SourceKind

class NativeError(ValueError):pass

@dataclass
class Node:
    tag:str
    attrs:dict
    parent:object=None
    children:list=field(default_factory=list)

    @property
    def visible(self):
        if self.tag in {'script','style','noscript','iframe','template','blockquote'}:return False
        if 'hidden' in self.attrs or self.attrs.get('aria-hidden')=='true':return False
        style=re.sub(r'\s+','',self.attrs.get('style','')).lower()
        if 'display:none' in style or 'visibility:hidden' in style:return False
        return self.parent is None or self.parent.visible

    @property
    def role_visible(self):
        node=self
        while node is not None:
            if role_control(node.tag,node.attrs):
                return False
            node=node.parent
        return self.visible

    def text(self,*,raw=False,role_only=True):
        if not raw:
            if not self.visible:return ''
            if role_only and not self.role_visible:return '\n'
        value=''.join(c if isinstance(c,str) else c.text(raw=raw,role_only=role_only) for c in self.children)
        if not raw and self.tag in {'div','p','li','h1','h2','h3','h4','ul','ol','br','section'}:
            return '\n'+value+'\n'
        return value

class Tree(HTMLParser):
    def __init__(self,body):
        super().__init__();self.root=Node('root',{});self.stack=[self.root];self.nodes=[]
        if len(body.encode('utf-8'))>2_000_000:raise NativeError('native_body_limit')
        self.feed(body)
    def handle_starttag(self,tag,attrs):
        if len(self.nodes)>=6000 or len(self.stack)>=64:raise NativeError('native_structure_limit')
        node=Node(tag,dict(attrs),self.stack[-1]);self.stack[-1].children.append(node);self.nodes.append(node)
        if tag not in {'area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'}:self.stack.append(node)
    def handle_startendtag(self,tag,attrs):
        self.handle_starttag(tag,attrs);self.handle_endtag(tag)
    def handle_endtag(self,tag):
        for i in range(len(self.stack)-1,0,-1):
            if self.stack[i].tag==tag:self.stack=self.stack[:i];break
    def handle_data(self,data):self.stack[-1].children.append(data)

def clean(text):return re.sub(r'[ \t\r\f\v]+',' ',text).strip()
def name(text):return re.sub(r'\s+',' ',text).strip().casefold()

def excluded_sections(tree):
    """Record outer omitted regions, bound to text plus the full native-body hash."""
    result=[]
    for node in tree.nodes:
        if node.visible and not node.role_visible and (node.parent is None or node.parent.role_visible):
            text=node.text(role_only=False)
            result.append(dict(tag=node.tag,aria_role=node.attrs.get('role'),
                text_sha256=hashlib.sha256(text.encode()).hexdigest(),text_characters=len(text)))
    return result

def role_route(url):
    try:
        parts=urlsplit(url)
        port=parts.port
    except ValueError:return None
    if parts.scheme!='https' or parts.username or parts.password or parts.query or parts.fragment:return None
    if port not in {None,443}:return None
    if parts.hostname=='work.turing.com':
        match=re.fullmatch(r'/r/([A-Za-z0-9_-]{6,64})/?',parts.path)
        return ('turing',match[1]) if match else None
    if parts.hostname=='jobs.micro1.ai':
        match=re.fullmatch(r'/post/([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})/?',parts.path)
        return ('micro1',match[1]) if match else None
    return None

def _required_sections(text,labels):
    spans={}
    for label in labels:
        matches=list(re.finditer(r'(?im)^\s*'+re.escape(label)+r'\s*[:\-–—]?\s*(?:\n|$)',text))
        if len(matches)!=1:raise NativeError('native_sections_incomplete')
        spans[label]=matches[0].span()
    ordered=sorted((start,end,label) for label,(start,end) in spans.items())
    for i,(start,end,label) in enumerate(ordered):
        stop=ordered[i+1][0] if i+1<len(ordered) else len(text)
        if len(text[end:stop].strip())<8:raise NativeError('native_sections_incomplete')
    return spans

def _turing(tree,url,role_id):
    # Apply only explicit completed native RS/RC fragment moves. An inner role
    # fragment may move into an unfinished outer boundary: it stays hidden until
    # that outer boundary also completes. No script code is executed.
    for script in [n for n in tree.nodes if n.tag=='script' and 'src' not in n.attrs]:
        for kind,first,second in re.findall(r'\$(RS|RC)\("([SPB]:[0-9a-f]+)","([SPB]:[0-9a-f]+)"\)',script.text(raw=True)):
            fragment,placeholder=(first,second) if kind=='RS' else (second,first)
            if not fragment.startswith('S:') or not placeholder.startswith('P:' if kind=='RS' else 'B:'):continue
            sources=[n for n in tree.nodes if n.attrs.get('id')==fragment and n.tag=='div' and 'hidden' in n.attrs]
            targets=[n for n in tree.nodes if n.attrs.get('id')==placeholder and n.tag=='template']
            if len(sources)!=1 or len(targets)!=1:continue
            source,target=sources[0],targets[0];parent=target.parent;ancestor=parent
            while ancestor is not None and ancestor is not source:ancestor=ancestor.parent
            if ancestor is source:continue
            index=parent.children.index(target);parent.children[index:index+1]=source.children
            for child in source.children:
                if isinstance(child,Node):child.parent=parent
            source.children=[];source.parent.children.remove(source)
            source.attrs.pop('id');target.attrs.pop('id')
    heads=[n for n in tree.nodes if n.tag=='h1' and n.role_visible]
    if len(heads)!=1 or not name(heads[0].text()):raise NativeError('native_role_heading_missing')
    title=clean(heads[0].text())
    metadata=[n.attrs.get('content') for n in tree.nodes if n.tag=='meta' and n.role_visible and n.attrs.get('property')=='og:url']
    valid={f'https://work.turing.com/r/{role_id}',f'https://developers.turing.com/r/{role_id}'}
    if len(metadata)!=1 or metadata[0].rstrip('/') not in valid:raise NativeError('native_role_identity_mismatch')
    cards=[]
    for card in tree.nodes:
        if card.attrs.get('data-slot')!='card' or not card.role_visible:continue
        headers=[n for n in card.children if isinstance(n,Node) and n.attrs.get('data-slot')=='card-header']
        if len(headers)==1 and name(headers[0].text())=='overview':cards.append(card)
    if len(cards)!=1:raise NativeError('native_role_card_missing')
    contents=[n for n in cards[0].children if isinstance(n,Node) and n.attrs.get('data-slot')=='card-content' and n.role_visible]
    if len(contents)!=1:raise NativeError('native_role_card_missing')
    # The heading and card must belong to the same native column, not a sidebar.
    ancestor=cards[0].parent
    while ancestor is not None and ancestor is not heads[0].parent.parent:ancestor=ancestor.parent
    if ancestor is None:raise NativeError('native_role_card_scope_mismatch')
    text=clean(contents[0].text())
    start=re.search(r'(?im)^\s*Role Overview\s*:\s*(?:\n|$)',text)
    if start is None:raise NativeError('native_sections_incomplete')
    text=text[start.start():].strip()
    sections=_required_sections(text,('Role Overview','Key Qualifications','Description','Education & Experience','Offer Details','Evaluation Process'))
    badges=clean(heads[0].parent.text())
    location='Remote' if re.search(r'(?im)^\s*Remote\s*$',badges) else ''
    return Job(source='turing_role',url=url,title=title,company='Turing',description=text,location=location,is_remote=location=='Remote'),dict(
        role_id=role_id,sections=sections,native_role_text=text,native_role_text_sha256=hashlib.sha256(text.encode()).hexdigest(),
        excluded_document_sections=excluded_sections(tree),
        native_status='unknown',role_metadata_url=metadata[0]),'unknown'

def _flight(tree):
    """Decode bounded public Flight JSON/text records; never execute script code."""
    chunks=[]
    for node in tree.nodes:
        if node.tag!='script' or 'src' in node.attrs:continue
        script=node.text(raw=True).strip().removesuffix(';')
        if not script.startswith('self.__next_f.push([1,'):continue
        try:pair=json.loads(script.removeprefix('self.__next_f.push(').removesuffix(')'))
        except ValueError:raise NativeError('native_stream_invalid_json') from None
        if len(pair)!=2 or pair[0]!=1 or not isinstance(pair[1],str):raise NativeError('native_stream_invalid_record')
        chunks.append(pair[1])
    if not chunks:raise NativeError('native_js_shell')
    stream=''.join(chunks).encode('utf-8');offset=0;records={};decoder=json.JSONDecoder()
    while offset<len(stream):
        if stream[offset:offset+1]==b'\n':offset+=1;continue
        match=re.match(rb'([0-9a-f]+):',stream[offset:])
        if match is None:raise NativeError('native_stream_invalid_record')
        key=match[1].decode();offset+=match.end()
        if stream[offset:offset+1]==b'T':
            length=re.match(rb'T([0-9a-f]+),',stream[offset:])
            if length is None:raise NativeError('native_stream_invalid_text')
            size=int(length[1],16);offset+=length.end()
            if size>100000 or offset+size>len(stream):raise NativeError('native_stream_truncated')
            try:value=stream[offset:offset+size].decode('utf-8')
            except UnicodeError:raise NativeError('native_stream_invalid_text') from None
            offset+=size
        elif stream[offset:offset+1] in {b'[',b'{',b'"'} or stream[offset:offset+4]==b'null':
            try:
                suffix=stream[offset:].decode('utf-8');value,end=decoder.raw_decode(suffix)
            except (ValueError,UnicodeError,RecursionError):raise NativeError('native_stream_invalid_json') from None
            offset+=len(suffix[:end].encode('utf-8'))
        else:
            # Module/preload records are not role evidence.
            end=stream.find(b'\n',offset)
            offset=len(stream) if end<0 else end+1
            continue
        if key in records:raise NativeError('native_stream_duplicate_record')
        records[key]=value
        if len(records)>500:raise NativeError('native_stream_record_limit')
    return records

def _dictionaries(value,depth=0):
    if depth>48:raise NativeError('native_stream_structure_limit')
    if isinstance(value,dict):
        yield value
        for child in value.values():yield from _dictionaries(child,depth+1)
    elif isinstance(value,list):
        for child in value:yield from _dictionaries(child,depth+1)

def _micro1(tree,url,role_id):
    records=_flight(tree)
    components=[d for value in records.values() for d in _dictionaries(value) if {'id','data','error','loading'}<=set(d)]
    if len(components)!=1:raise NativeError('native_role_component_missing')
    component=components[0];data=component['data']
    if component['id']!=role_id or not isinstance(data,dict) or data.get('client_job_id')!=role_id:
        raise NativeError('native_role_identity_mismatch')
    if component['error'] is not None or component['loading'] is not False:raise NativeError('native_role_not_loaded')
    title=data.get('job_role_name');status=data.get('job_status');description=data.get('job_description')
    if not isinstance(title,str) or not name(title) or not isinstance(status,str) or not status:
        raise NativeError('native_role_fields_missing')
    if isinstance(description,str) and re.fullmatch(r'\$[0-9a-f]+',description):description=records.get(description[1:])
    if not isinstance(description,str):raise NativeError('native_description_missing')
    role_tree=Tree(description);text=clean(role_tree.root.text())
    title_match=re.search(r'(?im)^\s*Role Title:\s*(.+)$',text)
    if title_match is None or name(title_match[1])!=name(title):raise NativeError('native_description_identity_mismatch')
    sections=_required_sections(text,('Scope of Work','Preferred Qualifications'))
    skills=data.get('required_skills')
    if not isinstance(skills,list) or not skills or len(skills)>50 or any(not isinstance(s,str) or not s.strip() or len(s)>300 for s in skills):
        raise NativeError('native_required_skills_missing')
    client=data.get('client_details')
    if not isinstance(client,dict) or name(str(client.get('client_name') or ''))!='micro1':raise NativeError('native_publisher_identity_mismatch')
    # Only role fields enter description. Forms, referrals and company prose do not.
    text+='\nRequired Skills:\n'+'\n'.join(skills)
    pay=data.get('ideal_hourly_rate');money=None
    if pay is not None:
        if not isinstance(pay,dict) or set(pay)!={'min','max'}:raise NativeError('native_pay_malformed')
        try:
            if any(type(v) not in {int,float} for v in pay.values()):raise InvalidOperation
            low,high=(Decimal(str(pay[k])) for k in ('min','max'))
            if not low.is_finite() or not high.is_finite() or not 0<low<=high:raise InvalidOperation
        except InvalidOperation:raise NativeError('native_pay_malformed') from None
        # This exact template renders these role fields with a dollar/hour label.
        text+=f'\nAdvertised pay range: ${low} - ${high}/hour.'
        money=dict(min=str(low),max=str(high),currency_symbol='$',unit='hour',basis='advertised_public_range')
    location_match=re.search(r'(?im)^\s*Location:\s*(.+)$',text)
    location=clean(location_match[1]) if location_match else ''
    published=None
    if isinstance(data.get('create_datetime'),str):
        try:
            parsed=datetime.fromisoformat(data['create_datetime'].replace('Z','+00:00'))
            if parsed.tzinfo is not None:published=parsed
        except ValueError:pass
    return Job(source='micro1_role',url=url,title=clean(title),company='micro1',description=text,location=location,
        is_remote=name(location)=='remote',posted_at=published),dict(role_id=role_id,sections=sections,native_role_text=text,
        native_role_text_sha256=hashlib.sha256(text.encode()).hexdigest(),native_status=status,
        excluded_document_sections=excluded_sections(role_tree),
        advertised_pay=money,publication_date=data.get('create_datetime')),'explicitly_closed' if status=='closed' else 'unknown'

def parse_native_role(body,url,original,*,captured_at=None,from_cache=False):
    """Return None for unrelated hosts; malformed native role content stays partial."""
    parts=urlsplit(url);host=(parts.hostname or '').casefold()
    if host not in {'work.turing.com','jobs.micro1.ai'}:return None
    from .hydration import HydrationResult,_exact_identity
    route=role_route(url);provider='turing' if host=='work.turing.com' else 'micro1'
    source=provider+'_native';when=captured_at or datetime.now(timezone.utc)
    def failed(code,state='partial'):
        return HydrationResult(state=state,source=source,request_url=url,public_url=url,error=code,
            captured_at=when,from_cache=from_cache,source_kind=SourceKind.ORIGINAL_EMPLOYER)
    if route is None:return failed('native_role_route_unsupported','unavailable')
    if role_route(original.url)!=route:return failed('native_original_role_mismatch','unavailable')
    try:
        tree=Tree(body)
        job,payload,vacancy=(_turing if provider=='turing' else _micro1)(tree,url,route[1])
    except (NativeError,RecursionError,UnicodeError,ValueError,TypeError,IndexError,AttributeError) as exc:
        return failed(str(exc) if isinstance(exc,NativeError) else 'native_contract_malformed')
    identity=_exact_identity(original,job,route[1],{'id':route[1]})
    payload.update(native_contract=provider+'-role/v1',native_body_sha256=hashlib.sha256(body.encode()).hexdigest())
    return HydrationResult(state='complete',job=job,source=source,request_url=url,public_url=url,payload=payload,
        identity_state=identity,captured_at=when,from_cache=from_cache,vacancy_state=vacancy,
        application_route_state='account_unknown',source_kind=SourceKind.ORIGINAL_EMPLOYER,
        task_availability_state='advertised_only')
