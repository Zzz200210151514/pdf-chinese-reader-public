"""Five-layer workflow: local parsing, classification, routing, terminology, rebuild."""
import json,hashlib,shutil,re
from pathlib import Path
from copy import deepcopy
import pymupdf as fitz
from .pdf_reader import parse
from .classifier import classify
from .paragraphs import link_paragraphs, load_mineru_items
from .mineru_hybrid import extract_with_mineru, fuse_mineru, mineru_configured
from .layout import compile_layout,LayoutError
from .rebuild import rebuild,legacy
from .validator import validate
from .runlog import RunLog
from .structure import preflight as structure_preflight, fingerprint, reconcile_source_map
from .fonts import resolve_fonts
from .reflow_pdf import build_reflow_pdf_from_merged
from .sampling import sample_and_report
from . import engine_identity
from llm import Router,HostHandoffRequired

def write(path,data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(data,ensure_ascii=False,indent=2),'utf-8')

def read(path):
    from llm.router import strict_json
    return strict_json(Path(path).read_text('utf-8-sig'))

def load_config(config):
    import yaml
    if config is None:config=Path(__file__).resolve().parents[1]/'config.yaml'
    if isinstance(config,(str,Path)):config=yaml.safe_load(Path(config).read_text('utf-8')) or {}
    return config

def _is_resilient(config):
    return load_config(config).get('delivery_mode')=='resilient'

def make_router(config,work,log):
    config=load_config(config)
    def record(item):
        item={**item,'workflow_id':log.workflow_id}
        with (work/'translation-events.jsonl').open('a',encoding='utf-8') as f:
            f.write(json.dumps(item,ensure_ascii=False)+'\n')
        log.event('translation_call',item['status'],**{k:v for k,v in item.items() if k not in ('status','workflow_id')})
    return Router({**config,'workflow_id':log.workflow_id},cache_dir=work/'cache',log=record)

def _font_path(name,font_dir):
    if not name:return None
    p=Path(name)
    if p.is_file():return p
    if font_dir and (Path(font_dir)/name).is_file():return Path(font_dir)/name
    return None

def glyph_report(mapping,translations,config,font_dir):
    """Per-ID missing-glyph pre-check using the configured fonts.

    Returns {id: [missing chars]}; empty/None when fonts cannot be resolved here.
    This moves the late finish-stage hard failure forward to translation time.
    """
    fonts_conf=mapping.get('fonts') or load_config(config).get('fonts')
    try:resolved=resolve_fonts(fonts_conf,font_dir)
    except Exception:return {}
    regular=_font_path(resolved.get('regular'),font_dir)
    if not regular:return {}
    bold=_font_path(resolved.get('bold'),font_dir) or regular
    fallback=_font_path(resolved.get('fallback'),font_dir)
    try:
        rf=fitz.Font(fontfile=str(regular));bf=fitz.Font(fontfile=str(bold))
        ff=fitz.Font(fontfile=str(fallback)) if fallback else None
    except Exception:return {}
    report={}
    for bid,text in translations.items():
        if not isinstance(text,str):continue
        text=re.sub(r'<[^>]+>','',text)
        missing=sorted({ch for ch in text if not ch.isspace() and not ch.isascii()
                        and not (rf.has_glyph(ord(ch)) or bf.has_glyph(ord(ch)) or (ff and ff.has_glyph(ord(ch))))})
        if missing:report[bid]=missing
    return report

