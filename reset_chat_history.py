#!/usr/bin/env python3
"""Reset one local Codex chat while preserving its thread ID.

Run only while Codex/ChatGPT is closed. This edits Codex's undocumented local
storage, so it is deliberately limited to the tested app-server version.
"""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import uuid


CODEX_HOME = Path.home() / '.codex'
CODEX = Path('/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex')
TESTED_VERSION = 'codex-cli 0.166.0-alpha.5.1'

# Each table is copied/restored only for the selected chat.
TABLES = {
    'state_5.sqlite': [('threads', 'id'), ('thread_read_receipts', 'thread_id')],
    'thread_history_1.sqlite': [('thread_items', 'thread_id'),
                                ('thread_realtime_items', 'thread_id'),
                                ('thread_turns', 'thread_id'),
                                ('thread_history_projection_state', 'thread_id')],
}


class ResetError(Exception):
    pass


def ro(path):
    return sqlite3.connect(f'{path.resolve().as_uri()}?mode=ro', uri=True, timeout=5)


def assert_closed():
    result = subprocess.run(['/bin/ps', '-axo', 'pid=,comm='], check=True,
                            capture_output=True, text=True, timeout=5)
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2: continue
        exe = parts[1]
        if (exe.endswith('/ChatGPT.app/Contents/MacOS/ChatGPT') or
            exe.endswith('/Codex.app/Contents/MacOS/Codex') or
            exe.endswith('/codex') or exe == 'codex'):
            raise ResetError('Полностью закройте ChatGPT/Codex и все запущенные процессы Codex, затем повторите.')


def assert_version():
    try:
        v = subprocess.run([str(CODEX), '--version'], check=True, capture_output=True,
                           text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError) as e:
        raise ResetError('Не удалось определить версию встроенного Codex.') from e
    if v != TESTED_VERSION:
        raise ResetError(f'Скрипт проверен на {TESTED_VERSION}; обнаружен {v}.')


