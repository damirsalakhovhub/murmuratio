"""Launch one headless worker and associate its provider identity with an exact parent."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

from murmuratio_app.agent_registry import AgentRegistry, process_start, single_line


class InterruptedLaunch(BaseException):
    def __init__(self, signum):
        self.signum = signum


def command_for(args, session_id):
    if args.to == 'codex':
        if args.permission_mode:
            raise ValueError('--permission-mode applies only to Claude')
        command = ['codex', 'exec', '--json', '--model', args.model,
                   '-c', 'model_reasoning_effort=' + json.dumps(args.effort)]
        if args.sandbox:
            command += ['--sandbox', args.sandbox]
        return command + ['-']
    if args.sandbox:
        raise ValueError('--sandbox applies only to Codex')
    command = ['claude', '-p', '--session-id', session_id, '--model', args.model,
               '--effort', args.effort, '--output-format', 'stream-json', '--verbose']
    if args.permission_mode:
        command += ['--permission-mode', args.permission_mode]
    return command


def observed_settings(event, kind):
    """Only effective values explicitly emitted by a provider, never requested defaults."""
    values = {}
    if kind == 'claude':
        source = event if event.get('type') == 'system' and event.get('subtype') == 'init' else event.get('message', {})
    else:
        source = event.get('payload', {}) if event.get('type') in ('turn_context', 'session_meta') else {}
    if not isinstance(source, dict):
        return values
    model = source.get('model')
    effort = source.get('effort') or source.get('reasoning_effort')
    if isinstance(effort, dict):
        effort = effort.get('level')
    for key, value in (('model', model), ('effort', effort)):
        if isinstance(value, str):
            try:
                values[key] = single_line(value, key)
            except ValueError:
                pass
    return values


def stop_process(process, signum):
    if process is None:
        return
    try:
        os.killpg(process.pid, signum)
    except ProcessLookupError:
        pass
    if process.poll() is None:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()


def spawn(home, args):
    for key in ('title', 'model', 'effort', 'parent_id'):
        single_line(getattr(args, key), '--' + key.replace('_', '-'), 200 if key == 'parent_id' else 1024)
    if args.parent_subagent_id:
        single_line(args.parent_subagent_id, '--parent-subagent-id', 200)
        if args.parent_kind != 'claude':
            raise ValueError('--parent-subagent-id requires --parent-kind claude')
    cwd = Path(args.cwd or Path.cwd()).resolve(strict=True)
    if not cwd.is_dir():
        raise ValueError('--cwd must be a directory')
    provider_id = str(uuid.uuid4()) if args.to == 'claude' else None
    command = command_for(args, provider_id)
    registry = AgentRegistry(home)
    with Path(args.brief_file).open('rb') as brief:
        # A denied registry write must fail before a worker starts.
        record = registry.write(dict(kind=args.to, provider_id=provider_id, role='worker', name=args.title,
            parent_kind=args.parent_kind, parent_id=args.parent_id, parent_subagent_id=args.parent_subagent_id,
            cwd=str(cwd), status='starting', model_requested=args.model, effort_requested=args.effort, via='launcher'))
        process, exit_code, status = None, 1, 'failed'
        handlers = {}
        pending_interrupt = []
        def interrupted(signum, frame):
            if process is None:
                pending_interrupt.append(signum)
                return
            raise InterruptedLaunch(signum)
        try:
            for signum in (signal.SIGINT, signal.SIGTERM):
                handlers[signum] = signal.signal(signum, interrupted)
            process = subprocess.Popen(command, cwd=cwd, stdin=brief, stdout=subprocess.PIPE,
                                       start_new_session=True)
            if pending_interrupt:
                raise InterruptedLaunch(pending_interrupt[0])
            record = registry.write(dict(status='running', pid=process.pid, pid_start=process_start(process.pid)), record['id'])
            try:
                for line in process.stdout:
                    sys.stdout.buffer.write(line)
                    sys.stdout.buffer.flush()
                    try:
                        event = json.loads(line)
                    except (ValueError, UnicodeError):
                        continue
                    if not isinstance(event, dict):
                        continue
                    updates = observed_settings(event, args.to)
                    if args.to == 'codex' and event.get('type') == 'thread.started' and not record['provider_id']:
                        updates['provider_id'] = single_line(event.get('thread_id'), 'Codex thread ID', 200)
                    updates = {key: value for key, value in updates.items() if value != record.get(key)}
                    if updates:
                        record = registry.write(updates, record['id'])
                result = process.wait()
            finally:
                process.stdout.close()
            exit_code = result if result >= 0 else 128 - result
            status = 'done' if result == 0 else 'interrupted' if result < 0 else 'failed'
            if result == 0 and not record['provider_id']:
                exit_code, status = 1, 'failed'
                print('Error: Codex exited without reporting its thread ID', file=sys.stderr)
        except InterruptedLaunch as exc:
            stop_process(process, exc.signum)
            if process is not None and process.stdout is not None:
                process.stdout.close()
            exit_code, status = 128 + exc.signum, 'interrupted'
        except BaseException as exc:
            stop_process(process, signal.SIGTERM)
            exit_code = 127 if isinstance(exc, FileNotFoundError) and process is None else 1
            if isinstance(exc, KeyboardInterrupt):
                exit_code, status = 130, 'interrupted'
            else:
                message = str(exc).splitlines()
                print('Error: ' + (message[0] if message else type(exc).__name__), file=sys.stderr)
        finally:
            for signum, handler in handlers.items():
                signal.signal(signum, handler)
            registry.write(dict(status=status, exit_code=exit_code, ended_at=time.time()), record['id'])
        return exit_code
