"""Check translation block coverage, numeric tokens, citations, and term-ledger consistency.
This does not certify semantic translation accuracy.
"""
import argparse,json,re,unicodedata
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
class Plain(HTMLParser):
 def __init__(self,s):super().__init__();self.parts=[];self.feed(s)
 def handle_data(self,s):self.parts.append(s)
 def text(self):return ''.join(self.parts)
def numbers(s):
 s=re.sub(r'\[\[(?:fig|ref|eq|table):[^\]\n]+\]\]', '', s)
 s=unicodedata.normalize('NFKC',Plain(s).text()).replace('−','-')
 return Counter(re.findall(r'(?<![A-Za-z])[-+]?\d+(?:[.,]\d+)*(?:[eE][-+]?\d+)?',s))
def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--map',required=True);p.add_argument('--output',required=True);a=p.parse_args()
 m=json.loads(Path(a.map).read_text(encoding='utf-8-sig'));blocks=m['blocks'];terms=m.get('terminology_ledger',[]);issues=[];ids=[]
 for b in blocks:
  ident=b['id'];ids.append(ident)
  if b.get('translation_required',True) is False:continue
  en=b.get('source_text',b.get('text_en',''));zh=b.get('translation',b.get('text_zh',''))
  if not en or not zh:issues.append({'id':ident,'check':'missing-source-or-translation'});continue
  if numbers(en)!=numbers(zh):issues.append({'id':ident,'check':'numeric-signature-review','source':dict(numbers(en)),'translation':dict(numbers(zh))})
  if 'source_citations' in b and Counter(b['source_citations'])!=Counter(b.get('translation_citations',[])):issues.append({'id':ident,'check':'citation-mismatch'})
  for term in terms:
   if term['english'].lower() in en.lower() and not any(v in zh for v in [term['chinese']]+term.get('allowed_variants',[])):issues.append({'id':ident,'check':'term-review','term':term['english'],'expected':term['chinese']})
 if len(ids)!=len(set(ids)):issues.append({'check':'duplicate-block-ids'})
 expected=m.get('expected_block_ids')
 if expected is not None and set(expected)!=set(ids):issues.append({'check':'block-coverage-mismatch'})
 report={'block_count':len(blocks),'issues':issues,'mechanical_checks_passed':not issues,'semantic_accuracy':'NOT CERTIFIED','academic_style':'REQUIRES REVIEW','source_extraction_coverage':'requires independently prepared expected_block_ids and PDF comparison'}
 Path(a.output).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps({'blocks':len(blocks),'issues':len(issues),'semantic_accuracy':'NOT CERTIFIED'},ensure_ascii=False))
 raise SystemExit(bool(issues))
if __name__=='__main__':main()
