"""Murmuratio: local Board and saved Codex chats."""
import ipaddress
import json
import os
from pathlib import Path
import re
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from murmuratio_app.context_reset import ResetController, ResetError
from murmuratio_app.common import PlainArgumentParser

ROOT = Path(__file__).resolve().parent.parent
SECRET_KEY = re.compile(r'^(?:authorization|proxy.authorization|cookie|set.cookie|.*(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password))$', re.I)
INVALID_UNICODE = re.compile(r'[\ud800-\udfff]')
SECRET_TEXT = [
    (re.compile(r'(?i)\b(Bearer\s+)[A-Za-z0-9._~+/-]+=*'), r'\1[REDACTED]'),
    (re.compile(r'\b(?:sk-[A-Za-z0-9_-]{12,}|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)'), '[REDACTED]'),
    (re.compile(r'''(?im)((?:["']?)(?:authorization|proxy-authorization|cookie|set-cookie|[\w-]*(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password))["']?\s*[:=]\s*)(?:"[^"\n]*"|'[^'\n]*'|[^\s,}\n]+)'''), r'\1[REDACTED]'),
]


def sanitize(value):
    if isinstance(value, dict):
        return {k: '[REDACTED]' if SECRET_KEY.match(k) else sanitize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize(v) for v in value]
    if isinstance(value, str):
        for pattern, replacement in SECRET_TEXT:
            value = pattern.sub(replacement, value)
    return value


def make_handler(codex=None, public_url=None, reset=None, now=None, config=None):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'LocalInspector'

        def log_message(self, *args):
            pass  # Do not log URL, IDs, exceptions or request content.

        def send_data(self, status, data, content_type='application/json; charset=utf-8'):
            body = INVALID_UNICODE.sub('\ufffd', json.dumps(data, ensure_ascii=False)).encode() if not isinstance(data, bytes) else data
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            # The approved Now icon embeds image data; network images stay disallowed.
            self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(body)

        def allowed(self, write=False):
            port = self.server.server_port
            authorities = {f'127.0.0.1:{port}', f'localhost:{port}'}
            if public_url:
                authorities.add(urlsplit(public_url).netloc)
            origin = self.headers.get('Origin')
            host = self.headers.get('Host')
            ok = host in authorities and (origin is None or origin in {'http://' + h for h in authorities})
            forwarded = self.headers.get('X-Forwarded-For')
            if forwarded:
                try:
                    peer = ipaddress.ip_address(forwarded.split(',')[-1].strip())
                    peer = getattr(peer, 'ipv4_mapped', None) or peer
                    ok = ok and peer.is_loopback
                except ValueError:
                    ok = False
            # External links may open the board, but never its data or assets.
            page_navigation = (not write and self.command == 'GET' and urlsplit(self.path).path == '/'
                               and self.headers.get('Sec-Fetch-Mode') == 'navigate'
                               and self.headers.get('Sec-Fetch-Dest') == 'document')
            ok = ok and (page_navigation or self.headers.get('Sec-Fetch-Site', 'same-origin') not in ('cross-site', 'same-site'))
            if write:
                ok = ok and self.headers.get('X-Inspector-Action') == '1' and self.headers.get('Content-Type', '').split(';')[0] == 'application/json'
            if not ok:
                self.send_data(403, {'error': 'Only local same-origin access is allowed'})
            return ok

        def do_GET(self):
            if not self.allowed():
                return
            parsed = urlsplit(self.path)
            try:
                if parsed.path == '/api/config':
                    self.send_data(200, config.frontend() if config else {
                        'modules': {'codex_chats': bool(codex), 'context_reset': bool(reset),
                                    'forecast': True, 'scenarios': True, 'burn_rate': True, 'branding': True},
                        'project_order': [], 'work_hours': [9, 21]})
                elif parsed.path == '/api/now' and now:
                    self.send_data(200, now.state())
                elif parsed.path == '/api/codex/reset/status' and reset and (config is None or config.modules['codex_chats'] and config.modules['context_reset']):
                    self.send_data(200, reset.status(parse_qs(parsed.query).get('id', [''])[0]))
                elif parsed.path in ('/api/codex/state', '/api/codex/session', '/api/codex/event') and codex and (config is None or config.modules['codex_chats']):
                    query = parse_qs(parsed.query)
                    if parsed.path.endswith('/state'):
                        state = codex.state()
                        titles = now.codex_titles() if now is not None and hasattr(now, 'codex_titles') and (config is None or config.modules['codex_wrapper']) else {}
                        for session in state.get('sessions') or []:
                            if isinstance(session, dict) and titles.get(session.get('id')):
                                session['task'] = titles[session['id']]
                        status = 503 if state.get('source', {}).get('connected') is False else 200
                        if status == 503:
                            state['error'] = 'Source unavailable or format unsupported. Retrying.'
                        self.send_data(status, state)
                    elif parsed.path.endswith('/event'):
                        self.send_data(200, codex.event_text(query.get('id', [''])[0], query.get('event', [''])[0]))
                    else:
                        sid = query.get('id', [''])[0]
                        before = query.get('before', [None])[0]
                        before = int(before) if before is not None else None
                        if len(sid) > 200 or (before is not None and before < 0):
                            raise ValueError('Invalid cursor')
                        after = query.get('after', [None])[0]
                        after = int(after) if after is not None else None
                        if after is not None and after < 0:
                            raise ValueError('Invalid cursor')
                        self.send_data(200, codex.session(sid, before, after))
                elif parsed.path in ('/', '/app.js', '/panels.js', '/now.js', '/activity.js', '/style.css'):
                    name = 'index.html' if parsed.path == '/' else parsed.path[1:]
                    ct = 'text/javascript; charset=utf-8' if name.endswith('.js') else {'index.html': 'text/html; charset=utf-8', 'style.css': 'text/css; charset=utf-8'}[name]
                    body = (ROOT / 'web' / name).read_bytes()
                    if name == 'index.html':
                        branding = (ROOT / 'web' / 'branding.html').read_bytes() if config is None or config.modules['branding'] else b''
                        body = body.replace(b'  <!-- murmuratio-branding -->', branding.rstrip(b'\n'))
                    self.send_data(200, body, ct)
                else:
                    self.send_data(404, {'error': 'Not found'})
            except (BrokenPipeError, ConnectionResetError):
                return
            except KeyError:
                self.send_data(404, {'error': 'Chat not found'})
            except (sqlite3.Error, OSError, ValueError, TypeError, AttributeError, OverflowError) as exc:
                response = {'error': 'Source unavailable or format unsupported. Retrying.'}
                if parsed.path in ('/api/codex/state', '/api/codex/session', '/api/codex/event'):
                    status = 'unavailable' if isinstance(exc, OSError) else 'unsupported'
                    if isinstance(exc, sqlite3.OperationalError):
                        message = str(exc).lower()
                        status = 'unsupported' if 'no such table' in message or 'no such column' in message else 'unavailable'
                    response['source'] = {'name': 'codex_sessions', 'status': status}
                self.send_data(503, response)
            except Exception:
                if parsed.path not in ('/api/codex/state', '/api/codex/session', '/api/codex/event'):
                    raise
                self.send_data(503, {'error': 'Source unavailable. Retrying.',
                                     'source': {'name': 'codex_sessions', 'status': 'unavailable'}})

        def do_POST(self):
            if not self.allowed(write=True):
                return
            if self.path.startswith('/api/codex/reset/') and self.headers.get('Origin') != 'http://' + str(self.headers.get('Host')):
                self.send_data(403, {'error': 'Reset requires a local inspector page.'})
                return
            try:
                n = int(self.headers.get('Content-Length', '0'))
                if n < 0 or n > 512:
                    raise ValueError()
                data = json.loads(self.rfile.read(n))
                if self.path == '/api/codex/reset/prepare' and reset and (config is None or config.modules['codex_chats'] and config.modules['context_reset']):
                    self.send_data(200, reset.prepare(data.get('id')))
                elif self.path == '/api/codex/reset/start' and reset and (config is None or config.modules['codex_chats'] and config.modules['context_reset']):
                    self.send_data(202, reset.start(data.get('id'), data.get('token')))
                else:
                    self.send_data(400, {'error': 'Unknown operation'})
            except (ValueError, TypeError, AttributeError):
                self.send_data(400, {'error': 'Invalid data'})
            except ResetError as exc:
                self.send_data(409, {'error': str(exc)})
            except (OSError, sqlite3.Error):
                self.send_data(503, {'error': 'Could not save reset status'})
    return Handler


