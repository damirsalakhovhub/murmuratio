import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from murmuratio_app.config import Config
from murmuratio_app.health import assignment_health, limits_health
from murmuratio_app.history import append_routing_history, read_routing_history, routing_history_addition
from murmuratio_app.now import NowSource

NOW = 2_000_000


def routing(claude_projected=None, demand=None, available=None):
    return dict(claude=dict(used_percent=40, resets_at=NOW + 86400), codex=dict(used_percent=50, resets_at=NOW + 86400),
                codex_mode=dict(demand=demand, available=available)), dict(claude=dict(projected_at_reset=claude_projected))


class LimitsHealthTests(unittest.TestCase):
    def test_on_plan_is_full(self):
        result = limits_health(*routing(95, 100, 105))
        self.assertEqual((result['score'], result['reason']), (100, 'On plan'))

    def test_overspend_names_the_tool(self):
        result = limits_health(*routing(140, 100, 105))
        self.assertLess(result['score'], 35)
        self.assertEqual(result['reason'], 'Claude runs out before reset')

    def test_unused_quota_lowers_the_bar(self):
        result = limits_health(*routing(95, 40, 100))
        self.assertLess(result['score'], 70)
        self.assertEqual(result['reason'], 'Codex leaves 60% unused at reset')


class AssignmentHealthTests(unittest.TestCase):
    history = [dict(at=NOW - 7200, code_to='codex', design_to='claude'), dict(at=NOW - 3600, code_to='claude', design_to='claude')]

    def agent(self, kind, started, **extra):
        return dict(id=f'{kind}:{started}', kind=kind, started=started, task='t', **extra)

    def test_worker_against_advice_at_its_start(self):
        agents = [self.agent('codex', NOW - 5000), self.agent('codex', NOW - 100), self.agent('claude', NOW - 100)]
        result = assignment_health(agents, self.history, NOW)
        self.assertEqual((result['judged'], len(result['wrong']), result['score']), (3, 1, 67))
        self.assertEqual(result['wrong'][0]['advice'], 'claude')

    def test_orchestrators_old_and_unlogged_workers_are_not_judged(self):
        agents = [self.agent('codex', NOW - 100, orchestrator=True), self.agent('codex', NOW - 9000),
                  self.agent('codex', NOW - 100, codex_role='chat')]
        result = assignment_health(agents, self.history, NOW)
        self.assertEqual((result['judged'], result['score']), (0, 100))

    def test_no_log_gives_no_score(self):
        self.assertIsNone(assignment_health([], [], NOW)['score'])

    def test_history_row_only_on_change(self):
        self.assertIsNone(routing_history_addition(self.history, dict(at=NOW, code_to='claude', design_to='claude')))
        self.assertEqual(routing_history_addition(self.history, dict(at=NOW, code_to='codex', design_to='claude'))['code_to'], 'codex')


class RoutingHistoryTests(unittest.TestCase):
    def test_invalid_rows_are_skipped_and_valid_advice_is_sorted(self):
        earlier = dict(at=NOW - 3600, code_to='claude', design_to='claude')
        later = dict(at=NOW, code_to='codex', design_to='claude')
        invalid = [None, [], 1, {}, dict(later, at=float('nan')), dict(later, at=True),
                   dict(later, code_to='unknown'), dict(later, design_to=['claude'])]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'routing-history.jsonl'
            self.assertEqual(read_routing_history(path), [])
            path.write_text('\n'.join(json.dumps(row) for row in [later, *invalid, earlier]) + '\n{partial')
            self.assertEqual(read_routing_history(path), [earlier, later])

    def test_unreadable_advice_log_cannot_break_snapshot_or_routing_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config = Config.load(home)
            config.modules['routing'] = True
            source = NowSource(home, config=config, clock=lambda: NOW)
            # A path occupied by a directory fails both reads and appends on every platform.
            source.routing_history_path().mkdir(parents=True)
            state = source.state()
            self.assertEqual(json.loads(config.routing_file.read_text()), state['routing'])
            self.assertEqual(state['sources']['routing_history']['status'], 'unavailable')
            self.assertIsNone(state['health']['assignments']['score'])

    def test_append_failure_keeps_routing_available(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config = Config.load(home)
            config.modules['routing'] = True
            source = NowSource(home, config=config, clock=lambda: NOW)
            with patch('murmuratio_app.now.append_routing_history', side_effect=PermissionError()):
                state = source.state()
            self.assertEqual(json.loads(config.routing_file.read_text()), state['routing'])
            self.assertEqual(state['sources']['routing_history']['reason'], 'permission_denied')

    def test_advice_is_recorded_once_until_the_route_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'routing-history.jsonl'
            first = dict(at=NOW, code_to='codex', design_to='claude')
            append_routing_history(path, first)
            append_routing_history(path, dict(first, at=NOW + 60))
            with path.open('a') as stream:
                stream.write('{partial')
            second = dict(first, at=NOW + 120, code_to='claude')
            append_routing_history(path, second)
            self.assertEqual(read_routing_history(path), [first, second])


if __name__ == '__main__':
    unittest.main()
