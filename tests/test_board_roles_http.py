"""Worker and interactive-chat classification through the real board API."""
import json

from tests.test_board_http import BoardHTTPHarness


class BoardRolesHTTPTests(BoardHTTPHarness):
    def test_workers_and_lone_interactive_chats_follow_parent_rule(self):
        self.desktop('parent')
        self.claude('parent', [self.message('user', content='Coordinate')])
        self.rollout('exec')
        self.rollout('cli', source='cli')
        self.rollout('desktop', source='vscode')
        self.rollout('subagent', source={'subagent': {'thread_spawn': {'parent_thread_id': 'exec'}}})
        linked = self.rollout('linked-chat', source='vscode')
        records = [json.loads(line) for line in linked.read_text().splitlines()]
        records[0]['payload']['parent_session'] = 'parent'
        self.write(linked, records)
        state = self.api()
        cards = {a['id']: a for a in state['agents'] + state['completed']}
        self.assertEqual(set(cards), {'claude:parent', 'codex:exec', 'codex:subagent', 'codex:linked-chat'})
        self.assertNotIn('codex_chats', state)
        self.assertEqual(cards['codex:linked-chat']['parent_id'], 'claude:parent')
        self.assertEqual(cards['codex:linked-chat']['codex_role'], 'worker')
        self.assertNotIn('codex_chats', self.config.modules)
        self.assertNotIn('codex:cli', cards)
        self.assertNotIn('codex:desktop', cards)
        self.assertEqual(cards['codex:subagent']['parent_id'], 'codex:exec')
