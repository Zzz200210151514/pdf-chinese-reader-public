"""Portable V4 prepare/finish commands; host model handoff is explicit and offline by default."""
import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core.pipeline import prepare,finish,plan,translate,resolve_issue,load_config
from core.doctor import status as work_status,doctor as run_doctor
from core.sampling import sample_and_report
from core import engine_identity

def _effective_config(config_path,delivery_mode=None,layout_mode=None):
    cfg=dict(load_config(config_path))
    if delivery_mode:cfg['delivery_mode']=delivery_mode
    else:cfg.setdefault('delivery_mode','resilient')
    if layout_mode:cfg['layout_mode']=layout_mode
    return cfg

def main():
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='command',required=True)
    a=sub.add_parser('prepare');a.add_argument('--source',required=True);a.add_argument('--work',required=True)
    a.add_argument('--config');a.add_argument('--extractor',choices=['auto','mineru','local'],default='auto',
                   help='auto runs MinerU when a token is configured and falls back to local');a.add_argument('--mineru-cli')
    a.add_argument('--delivery-mode',choices=['resilient','strict']);a.add_argument('--force',action='store_true',help='overwrite an existing reviewed/frozen work directory')
    a=sub.add_parser('finish');a.add_argument('--source',help='original PDF; defaults to the work/source.pdf snapshot')
    a.add_argument('--work',required=True)
    a.add_argument('--output',required=True);a.add_argument('--font-dir',required=True)
    a.add_argument('--translations');a.add_argument('--config');a.add_argument('--reviewed',action='store_true')
    a.add_argument('--delivery-mode',choices=['resilient','strict']);a.add_argument('--layout-mode',choices=['auto','inplace','reflow'])
    a=sub.add_parser('plan');a.add_argument('--work',required=True);a.add_argument('--config');a.add_argument('--reviewed',action='store_true');a.add_argument('--delivery-mode',choices=['resilient','strict'])
    a=sub.add_parser('translate');a.add_argument('--work',required=True);a.add_argument('--config');a.add_argument('--translations')
    a.add_argument('--font-dir',help='enables the per-ID missing-glyph pre-check on supplied batches');a.add_argument('--delivery-mode',choices=['resilient','strict'])
    a=sub.add_parser('resolve');a.add_argument('--work',required=True);a.add_argument('--issue-key',required=True)
    a.add_argument('--note',required=True);a.add_argument('--config');a.add_argument('--delivery-mode',choices=['resilient','strict'])
    a=sub.add_parser('sample');a.add_argument('--work',required=True);a.add_argument('--output',required=True)
    a.add_argument('--source');a.add_argument('--pages',type=int,default=3);a.add_argument('--seed',type=int)
    a=sub.add_parser('status');a.add_argument('--work',required=True)
    a=sub.add_parser('doctor');a.add_argument('--source');a.add_argument('--font-dir')
    args=p.parse_args()
    if args.command=='prepare':
        cfg=_effective_config(args.config,args.delivery_mode)
        m,jobs=prepare(args.source,args.work,cfg,args.extractor,args.mineru_cli,force=args.force)
        from core.pipeline import read
        review=read(Path(args.work)/'review.json')
        extraction=m.get('extraction') or {}
        print(json.dumps({'engine':engine_identity(),'jobs':len(jobs),'blocks':len(m['blocks']),'need_review':review['need_review'],
                          'blocking_count':review['blocking_count'],'review_required_count':review['review_required_count'],
                          'extractor':extraction.get('extractor'),'extraction_warnings':extraction.get('warnings') or [],
                          'fallback_count':len(m.get('fallback_records') or []),
                          'next_step':'plan then translate' if not review['need_review'] else 'review source map then plan',
                          'source_snapshot':str(Path(args.work)/'source.pdf'),'work':args.work},ensure_ascii=False))
    elif args.command=='plan':
        cfg=_effective_config(args.config,args.delivery_mode)
        jobs=plan(args.work,cfg,args.reviewed)
        print(json.dumps({'jobs':len(jobs),'map_frozen':True},ensure_ascii=False))
    elif args.command=='translate':
        cfg=_effective_config(args.config,args.delivery_mode)
        result=translate(args.work,cfg,args.translations,font_dir=args.font_dir)
        out={'completed':result is not None,'translated_ids':len(result or {}),
             'pending_jobs':str(Path(args.work)/'pending-jobs.json') if result is None else None}
        glyph=Path(args.work)/'glyph-preflight.json'
        if glyph.exists():
            from core.pipeline import read
            out['missing_glyphs']=read(glyph)
            out['glyph_hint']='IDs above contain characters the configured fonts lack; fix those IDs or set a symbol fallback font.'
        print(json.dumps(out,ensure_ascii=False))
    elif args.command=='resolve':
        cfg=_effective_config(args.config,args.delivery_mode)
        outcome=resolve_issue(args.work,args.issue_key,args.note,cfg)
        print(json.dumps(outcome,ensure_ascii=False))
    elif args.command=='sample':
        from core.pipeline import read
        work=Path(args.work)
        merged=read(work/'merged.json') if (work/'merged.json').exists() else (read(work/'source_map.json') if (work/'source_map.json').exists() else {})
        val_path=Path(args.output).with_suffix('.validation.json')
        report=read(val_path) if val_path.exists() else {}
        src=args.source or (work/'source.pdf' if (work/'source.pdf').exists() else None)
        print(json.dumps(sample_and_report(src,args.output,work,report,mapping=merged,sample_pages=args.pages,seed=args.seed),ensure_ascii=False))
    elif args.command=='status':
        print(json.dumps(work_status(args.work),ensure_ascii=False,indent=2))
    elif args.command=='doctor':
        print(json.dumps(run_doctor(args.source,args.font_dir),ensure_ascii=False,indent=2))
    else:
        cfg=_effective_config(args.config,args.delivery_mode,args.layout_mode)
        report,check=finish(args.source,args.work,args.output,args.font_dir,args.translations,cfg,args.reviewed,layout_mode=args.layout_mode)
        delivery=json.loads((Path(args.work)/'delivery.json').read_text('utf-8-sig')) if (Path(args.work)/'delivery.json').exists() else {}
        print(json.dumps({'engine':engine_identity(),'pages':report['total_pages'],'functional_checks_passed':check['passed'],
                          'delivery_status':report.get('delivery_status','passed'),'layout_mode':report.get('layout_mode','inplace'),
                          'warnings':report.get('warning_count',0),'fallback_count':delivery.get('fallback_count',0),
                          'sampled_pages':delivery.get('sampled_pages'),'report_md':delivery.get('report_md'),
                          'timings':report.get('timings')},ensure_ascii=False))
if __name__=='__main__':main()
