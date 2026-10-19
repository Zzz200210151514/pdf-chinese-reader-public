"""Offline checks: source coverage, strict merge, pre-marking, all-at-once overflow and PDF output."""
import copy,hashlib,json,tempfile
from pathlib import Path
from types import SimpleNamespace
import pymupdf as fitz
from prepare_reader import prepare,merge,layout,read,reader
from pdf_reader import check_layout,build,outside_change
from reader_export import validate as export_validate

def rejected(call):
    try:call()
    except ValueError:return
    raise AssertionError('Invalid input accepted')

def check():
    with tempfile.TemporaryDirectory() as temp:
        root=Path(temp);source=root/'source.pdf';font=root/'regular.ttf'
        font.write_bytes(fitz.Font('helv').buffer)
        with fitz.open() as doc:
            p=doc.new_page(width=300,height=400)
            p.insert_text((20,35),'Heading',fontsize=11)
            p.insert_text((20,80),'The oxygen evolution reaction (OER) was measured at 10 mA.',fontsize=9)
            p.insert_text((20,120),'OER region',fontsize=6);p.draw_rect(fitz.Rect(18,115,100,135))
            p.insert_text((40,200),'rotated axis',fontsize=8,rotate=90)
            p.insert_text((20,280),'Journal 1',fontsize=8);p.insert_text((20,330),'E = m × c (1)',fontsize=9)
            data=p.get_text('dict');clusters=[list(r) for r in p.cluster_drawings()]
            extra=[]
            for n in (2,3):
                q=doc.new_page(width=300,height=400);q.insert_text((20,280),f'Journal {n}',fontsize=8)
                if n==3:
                    q.insert_text((20,60),'References',fontsize=9)
                    q.insert_text((20,100),'1. A. Author, Paper title. J. Chem. 1, 1 (2026).',fontsize=9)
                    q.insert_text((20,140),'2. B. Author, Other title. J. Chem. 2, 2 (2026).',fontsize=9)
                    q.insert_text((20,180),'Acknowledgements follow the list here and thank many kind people for their help.',fontsize=7)
                extra.append(q.get_text('dict'))
            doc.save(source)
        inspection={'sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'pages':[
            {'source_page':1,'rect':[0,0,300,400],'blocks':data['blocks'],'drawing_clusters':clusters}]+[
            {'source_page':n,'rect':[0,0,300,400],'blocks':d['blocks'],'drawing_clusters':[]} for n,d in zip((2,3),extra)]}
        m=prepare(inspection)
        assert m['expected_block_ids']==['P01B001','P01B002','P01B003','P01B004','P01B005','P01B006','P02B001','P03B001','P03B002','P03B003','P03B004','P03B005'],m['expected_block_ids']
        pre=m['prepare_summary']['preserved'];reasons=[b.get('preservation_reason') for b in m['blocks']]
        assert reasons==[None,None,'figure-label-candidate','rotated-text','running-header-footer','non-prose-candidate',
                         'running-header-footer','running-header-footer',None,'bibliography','bibliography',None],reasons
        assert pre=={'figure-label-candidate':1,'rotated-text':1,'running-header-footer':3,'non-prose-candidate':1,'bibliography':2}
        assert m['acronym_candidates']==[{'acronym':'OER','english':'oxygen evolution reaction','first_block':'P01B002'}],m['acronym_candidates']
        assert [c['number'] for c in m['reference_candidates']]==[1,2]
        rejected(lambda:merge(copy.deepcopy(m),{'unknown':'text'}))
        rejected(lambda:merge(copy.deepcopy(m),{'P01B001':'heading'}))
        zh={'P01B001':'Heading translated','P01B002':'Measured at 10 mA^{2} [[ref:1]] H<sub>2</sub>O.','P03B002':'Reference list','P03B005':'Thanks.'}
        rejected(lambda:merge(copy.deepcopy(m),{**zh,'P01B003':'pre-marked blocks are not translated'}))
        dup=root/'duplicate.json';dup.write_text('{"x":"a","x":"b"}',encoding='utf-8')
        rejected(lambda:read(dup))
        merged=merge(copy.deepcopy(m),zh)
        rejected(lambda:layout(merged))
        merged['layout_reviewed']=True;merged['fonts']={'regular':font.name,'bold':font.name}
        merged['blocks'][0]['bold']=True;merged['blocks'][1]['paragraph_start']=False
        for b in merged['blocks']:
            if b['translation_required']:b['rect'][2]=280;b['rect'][3]+=15
        rejected(lambda:layout(merged))  # ref/1 has neither a target nor a bibliography entry
        merged['references']=[{'number':1,'text':'A. Author, Journal 1, 1 (2026).'}]
        manifest=layout(merged);assert check_layout(source,manifest,root)['layout_checks_passed']
        assert manifest['references']==merged['references']
        body=manifest['pages'][0]['regions'][1]['html']
        assert '<sup>2</sup>' in body and '<sub>2</sub>' in body and 'pdf-reader.invalid/ref/1' in body,body
        rejected(lambda:reader(merged))  # needs a reviewed title
        merged['title']='测试'
        rd=reader(merged);assert [b['source_block_ids'] for b in rd['blocks']]==[['P01B001','P01B002'],['P03B002'],['P03B005']]
        assert rd['omitted_preserved_ids']==['P01B003','P01B004','P01B005','P01B006','P02B001','P03B001','P03B003','P03B004']
        export_validate(rd,root)
        bad=copy.deepcopy(manifest)
        # Use real text-box overlap; 2pt extraction bleed is intentionally tolerated.
        for b in bad['pages'][0]['regions']:b['rect']=[20,100,40,112];b['html']='Very long paragraph '*40;b['min_scale']=1
        bad['pages'][0]['protected']=[{'id':'figure','rect':[20,99,45,110]}]
        report=check_layout(source,bad,root)
        assert sum(x['check']=='overflow' for x in report['issues'])==2
        assert any(x['check']=='overlap' for x in report['issues'])
        assert any(x['check']=='protected-intersection' for x in report['issues'])
        path=root/'manifest.json';path.write_text(json.dumps(manifest),encoding='utf-8')
        output=root/'out.pdf';build(SimpleNamespace(source=str(source),manifest=str(path),output=str(output),font_dir=str(root)))
        assert output.is_file() and output.with_suffix('.overview.pdf').is_file()
        result=json.loads(output.with_suffix('.validation.json').read_text())
        assert result['structural_checks_passed'] and result['reference_count']==1 and any(l['key']=='ref/1' for l in result['links'])
        assert result['english_residue']==[] and result['outside_region_changes']==[],(result['english_residue'],result['outside_region_changes'])
        # A paragraph left out of regions and protected areas is reported.
        loose=copy.deepcopy(manifest);loose['pages'][2]['regions']=[];loose['pages'][2]['redactions']=[]
        path2=root/'loose.json';path2.write_text(json.dumps(loose),encoding='utf-8')
        out2=root/'loose.pdf';build(SimpleNamespace(source=str(source),manifest=str(path2),output=str(out2),font_dir=str(root)))
        loose_result=json.loads(out2.with_suffix('.validation.json').read_text())
        assert [x['page'] for x in loose_result['english_residue']]==[3],loose_result['english_residue']
        # Damage outside the declared regions shows up as changed pixels; declared regions are ignored.
        with fitz.open(source) as a, fitz.open(source) as b:
            b[0].draw_rect(fitz.Rect(150,300,250,380),color=None,fill=(0,0,0))
            assert outside_change(a,0,a,0,[])==0 and outside_change(a,0,b,0,[])>.001
            assert outside_change(a,0,b,0,[fitz.Rect(150,300,250,380)])==0
        undone=copy.deepcopy(m);b=undone['blocks'][2];b['translation_required']=True;del b['preservation_reason']
        assert merge(undone,{**zh,'P01B003':'OER 区'})['blocks'][2]['translation']=='OER 区'
        preserved=copy.deepcopy(m);preserved['blocks'][0]['translation_required']=False
        rejected(lambda:merge(preserved,{'P01B002':'translation'}))
    print('PASS: pre-marking (vector/rotated/running/bibliography/non-prose), acronym cues, undo pre-mark, strict merge, coverage, sub/sup tags, references, reader manifest, overflows, geometry conflicts, build, contact sheet, English residue, outside-region change')
if __name__=='__main__':check()
