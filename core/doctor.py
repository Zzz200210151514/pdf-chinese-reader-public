"""Work-directory status and pre-prepare environment probe (doctor)."""
import hashlib, importlib, json, platform, sys
from pathlib import Path
from . import engine_identity

DEPENDENCIES = [
    ('pymupdf', 'PyMuPDF (PDF parse/build)'),
    ('yaml', 'PyYAML (config & terminology)'),
    ('fonttools', 'fonttools (optional: HTML font embedding)'),
]

# role -> list of (filename candidates). First hit wins per role.
FONT_CANDIDATES = {
    'Windows': {
        'regular': ['msyh.ttc', 'simsun.ttc', 'simhei.ttf', 'Deng.ttf'],
        'bold': ['msyhbd.ttc', 'simhei.ttf'],
        'fallback': ['seguisym.ttf', 'msjh.ttc'],
    },
    'Darwin': {
        'regular': ['PingFang.ttc', 'Songti.ttc', 'Hiragino Sans GB.ttc', 'STHeiti Light.ttc'],
        'bold': ['PingFang.ttc', 'STHeiti Medium.ttc'],
        'fallback': ['LastResort.otf', 'Apple Symbols.ttf'],
    },
    'Linux': {
        'regular': ['NotoSansCJK-Regular.ttc', 'NotoSansCJKsc-Regular.otf', 'wqy-microhei.ttc',
                    'NotoSansCJK-Regular.otf', 'LXGWWenKai-Regular.ttf'],
        'bold': ['NotoSansCJK-Bold.ttc', 'NotoSansCJKsc-Bold.otf', 'wqy-zenhei.ttc'],
        'fallback': ['DejaVuSans.ttf', 'NotoSansSymbols-Regular.ttf'],
    },
}


def _font_dirs():
    sysname = platform.system()
    if sysname == 'Windows':
        return [Path(r'C:/Windows/Fonts')]
    if sysname == 'Darwin':
        return [Path('/System/Library/Fonts'), Path('/Library/Fonts'), Path.home() / 'Library/Fonts']
    return [Path('/usr/share/fonts'), Path('/usr/local/share/fonts'),
            Path.home() / '.fonts', Path.home() / '.local/share/fonts']


def _find_font(names):
    dirs = _font_dirs()
    for name in names:
        for d in dirs:
            p = d / name
            if p.is_file():
                return str(p)
    return None


def _check_imports():
    rows = []
    for module, label in DEPENDENCIES:
        try:
            m = importlib.import_module(module)
            ver = getattr(m, '__version__', None) or getattr(m, 'version', None)
            rows.append({'module': module, 'name': label, 'ok': True, 'version': str(ver) if ver else 'present'})
        except Exception as exc:
            rows.append({'module': module, 'name': label, 'ok': False, 'error': f'{type(exc).__name__}: {exc}'})
    return rows


def _scientific_chars(text):
    return {ch for ch in text if ('\u0370' <= ch <= '\u03ff' or '\u2070' <= ch <= '\u209f'
            or ch in '¹²³±×÷−≈≤≥∂∑∫∞')}


def doctor(source=None, font_dir=None):
    """Probe dependencies and system fonts before prepare; emit a paste-ready fonts config.

    With --source, also pre-checks Greek letters / super-subscripts in the PDF
    against the resolved fonts so a missing-symbol font is found now, not at finish.
    """
    sysname = platform.system()
    result = {'platform': sysname, 'python': sys.version.split()[0],
              'dependencies': _check_imports(), 'fonts': {}, 'fonts_yaml': '', 'coverage': None}

    candidates = FONT_CANDIDATES.get(sysname, FONT_CANDIDATES['Linux'])
    if font_dir:
        dirs = [Path(font_dir)]
    else:
        dirs = _font_dirs()
    found = {}
    for role, names in candidates.items():
        hit = None
        for name in names:
            for d in dirs:
                p = d / name
                if p.is_file():
                    hit = str(p)
                    break
            if hit:
                break
        found[role] = hit
    result['fonts'] = found
    lines = ['fonts:']
    for role in ('regular', 'bold', 'fallback'):
        hit = found.get(role)
        if hit:
            lines.append(f'  {role}: {hit}')
        elif role != 'fallback':
            lines.append(f'  # {role}: <not found on this system — install a CJK font>')
    if not found.get('fallback'):
        lines.append('  # fallback: <optional; only for Greek/superscript symbols>')
    result['fonts_yaml'] = '\n'.join(lines)

    if source:
        try:
            import pymupdf as fitz
            doc = fitz.open(source)
            text = ''.join(page.get_text() for page in doc)
            chars = sorted(_scientific_chars(text))
            if chars:
                import pymupdf as _fitz
                fonts = []
                for role in ('regular', 'bold', 'fallback'):
                    path = found.get(role)
                    if path:
                        try:
                            fonts.append((role, _fitz.Font(fontfile=path)))
                        except Exception:
                            pass
                missing = []
                for ch in chars:
                    ok = any(f.has_glyph(ord(ch)) for _, f in fonts)
                    if not ok:
                        missing.append(ch)
                result['coverage'] = {'scanned_characters': ''.join(chars), 'missing': missing,
                                      'checked': bool(fonts)}
            else:
                result['coverage'] = {'scanned_characters': '', 'missing': [], 'checked': True}
        except Exception as exc:
            result['coverage'] = {'error': f'{type(exc).__name__}: {exc}'}
    result['engine']=engine_identity()
    return result


