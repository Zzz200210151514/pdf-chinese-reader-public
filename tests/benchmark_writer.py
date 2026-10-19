"""Sequential same-input writer comparison. Papers/fonts/reports remain local."""
import argparse,json,subprocess,sys,time
from pathlib import Path
import pymupdf

def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('source','manifest','font-dir','baseline-skill','output-dir'):p.add_argument('--'+name,required=True)
    args=p.parse_args();directory=Path(args.output_dir);directory.mkdir(parents=True,exist_ok=True)
    records=[];outputs=[]
    for label,root in [('baseline',Path(args.baseline_skill)),('current',Path(__file__).resolve().parents[1])]:
        output=directory/(label+'.pdf');clock=time.perf_counter()
        command=[sys.executable,'-X','utf8',str(root/'scripts/pdf_reader.py'),'build',
                 '--source',args.source,'--manifest',args.manifest,'--font-dir',args.font_dir,'--output',str(output)]
        result=subprocess.run(command,capture_output=True,text=True,encoding='utf-8')
        (directory/(label+'.log')).write_text(result.stdout+result.stderr,'utf-8')
        result.check_returncode()
        report=json.loads(output.with_suffix('.validation.json').read_text('utf-8'))
        records.append({'variant':label,'process_seconds':time.perf_counter()-clock,'timings':report['timings'],
                        'passed':report['structural_checks_passed'],'protected_count':len(report['protected'])})
        outputs.append(output)
    pages=[]
    with pymupdf.open(outputs[0]) as a,pymupdf.open(outputs[1]) as b:
        if len(a)!=len(b):raise ValueError('Output page count changed')
        for i in range(len(a)):
            def links(page):return [{k:v for k,v in link.items() if k not in {'xref','id'}} for link in page.get_links()]
            pages.append({'page':i+1,'pixels_equal':a[i].get_pixmap(dpi=120).samples==b[i].get_pixmap(dpi=120).samples,
                          'text_equal':a[i].get_text()==b[i].get_text(),'links_equal':links(a[i])==links(b[i])})
    report={'sequence':'sequential, identical source/manifest/fonts','results':records,'equivalence':pages,
            'passed':all(all(r[k] for k in ('pixels_equal','text_equal','links_equal')) for r in pages),
            'model_calls':0,'semantic_accuracy':'not measured','repetitions':1}
    (directory/'comparison.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str),'utf-8')
    print(json.dumps({'passed':report['passed'],'results':records},ensure_ascii=False))
    if not report['passed']:raise SystemExit(1)

if __name__=='__main__':main()
