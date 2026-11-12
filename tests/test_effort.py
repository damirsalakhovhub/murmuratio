"""Recorded model/effort settings, including absent and changing provider data."""
import json
from pathlib import Path
import tempfile
import unittest

from murmuratio_app.adapters.codex_board import consume_rollout
from murmuratio_app.adapters.parsing import consume_claude, consume_claude_sidechain
from murmuratio_app.adapters.settings import recorded_effort
from tests.board_fixture_helpers import fixture_parent
from murmuratio_app.config import Config
from murmuratio_app.now import NowSource
from murmuratio_app.grouping import merge_codex_sources


class EffortTests(unittest.TestCase):
    def test_codex_session_and_turn_fields(self):
        for kind, payload in (
            ('session_meta', {'model_reasoning_effort': 'high'}),
            ('turn_context', {'effort': 'high'}),
            ('turn_context', {'model_reasoning_effort': 'high'}),
            ('turn_context', {'collaboration_mode': {'settings': {'reasoning_effort': 'high'}}}),
        ):
            with self.subTest(kind=kind, payload=payload):
                data = {}
                consume_rollout(data, {'type': kind, 'payload': payload})
                self.assertEqual(data['effort'], 'high')

    def test_codex_missing_turn_effort_clears_the_previous_turn(self):
        data = {}
        for payload, expected in (({'effort': 'xhigh'}, 'xhigh'), ({'model': 'gpt-6.1-sol'}, None),
                                  ({'effort': 'low'}, 'low'), ({'effort': None}, None)):
            consume_rollout(data, {'type': 'turn_context', 'payload': payload})
            self.assertEqual(data['effort'], expected)

    def test_codex_recorded_launch_setting_survives_a_turn_without_settings(self):
        data = {}
        consume_rollout(data, {'type': 'session_meta', 'payload': {'model_reasoning_effort': 'high'}})
        consume_rollout(data, {'type': 'turn_context', 'payload': {'model': 'gpt-6.1-sol'}})
        self.assertEqual(data['effort'], 'high')
        consume_rollout(data, {'type': 'turn_context', 'payload': {'effort': None}})
        self.assertIsNone(data['effort'])

    def test_claude_actual_transcript_fields_and_latest_value(self):
        cases = (
            {'type': 'assistant', 'perTurnEffort': 'high', 'effort': 'medium', 'message': {}},
            {'type': 'assistant', 'effort': 'high', 'message': {}},
            {'type': 'api-request', 'params': {'output_config': {'effort': 'high'}}},
            {'type': 'api-request-blob', 'message': {'output_config': {'effort': 'high'}}},
        )
        for record in cases:
            with self.subTest(record=record):
                data = {}
                consume_claude(data, record)
                self.assertEqual(data['effort'], 'high')
                consume_claude(data, {'type': 'assistant', 'effort': 'xhigh', 'message': {}})
                self.assertEqual(data['effort'], 'xhigh')

    def test_claude_explicit_unknown_transitions(self):
        unknowns = (
            {'type': 'assistant', 'perTurnEffort': None, 'effort': 'medium', 'message': {}},
            {'type': 'assistant', 'effort': None, 'message': {}},
            {'type': 'api-request', 'params': {'output_config': {'effort': None}}},
            {'type': 'api-request-blob', 'message': {'output_config': {'effort': None}}},
        )
        for unknown in unknowns:
            with self.subTest(unknown=unknown):
                data = {}
                consume_claude(data, {'type': 'assistant', 'perTurnEffort': 'high', 'message': {}})
                consume_claude(data, unknown)
                self.assertIsNone(data['effort'])
                consume_claude(data, {'type': 'assistant', 'message': {}})
                self.assertIsNone(data['effort'])
                consume_claude(data, {'type': 'assistant', 'perTurnEffort': 'low', 'message': {}})
                self.assertEqual(data['effort'], 'low')

    def test_claude_sidechain_is_isolated(self):
        record = {'type': 'assistant', 'isSidechain': True, 'effort': 'high', 'message': {}}
        parent, child = {}, {}
        consume_claude(parent, record)
        consume_claude_sidechain(child, record)
        self.assertNotIn('effort', parent)
        self.assertEqual(child['effort'], 'high')

    def test_model_and_effort_not_guessed_from_model_or_text(self):
        for value in (None, '', 5, {}, []):
            self.assertIsNone(recorded_effort({'effort': value}))
        for consume, record in ((consume_claude, {'type': 'assistant', 'message': {'model': 'claude-opus-5-5'}}),
                                (consume_rollout, {'type': 'turn_context', 'payload': {'model': 'gpt-6.1-sol'}})):
            data = {}
            consume(data, record)
            self.assertIsNone(data.get('effort'))

    def test_claude_desktop_settings_and_api_unknowns(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            fixture_parent(home, 'sample')
            project = home / '.claude/projects/sample'
            project.mkdir(parents=True)
            desktop = home / 'Library/Application Support/Claude/claude-code-sessions/a/b'
            desktop.mkdir(parents=True)
            for settings in ({'effort': 'high'}, {'sessionSettings': {'effort': 'xhigh'}}, {}):
                (project / 'sample.jsonl').write_text(json.dumps({'type': 'user', 'timestamp': 100,
                    'message': {'content': 'Fix the sample task'}, 'cwd': '/sample'}) + '\n')
                (desktop / 'local_sample.json').write_text(json.dumps({'cliSessionId': 'sample', **settings}))
                source = NowSource(home=home, config=Config(), clock=lambda: 101)
                card, = source.state()['agents']
                self.assertIsNone(card['model'])
                self.assertEqual(card['effort'], settings.get('effort') or settings.get('sessionSettings', {}).get('effort'))

    def test_wrapper_without_settings_has_explicit_nulls(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            fixture_parent(home, 'sample')
            runs = home / '.local/share/murmuratio/codex-runs'
            runs.mkdir(parents=True)
            (runs / 'sample.json').write_text(json.dumps({'status': 'running', 'parent_session': 'fixture-launcher', 'title': 'Fix sample',
                'tier': 'sol', 'target': '/sample', 'started': 100, 'pid': 7}))
            config = Config(); config.modules['codex_wrapper'] = True
            card, = NowSource(home=home, config=config, clock=lambda: 101, alive=lambda _: True).state()['agents']
            self.assertIsNone(card['model'])
            self.assertIsNone(card['effort'])

    def test_completed_wrapper_does_not_borrow_effort_from_later_resume(self):
        common = {'thread': 'sample', 'status': 'done', 'seconds': 1, '_stopped_seconds': 1}
        wrapper = {**common, '_wrapper': True, 'completed': True, 'started': 100, 'ended': 200,
                   'task': 'Fix sample', 'title_source': 'wrapper', 'effort': None}
        native = {**common, '_rollout': True, 'turn_started': 300, 'effort': 'xhigh',
                  'task': 'Later task', 'title_source': 'first_user'}
        card, = merge_codex_sources([wrapper], [native])
        self.assertIsNone(card['effort'])
        native['turn_started'] = 50
        card, = merge_codex_sources([wrapper], [native])
        self.assertIsNone(card['effort'])
        native['turn_started'] = 110
        card, = merge_codex_sources([wrapper], [native])
        self.assertEqual(card['effort'], 'xhigh')
