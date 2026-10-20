"""Bidirectional paper navigation resolved from final PDF glyphs and original media.

No document-specific paths, figure grids, translation provider, or network access.
"""
from __future__ import annotations
import base64, collections, html, re, unicodedata
from pathlib import Path
import pymupdf

TAGS = re.compile(r'</?(?:sub|sup|b|strong|i|em|br|scp)\b[^>]*>', re.I)
REF = re.compile(r'\[\d+(?:[,，;；–—−-]\d+)*\]')
NUMBER = r'[A-Z]?\d+[a-f]?'
SEQUENCE = rf'{NUMBER}(?:[、,，;；和及或与–—−-](?:{NUMBER}|[a-f](?![A-Za-z0-9])))*'
OBJECT = re.compile(rf'(公式|方程|反应|式|图|表|Figs?\.?|Figures?|Tables?|Eqs?\.?|Equations?)[（(]?({SEQUENCE})[）)]?', re.I)
PREFIX = {'figure':'fig','table':'table','equation':'eq'}

def compact(text):
    return ''.join(c for c in unicodedata.normalize('NFKC',html.unescape(TAGS.sub('',text))) if not c.isspace() and c!='\u00ad')

def references(blocks):
    result={};inside=False;last=None
    for block in blocks:
        if block['type']=='heading':
            inside=compact(block['source_text']).lower() in {'references','bibliography','参考文献'}
        if not (inside or block.get('reference_source')):continue
        if block['type']=='heading':continue
        match=re.match(r'^\s*\[(\d+)\]',block['source_text'])
        if match:
            last=int(match[1])
            if last in result:raise ValueError('Repeated reference label needs a reviewed merge: '+str(last))
            result[last]=[block]
        elif last is not None:result[last].append(block)
    return result

def object_targets(blocks,translations,reviews):
    targets={};panels={}
    for block in blocks:
        kind=block['type']
        if kind not in PREFIX:continue
        number=str(block.get('number') or '')
        if not number:
            for caption in [block['source_text'],translations.get(block['id'],'')]:
                match=re.match(r'\s*(?:Fig(?:ure)?\.?|Table|图|表)\s*([A-Z]?\d+)',caption,re.I)
                if match:number=match[1];break
        if not number:continue
        key=PREFIX[kind]+'-'+number
        if key in targets:
            if not re.search(r'continued|续',block['source_text'],re.I) and not reviews.get(block['id'],{}).get('note'):
                raise ValueError('Repeated object number needs review: '+key)
            continue
        targets[key]=block['id']
        for letter,box in reviews.get(block['id'],{}).get('panels',{}).items():
            if not re.fullmatch('[a-f]',letter) or len(box)!=4 or not(0<=box[0]<box[2]<=1 and 0<=box[1]<box[3]<=1):
                raise ValueError('Invalid reviewed panel rectangle: '+key)
            panels[key+letter]=(key,box)
    return targets,panels

def glyph_stream(doc):
    text=[];glyphs=[]
    for page in doc:
        for block in page.get_text('rawdict')['blocks']:
            for line in block.get('lines',[]):
                for span in line['spans']:
                    for char in span['chars']:
                        # Ignore only the pagination footer added by this engine.
                        if char['bbox'][1]>page.rect.height-40:continue
                        for letter in unicodedata.normalize('NFKC',char['c']):
                            if letter.isspace() or letter=='\u00ad':continue
                            text.append(letter);glyphs.append((page.number,pymupdf.Rect(char['bbox'])))
    return ''.join(text),glyphs

def locate(text,needle,old_page=None):
    if not needle:return None
    starts=[];offset=0
    while True:
        pos=text.find(needle,offset)
        if pos<0:break
        starts.append(pos);offset=pos+1
    return starts

def media_geometry(doc,blocks,work):
    images=collections.defaultdict(list)
    for page in doc:
        for item in page.get_image_info(hashes=True):images[item['digest'].hex()].append((page.number,pymupdf.Rect(item['bbox'])))
    result={}
    for block in blocks:
        if 'asset' not in block:continue
        digest=pymupdf.Pixmap(str(Path(work)/block['asset'])).digest.hex()
        if not images[digest]:raise ValueError('Navigation cannot find original media '+block['id'])
        result[block['id']]=images[digest].pop(0)
    return result

def point_at(glyph):
    page,rect=glyph
    return page,rect.x0,max(0,rect.y0-3)