def prepare(source,work,config=None,extractor='auto',mineru_cli=None,force=False,*,reuse_extraction=False):
    work=Path(work);log=RunLog(work)
    # A2: never silently discard a reviewed map or frozen plan. Re-running prepare
    # would overwrite review_resolutions; require an explicit force to start fresh.
    prior_map=work/'source_map.json'
    if not force and prior_map.exists():
        try:prior=read(prior_map)
        except Exception:prior={}
        if prior.get('review_resolutions') or (work/'frozen-map.json').exists():
            raise ValueError('Work directory already holds review decisions or a frozen plan; '
                             're-running prepare would overwrite them. Resolve issues with '
                             '`resolve`, or pass force/--force to start a fresh work directory.')
    source_sha256=hashlib.sha256(Path(source).read_bytes()).hexdigest()
    # Drag/drop files may be removed by Zotero between stages. Keep the immutable
    # original locally, without requiring agents to find it again after failure.
    stable=work/'source.pdf'
    with log.stage('source_snapshot',source_sha256=source_sha256):
        if Path(source).resolve()!=stable.resolve():
            if stable.exists() and hashlib.sha256(stable.read_bytes()).hexdigest()!=source_sha256:
                raise ValueError('Work directory has a different source; use a new directory')
            if not stable.exists():
                temporary=work/'source.pdf.tmp';shutil.copyfile(source,temporary);temporary.replace(stable)
        source=stable
    requested=(extractor or 'auto').lower()
    if requested not in {'auto','mineru','local'}:raise ValueError('Unknown extraction mode')
    notes=[]
    mode=requested
    if requested=='auto':
        if mineru_configured():mode='mineru'
        else:
            mode='local'
            notes.append({'check':'mineru-not-configured','severity':'warning',
                          'detail':'No MINERU_API_KEY, MINERU_TOKEN, or token file; local extraction used.'})
    with log.stage('parse',extractor=requested,source_sha256=source_sha256):inspection=parse(source,work/'inspect')
    write(work/'inspect/source.json',inspection)
    used=mode
    if mode=='mineru':
        staged=reuse_extraction and bool(load_mineru_items(work/'extraction'))
        if staged:
            ok,detail=True,''
        else:
            with log.stage('mineru'):ok,detail=extract_with_mineru(source,work/'extraction',mineru_cli)
        if not ok:
            if requested=='auto':
                used='local'
                notes.append({'check':'mineru-fallback','severity':'warning','detail':detail or 'MinerU extraction failed; local extraction used.'})
            else:raise ValueError(detail or 'MinerU extraction failed')
    with log.stage('classify'):
        mapping=classify(inspection)
        if used=='mineru':
            report=fuse_mineru(mapping, work/'extraction')
            if not report.get('applied'):
                notes.append({'check':'mineru-structure-missing','severity':'warning',
                              'detail':report.get('reason') or 'MinerU content list missing; geometric paragraphs used.'})
                link_paragraphs(mapping, None)
                used='local'
        else:link_paragraphs(mapping, None)
    cfg=load_config(config)
    resilient=_is_resilient(cfg)
    mapping['extraction']={'requested':requested,'extractor':used,'warnings':notes}
    mapping['source_path']=str(Path(source).resolve())
    mapping['source_sha256']=source_sha256
    mapping['workflow_version']='4.1'
    if resilient:
        mapping,_=reconcile_source_map(mapping,inspection)
    write(work/'source_map.json',mapping)
    with log.stage('source_preflight'):
        gate=structure_preflight(mapping,inspection,resilient=resilient)
        write(work/'structure-preflight.json',gate)
    # A candidate map is not a dispatch authorization. plan() freezes the map
    # after specific review issues and blocking geometry defects are resolved.
    jobs=[]
    write(work/'jobs.json',jobs)
    write(work/'review.json',{'issues':gate['issues'],'need_review':not gate['ready_to_dispatch'],'coverage':mapping['prepare_summary'],
                             'ready_to_dispatch':gate['ready_to_dispatch'] if resilient else False,
                             'blocking_count':sum(i['severity']=='blocking' for i in gate['issues']),
                             'review_required_count':sum(i['severity']=='review-required' for i in gate['issues'])})
    log.event('prepare','completed',source_pages=inspection['page_count'],translation_jobs=len(jobs),source_ready=gate['ready_to_dispatch'])
    return mapping,jobs

def plan(work,config=None,reviewed=False):
    work=Path(work);log=RunLog(work);mapping=read(work/'source_map.json')
    cfg=load_config(config);resilient=_is_resilient(cfg)
    verify_source(mapping)
    inspection=read(work/'inspect/source.json') if (work/'inspect/source.json').exists() else None
    if mapping.get('workflow_version')=='4.1' and inspection is None:
        raise LayoutError([{'check':'missing-original-inspection'}])
    with log.stage('source_preflight'):
        if resilient:
            mapping,_=reconcile_source_map(mapping,inspection)
            mapping['layout_reviewed']=True
            write(work/'source_map.json',mapping)
        gate=structure_preflight(mapping,inspection,resilient=resilient)
        write(work/'structure-preflight.json',gate)
        if not resilient and (not gate['ready_to_dispatch'] or not (reviewed or mapping.get('layout_reviewed'))):
            raise LayoutError(gate['issues'] or [{'check':'source-map-not-reviewed'}])
        if resilient and not gate['passed']:
            raise LayoutError(gate['issues'])
    with log.stage('translation_plan'):
        jobs=make_router(cfg,work,log).plan(mapping)
        write(work/'jobs.json',jobs)
        write(work/'frozen-map.json',{'map_fingerprint':fingerprint(mapping),'source_order':gate['source_order'],
                                    'workflow_id':getattr(log,'workflow_id',None)})
    return jobs

