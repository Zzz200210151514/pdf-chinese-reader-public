"""Prepare source maps from PDF inspection; merge id:translation without reprinting English."""
import argparse, hashlib, html, json, re
from collections import Counter
from pathlib import Path

# pdf_reader applies these when a hand-written manifest omits size/leading/min_scale.
DEFAULT_SIZE,DEFAULT_LEADING,DEFAULT_MIN_SCALE=9.35,1.35,1
# prepare takes size from the source block and keeps its own leading/shrink limit; see references/manifest.md.
PREPARE_LEADING,PREPARE_MIN_SCALE=1.17,.88
# Axis titles and panel letters sit just outside the vector cluster they label.
ZONE_MARGIN=20;LABEL_CHARS=60;SHORT_CHARS=40
TOKEN=r'(?:[-−+]?\d[\d.,]*%?|[A-Za-z])'
LABEL_RE=re.compile(rf'{TOKEN}(?:\s+{TOKEN})*')
REF_HEADINGS={'references','bibliography','references and notes'}
EQUATION_RE=re.compile(r'[=×≈≤≥±∑∫]|[(ð]\d{1,3}[)Þ]\s*$')
ACRONYM_RE=re.compile(r'((?:[A-Za-z][A-Za-z−–-]*\s+){1,8})\(([A-Z][A-Za-z0-9-]{1,9})\)')
REF_STYLES=[('bracket',re.compile(r'^\[(\d{1,3})\](?:\s|$)')),('parenthesis',re.compile(r'^\((\d{1,3})\)(?:\s|$)')),('dot',re.compile(r'^(\d{1,3})\.(?:\s|$)')),('plain',re.compile(r'^(\d{1,3})\s+[A-Z]'))]

