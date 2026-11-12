"""Current-turn settings must survive every merge into /api/now."""
from tests import test_naming_audit_http as naming_http
from tests.test_board_http import BoardHTTPHarness


class EffortAuditHTTPTests(BoardHTTPHarness):
    wrapper = naming_http.NamingAuditHTTPTests.wrapper

    def test_latest_rollout_model_and_explicit_unknown_override_wrapper(self):
        path = self.rollout('settings', records=[self.event('task_started', at=self.now - 50),
            dict(type='turn_context', timestamp=self.now - 49,
                 payload=dict(model='gpt-6.1-terra', effort=None))])
        self.wrapper('run', thread='settings', model='gpt-6.1-sol', effort='xhigh')
        self.start_server()
        card = self.cards()['codex:run']
        self.assertEqual(card['model'], 'gpt-6.1-terra')
        self.assertIsNone(card['effort'])
        self.write(path, [dict(type='turn_context', timestamp=self.now - 1,
                              payload=dict(model=None, effort='low'))], mode='a')
        card = self.cards()['codex:run']
        self.assertIsNone(card['model'])
        self.assertEqual(card['effort'], 'low')

    def test_claude_unknown_turn_does_not_borrow_desktop_or_wrapper_effort(self):
        path = self.claude('worker', [self.message('user', at=self.now - 50, content='Fix parser'),
            dict(self.message('assistant', at=self.now - 40, content='First', stop_reason='end_turn'), perTurnEffort='high'),
            self.message('user', at=self.now - 20, content='Continue'),
            dict(self.message('assistant', at=self.now - 10, content='Second', stop_reason='tool_use'), perTurnEffort=None)])
        self.desktop('worker', title='Fix parser', effort='xhigh')
        self.wrapper('run', tool='claude', session_id='worker', title='Fix parser', effort='high')
        self.start_server()
        self.assertIsNone(self.cards()['claude:worker']['effort'])
        self.write(path, [dict(self.message('assistant', at=self.now, content='Third', stop_reason='tool_use'), effort='low')], mode='a')
        self.assertEqual(self.cards()['claude:worker']['effort'], 'low')

    def test_claude_new_turn_without_settings_clears_previous_effort(self):
        self.claude('turn', [self.message('user', at=self.now - 50, content='Fix parser'),
            dict(self.message('assistant', at=self.now - 40, content='First', stop_reason='end_turn'), effort='high'),
            self.message('user', at=self.now - 20, content='Continue'),
            self.message('assistant', at=self.now - 10, content='Second', stop_reason='tool_use')])
        self.assertIsNone(self.cards()['claude:turn']['effort'])
