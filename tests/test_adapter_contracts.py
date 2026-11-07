"""Contract and fault-isolation tests against independent local source fixtures."""
import http.client
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from murmuratio_app.adapters.codex_sessions import CodexSource
from murmuratio_app.schema import Observation
from murmuratio_app.config import Config, MODULES
from murmuratio_app.now import NowSource
from murmuratio_app.server import make_handler

FIXTURES = Path(__file__).resolve().parent / 'fixtures/adapters'
NOW = 2_000_000
KINDS = ('claude_sessions', 'claude_desktop', 'codex_sessions', 'codex_wrapper',
         'claude_limits', 'codex_limits', 'claude_usage', 'codex_meter')


class AdapterContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.config = Config.load(self.home)
        self.source = NowSource(self.home, config=self.config, clock=lambda: NOW, alive=lambda pid: pid == 7)

    def write_fixture(self, path, fixture, scenario):
        if scenario == 'missing':
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        if scenario == 'malformed':
            path.write_text('{broken\n')
        elif scenario == 'unknown_structure':
            path.write_text('{"type":"future_record","rate_limits":{"new_format":{}}}\n')
        elif scenario == 'unknown_fields':
            if fixture.endswith('.jsonl'):
                rows = [json.loads(line) for line in (FIXTURES / fixture).read_text().splitlines()]
                for row in rows:
                    row['future_field'] = {'arbitrary': [1, 2, 3]}
                path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
            else:
                data = json.loads((FIXTURES / fixture).read_text())
                data['future_field'] = {'arbitrary': [1, 2, 3]}
                path.write_text(json.dumps(data))
        else:
            path.write_text((FIXTURES / fixture).read_text())
        os.utime(path, (NOW, NOW))

    def setup_adapter(self, kind, scenario):
        if kind in ('claude_sessions', 'claude_usage'):
            path = self.home / '.claude/projects/example/session-1.jsonl'
            self.write_fixture(path, 'claude-session.jsonl', scenario)
            if kind == 'claude_sessions':
                return lambda: self.source.claude_adapter.read(NOW, {}, set())
            return lambda: self.source.claude_usage_adapter.read(NOW - 1000, {}, True)
        if kind == 'claude_desktop':
            path = self.home / 'Library/Application Support/Claude/claude-code-sessions/a/b/local_1.json'
            self.write_fixture(path, 'claude-desktop.json', scenario)
            return self.source.desktop_adapter.read
        if kind == 'codex_wrapper':
            self.write_fixture(self.config.wrapper_runs / 'run-1.json', 'codex-wrapper.json', scenario)
            return lambda: self.source.wrapper_adapter.read(NOW, set())
        if kind == 'claude_limits':
            self.write_fixture(self.home / '.claude/tools/claude-limits.json', 'claude-limits.json', scenario)
            return self.source.claude_limits_adapter.read
        if kind in ('codex_limits', 'codex_meter'):
            path = self.home / '.codex/sessions/1970/01/24/rollout-1.jsonl'
            self.write_fixture(path, 'codex-rollout.jsonl', scenario)
            if kind == 'codex_limits':
                return self.source.codex_limits_adapter.read
            return lambda: self.source.codex_meter_adapter.read(NOW, True, True)
        if kind == 'codex_sessions':
            codex = CodexSource(self.home / '.codex', lambda x: x)
            path = codex.home / 'state_5.sqlite'
            if scenario != 'missing':
                path.parent.mkdir(parents=True, exist_ok=True)
                if scenario == 'malformed':
                    path.write_text('not a sqlite database')
                else:
                    db = sqlite3.connect(path)
                    if scenario == 'unknown_structure':
                        db.execute('CREATE TABLE future_format(id TEXT)')
                    else:
                        db.executescript((FIXTURES / 'codex-state.sql').read_text())
                        if scenario == 'unknown_fields':
                            db.execute('ALTER TABLE threads ADD COLUMN future_field TEXT')
                            db.execute("UPDATE threads SET future_field='arbitrary'")
                    db.commit()
                    db.close()
            return codex.read
        raise AssertionError(kind)

    def test_good_fixture_observations_have_provenance(self):
        for kind in KINDS:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                self.home = Path(directory)
                self.config = Config.load(self.home)
                self.source = NowSource(self.home, config=self.config, clock=lambda: NOW, alive=lambda pid: pid == 7)
                result = self.setup_adapter(kind, 'good')()
                self.assertEqual(result.status, 'available')
                self.assertTrue(result.observations)
                for observation in result.observations:
                    self.assertIsInstance(observation, Observation)
                    self.assertEqual(observation.source, kind)
                    self.assertIsInstance(observation.timestamp, (int, float))
                    self.assertIn(observation.confidence, ('observed', 'inferred'))
                expected = 'inferred' if kind in ('claude_sessions', 'claude_usage', 'codex_wrapper', 'codex_sessions', 'codex_meter') else 'observed'
                self.assertTrue(all(item.confidence == expected for item in result.observations))

    def test_missing_source_is_unavailable(self):
        for kind in KINDS:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                self.home = Path(directory)
                self.config = Config.load(self.home)
                self.source = NowSource(self.home, config=self.config, clock=lambda: NOW, alive=lambda pid: pid == 7)
                result = self.setup_adapter(kind, 'missing')()
                self.assertEqual(result.status, 'unavailable' if kind == 'codex_sessions' else 'absent')
                self.assertEqual(result.observations, [])

    def test_malformed_source_is_unsupported(self):
        for kind in KINDS:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                self.home = Path(directory)
                self.config = Config.load(self.home)
                self.source = NowSource(self.home, config=self.config, clock=lambda: NOW, alive=lambda pid: pid == 7)
                result = self.setup_adapter(kind, 'malformed')()
                self.assertEqual(result.status, 'unsupported')

    def test_unknown_structure_is_unsupported(self):
        for kind in KINDS:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                self.home = Path(directory)
                self.config = Config.load(self.home)
                self.source = NowSource(self.home, config=self.config, clock=lambda: NOW, alive=lambda pid: pid == 7)
                result = self.setup_adapter(kind, 'unknown_structure')()
                self.assertEqual(result.status, 'unsupported')

    def test_unknown_fields_do_not_change_normalized_values(self):
        for kind in KINDS:
            with self.subTest(kind=kind):
                results = []
                for scenario in ('good', 'unknown_fields'):
                    with tempfile.TemporaryDirectory() as directory:
                        self.home = Path(directory)
                        self.config = Config.load(self.home)
                        self.source = NowSource(self.home, config=self.config, clock=lambda: NOW, alive=lambda pid: pid == 7)
                        result = self.setup_adapter(kind, scenario)()
                        self.assertEqual(result.status, 'available')
                        results.append(result.values())
                self.assertEqual(results[0], results[1])

    def test_board_matches_the_normalized_fixture(self):
        for kind in ('claude_sessions', 'claude_desktop', 'codex_wrapper', 'claude_limits', 'codex_limits'):
            self.setup_adapter(kind, 'good')
        self.config.modules.update({name: True for name in MODULES})
        with patch('murmuratio_app.now.web_build', return_value='fixture-build'):
            actual = self.source.state()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                with self.source.usage_lock:
                    ready = {'codex_meter', 'claude_usage'}.issubset(self.source.usage_cache)
                if ready:
                    break
                time.sleep(0.01)
            self.assertTrue(ready, 'fixture usage scans completed')
            actual = self.source.state()
        expected = json.loads((FIXTURES / 'expected-board.json').read_text())
        expected['local_day'] = actual['local_day']  # Calendar bounds depend on the host timezone.
        self.assertEqual(actual, expected)

    def test_partial_malformed_rollout_keeps_valid_events(self):
        codex = CodexSource(self.home / '.codex', lambda x: x)
        self.setup_adapter('codex_sessions', 'good')
        rollout = codex.home / 'sessions/1970/01/24/rollout-1.jsonl'
        self.write_fixture(rollout, 'codex-rollout.jsonl', 'good')
        with rollout.open('a') as stream:
            stream.write('[]\n{"type":"event_msg","payload":{"type":"token_count","info":[]}}\n')
        db = sqlite3.connect(codex.home / 'state_5.sqlite')
        db.execute('UPDATE threads SET rollout_path=? WHERE id=?', (str(rollout), 'thread-1'))
        db.commit()
        db.close()
        result = codex.session('thread-1')
        self.assertGreater(len(result['events']), 0)
        self.assertEqual(result['source']['status'], 'unsupported')
        self.assertGreaterEqual(result['analysis']['malformed_records'], 1)

    def test_raising_adapter_does_not_break_api_or_other_sources(self):
        self.setup_adapter('claude_sessions', 'good')
        self.setup_adapter('codex_wrapper', 'good')
        self.setup_adapter('codex_limits', 'good')
        self.config.modules['codex_wrapper'] = True
        server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(now=self.source))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with patch.object(self.source.claude_limits_adapter, 'read', side_effect=RuntimeError('broken adapter')):
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port)
            self.addCleanup(connection.close)
            connection.request('GET', '/api/now')
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            state = json.loads(response.read())
        self.assertEqual(state['sources']['claude_limits']['status'], 'unavailable')
        self.assertEqual({item['id'] for item in state['agents']}, {'claude:session-1', 'codex:run-1'})
        self.assertEqual(state['limits']['codex'][0]['used_percent'], 25)
        self.setup_adapter('claude_limits', 'unknown_structure')
        connection.request('GET', '/api/now')
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        state = json.loads(response.read())
        self.assertEqual(state['sources']['claude_limits']['status'], 'unsupported')
        self.assertEqual({item['id'] for item in state['agents']}, {'claude:session-1', 'codex:run-1'})

    def test_unknown_nested_records_are_reported_without_losing_valid_data(self):
        self.setup_adapter('claude_usage', 'good')
        transcript = self.home / '.claude/projects/example/session-1.jsonl'
        with transcript.open('a') as stream:
            stream.write(json.dumps({'type': 'assistant', 'timestamp': NOW,
                                     'message': {'usage': {'input_tokens': float('nan')}}}) + '\n')
            stream.write(json.dumps({'type': 'assistant', 'message': ['unknown format']}) + '\n')
        usage = self.source.claude_usage_adapter.read(NOW - 1000, {}, True)
        self.assertEqual(usage.status, 'unsupported')
        self.assertTrue(usage.values()[0]['hours'])
        sessions = self.source.claude_adapter.read(NOW, {}, set())
        self.assertEqual(sessions.status, 'unsupported')
        self.assertEqual(len(sessions.values()), 1)

    def test_codex_limits_report_unknown_event_format_without_rate_limits(self):
        path = self.home / '.codex/sessions/1970/01/24/rollout-1.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"type":"future_format","payload":{}}\n')
        self.assertEqual(self.source.codex_limits_adapter.read().status, 'unsupported')

    def test_bad_file_keeps_good_desktop_and_wrapper_observations(self):
        for kind, bad_path in (
                ('claude_desktop', self.home / 'Library/Application Support/Claude/claude-code-sessions/a/b/local_bad.json'),
                ('codex_wrapper', self.config.wrapper_runs / 'bad.json')):
            with self.subTest(kind=kind):
                read = self.setup_adapter(kind, 'good')
                bad_path.write_text('{broken')
                result = read()
                self.assertEqual(result.status, 'unsupported')
                self.assertEqual(len(result.values()), 1)
                bad_path.unlink()
                recovered = read()
                self.assertEqual(recovered.status, 'available')
                self.assertEqual(len(recovered.values()), 1)

    def test_meter_retry_after_temporary_corruption(self):
        path = self.home / '.codex/sessions/1970/01/24/rollout-1.jsonl'
        self.write_fixture(path, 'codex-rollout.jsonl', 'malformed')
        self.config.modules['codex_credits'] = True
        status, (credits, readings) = self.source._collect_codex_usage(NOW, True, False)
        self.assertIsNone(credits)
        self.assertEqual(status, 'unsupported')
        self.write_fixture(path, 'codex-rollout.jsonl', 'good')
        os.utime(path, (NOW + 1, NOW + 1))
        status, (credits, readings) = self.source._collect_codex_usage(NOW + 1, True, False)
        self.assertEqual(credits['balance'], 80)
        self.assertEqual(status, 'available')

    def test_missing_codex_database_is_reported_by_api(self):
        codex = CodexSource(self.home / '.codex', lambda x: x)
        server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(codex=codex))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port)
        self.addCleanup(connection.close)
        connection.request('GET', '/api/codex/state')
        response = connection.getresponse()
        self.assertEqual(response.status, 503)
        payload = json.loads(response.read())
        self.assertEqual(payload['source']['status'], 'unavailable')
        self.assertIn('error', payload)
        connection.request('GET', '/api/codex/session?id=thread-1')
        response = connection.getresponse()
        self.assertEqual(response.status, 503)
        self.assertEqual(json.loads(response.read())['source']['status'], 'unavailable')

    def test_disabled_wrapper_is_not_discovered_or_read(self):
        self.config.modules['codex_wrapper'] = False
        with patch.object(self.source.wrapper_adapter, 'read', side_effect=AssertionError('unexpected read')):
            state = self.source.state()
        self.assertNotIn('codex_wrapper', state.get('sources', {}))


if __name__ == '__main__':
    unittest.main()
