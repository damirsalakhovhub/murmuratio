"""Shared HTTP and privacy checks, with no real session files or source writes."""
import http.client
import json
from pathlib import Path
import plistlib
import threading
from types import SimpleNamespace
import unittest
from http.server import ThreadingHTTPServer

from render_preview import render
from server import make_handler, sanitize


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
        state = json.loads((Path(__file__).parent / 'fixtures' / 'demo.json').read_text())
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

    def test_preview_paths_follow_this_checkout(self):
        root = Path(__file__).resolve().parent
        config = plistlib.loads(render())
        self.assertEqual(config['WorkingDirectory'], str(root))
        self.assertEqual(config['ProgramArguments'][-3:], [str(root / 'murmuratio'), 'run', '--portless'])
        self.assertEqual(config['Label'], 'dev.local-sites.murmuratio')
        self.assertEqual(config['StandardErrorPath'], str(root / '.inspector-data/service/stderr.log'))
        self.assertEqual(config['StandardOutPath'], str(root / '.inspector-data/service/stdout.log'))
        self.assertTrue(config['RunAtLoad'])
        self.assertTrue(config['KeepAlive'])
        self.assertNotIn('__PROJECT_ROOT__', render().decode())


if __name__ == '__main__':
    unittest.main(verbosity=2)