def citation_tokens(text,targets):
    """Offsets refer to visible normalized text, never HTML attributes or charges."""
    for match in REF.finditer(text):
        values=[int(x) for x in re.findall(r'\d+',match[0])]
        covered=set(values)
        for a,z in re.findall(r'(\d+)[–—−-](\d+)',match[0]):covered.update(range(int(a),int(z)+1))
        for part in re.finditer(r'\d+',match[0]):
            yield 'ref-'+part[0],match.start()+part.start(),match.start()+part.end(),covered
    for match in OBJECT.finditer(text):
        prefix=match[1].lower()
        kind='fig' if prefix.startswith(('图','fig')) else 'table' if prefix.startswith(('表','tab')) else 'eq'
        current=None
        for part in re.finditer(r'[A-Z]?\d+[a-f]?|[a-f](?![A-Za-z0-9])',match[2]):
            label=part[0]
            if label.isalpha() and len(label)==1:
                if kind!='fig' or current is None:continue
                label=current+label
            else:current=re.sub('[a-f]$','',label)
            target=kind+'-'+label
            if target not in targets:target=kind+'-'+re.sub('[a-f]$','',label)
            yield target,match.start(2)+part.start(),match.start(2)+part.end(),set()

def image_citations(source,block):
    crop=pymupdf.Rect(block.get('crop_rect') or (pymupdf.Rect(block['source_rect'])+(-3,-3,3,3)))
    page=source[block['source_page']-1];crop &= page.rect
    for raw in page.get_text('rawdict')['blocks']:
        for line in raw.get('lines',[]):
            letters=[];rects=[]
            for span in line['spans']:
                for ch in span['chars']:
                    r=pymupdf.Rect(ch['bbox'])
                    if not crop.contains((r.tl+r.br)/2):continue
                    for c in unicodedata.normalize('NFKC',ch['c']):
                        if c.isspace():continue
                        letters.append(c);rects.append(r)
            for match in REF.finditer(''.join(letters)):
                covered=set(map(int,re.findall(r'\d+',match[0])))
                for a,z in re.findall(r'(\d+)[–—−-](\d+)',match[0]):covered.update(range(int(a),int(z)+1))
                for digit in re.finditer(r'\d+',match[0]):
                    chosen=rects[match.start()+digit.start():match.start()+digit.end()]
                    r=pymupdf.Rect(chosen[0])
                    for other in chosen[1:]:r |= other
                    yield int(digit[0]),[(r.x0-crop.x0)/crop.width,(r.y0-crop.y0)/crop.height,r.width/crop.width,r.height/crop.height],covered

