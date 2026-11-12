from pathlib import Path
import sqlite3
import tempfile
import unittest
from murmuratio_app.adapters.codex_sessions import CodexSource

class CodexTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.home=Path(self.tmp.name)
        (self.home/'sessions').mkdir();self.file=self.home/'sessions/chat.jsonl';self.file.touch()
        c=sqlite3.connect(self.home/'state_5.sqlite')
        c.execute('CREATE TABLE threads(id TEXT,title TEXT,updated_at INTEGER,cwd TEXT,model TEXT,archived INTEGER,source TEXT,agent_path TEXT,tokens_used INTEGER,rollout_path TEXT)')
        c.execute('INSERT INTO threads VALUES(?,?,?,?,?,?,?,?,?,?)',('chat','Test',1,'/project','selected-model',0,'cli','/root',999,str(self.file)))
        c.commit();c.close();self.source=CodexSource(self.home)
    def test_cwd_and_context_queries_are_read_only(self):
        before = (self.home / 'state_5.sqlite').read_bytes(), self.file.read_bytes()
        self.assertEqual(self.source.read('cwd', 'chat').values(), [{'cwd': '/project'}])
        result = self.source.read('context', 'chat', lambda p, consume: ({'path': str(p)}, 1))
        self.assertEqual(result.values(), [{'path': str(self.file.resolve())}])
        with self.source.connect() as connection:
            self.assertEqual(connection.execute('PRAGMA temp_store').fetchone()[0], 2)
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute('DELETE FROM threads')
        connection.close()
        self.assertEqual(before, ((self.home / 'state_5.sqlite').read_bytes(), self.file.read_bytes()))

    def test_context_rejects_rollout_outside_codex_directories(self):
        outside = self.home / 'private.jsonl'
        outside.write_text('do not read')
        link = self.home / 'sessions' / 'escape.jsonl'
        link.symlink_to(outside)
        def unexpected_read(*args):
            self.fail('An outside rollout was read')
        for path in (outside, link, self.home / 'sessions' / '..' / 'private.jsonl'):
            with self.subTest(path=path):
                c = sqlite3.connect(self.home / 'state_5.sqlite')
                c.execute('UPDATE threads SET rollout_path=? WHERE id=?', (str(path), 'chat'))
                c.commit(); c.close()
                result = self.source.read('context', 'chat', unexpected_read)
                self.assertEqual(result.status, 'unsupported')
                self.assertEqual(result.values(), [])

    def test_context_allows_archived_rollout(self):
        archive = self.home / 'archived_sessions'
        archive.mkdir()
        path = archive / 'chat.jsonl'
        path.touch()
        c = sqlite3.connect(self.home / 'state_5.sqlite')
        c.execute('UPDATE threads SET rollout_path=? WHERE id=?', (str(path), 'chat'))
        c.commit(); c.close()
        result = self.source.read('context', 'chat', lambda p, consume: ({'ok': p == path.resolve()}, 1))
        self.assertEqual(result.values(), [{'ok': True}])

if __name__=='__main__':unittest.main()