def main(argv=None):
    parser = PlainArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=int(os.environ.get('PORT', '8765')))
    parser.add_argument('--data-dir', type=Path, default=ROOT / '.inspector-data')
    parser.add_argument('--codex-home', type=Path)
    parser.add_argument('--config', type=Path, help='Optional TOML config file')
    parser.add_argument('--demo', action='store_true', help='Serve deterministic fixtures without reading personal data')
    args = parser.parse_args(argv)
    from murmuratio_app.config import Config
    codex = reset = None
    if args.demo:
        class DemoSource:
            def __init__(self):
                fixture = json.loads((ROOT / 'fixtures/demo.json').read_text())
                self.fixture = {key: value for key, value in fixture.items()
                                if key not in ('routing', 'forecast', 'burn', 'credits')}
            def state(self):
                return self.fixture
        config = Config()
        now = DemoSource()
    else:
        from murmuratio_app.adapters.codex_sessions import CodexSource
        from murmuratio_app.now import NowSource
        home = Path.home()
        config = Config.load(home, args.config)
        from murmuratio_app.discovery import Discovery
        codex_home = Discovery.at(home, config, args.codex_home).codex_home
        if config.modules['codex_chats']:
            codex = CodexSource(codex_home, sanitize, include_reset=config.modules['context_reset'])
            if config.modules['context_reset']:
                reset = ResetController(codex, args.data_dir)
        now = NowSource(home=home, sanitize=sanitize, codex=codex, config=config, codex_home=codex_home)
    public_url = os.environ.get('PORTLESS_URL')
    server = ThreadingHTTPServer(('127.0.0.1', args.port), make_handler(codex, public_url, reset, now, config))
    print(f'Murmuratio: http://127.0.0.1:{server.server_port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
