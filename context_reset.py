"""Explicit, local-only context reset through the bundled Codex app server.

This module does not start work until the user confirms a particular chat in
the inspector. It never edits rollouts or changes the user's global config.
"""
from collections import deque
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import queue
import re
import secrets
import sqlite3
import stat
import subprocess
import threading
import time
import tomllib

CODEX = Path('/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex')
MODEL = 'gpt-6-astra'
VERIFIED_VERSION = 'codex-cli 0.166.0-alpha.5.1'
UUID = re.compile(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')
SETTINGS = ('model', 'modelProvider', 'reasoningEffort', 'approvalPolicy', 'approvalsReviewer',
            'activePermissionProfile', 'sandbox', 'cwd', 'serviceTier')


class ResetError(Exception):
    pass


def codex_version(executable):
    try:
        return subprocess.run([str(executable), '--version'], check=True, capture_output=True,
                              text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise ResetError('Could not check the Codex version.') from exc


def recovery_message(original):
    profile = original.get('activePermissionProfile') or {}
    sandbox = original.get('sandbox') or {}
    mode = profile.get('id') if isinstance(profile, dict) else None
    if not mode and isinstance(sandbox, dict):
        mode = sandbox.get('type')
    return ('Check the original settings in Codex: model '
            f"{original.get('model') or 'unknown'}, reasoning {original.get('reasoningEffort') or 'unknown'}, "
            f"permissions {mode or 'unknown'}, approvals {original.get('approvalPolicy') or 'unknown'}. "
            'Retry is blocked until the settings are checked separately.')


def desktop_busy(ignored_pids=()):
    """Fail closed while desktop or another local Codex engine is running."""
    try:
        result = subprocess.run(['/bin/ps', '-axo', 'pid=,comm='], check=True,
                                capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        raise ResetError('Could not check running Codex processes.')
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        if int(parts[0]) in ignored_pids:
            continue
        executable = parts[1]
        if executable.endswith('/ChatGPT.app/Contents/MacOS/ChatGPT') or executable.endswith('/Codex.app/Contents/MacOS/Codex'):
            return True
        if executable.endswith('/codex') or executable == 'codex':
            return True
    return False


def _flags(home):
    # Restrict the two probe turns to text-only replies. No global config writes.
    args = ['-c', 'features.context_management.experimental_mode=true',
            '-c', 'web_search="disabled"',
            '--disable', 'multi_agent', '--disable', 'shell_tool', '--disable', 'unified_exec']
    path = home / 'config.toml'
    try:
        with path.open('rb') as file:
            data = tomllib.load(file)
    except FileNotFoundError:
        data = {}
    except (OSError, ValueError, tomllib.TOMLDecodeError):
        raise ResetError('Could not safely read Codex settings.')
    for section in ('mcp_servers', 'plugins', 'apps'):
        values = data.get(section, {})
        if not isinstance(values, dict):
            raise ResetError('Unknown Codex tool settings format.')
        names = set(values)
        if section == 'apps':
            names.add('_default')
        if names:
            # The CLI treats quoted dotted path segments as literal quote characters.
            # Put quoted names inside a TOML value instead, so dots and @ stay intact.
            entries = ','.join(json.dumps(key, ensure_ascii=False) + '={enabled=false}' for key in sorted(names))
            args.extend(['-c', section + '={' + entries + '}'])
    return args


class AppServer:
    def __init__(self, home, cwd, executable=CODEX):
        self.cwd = Path(cwd)
        env = os.environ.copy()
        env['CODEX_HOME'] = str(home)
        try:
            self.proc = subprocess.Popen([str(executable), *_flags(home), 'app-server', '--listen', 'stdio://'],
                cwd=str(cwd), env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, bufsize=1)
        except OSError as exc:
            raise ResetError('Could not start local Codex.') from exc
        self.queue = queue.Queue()
        self.pending = deque()
        self.seq = 0
        self.blocked_tool = False
        def read():
            for line in self.proc.stdout:
                try:
                    self.queue.put(json.loads(line))
                except (ValueError, TypeError):
                    continue
        threading.Thread(target=read, daemon=True).start()

    def send(self, value):
        try:
            self.proc.stdin.write(json.dumps(value, ensure_ascii=False) + '\n')
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise ResetError('Codex disconnected.') from exc

    def wait(self, predicate, timeout=90):
        deadline = time.monotonic() + timeout
        # Check already-seen notifications before reading more.
        for _ in range(len(self.pending)):
            msg = self.pending.popleft()
            if predicate(msg):
                return msg
            self.pending.append(msg)
        while time.monotonic() < deadline:
            try:
                msg = self.queue.get(timeout=min(1, max(.01, deadline-time.monotonic())))
            except queue.Empty:
                if self.proc.poll() is not None:
                    raise ResetError('Codex exited before the operation finished.')
                continue
            if 'method' in msg and 'id' in msg:
                # Never authorize dynamic tools, confirmations, or side effects.
                self.blocked_tool = True
                self.send({'id': msg['id'], 'error': {'code': -32000, 'message': 'Blocked during measurement'}})
                continue
            if predicate(msg):
                return msg
            self.pending.append(msg)
            if len(self.pending) > 10000:
                raise ResetError('Too many Codex events during measurement.')
        raise ResetError('Codex timed out.')

    def rpc(self, method, params, timeout=90):
        self.seq += 1
        ident = self.seq
        self.send({'id': ident, 'method': method, 'params': params})
        msg = self.wait(lambda x: x.get('id') == ident and 'method' not in x, timeout)
        if self.blocked_tool:
            raise ResetError('Codex requested an unsupported action; operation stopped.')
        if 'error' in msg:
            raise ResetError('Codex rejected the request: ' + str(msg['error'].get('message', 'error'))[:200])
        return msg.get('result', {})

    def initialize(self):
        self.rpc('initialize', {'clientInfo': {'name': 'agent_inspector', 'version': '1.0'},
                                'capabilities': {'experimentalApi': True}})
        self.send({'method': 'initialized', 'params': {}})
        # Project or managed config can add tools absent from the user config.
        # Inspect the effective config before resuming or modifying a chat.
        result = self.rpc('config/read', {'cwd': str(self.cwd), 'includeLayers': False})
        config = result.get('config')
        if not isinstance(config, dict):
            raise ResetError('Could not check tool settings; reset not started.')
        features = config.get('features') or {}
        if not isinstance(features, dict) or not isinstance(features.get('context_management'), dict) or features['context_management'].get('experimental_mode') is not True:
            raise ResetError('Experimental context window unavailable; operation not started.')
        for section in ('mcp_servers', 'plugins', 'apps'):
            values = config.get(section, {})
            if not isinstance(values, dict) or any(not isinstance(v, dict) or v.get('enabled', True) is not False for v in values.values()):
                raise ResetError('Could not disable all MCP servers, plugins and apps; reset not started.')
            if section == 'apps' and not isinstance(values.get('_default'), dict):
                raise ResetError('Could not check apps; reset not started.')

    def close(self):
        try:
            if self.proc.poll() is None:
                try:
                    self.proc.stdin.close()
                except (OSError, ValueError):
                    pass
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.terminate()
                    try:
                        self.proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self.proc.kill()
                        self.proc.wait(timeout=5)
        finally:
            for stream in (self.proc.stdin, self.proc.stdout):
                if stream and not stream.closed:
                    try:
                        stream.close()
                    except OSError:
                        pass


def read_appended(path, offset):
    records = []
    with path.open('rb') as file:
        file.seek(offset)
        for line in file:
            if not line.endswith(b'\n'):
                break
            try:
                records.append(json.loads(line))
            except (ValueError, UnicodeError):
                continue
    return records


def has_fresh_window(payload):
    return (not payload.get('compaction_response_id') and bool(payload.get('previous_window_id'))
            and bool(payload.get('window_id')) and payload['previous_window_id'] != payload['window_id']
            and not payload.get('message') and not any(isinstance(x, dict) and x.get('type') == 'compaction'
                                                       for x in payload.get('replacement_history', []) or []))


class ResetController:
    def __init__(self, codex, directory, busy_check=desktop_busy, executable=CODEX, version_check=codex_version):
        self.codex = codex
        requested = Path(directory).expanduser().absolute()
        self.base_directory = requested.parent.resolve() / requested.name
        self.directory = self.base_directory / 'reset'
        self.busy_check, self.executable = busy_check, executable
        self.version_check, self.version_stamp = version_check, None
        self.lock = threading.RLock()
        self.challenges, self.jobs = {}, {}
        self.results = self._load()
        for result in self.results.values():
            if isinstance(result, dict) and result.get('state') == 'running':
                result.update({'state': 'error', 'reset_applied': None,
                    'error': 'The inspector restarted during the operation. The reset result is unknown.',
                    'restore_error': 'Check the chat model and permissions in Codex.'})
            if isinstance(result, dict) and isinstance(result.get('original_settings'), dict):
                result['restore_error'] = recovery_message(result['original_settings'])

    def directory_fd(self):
        # Reject symlinks before writing reset state, including existing ancestors.
        for parent in [self.directory, *self.directory.parents]:
            if parent.is_symlink():
                raise OSError('Symlink directory refused')
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        st = os.fstat(fd)
        if st.st_uid != os.getuid() or not stat.S_ISDIR(st.st_mode):
            os.close(fd)
            raise OSError('Unowned directory refused')
        os.fchmod(fd, 0o700)
        return fd

    def _check_version(self):
        info = self.executable.stat()
        stamp = (info.st_ino, info.st_size, info.st_mtime_ns)
        if stamp != self.version_stamp:
            if self.version_check(self.executable) != VERIFIED_VERSION:
                raise ResetError('This Codex version is untested; window creation is disabled.')
            self.version_stamp = stamp

    def _load(self):
        try:
            fd = self.directory_fd()
            try:
                try:
                    rawfd = os.open('reset-results.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                except FileNotFoundError:
                    # Older versions stored the same private file directly in the supplied directory.
                    legacy = os.open(self.base_directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                    try:
                        if os.fstat(legacy).st_uid != os.getuid():
                            return {}
                        rawfd = os.open('reset-results.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=legacy)
                    finally:
                        os.close(legacy)
                with os.fdopen(rawfd, 'r') as file:
                    st = os.fstat(file.fileno())
                    if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_nlink != 1
                            or st.st_mode & 0o077 or st.st_size > 1024*1024):
                        return {}
                    result = json.load(file)
                    return result if isinstance(result, dict) else {}
            finally:
                os.close(fd)
        except (OSError, ValueError, TypeError):
            return {}

    def _save(self):
        fd = self.directory_fd()
        name = '.reset-results-' + secrets.token_hex(8) + '.tmp'
        try:
            rawfd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
            with os.fdopen(rawfd, 'w') as file:
                json.dump(dict(list(self.results.items())[-100:]), file, ensure_ascii=False)
                file.flush()
                os.fsync(file.fileno())
            os.replace(name, 'reset-results.json', src_dir_fd=fd, dst_dir_fd=fd)
        finally:
            try:
                os.unlink(name, dir_fd=fd)
            except FileNotFoundError:
                pass
            os.close(fd)

    def _target(self, thread_id):
        if not isinstance(thread_id, str) or not UUID.fullmatch(thread_id):
            raise ResetError('Invalid chat ID.')
        c = self.codex.connect()
        try:
            row = c.execute('SELECT * FROM threads WHERE id=?', (thread_id,)).fetchone()
        finally:
            c.close()
        if row is None or row['archived']:
            raise ResetError('Active local chat not found.')
        rollout = Path(row['rollout_path']).resolve()
        if not rollout.is_relative_to(self.codex.home / 'sessions') or not rollout.is_file():
            raise ResetError('Chat file unavailable for reset.')
        cwd = Path(row['cwd']).expanduser().resolve()
        if not cwd.is_dir():
            raise ResetError('Chat working directory unavailable.')
        return row, rollout, cwd

    def _check_goal(self, thread_id):
        path = self.codex.home / 'goals_1.sqlite'
        if not path.exists():
            return
        try:
            c = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2)
            try:
                row = c.execute('SELECT status FROM thread_goals WHERE thread_id=?', (thread_id,)).fetchone()
            finally:
                c.close()
        except sqlite3.Error as exc:
            raise ResetError('Could not check the chat goal status.') from exc
        if row and row[0] == 'active':
            raise ResetError('The chat has an active goal. Pause it in Codex first.')

    def _baseline(self, thread_id):
        analysis = self.codex.session(thread_id).get('analysis', {})
        points = analysis.get('points') or []
        last = points[-1] if points else {}
        if (analysis.get('pending_compaction') or not isinstance(last.get('input'), int)
                or last['input'] < 0 or not isinstance(last.get('limit'), int)
                or last['limit'] <= 0 or not last.get('model')):
            raise ResetError('Send a short question in the chat and wait for a reply to measure context before reset.')
        return {'input_tokens': last['input'], 'context_limit': last['limit'],
                'model': last['model'], 'time': last.get('time')}

    def readiness(self, thread_id):
        try:
            with self.lock:
                previous = self.results.get(thread_id, {})
                original = previous.get('original_settings') if isinstance(previous, dict) else None
                if isinstance(original, dict):
                    raise ResetError(recovery_message(original))
            self._target(thread_id)
            self._check_goal(thread_id)
            self._baseline(thread_id)
            if not self.executable.is_file():
                raise ResetError('Bundled Codex not found.')
            self._check_version()
            if self.busy_check():
                raise ResetError('Close Codex and other Codex processes, then check again.')
            return {'ready': True, 'reason': ''}
        except (ResetError, OSError, sqlite3.Error, KeyError, ValueError, TypeError) as exc:
            return {'ready': False, 'reason': str(exc)}

    def prepare(self, thread_id):
        with self.lock:
            row, rollout, _ = self._target(thread_id)
            if self.jobs.get(thread_id, {}).get('state') == 'running':
                raise ResetError('This chat is already being reset.')
            now = time.monotonic()
            self.challenges = {key: value for key, value in self.challenges.items() if value[3] > now}
            if len(self.challenges) >= 128:
                raise ResetError('Too many pending confirmations. Try again later.')
            token = secrets.token_urlsafe(24)
            st = rollout.stat()
            self.challenges[token] = (thread_id, st.st_size, st.st_mtime_ns, now+120)
            return {'token': token, 'name': self.codex.summary(row)['name'], **self.readiness(thread_id)}

    def start(self, thread_id, token):
        with self.lock:
            challenge = self.challenges.pop(token, None)
            if not challenge or challenge[0] != thread_id or challenge[3] < time.monotonic():
                raise ResetError('Confirmation expired. Reopen the dialog.')
            row, rollout, cwd = self._target(thread_id)
            st = rollout.stat()
            if (st.st_size, st.st_mtime_ns) != (challenge[1], challenge[2]):
                raise ResetError('The chat changed after confirmation. Check again.')
            ready = self.readiness(thread_id)
            if not ready['ready']:
                raise ResetError(ready['reason'])
            if any(job.get('state') == 'running' for job in self.jobs.values()):
                raise ResetError('Wait for the other reset to finish.')
            job = {'state': 'running', 'stage': 'Preparing', 'reset_applied': False,
                   'thread_id': thread_id, 'model': MODEL, 'started_at': datetime.now(timezone.utc).isoformat()}
            self.jobs[thread_id] = job
            previous = self.results.get(thread_id)
            self.results[thread_id] = dict(job)
            try:
                self._save()
            except OSError as exc:
                self.jobs.pop(thread_id, None)
                if previous is None:
                    self.results.pop(thread_id, None)
                else:
                    self.results[thread_id] = previous
                raise ResetError('Could not save operation status; reset not started.') from exc
            try:
                threading.Thread(target=self._run, args=(thread_id, rollout, cwd, job), daemon=True).start()
            except RuntimeError as exc:
                job.update({'state': 'error', 'stage': 'Incomplete', 'error': 'Could not start reset.'})
                self.results[thread_id] = dict(job)
                try:
                    self._save()
                except OSError:
                    pass
                raise ResetError(job['error']) from exc
            return dict(job)

    def status(self, thread_id):
        with self.lock:
            return {'readiness': self.readiness(thread_id), 'job': dict(self.jobs.get(thread_id, {})),
                    'result': self.results.get(thread_id)}

    def _stage(self, job, value):
        with self.lock:
            job['stage'] = value
            self.results[job['thread_id']] = dict(job)
            try:
                self._save()
            except OSError as exc:
                raise ResetError('Could not save operation status.') from exc

    def _run(self, thread_id, rollout, cwd, job):
        original = None
        server = None
        reset_attempted = False
        settings_overridden = False
        restored = False
        try:
            job['before'] = self._baseline(thread_id)
            self._stage(job, 'Checking chat settings')
            server = AppServer(self.codex.home, cwd, self.executable)
            server.initialize()
            result = server.rpc('thread/resume', {'threadId': thread_id, 'excludeTurns': True})
            if result.get('thread', {}).get('status', {}).get('type') not in ('idle', 'notLoaded'):
                raise ResetError('Chat is busy. Wait for the task to finish.')
            original = {k: result.get(k) for k in SETTINGS}
            if not original['model'] or not original['reasoningEffort'] or not original['sandbox'] or original['approvalPolicy'] is None:
                raise ResetError('Could not identify chat settings to restore.')
            if original['modelProvider'] != 'openai':
                raise ResetError('The experiment only supports the OpenAI provider.')
            job['original_settings'] = original
            # Persist recovery data before even attempting to override settings.
            self._stage(job, 'Saving chat settings')
            server.close(); server = None
            if self.busy_check():
                raise ResetError('Codex was open. Close it and try again.')
            server = AppServer(self.codex.home, cwd, self.executable)
            server.initialize()
            settings_overridden = True
            resumed = server.rpc('thread/resume', {'threadId': thread_id, 'excludeTurns': True,
                                 'model': MODEL, 'sandbox': 'read-only', 'approvalPolicy': 'never'})
            if resumed.get('thread', {}).get('id') != thread_id or resumed.get('model') != MODEL or resumed.get('modelProvider') != 'openai':
                raise ResetError('Codex opened a different chat, model or provider; operation stopped.')
            if self.busy_check({server.proc.pid}):
                raise ResetError('Codex was open. Reset not started.')
            self._stage(job, 'Creating a new context window')
            reset_offset = rollout.stat().st_size
            reset_attempted = True
            server.rpc('thread/compact/start', {'threadId': thread_id})
            completed = server.wait(lambda x: x.get('method') == 'turn/completed' and
                                    x.get('params', {}).get('threadId') == thread_id, 180)
            if completed.get('params', {}).get('turn', {}).get('status') != 'completed':
                raise ResetError('Codex did not finish the reset.')
            compacted = [r.get('payload', {}) for r in read_appended(rollout, reset_offset) if r.get('type') == 'compacted']
            if not compacted or not has_fresh_window(compacted[-1]):
                raise ResetError('Could not confirm the new context window.')
            job['reset_applied'] = True
            compactions = self.codex.session(thread_id)['analysis'].get('compactions') or []
            if not compactions or compactions[-1].get('mode') != 'reset':
                raise ResetError('Window created, but its record was not found for comparison.')
            job['reset_event_index'] = compactions[-1]['event_index']
        except Exception as exc:
            job['error'] = str(exc) if isinstance(exc, ResetError) else 'An internal error prevented completion.'
            if reset_attempted and not job.get('reset_applied'):
                try:
                    new = [r.get('payload', {}) for r in read_appended(rollout, reset_offset) if r.get('type') == 'compacted']
                    if any(has_fresh_window(x) for x in new):
                        job['reset_applied'] = True
                    elif new and all(x.get('compaction_response_id') for x in new):
                        job['reset_applied'] = False
                    else:
                        job['reset_applied'] = None
                except (OSError, ValueError):
                    job['reset_applied'] = None
        finally:
            if not settings_overridden:
                job.pop('original_settings', None)
            if server and original and settings_overridden:
                try:
                    self._stage(job, 'Restoring chat settings')
                except ResetError:
                    job['storage_error'] = 'Could not save operation status.'
                try:
                    params = {'threadId': thread_id, 'model': original['model'],
                              'effort': original['reasoningEffort'], 'approvalPolicy': original['approvalPolicy']}
                    if original.get('approvalsReviewer') is not None:
                        params['approvalsReviewer'] = original['approvalsReviewer']
                    profile = original.get('activePermissionProfile')
                    if isinstance(profile, dict) and profile.get('id'):
                        params['permissions'] = profile['id']
                    else:
                        params['sandboxPolicy'] = original['sandbox']
                    server.rpc('thread/settings/update', params, timeout=20)
                    restored = True
                except Exception:
                    job['restore_error'] = 'Could not restore the chat model and permissions. Check them in Codex.'
            if server:
                try:
                    server.close()
                except Exception:
                    job['restore_error'] = 'Could not shut down Codex cleanly. Check chat settings.'
            if restored and not job.get('restore_error'):
                verifier = None
                verified = False
                try:
                    if self.busy_check():
                        raise ResetError('Codex is already open.')
                    verifier = AppServer(self.codex.home, cwd, self.executable)
                    verifier.initialize()
                    checked = verifier.rpc('thread/resume', {'threadId': thread_id, 'excludeTurns': True}, timeout=20)
                    if checked.get('thread', {}).get('id') != thread_id or any(checked.get(k) != original.get(k) for k in SETTINGS):
                        raise ResetError('Settings differ from the originals.')
                    verified = True
                except Exception:
                    job['restore_error'] = 'Could not confirm restored settings. Check the model and permissions in Codex.'
                finally:
                    if verifier:
                        try:
                            verifier.close()
                        except Exception:
                            job['restore_error'] = 'Could not finish checking Codex settings.'
                if verified and not job.get('restore_error'):
                    job.pop('original_settings', None)
            if isinstance(job.get('original_settings'), dict):
                job['restore_error'] = recovery_message(job['original_settings'])
            with self.lock:
                if not job.get('error') and (not job.get('reset_applied') or 'reset_event_index' not in job):
                    job['error'] = 'Window creation or its record was not confirmed.'
                job['state'] = 'error' if job.get('error') or job.get('restore_error') else 'completed'
                job['stage'] = 'Done' if job['state'] == 'completed' else 'Incomplete'
                job.setdefault('completed_at', datetime.now(timezone.utc).isoformat())
                self.results[thread_id] = dict(job)
                try:
                    self._save()
                except OSError:
                    job['storage_error'] = 'Could not save the result after the inspector restarted.'