def attach(doc,blocks,translations,reviews,source_pdf,work):
    refs=references(blocks);objects,panels=object_targets(blocks,translations,reviews)
    text,glyphs=glyph_stream(doc);media=media_geometry(doc,blocks,work)
    points={};starts={};by_id={b['id']:b for b in blocks}
    for block in blocks:
        ident=block['id']
        if reviews.get(ident,{}).get('omit_reason'):continue
        if reviews.get(ident,{}).get('translation_in'):continue
        if ident in media:
            page,rect=media[ident];points[ident]=(page,rect.x0,max(0,rect.y0-3))
        value=block['source_text'] if reviews.get(ident,{}).get('preserve') else translations.get(ident,'')
        matches=locate(text,compact(value))
        if matches:
            old=doc.resolve_link('#'+ident)
            start=min(matches,key=lambda n:abs(glyphs[n][0]-old[0])) if old[0]>=0 else matches[0]
            starts[ident]=start
            if ident not in media:points[ident]=point_at(glyphs[start])
        elif compact(value):raise ValueError('Cannot locate rendered block for navigation: '+ident)
    for ident,review in reviews.items():
        if review.get('translation_in'):points[ident]=points[review['translation_in']]
    for target,ident in objects.items():points[target]=points[ident]
    for target,(owner,box) in panels.items():
        page,rect=media[objects[owner]]
        points[target]=(page,rect.x0+rect.width*box[0],max(0,rect.y0+rect.height*box[1]-3))
    for number,parts in refs.items():points['ref-'+str(number)]=points[parts[0]['id']]
    first={};links=[];hotspots=collections.defaultdict(list);external=[]
    ref_ids={b['id'] for parts in refs.values() for b in parts}
    for block in blocks:
        ident=block['id']
        if ident in ref_ids or ident not in starts:continue
        value=block['source_text'] if reviews.get(ident,{}).get('preserve') else translations.get(ident,'')
        for target,a,z,covered in citation_tokens(compact(value),points):
            if objects.get(target)==ident or (target in panels and objects[panels[target][0]]==ident):continue
            if target not in points:raise ValueError('Citation has no verified target: '+target)
            span=glyphs[starts[ident]+a:starts[ident]+z]
            for number in covered:
                key='ref-'+str(number)
                if key not in points:raise ValueError('Citation range has missing reference '+key)
                first.setdefault(key,{'id':ident,'point':point_at(span[0])})
            first.setdefault(target,{'id':ident,'point':point_at(span[0])})
            for page in sorted(set(g[0] for g in span)):
                rect=None
                for p,r in span:
                    if p==page:rect=pymupdf.Rect(r) if rect is None else rect|r
                links.append((page,rect,target))
    with pymupdf.open(source_pdf) as source:
        for number,parts in refs.items():
            for block in parts:
                region=pymupdf.Rect(block['source_rect']);uris=set(block.get('urls',[]))
                for original in source[block['source_page']-1].get_links():
                    if original.get('uri','').startswith(('https://','http://')) and region.intersects(original['from']):uris.add(original['uri'])
                for uri in uris:
                    value=compact(block['source_text']);needle=compact(uri).rstrip('.,;')
                    if needle not in value and 'doi.org/' in needle:needle=needle.split('doi.org/',1)[1]
                    offset=value.find(needle)
                    if offset<0:continue
                    span=glyphs[starts[block['id']]+offset:starts[block['id']]+offset+len(needle)]
                    # A long DOI can span multiple lines; keep each annotation on its line.
                    rows=[]
                    for pg,r in span:
                        if rows and rows[-1][0]==pg and abs(rows[-1][1].y0-r.y0)<2:rows[-1][1] |= r
                        else:rows.append([pg,pymupdf.Rect(r)])
                    for pg,r in rows:doc[pg].insert_link({'kind':pymupdf.LINK_URI,'from':r,'uri':uri})
                    external.append({'reference':number,'uri':uri})
        for ident,(page,rect) in media.items():
            for n,box,covered in image_citations(source,by_id[ident]):
                target='ref-'+str(n)
                if target not in points:raise ValueError('Image citation has no reference '+target)
                hit=pymupdf.Rect(rect.x0+box[0]*rect.width,rect.y0+box[1]*rect.height,rect.x0+(box[0]+box[2])*rect.width,rect.y0+(box[1]+box[3])*rect.height)
                for number in covered:
                    key='ref-'+str(number)
                    if key not in points:raise ValueError('Image citation range has no reference '+key)
                    first.setdefault(key,{'id':ident,'point':(page,hit.x0,max(0,hit.y0-3))})
                links.append((page,hit,target));hotspots[ident].append((target,box))
    for page,rect,target in links:
        p,x,y=points[target]
        doc[page].insert_link({'kind':pymupdf.LINK_GOTO,'from':rect,'page':p,'to':pymupdf.Point(x,y)})
    backwards=0;uncited=[]
    for n,parts in refs.items():
        target='ref-'+str(n)
        if target not in first:uncited.append(n);continue
        start=starts[parts[0]['id']];label=compact('['+str(n)+']')
        label_glyphs=glyphs[start:start+len(label)];page=label_glyphs[0][0]
        rect=pymupdf.Rect(label_glyphs[0][1])
        for _,r in label_glyphs[1:]:rect |= r
        p,x,y=first[target]['point']
        doc[page].insert_link({'kind':pymupdf.LINK_GOTO,'from':rect,'page':p,'to':pymupdf.Point(x,y)})
        backwards+=1
    # A direct Dests dictionary uses actual page-space geometry, with PDF bottom-up Y.
    entries=[]
    for name,(page,x,y) in sorted(points.items()):
        if not re.fullmatch('[A-Za-z0-9_-]+',name):raise ValueError('Unsafe destination name')
        entries.append(f'/{name} [{doc.page_xref(page)} 0 R /XYZ {x:.5f} {doc[page].rect.height-y:.5f} 0]')
    xref=doc.get_new_xref();doc.update_object(xref,'<< '+' '.join(entries)+' >>')
    doc.xref_set_key(doc.pdf_catalog(),'Dests',str(xref)+' 0 R')
    toc=[]
    for block in blocks:
        if block['type']=='heading' and block['id'] in points:
            page,x,y=points[block['id']]
            level=1 if block.get('level',2)<=2 or not toc else 2
            label=html.unescape(TAGS.sub('',translations.get(block['id'],block['source_text'])))
            toc.append([level,label,page+1,{'kind':pymupdf.LINK_GOTO,'page':page,'to':pymupdf.Point(x,y)}])
    doc.set_toc(toc)
    report={'forward_links':len(links),'reference_number_backlinks':backwards,'uncited_references':uncited,'reference_targets':len(refs),'object_targets':len(objects),'reviewed_panel_targets':len(panels),'image_citations':sum(len(v) for v in hotspots.values()),'external_reference_urls':external,'geometry':'actual final PDF glyphs and original-media digests','verified_points':{k:{'page':p+1,'point':[x,y]} for k,(p,x,y) in points.items()},'first_citations':first,'hotspots':dict(hotspots),'objects':objects,'panels':panels,'reference_blocks':{n:[b['id'] for b in parts] for n,parts in refs.items()}}
    return report