def write(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')

def unique_pairs(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ValueError('Duplicate JSON key: '+key)
        result[key]=value
    return result

def read(path):return json.loads(Path(path).read_text(encoding='utf-8-sig'),object_pairs_hook=unique_pairs)

def dominant(spans):
    """Character-weighted font size, so a heading merged into a paragraph does not set the body size."""
    weights=Counter()
    for s in spans:
        if s['text'].strip():weights[round(s['size'],1)]+=len(s['text'].strip())
    return weights.most_common(1)[0][0] if weights else None

def zones(page):
    w=page['rect'][2]-page['rect'][0];h=page['rect'][3]-page['rect'][1];result=[]
    texts=[(b['bbox'],len(''.join(s['text'] for l in b.get('lines',[]) for s in l['spans']).strip())) for b in page['blocks'] if b.get('type')!=1]
    for x0,y0,x1,y1 in page.get('drawing_clusters',[])+[b['bbox'] for b in page['blocks'] if b.get('type')==1]:
        # Rules, underlines and full-page backgrounds are not figures.
        if x1-x0<30 or y1-y0<30 or (x1-x0)*(y1-y0)>.6*w*h:continue
        zone=[x0-ZONE_MARGIN,y0-ZONE_MARGIN,x1+ZONE_MARGIN,y1+ZONE_MARGIN]
        # A shaded box around an abstract or key points holds prose, not plot labels.
        if any(n>LABEL_CHARS and inside(box,zone) for box,n in texts):continue
        result.append(zone)
    return result

def inside(box,zone):
    w=min(box[2],zone[2])-max(box[0],zone[0]);h=min(box[3],zone[3])-max(box[1],zone[1])
    area=(box[2]-box[0])*(box[3]-box[1])
    return w>0 and h>0 and (area<=0 or w*h>=.8*area)

def prose_boxes(page,body):
    """Long horizontal blocks near body size; long legends in small type are not prose."""
    result=[]
    for b in page['blocks']:
        if b.get('type')==1:continue
        spans=[s for l in b.get('lines',[]) for s in l['spans']];size=dominant(spans)
        if len(''.join(s['text'] for s in spans).strip())>LABEL_CHARS and size and (not body or size>=.8*body):result.append(b['bbox'])
    return result

def touches(a,b):return min(a[2],b[2])>max(a[0],b[0]) and min(a[3],b[3])>max(a[1],b[1])

def label_reason(text,lines,size,body,box,page_zones,prose):
    if any(abs(l['dir'][0]-1)>1e-3 or abs(l['dir'][1])>1e-3 for l in lines):return 'rotated-text'
    # Superscript/subscript fragments overlap their paragraph; preserving them would cut into its region.
    if any(touches(box,p) for p in prose):return None
    # Tick values and panel letters read the same in Chinese.
    if LABEL_RE.fullmatch(text):return 'figure-label-candidate'
    if len(text)<=LABEL_CHARS and any(inside(box,z) for z in page_zones):return 'figure-label-candidate'
    if size and body and size<.8*body and len(text)<=SHORT_CHARS:return 'figure-label-candidate'
    return None

def non_prose(text):
    """Display equations, equation numbers and numeric rows: nothing for a translator to render in Chinese."""
    visible=sum(not c.isspace() for c in text);letters=sum(c.isalpha() for c in text)
    if not re.search(r'[A-Za-z]{3}',text) or letters<.33*visible:return True
    return len(text)<=LABEL_CHARS and bool(EQUATION_RE.search(text)) and len(re.findall(r'\b[a-z]{4,}\b',text))<2

def acronyms(blocks):
    """First 'long form (ACRONYM)' definitions, as cues for the terminology ledger."""
    found={}
    for b in blocks:
        if not b['translation_required']:continue
        for m in ACRONYM_RE.finditer(b['source_text']):
            words=re.findall(r'[A-Za-z]+',m[1]);letters=''.join(c for c in m[2] if c.isupper())
            if len(letters)<2 or m[2] in found:continue
            for start in range(len(words)-1,-1,-1):
                if ''.join(w[0] for w in words[start:]).upper()==letters:
                    found[m[2]]={'acronym':m[2],'english':' '.join(words[start:]),'first_block':b['id']};break
    return list(found.values())

def running_key(text,box):return re.sub(r'\d+','#',text),round(box[1]/5)

def running(pages):
    """Header/footer lines repeat at the same height on most pages, differing only in page numbers."""
    seen=Counter()
    for page in pages:
        seen.update({running_key(''.join(s['text'] for l in b.get('lines',[]) for s in l['spans']).strip(),b['bbox'])
                     for b in page['blocks'] if b.get('type')!=1})
    return {k for k,n in seen.items() if n>=3 and n>=len(pages)/2}

def prepare(data):
    pages=data['pages'];repeated=running(pages)
    body=dominant([s for p in pages for b in p['blocks'] for l in b.get('lines',[]) for s in l['spans']])
    result={'source_sha256':data['sha256'],'layout_reviewed':False,'terminology_ledger':[],
            'blocks':[],'pages':[],'targets':{},'references':[],'reference_candidates':[]}
    candidates=result['reference_candidates'];in_refs=False;expected=1;ref_style=None
    for page in pages:
        pn=page['source_page'];number=0;images=[];page_zones=zones(page);prose=prose_boxes(page,body)
        for block in page['blocks']:
            if block.get('type')==1:
                images.append({'id':f'p{pn}-image-{len(images)+1}','rect':list(block['bbox'])});continue
            lines=block.get('lines',[])
            text=' '.join(''.join(s['text'] for s in line['spans']) for line in lines).replace('­','').strip()
            if not text:continue
            number+=1;ident=f'P{pn:02}B{number:03}'
            spans=[s for line in lines for s in line['spans'] if s['text'].strip()]
            size=dominant(spans)
            entry={'id':ident,'source_page':pn,'rect':list(block['bbox']),
                'source_text':text,'source_text_sha256':hashlib.sha256(text.encode()).hexdigest(),
                'translation_required':True,'translation':'','size':size or DEFAULT_SIZE,'leading':PREPARE_LEADING,
                'min_scale':PREPARE_MIN_SCALE,'source_font_pt':size}
            if spans and all(s.get('flags',0)&16 or 'bold' in s.get('font','').lower() for s in spans):entry['bold']=True
            reason=label_reason(text,lines,size,body,block['bbox'],page_zones,prose)
            if running_key(''.join(s['text'] for l in lines for s in l['spans']).strip(),block['bbox']) in repeated:reason='running-header-footer'
            starts=[]
            for line in lines:
                value=''.join(s['text'] for s in line['spans']).strip()
                if value.lower().lstrip('■•* ').rstrip('.:') in REF_HEADINGS:in_refs=True;expected=1;ref_style=None
                for style,pattern in REF_STYLES:
                    match=pattern.match(value)
                    if match:starts.append((int(match[1]),style,line['bbox'],value));break
            if reason is None and in_refs:
                # Only blocks that continue the entry numbering are bibliography; prose after the list stays translatable.
                accepted=0
                for n,style,*_ in starts:
                    if expected<=n<=expected+2 and ref_style in (None,style):ref_style=style;expected=n+1;accepted+=1
                if accepted:reason='bibliography'
            if reason is None and non_prose(text) and not any(touches(block['bbox'],p) for p in prose):reason='non-prose-candidate'
            # Pre-marks are reversible: set translation_required=true and drop preservation_reason.
            if reason:entry.update(translation_required=False,preservation_reason=reason)
            result['blocks'].append(entry)
            if reason in (None,'bibliography'):
                candidates.extend({'number':n,'style':style,'source_page':pn,'rect':box,'first_line':value} for n,style,box,value in starts)
        result['pages'].append({'source_page':pn,'rect':page['rect'],'protected':images})
    # Numbered headings and table rows also match; the style counts show which pattern the bibliography uses.
    styles=Counter(c['style'] for c in candidates)
    result['acronym_candidates']=acronyms(result['blocks'])
    result['expected_block_ids']=[b['id'] for b in result['blocks']]
    preserved={}
    for b in result['blocks']:
        if not b['translation_required']:preserved.setdefault(b['preservation_reason'],[]).append(b['id'])
    result['prepare_summary']={'blocks':len(result['blocks']),
        'translation_required':sum(b['translation_required'] for b in result['blocks']),
        'preserved':{k:len(v) for k,v in preserved.items()},'preserved_ids':preserved,'body_font_pt':body,
        'reference_styles':dict(styles),'acronym_candidates':len(result['acronym_candidates']),
        'drawing_clusters':'available' if all('drawing_clusters' in p for p in pages) else 'missing; rerun pdf_reader.py inspect to detect vector-figure labels'}
    return result

def merge(mapping,translations):
    blocks=mapping['blocks'];ids=[b['id'] for b in blocks]
    if len(ids)!=len(set(ids)):raise ValueError('Duplicate source block IDs')
    if set(ids)!=set(mapping['expected_block_ids']):raise ValueError('Source block coverage changed')
    unknown=set(translations)-set(ids)
    if unknown:raise ValueError('Unknown translation IDs: '+','.join(sorted(unknown)))
    missing=[]
    for b in blocks:
        if b['id'] in translations:
            value=translations[b['id']]
            if not isinstance(value,str):raise ValueError('Translation must be text: '+b['id'])
            if not b.get('translation_required',True):
                raise ValueError('Preserved block was translated; set translation_required=true to undo the pre-mark: '+b['id'])
            b['translation']=value
        if b.get('translation_required',True) and not b.get('translation','').strip():missing.append(b['id'])
        if not b.get('translation_required',True) and not b.get('preservation_reason'):
            raise ValueError('Preserved block needs a reason: '+b['id'])
    if missing:raise ValueError('Missing translations: '+','.join(missing))
    return mapping

def review_required(mapping):
    if not mapping.get('layout_reviewed'):raise ValueError('Review read order, metadata, equations, tables and bibliography before setting layout_reviewed=true')

def layout(mapping):
    review_required(mapping)
    targets=mapping.get('targets',{});refs=mapping.get('references',[])
    # pdf_reader.build creates ref/N for every bibliography entry.
    known=set(targets)|{f'ref/{r["number"]}' for r in refs}
    def markup(s):
        s=html.escape(s).replace('\n','<br/>')
        s=re.sub(r'&lt;(/?(?:sub|sup|b|i|strong|em))&gt;',r'<\1>',s)
        s=re.sub(r'\^\{([^{}\n]+)\}',r'<sup>\1</sup>',s);s=re.sub(r'_\{([^{}\n]+)\}',r'<sub>\1</sub>',s)
        def link(m):
            kind,num,label=m.groups();key=kind+'/'+num
            if key not in known:raise ValueError('Missing navigation target: '+key)
            return f'<a href="https://pdf-reader.invalid/{key}">{label or num}</a>'
        return re.sub(r'\[\[(fig|ref|table|eq):(\d+)(?:\|([^\]]+))?\]\]',link,s)
    result={'title':mapping.get('title','中文全文阅读版'),'scope':mapping.get('scope','全文译文，原版式'),'source_sha256':mapping['source_sha256'],
            'fonts':mapping.get('fonts',{'regular':'msyh.ttc','bold':'msyhbd.ttc'}),'targets':targets,'references':refs,'pages':[]}
    for key in ('layout_mode','requested_layout','layout_change_authorized','reference_layout'):
        if key in mapping:result[key]=mapping[key]
    for page in mapping['pages']:
        regions=[];protected=list(page.get('protected',[]))
        for b in mapping['blocks']:
            if b['source_page']!=page['source_page']:continue
            if not b.get('translation_required',True):
                protected.append({'id':b['id'],'rect':b['rect']});continue
            text=markup(b['translation'])
            regions.append({'id':b['id'],'rect':b['rect'],'html':('<b>'+text+'</b>' if b.get('bold') else text),
                            'paragraph_start':b.get('paragraph_start',True),
                            **{k:b[k] for k in ['size','leading','min_scale'] if k in b}})
        # ponytail: union only same-column overlaps; cross-column ambiguities stay for preflight review.
        changed=True
        while changed:
            changed=False
            for i,a in enumerate(regions):
                for j in range(i+1,len(regions)):
                    b=regions[j];x,y=a['rect'],b['rect']
                    if abs(x[0]-y[0])<3 and abs(x[2]-y[2])<3 and x[1]<y[3] and y[1]<x[3]:
                        if x[1]>y[1]:a,b=b,a
                        merged={**a,'id':a['id']+'+'+b['id'],'rect':[min(x[0],y[0]),min(x[1],y[1]),max(x[2],y[2]),max(x[3],y[3])],
                                'html':a['html']+('<br/>' if b.get('paragraph_start',True) else '')+b['html'],
                                'size':min(a.get('size',DEFAULT_SIZE),b.get('size',DEFAULT_SIZE)),
                                'leading':min(a.get('leading',DEFAULT_LEADING),b.get('leading',DEFAULT_LEADING)),
                                'min_scale':max(a.get('min_scale',DEFAULT_MIN_SCALE),b.get('min_scale',DEFAULT_MIN_SCALE))}
                        regions[i]=merged;regions.pop(j);changed=True;break
                if changed:break
        result['pages'].append({'source_page':page['source_page'],'regions':regions,
                               'redactions':[r['rect'] for r in regions],'protected':protected})
    return result

def reader(mapping):
    """Reader manifest for reader_export.py: paragraph_start=false fragments join the previous translated block."""
    review_required(mapping)
    if not isinstance(mapping.get('title'),str) or not mapping['title'].strip():raise ValueError('Set the reviewed Chinese title in the source map before --reader')
    blocks=[];omitted=[]
    for b in mapping['blocks']:
        if not b.get('translation_required',True):omitted.append(b['id']);continue
        if blocks and b.get('paragraph_start',True) is False:
            last=blocks[-1];last['source_block_ids'].append(b['id'])
            last['source_text']+=' '+b['source_text'];last['translation']+=b['translation']
            if b['source_page'] not in last['pages']:last['pages'].append(b['source_page'])
            continue
        item={'id':b['id'],'type':b.get('type','paragraph'),'pages':[b['source_page']],'source_block_ids':[b['id']],
              'source_text':b['source_text'],'translation':b['translation']}
        if 'level' in b:item['level']=b['level']
        blocks.append(item)
    covered=[i for b in blocks for i in b['source_block_ids']]+omitted
    if sorted(covered)!=sorted(mapping['expected_block_ids']):raise ValueError('Reader blocks do not cover the source map')
    # Preserved figure labels, equations and tables need cropped assets added as figure/equation/table blocks by review.
    return {'title':mapping['title'],'scope':mapping.get('scope','全文译文'),'meta':mapping.get('meta',{}),
            'source_sha256':mapping['source_sha256'],'blocks':blocks,'references':mapping.get('references',[]),
            'expected_block_ids':[b['id'] for b in blocks],'omitted_preserved_ids':omitted}

def main():
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='command',required=True)
    a=sub.add_parser('prepare');a.add_argument('--inspect',required=True);a.add_argument('--output',required=True)
    a=sub.add_parser('merge');a.add_argument('--map',required=True);a.add_argument('--translations',required=True);a.add_argument('--output',required=True)
    a.add_argument('--layout');a.add_argument('--reader')
    a=p.parse_args()
    if a.command=='prepare':
        m=prepare(read(a.inspect));write(a.output,m)
        print(json.dumps({'output':a.output,**{k:v for k,v in m['prepare_summary'].items() if k!='preserved_ids'}},ensure_ascii=False));return
    m=merge(read(a.map),read(a.translations))
    manifest=layout(m) if a.layout else None
    reading=reader(m) if a.reader else None
    write(a.output,m)
    if manifest is not None:write(a.layout,manifest)
    if reading is not None:write(a.reader,reading)
    print(json.dumps({'output':a.output,'layout':a.layout,'reader':a.reader},ensure_ascii=False))
if __name__=='__main__':main()
