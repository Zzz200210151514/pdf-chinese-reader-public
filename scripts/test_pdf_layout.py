"""Regression checks for unintended appendix output, collisions and font coverage."""
import copy,json,tempfile
from pathlib import Path
from types import SimpleNamespace
import pymupdf as fitz
from pdf_layout import validate_layout
from pdf_fonts import FontCoverage
from pdf_reader import check_layout,build
from append_translation_pdf import build as append_build
from prepare_reader import prepare


def rejected(call):
    try:call()
    except ValueError:return
    raise AssertionError('Invalid layout or font was accepted')


def check():
    assert validate_layout({},'source')['mode']=='source'
    rejected(lambda:validate_layout({},'appendix'))
    rejected(lambda:validate_layout({'layout_mode':'appendix','reference_layout':'sample'},'appendix'))
    rejected(lambda:validate_layout({'layout_mode':'appendix','layout_change_authorized':'yes'},'appendix'))
    assert validate_layout({'layout_mode':'appendix','requested_layout':'appendix'},'appendix')['mode']=='appendix'
    assert validate_layout({'layout_mode':'appendix','layout_change_authorized':True},'appendix')['change_authorized']
    with tempfile.TemporaryDirectory() as directory:
        root=Path(directory);source=root/'source.pdf';manifest=root/'layout.json';out=root/'out.pdf'
        (root/'regular.ttf').write_bytes(fitz.Font('helv').buffer)
        (root/'fallback.ttf').write_bytes(fitz.Font('cjk').buffer)
        with fitz.open() as doc:
            page=doc.new_page(width=300,height=400)
            page.insert_text((20,60),'Original prose',fontsize=10)
            page.draw_rect(fitz.Rect(150,90,220,130),color=(0,0,1))
            page.insert_text((20,240),'REFERENCES',fontsize=10)
            page.insert_text((20,270),'(1) Author, First paper.',fontsize=9)
            page.insert_text((20,300),'(2) Author, Second paper.',fontsize=9)
            data=page.get_text('dict');doc.save(source)
        for block in data['blocks']:
            for line in block.get('lines',[]):
                for span in line.get('spans',[]):
                    if span['text']=='REFERENCES':span['text']='■ REFERENCES'
        mapping=prepare({'sha256':'fixture','pages':[{'source_page':1,'rect':[0,0,300,400],'blocks':data['blocks'],'drawing_clusters':[]}]})
        assert [r['number'] for r in mapping['reference_candidates']]==[1,2]
        assert mapping['prepare_summary']['preserved']['bibliography']==2
        fonts={'regular':'regular.ttf','bold':'regular.ttf'}
        valid={'fonts':fonts,'pages':[{'source_page':1,'redactions':[[18,45,140,75]],
               'regions':[{'id':'prose','rect':[18,45,140,75],'html':'Translated prose'}],
               'protected':[{'id':'formula','rect':[150,90,220,130]}]}],
               'targets':{'ref/1':{'source_page':1,'point':[20,260]}}}
        collision=copy.deepcopy(valid);collision['pages'][0]['regions'][0]['rect']=[18,45,200,120]
        report=check_layout(source,collision,root)
        issue=next(x for x in report['issues'] if x['check']=='protected-intersection')
        assert 'does not authorize' in issue['action'] and report['layout_contract']['mode']=='source'
        manifest.write_text(json.dumps(collision),'utf-8')
        rejected(lambda:append_build(source,manifest,out,root/'regular.ttf'))
        assert not out.exists()
        no_glyph=copy.deepcopy(valid);no_glyph['pages'][0]['regions'][0]['html']='正'
        assert any(x['check']=='missing-glyph' for x in check_layout(source,no_glyph,root)['issues'])
        no_glyph['fonts']['fallback']='fallback.ttf'
        rendered,missing=FontCoverage(no_glyph,root).render('<b>正</b> &amp; <a href="ref/1">1</a>')
        assert not missing and 'font-family:PdfFallback' in rendered and 'href="ref/1"' in rendered
        assert check_layout(source,no_glyph,root)['layout_checks_passed']
        _,missing=FontCoverage(no_glyph,root).render('\U0010ffff')
        assert missing==['\U0010ffff']
        no_glyph['pages'][0]['regions'][0]['html']='正 <a href="https://pdf-reader.invalid/ref/1">1</a>'
        manifest.write_text(json.dumps(no_glyph),'utf-8')
        build(SimpleNamespace(source=str(source),manifest=str(manifest),output=str(out),font_dir=str(root)))
        result=json.loads(out.with_suffix('.validation.json').read_text('utf-8'))
        assert result['reference_count']==1 and result['appended_reference_count']==0 and result['total_pages']==1
        with fitz.open(out) as final:
            assert '正' in final[0].get_text() and any(l['kind']==fitz.LINK_GOTO for l in final[0].get_links())
    print('PASS: requested layout guard, no output on unauthorized fallback, local collision guidance, glyph rejection/fallback, parenthesized bibliography, in-place reference count and return links')


if __name__=='__main__':check()
