import copy
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from llm import Router, HostHandoffRequired, RemoteAdapter


def mapping(n=5):
    return {'blocks': [{'id': str(i), 'source_text': 'Water paragraph ' + chr(65+i), 'type': 'paragraph'} for i in range(n)]}


def echo(job):
    return {b['id']: b['source_text'] for b in job['blocks']}


class IncrementalTests(unittest.TestCase):
    def test_regroup_reuses_blocks(self):
        with tempfile.TemporaryDirectory() as cache:
            Router({'batch_size': 2}, cache).translate(mapping(), echo)
            result = Router({'batch_size': 3, 'concurrency': 2}, cache).translate(mapping(), lambda j: self.fail('Unexpected call'))
            self.assertEqual(len(result), 5)

    def test_context_invalidates_only_neighbors_and_changed_block(self):
        with tempfile.TemporaryDirectory() as cache:
            original = mapping(7)
            Router({}, cache).translate(original, echo)
            changed = copy.deepcopy(original)
            changed['blocks'][3]['source_text'] += ' changed'
            called = []
            Router({}, cache).translate(changed, lambda j: called.extend(b['id'] for b in j['blocks']) or echo(j))
            self.assertEqual(set(called), {'2', '3', '4'})
            changed['blocks'][0]['bbox'] = [1, 2, 3, 4]
            Router({}, cache).translate(changed, lambda j: self.fail('Geometry invalidated cache'))
            Router({'fonts': {'serif': 'different-font.ttf'}}, cache).translate(changed, lambda j: self.fail('Font invalidated cache'))

    def test_supplied_revision_priority_and_persistence(self):
        with tempfile.TemporaryDirectory() as cache:
            Router({}, cache).translate(mapping(1), echo)
            router = Router({}, cache)
            self.assertEqual(router.translate(mapping(1), supplied={'0': '水段落'}), {'0': '水段落'})
            record = router.records[0]
            self.assertEqual(record['provider'], 'host')
            self.assertIsNone(record['model'])
            self.assertIsNone(record['usage'])
            for field in ('request_seconds', 'queue_seconds', 'retry_count', 'active_concurrency'):
                self.assertEqual(record[field], 0)
            self.assertEqual(Router({}, cache).translate(mapping(1)), {'0': '水段落'})
            with self.assertRaises(ValueError):
                Router({}, cache).translate(mapping(1), supplied={'unknown': '水'})

    def test_id_retry_never_repeats_valid_blocks(self):
        calls = []
        def partial(job):
            calls.append([b['id'] for b in job['blocks']])
            out = echo(job)
            if len(calls) == 1:
                out['1'] = ''
            return out
        result = Router({'validation_retries': 1}).translate(mapping(3), partial)
        self.assertEqual(calls, [['0', '1', '2'], ['1']])
        self.assertEqual(len(result), 3)

    def test_partial_failure_resumes_missing_ids_only(self):
        with tempfile.TemporaryDirectory() as cache:
            with self.assertRaises(ValueError):
                Router({}, cache).translate(mapping(3), lambda j: {'0': '水', '1': '', '2': '水'})
            with self.assertRaises(HostHandoffRequired) as caught:
                Router({}, cache).translate(mapping(3))
            self.assertEqual([b['id'] for b in caught.exception.jobs[0]['blocks']], ['1'])
            self.assertEqual({n['id'] for n in caught.exception.jobs[0]['neighbor_context']}, {'0', '2'})

    def test_legacy_exact_cache_migrates(self):
        with tempfile.TemporaryDirectory() as cache:
            router = Router({}, cache)
            job = router.plan(mapping(1))[0]
            (Path(cache) / (router._key(job) + '.json')).write_text(json.dumps({'translations': {'0': '水'}}), encoding='utf-8')
            self.assertEqual(router.translate(mapping(1)), {'0': '水'})
            self.assertEqual(len(list(Path(cache).glob('block-*.json'))), 1)

    def test_remote_actual_concurrency_bounded(self):
        lock = threading.Lock()
        active = peak = 0
        def transport(url, headers, body, timeout):
            nonlocal active, peak
            payload = json.loads(body['messages'][1]['content'])
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.05)
            with lock:
                active -= 1
            output = {b['id']: b['source_text'] for b in payload['blocks']}
            return {'choices': [{'message': {'content': json.dumps(output)}}], 'usage': {'total_tokens': 8}}
        config = {'provider': 'local', 'base_url': 'http://localhost/v1', 'model': 'same-model', 'batch_size': 1, 'concurrency': 2}
        router = Router(config)
        result = router.translate(mapping(6), RemoteAdapter(config, transport))
        self.assertEqual(peak, 2)
        self.assertEqual(len(result), 6)
        self.assertEqual(max(r['active_concurrency'] for r in router.records), 2)
        self.assertTrue(all(r['usage']['total_tokens'] == 8 for r in router.records))
        self.assertTrue(all(r['transport_retries'] == 0 for r in router.records))
        self.assertGreater(max(r['queue_seconds'] for r in router.records), 0.04)

    def test_concurrency_and_retry_bounds(self):
        for config in [{'concurrency': 9}, {'validation_retries': 4}]:
            with self.assertRaises(ValueError):
                Router(config).translate(mapping(), echo)


if __name__ == '__main__':
    unittest.main()
