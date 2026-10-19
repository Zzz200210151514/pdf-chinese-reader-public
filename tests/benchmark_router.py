"""No-network scheduling benchmark; fixed latency does not model LLM quality/speed.

Run: python tests/benchmark_router.py --output /local/path/router-benchmark.json
"""
import argparse
import json
from pathlib import Path
import sys
import threading
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from llm import Router, RemoteAdapter


def benchmark():
    rows = []
    mapping = {'blocks': [{'id': str(i), 'source_text': 'Water paragraph ' + chr(65+i),
                           'type': 'paragraph'} for i in range(24)]}
    for size in (1, 4, 12):
        for workers in (1, 2, 4):
            lock = threading.Lock()
            state = {'active': 0, 'peak': 0, 'calls': 0}
            def transport(url, headers, body, timeout):
                with lock:
                    state['calls'] += 1
                    state['active'] += 1
                    state['peak'] = max(state['peak'], state['active'])
                payload = json.loads(body['messages'][1]['content'])
                time.sleep(0.05)
                with lock:
                    state['active'] -= 1
                output = {b['id']: b['source_text'] for b in payload['blocks']}
                return {'choices': [{'message': {'content': json.dumps(output)}}]}
            config = {'provider': 'local', 'base_url': 'http://localhost/v1',
                      'model': 'unchanged-mock-model', 'batch_size': size, 'concurrency': workers}
            start = time.perf_counter()
            result = Router(config).translate(mapping, RemoteAdapter(config, transport))
            rows.append({'batch_size': size, 'workers': workers, 'calls': state['calls'],
                         'actual_peak': state['peak'], 'translated_ids': len(result),
                         'elapsed_seconds': round(time.perf_counter()-start, 6), 'provider_tokens': None})
    return {'scope': 'Fixed 50ms mocked transport; not real LLM performance/quality', 'rows': rows}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(benchmark(), indent=2), encoding='utf-8')
    print(str(output.resolve()))
