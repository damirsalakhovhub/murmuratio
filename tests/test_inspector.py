"""Shared HTTP and privacy checks, with no real session files or source writes."""
import http.client
import json
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from http.server import ThreadingHTTPServer

from murmuratio_app.server import make_handler, sanitize


CROSS_SITE_NAVIGATION = {'Sec-Fetch-Site': 'cross-site',
                         'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Dest': 'document'}


class InspectorTests(unittest.TestCase):
    def setUp(self):
        self.now = SimpleNamespace(state=lambda: {'agents': [], 'completed': []})
        self.http = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(now=self.now))
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
        for path in ('/', '/app.js', '/now.js', '/panels.js', '/style.css'):
            with self.subTest(path=path):
                code, body, headers = self.request(path)
                self.assertEqual(code, 200)
                self.assertTrue(body)
                self.assertEqual(headers['Cache-Control'], 'no-store')
                self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
                self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
                self.assertIn("script-src 'self'", headers['Content-Security-Policy'])
                self.assertNotIn('Access-Control-Allow-Origin', headers)
        self.assertEqual(json.loads(self.request('/api/now')[1]), {'agents': [], 'completed': []})

    def test_removed_routes_and_private_files_are_unavailable(self):
        for path in ('/api/state', '/api/session?id=test', '/config.yaml', '/server.py',
                     '/.inspector-data/reset-results.json', '/../server.py'):
            self.assertEqual(self.request(path)[0], 404)
        headers = {'Content-Type': 'application/json', 'X-Inspector-Action': '1'}
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
        self.assertEqual(self.request('/api/codex/reset/prepare', 'POST', '{}',
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
                     '/now.js', '/panels.js', '/style.css', '/index.html', '/missing'):
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
            for path in ('/', '/api/codex/reset/prepare'):
                with self.subTest(site=site, path=path):
                    headers = {**CROSS_SITE_NAVIGATION, 'Sec-Fetch-Site': site,
                               'Origin': f'http://127.0.0.1:{self.http.server_port}',
                               'Content-Type': 'application/json', 'X-Inspector-Action': '1'}
                    self.assertEqual(self.request(path, 'POST', '{}', headers)[0], 403)

    def test_requests_without_fetch_metadata_keep_existing_behavior(self):
        self.assertEqual(self.request('/')[0], 200)
        self.assertEqual(self.request('/api/now')[0], 200)
        self.assertEqual(self.request('/app.js')[0], 200)
        self.assertEqual(self.request('/missing')[0], 404)
        self.assertEqual(self.request('/unknown', 'POST', '{}')[0], 403)
        headers = {'Content-Type': 'application/json', 'X-Inspector-Action': '1'}
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

if __name__ == '__main__':
    unittest.main(verbosity=2)
