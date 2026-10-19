"""Five-layer workflow: local parsing, classification, routing, terminology, rebuild."""
import json,subprocess,sys,hashlib,shutil,re
from pathlib import Path
from copy import deepcopy
import pymupdf as fitz
from .pdf_reader import parse
from .classifier import classify
from .layout import compile_layout,LayoutError
from .rebuild import rebuild,legacy
from .validator import validate
from .runlog import RunLog
from .structure import preflight as structure_preflight, fingerprint
from .fonts import resolve_fonts
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

def prepare(source,work,config=None,extractor='local',mineru_cli=None,force=False):
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
    with log.stage('parse',extractor=extractor,source_sha256=source_sha256):inspection=parse(source,work/'inspect')
    write(work/'inspect/source.json',inspection)
    if extractor=='mineru':
        command=[sys.executable,'-X','utf8',str(Path(__file__).resolve().parents[1]/'scripts/mineru_extract.py'),
                 '--source',str(source),'--output',str(work/'extraction'),'--mode','precision']
        if mineru_cli:command+=['--cli',str(mineru_cli)]
        with log.stage('mineru'):subprocess.run(command,check=True)
    elif extractor!='local':raise ValueError('Unknown extraction mode')
    with log.stage('classify'):mapping=classify(inspection)
    mapping['source_path']=str(Path(source).resolve())
    mapping['source_sha256']=source_sha256
    mapping['workflow_version']='4.1'
    write(work/'source_map.json',mapping)
    with log.stage('source_preflight'):
        gate=structure_preflight(mapping,inspection)
        write(work/'structure-preflight.json',gate)
    # A candidate map is not a dispatch authorization. plan() freezes the map
    # after specific review issues and blocking geometry defects are resolved.
    jobs=[]
    write(work/'jobs.json',jobs)
    write(work/'review.json',{'issues':gate['issues'],'need_review':not gate['ready_to_dispatch'],'coverage':mapping['prepare_summary'],
                             'ready_to_dispatch':False,'blocking_count':sum(i['severity']=='blocking' for i in gate['issues']),
                             'review_required_count':sum(i['severity']=='review-required' for i in gate['issues'])})
    log.event('prepare','completed',source_pages=inspection['page_count'],translation_jobs=len(jobs),source_ready=gate['ready_to_dispatch'])
    return mapping,jobs

def plan(work,config=None,reviewed=False):
    work=Path(work);log=RunLog(work);mapping=read(work/'source_map.json')
    verify_source(mapping)
    inspection=read(work/'inspect/source.json') if (work/'inspect/source.json').exists() else None
    if mapping.get('workflow_version')=='4.1' and inspection is None:
        raise LayoutError([{'check':'missing-original-inspection'}])
    with log.stage('source_preflight'):
        gate=structure_preflight(mapping,inspection)
        write(work/'structure-preflight.json',gate)
        if not gate['ready_to_dispatch'] or not (reviewed or mapping.get('layout_reviewed')):
            raise LayoutError(gate['issues'] or [{'check':'source-map-not-reviewed'}])
    with log.stage('translation_plan'):
        jobs=make_router(config,work,log).plan(mapping)
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
    work=Path(work);log=RunLog(work)
    mapping=read(work/'source_map.json')
    resolutions=mapping.setdefault('review_resolutions',{})
    resolutions[issue_key]=note
    write(work/'source_map.json',mapping)
    with log.stage('resolve',issue_key=issue_key):
        gate=structure_preflight(mapping,read(work/'inspect/source.json') if (work/'inspect/source.json').exists() else None)
        write(work/'structure-preflight.json',gate)
        write(work/'review.json',{'issues':gate['issues'],'need_review':not gate['ready_to_dispatch'],'coverage':mapping.get('prepare_summary'),
                                 'ready_to_dispatch':gate['ready_to_dispatch'],
                                 'blocking_count':sum(i['severity']=='blocking' for i in gate['issues']),
                                 'review_required_count':sum(i['severity']=='review-required' for i in gate['issues'])})
    jobs=plan(work,config,reviewed=True)
    log.event('resolve','completed',issue_key=issue_key,jobs=len(jobs))
    return {'resolved':issue_key,'note':note,'map_frozen':True,'jobs':len(jobs),
            'remaining_review':sum(1 for i in gate['issues'] if i['severity']=='review-required')}