def status(work):
    """Read run artifacts and report the current stage plus the single next command."""
    work = Path(work)
    out = {'work': str(work), 'engine':engine_identity()}

    events = []
    ev_path = work / 'events.jsonl'
    if ev_path.exists():
        for line in ev_path.read_text('utf-8', errors='replace').splitlines():
            try:
                events.append(json.loads(line))
            except Exception:
                pass
    failures = [e for e in events if e.get('status') == 'failed']
    out['last_event'] = events[-1] if events else None
    out['failure_count'] = len(failures)
    out['last_failure'] = failures[-1] if failures else None

    review = {}
    rv_path = work / 'review.json'
    if rv_path.exists():
        try:
            review = json.loads(rv_path.read_text('utf-8-sig'))
        except Exception:
            review = {}
    out['review'] = {'need_review': review.get('need_review'),
                     'blocking_count': review.get('blocking_count'),
                     'review_required_count': review.get('review_required_count')}

    has_map = (work / 'source_map.json').exists()
    frozen = (work / 'frozen-map.json').exists()
    pending = work / 'pending-jobs.json'
    has_pending = pending.exists() and pending.stat().st_size > 2
    has_translations = (work / 'translations.json').exists()
    has_output=False
    delivery_path=work/'delivery.json'
    if delivery_path.exists():
        try:
            delivery=json.loads(delivery_path.read_text('utf-8-sig'))
            target=Path(delivery.get('output',''))
            has_output=(delivery.get('status') in ('passed','passed_with_warnings') and target.is_file()
                        and hashlib.sha256(target.read_bytes()).hexdigest()==delivery.get('output_sha256'))
            out['delivery']={**delivery,'output_verified':has_output}
        except (OSError,ValueError,TypeError):pass
    elif (work/'functional-validation.json').exists():
        try:
            checks=json.loads((work/'functional-validation.json').read_text('utf-8-sig'))
            completed=[i for i,e in enumerate(events) if e.get('stage')=='finish' and e.get('status')=='completed']
            failed=[i for i,e in enumerate(events) if e.get('status')=='failed']
            has_output=checks.get('passed') is True and bool(completed) and max(completed)>max(failed,default=-1)
        except (OSError,ValueError):pass

    if not has_map:
        step, cmd = 'prepare', 'translator_v4.py prepare --source <paper.pdf> --work <work>'
    elif review.get('need_review') and not frozen:
        step, cmd = 'review', 'translator_v4.py resolve --issue-key <id|check|page> --note "<handling>" (repeat per issue)'
    elif not frozen:
        step, cmd = 'plan', 'translator_v4.py plan --work <work> --reviewed'
    elif has_output:
        step, cmd = 'verify', 'Deliver the validated PDF. Warnings do not require rebuilding; a quick overview is optional.'
    elif has_pending:
        step, cmd = 'translate', 'translator_v4.py translate --work <work> --translations <batch.json> (then finish)'
    elif not has_translations:
        step, cmd = 'translate', 'translator_v4.py translate --work <work>'
    elif not has_output:
        step, cmd = 'finish', 'translator_v4.py finish --work <work> --output <out.zh.pdf> --font-dir <dir> --reviewed'
    else:
        step, cmd = 'verify', 'Deliver the validated PDF. Warnings do not require rebuilding.'
    out['stage'] = step
    out['next_command'] = cmd
    return out
