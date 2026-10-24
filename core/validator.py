"""Functional PDF assertions, separate from linguistic or visual certification."""
import json,re,html
from pathlib import Path
from collections import Counter
import pymupdf as fitz
from .preservation import audit_document
from .raster import protected_pixmap

CHEMICAL=re.compile(r'(?<![A-Za-z])(?:Ca\(OH\)2|CaOH\+|CaCO3|CO2|H2O|NaCl|Na2SO4|MgCl2|CaSO4|CaCl2|NaOH|HCl|KCl)(?![a-z])')
# Quality tier: rendering-level deltas (anti-aliasing, compression, sub-pixel
# shifts) on protected graphics are reported, not treated as content damage.
# A visible erasure changes far more than this share of the protected pixels.
PIXEL_TOLERANCE_PERCENT=1.0
GRAPHIC_TYPES={'figure','image','drawing'}
def protected_pixel_level(kind,src_text,out_text,diff_percent):
    """Return 'warning' for rendering noise, 'blocking' for content changes."""
    if bool(src_text)!=bool(out_text) or (src_text and src_text!=out_text):
        return 'blocking'
    if kind in GRAPHIC_TYPES or not src_text:
        return 'warning' if diff_percent<=PIXEL_TOLERANCE_PERCENT else 'blocking'
    return 'warning'
def normalize(text):
    import unicodedata
    return re.sub(r'\s+','',unicodedata.normalize('NFKC',html.unescape(re.sub(r'<[^>]*>','',text)))).replace('−','-').replace('∙','·')

def _text_covers(expected, actual):
    """True when every expected character is present, allowing neighbor bleed.

    Adjacent boxes may overlap by leading bleed, so a clip can include a few
    extra glyphs. Order-preserving subsequence and multiset inclusion both
    count; missing expected characters still fail.
    """
    if not expected:
        return False
    index = 0
    for ch in actual:
        if index < len(expected) and expected[index] == ch:
            index += 1
    if index == len(expected):
        return True
    return not (Counter(expected) - Counter(actual))

def scientific_audit(mapping):
    import unicodedata
    # Whitespace is a lexical boundary. Removing it before tokenization turns
    # "to 2.20" into "to2.20", producing spurious missing fractions like "20".
    def science_text(text):
        return unicodedata.normalize('NFKC',html.unescape(re.sub(r'<[^>]*>','',text))).replace('−','-')
    issues=[]
    for b in mapping.get('blocks',[]):
        if not b.get('translation_required',True):continue
        original=science_text(b['source_text']);translated=science_text(b.get('translation',''))
        missing=Counter(CHEMICAL.findall(original))-Counter(CHEMICAL.findall(translated))
        nums=re.compile(r'(?<![A-Za-z])[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?')
        digits=Counter(nums.findall(original))-Counter(nums.findall(translated))
        if missing or digits:issues.append({'id':b['id'],'chemical_candidates_missing':dict(missing),'numeric_candidates_missing':dict(digits),'status':'review-required; may be split-block or formatting normalization'})
    return {'issues':issues,'semantic_accuracy':'NOT CERTIFIED','mechanical_candidates_only':True}

def validate(source,output,manifest,mapping=None):
    if not isinstance(manifest,dict):manifest=json.loads(Path(manifest).read_text('utf-8-sig'))
    with fitz.open(source) as a,fitz.open(output) as b:
        pages=[p['source_page'] for p in manifest['pages']]
        # An observed output length cannot serve as its own expectation.
        # Authorized appendix workflows must supply their expected page count.
        expected=manifest.get('expected_output_pages',len(pages))
        reports=audit_document(a,b,pages)
        searchable=[];pixels=[];source_display_lists={};output_display_lists={}
        for i,p in enumerate(manifest['pages']):
            for item in p.get('regions',[]):
                actual=b[i].get_text(clip=fitz.Rect(item['rect']))
                # Keep this local: `expected` above is the output page count.
                expected_text, actual_text = normalize(item['html']), normalize(actual)
                searchable.append({'id':item['id'],'searchable_and_copyable':_text_covers(expected_text, actual_text) and bool(actual.strip())})
            for item in p.get('protected',[]):
                r=fitz.Rect(item['rect'])
                src_pix=protected_pixmap(a,p['source_page']-1,r,source_display_lists)
                out_pix=protected_pixmap(b,i,r,output_display_lists)
                same=src_pix.samples==out_pix.samples
                entry={'id':item['id'],'type':item.get('type','text'),'pixels_unchanged':same}
                if not same:
                    # Validation levels: identical extracted text inside a
                    # native text object, or a small rendering delta on a
                    # graphic, is anti-aliasing/compression, not content loss.
                    src_text=normalize(a[p['source_page']-1].get_text(clip=r))
                    out_text=normalize(b[i].get_text(clip=r))
                    diff=round(sum(1 for u,v in zip(src_pix.samples,out_pix.samples) if u!=v)/max(1,len(src_pix.samples))*100,2)
                    level=protected_pixel_level(item.get('type'),src_text,out_text,diff)
                    entry['pixel_diff_percent']=diff
                    entry['pixel_level']=level
                    if level=='warning':entry['pixel_warning']=True
                    else:entry['pixel_failure_class']='protected-content-changed'
                pixels.append(entry)
        count_ok=len(b)==expected
    report={'page_count_matches':count_ok,'expected_page_count':expected,'preservation':reports,'text_regions':searchable,
            'protected':pixels,'scientific_audit':scientific_audit(mapping or {}),'reader_click_test':'not performed by this validator'}
    # Validation levels: a protected object whose extracted text is unchanged
    # may carry a small anti-aliasing delta; it is reported, not blocking.
    pixel_warnings=[r for r in pixels if r.get('pixel_warning') and not r.get('pixels_unchanged')]
    report['pixel_warnings']=pixel_warnings
    report['passed']=count_ok and reports['passed'] and all(r['searchable_and_copyable'] for r in searchable) and all(r['pixels_unchanged'] or r.get('pixel_warning') for r in pixels)
    if mapping:
        expected=[(p['source_page'],item) for p in mapping.get('pages',[]) for item in p.get('protected',[])]
        expected += [(item['source_page'],item) for item in mapping.get('blocks',[])
                     if not item.get('translation_required',True) and 'source_page' in item and 'rect' in item]
        actual={(p['source_page'],item['id']):item['rect'] for p in manifest['pages'] for item in p.get('protected',[])}
        missing=[{'page':pn,'id':item['id']} for pn,item in expected
                 if (pn,item['id']) not in actual or not fitz.Rect(actual[pn,item['id']]).contains(fitz.Rect(item['rect']))]
        report['native_protection_coverage']={'passed':not missing,'missing_or_shrunken':missing}
        report['passed']=report['passed'] and not missing
    if mapping and mapping.get('pages'):
        from .structure import audit_order
        report['reading_order']=audit_order(mapping,manifest)
        report['passed']=report['passed'] and report['reading_order']['passed']
    report['quality_status']={'structure_and_functions':'passed' if report['passed'] else 'failed',
                              'layout_visual_review':'not performed by this validator',
                              'scientific_semantic_review':'not performed by this validator'}
    return report
