"""Source-map compatible routing, strict output validation and versioned cache."""
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import time
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
import yaml
from knowledge import Glossary
from .adapters import RemoteAdapter, ProviderResponse
from .freeze import freeze, restore, audit

INSTRUCTIONS = (Path(__file__).parent / 'prompts' / 'translate.txt').read_text(encoding='utf-8').strip()


def reject_config_secrets(value):
    if isinstance(value, dict):
        for key, nested in value.items():
            if str(key).lower() in ('api_key', 'apikey', 'key', 'token', 'headers', 'authorization', 'password', 'secret'):
                raise ValueError('Credentials must be in environment variables only')
            reject_config_secrets(nested)
    elif isinstance(value, list):
        for item in value:
            reject_config_secrets(item)


def strict_json(value):
    def pairs(items):
        result = {}
        for key, val in items:
            if key in result:
                raise ValueError('Duplicate JSON key')
            result[key] = val
        return result
    if isinstance(value, str):
        return json.loads(value, object_pairs_hook=pairs)
    return value


class HostHandoffRequired(RuntimeError):
    def __init__(self, jobs, completed=None):
        super().__init__('Host translation adapter required; consume .jobs then submit assigned ID translations')
        self.jobs = jobs
        self.completed = dict(completed or {})


class Router:
    def __init__(self, config=None, cache_dir=None, log=None):
        if config is None:
            config = Path(__file__).parent.parent / 'config.yaml'
        if isinstance(config, (str, Path)):
            config = yaml.safe_load(Path(config).read_text(encoding='utf-8')) or {}
        self.config = {'provider': 'host', 'model': None, 'domain': 'chemistry', 'rules_version': '4.1', 'batch_size': 12, **config}
        reject_config_secrets(self.config)
        self.glossary = Glossary(self.config.get('terminology_dir'))
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.log = log
        self.records = []
        self._lock = threading.Lock()

    def _blocks(self, mapping):
        blocks = mapping.get('blocks', []) if isinstance(mapping, dict) else mapping
        if not isinstance(blocks, list):
            raise ValueError('Source map blocks must be a list')
        ids = [b.get('id') for b in blocks]
        if any(not isinstance(i, str) or not i for i in ids) or len(ids) != len(set(ids)):
            raise ValueError('Source map IDs must be unique nonempty strings')
        return blocks

    def _route(self, block):
        kind = block.get('type', 'paragraph')
        routes = self.config.get('routes') or {}
        override = routes.get(kind) or {}
        if isinstance(override, str):
            override = {'provider': override}
        return kind, {**self.config, **override}

    def plan(self, mapping):
        blocks = self._blocks(mapping)
        groups = []
        batch_size = int(self.config.get('batch_size', 12))
        if not 1 <= batch_size <= 100:
            raise ValueError('batch_size must be in [1,100]')
        for index, block in enumerate(blocks):
            kind, route = self._route(block)
            if kind in ('formula', 'reference', 'references', 'bibliography') or route.get('preserve') or not block.get('translation_required', True):
                continue
            if not isinstance(block.get('source_text'), str):
                raise ValueError('Translatable block requires source_text')
            domain = block.get('domain', route.get('domain', 'chemistry'))
            text, frozen = freeze(block['source_text'])
            assigned = {'id': block['id'], 'source_page': block.get('source_page'), 'type': kind, 'source_text': text, 'domain': domain}
            signature = json.dumps({'type': kind, 'route': route, 'domain': domain}, sort_keys=True)
            if not groups or groups[-1]['signature'] != signature or len(groups[-1]['blocks']) >= batch_size:
                groups.append({'id': f'job-{len(groups)+1:04d}', 'type': kind, 'provider': route['provider'], 'model': route.get('model'), 'route_config': route, 'blocks': [], 'neighbor_context': [], 'terms': [], 'instructions': INSTRUCTIONS, 'signature': signature, '_frozen': {}, '_source': {}, '_context': {}})
            job = groups[-1]
            job['blocks'].append(assigned)
            job['_frozen'][block['id']] = frozen
            job['_source'][block['id']] = block['source_text']
            job['_context'][block['id']] = [
                {'id': n['id'], 'source_text': n.get('source_text', '')[:1000]}
                for n in blocks[max(0, index-1):min(len(blocks), index+2)] if n['id'] != block['id']]
            for neighbor in blocks[max(0, index-1):min(len(blocks), index+2)]:
                if neighbor['id'] != block['id'] and not any(n['id'] == neighbor['id'] for n in job['neighbor_context']):
                    job['neighbor_context'].append({'id': neighbor['id'], 'source_text': neighbor.get('source_text', '')[:1000]})
            for term in self.glossary.inspect(block['source_text'], domain):
                if term not in job['terms']:
                    job['terms'].append(term)
        for job in groups:
            assigned_ids = {b['id'] for b in job['blocks']}
            job['neighbor_context'] = [n for n in job['neighbor_context'] if n['id'] not in assigned_ids]
            job.pop('signature')
        return groups

    def _key(self, job):
        # Neighbor context and full routing policy can change a contextual translation.
        data = {'source': job['_source'], 'context': job['neighbor_context'], 'terms': self.glossary.version,
                'rules': self.config.get('rules_version'), 'route': job['route_config'], 'instructions': INSTRUCTIONS}
        return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def _validate(self, job, output):
        output = strict_json(output)
        expected = {b['id'] for b in job['blocks']}
        if not isinstance(output, dict) or set(output) != expected:
            raise ValueError('Translation IDs missing or unknown')
        validated = {}
        for block in job['blocks']:
            identity = block['id']
            value = output[identity]
            if not isinstance(value, str) or not value.strip():
                raise ValueError('Translation must be a nonempty string')
            value = restore(value, job['_frozen'][identity])
            check = audit(job['_source'][identity], value)
            if check['missing'] or check['added']:
                raise ValueError(f'Protected chemistry/numeric/unit content changed for {identity}')
            value = self.glossary.apply(job['_source'][identity], value, block['domain'])
            if self.glossary.validate(job['_source'][identity], value, block['domain']):
                raise ValueError(f'Forbidden contextual term for {identity}')
            validated[identity] = value
        return validated

    def _record(self, record):
        record.setdefault('workflow_id', self.config.get('workflow_id'))
        record.setdefault('monotonic_seconds', time.perf_counter())
        with self._lock:
            self.records.append(record)
            if callable(self.log):
                self.log(dict(record))
            elif self.log is not None:
                path = Path(self.log)
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open('a', encoding='utf-8') as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + '\n')

    def _block_key(self, job, identity):
        # Scheduling, geometry and batch size do not alter translation meaning.
        policy = {k: v for k, v in job['route_config'].items() if k not in
                  ('batch_size', 'concurrency', 'validation_retries', 'timeout', 'retries',
                   'workflow_id', 'routes', 'terminology_dir')}
        for field in ('fonts', 'font_dir', 'font_size', 'layout', 'quality_mode'):
            policy.pop(field, None)
        block = next(b for b in job['blocks'] if b['id'] == identity)
        data = {'schema': 1, 'source': job['_source'][identity],
                'context': job['_context'][identity], 'terms': self.glossary.version,
                'domain': block['domain'], 'type': block['type'],
                'route': policy, 'instructions': INSTRUCTIONS}
        return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def _subset(self, job, identities):
        ids = set(identities)
        subset = {**job, 'blocks': [b for b in job['blocks'] if b['id'] in ids],
                  '_source': {i: job['_source'][i] for i in ids},
                  '_frozen': {i: job['_frozen'][i] for i in ids},
                  '_context': {i: job['_context'][i] for i in ids}}
        # Keep each block's necessary context even when cached peers leave the job.
        context = {n['id']: n for i in ids for n in job['_context'][i] if n['id'] not in ids}
        subset['neighbor_context'] = list(context.values())
        return subset

    def _store(self, job, values):
        if self.cache_dir is None:
            return
        for identity, value in values.items():
            path = self.cache_dir / ('block-' + self._block_key(job, identity) + '.json')
            temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
            temporary.write_text(json.dumps({'translation': value}, ensure_ascii=False), encoding='utf-8')
            temporary.replace(path)

    def _load(self, job, identity):
        if self.cache_dir is None:
            return None
        path = self.cache_dir / ('block-' + self._block_key(job, identity) + '.json')
        try:
            value = strict_json(path.read_text(encoding='utf-8'))['translation']
            return self._validate(self._subset(job, [identity]), {identity: value})[identity]
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def translate(self, mapping, adapter=None, supplied=None):
        """Reuse valid blocks; supplied revisions win; remote jobs use bounded workers.

        Custom adapters must be thread safe to opt into concurrency. Host handoffs
        remain scheduler-neutral: callers consume only the still-missing IDs.
        """
        jobs = self.plan(mapping)
        concurrency = int(self.config.get('concurrency', 1))
        retries = int(self.config.get('validation_retries', 0))
        if not 1 <= concurrency <= 8 or not 0 <= retries <= 3:
            raise ValueError('concurrency must be in [1,8], validation_retries in [0,3]')
        supplied = strict_json(supplied) if supplied is not None else {}
        expected = {b['id'] for job in jobs for b in job['blocks']}
        if not isinstance(supplied, dict) or not set(supplied).issubset(expected):
            raise ValueError('Supplied translation IDs unknown')
        results, pending = {}, []
        for job in jobs:
            # Import old exact-job entries once; never let them replace revisions.
            if self.cache_dir:
                legacy = self.cache_dir / (self._key(job) + '.json')
                try:
                    old = self._validate(job, strict_json(legacy.read_text(encoding='utf-8'))['translations'])
                    missing = {i: v for i, v in old.items() if i not in supplied and self._load(job, i) is None}
                    self._store(job, missing)
                except (OSError, ValueError, KeyError, TypeError):
                    pass
            remaining = []
            for block in job['blocks']:
                identity = block['id']
                if identity in supplied:
                    value = self._validate(self._subset(job, [identity]), {identity: supplied[identity]})
                    self._store(job, value)
                    results.update(value)
                    status = 'supplied'
                else:
                    cached = self._load(job, identity)
                    if cached is None:
                        remaining.append(identity)
                        continue
                    results[identity] = cached
                    status = 'cache_hit'
                self._record({'job_id': job['id'], 'block_ids': [identity], 'status': status,
                              'provider': job['provider'], 'model': job.get('model'),
                              'configured_concurrency': concurrency, 'active_concurrency': 0,
                              'retry_count': 0, 'transport_retries': 0,
                              'request_seconds': 0, 'queue_seconds': 0,
                              'timestamp_utc': datetime.now(timezone.utc).isoformat(),
                              'duration_seconds': 0, 'usage': None})
            if remaining:
                pending.append(self._subset(job, remaining))
        if adapter is None and any(j['provider'] == 'host' for j in pending):
            raise HostHandoffRequired(pending, results)
        runner = adapter or RemoteAdapter(self.config)
        queued = time.perf_counter()
        active = 0
        def execute(job):
            nonlocal active
            done = {}
            current = job
            for attempt in range(retries + 1):
                start, clock = datetime.now(timezone.utc).isoformat(), time.perf_counter()
                usage, transport_retries = None, None
                with self._lock:
                    active += 1
                    observed = active
                record = {'job_id': job['id'], 'block_ids': [b['id'] for b in current['blocks']],
                          'provider': job['provider'], 'model': job.get('model'), 'start_utc': start,
                          'queue_seconds': clock - queued if attempt == 0 else 0,
                          'configured_concurrency': concurrency, 'active_concurrency': observed,
                          'retry_count': attempt, 'workflow_id': self.config.get('workflow_id')}
                try:
                    request_clock = time.perf_counter()
                    response = runner(current) if callable(runner) else runner.translate(current)
                    record['request_seconds'] = time.perf_counter() - request_clock
                    if isinstance(response, ProviderResponse):
                        usage, transport_retries, response = response.usage, response.retries, response.text
                    output = strict_json(response)
                    ids = {b['id'] for b in current['blocks']}
                    if not isinstance(output, dict) or set(output) - ids:
                        raise ValueError('Translation IDs unknown or output is not an object')
                    valid, failed = {}, []
                    for identity in ids:
                        try:
                            if identity not in output:
                                raise ValueError('Translation ID missing')
                            valid.update(self._validate(self._subset(current, [identity]), {identity: output[identity]}))
                        except ValueError:
                            failed.append(identity)
                    self._store(job, valid)
                    done.update(valid)
                    record['status'] = 'partial' if failed else 'success'
                    record['validated_count'] = len(valid)
                    record['failed_ids'] = failed
                    if failed and attempt == retries:
                        raise ValueError('Translation validation failed for IDs: ' + ', '.join(sorted(failed)))
                    if not failed:
                        return done
                    current = self._subset(job, failed)
                except Exception:
                    record['status'] = 'failed'
                    raise
                finally:
                    with self._lock:
                        active -= 1
                    record.update({'end_utc': datetime.now(timezone.utc).isoformat(),
                                   'duration_seconds': time.perf_counter() - clock,
                                   'usage': None if job['provider'] == 'host' else usage,
                                   'transport_retries': transport_retries})
                    record.setdefault('request_seconds', time.perf_counter() - request_clock)
                    self._record(record)
            return done
        if concurrency == 1:
            for job in pending:
                results.update(execute(job))
        else:
            # Running successes are persisted even if another future fails.
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                futures = [pool.submit(execute, job) for job in pending]
                try:
                    for future in as_completed(futures):
                        results.update(future.result())
                except Exception:
                    for future in futures:
                        future.cancel()
                    raise
        return results
