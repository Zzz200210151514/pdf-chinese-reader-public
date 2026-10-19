"""Build a source-layout Chinese PDF from explicit, reviewed regions. No translation service."""
import argparse, hashlib, html, json, re, tempfile,sys,time
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
import pymupdf as fitz
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core.preservation import document_copy,restore_links,audit_document
from core.resource_colors import snapshot_default_spaces,restore_default_spaces
from core.fonts import resolve_fonts
from core.raster import protected_pixmap
from core.redaction import check_erasure_coverage
from core import engine_identity
from core.validator import protected_pixel_level
from pdf_colors import capture_separation_tints, restore_separation_tints
from pdf_layout import validate_layout
from pdf_fonts import FontCoverage, scientific_symbols

class Plain(HTMLParser):
    def __init__(self, text):
        super().__init__(); self.parts=[]; self.feed(text)
    def handle_data(self, text): self.parts.append(text)
    def text(self): return ''.join(self.parts)

def cjk(text): return ''.join(re.findall(r'[\u3400-\u9fff]', text))
def rect(value, bounds):
    r=fitz.Rect(value)
    if r.is_empty or r.is_infinite or not bounds.contains(r): raise ValueError(f'Invalid rectangle: {value}')
    return r

def style(m, fontdir):
    fonts=resolve_fonts(m.get('fonts'),fontdir)
    reg = Path(fonts['regular']).name if Path(fonts['regular']).is_file() else fonts['regular']
    bld = Path(fonts.get('bold', fonts['regular'])).name if Path(fonts.get('bold', fonts['regular'])).is_file() else fonts.get('bold', fonts['regular'])
    css=f"@font-face{{font-family:Body;src:url('{reg}')}}@font-face{{font-family:Body;src:url('{bld}');font-weight:bold}}*{{margin:0;padding:0}}body{{font-family:Body}}sup,sub{{font-size:70%}}a{{color:#0000ff;text-decoration:none}}"
    css+='p{margin:0;padding:0}body{margin:0;padding:0}'
    fallback=fonts.get('fallback') or fonts.get('symbol')
    if fallback:
        fb = Path(fallback).name if Path(fallback).is_file() else fallback
        css+=f"@font-face{{font-family:PdfFallback;src:url('{fb}')}}"
    dirs = {str(fontdir)} if fontdir else set()
    for f in (fonts.get('regular'), fonts.get('bold'), fallback):
        if f and Path(f).is_file(): dirs.add(str(Path(f).parent.resolve()))
    archive = fitz.Archive(list(dirs)) if len(dirs) > 1 else fitz.Archive(list(dirs)[0] if dirs else '.')
    return css, archive

def content(item, fonts=None):
    text=item['html']
    if fonts:
        text,missing=fonts.render(text,item.get('bold',False))
        if missing:raise ValueError('Font glyphs missing: '+''.join(missing))
    return f'<div style="font-size:{float(item.get("size",9.35))}pt;line-height:{float(item.get("leading",1.35))};font-weight:{"bold" if item.get("bold") else "normal"}">{text}</div>'

def fit_only(item, bounds, fonts, css, archive, scale_low=None):
    """Use insert_htmlbox's exact fit algorithm without writing a throwaway PDF."""
    r=fitz.Rect(item['rect'])
    story=fitz.Story(html=content(item,fonts),user_css='body {margin:1px;}'+css,archive=archive)
    minimum=item.get('min_scale',1) if scale_low is None else scale_low
    if not 0<=minimum<=1:raise ValueError("'scale_low' must be in [0, 1]")
    fit=story.fit_scale(fitz.Rect(0,0,r.width,r.height),scale_min=1,
        scale_max=None if minimum==0 else 1/minimum,
        flags=fitz.mupdf.FZ_PLACE_STORY_FLAG_NO_OVERFLOW)
    scale=1/fit.parameter
    if not fit.big_enough:return -1,scale
    return max((fit.rect.y1-fitz.Rect(fit.filled).y1)*scale,0),scale


