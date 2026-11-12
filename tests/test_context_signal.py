"""Context warnings from a sanitized 2026-11-11 live /api/now snapshot."""
import copy
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from tests.test_board_http import BoardHTTPHarness
from murmuratio_app.config import Config
from murmuratio_app.now import NowSource, context_signal
from murmuratio_app.server import LocalHTTPServer, make_handler

FIXTURE = Path(__file__).parent / 'fixtures/context-signals.json'


class ContextSignalTests(unittest.TestCase):
    def test_context_edges_and_terminal_states(self):
        for used, status, completed, percent, high in [
            (79, 'working', False, 79, False), (79.9, 'working', False, 80, False),
            (80, 'working', False, 80, True), (84, 'waiting', False, 84, True),
            (100, 'working', False, 100, True), (120, 'working', False, 100, True),
            (None, 'working', False, None, False), (100, 'done', False, 100, False),
            (100, 'crashed', False, 100, False), (100, 'blocked', True, 100, False),
        ]:
            with self.subTest(used=used, status=status, completed=completed):
                signal = context_signal(dict(context_used=used, context_window=100, status=status, completed=completed), 80)
                self.assertEqual((signal['context_percent'], signal['context_high']), (percent, high))
        for used, window in [(1, None), (1, 0), (-1, 100), (float('nan'), 100),
                             (1, float('inf')), ('80', 100), (True, 100)]:
            self.assertEqual(context_signal(dict(context_used=used, context_window=window), 80),
                             dict(context_percent=None, context_high=False, context_level=None))

    def test_configured_threshold(self):
        with tempfile.TemporaryDirectory() as home:
            path = Path(home) / 'config.toml'
            self.assertEqual(Config.load(home, path).context_high_percent, 80)
            path.write_text('[board]\ncontext_high_percent = 90\n')
            config = Config.load(home, path)
            self.assertFalse(context_signal(dict(context_used=84, context_window=100), config.context_high_percent)['context_high'])
            for value in ('0', '101', 'true', '80.5', '"80"'):
                path.write_text('[board]\ncontext_high_percent = ' + value + '\n')
                with self.subTest(value=value), self.assertRaises(ValueError):
                    Config.load(home, path)

    def test_every_api_card_has_server_context_signal(self):
        fixture = json.loads(FIXTURE.read_text())
        with tempfile.TemporaryDirectory() as home:
            source = NowSource(home, config=Config(), clock=lambda: fixture['now'])
            with patch.object(source, 'link_parents', return_value=copy.deepcopy(fixture['agents'] + fixture['completed'])):
                server = LocalHTTPServer(('127.0.0.1', 0), make_handler(now=source))
                worker = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
                worker.start()
                try:
                    connection = http.client.HTTPConnection('127.0.0.1', server.server_port)
                    connection.request('GET', '/api/now')
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    state = json.loads(response.read())
                    connection.close()
                finally:
                    server.shutdown()
                    server.server_close()
                    worker.join()
        cards = {a['id']: a for a in state['agents'] + state['completed']}
        self.assertEqual(set(cards), {'root', 'below', 'edge', 'high', 'full', 'unknown', 'finished'})
        self.assertEqual({key: (a['context_percent'], a['context_high']) for key, a in cards.items()}, {
            'root': (10, False), 'below': (79, False), 'edge': (80, True), 'high': (84, True),
            'full': (100, True), 'unknown': (None, False), 'finished': (100, False)})


class ContextModelTests(BoardHTTPHarness):
    def test_live_models_without_recorded_windows(self):
        readings = json.loads((FIXTURE.parent / 'context-models.json').read_text())['agents']
        for index, reading in enumerate(readings):
            self.claude('live-' + str(index), [self.message('assistant', model=reading['model'],
                usage={'input_tokens': reading['context_used']})])
        cards = self.cards()
        for index, reading in enumerate(readings):
            card = cards['claude:live-' + str(index)]
            self.assertEqual(card['context_window'], 1000000)
            self.assertEqual(card['context_percent'], round(reading['context_used'] / 10000))

    def test_recorded_windows_override_family_and_unknown_families_stay_unknown(self):
        from murmuratio_app.adapters.shared import model_context_window
        for model in ('claude-fable-5-2', 'claude-haiku-5-5', 'gpt-6.1-example'):
            self.assertEqual(model_context_window({'context_window': 200000}, model), 200000)
            self.assertIsNotNone(model_context_window({}, model))
        self.assertIsNone(model_context_window({}, 'unknown-model'))
        self.assertEqual(model_context_window({}, 'claude-sonnet-4-5'), 200000)
        self.assertEqual(model_context_window({}, 'claude-haiku-4-5'), 200000)
        for model in ('claude-opus-5-5', 'claude-sonnet-5-5', 'claude-mythos-5-2', 'claude-fable-6-0'):
            self.assertEqual(model_context_window({}, model), 1000000)

    def test_family_window_is_applied_after_source_merging(self):
        reading = json.loads((FIXTURE.parent / 'context-models.json').read_text())['agents'][0]
        card = json.loads(FIXTURE.read_text())['agents'][0]
        card.update(reading)
        with patch('murmuratio_app.now.NowSource.link_parents', return_value=[card]):
            self.assertEqual(self.cards()['root']['context_window'], 1000000)

    def test_codex_family_fallback_reaches_api(self):
        self.rollout('fallback', records=[{'type': 'turn_context', 'payload': {'model': 'gpt-6.1-example'}},
            self.event('task_started'), self.event('token_count', info={'last_token_usage': {'input_tokens': 400000}})])
        card = self.cards()['codex:fallback']
        self.assertEqual(card['context_window'], 475000)
        self.assertTrue(card['context_high'])


