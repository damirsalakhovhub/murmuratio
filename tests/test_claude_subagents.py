"""Claude Agent-tool workers use the ordinary card, panel and grouping boundaries."""
import json
import os
from pathlib import Path
import tempfile
import unittest

from murmuratio_app.config import Config
from murmuratio_app.grouping import link_parents, retain_agents
from murmuratio_app.now import NowSource


FIXTURES = Path(__file__).resolve().parent / 'fixtures/adapters'
NOW = 2_000_000
PARENT = 'claude:session-1'
RUNNING = PARENT + ':agent-running'
FINISHED = PARENT + ':agent-finished'


class ClaudeSubagentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.config = Config.load(self.home)
        self.config.modules = {name: False for name in self.config.modules}
        self.now = NOW
        self.source = NowSource(self.home, config=self.config, clock=lambda: self.now)
        self.parent = self.home / '.claude/projects/example/session-1.jsonl'
        self.children = self.parent.with_suffix('') / 'subagents'
        self.children.mkdir(parents=True)
        self.parent.write_text((FIXTURES / 'claude-session.jsonl').read_text())
        os.utime(self.parent, (NOW, NOW))
        for name in ('running', 'finished'):
            for suffix in ('.jsonl', '.meta.json'):
                target = self.children / ('agent-' + name + suffix)
                target.write_text((FIXTURES / ('claude-subagent-' + name + suffix)).read_text())
                os.utime(target, (NOW, NOW))
        self.desktop = {'session-1': {'app_session': 'local_parent', 'title': 'Example orchestrator'}}

    def read(self, desktop=None):
        result = self.source.claude_adapter.read(self.now, self.desktop if desktop is None else desktop, set())
        self.assertEqual(result.status, 'available')
        return result.values()

    def test_adapter_cards_have_exact_parent_models_roles_and_panel_text(self):
        cards = {card['id']: card for card in self.read()}
        self.assertEqual(set(cards), {PARENT, RUNNING, FINISHED})
        for child_id in (RUNNING, FINISHED):
            child = cards[child_id]
            self.assertEqual((child['kind'], child['parent_id'], child['link']), ('claude', PARENT, 'exact'))
            self.assertEqual(child['project'], cards[PARENT]['project'])
            self.assertIsNone(child['app_session'])
        running, finished = cards[RUNNING], cards[FINISHED]
        self.assertEqual((running['status'], running['task'], running['agent_type']),
                         ('working', 'Inspect parent grouping', 'Explore'))
        self.assertEqual((running['model'], running['who']), ('claude-sonnet-5-5', 'Claude Sonnet 5.5'))
        self.assertFalse(running['completed'])
        self.assertEqual(running['brief']['request'], 'Find the grouping rules Report the relevant functions.')
        self.assertEqual(running['brief']['reply'], 'I am checking the parent links.')
        self.assertEqual((finished['status'], finished['ended'], finished['agent_type']),
                         ('done', NOW - 30, 'general-purpose'))
        self.assertEqual((finished['model'], finished['who']), ('claude-opus-5-5', 'Claude Opus 5.5'))
        self.assertTrue(finished['completed'])
        self.assertEqual(finished['brief']['request'], 'Review the adapter Check status handling.')
        self.assertEqual(finished['brief']['reply'], 'The adapter review is complete.')
        self.assertEqual(cards[PARENT]['status'], 'waiting')

    def test_grouping_keeps_children_under_session_and_only_live_child_keeps_parent_idle(self):
        linked = link_parents(self.read(), self.desktop, self.now)
        cards = {card['id']: card for card in retain_agents(linked, self.now)}
        self.assertEqual(set(cards), {PARENT, RUNNING, FINISHED})
        self.assertEqual(cards[PARENT]['status'], 'idle')
        self.assertTrue(cards[PARENT]['orchestrator'])
        for child_id in (RUNNING, FINISHED):
            self.assertEqual((cards[child_id]['parent_id'], cards[child_id]['link'], cards[child_id]['orchestrator']),
                             (PARENT, 'exact', False))
        meta = self.children / 'agent-running.meta.json'
        meta.write_text(json.dumps({**json.loads(meta.read_text()), 'finished': True}))
        cards = {card['id']: card for card in link_parents(self.read(), self.desktop, self.now)}
        self.assertEqual(cards[RUNNING]['status'], 'done')
        self.assertEqual(cards[PARENT]['status'], 'waiting')
        self.assertEqual(cards[PARENT]['ended'], None)

    def test_missing_metadata_uses_first_prompt_line_and_transcript_completion(self):
        (self.children / 'agent-running.meta.json').unlink()
        path = self.children / 'agent-running.jsonl'
        with path.open('a') as stream:
            stream.write(json.dumps({'type': 'assistant', 'isSidechain': True, 'timestamp': NOW - 10,
                                    'message': {'model': 'claude-sonnet-5-5', 'stop_reason': 'end_turn',
                                                'content': 'Grouping review finished.'}}) + '\n')
        cards = {card['id']: card for card in self.read()}
        self.assertEqual((cards[RUNNING]['task'], cards[RUNNING]['model'], cards[RUNNING]['status']),
                         ('Find the grouping rules', 'claude-sonnet-5-5', 'done'))
        self.assertTrue(cards[RUNNING]['completed'])
        self.assertEqual(cards[RUNNING]['brief']['reply'], 'Grouping review finished.')

    def test_matching_orchestrator_title_does_not_replace_the_parent(self):
        meta = self.children / 'agent-running.meta.json'
        meta.write_text(json.dumps({**json.loads(meta.read_text()), 'description': 'Example orchestrator'}))
        cards = {card['id']: card for card in link_parents(self.read(), self.desktop, self.now)}
        self.assertEqual(set(cards), {PARENT, RUNNING, FINISHED})
        self.assertEqual(cards[RUNNING]['parent_id'], PARENT)
        self.assertTrue(cards[PARENT]['orchestrator'])

    def test_board_state_includes_both_worker_cards_with_panel_data(self):
        desktop = self.home / 'Library/Application Support/Claude/claude-code-sessions/a/b/local_parent.json'
        desktop.parent.mkdir(parents=True)
        desktop.write_text(json.dumps({'cliSessionId': 'session-1', **self.desktop['session-1']}))
        state = self.source.state()
        cards = {card['id']: card for card in state['agents'] + state['completed']}
        self.assertEqual(set(cards), {PARENT, RUNNING, FINISHED})
        self.assertEqual(cards[RUNNING]['parent_id'], PARENT)
        self.assertEqual(cards[FINISHED]['status'], 'done')
        self.assertEqual(cards[FINISHED]['brief']['reply'], 'The adapter review is complete.')

    def test_fable_running_to_finished_keeps_completed_card_after_live_retention(self):
        desktop = self.home / 'Library/Application Support/Claude/claude-code-sessions/a/b/local_parent.json'
        desktop.parent.mkdir(parents=True)
        desktop.write_text(json.dumps({'cliSessionId': 'session-1', **self.desktop['session-1']}))
        registry = self.config.agents_registry
        registry.parent.mkdir(parents=True)
        registry.write_text(json.dumps({'id': 'session-1', 'kind': 'claude', 'role': 'orchestrator',
                                        'project': 'Registered Example', 'started_at': NOW - 100}) + '\n')
        self.config.mark_guessed = True
        transcript = self.children / 'agent-running.jsonl'
        transcript.write_text(transcript.read_text().replace('claude-sonnet-5-5', 'claude-fable-5-2'))
        os.utime(transcript, (NOW, NOW))
        meta = self.children / 'agent-running.meta.json'
        metadata = {**json.loads(meta.read_text()), 'description': 'Build example from scratch (Fable)',
                    'model': 'fable', 'pickedEffortModel': 'claude-opus-5-5'}
        meta.write_text(json.dumps(metadata))

        state = self.source.state()
        running = next(card for card in state['agents'] if card['id'] == RUNNING)
        self.assertEqual((running['status'], running['completed'], running['model'], running['who']),
                         ('working', False, 'claude-fable-5-2', 'Claude Fable 5.2'))
        self.assertNotIn(RUNNING, {card['id'] for card in state['completed']})
        meta.write_text(json.dumps({**metadata, 'finished': True}))

        for age in (0, 1801, 3600):
            with self.subTest(age=age):
                self.now = NOW + age
                state = self.source.state()
                self.assertNotIn(RUNNING, {card['id'] for card in state['agents']})
                finished = next(card for card in state['completed'] if card['id'] == RUNNING)
                self.assertEqual((finished['status'], finished['completed'], finished['ended']),
                                 ('done', True, NOW - 40))
                self.assertEqual((finished['model'], finished['who']), ('claude-fable-5-2', 'Claude Fable 5.2'))
                self.assertEqual((finished['parent_id'], finished['link'], finished['project']),
                                 (PARENT, 'exact', 'Registered Example'))
                self.assertNotIn('guessed', finished)
                self.assertEqual(finished['brief'], running['brief'])
                self.assertEqual([card['id'] for card in state['agents']], [PARENT])

    def test_subagent_model_uses_meta_but_never_the_unrelated_picked_effort_model(self):
        path = self.children / 'agent-running.jsonl'
        records = [json.loads(line) for line in path.read_text().splitlines()]
        for record in records:
            record['message'].pop('model', None)
        path.write_text(''.join(json.dumps(record) + '\n' for record in records))
        os.utime(path, (NOW, NOW))
        meta = self.children / 'agent-running.meta.json'
        metadata = json.loads(meta.read_text())
        for model, picked, expected in (('fable', 'claude-opus-5-5', 'fable'),
                                        ('', 'claude-opus-5-5', None),
                                        (None, None, None)):
            with self.subTest(model=model, picked=picked):
                meta.write_text(json.dumps({**metadata, 'model': model, 'pickedEffortModel': picked}))
                running = next(card for card in self.read() if card['id'] == RUNNING)
                self.assertEqual(running['model'], expected)

    def test_completed_today_subagents_keep_their_top_level_session(self):
        meta = self.children / 'agent-running.meta.json'
        meta.write_text(json.dumps({**json.loads(meta.read_text()), 'finished': True}))
        self.now += 3600
        state = self.source.state()
        self.assertEqual([a['id'] for a in state['agents']], [PARENT])
        self.assertEqual({a['id'] for a in state['completed']}, {RUNNING, FINISHED})

    def test_archived_scheduled_absent_and_expired_parents_do_not_leave_subagent_cards(self):
        for fields in ({'archived': True}, {'scheduled_task_id': 'scheduled'}):
            with self.subTest(fields=fields):
                self.assertEqual(self.read({'session-1': fields}), [])
        self.parent.unlink()
        self.assertEqual(self.read(), [])
        self.parent.write_text((FIXTURES / 'claude-session.jsonl').read_text().replace('end_turn', 'tool_use'))
        os.utime(self.parent, (NOW - 86401, NOW - 86401))
        self.assertEqual(self.read({}), [])

    def test_subagents_expire_at_24_hours_even_when_file_is_touched(self):
        self.now += 86400
        for path in self.children.glob('*.jsonl'):
            os.utime(path, (self.now, self.now))
        self.assertEqual([card['id'] for card in self.read()], [PARENT])

    def test_retention_drops_subagents_whose_session_is_no_longer_on_board(self):
        cards = self.read()
        for card in cards:
            card.update(status='done', ended=NOW - 3600, last_at=NOW - 3600)
        next(card for card in cards if card['id'] == FINISHED).update(ended=NOW - 30, last_at=NOW - 30)
        self.assertEqual(retain_agents(cards, NOW), [])


if __name__ == '__main__':
    unittest.main()