def micro_region(item):
    """Only small standalone inline objects may use the micro fitting floor."""
    if item.get('block_type') in ('title','heading','abstract') or len(item.get('source_block_ids',[]))>1:
        return False
    text=''.join(Plain(item['html']).text().split())
    box=fitz.Rect(item['rect'])
    citation=re.fullmatch(r'\[\d[\d,;–−-]*\][.。,;，；]?',text)
    return (0<len(text)<=16 and box.width<=80 and box.height<=30
            and (item.get('inline_fragment') or bool(citation)))


def check_layout(source,m,fontdir):
    """Collect all geometry/fit problems before copying pages or embedding final fonts."""
    css,archive=style(m,fontdir);issues=[];warnings=[];fits=[];seen=set()
    fonts=FontCoverage(m,fontdir)
    try:contract=validate_layout(m,'source')
    except ValueError as e:
        contract=None;issues.append({'check':'layout-contract','detail':str(e)})
    if m.get('source_sha256') and m['source_sha256']!=hashlib.sha256(Path(source).read_bytes()).hexdigest():
        issues.append({'check':'source-hash-mismatch'})
    with fitz.open(source) as original:
        for spec in m['pages']:
            n=int(spec['source_page'])
            if n in seen or not 1<=n<=len(original):
                issues.append({'page':n,'check':'duplicate-or-invalid-page'});continue
            seen.add(n);bounds=original[n-1].rect;boxes=[];mutations=[];ids=set()
            for item in spec.get('regions',[]):
                ident=item['id']
                if ident in ids:issues.append({'page':n,'id':ident,'check':'duplicate-region-id'})
                ids.add(ident)
                try:r=rect(item['rect'],bounds)
                except ValueError as e:issues.append({'page':n,'id':ident,'check':'invalid-rect','detail':str(e)});continue
                boxes.append((ident,r));mutations.append(r)
                _,missing=fonts.render(item['html'],item.get('bold',False))
                if missing:
                    issues.append({'page':n,'id':ident,'check':'missing-glyph','characters':missing,
                                   'action':'Provide a covering fonts.fallback or explicit sub/sup markup; do not emit tofu glyphs.'})
                    continue
                spare,scale=fit_only(item,bounds,fonts,css,archive)
                if spare<0:
                    original_leading=float(item.get('leading',1.35))
                    plain=Plain(item['html']).text()
                    short_line=len(plain)<=120 and '<br' not in item['html'].lower()
                    for leading in ([1.1,1.05,1.0] if short_line else [1.1,1.05]):
                        if leading>=original_leading:continue
                        item['leading']=leading
                        spare,scale=fit_only(item,bounds,fonts,css,archive)
                        if spare>=0:
                            item['fit_adjustment']={'leading_before':original_leading,'leading_after':leading,
                                                    'min_scale_unchanged':item.get('min_scale',1)}
                            break
                    if spare<0:item['leading']=original_leading
                if spare<0 and micro_region(item) and float(item.get('min_scale',1))>.75:
                    original_min=float(item.get('min_scale',1))
                    item['min_scale']=.75
                    spare,scale=fit_only(item,bounds,fonts,css,archive)
                    if spare>=0:
                        item.setdefault('fit_adjustment',{}).update(
                            kind='micro-fit',min_scale_before=original_min,min_scale_after=.75)
                    else:item['min_scale']=original_min
                if spare<0:
                    _,needed=fit_only(item,bounds,fonts,css,archive,scale_low=0)
                    issues.append({'page':n,'id':ident,'check':'overflow','suggested_font_pt':round(float(item.get('size',9.35))*needed,2),
                                   'action':'Review spacing or enlarge/reflow the region; do not delete text or blindly accept a tiny font.'})
                else:
                    fits.append({'page':n,'id':ident,'scale':scale,'spare_height_pt':spare})
                    if item.get('fit_adjustment'):
                        warnings.append({'check':'adaptive-fit','severity':'warning','page':n,'id':ident,
                                         'rect':list(r),'scale':scale,'font_pt':round(float(item.get('size',9.35))*scale,2),
                                         'adjustment':dict(item['fit_adjustment'])})
            def bleed(a,b):
                v=min(a.y1,b.y1)-max(a.y0,b.y0)
                return 0<v<=max(2.5,.35*max(1.0,min(a.height,b.height,20)))
            for i,(ident,r) in enumerate(boxes):
                for other,q in boxes[i+1:]:
                    if r.intersects(q) and not bleed(r,q):issues.append({'page':n,'id':ident,'other':other,'check':'overlap'})
            for value in spec.get('redactions',[])+[x['rect'] for x in spec.get('overlays',[])]:
                try:mutations.append(rect(value,bounds))
                except ValueError as e:issues.append({'page':n,'check':'invalid-mutation-rect','detail':str(e)})
            for item in spec.get('protected',[]):
                try:r=rect(item['rect'],bounds)
                except ValueError as e:issues.append({'page':n,'id':item.get('id'),'check':'invalid-protected-rect','detail':str(e)});continue
                text_native=item.get('type') not in ('figure',)
                if any(q.intersects(r) and not (text_native and bleed(q,r)) for q in mutations):
                    issues.append({'page':n,'id':item.get('id'),'check':'protected-intersection',
                        'action':'Re-divide prose and protected objects, then rerun preflight. This local collision does not authorize a whole-document layout change.'})
            issues.extend(check_erasure_coverage(original[n-1], spec, repair=True))
    return {'layout_checks_passed':not issues,'layout_contract':contract,'issues':issues,'regions':fits,
            'warnings':warnings,'blocking_count':len(issues),'warning_count':len(warnings)}

