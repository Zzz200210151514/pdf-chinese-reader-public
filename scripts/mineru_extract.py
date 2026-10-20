"""Run MinerU with a content-addressed local cache. No translation is performed here."""
import argparse,hashlib,json,shutil,subprocess,time,tempfile,uuid
from pathlib import Path

def stored_token():
 """Read the optional per-user Windows Credential Manager entry without printing it."""
 if __import__('os').name!='nt':return None
 import ctypes
 from ctypes import wintypes
 class CREDENTIAL(ctypes.Structure):
  _fields_=[('Flags',wintypes.DWORD),('Type',wintypes.DWORD),('TargetName',wintypes.LPWSTR),
   ('Comment',wintypes.LPWSTR),('LastWritten',wintypes.FILETIME),('CredentialBlobSize',wintypes.DWORD),
   ('CredentialBlob',ctypes.POINTER(ctypes.c_ubyte)),('Persist',wintypes.DWORD),('AttributeCount',wintypes.DWORD),
   ('Attributes',ctypes.c_void_p),('TargetAlias',wintypes.LPWSTR),('UserName',wintypes.LPWSTR)]
 pointer=ctypes.POINTER(CREDENTIAL)();api=ctypes.WinDLL('Advapi32.dll',use_last_error=True)
 api.CredReadW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.POINTER(ctypes.POINTER(CREDENTIAL))]
 api.CredReadW.restype=wintypes.BOOL
 if not api.CredReadW('pdf-chinese-reader/MinerU',1,0,ctypes.byref(pointer)):return None
 try:return ctypes.string_at(pointer.contents.CredentialBlob,pointer.contents.CredentialBlobSize).decode('utf-8')
 finally:
  api.CredFree.argtypes=[ctypes.POINTER(CREDENTIAL)];api.CredFree.restype=None;api.CredFree(pointer)

def main():
 p=argparse.ArgumentParser(description=__doc__)
 p.add_argument('--source',required=True);p.add_argument('--output',required=True)
 p.add_argument('--mode',choices=['precision','flash'],default='precision')
 p.add_argument('--cli',help='Path to the MinerU native executable when not on PATH')
 p.add_argument('--model',choices=['vlm','pipeline'],default='pipeline')
 p.add_argument('--structured',action='store_true',help='Precision mode: retrieve Markdown and structured JSON for reflow')
 p.add_argument('--ocr',action='store_true');p.add_argument('--timeout',type=int,default=300)
 a=p.parse_args();source=Path(a.source).resolve();out=Path(a.output).resolve()
 if a.structured and a.mode!='precision':p.error('--structured requires precision mode')
 cli=a.cli or shutil.which('mineru-open-api.exe') or shutil.which('mineru-open-api')
 if not cli or Path(cli).suffix.lower() in ('.cmd','.ps1'):
  raise SystemExit('Provide --cli with the MinerU native executable path; a shell wrapper is insufficient.')
 import os
 token=os.environ.get('MINERU_TOKEN') or stored_token()
 version=subprocess.run([cli,'version'],capture_output=True,text=True,encoding='utf-8',errors='replace',check=True).stdout.strip()
 parameters={'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'mode':a.mode,'model':a.model if a.mode=='precision' else None,'ocr':a.ocr,'formula':True,'table':True,'language':'en','cli_version':version}
 if a.structured:parameters['formats']=['md','json']
 key=hashlib.sha256(json.dumps(parameters,sort_keys=True).encode()).hexdigest()[:20]
 cache=out/key;marker=cache/'extraction-report.json'
 if marker.exists():
  old=json.loads(marker.read_text(encoding='utf-8'))
  intact=old.get('success') and all((cache/f['path']).is_file() and hashlib.sha256((cache/f['path']).read_bytes()).hexdigest()==f['sha256'] for f in old['artifacts'])
  if intact:print(json.dumps({'cache_hit':True,'output':str(cache)},ensure_ascii=False));return
 cache.mkdir(parents=True,exist_ok=True)
 # A fresh attempt cannot inherit a Markdown file left by an earlier failure.
 with tempfile.TemporaryDirectory(prefix='mineru-attempt-',dir=out) as temp:
  stage=Path(temp)/'result';stage.mkdir()
  command=[cli,'extract' if a.mode=='precision' else 'flash-extract',str(source),'--language','en','--formula','--table','--timeout',str(a.timeout),'-o',str(stage/'document.md')]
  if a.structured:command[-1]=str(stage)+__import__('os').sep;command+=['--format','md,json']
  if a.mode=='precision':command+=['--model',a.model]
  if a.ocr:command+=['--ocr']
  env=os.environ.copy()
  if token:env['MINERU_TOKEN']=token
  start=time.perf_counter();r=subprocess.run(command,capture_output=True,text=True,encoding='utf-8',errors='replace',env=env)
  env.pop('MINERU_TOKEN',None);token=None
  artifacts=[{'path':f.relative_to(stage).as_posix(),'sha256':hashlib.sha256(f.read_bytes()).hexdigest(),'bytes':f.stat().st_size} for f in stage.rglob('*') if f.is_file()]
  success=r.returncode==0 and any(f['path'].endswith('.md') and (stage/f['path']).read_text(encoding='utf-8-sig').strip() for f in artifacts)
  if a.structured:success=success and any(f['path'].endswith('.json') for f in artifacts)
  report={**parameters,'success':success,'exit_code':r.returncode,'elapsed_seconds':round(time.perf_counter()-start,2),'artifacts':artifacts if success else [],'translation_model_usage':None,'translation_model_usage_status':'not measured by extraction tool','asset_completeness':'must compare captions/equations/figures with original PDF'}
  if not success:
   message=r.stderr.lower();report['failure_category']='certificate-validation' if ('certificate' in message or 'x509' in message) else ('authentication' if ('token' in message or 'auth' in message) else 'other')
   marker.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
  else:
   (stage/'extraction-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
   # Preserve old results outside the active cache instead of mixing artifacts.
   backup=out/(key+'.previous-'+uuid.uuid4().hex[:8])
   cache.rename(backup)
   try:stage.rename(cache)
   except OSError:
    backup.rename(cache);raise
 # Do not print signed result URLs, tokens or raw API response logs.
 print(json.dumps({'success':success,'output':str(cache),'seconds':report['elapsed_seconds'],'failure_category':report.get('failure_category')},ensure_ascii=False))
 if not success:raise SystemExit('MinerU extraction did not complete. Check authentication/network/service and preserve TLS verification.')
if __name__=='__main__':main()
