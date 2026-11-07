"""Owner-shaped corpus: projects and cards through the actual board API."""
from tests.test_board_http import BoardHTTPHarness


class BoardCanaryHTTPTests(BoardHTTPHarness):
    def test_B1_S3_S5_projects_keep_idle_roots_and_only_eligible_workers(self):
        expected = {}
        for project in ('Alpha', 'Beta', 'Gamma'):
            cwd = '/synthetic/' + project
            root, working, finished, cli, chat = (project.lower() + '-' + suffix
                for suffix in ('root', 'working', 'finished', 'cli', 'chat'))
            def message(kind, at, **fields):
                return {**self.message(kind, at=at, **fields), 'cwd': cwd}
            self.claude(root, [message('user', self.now - 7260, content='Coordinate'),
                               message('assistant', self.now - 7200, stop_reason='end_turn', content='Ready')],
                        modified=self.now - 7200)
            self.desktop(root, title='Coordinator')
            self.claude(working, [message('user', self.now - 60, content='Work')])
            # A different worker need not be a child of the idle coordinator.
            self.desktop(working, spawnedFrom={'sessionId': 'not-on-the-board'})
            self.claude(finished, [message('user', self.now - 660, content='Work'),
                                   message('assistant', self.now - 600, stop_reason='end_turn', content='Done')])
            self.desktop(finished, spawnedFrom={'sessionId': root})
            self.claude(cli, [message('user', self.now - 360, content='One shot'),
                              message('assistant', self.now - 300, stop_reason='stop_sequence', content='Done')])
            self.write(self.codex_home / 'sessions/2026/11/07' / ('rollout-' + chat + '.jsonl'), [
                {'type': 'session_meta', 'timestamp': self.now - 60,
                 'payload': {'id': chat, 'timestamp': self.now - 60, 'cwd': cwd, 'source': 'cli'}},
                self.event('task_started')])
            for name, status, orchestrator in ((root, 'waiting', True), (working, 'working', False),
                                               (finished, 'done', False), (cli, 'done', False)):
                expected['claude:' + name] = (project, status, orchestrator)
        state = self.api()
        cards = {card['id']: (card['project'], card['status'], card['orchestrator'])
                 for card in state['agents'] + state['completed']}
        self.assertEqual(cards, expected)
        self.assertEqual({card[0] for card in cards.values()}, {'Alpha', 'Beta', 'Gamma'})
        self.assertEqual(state['codex_chats']['hidden'], 3)
        self.now += 1800
        cards = self.cards()
        self.assertEqual(set(cards), {name for name in expected if name.endswith(('-root', '-working'))})
        self.assertEqual({card['project'] for card in cards.values()}, {'Alpha', 'Beta', 'Gamma'})
