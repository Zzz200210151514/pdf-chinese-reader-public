"""Layer 5: use the proven legacy PDF writer without a second reconstruction engine."""
import sys,json
from pathlib import Path
from types import SimpleNamespace
SCRIPT_DIR=Path(__file__).resolve().parents[1]/'scripts'
if str(SCRIPT_DIR) not in sys.path:sys.path.insert(0,str(SCRIPT_DIR))
import pdf_reader as legacy

def rebuild(source,manifest,output,font_dir):
    if isinstance(manifest,dict):
        path=Path(output).with_suffix('.layout.json');path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),'utf-8')
    else:path=Path(manifest)
    legacy.build(SimpleNamespace(source=str(source),manifest=str(path),output=str(output),font_dir=str(font_dir)))
    return json.loads(Path(output).with_suffix('.validation.json').read_text('utf-8'))