def overlap(a,b):return max(0,min(a.x1,b.x1)-max(a.x0,b.x0))*max(0,min(a.y1,b.y1)-max(a.y0,b.y0))

def outside_change(original,source_index,saved,output_index,rects):
    """Share of 72 dpi pixel bytes that differ once every declared text region is blanked on both pages."""
    shots=[]
    for doc,index in ((original,source_index),(saved,output_index)):
        with fitz.open() as temp:
            temp.insert_pdf(doc,from_page=index,to_page=index);page=temp[0]
            for r in rects:page.draw_rect(r+(-2,-2,2,2),color=None,fill=(1,1,1))
            shots.append(page.get_pixmap(dpi=72).samples)
    a,b=shots
    if a==b:return 0.0
    return sum(x!=y for x,y in zip(a,b))/max(len(a),1) if len(a)==len(b) else 1.0

def preflight(args):
    m=json.loads(Path(args.manifest).read_text(encoding='utf-8-sig'))
    m['fonts']=resolve_fonts(m.get('fonts'),Path(args.font_dir).resolve())
    report=check_layout(args.source,m,Path(args.font_dir).resolve())
    Path(args.output).parent.mkdir(parents=True,exist_ok=True)
    Path(args.output).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'issues':len(report['issues']),'warnings':report['warning_count'],'report':args.output},ensure_ascii=False))
    if report['issues']:raise SystemExit(1)

def inspect(args):
    out=Path(args.output); out.mkdir(parents=True,exist_ok=True)
    with fitz.open(args.source) as doc:
        numbers=args.pages or list(range(1,len(doc)+1))
        pages=[]
        for n in numbers:
            p=doc[n-1]; data=p.get_text('dict')
            # Image bytes are unnecessary for coordinate inspection.
            for b in data['blocks']: b.pop('image',None); b.pop('mask',None)
            # Vector plots are not image blocks; prepare_reader uses these boxes to pre-mark figure labels.
            clusters=[list(r) for r in p.cluster_drawings()] if hasattr(p,'cluster_drawings') else []
            pages.append({'source_page':n,'rect':list(p.rect),'text':p.get_text(),'blocks':data['blocks'],'drawing_clusters':clusters})
            p.get_pixmap(dpi=120).save(out/f'page-{n:03}.png')
        (out/'source.json').write_text(json.dumps({'page_count':len(doc),'sha256':hashlib.sha256(Path(args.source).read_bytes()).hexdigest(),'pages':pages},ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'inspection':str(out.resolve()),'pages':numbers},ensure_ascii=False))