def style_print_citations(body, blocks, translations, reviews, color='#2196d1'):
    """Color the same citation glyphs that attach() makes clickable in the PDF."""
    if not re.fullmatch(r'#[0-9a-fA-F]{6}', color):
        raise ValueError('Citation color must be a six-digit CSS hex color')
    refs=references(blocks);objects,panels=object_targets(blocks,translations,reviews)
    targets=set(objects)|set(panels)|{'ref-'+str(n) for n in refs}
    ref_ids={b['id'] for parts in refs.values() for b in parts}
    for block in blocks:
        ident=block['id']
        pattern=rf'(<(?:p|h[1-4])\b[^>]*id="{re.escape(ident)}"[^>]*>)(.*?)(</(?:p|h[1-4])>)'
        if block['type'] in PREFIX:
            pattern=rf'(<div\b[^>]*id="{re.escape(ident)}"[^>]*>.*?<p class="caption">)(.*?)(</p>)'
        def paint(match):
            head,content,tail=match.groups()
            if ident in ref_ids:
                content=re.sub(r'^(\s*)(\[\d+\])',lambda m:m[1]+f'<span style="color:{color}">'+m[2]+'</span>',content,count=1)
                return head+content+tail
            chunks=re.split('(<[^>]+>)',content)
            for i,chunk in enumerate(chunks):
                if chunk.startswith('<'):continue
                value=html.unescape(chunk);normalized=[];offset=[]
                for j,c in enumerate(value):
                    for letter in unicodedata.normalize('NFKC',c):
                        if not letter.isspace():normalized.append(letter);offset.append(j)
                ranges=set()
                for key,a,z,_ in citation_tokens(''.join(normalized),targets):
                    if key in targets and objects.get(key)!=ident and not (key in panels and objects[panels[key][0]]==ident):
                        ranges.add((offset[a],offset[z-1]+1))
                cursor=0;fragments=[]
                for a,z in sorted(ranges):
                    if a<cursor:continue
                    fragments.extend([html.escape(value[cursor:a]),f'<span style="color:{color}">{html.escape(value[a:z])}</span>']);cursor=z
                fragments.append(html.escape(value[cursor:]));chunks[i]=''.join(fragments)
            return head+''.join(chunks)+tail
        body=re.sub(pattern,paint,body,flags=re.S)
    return body.replace('</head>','<style>html{-webkit-print-color-adjust:exact;print-color-adjust:exact}</style></head>',1)


