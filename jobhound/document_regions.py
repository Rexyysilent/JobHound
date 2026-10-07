"""Explicit non-role HTML regions; original provider payloads stay separate."""
from html import unescape
from html.parser import HTMLParser
import re

ROLE_CONTROL_TAGS={'form','fieldset','legend','label','input','select','option',
                   'optgroup','datalist','textarea','button','nav'}
ROLE_CONTROL_ARIA={'form','navigation','listbox','combobox','radiogroup','textbox','button','menu','menuitem'}
_VOID={'area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'}


def role_control(tag,attrs):
    return tag in ROLE_CONTROL_TAGS or bool(set((attrs.get('role') or '').casefold().split()) & ROLE_CONTROL_ARIA)


class _RoleMarkup(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.stack=[];self.output=[];self.nodes=0

    @property
    def omitted(self):return bool(self.stack and self.stack[-1][1])

    def handle_starttag(self,tag,attrs):
        self.nodes+=1
        if self.nodes>6000 or len(self.stack)>=64:raise ValueError('role_html_structure_limit')
        parent=self.omitted
        omitted=parent or role_control(tag,dict(attrs))
        if omitted and not parent:self.output.append('\n')
        if not omitted:self.output.append(self.get_starttag_text())
        if tag not in _VOID:self.stack.append((tag,omitted))

    def handle_startendtag(self,tag,attrs):
        self.handle_starttag(tag,attrs)
        if tag not in _VOID:self.handle_endtag(tag)

    def handle_endtag(self,tag):
        before=self.omitted
        if not before and tag not in ROLE_CONTROL_TAGS:self.output.append('</'+tag+'>')
        for index in range(len(self.stack)-1,-1,-1):
            if self.stack[index][0]==tag:
                if before and not self.stack[index][1]:raise ValueError('role_html_control_boundary_uncertain')
                self.stack=self.stack[:index];break
        if before and not self.omitted:self.output.append('\n')

    def handle_data(self,data):
        if not self.omitted:self.output.append(data)

    def handle_entityref(self,name):self.handle_data('&'+name+';')
    def handle_charref(self,name):self.handle_data('&#'+name+';')
    def handle_comment(self,data):
        if not self.omitted:self.output.append('<!--'+data+'-->')
    def handle_decl(self,decl):
        if not self.omitted:self.output.append('<!'+decl+'>')
    def handle_pi(self,data):
        if not self.omitted:self.output.append('<?'+data+'>')


def role_markup(value):
    """Remove explicit UI regions before text conversion, preserving other markup.

    Do not decode escaped examples inside actual markup as live form elements.
    Whole encoded HTML is decoded only while there is no actual markup. An
    unclosed omitted region cannot claim that the remainder was safely captured.
    """
    source=value or ''
    if len(source.encode('utf-8'))>2_000_000:raise ValueError('role_html_body_limit')
    for _ in range(2):
        if re.search(r'</?[a-z][^>]*>',source,re.I) or not re.search(r'&(?:amp;)?lt;/?[a-z]',source,re.I):break
        decoded=unescape(source)
        if decoded==source:break
        source=decoded
    parser=_RoleMarkup();parser.feed(source);parser.close()
    if parser.omitted:raise ValueError('role_html_unclosed_control')
    return ''.join(parser.output)
