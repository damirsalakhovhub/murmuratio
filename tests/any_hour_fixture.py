"""Provider-file fixtures shared by the reset regressions and the real HTTP browser check."""
from datetime import datetime
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from murmuratio_app.config import Config
from tests.routing_fixture import example_policy
from murmuratio_app.now import NowSource
from murmuratio_app.server import LocalHTTPServer, make_handler

FIXTURE = json.loads((ROOT / 'tests/fixtures/any-hour.json').read_text())
START = datetime.fromisoformat(FIXTURE['week_start']).timestamp()
WEEK = 7 * 86400


def staged_source(home, moment):
    case = FIXTURE['moments'][moment]
    now = START + case['elapsed']
    config = Config.load(home)
    config.routing_policy = example_policy()
    config.modules.update(forecast=True, scenarios=True, routing=True, codex_credits=True, burn_rate=True, branding=True)
    source = NowSource(home, config=config, clock=lambda: now)
    if not case.get('missing'):
        reset = START if case.get('expired') else START + WEEK
        history = [dict(at=START - 86400, tool=tool, window='week', used_percent=84, resets_at=START)
                   for tool in ('claude', 'codex')]
        config.limits_history.parent.mkdir(parents=True, exist_ok=True)
        config.limits_history.write_text(''.join(json.dumps(row) + '\n' for row in history))
        claude = home / '.local/share/murmuratio/claude-limits.json'
        claude.write_text(json.dumps(dict(at=now, rate_limits=dict(seven_day=dict(
            used_percentage=case['used'], resets_at=reset)))))
        rollout = home / '.codex/sessions/2026/11/09/rollout-any-hour.jsonl'
        rollout.parent.mkdir(parents=True)
        rows = [dict(type='session_meta', timestamp=START - 60, payload=dict(id='fixture', cwd=str(home))),
                dict(type='turn_context', payload=dict(model='gpt-6.1-sol'))]
        for at, used, end, balance in ((START - 60, 100, START, case['balance'] + case['spent']),
                                      (now, case['used'], reset, case['balance'])):
            rows.append(dict(type='event_msg', timestamp=at, payload=dict(type='token_count', rate_limits=dict(
                primary=dict(window_minutes=10080, used_percent=used, resets_at=end),
                credits=dict(balance=balance)))))
        rollout.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        os.utime(rollout, (now, now))
    # Warm the real usage adapters once; the fixture server never depends on a thread race.
    status, value = source._collect_codex_usage(now, True, True)
    source.usage_cache['codex_meter'] = dict(key=(True, True), at=now, status=status, value=value)
    since = START
    status, value = source._collect_claude_usage(since, {}, True, True)
    source.usage_cache['claude_usage'] = dict(key=(since, True, True), at=now, status=status, value=value)
    return source


def main():
    # The browser stops this fixture with SIGTERM; unwind the temporary home too.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    # Both runtimes use the owner's local clock, including work-hour boundaries.
    os.environ['TZ'] = 'Asia/Almaty'
    time.tzset()
    with tempfile.TemporaryDirectory(prefix='murmuratio-any-hour-') as directory:
        states = {}
        for moment in FIXTURE['moments']:
            home = Path(directory) / moment
            home.mkdir()
            states[moment] = staged_source(home, moment).state()

        class FixtureSource:
            moment = 'fresh'

            def state(self):
                return states[self.moment]

        source = FixtureSource()
        config = Config()
        config.modules.update(forecast=True, scenarios=True, routing=True, codex_credits=True, burn_rate=True, branding=True)
        base = make_handler(now=source, config=config)

        class Handler(base):
            def do_GET(self):
                if self.path.startswith('/fixture/'):
                    moment = self.path.removeprefix('/fixture/')
                    if moment not in states:
                        self.send_error(404)
                        return
                    source.moment = moment
                    self.send_response(204)
                    self.end_headers()
                    return
                super().do_GET()

        server = LocalHTTPServer(('127.0.0.1', 0), Handler)
        print(f'http://127.0.0.1:{server.server_port}', flush=True)
        try:
            server.serve_forever()
        finally:
            server.server_close()


if __name__ == '__main__':
    main()
