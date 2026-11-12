"""Read-only Codex state database and rollout adapter."""
from pathlib import Path
import sqlite3
from murmuratio_app.adapters.base import Adapter, UnavailableSource, UnsupportedSource
from murmuratio_app.adapters.parsing import consume_codex


class CodexSource(Adapter):
    name = 'codex_sessions'
    def __init__(self,home):
        super().__init__()
        self.home=Path(home).expanduser().resolve()
    def connect(self):
        c=sqlite3.connect((self.home/'state_5.sqlite').as_uri()+'?mode=ro',uri=True,timeout=2)
        c.row_factory=sqlite3.Row;c.execute('PRAGMA temp_store=MEMORY');c.execute('PRAGMA query_only=ON');return c
    def rollout_path(self, value):
        path=Path(value).resolve()
        if not any(path.is_relative_to(self.home/x) for x in ['sessions','archived_sessions']):
            raise ValueError('Unexpected rollout path')
        return path
    def collect(self, operation='cwd', thread=None, summary=None, seen=None, cache=None):
        database = self.home / 'state_5.sqlite'
        if not database.is_file():
            raise UnavailableSource()
        if operation in ('context', 'cwd'):
            column = 'rollout_path' if operation == 'context' else 'cwd'
            connection = self.connect()
            try:
                row = connection.execute('SELECT ' + column + ' FROM threads WHERE id=?', (thread,)).fetchone()
            finally:
                connection.close()
            if not row or not row[column]:
                return
            if operation == 'cwd':
                yield self.observation({'cwd': row['cwd']}, database.stat().st_mtime)
                return
            path = self.rollout_path(row['rollout_path'])
            data, modified = summary(path, consume_codex)
            if cache is not None and cache[path].unreadable():
                self.unsupported()
            if seen is not None:
                seen.add(path)
            yield self.observation(data, modified, 'inferred')
            return
        raise UnsupportedSource()
