"""Keep original document resources and source annotations across text redaction."""
import hashlib,re
from collections import Counter
import pymupdf as fitz

def document_copy(source, pages):
    doc=fitz.open(source)
    if not pages or len(pages)!=len(set(pages)) or any(n<1 or n>len(doc) for n in pages):
        doc.close();raise ValueError('Invalid selected source pages')
    if pages!=list(range(1,len(doc)+1)):
        # select() creates a fresh catalog and drops OutputIntents/metadata.
        # Remove unused pages in this document, then move retained pages in order.
        retained=sorted(pages)
        for n in range(len(doc),0,-1):
            if n not in pages:doc.delete_page(n-1)
        for destination,n in enumerate(pages):
            current=retained.index(n)
            if current!=destination:
                doc.move_page(current,to=destination)
                retained.insert(destination,retained.pop(current))
    return doc

def key_signature(doc,xref,key):
    kind,value=doc.xref_get_key(xref,key)
    if kind=='xref':
        ref=int(value.split()[0])
        if doc.xref_is_stream(ref):return (kind,hashlib.sha256(doc.xref_stream(ref)).hexdigest())
        return (kind,re.sub(r'\d+\s+\d+\s+R','REF',doc.xref_object(ref)))
    return kind,value

def color_profiles(doc):
    kind,value=doc.xref_get_key(doc.pdf_catalog(),'OutputIntents');result=[]
    if kind=='xref':value=doc.xref_object(int(value.split()[0]))
    for ident in re.findall(r'(\d+)\s+\d+\s+R',value):
        obj=int(ident);pk,pv=doc.xref_get_key(obj,'DestOutputProfile')
        if pk=='xref':
            result.append(hashlib.sha256(doc.xref_stream(int(pv.split()[0]))).hexdigest())
    return result

def link_signature(link):
    kind=link['kind'];box=tuple(round(x,3) for x in link['from'])
    if kind==fitz.LINK_URI:return kind,box,link.get('uri','')
    if kind==fitz.LINK_GOTO:
        point=link.get('to',fitz.Point(0,0))
        return kind,box,link.get('page'),round(point.x,3),round(point.y,3)
    return kind,box,link.get('name'),link.get('file'),link.get('nameddest')

def restore_links(page,before):
    """Redaction removes overlapping annotations; recreate all source link semantics."""
    seen=Counter(link_signature(x) for x in page.get_links());added=0
    for item in before:
        sig=link_signature(item)
        if seen[sig]:seen[sig]-=1;continue
        value={k:v for k,v in item.items() if k not in {'xref','id'}}
        if value.get('kind')==fitz.LINK_NONE:continue
        page.insert_link(value);added+=1
    return added