class ContextBoundaryTests(BoardHTTPHarness):
    def signal(self, card_id):
        card = self.cards()[card_id]
        return (card['context_used'], card['context_percent'], card['context_high'])

    def test_claude_compaction_clears_until_a_real_measurement(self):
        path = self.claude('context', [self.message('user', content='Work'), self.message('assistant',
            model='claude-opus-5-5', stop_reason='tool_use', content='Working',
            usage={'input_tokens': 84000, 'model_context_window': 100000})])
        self.assertEqual(self.signal('claude:context'), (84000, 84, True))
        self.write(path, [{'type': 'system', 'subtype': 'compact_boundary', 'timestamp': self.now - 2,
                           'compactMetadata': {'trigger': 'auto', 'preTokens': 84000}}], mode='a')
        self.assertEqual(self.signal('claude:context'), (None, None, False))
        self.write(path, [self.message('assistant', model='<synthetic>', content='API Error',
                                     usage={'input_tokens': 0})], mode='a')
        self.assertEqual(self.signal('claude:context'), (None, None, False))
        self.write(path, [self.message('assistant', model='claude-opus-5-5', content='Working',
                                     usage={'input_tokens': 10000})], mode='a')
        self.assertEqual(self.signal('claude:context'), (10000, 10, False))

    def test_claude_synthetic_usage_preserves_measured_or_unknown_context(self):
        for measured in (False, True):
            with self.subTest(measured=measured):
                records = [self.message('user', content='Work')]
                if measured:
                    records.append(self.message('assistant', model='claude-opus-5-5',
                        usage={'input_tokens': 84000, 'model_context_window': 100000}))
                name = 'synthetic-' + str(measured)
                path = self.claude(name, records)
                expected = (84000, 84, True) if measured else (None, None, False)
                self.assertEqual(self.signal('claude:' + name), expected)
                self.write(path, [self.message('assistant', model='<synthetic>', content='API Error',
                    usage={'input_tokens': 0, 'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0})], mode='a')
                self.assertEqual(self.signal('claude:' + name), expected)

    def test_codex_compaction_clears_native_and_merged_wrapper_context(self):
        for boundary in ({'type': 'compacted', 'payload': {'message': 'Summary'}},
                         self.event('context_compacted')):
            with self.subTest(boundary=boundary['type']):
                for wrapped in (False, True):
                    with self.subTest(wrapped=wrapped):
                        name = boundary['type'] + str(wrapped)
                        path = self.rollout(name, records=[self.event('task_started'),
                            self.event('token_count', info={'model_context_window': 200000,
                                       'last_token_usage': {'input_tokens': 168000}})])
                        card_id = 'codex:' + name
                        if wrapped:
                            self.config.modules['codex_wrapper'] = True
                            events = self.home / (name + '-events.jsonl')
                            self.write(events, [{'type': 'thread.started', 'thread_id': name},
                                                {'type': 'token_usage_record', 'usage': {'input_tokens': 168000}}])
                            run = self.home / '.local/share/murmuratio/codex-runs' / (name + '-run.json')
                            run.parent.mkdir(parents=True, exist_ok=True)
                            run.write_text(json.dumps(dict(status='running', thread=name, started=self.now - 60,
                                                          events=str(events), cwd='/synthetic/project')))
                            card_id += '-run'
                        self.assertEqual(self.signal(card_id), (168000, 84, True))
                        self.write(path, [dict(boundary, timestamp=self.now - 1)], mode='a')
                        self.assertEqual(self.signal(card_id), (None, None, False))
                        self.write(path, [self.event('token_count', info={
                            'last_token_usage': {'input_tokens': 20000}})], mode='a')
                        self.assertEqual(self.signal(card_id), (20000, 10, False))
