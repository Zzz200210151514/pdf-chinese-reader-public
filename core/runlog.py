"""Append-only stage events with UTC time, monotonic duration and no credentials."""
import json,time,uuid,threading,os
from datetime import datetime,timezone
from contextlib import contextmanager
from pathlib import Path

_WRITE_LOCK=threading.Lock()
_SECRET_NAMES=('api_key','apikey','secret','authorization','password','access_token','refresh_token','credential','bearer')

def _safe(value):
    if isinstance(value,dict):
        return {k:_safe(v) for k,v in value.items() if not any(s in str(k).lower() for s in _SECRET_NAMES)}
    if isinstance(value,(list,tuple)):return [_safe(v) for v in value]
    return value

class RunLog:
    def __init__(self,directory,workflow_id=None):
        self.directory=Path(directory);self.directory.mkdir(parents=True,exist_ok=True)
        self.path=self.directory/'events.jsonl';self.run_id=uuid.uuid4().hex
        identity=self.directory/'workflow.json'
        with _WRITE_LOCK:
            if identity.exists():
                recorded=json.loads(identity.read_text('utf-8'))['workflow_id']
                if workflow_id is not None and workflow_id!=recorded:
                    raise ValueError('Work directory belongs to another workflow; use a new directory')
                self.workflow_id=recorded
            else:
                self.workflow_id=workflow_id or uuid.uuid4().hex
                try:
                    with identity.open('x',encoding='utf-8') as f:json.dump({'workflow_id':self.workflow_id},f)
                except FileExistsError:
                    self.workflow_id=json.loads(identity.read_text('utf-8'))['workflow_id']
    def event(self,stage,status,**fields):
        item={'workflow_id':self.workflow_id,'run_id':self.run_id,'utc':datetime.now(timezone.utc).isoformat(),
              'monotonic_seconds':time.perf_counter(),'process_id':os.getpid(),'stage':stage,'status':status,**_safe(fields)}
        with _WRITE_LOCK:
            with self.path.open('a',encoding='utf-8') as f:f.write(json.dumps(item,ensure_ascii=False)+'\n')
        return item
    @contextmanager
    def stage(self,name,**fields):
        start=time.perf_counter();stage_id=uuid.uuid4().hex;self.event(name,'started',stage_id=stage_id,**fields)
        try:yield
        except Exception as exc:
            self.event(name,'failed',stage_id=stage_id,elapsed_seconds=round(time.perf_counter()-start,6),error_type=type(exc).__name__,**fields)
            raise
        else:self.event(name,'completed',stage_id=stage_id,elapsed_seconds=round(time.perf_counter()-start,6),**fields)