def build(args):
    started=time.perf_counter();timings={}
    source=Path(args.source).resolve(); target=Path(args.output).resolve()
    if source==target: raise ValueError('Output must not overwrite source PDF')
    m=json.loads(Path(args.manifest).read_text(encoding='utf-8-sig'))
    fontdir=Path(args.font_dir).resolve();css,archive=style(m,fontdir)
    fonts=FontCoverage(m,fontdir)
    pre=check_layout(source,m,fontdir);target.parent.mkdir(parents=True,exist_ok=True)
    timings['preflight_seconds']=round(time.perf_counter()-started,3);phase=time.perf_counter()
    prepath=target.with_suffix('.preflight.json');prepath.write_text(json.dumps(pre,ensure_ascii=False,indent=2),encoding='utf-8')
    target.with_suffix('.effective-layout.json').write_text(json.dumps(m,ensure_ascii=False,indent=2),encoding='utf-8')
    if pre['issues']:raise ValueError(f'{len(pre["issues"])} layout problems; see {prepath}')
    original=fitz.open(source)
    specs=m['pages']; source_pages=[int(p['source_page']) for p in specs]
    if len(set(source_pages))!=len(source_pages):raise ValueError('Duplicate source pages')
    page_map={n:i for i,n in enumerate(source_pages)}
    doc=document_copy(source,source_pages)
    source_toc=doc.get_toc(simple=False)
    checks=[]; protected=[]; mutated=[];color_repairs=[];restored_links=0;restored_default_spaces=0
    for pn,spec in enumerate(specs):
        page=doc[pn]; masks=[rect(r,page.rect) for r in spec.get('redactions',[])]
        regions=spec.get('regions',[]); ids=[r['id'] for r in regions]
        if len(ids)!=len(set(ids)):raise ValueError('Duplicate region ids')
        boxes=[rect(r['rect'],page.rect) for r in regions]
        def bleed(a,b):
            v=min(a.y1,b.y1)-max(a.y0,b.y0)
            return 0<v<=max(2.5,.35*max(1.0,min(a.height,b.height,20)))
        for i,r in enumerate(boxes):
            if any(r.intersects(other) and not bleed(r,other) for other in boxes[i+1:]):raise ValueError('Text regions overlap')
        for item in spec.get('protected',[]):
            r=rect(item['rect'],page.rect)
            text_native=item.get('type') not in ('figure',)
            overlays=[rect(o['rect'],page.rect) for o in spec.get('overlays',[])]
            if any(b.intersects(r) and not (text_native and bleed(b,r)) for b in masks+boxes+overlays):
                raise ValueError('Mutation crosses protected area: '+item['id'])
            protected.append({'id':item['id'],'type':item.get('type','text'),'output_page':pn,'source_page':spec['source_page']-1,'rect':list(r)})
        original_links=page.get_links()
        default_spaces=snapshot_default_spaces(page) if masks else None
        color_state=capture_separation_tints(page) if masks else {}
        for r in masks:page.add_redact_annot(r,fill=None,cross_out=False)
        if masks:
            page.apply_redactions(images=0,graphics=0,text=0)
            color_repairs.extend({'source_page':spec['source_page'],**r} for r in restore_separation_tints(page,color_state))
            page=doc.reload_page(page)
            restored_default_spaces+=restore_default_spaces(page,default_spaces)
            restored_links+=restore_links(page,original_links)
        for overlay in spec.get('overlays',[]):page.draw_rect(rect(overlay['rect'],page.rect),color=None,fill=(1,1,1))
        mutated.append(masks+boxes+[rect(o['rect'],page.rect) for o in spec.get('overlays',[])])
        for item,r in zip(regions,boxes):
            text=item['html']; size=float(item.get('size',9.35)); leading=float(item.get('leading',1.35))
            # The fitter can land one ulp below min_scale; PyMuPDF asserts on
            # scale_low, so allow a float-epsilon margin.
            scale_low=max(0.0,float(item.get('min_scale',1))-1e-6)
            spare,scale=page.insert_htmlbox(r,content(item,fonts),css=css,archive=archive,scale_low=scale_low)
            if spare<0:raise ValueError(f'Text overflow: page {pn+1}, {item["id"]}')
            checks.append({'id':item['id'],'page':pn,'rect':list(r),'scale':scale,'font_pt':size,'spare_height_pt':spare,
                           'expected_cjk':cjk(Plain(text).text()),'expected_symbols':scientific_symbols(Plain(text).text())})
    timings['text_rebuild_seconds']=round(time.perf_counter()-phase,3);phase=time.perf_counter()
    destinations={k:(page_map[int(v['source_page'])],fitz.Point(v['point'])) for k,v in m.get('targets',{}).items()}
    refs=m.get('references',[]); numbers=[r['number'] for r in refs]
    if refs and (any(type(n) is not int or n < 1 for n in numbers) or len(set(numbers)) != len(numbers)): raise ValueError('Bibliography references must have positive unique integer numbers')
    reference_checks=[]; page=None; col=0;y=0
    for item in refs:
        text=str(item['number'])+'. '+item['text']
        while True:
            if page is None:
                page=doc.new_page(width=doc[0].rect.width,height=doc[0].rect.height);col=0;y=72
                header=fitz.Rect(40,24,page.rect.width-40,60)
                spare,_=page.insert_htmlbox(header,'<b>参考文献（原文书目信息）</b>',css=css+'body{font-size:11pt;line-height:1.3}',archive=archive,scale_low=1)
                if spare<0:raise ValueError('Reference header overflow')
            width=(page.rect.width-92)/2;x=40 if col==0 else 52+width;bottom=page.rect.height-48
            r=fitz.Rect(x,y,x+width,bottom)
            spare,scale=page.insert_htmlbox(r,html.escape(text),css=css+'body{font-size:8.6pt;line-height:1.25}',archive=archive,scale_low=1)
            if spare>=0:
                used=r.height-spare;destinations[f'ref/{item["number"]}']=(page.number,fitz.Point(x,y))
                reference_checks.append({'number':item['number'],'page':page.number,'rect':[x,y,x+width,y+used+1],'text':text})
                y+=used+5;break
            if y==72 and col==1:raise ValueError('A bibliography entry cannot fit in an empty column')
            if col==0:col=1;y=72
            else:page=None
    # Avoid serializing repeated full CJK fonts to an in-memory buffer.
    target.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='pdf-reader-') as temp:
        temp_pdf=Path(temp)/'annotations.pdf';doc.subset_fonts();doc.save(temp_pdf,garbage=4,deflate=True);doc.close();doc=fitz.open(temp_pdf)
        links=[];first={}
        for p in doc:
            for link in p.get_links():
                uri=link.get('uri','')
                if not uri.startswith('https://pdf-reader.invalid/'):continue
                key=uri.split('https://pdf-reader.invalid/',1)[1]
                if key not in destinations:raise ValueError('Missing internal target: '+key)
                dst,point=destinations[key]
                if not doc[dst].rect.contains(point):raise ValueError('Target point outside page: '+key)
                first.setdefault(key,(p.number,fitz.Point(link['from'].x0,max(0,link['from'].y0-20))))
                box=link['from'];p.delete_link(link);p.insert_link({'kind':fitz.LINK_GOTO,'from':box,'page':dst,'to':point})
                links.append({'key':key,'source_page':p.number+1,'target_page':dst+1,'from':list(box),'to':list(point)})
        for item in reference_checks:
            key=f'ref/{item["number"]}'
            if key in first:
                pn,point=first[key];r=fitz.Rect(item['rect']);r.x1=r.x0+18;r.y1=r.y0+12
                doc[item['page']].insert_link({'kind':fitz.LINK_GOTO,'from':r,'page':pn,'to':point})
        appended={f'ref/{item["number"]}' for item in reference_checks}
        for key,(dst,point) in destinations.items():
            if re.fullmatch(r'ref/[1-9]\d*',key) and key in first and key not in appended:
                pn,back=first[key];box=fitz.Rect(point.x,point.y,point.x+18,point.y+12)&doc[dst].rect
                doc[dst].insert_link({'kind':fitz.LINK_GOTO,'from':box,'page':pn,'to':back})
        toc=[[1,f'中文正文 · 原第 {n} 页',page_map[n]+1] for n in source_pages]
        if refs:toc.append([1,'参考文献',len(source_pages)+1])
        # Keep the native outline tree when present. set_toc cannot recreate
        # named destinations faithfully and silently turns some into LINK_NONE.
        if not source_toc:doc.set_toc(toc)
        metadata=original.metadata.copy()
        if 'title' in m:metadata['title']=m['title']
        if 'scope' in m:metadata['subject']=m['scope']
        doc.set_metadata(metadata)
        doc.subset_fonts();doc.save(target,garbage=4,deflate=True);doc.close()
    saved=fitz.open(target)
    timings['save_fonts_links_seconds']=round(time.perf_counter()-phase,3);phase=time.perf_counter()
    for item in checks:
        actual_text=saved[item['page']].get_text(clip=fitz.Rect(item['rect']))
        actual=cjk(actual_text)
        item['cjk_complete']=actual==item.pop('expected_cjk')
        if not item['cjk_complete']:raise ValueError('Chinese coverage mismatch: '+item['id'])
        item['scientific_symbols_complete']=not (Counter(item.pop('expected_symbols'))-Counter(scientific_symbols(actual_text)))
        if '\x00' in actual_text or not item['scientific_symbols_complete']:
            raise ValueError('Scientific glyph coverage mismatch: '+item['id'])
    normalize=lambda s:re.sub(r'\s+','',s)
    for item in reference_checks:
        actual=saved[item['page']].get_text(clip=fitz.Rect(item['rect']))
        if normalize(actual)!=normalize(item['text']):raise ValueError('Bibliography mismatch: '+str(item['number']))
    source_display_lists={};saved_display_lists={}
    for item in protected:
        r=fitz.Rect(item['rect']);a=protected_pixmap(original,item['source_page'],r,source_display_lists);b=protected_pixmap(saved,item['output_page'],r,saved_display_lists)
        item['pixels_unchanged']=a.samples==b.samples
        if not item['pixels_unchanged']:
            # Quality tier: identical text in a native text object, or a small
            # rendering delta on a protected graphic, is anti-aliasing or
            # compression rather than content loss. Visible changes still fail.
            src_text=normalize(original[item['source_page']].get_text(clip=r))
            out_text=normalize(saved[item['output_page']].get_text(clip=r))
            diff=round(sum(1 for u,v in zip(a.samples,b.samples) if u!=v)/max(1,len(a.samples))*100,2)
            level=protected_pixel_level(item.get('type'),src_text,out_text,diff)
            item['pixel_diff_percent']=diff
            item['pixel_level']=level
            if level=='blocking':raise ValueError('Protected area changed: '+item['id'])
            item['pixel_warning']=True
    # Review signals, not failures: prose nobody translated or preserved, and damage outside the declared regions.
    residue=[];outside=[]
    for pn,spec in enumerate(specs):
        keep=[fitz.Rect(x['rect']) for x in spec.get('protected',[])]
        for block in saved[pn].get_text('dict')['blocks']:
            if block.get('type')!=0:continue
            text=' '.join(''.join(s['text'] for s in line['spans']) for line in block['lines'])
            words=re.findall(r"\b[A-Za-z][A-Za-z'-]{2,}\b",text)
            if len(words)<10 or cjk(text):continue
            r=fitz.Rect(block['bbox'])
            if sum(overlap(r,k) for k in keep)>=.5*max(r.get_area(),1):continue
            residue.append({'page':pn+1,'rect':[round(v,1) for v in r],'english_words':len(words),'text':re.sub(r'\s+',' ',text)[:160]})
        ratio=outside_change(original,spec['source_page']-1,saved,pn,mutated[pn])
        if ratio>.001:outside.append({'page':pn+1,'changed_ratio':round(ratio,5)})
    for p in saved:
        for link in p.get_links():
            if 'pdf-reader.invalid' in link.get('uri',''):raise ValueError('Temporary URI remains')
            if link['kind']==fitz.LINK_GOTO and not 0<=link['page']<len(saved):raise ValueError('Invalid GoTo page')
    preservation=audit_document(original,saved,source_pages)
    if not preservation['passed']:raise ValueError('Document or source links were not preserved: '+str(preservation))
    if not refs and len(saved)!=len(source_pages):raise ValueError('Unexpected page count change')
    timings['verification_seconds']=round(time.perf_counter()-phase,3);phase=time.perf_counter()
    previews=target.with_suffix('.previews');previews.mkdir(exist_ok=True)
    for i,p in enumerate(saved):p.get_pixmap(dpi=120).save(previews/f'page-{i+1:03}.png')
    # A PDF contact sheet needs no Pillow dependency and contains every output page.
    with fitz.open() as sheet:
        for start in range(0,len(saved),9):
            grid=sheet.new_page(width=900,height=1260)
            for offset in range(min(9,len(saved)-start)):
                x=(offset%3)*300;y=(offset//3)*420
                grid.insert_text((x+10,y+15),str(start+offset+1),fontsize=10)
                grid.show_pdf_page(fitz.Rect(x+5,y+22,x+295,y+415),saved,start+offset)
        sheet.save(target.with_suffix('.overview.pdf'),garbage=4,deflate=True)
    ref_numbers=set(numbers)|{int(key.split('/')[1]) for key in destinations if re.fullmatch(r'ref/[1-9]\d*',key)}
    report={'engine':engine_identity(),'structural_checks_passed':True,'layout_contract':pre['layout_contract'],'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'output_sha256':hashlib.sha256(target.read_bytes()).hexdigest(),'color_repairs':color_repairs,'body_source_pages':source_pages,'total_pages':len(saved),'reference_count':len(ref_numbers),'appended_reference_count':len(refs),'regions':checks,'protected':protected,'english_residue':residue,'outside_region_changes':outside,'links':links,'scope':m.get('scope',''),'translation_review':'NOT AUTOMATED','visual_review':'REQUIRED','reader_click_test':'NOT PERFORMED BY SCRIPT'}
    warnings=pre['warnings']+[{'check':'protected-rendering-delta','severity':'warning',**item}
                              for item in protected if item.get('pixel_warning')]
    report.update(warnings=warnings,warning_count=len(warnings),blocking_count=0,
                  delivery_status='passed_with_warnings' if warnings else 'passed',visual_review='OPTIONAL QUICK REVIEW')
    timings['preview_seconds']=round(time.perf_counter()-phase,3)
    timings['total_seconds']=round(time.perf_counter()-started,3)
    report.update(timings=timings,document_preservation=preservation,restored_source_links=restored_links,restored_default_color_spaces=restored_default_spaces)
    target.with_suffix('.validation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'output':str(target),'pages':len(saved),'reference_count':len(ref_numbers),'structural_checks_passed':True,'english_residue':len(residue),'outside_region_changes':len(outside),
                      'delivery_status':report['delivery_status'],'warnings':len(warnings)},ensure_ascii=False))
    # Detailed bookmark extraction keeps native outline handles alive until GC.
    # Release PDF handles explicitly, particularly for Windows repeat builds.
    saved.close();original.close()

def main():
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='command',required=True)
    a=sub.add_parser('inspect');a.add_argument('--source',required=True);a.add_argument('--output',required=True);a.add_argument('--pages',type=int,nargs='+');a.set_defaults(run=inspect)
    a=sub.add_parser('preflight');a.add_argument('--source',required=True);a.add_argument('--manifest',required=True);a.add_argument('--output',required=True);a.add_argument('--font-dir',required=True);a.set_defaults(run=preflight)
    a=sub.add_parser('build');a.add_argument('--source',required=True);a.add_argument('--manifest',required=True);a.add_argument('--output',required=True);a.add_argument('--font-dir',required=True);a.set_defaults(run=build)
    args=parser.parse_args();args.run(args)
if __name__=='__main__':main()