def verify_source(mapping):
    if mapping.get('source_path') and mapping.get('source_sha256'):
        if hashlib.sha256(Path(mapping['source_path']).read_bytes()).hexdigest()!=mapping['source_sha256']:
            raise ValueError('Source PDF changed since preparation')

def resolve_issue(work,issue_key,note,config=None):
    """A3: record one review decision and immediately re-freeze the plan.

    Agents no longer hand-edit JSON and re-run plan manually; one command writes
    the resolution into source_map.review_resolutions and re-plans, so the frozen
    fingerprint always matches the reviewed map.
    """
    work=Path(work);log=RunLog(work);cfg=load_config(config);resilient=_is_resilient(cfg)
    mapping=read(work/'source_map.json')
    resolutions=mapping.setdefault('review_resolutions',{})
    resolutions[issue_key]=note
    write(work/'source_map.json',mapping)
    with log.stage('resolve',issue_key=issue_key):
        gate=structure_preflight(mapping,read(work/'inspect/source.json') if (work/'inspect/source.json').exists() else None,resilient=resilient)
        write(work/'structure-preflight.json',gate)
        write(work/'review.json',{'issues':gate['issues'],'need_review':not gate['ready_to_dispatch'],'coverage':mapping.get('prepare_summary'),
                                 'ready_to_dispatch':gate['ready_to_dispatch'],
                                 'blocking_count':sum(i['severity']=='blocking' for i in gate['issues']),
                                 'review_required_count':sum(i['severity']=='review-required' for i in gate['issues'])})
    # Resolving one of N issues must exit cleanly. Freeze the plan only when
    # nothing review-required remains; otherwise the caller still has work.
    jobs=[]
    frozen_now=False
    if gate['ready_to_dispatch']:
        jobs=plan(work,cfg,reviewed=True)
        frozen_now=True
    log.event('resolve','completed',issue_key=issue_key,jobs=len(jobs),map_frozen=frozen_now)
    return {'resolved':issue_key,'note':note,'map_frozen':frozen_now,'jobs':len(jobs),
            'ready_to_dispatch':gate['ready_to_dispatch'],
            'remaining_review':sum(1 for i in gate['issues'] if i['severity']=='review-required')}

def translate(work,config=None,translations=None,font_dir=None):
    """Resume translation without rebuilding PDF; host receives only missing IDs."""
    work=Path(work);log=RunLog(work);cfg=load_config(config);resilient=_is_resilient(cfg)
    mapping=read(work/'source_map.json')
    verify_source(mapping)
    frozen=work/'frozen-map.json'
    if not frozen.exists() or read(frozen)['map_fingerprint']!=fingerprint(mapping):
        if resilient:
            plan(work,cfg,reviewed=True)
            mapping=read(work/'source_map.json')
        else:
            raise LayoutError([{'check':'missing-or-stale-translation-plan'}])
    gate=structure_preflight(mapping,resilient=resilient)
    if not gate['ready_to_dispatch']:raise LayoutError(gate['issues'])
    with log.stage('translate_validate'):
        try:result=make_router(cfg,work,log).translate(mapping,supplied=read(translations) if translations else None)
        except HostHandoffRequired as exc:
            # Only public translation input reaches agents; no geometry/full map.
            jobs=[{k:v for k,v in j.items() if not k.startswith('_') and k!='route_config'} for j in exc.jobs]
            write(work/'pending-jobs.json',jobs)
            log.event('host_handoff','waiting',jobs=len(jobs),usage=None)
            return None
        write(work/'translations.json',result)
        (work/'pending-jobs.json').unlink(missing_ok=True)
        # B4: report missing glyphs per ID now, not as a finish-stage hard failure.
        if translations and font_dir:
            missing=glyph_report(mapping,result,cfg,font_dir)
            if missing:
                write(work/'glyph-preflight.json',missing)
                log.event('glyph_preflight','warning',missing_ids=sorted(missing),missing_chars={k:''.join(v) for k,v in missing.items()})
    return result