def decorate_html(body,blocks,translations,reviews,report,work):
    """Keep the same bidirectional reference-number interaction in offline HTML."""
    by_id={b['id']:b for b in blocks};objects=report['objects'];panels=report['panels']
    reverse={ident:key for key,ident in objects.items()};first=report['first_citations']
    target_ids=set(report['verified_points']);ref_ids={str(n):ids for n,ids in report['reference_blocks'].items()}
    for b in blocks:
        ident=b['id'];asset=b.get('asset')
        if asset:
            uri='data:image/png;base64,'+base64.b64encode((Path(work)/asset).read_bytes()).decode('ascii')
            body=body.replace('src="'+asset+'"','src="'+uri+'"')
        # Work only within this block's visible paragraph/caption, never attributes.
        pattern=rf'(<(?:p|h[1-4])\b[^>]*id="{re.escape(ident)}"[^>]*>)(.*?)(</(?:p|h[1-4])>)'
        if b['type'] in PREFIX:pattern=rf'(<div\b[^>]*id="{re.escape(ident)}"[^>]*>.*?<p class="caption">)(.*?)(</p>)'
        if any(ident in ids for ids in ref_ids.values()):continue
        def link_text_node(match):
            head,content,tail=match.groups()
            chunks=re.split('(<[^>]+>)',content)
            for i,chunk in enumerate(chunks):
                if chunk.startswith('<'):continue
                # Mapping raw text indices through entities is unnecessary for whole nodes.
                value=html.unescape(chunk);normalized=[];offset=[]
                for j,c in enumerate(value):
                    for letter in unicodedata.normalize('NFKC',c):
                        if not letter.isspace():normalized.append(letter);offset.append(j)
                replacements=[]
                for key,a,z,_ in citation_tokens(''.join(normalized),target_ids):
                    if key not in target_ids or objects.get(key)==ident:continue
                    replacements.append((offset[a],offset[z-1]+1,key))
                fragments=[];cursor=0
                for a,z,key in sorted(set(replacements)):
                    if a<cursor:continue
                    fragments.extend([html.escape(value[cursor:a]),f'<a class="cite" href="#{key}">{html.escape(value[a:z])}</a>'])
                    cursor=z
                fragments.append(html.escape(value[cursor:]));chunks[i]=''.join(fragments)
            return head+''.join(chunks)+tail
        body=re.sub(pattern,link_text_node,body,flags=re.S)
    for number,ids in ref_ids.items():
        ident=ids[0];key='ref-'+number
        body=body.replace(f'id="{ident}">',f'id="{ident}"><span id="{key}"></span>',1)
        if key in first:
            dest=first[key]['id']
            pattern=rf'(id="{ident}"><span id="{key}"></span>\s*(?:<span style="color:#[0-9a-fA-F]{{6}}">)?)\[{number}\]'
            body=re.sub(pattern,lambda m:m[1]+f'<a class="ref-back" href="#{dest}" title="返回正文引用处">[{number}]</a>',body,count=1)
    for ident,key in reverse.items():body=body.replace(f'id="{ident}">',f'id="{ident}"><span id="{key}"></span>',1)
    for ident in set(report['hotspots'])|{objects[p[0]] for p in panels.values()}:
        overlays=''
        for key,box in report['hotspots'].get(ident,[]):
            x,y,w,h=box;overlays+=f'<a class="image-cite" href="#{key}" style="left:{x*100}%;top:{y*100}%;width:{w*100}%;height:{h*100}%"></a>'
        for key,(parent,box) in panels.items():
            if objects[parent]==ident:overlays+=f'<span id="{key}" style="position:absolute;left:{box[0]*100}%;top:{box[1]*100}%"></span>'
        pattern=rf'(<div\b[^>]*id="{ident}"[^>]*>.*?)(<img[^>]*>)'
        body=re.sub(pattern,lambda m:m[1]+'<span class="linked-image">'+m[2]+overlays+'</span>',body,count=1,flags=re.S)
    css='''<style>.linked-image{display:inline-block;position:relative;max-width:100%;line-height:0}.linked-image img{display:block}.image-cite{position:absolute;z-index:2}.image-cite:hover{outline:1px solid #357}.reference-return{display:none}.ref-back{cursor:pointer}#reader-back{position:fixed;right:16px;bottom:16px;z-index:50;padding:9px} @media print{#reader-back{display:none}} @media screen{body{max-width:1100px;margin:35px auto;padding:20px}.media img{height:auto!important}} @media(max-width:700px){.columns{column-count:1!important}}</style>'''
    script='''<button id="reader-back" hidden>↩ 返回刚才位置</button><script>const visits=[],lastRef=new Map(),btn=document.getElementById('reader-back');document.addEventListener('click',e=>{const a=e.target.closest('a[href^="#"]');if(!a)return;const id=decodeURIComponent(a.hash.slice(1)),t=document.getElementById(id);if(!t)return;e.preventDefault();if(a.classList.contains('ref-back')){const ref=a.closest('p').querySelector('[id^="ref-"]');if(ref&&lastRef.has(ref.id)){const s=lastRef.get(ref.id);scrollTo(s.x,s.y);return;}}const s={x:scrollX,y:scrollY};visits.push(s);if(id.startsWith('ref-'))lastRef.set(id,s);btn.hidden=false;t.scrollIntoView({block:'start'});});btn.onclick=()=>{const s=visits.pop();if(s)scrollTo(s.x,s.y);btn.hidden=!visits.length;};</script>'''
    return body.replace('</head>',css+'</head>').replace('</body>',script+'</body>')
