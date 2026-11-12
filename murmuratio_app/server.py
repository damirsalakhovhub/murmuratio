"""Murmuratio: local agent board."""
import ipaddress
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import TCPServer
from urllib.parse import urlsplit
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


ROUTING_REFRESH_SECONDS = 60


class LocalHTTPServer(ThreadingHTTPServer):
    """Bind the local address without a potentially blocking reverse DNS lookup."""

    def server_bind(self):
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


def keep_routing_fresh(source, interval=ROUTING_REFRESH_SECONDS, stop=None):
    """Refresh routing on a timer, even with no page open.

    Without it the file was rewritten only when something requested /api/now; right after a
    start the first request comes before the credit scan ends, and nothing wrote it afterwards."""
    stop = stop or threading.Event()

    def run():
        while not stop.is_set():
            try:
                source.state()
            except Exception:
                pass  # The next tick, or the next request, tries again.
            stop.wait(interval)

    thread = threading.Thread(target=run, name='murmuratio-routing', daemon=True)
    thread.start()
    return thread


def make_handler(public_url=None, now=None, config=None):
    from murmuratio_app.config import Config
    config = config or Config()  # without a config every optional module is off

    class Handler(BaseHTTPRequestHandler):
        server_version = 'Murmuratio'

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
            self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; connect-src 'self'; img-src data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
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
                ok = ok and self.headers.get('X-Murmuratio-Action') == '1' and self.headers.get('Content-Type', '').split(';')[0] == 'application/json'
            if not ok:
                self.send_data(403, {'error': 'Only local same-origin access is allowed'})
            return ok

        def do_GET(self):
            if not self.allowed():
                return
            parsed = urlsplit(self.path)
            try:
                if parsed.path == '/api/config':
                    self.send_data(200, config.frontend())
                elif parsed.path == '/api/now' and now:
                    # Never computed on the request thread: see NowSource.snapshot.
                    state = now.snapshot() if hasattr(now, 'snapshot') else now.state()
                    if state is None:
                        self.send_data(503, {'error': 'The board is still loading. Retrying.'})
                    else:
                        self.send_data(200, state)
                elif parsed.path == '/codex-plan':
                    self.send_response(302)
                    self.send_header('Location', '/calculator')
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                elif parsed.path in ('/', '/app.js', '/now.js', '/style.css',
                                     '/calculator', '/calculator.js', '/geist.woff2'):
                    name = {'/': 'index.html', '/calculator': 'calculator.html'}.get(parsed.path, parsed.path[1:])
                    ct = 'font/woff2' if name.endswith('.woff2') else 'text/javascript; charset=utf-8' if name.endswith('.js') else 'text/css; charset=utf-8' if name.endswith('.css') else 'text/html; charset=utf-8'
                    body = (ROOT / 'web' / name).read_bytes()
                    if name in ('index.html', 'calculator.html'):
                        branding = (ROOT / 'web' / 'branding.html').read_bytes() if config.modules['branding'] else b''
                        body = body.replace(b'  <!-- murmuratio-branding -->', branding.rstrip(b'\n'))
                    self.send_data(200, body, ct)
                else:
                    self.send_data(404, {'error': 'Not found'})
            except (BrokenPipeError, ConnectionResetError):
                return
            except (sqlite3.Error, OSError, ValueError, TypeError, AttributeError, OverflowError):
                self.send_data(503, {'error': 'Source unavailable or format unsupported. Retrying.'})

        def do_POST(self):
            if not self.allowed(write=True):
                return
            self.send_data(400, {'error': 'Unknown operation'})
    return Handler


def main(argv=None):
    parser = PlainArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=int(os.environ.get('PORT', '8765')))
    parser.add_argument('--codex-home', type=Path)
    parser.add_argument('--config', type=Path, help='Optional TOML config file')
    parser.add_argument('--demo', action='store_true', help='Serve deterministic fixtures without reading personal data')
    args = parser.parse_args(argv)
    from murmuratio_app.config import Config
    if args.demo:
        class DemoSource:
            def __init__(self):
                fixture = json.loads((ROOT / 'fixtures/demo.json').read_text())
                self.fixture = {key: value for key, value in fixture.items()
                                if key not in ('routing', 'forecast', 'burn', 'credits')}
            def state(self):
                from murmuratio_app.now import context_signal
                for card in self.fixture['agents'] + self.fixture['completed']:
                    card.update(context_signal(card, config.context_high_percent))
                return self.fixture
        config = Config()
        now = DemoSource()
    else:
        from murmuratio_app.now import NowSource
        home = Path.home()
        config = Config.load(home, args.config)
        from murmuratio_app.discovery import Discovery
        codex_home = Discovery.at(home, config, args.codex_home).codex_home
        now = NowSource(home=home, sanitize=sanitize, config=config, codex_home=codex_home)
        if config.modules['routing']:
            keep_routing_fresh(now)
    public_url = os.environ.get('PORTLESS_URL')
    server = LocalHTTPServer(('127.0.0.1', args.port), make_handler(public_url, now, config))
    print(f'Murmuratio: http://127.0.0.1:{server.server_port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