def get_thread(home, query):
    with ro(home / 'state_5.sqlite') as db:
        db.row_factory = sqlite3.Row
        try:
            uid = str(uuid.UUID(query))
            rows = db.execute('SELECT * FROM threads WHERE id=?', (uid,)).fetchall()
        except ValueError:
            rows = db.execute("SELECT * FROM threads WHERE name=? COLLATE NOCASE OR title=? COLLATE NOCASE",
                              (query, query)).fetchall()
            if not rows:
                rows = db.execute("SELECT * FROM threads WHERE name LIKE ? ESCAPE '\\' COLLATE NOCASE",
                                  ('%' + query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%',)).fetchall()
    if not rows: raise ResetError('Чат не найден.')
    if len(rows) > 1:
        matches = '\n'.join(f"  {r['id']}  {r['name'] or r['title']}" for r in rows[:20])
        raise ResetError('Найдено несколько чатов. Запустите скрипт с нужным ID:\n' + matches)
    return dict(rows[0])


def check_thread(home, thread):
    tid = thread['id']
    if thread['archived']: raise ResetError('Чат находится в архиве; сначала верните его из архива.')
    if thread['history_mode'] != 'paginated':
        raise ResetError('Этот формат истории не проверен; сброс остановлен.')
    path = Path(thread['rollout_path']).resolve()
    if not path.is_relative_to((home / 'sessions').resolve()) or not path.is_file():
        raise ResetError('Файл истории находится в неожиданном месте; сброс остановлен.')
    with path.open('rb') as f: first = f.readline()
    try: meta = json.loads(first)
    except (ValueError, UnicodeError) as e: raise ResetError('Некорректный файл истории.') from e
    if not first.endswith(b'\n') or meta.get('type') != 'session_meta' or meta.get('payload', {}).get('id') != tid:
        raise ResetError('ID в файле истории не совпадает; сброс остановлен.')
    for dbname, table, message in [
        ('queue_1.sqlite', 'queued_items', 'В чате есть сообщения в очереди.'),
        ('goals_1.sqlite', 'thread_goals', 'У чата есть сохранённая цель.'),
        ('memories_1.sqlite', 'stage1_outputs', 'Для чата существует сохранённая память.')]:
        p = home / dbname
        if not p.exists(): continue
        try:
            with ro(p) as db: count = db.execute(f'SELECT COUNT(*) FROM {table} WHERE thread_id=?', (tid,)).fetchone()[0]
        except sqlite3.Error as e:
            raise ResetError(f'Не удалось проверить {dbname}; сброс остановлен.') from e
        if count: raise ResetError(message + ' Сброс не запущен.')
    return path, first


def snapshot_table(source, snapshot, table, column, tid):
    # CREATE TABLE AS copies SQLite values only for this thread, including blobs.
    with sqlite3.connect(snapshot, timeout=10, uri=True) as db:
        db.execute('ATTACH DATABASE ? AS src', (f'{source.resolve().as_uri()}?mode=ro',))
        db.execute(f'CREATE TABLE "{table}" AS SELECT * FROM src."{table}" WHERE "{column}"=?', (tid,))
        db.commit()


def make_backup(home, thread, rollout):
    root = home / 'inspector-reset-backups'
    root.mkdir(mode=0o700, exist_ok=True)
    os.chmod(root, 0o700)
    folder = root / (datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '-' + thread['id'])
    folder.mkdir(mode=0o700)
    try:
        shutil.copyfile(rollout, folder / 'rollout.jsonl')
        os.chmod(folder / 'rollout.jsonl', 0o600)
        for name, tables in TABLES.items():
            src, dst = home / name, folder / name
            if not src.exists(): raise ResetError(f'Не найдена база {name}.')
            for table, column in tables: snapshot_table(src, dst, table, column, thread['id'])
            os.chmod(dst, 0o600)
        manifest = {'thread_id': thread['id'], 'codex_home': str(home.resolve()),
                    'rollout_path': str(rollout), 'name': thread['name'],
                    'version': TESTED_VERSION}
        (folder / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
        os.chmod(folder / 'manifest.json', 0o600)
        return folder
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise


def atomic_write(path, content):
    mode = stat.S_IMODE(path.stat().st_mode)
    fd, temp = tempfile.mkstemp(prefix='.inspector-reset-', dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'wb') as f:
            f.write(content); f.flush(); os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        try: os.unlink(temp)
        except FileNotFoundError: pass


def atomic_copy(source, target):
    mode = stat.S_IMODE(target.stat().st_mode)
    fd, temp = tempfile.mkstemp(prefix='.inspector-restore-', dir=target.parent)
    try:
        os.fchmod(fd, mode)
        with source.open('rb') as src, os.fdopen(fd, 'wb') as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)
            dst.flush(); os.fsync(dst.fileno())
        os.replace(temp, target)
    finally:
        try: os.unlink(temp)
        except FileNotFoundError: pass


def wipe(home, thread, rollout, first):
    tid = thread['id']
    # Metadata is kept verbatim, including the ID, project and configured tools.
    atomic_write(rollout, first)
    with sqlite3.connect(home / 'thread_history_1.sqlite', timeout=10) as db:
        db.execute('PRAGMA secure_delete=ON')
        db.execute('BEGIN IMMEDIATE')
        for table, column in TABLES['thread_history_1.sqlite']:
            db.execute(f'DELETE FROM "{table}" WHERE "{column}"=?', (tid,))
        db.commit()
    with sqlite3.connect(home / 'state_5.sqlite', timeout=10) as db:
        db.execute('PRAGMA secure_delete=ON')
        db.execute('BEGIN IMMEDIATE')
        # The old first prompt can be duplicated in title/preview even after the
        # rollout is cleared. Preserve the explicit name rather than that text.
        db.execute("UPDATE threads SET title=CASE WHEN name IS NOT NULL AND name!='' THEN name ELSE '' END, "
                   "first_user_message='', preview='', tokens_used=0, has_user_event=0 WHERE id=?", (tid,))
        if db.total_changes != 1: raise ResetError('Строка чата не найдена при обновлении.')
        db.execute('DELETE FROM thread_read_receipts WHERE thread_id=?', (tid,))
        db.commit()


def restore(home, folder):
    manifest_path = folder / 'manifest.json'
    meta = json.loads(manifest_path.read_text())
    tid = str(uuid.UUID(meta['thread_id']))
    if Path(meta['codex_home']).resolve() != home.resolve():
        raise ResetError('Резервная копия создана для другого каталога Codex.')
    rollout = Path(meta['rollout_path']).resolve()
    if not rollout.is_relative_to((home / 'sessions').resolve()) or not rollout.is_file():
        raise ResetError('Исходный файл чата не найден в текущем Codex.')
    backup_rollout = folder / 'rollout.jsonl'
    with backup_rollout.open('rb') as f:
        first = json.loads(f.readline())
    if first.get('payload', {}).get('id') != tid: raise ResetError('Некорректная резервная копия.')
    for name, tables in TABLES.items():
        snap = folder / name
        if not snap.is_file(): raise ResetError(f'В резервной копии нет {name}.')
        with sqlite3.connect(home / name, timeout=10) as db:
            db.execute('ATTACH DATABASE ? AS saved', (f'{snap.resolve().as_uri()}?mode=ro',))
            db.execute('BEGIN IMMEDIATE')
            for table, column in tables:
                columns = [r[1] for r in db.execute(f'PRAGMA main.table_info("{table}")')]
                saved_cols = [r[1] for r in db.execute(f'PRAGMA saved.table_info("{table}")')]
                if columns != saved_cols:
                    raise ResetError('Схема Codex изменилась; автоматическое восстановление остановлено.')
                if table == 'threads':
                    values = db.execute('SELECT * FROM saved.threads WHERE id=?', (tid,)).fetchone()
                    if not values: raise ResetError('В резервной копии нет данных чата.')
                    updates = [c for c in columns if c != 'id']
                    data = dict(zip(columns, values))
                    updated = db.execute('UPDATE threads SET ' + ','.join(f'"{c}"=?' for c in updates) + ' WHERE id=?',
                                         [data[c] for c in updates] + [tid])
                    if updated.rowcount != 1: raise ResetError('Исходный чат не найден.')
                else:
                    db.execute(f'DELETE FROM main."{table}" WHERE "{column}"=?', (tid,))
                    db.execute(f'INSERT INTO main."{table}" SELECT * FROM saved."{table}" WHERE "{column}"=?', (tid,))
            db.commit()
    atomic_copy(backup_rollout, rollout)


def main():
    parser = argparse.ArgumentParser(description='Очистить историю одного локального чата Codex, сохранив его ID.')
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--chat', help='Название или ID чата')
    group.add_argument('--restore', metavar='BACKUP_DIR', help='Восстановить созданную скриптом резервную копию')
    args = parser.parse_args()
    try:
        assert_version()
        assert_closed()
        if args.restore:
            folder = Path(args.restore).expanduser().resolve()
            if not folder.is_relative_to((CODEX_HOME / 'inspector-reset-backups').resolve()):
                raise ResetError('Путь находится вне каталога резервных копий.')
            print('Восстановить резервную копию:', folder)
            print('Сообщения, добавленные в этот чат после сброса, будут заменены содержимым копии.')
            if input('Для подтверждения введите ВОССТАНОВИТЬ: ').strip() != 'ВОССТАНОВИТЬ':
                print('Отменено.'); return
            assert_closed(); restore(CODEX_HOME, folder)
            print('Резервная копия восстановлена.'); return
        thread = get_thread(CODEX_HOME, args.chat)
        rollout, first = check_thread(CODEX_HOME, thread)
        print(f"Чат: {thread['name'] or thread['title']}\nID: {thread['id']}\nПроект: {thread['cwd']}")
        print('История чата будет очищена; ID и проект сохранятся. Резервная копия останется на диске.')
        if input('Для подтверждения введите СБРОС: ').strip() != 'СБРОС':
            print('Отменено.'); return
        folder = make_backup(CODEX_HOME, thread, rollout)
        assert_closed()
        try:
            wipe(CODEX_HOME, thread, rollout, first)
        except Exception as error:
            try:
                restore(CODEX_HOME, folder)
            except Exception as restore_error:
                raise ResetError(f'Сброс не завершён; автоматическое восстановление не удалось: {restore_error}. Копия: {folder}') from error
            raise ResetError(f'Сброс не завершён, исходная история восстановлена: {error}') from error
        print('Готово. ID сохранён. Резервная копия:', folder)
        print('Откройте Codex и отправьте короткое сообщение, чтобы увидеть новый размер контекста.')
    except (ResetError, OSError, sqlite3.Error, subprocess.SubprocessError, json.JSONDecodeError, EOFError) as e:
        print('Сброс не выполнен:', e, file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__': main()