def audit_document(original,saved,source_pages):
    source_external=Counter(x.get('uri') for n in source_pages for x in original[n-1].get_links() if x['kind']==fitz.LINK_URI)
    output_external=Counter(x.get('uri') for p in saved for x in p.get_links() if x['kind']==fitz.LINK_URI)
    missing=source_external-output_external
    page_map={n-1:i for i,n in enumerate(source_pages)}
    source_uri_positions=Counter((page_map[n-1],link_signature(x)) for n in source_pages for x in original[n-1].get_links() if x['kind']==fitz.LINK_URI)
    output_uri_positions=Counter((p.number,link_signature(x)) for p in saved for x in p.get_links() if x['kind']==fitz.LINK_URI)
    missing_uri_positions=source_uri_positions-output_uri_positions
    source_internal=Counter()
    for n in source_pages:
        for link in original[n-1].get_links():
            if link['kind']==fitz.LINK_GOTO and link.get('page') in page_map:
                remapped=dict(link);remapped['page']=page_map[link['page']]
                source_internal[(page_map[n-1],link_signature(remapped))]+=1
    output_internal=Counter((p.number,link_signature(x)) for p in saved for x in p.get_links() if x['kind']==fitz.LINK_GOTO)
    missing_internal=source_internal-output_internal
    invalid=[]
    for p in saved:
        for link in p.get_links():
            if link['kind']==fitz.LINK_GOTO:
                target=link.get('page',-1)
                if not 0<=target<len(saved) or not saved[target].rect.contains(link.get('to',fitz.Point(0,0))):invalid.append({'page':p.number+1,'kind':'invalid-goto'})
            if 'pdf-reader.invalid' in link.get('uri',''):invalid.append({'page':p.number+1,'kind':'temporary-uri'})
    profiles_ok=color_profiles(original)==color_profiles(saved)
    groups_ok=all(key_signature(original,original[n-1].xref,'Group')==key_signature(saved,saved[i].xref,'Group') for i,n in enumerate(source_pages))
    original_metadata=original.metadata;new_metadata=saved.metadata
    preserved_info=all(new_metadata.get(k)==v for k,v in original_metadata.items() if k not in {'title','subject','format','encryption'})
    def bookmark_signature(item,remap=False):
        level,title,page,details=item
        if remap and page-1 in page_map:page=page_map[page-1]+1
        point=details.get('to',fitz.Point(0,0))
        return (level,title,page,details.get('kind'),round(point.x,3),round(point.y,3),details.get('zoom',0),details.get('bold',False),details.get('italic',False),tuple(details.get('color',())))
    source_bookmarks=Counter(bookmark_signature(x,True) for x in original.get_toc(False) if x[2]-1 in page_map)
    output_bookmarks=Counter(bookmark_signature(x) for x in saved.get_toc(False))
    missing_bookmarks=source_bookmarks-output_bookmarks
    return {'output_intents_preserved':profiles_ok,'page_groups_preserved':groups_ok,
            'source_metadata_preserved':preserved_info,'source_external_links':sum(source_external.values()),
            'output_external_links':sum(output_external.values()),'missing_external_links':dict(missing),'invalid_links':invalid,
            'source_internal_links':sum(source_internal.values()),'output_internal_links':sum(output_internal.values()),
            'missing_internal_links':[{'source_page':k[0]+1,'signature':str(k[1]),'count':v} for k,v in missing_internal.items()],
            'missing_external_link_positions':len(missing_uri_positions),'missing_bookmarks':len(missing_bookmarks),
            'passed':profiles_ok and groups_ok and preserved_info and not missing and not missing_uri_positions and not missing_internal and not missing_bookmarks and not invalid}

def align_author_year_links(original, saved, source_pages):
    """Place GOTO hotspots on Chinese author-year glyphs.

    Redaction restores original citation links at English coordinates. After
    in-place Chinese reflow those boxes no longer cover the cite text, so this
    pass searches the rewritten page for the same cite string and adds a
    correctly placed GOTO. Original-position links stay so audit_document still
    sees the source signatures.
    """
    from .citations import author_year_token
    page_map = {n - 1: i for i, n in enumerate(source_pages)}
    added = 0
    for src_index, out_index in page_map.items():
        source_page = original[src_index]
        output_page = saved[out_index]
        for link in source_page.get_links():
            # Frontiers author-year cites are LINK_NAMED with page/to filled in.
            if link.get('kind') not in (fitz.LINK_GOTO, fitz.LINK_NAMED, fitz.LINK_GOTOR):
                continue
            target = link.get('page')
            if target not in page_map:
                continue
            raw = source_page.get_textbox(link['from']).replace('\n', ' ')
            raw = re.sub(r'[\U00010000-\U0010ffff]', '', raw)
            token = author_year_token(raw)
            if not token:
                continue
            hits = output_page.search_for(token)
            if not hits:
                compact = re.sub(r'\s+', ' ', token)
                if compact != token:
                    hits = output_page.search_for(compact)
            if not hits:
                continue
            box = hits[0]
            covered = False
            for existing in output_page.get_links():
                if existing.get('page') != page_map[target]:
                    continue
                if fitz.Rect(existing['from']).intersects(box):
                    covered = True
                    break
            if covered:
                continue
            payload = {
                'kind': link.get('kind', fitz.LINK_GOTO),
                'from': box,
                'page': page_map[target],
                'to': link.get('to', fitz.Point(0, 0)),
            }
            if link.get('nameddest'):
                payload['nameddest'] = link['nameddest']
            if 'zoom' in link:
                payload['zoom'] = link['zoom']
            output_page.insert_link(payload)
            added += 1
    return added
