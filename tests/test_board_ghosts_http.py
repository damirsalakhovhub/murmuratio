"""B2/S3/S5/T1: one-shot Claude sessions cannot become immortal live cards."""
import json
from pathlib import Path

from murmuratio_app.common import timestamp
from tests.test_board_http import BoardHTTPHarness


class BoardGhostHTTPTests(BoardHTTPHarness):
    def setUp(self):
        super().setUp()
        fixture = Path(__file__).parent / 'fixtures/adapters/claude-oneshot.jsonl'
        self.records = [json.loads(line) for line in fixture.read_text().splitlines()]
        self.finished = timestamp(self.records[3]['timestamp'])
        self.now = self.finished + 60

    def test_B2_one_session_across_transcript_copies_metadata_and_repeated_polls(self):
        self.claude('synthetic-oneshot', self.records)
        self.write(self.home / '.claude/projects/another/synthetic-oneshot.jsonl', self.records)
        self.desktop('synthetic-oneshot', title='Sample inspection')
        for _ in range(9):
            state = self.api()
            self.assertEqual([a['id'] for a in state['agents'] + state['completed']],
                             ['claude:synthetic-oneshot'], 'B2: exactly one card per session')

    def test_S3_T1_one_shot_finishes_without_orchestrator_label_and_time_freezes(self):
        self.claude('synthetic-oneshot', self.records)
        for elapsed in (0, 300):
            self.now = self.finished + 60 + elapsed
            card = self.cards()['claude:synthetic-oneshot']
            self.assertEqual(card['status'], 'done', 'S3: stop_sequence finishes the run')
            self.assertFalse(card['orchestrator'], 'S3: a CLI one-shot is not an orchestrator')
            self.assertEqual(card['seconds'], 60, 'T1: completed turn duration is frozen')
            self.assertEqual(card['ended'], self.finished)

    def test_S3_idle_desktop_sessions_keep_their_finished_turn(self):
        self.desktop('retired', isArchived=True)
        for name, stop, fields in (
                ('root', 'stop_sequence', {'title': 'Sample orchestrator'}),
                ('successor', 'end_turn', {'title': 'Another orchestrator',
                                           'spawnedFrom': {'sessionId': 'retired'}}),
                ('chat', 'end_turn', {'title': 'Sample interactive chat'})):
            records = json.loads(json.dumps(self.records))
            for record in records:
                record['sessionId'] = name
            records[3]['message']['stop_reason'] = stop
            self.claude(name, records)
            self.desktop(name, **fields)
        self.claude('old-cli', [{**record, 'sessionId': 'old-cli'} for record in self.records])
        for age in (1799, 1800, 2 * 3600, 46 * 3600):
            with self.subTest(age=age):
                self.now = self.finished + age
                cards = self.cards()
                for name in ('root', 'successor', 'chat'):
                    card = cards['claude:' + name]
                    self.assertEqual(card['status'], 'waiting')
                    self.assertTrue(card['orchestrator'])
                    self.assertEqual(card['seconds'], 60)
                if age >= 1800:
                    self.assertNotIn('claude:old-cli', cards)

    def test_B2_identical_one_shots_in_different_folders_remain_distinct_sessions(self):
        for index in range(9):
            name = 'sample-' + str(index)
            records = [{**record, 'sessionId': name} for record in self.records]
            self.write(self.home / '.claude/projects' / name / (name + '.jsonl'), records)
        cards = self.cards()
        self.assertEqual(len(cards), 9, 'B2: identical prompts do not merge different session IDs')
        self.assertTrue(all(a['status'] == 'done' and not a['orchestrator'] for a in cards.values()))
        self.now = self.finished + 46 * 3600
        self.assertEqual(self.cards(), {}, 'S5: old one-shots leave every project folder')

    def test_S5_retention_uses_completion_not_file_mtime_for_cli_and_desktop(self):
        for name, stop, desktop in (('cli', 'stop_sequence', False), ('desktop', 'end_turn', True)):
            records = json.loads(json.dumps(self.records))
            records[3]['message']['stop_reason'] = stop
            self.claude(name, records)
            if desktop:
                self.desktop(name, title='Sample worker', spawnedFrom={'sessionId': 'absent-parent'})
        self.now = self.finished + 1799
        cards = self.cards()
        self.assertEqual(set(cards), {'claude:cli', 'claude:desktop'})
        self.assertTrue(all(card['status'] == 'done' and not card['orchestrator'] for card in cards.values()))
        self.now += 1
        self.assertEqual(self.cards(), {}, 'S5: finished sessions leave at 30 minutes')

    def test_S1_no_unfinished_claude_session_is_live_after_24_hours_without_activity(self):
        self.desktop('quiet', title='Sample orchestrator')
        self.claude('quiet', self.records[:3])
        self.now = timestamp(self.records[2]['timestamp']) + 86399
        self.assertEqual(self.cards()['claude:quiet']['status'], 'working')
        self.now += 1
        self.assertNotIn('claude:quiet', self.cards(), 'S1: stale activity is not live')
        self.now += 22 * 3600
        self.assertNotIn('claude:quiet', self.cards())

    def test_S5_finished_parent_remains_only_while_its_child_is_active(self):
        self.claude('parent', self.records)
        self.desktop('parent', title='Sample worker', spawnedFrom={'sessionId': 'absent-parent'})
        self.now = self.finished + 3600
        path = self.claude('child', [self.message('user', content='Continue sample work')])
        self.desktop('child', spawnedFrom={'sessionId': 'parent'})
        self.assertEqual(set(self.cards()), {'claude:parent', 'claude:child'})
        self.write(path, [self.message('assistant', stop_reason='end_turn', content='Done')], mode='a')
        self.assertEqual(set(self.cards()), {'claude:child'})

    def test_S5_active_grandchild_keeps_old_ancestor_through_a_recent_finished_parent(self):
        self.claude('root', self.records)
        self.desktop('root', title='Sample worker', spawnedFrom={'sessionId': 'absent-parent'})
        self.now = self.finished + 3600
        self.claude('middle', [self.message('user', content='Inspect sample'),
                              self.message('assistant', stop_reason='end_turn', content='Done')])
        self.desktop('middle', spawnedFrom={'sessionId': 'root'})
        self.claude('leaf', [self.message('user', content='Continue sample work')])
        self.desktop('leaf', spawnedFrom={'sessionId': 'middle'})
        self.assertEqual(set(self.cards()), {'claude:root', 'claude:middle', 'claude:leaf'})
