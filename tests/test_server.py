"""Shared HTTP and privacy checks, with no real session files or source writes."""
import http.client
import json
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from murmuratio_app.server import LocalHTTPServer, main, make_handler, sanitize


CROSS_SITE_NAVIGATION = {'Sec-Fetch-Site': 'cross-site',
                         'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Dest': 'document'}


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.now = SimpleNamespace(state=lambda: {'agents': [], 'completed': []})
        self.http = LocalHTTPServer(('127.0.0.1', 0), make_handler(now=self.now))
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join()

    def request(self, path, method='GET', body=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.http.server_port)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        result = response.status, response.read(), dict(response.getheaders())
        connection.close()
        return result

    def test_static_assets_and_response_headers(self):
        for path in ('/', '/app.js', '/now.js', '/style.css', '/calculator', '/calculator.js', '/geist.woff2'):
            with self.subTest(path=path):
                code, body, headers = self.request(path)
                self.assertEqual(code, 200)
                self.assertTrue(body)
                self.assertEqual(headers['Cache-Control'], 'no-store')
                self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
                self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
                self.assertIn("script-src 'self'", headers['Content-Security-Policy'])
                self.assertNotIn('Access-Control-Allow-Origin', headers)
        self.assertEqual(self.request('/geist.woff2')[2]['Content-Type'], 'font/woff2')
        self.assertEqual(json.loads(self.request('/api/now')[1]), {'agents': [], 'completed': []})

    def test_removed_routes_and_private_files_are_unavailable(self):
        for path in ('/api/state', '/api/session?id=test', '/config.yaml', '/server.py',
                     '/.murmuratio-data/reset-results.json', '/../server.py'):
            self.assertEqual(self.request(path)[0], 404)
        headers = {'Content-Type': 'application/json', 'X-Murmuratio-Action': '1'}
        for path in ('/api/recording', '/api/recording/clear'):
            self.assertEqual(self.request(path, 'POST', '{}', headers)[0], 400)

    def test_now_replaces_invalid_unicode_without_breaking_demo_data(self):
        state = json.loads((Path(__file__).resolve().parent.parent / 'fixtures' / 'demo.json').read_text())
        state['invalid\udcd0'] = 'Text 😀 \udcd0'
        self.now.state = lambda: state
        status, body, _ = self.request('/api/now')
        self.assertEqual(status, 200)
        result = json.loads(body)
        self.assertEqual(result.pop('invalid\ufffd'), 'Text 😀 \ufffd')
        state.pop('invalid\udcd0')
        self.assertEqual(result, state)

    def test_host_origin_and_mutation_guards(self):
        for headers in ({'Host': 'evil.example'}, {'Origin': 'https://evil.example'},
                        {'Sec-Fetch-Site': 'cross-site'}, {'Sec-Fetch-Site': 'same-site'},
                        {'X-Forwarded-For': '203.0.113.10'}, {'X-Forwarded-For': 'invalid'}):
            self.assertEqual(self.request('/api/now', headers=headers)[0], 403)
        self.assertEqual(self.request('/unknown', 'POST', '{}',
                                     {'Content-Type': 'application/json'})[0], 403)

    def test_cross_site_top_level_navigation_can_open_board(self):
        for site in ('cross-site', 'same-site'):
            for path in ('/', '/?codex=example'):
                with self.subTest(site=site, path=path):
                    headers = {**CROSS_SITE_NAVIGATION, 'Sec-Fetch-Site': site}
                    status, body, response_headers = self.request(path, headers=headers)
                    self.assertEqual(status, 200)
                    self.assertTrue(response_headers['Content-Type'].startswith('text/html'))
                    self.assertIn(b'<html', body)

    def test_cross_site_board_requests_require_top_level_navigation(self):
        for mode, dest in ((None, None), ('navigate', None), (None, 'document'),
                           ('cors', 'empty'), ('no-cors', 'document'),
                           ('navigate', 'iframe'), ('navigate', 'frame')):
            with self.subTest(mode=mode, dest=dest):
                headers = {'Sec-Fetch-Site': 'cross-site'}
                if mode is not None:
                    headers['Sec-Fetch-Mode'] = mode
                if dest is not None:
                    headers['Sec-Fetch-Dest'] = dest
                self.assertEqual(self.request('/', headers=headers)[0], 403)

    def test_cross_site_api_and_assets_stay_forbidden(self):
        for path in ('/api/now', '/api/config', '/api/codex/state', '/app.js',
                     '/now.js', '/style.css', '/index.html', '/missing'):
            for mode, dest in (('cors', 'empty'), ('navigate', 'document')):
                with self.subTest(path=path, mode=mode, dest=dest):
                    headers = {**CROSS_SITE_NAVIGATION, 'Sec-Fetch-Mode': mode,
                               'Sec-Fetch-Dest': dest}
                    self.assertEqual(self.request(path, headers=headers)[0], 403)

    def test_cross_site_navigation_preserves_host_origin_and_peer_guards(self):
        for extra in ({'Host': 'evil.example'}, {'Origin': 'https://evil.example'},
                      {'X-Forwarded-For': '203.0.113.10'}, {'X-Forwarded-For': 'invalid'}):
            with self.subTest(headers=extra):
                headers = {**CROSS_SITE_NAVIGATION, **extra}
                self.assertEqual(self.request('/', headers=headers)[0], 403)

    def test_cross_site_posts_stay_forbidden_even_with_navigation_headers(self):
        for site in ('cross-site', 'same-site'):
            for path in ('/', '/unknown'):
                with self.subTest(site=site, path=path):
                    headers = {**CROSS_SITE_NAVIGATION, 'Sec-Fetch-Site': site,
                               'Origin': f'http://127.0.0.1:{self.http.server_port}',
                               'Content-Type': 'application/json', 'X-Murmuratio-Action': '1'}
                    self.assertEqual(self.request(path, 'POST', '{}', headers)[0], 403)

    def test_requests_without_fetch_metadata_keep_existing_behavior(self):
        self.assertEqual(self.request('/')[0], 200)
        self.assertEqual(self.request('/api/now')[0], 200)
        self.assertEqual(self.request('/app.js')[0], 200)
        self.assertEqual(self.request('/missing')[0], 404)
        self.assertEqual(self.request('/unknown', 'POST', '{}')[0], 403)
        headers = {'Content-Type': 'application/json', 'X-Murmuratio-Action': '1'}
        self.assertEqual(self.request('/unknown', 'POST', '{}', headers)[0], 400)

    def test_known_secrets_are_masked_recursively(self):
        original = {'access_token': 'private', 'nested': [
            'Authorization: Bearer abcDEF123\napi_key="SENTINEL-SECRET"\n'
            'Cookie: login=hidden\nsk-abcdefghijklmnopqrstuv'], 'safe': 'visible'}
        result = sanitize(original)
        encoded = json.dumps(result)
        for secret in ('private', 'abcDEF123', 'SENTINEL-SECRET', 'login=hidden', 'sk-abcdefghijklmnopqrstuv'):
            self.assertNotIn(secret, encoded)
        self.assertEqual(result['safe'], 'visible')
        self.assertEqual(original['access_token'], 'private')


