"""Restore unambiguous source Separation tint operands dropped by text redaction.

Only page content streams are changed. Strings/comments are tokenized, not searched
as commands. Ambiguous source values and inline-image streams require manual review.
"""
import re
from decimal import Decimal

NUMBER=re.compile(rb'^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$')
SPACE=b'\x00\t\n\x0c\r '
DELIMITERS=b'()<>[]{}/%'


def tokens(data):
    i=0
    while i<len(data):
        if data[i] in SPACE:i+=1;continue
        start=i;c=data[i]
        if c==37:
            while i<len(data) and data[i] not in b'\r\n':i+=1
            continue
        if c==40:
            depth=1;i+=1
            while i<len(data) and depth:
                if data[i]==92:i+=2;continue
                if data[i]==40:depth+=1
                elif data[i]==41:depth-=1
                i+=1
            if depth:raise ValueError('Unterminated PDF string during color audit')
            yield start,i,b'(string)';continue
        if c==60 and data[i:i+2]!=b'<<':
            end=data.find(b'>',i+1)
            if end<0:raise ValueError('Unterminated PDF hex string during color audit')
            i=end+1;yield start,i,b'<string>';continue
        if data[i:i+2] in (b'<<',b'>>'):
            i+=2;yield start,i,data[start:i];continue
        if c==47:
            i+=1
            while i<len(data) and data[i] not in SPACE+DELIMITERS:i+=1
        elif c in DELIMITERS:i+=1
        else:
            while i<len(data) and data[i] not in SPACE+DELIMITERS:i+=1
        value=data[start:i]
        if value==b'BI':raise ValueError('Inline-image content requires manual color review')
        yield start,i,value


def separation_names(page):
    doc=page.parent;xref=page.xref;seen=set()
    while xref not in seen:
        seen.add(xref);kind,value=doc.xref_get_key(xref,'Resources/ColorSpace')
        if kind!='null':
            if kind=='xref':value=doc.xref_object(int(value.split()[0]))
            names=set()
            for alias in re.findall(r'/([^\s/<>\[\]]+)\s',value):
                typ,spec=doc.xref_get_key(xref,'Resources/ColorSpace/'+alias)
                if typ=='xref':spec=doc.xref_object(int(spec.split()[0]))
                if re.match(r'\[\s*/Separation\b',spec):names.add(('/'+alias).encode('ascii'))
            return names
        parent_kind,parent=doc.xref_get_key(xref,'Parent')
        if parent_kind!='xref':break
        xref=int(parent.split()[0])
    return set()


def capture_separation_tints(page):
    names=separation_names(page)
    if not names:return {}
    found={}
    data=b'\n'.join(page.parent.xref_stream(x) for x in page.get_contents())
    stream_tokens=list(tokens(data))
    for i,(_,_,value) in enumerate(stream_tokens[:-1]):
        op=stream_tokens[i+1][2]
        if value not in names or op not in (b'cs',b'CS'):continue
        key=(value,op);found.setdefault(key,set())
        if i+3<len(stream_tokens) and NUMBER.match(stream_tokens[i+2][2]) and stream_tokens[i+3][2]==(b'scn' if op==b'cs' else b'SCN'):
            found[key].add(Decimal(stream_tokens[i+2][2].decode('ascii')))
        else:found[key].add(None)
    return found


def restore_separation_tints(page,captured):
    if not captured:return []
    repairs=[];pending=[]
    for xref in page.get_contents():
        data=page.parent.xref_stream(xref);stream_tokens=list(tokens(data));insertions=[]
        for i,(_,_,value) in enumerate(stream_tokens[:-1]):
            op=stream_tokens[i+1][2];key=(value,op)
            if key not in captured:continue
            tint_op=b'scn' if op==b'cs' else b'SCN'
            if i+3<len(stream_tokens) and NUMBER.match(stream_tokens[i+2][2]) and stream_tokens[i+3][2]==tint_op:continue
            tints=captured[key]
            if len(tints)!=1 or None in tints:raise ValueError('Missing Separation tint has ambiguous source values: '+value.decode('ascii'))
            tint=format(next(iter(tints)),'f')
            insertions.append((stream_tokens[i+1][1],b' '+tint.encode('ascii')+b' '+tint_op))
            repairs.append({'color_space':value.decode('ascii'),'operator':op.decode('ascii'),'tint':tint})
        for pos,addition in reversed(insertions):data=data[:pos]+addition+data[pos:]
        if insertions:pending.append((xref,data))
    for xref,data in pending:page.parent.update_stream(xref,data)
    return repairs