def finish(source,work,output,font_dir,translations=None,config=None,reviewed=False,fonts=None,layout_mode=None):
    work=Path(work);log=RunLog(work);cfg=load_config(config);resilient=_is_resilient(cfg)
    effective_layout=(layout_mode or cfg.get('layout_mode') or ('auto' if resilient else 'inplace')).lower()
    write(work/'delivery.json',{'status':'building','output':str(Path(output).resolve()),'engine':engine_identity()})
    if source is None:
        # A4: prepare snapshots the original to work/source.pdf; use it by default.
        source=work/'source.pdf'
        if not Path(source).is_file():
            raise ValueError('Missing source PDF; pass --source or ensure work/source.pdf exists.')
    mapping=read(work/'source_map.json')
    if mapping.get('source_sha256') and mapping['source_sha256']!=hashlib.sha256(Path(source).read_bytes()).hexdigest():
        raise ValueError('Source PDF changed since preparation')
    if not reviewed and not mapping.get('layout_reviewed') and not resilient:
        raise LayoutError([{'check':'source-map-not-reviewed'}])
    with log.stage('source_preflight'):
        inspection=read(work/'inspect/source.json') if (work/'inspect/source.json').exists() else None
        if resilient:
            mapping,_=reconcile_source_map(mapping,inspection)
            mapping['layout_reviewed']=True
            write(work/'source_map.json',mapping)
        gate=structure_preflight(mapping,resilient=resilient)
        write(work/'structure-preflight.json',gate)
        if not gate['passed'] or (not resilient and mapping.get('workflow_version')=='4.1' and not gate['ready_to_dispatch']):raise LayoutError(gate['issues'])
        frozen=work/'frozen-map.json'
        if mapping.get('workflow_version')=='4.1' and not frozen.exists():
            # Legacy reviewed manifests remain compatible; new prepares must plan.
            plan(work,cfg,reviewed=True if resilient else reviewed)
        if frozen.exists() and read(frozen)['map_fingerprint']!=fingerprint(mapping):
            if resilient:
                plan(work,cfg,reviewed=True)
            else:
                raise LayoutError([{'check':'stale-translation-plan','action':'Review map and regenerate jobs before translation.'}])
    with log.stage('translate_validate'):
        supplied=read(translations) if translations else None
        # Explicit, newly supplied revisions take priority over a previous cache.
        router=make_router(cfg,work,log)
        if supplied is not None:
            # Continuation boxes are covered by the anchor's logical translation.
            expected={b['id'] for b in mapping['blocks'] if b.get('translation_required',True) and not b.get('translation_in')}
            if not resilient and set(supplied)!=expected:
                raise ValueError('Translations do not cover the full reviewed source map')
            try:
                result=router.translate(mapping,supplied=supplied)
            except HostHandoffRequired as exc:
                if not resilient:raise
                result=dict(exc.completed)
                for j in exc.jobs:
                    for b in j['blocks']:
                        if b['id'] not in result:
                            result[b['id']]=j['_source'][b['id']]
                            mapping.setdefault('fallback_records',[]).append({
                                'page':b.get('source_page'),'id':b['id'],'type':b.get('type','paragraph'),
                                'reason':'未提交该块译文，自动保留原文','source_text':j['_source'][b['id']][:120]})
        else:
            try:
                result=router.translate(mapping)
            except HostHandoffRequired as exc:
                if not resilient or not exc.completed:
                    raise
                result=dict(exc.completed)
                for j in exc.jobs:
                    for b in j['blocks']:
                        if b['id'] not in result:
                            result[b['id']]=j['_source'][b['id']]
                            mapping.setdefault('fallback_records',[]).append({
                                'page':b.get('source_page'),'id':b['id'],'type':b.get('type','paragraph'),
                                'reason':'未完成翻译，自动保留原文','source_text':j['_source'][b['id']][:120]})
        merged=deepcopy(mapping)
        for b in merged['blocks']:
            if b['id'] in result:b['translation']=result[b['id']]
        merged['layout_reviewed']=True
        titles=[b['translation'] for b in merged['blocks'] if b.get('type')=='title' and b.get('translation')]
        if titles:merged['title']=' '.join(titles)
        write(work/'merged.json',merged)
        translation_warnings=[dict(r) for r in router.records if r.get('severity')=='warning' or r.get('status')=='warning']
    resolved_fonts=resolve_fonts(fonts or mapping.get('fonts') or cfg.get('fonts'),font_dir)
    auto_switched=False
    if effective_layout=='reflow':
        with log.stage('rebuild',layout_mode='reflow'):
            report=build_reflow_pdf_from_merged(merged,source,work,output,font_dir,fonts_conf=resolved_fonts)
        check={'passed':True,'scientific_audit':{'issues':[]},'layout_warnings':report.get('warnings',[]),'delivery_status':report.get('delivery_status','passed')}
        write(work/'functional-validation.json',check)
    else:
        with log.stage('layout'):
            manifest=compile_layout(merged,fonts=resolved_fonts,resilient=resilient)
            if translation_warnings:
                manifest.setdefault('warnings', [])
                manifest['warnings']=[*manifest['warnings'], *translation_warnings]
            write(work/'layout.json',manifest)
        # The proven writer performs and saves preflight before touching the source.
        # Running the same fit calculation here again doubles this stage's work.
        with log.stage('rebuild'):
            report=rebuild(source,work/'layout.json',output,font_dir,resilient=resilient)
        with log.stage('functional_validate'):
            check=validate(source,output,manifest,merged)
            check['layout_warnings']=report.get('warnings',[])
            check['delivery_status']=report.get('delivery_status','passed') if (check['passed'] or resilient) else 'failed'
            write(work/'functional-validation.json',check)
            if not check['passed'] and not resilient:
                raise ValueError('Functional PDF validation failed')
        if resilient and effective_layout=='auto':
            translatable_cnt=max(1,sum(1 for b in merged['blocks'] if b.get('translation_required',True)))
            fallback_cnt=report.get('fallback_count',0)
            block_by_id={b['id']:b for b in merged['blocks']}
            overflow_ids=[str(w.get('id','')) for w in report.get('warnings',[]) if 'overflow' in str(w.get('check',''))]
            overflow_cnt=len(overflow_ids)
            body_overflow_cnt=0
            for oid in overflow_ids:
                parts=[block_by_id.get(pid) for pid in oid.split('+') if pid in block_by_id]
                if not parts or any(
                    p.get('type') not in ('table','formula') and p.get('rect')
                    and (p['rect'][2]-p['rect'][0])>=140 and (p['rect'][3]-p['rect'][1])>=18
                    for p in parts
                ):
                    body_overflow_cnt+=1
            if fallback_cnt/translatable_cnt>0.25 or overflow_cnt/translatable_cnt>0.22 or body_overflow_cnt>=max(8,int(0.12*translatable_cnt)):
                auto_switched=True
                inplace_copy=Path(output).with_suffix('.inplace.pdf')
                shutil.copyfile(output,inplace_copy)
                with log.stage('rebuild',layout_mode='reflow-auto-fallback'):
                    report=build_reflow_pdf_from_merged(merged,source,work,output,font_dir,fonts_conf=resolved_fonts)
                report['auto_switched_from_inplace']=str(inplace_copy)
                check['delivery_status']=report.get('delivery_status','passed_with_warnings')
                write(work/'functional-validation.json',check)
    sample_info=None
    if resilient:
        with log.stage('sampling'):
            sample_info=sample_and_report(source,output,work,report,check,mapping=merged,
                                          sample_pages=int(cfg.get('sample_check_pages',3)))
    write(work/'delivery.json',{'status':report.get('delivery_status','passed'),'output':str(Path(output).resolve()),
                              'output_sha256':report['output_sha256'],'warning_count':report.get('warning_count',0),
                              'fallback_count':report.get('fallback_count',len(report.get('fallback_records',[]))),
                              'unresolved_links_count':len(report.get('unresolved_links',[])),
                              'layout_mode':report.get('layout_mode','inplace'),
                              'auto_switched_to_reflow':auto_switched,
                              'report_md':sample_info.get('report_md') if sample_info else None,
                              'sampled_pages':sample_info.get('sampled_pages') if sample_info else None,
                              'engine':engine_identity()})
    log.event('finish','completed',output_pages=report['total_pages'],scientific_candidates=len(check['scientific_audit']['issues']))
    return report,check