class ServerStartupTests(unittest.TestCase):
    def test_startup_never_resolves_the_loopback_hostname(self):
        import io
        from contextlib import redirect_stdout

        def serve(server):
            self.assertEqual(server.server_name, '127.0.0.1')
            self.assertEqual(server.server_port, server.socket.getsockname()[1])
            self.assertGreater(server.server_port, 0)

        with patch('socket.getfqdn', side_effect=AssertionError('Local startup must not use DNS')), \
                patch.object(LocalHTTPServer, 'serve_forever', autospec=True, side_effect=serve) as run, \
                redirect_stdout(io.StringIO()):
            main(['--demo', '--port', '0'])
        run.assert_called_once()


class RoutingRefreshTests(unittest.TestCase):
    def test_state_is_refreshed_on_a_timer_with_no_requests(self):
        import threading
        from murmuratio_app.server import keep_routing_fresh
        calls, enough = [], threading.Event()
        class Source:
            def state(self):
                calls.append(1)
                if len(calls) == 2:
                    raise RuntimeError('one failing tick does not stop the timer')
                if len(calls) >= 3:
                    enough.set()
        stop = threading.Event()
        thread = keep_routing_fresh(Source(), interval=0.01, stop=stop)
        self.assertTrue(enough.wait(2))
        stop.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())

    def test_routing_file_is_written_after_warm_up_without_a_request(self):
        import json, tempfile, threading, time
        from unittest.mock import patch
        from murmuratio_app.config import Config
        from murmuratio_app.now import NowSource
        from murmuratio_app.server import keep_routing_fresh
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config = Config.load(home)
            config.modules.update(routing=True, codex_credits=True)
            source = NowSource(home, config=config)
            release = threading.Event()
            def scan(*args):
                release.wait(5)
                return 'available', ({'balance': 80, 'per_hour': 10}, [], [])
            stop = threading.Event()
            with patch.object(source, '_collect_codex_usage', side_effect=scan):
                source.state()  # the one request comes during the warm-up
                self.assertFalse(config.routing_file.exists())
                thread = keep_routing_fresh(source, interval=0.05, stop=stop)
                release.set()
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not config.routing_file.exists():
                    time.sleep(0.02)
                stop.set()
                thread.join(2)
            self.assertIn('codex_boost', json.loads(config.routing_file.read_text()))


if __name__ == '__main__':
    unittest.main(verbosity=2)