def translate(work,config=None,translations=None,font_dir=None):
    """Resume translation without rebuilding PDF; host receives only missing IDs."""
    work=Path(work);log=RunLog(work);mapping=read(work/'source_map.json')
    verify_source(mapping)
    frozen=work/'frozen-map.json'
    if not frozen.exists() or read(frozen)['map_fingerprint']!=fingerprint(mapping):
        raise LayoutError([{'check':'missing-or-stale-translation-plan'}])
    gate=structure_preflight(mapping)
    if not gate['ready_to_dispatch']:raise LayoutError(gate['issues'])
    with log.stage('translate_validate'):
        try:result=make_router(config,work,log).translate(mapping,supplied=read(translations) if translations else None)
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
            missing=glyph_report(mapping,result,config,font_dir)
            if missing:
                write(work/'glyph-preflight.json',missing)
                log.event('glyph_preflight','warning',missing_ids=sorted(missing),missing_chars={k:''.join(v) for k,v in missing.items()})
    return result

def finish(source,work,output,font_dir,translations=None,config=None,reviewed=False,fonts=None):
    work=Path(work);log=RunLog(work)
    write(work/'delivery.json',{'status':'building','output':str(Path(output).resolve()),'engine':engine_identity()})
    if source is None:
        # A4: prepare snapshots the original to work/source.pdf; use it by default.
        source=work/'source.pdf'
        if not Path(source).is_file():
            raise ValueError('Missing source PDF; pass --source or ensure work/source.pdf exists.')
    mapping=read(work/'source_map.json')
    if mapping.get('source_sha256') and mapping['source_sha256']!=hashlib.sha256(Path(source).read_bytes()).hexdigest():
        raise ValueError('Source PDF changed since preparation')
    if not reviewed and not mapping.get('layout_reviewed'):
        raise LayoutError([{'check':'source-map-not-reviewed'}])
    with log.stage('source_preflight'):
        gate=structure_preflight(mapping)
        write(work/'structure-preflight.json',gate)
        if not gate['passed'] or (mapping.get('workflow_version')=='4.1' and not gate['ready_to_dispatch']):raise LayoutError(gate['issues'])
        frozen=work/'frozen-map.json'
        if mapping.get('workflow_version')=='4.1' and not frozen.exists():
            # Legacy reviewed manifests remain compatible; new prepares must plan.
            plan(work,config,reviewed=reviewed)
        if frozen.exists() and read(frozen)['map_fingerprint']!=fingerprint(mapping):
            raise LayoutError([{'check':'stale-translation-plan','action':'Review map and regenerate jobs before translation.'}])
    with log.stage('translate_validate'):
        supplied=read(translations) if translations else None
        # Explicit, newly supplied revisions take priority over a previous cache.
        router=make_router(config,work,log)
        if supplied is not None:
            expected={b['id'] for b in mapping['blocks'] if b.get('translation_required',True)}
            if set(supplied)!=expected:raise ValueError('Translations do not cover the full reviewed source map')
            result=router.translate(mapping,supplied=supplied)
        else:result=router.translate(mapping)
        merged=deepcopy(mapping)
        for b in merged['blocks']:
            if b['id'] in result:b['translation']=result[b['id']]
        merged['layout_reviewed']=True
        titles=[b['translation'] for b in merged['blocks'] if b.get('type')=='title' and b.get('translation')]
        if titles:merged['title']=' '.join(titles)
        write(work/'merged.json',merged)
    with log.stage('layout'):
        manifest=compile_layout(merged,fonts=resolve_fonts(fonts or mapping.get('fonts') or load_config(config).get('fonts'),font_dir))
        write(work/'layout.json',manifest)
    # The proven writer performs and saves preflight before touching the source.
    # Running the same fit calculation here again doubles this stage's work.
    with log.stage('rebuild'):
        report=rebuild(source,work/'layout.json',output,font_dir)
    with log.stage('functional_validate'):
        check=validate(source,output,manifest,merged)
        check['layout_warnings']=report.get('warnings',[])
        check['delivery_status']=report.get('delivery_status','passed') if check['passed'] else 'failed'
        write(work/'functional-validation.json',check)
        if not check['passed']:raise ValueError('Functional PDF validation failed')
    write(work/'delivery.json',{'status':report.get('delivery_status','passed'),'output':str(Path(output).resolve()),
                              'output_sha256':report['output_sha256'],'warning_count':report.get('warning_count',0),
                              'engine':engine_identity()})
    log.event('finish','completed',output_pages=report['total_pages'],scientific_candidates=len(check['scientific_audit']['issues']))
    return report,check
