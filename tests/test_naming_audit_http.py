"""Audit regressions checked at the final /api/now boundary."""
import builtins
import json
import os
from unittest.mock import patch

from tests.test_board_http import BoardHTTPHarness


class NamingAuditHTTPTests(BoardHTTPHarness):
    def wrapper(self, name, **fields):
        self.config.modules['codex_wrapper'] = True
        path = self.home / '.local/share/murmuratio/codex-runs' / (name + '.json')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(dict(status='running', started=self.now - 60,
            parent_session='fixture-launcher', target='/synthetic/project', **fields)))
        return path

    def test_legitimate_card_headings_and_two_sentence_headings(self):
        for name, text in (
            ('card', '# Board cleanup: card names say what the agent does\nRepo: /sample'),
            ('heading', '# Fix polling latency. Preserve task names'),
        ):
            self.claude(name, [self.message('user', content=text)])
            self.rollout(name, records=[self.event('user_message', message=text), self.event('task_started')])
        cards = self.cards()
        for provider in ('claude', 'codex'):
            self.assertEqual(cards[provider + ':card']['task'], 'Board cleanup: card names say what the agent does')
            self.assertEqual(cards[provider + ':heading']['task'], 'Fix polling latency. Preserve task names')

    def test_two_word_task_and_goal_values(self):
        for label in ('Task', 'Goal'):
            self.claude(label, [self.message('user', content=label + ': Fix parser')])
        for card in self.cards().values():
            self.assertEqual(card['task'], 'Fix parser')

    def test_project_prefix_is_kept_for_fragments(self):
        for index, title in enumerate(('Project for mobile', 'Project while testing', 'Project parser work')):
            self.claude('prefix-' + str(index), [self.message('user', content=title)])
        cards = self.cards()
        for index, title in enumerate(('Project for mobile', 'Project while testing', 'Project parser work')):
            self.assertEqual(cards['claude:prefix-' + str(index)]['task'], title)

    def test_provider_service_title_yields_to_brief_then_bare_id(self):
        brief = self.home / 'brief.md'
        brief.write_text('# Fix parser recovery')
        self.wrapper('better', title='Claude: Task: S01-0040', brief=str(brief), task_id='S01-0040')
        self.wrapper('bare', title='S01-0040', brief=str(brief), task_id='S01-0040')
        self.wrapper('id', title='Claude: Task: S01-0040', task_id='S01-0040')
        self.start_server()
        cards = self.cards()
        self.assertEqual(cards['codex:better']['task'], 'Fix parser recovery')
        self.assertEqual(cards['codex:bare']['task'], 'Fix parser recovery')
        self.assertEqual(cards['codex:id']['task'], 'S01-0040')

    def test_wrapper_title_and_effort_survive_claude_deduplication(self):
        self.claude('worker', [self.message('user', content='You are a worker started by an orchestrator.\nReview the worker preamble.')])
        self.wrapper('worker-run', tool='claude', session_id='worker', title='Fix wrapper naming', effort='high')
        self.start_server()
        cards = self.cards()
        self.assertNotIn('codex:worker-run', cards)
        self.assertEqual(cards['claude:worker']['task'], 'Fix wrapper naming')
        self.assertEqual(cards['claude:worker']['title_source'], 'wrapper')
        self.assertEqual(cards['claude:worker']['effort'], 'high')

    def test_explicit_title_does_not_open_brief(self):
        brief = self.home / 'brief.md'
        brief.write_text('# Other meaningful task')
        self.wrapper('explicit', title='Fix explicit naming', brief=str(brief))
        self.start_server()
        with patch('murmuratio_app.adapters.wrapper.open', wraps=builtins.open, create=True) as opened:
            self.assertEqual(self.cards()['codex:explicit']['task'], 'Fix explicit naming')
            self.assertFalse(any(str(call.args[0]) == str(brief) for call in opened.call_args_list))

    def test_fallback_brief_cached_and_invalidated_by_mtime_and_size(self):
        brief = self.home / 'brief.md'
        brief.write_text('# Fix first fallback')
        self.wrapper('cached', brief=str(brief))
        self.start_server()
        with patch('murmuratio_app.adapters.wrapper.open', wraps=builtins.open, create=True) as opened:
            self.assertEqual(self.cards()['codex:cached']['task'], 'Fix first fallback')
            self.now += 2
            self.assertEqual(self.cards()['codex:cached']['task'], 'Fix first fallback')
            self.assertEqual(sum(str(c.args[0]) == str(brief) for c in opened.call_args_list), 1)
            st = brief.stat()
            brief.write_text('# Fix other fallback')
            os.utime(brief, ns=(st.st_atime_ns, st.st_mtime_ns + 1000000))
            self.now += 2
            self.assertEqual(self.cards()['codex:cached']['task'], 'Fix other fallback')
            st = brief.stat()
            brief.write_text('# Fix longer fallback name')
            os.utime(brief, ns=(st.st_atime_ns, st.st_mtime_ns))
            self.now += 2
            self.assertEqual(self.cards()['codex:cached']['task'], 'Fix longer fallback name')
            self.assertEqual(sum(str(c.args[0]) == str(brief) for c in opened.call_args_list), 3)

    def test_heading_is_not_shortened_by_a_second_normalization(self):
        self.claude('heading', [self.message('user', content='# Fix polling latency. Preserve task names')])
        self.assertEqual(self.cards()['claude:heading']['task'], 'Fix polling latency. Preserve task names')

    def test_meaningful_line_after_real_project_preamble_is_not_untitled(self):
        text = ('# AGENTS.md instructions\n<INSTRUCTIONS>Rules</INSTRUCTIONS>\n'
                '# Fix parser card naming\nRepo: /synthetic/project')
        self.claude('preamble', [self.message('user', content=text)])
        self.assertEqual(self.cards()['claude:preamble']['task'], 'Fix parser card naming')
