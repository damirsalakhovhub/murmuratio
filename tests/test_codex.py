import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from murmuratio_app.adapters.codex_sessions import CodexSource
from murmuratio_app.server import sanitize
from tests.helpers import chats_config

class CodexTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.home=Path(self.tmp.name)
        (self.home/'sessions').mkdir();self.file=self.home/'sessions/chat.jsonl';self.file.touch()
        c=sqlite3.connect(self.home/'state_5.sqlite')
        c.execute('CREATE TABLE threads(id TEXT,title TEXT,updated_at INTEGER,cwd TEXT,model TEXT,archived INTEGER,source TEXT,agent_path TEXT,tokens_used INTEGER,rollout_path TEXT)')
        c.execute('INSERT INTO threads VALUES(?,?,?,?,?,?,?,?,?,?)',('chat','Test',1,'/project','selected-model',0,'cli','/root',999,str(self.file)))
        c.commit();c.close();self.source=CodexSource(self.home,sanitize)
    def add(self,t,p):
        with self.file.open('a') as f:f.write(json.dumps({'type':t,'timestamp':'2026-11-02T07:00:00Z','payload':p})+'\n')
    def message(self,text,role='user'):
        self.add('response_item',{'type':'message','role':role,'content':[{'type':'input_text','text':text}]})
    def record(self,rid,inp,total):
        self.add('token_usage_record',{'response_id':rid,'usage':{'input_tokens':inp,'cached_input_tokens':10,'output_tokens':5,'reasoning_output_tokens':2,'total_tokens':inp+5},'thread_token_usage':{'input_tokens':total,'output_tokens':5,'total_tokens':total+5}})
    def test_chat_list_has_no_age_cutoff_and_is_newest_first(self):
        # Codex's own thread index lists every saved chat; no rollout is opened for the list.
        import time
        now = int(time.time())
        c = sqlite3.connect(self.home/'state_5.sqlite')
        for tid, age in (('month-old', 30 * 86400), ('yesterday', 86400), ('year-old', 365 * 86400)):
            c.execute('INSERT INTO threads VALUES(?,?,?,?,?,?,?,?,?,?)', (tid, tid, now - age, '/project', 'm', 0, 'vscode', '/root', 1, str(self.home/'sessions/missing.jsonl')))
        c.commit(); c.close()
        ids = [s['id'] for s in self.source.state()['sessions']]
        self.assertEqual(ids, ['yesterday', 'month-old', 'year-old', 'chat'])

    def test_sidebar_reads_live_sections_order_and_project_assignments(self):
        db=sqlite3.connect(self.home/'state_5.sqlite')
        for name in ['thread_section_id TEXT','section_position INTEGER','project_id TEXT','is_pinned INTEGER','recency_at INTEGER']:
            db.execute('ALTER TABLE threads ADD COLUMN '+name)
        db.execute('CREATE TABLE thread_sections(id TEXT PRIMARY KEY,name TEXT)')
        db.execute('CREATE TABLE projects(id TEXT PRIMARY KEY,name TEXT,position INTEGER)')
        db.executemany('INSERT INTO thread_sections VALUES (?,?)',[('pin','Pinned'),('a','Section A'),('b','Section B')])
        db.execute('INSERT INTO projects VALUES (?,?,?)',('new-project','Project',1))
        db.execute("UPDATE threads SET thread_section_id='a',section_position=2 WHERE id='chat'")
        db.commit()
        prefs={'electron-persisted-atom-state':{'sidebar-custom-sections-v3':{
            'other':{'sections':[]},'current':{'sections':[
                {'id':'ui-a','hostSectionIds':{'local':'a'}},{'id':'ui-b','hostSectionIds':{'local':'b'}}],
                'sectionOrder':['custom:ui-b','custom:ui-a'],'collapsedSectionIds':['ui-a']}}},
            'thread-project-assignments':{'chat':{'projectKind':'local','projectId':'old-project'}},
            'app-server-project-id-by-legacy-project-id-by-host':{'local:'+str(self.home.resolve()):{'old-project':'new-project'}}}
        path=self.home/'.codex-global-state.json';path.write_text(json.dumps(prefs))
        result=self.source.state()
        self.assertEqual([s['name'] for s in result['sidebar']['sections']],['Pinned','Section B','Section A'])
        self.assertTrue(result['sidebar']['sections'][-1]['collapsed'])
        self.assertEqual(result['sessions'][0]['project_id'],'new-project')
        self.assertEqual(result['sessions'][0]['section_id'],'a')
        db.execute("UPDATE threads SET thread_section_id='b',section_position=1 WHERE id='chat'")
        db.execute("UPDATE thread_sections SET name='Renamed' WHERE id='b'")
        db.commit();db.close()
        result=self.source.state()
        self.assertEqual(result['sessions'][0]['section_id'],'b')
        self.assertEqual(result['sidebar']['sections'][1]['name'],'Renamed')
        prefs['electron-persisted-atom-state']['sidebar-custom-sections-v3']['current']['sectionOrder']=['custom:ui-a','custom:ui-b']
        path.write_text(json.dumps(prefs))
        self.assertEqual([s['name'] for s in self.source.state()['sidebar']['sections']],['Pinned','Section A','Renamed'])
        path.write_text('{')
        self.assertEqual(self.source.state()['sidebar']['sections'][-1]['name'],'Renamed')

    def test_compaction_intervals_and_missing_measurements(self):
        self.add('turn_context',{'model':'m','turn_id':'t'})
        self.add('event_msg',{'type':'task_started','model_context_window':1000})
        self.add('compacted',{})
        self.record('a',100,100);self.record('b',200,300)
        self.add('compacted',{})
        self.record('c',150,450)
        self.add('compacted',{})
        self.add('compacted',{})
        self.record('d',80,530)
        cs=self.source.session('chat')['analysis']['compactions']
        self.assertEqual([c['responses_after'] for c in cs],[2,1,0,1])
        self.assertEqual([c['after_delta'] for c in cs],[None,50,None,None])
        self.assertEqual([c['interval_complete'] for c in cs],[True,True,True,False])
        self.assertIsNone(cs[2]['after_input'])

    def test_reset_is_not_counted_as_compaction(self):
        self.add('turn_context',{'model':'m','turn_id':'t'})
        self.add('event_msg',{'type':'task_started','model_context_window':1000})
        self.add('compacted',{'compaction_response_id':'c1'})
        self.record('a',300,300)
        self.add('compacted',{'window_id':'new','previous_window_id':'old','compaction_response_id':None})
        self.record('b',80,380)
        self.add('compacted',{'compaction_response_id':'c2'})
        self.record('c',90,470)
        result=self.source.session('chat')
        self.assertEqual(result['analysis']['compaction_count'],2)
        comps=result['analysis']['compactions']
        self.assertEqual([c['mode'] for c in comps],['compaction','reset','compaction'])
        self.assertEqual([c['number'] for c in comps],[1,None,2])
        self.assertIsNone(comps[-1]['after_delta'])
        self.assertIn('context_reset',[e['kind'] for e in result['events']])

    def test_disabled_reset_hides_details_but_keeps_ordinary_compaction(self):
        self.source = CodexSource(self.home, sanitize, include_reset=False)
        self.add('compacted', {'compaction_response_id': 'c1'})
        self.record('a', 300, 300)
        self.add('compacted', {'window_id': 'new', 'previous_window_id': 'old', 'compaction_response_id': None})
        self.record('b', 80, 380)
        result = self.source.session('chat')
        self.assertEqual(result['analysis']['compaction_count'], 1)
        self.assertEqual([c['mode'] for c in result['analysis']['compactions']], ['compaction'])
        self.assertIsNone(result['analysis']['compactions'][0]['responses_after'])
        self.assertEqual([e['kind'] for e in result['events'] if e['kind'].startswith('context_')], [])
        self.assertNotIn('reset', json.dumps(result))
        self.assertNotIn('boundary', json.dumps(result))
        self.assertNotIn('Context reset', json.dumps(result, ensure_ascii=False))

    def test_counts_not_doubled_and_model_not_inferred(self):
        self.add('turn_context',{'model':'requested','turn_id':'t','effort':'high'})
        self.add('event_msg',{'type':'task_started','model_context_window':1000})
        self.record('a',100,100);self.record('a',100,100)
        self.add('event_msg',{'type':'token_count','info':{'last_token_usage':{'input_tokens':100},'total_token_usage':{'input_tokens':100},'model_context_window':1000}})
        d=self.source.session('chat');self.assertEqual(d['analysis']['point_count'],1)
        self.assertEqual(d['session']['totals']['input_tokens'],100)
        self.assertEqual(d['events'][0]['inference']['requestedModel'],'requested')
        self.assertIsNone(d['events'][0]['inference']['resolvedModel'])
        self.assertEqual(d['analysis']['last_usage']['cache_read_tokens'],10)
    def test_compaction_replaces_context_and_waits_for_response(self):
        self.message('OLD'*1000);self.record('a',900,900)
        self.add('compacted',{'replacement_history':[{'type':'message','role':'user','content':[{'text':'KEEP'}]},{'type':'compaction','encrypted_content':'SECRET'}]})
        d=self.source.session('chat');self.assertTrue(d['analysis']['pending_compaction'])
        self.assertEqual([x['preview'] for x in d['analysis']['largest_blocks']],['KEEP'])
        self.record('b',50,950);d=self.source.session('chat')
        self.assertFalse(d['analysis']['pending_compaction']);self.assertEqual(d['analysis']['compactions'][0]['after_input'],50)
    def test_partial_append_pagination_and_cache(self):
        for n in range(130):self.message(str(n))
        d=self.source.session('chat');self.assertEqual(len(d['events']),120);self.assertEqual(d['next_before'],10)
        early=self.source.session('chat',10);self.assertEqual(len(early['events']),10)
        self.assertFalse(set(x['id'] for x in early['events']) & set(x['id'] for x in d['events']))
        with self.file.open('ab') as f:f.write(b'{"type":"response_item",')
        self.assertEqual(self.source.session('chat')['event_count'],130)
        with self.file.open('ab') as f:f.write(b'"payload":{"type":"message","role":"assistant","content":[{"text":"done"}]}}\n')
        self.assertEqual(self.source.session('chat')['event_count'],131)
        idx=self.source.cache['chat'];self.source.session('chat');self.assertIs(self.source.cache['chat'],idx)
    def test_incremental_burst_and_full_text(self):
        self.message('initial');d=self.source.session('chat');cursor=d['events'][-1]['index']
        for i in range(250):self.message(str(i))
        first=self.source.session('chat',after=cursor)
        self.assertEqual(len(first['events']),120);self.assertTrue(first['more_after'])
        second=self.source.session('chat',after=first['events'][-1]['index'])
        third=self.source.session('chat',after=second['events'][-1]['index'])
        self.assertEqual(len(third['events']),10);self.assertFalse(third['more_after'])
        text='x'*20000+' END';self.message(text)
        d=self.source.session('chat');e=d['events'][-1]
        self.assertTrue(e['blocks'][0]['truncated'])
        self.assertEqual(self.source.event_text('chat',e['id'])['text'],text)

    def test_security_and_read_only(self):
        self.message('Bearer abcdefghijklmnop <script>');self.add('response_item',{'type':'reasoning','summary':[{'text':'Summary'}],'encrypted_content':'DO_NOT_EXPOSE'})
        before=self.file.read_bytes();d=self.source.session('chat');raw=json.dumps(d)
        self.assertNotIn('abcdefghijklmnop',raw);self.assertNotIn('DO_NOT_EXPOSE',raw)
        self.assertEqual(before,self.file.read_bytes())
        c=self.source.connect()
        self.assertEqual(c.execute('PRAGMA temp_store').fetchone()[0], 2)
        with self.assertRaises(sqlite3.OperationalError):c.execute('DELETE FROM threads')
        c.close()

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
                with self.assertRaises(ValueError):
                    self.source.session('chat')

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
    def test_legacy_and_missing_usage(self):
        self.message('no usage');self.assertEqual(self.source.session('chat')['analysis']['last_usage'],{})
        info={'last_token_usage':{'input_tokens':0,'output_tokens':0},'total_token_usage':{'input_tokens':0,'output_tokens':0},'model_context_window':1000}
        self.add('event_msg',{'type':'token_count','info':info});self.add('event_msg',{'type':'token_count','info':info})
        d=self.source.session('chat');self.assertEqual(d['analysis']['point_count'],1);self.assertEqual(d['analysis']['last_usage']['input_tokens'],0)
    def test_file_replaced_and_path_rejected(self):
        self.message('old');self.source.session('chat');self.file.write_text('');self.message('new')
        self.assertEqual(self.source.session('chat')['event_count'],1)
        c=sqlite3.connect(self.home/'state_5.sqlite');c.execute('UPDATE threads SET rollout_path=?',(str(self.home/'auth.json'),));c.commit();c.close()
        with self.assertRaises(ValueError):self.source.session('chat')
    def test_display_name_and_proxy_local_only(self):
        import http.client, threading
        from http.server import ThreadingHTTPServer
        from murmuratio_app.server import make_handler
        c=sqlite3.connect(self.home/'state_5.sqlite')
        c.execute('ALTER TABLE threads ADD COLUMN name TEXT')
        c.execute("UPDATE threads SET name='Short name'");c.commit();c.close()
        self.assertEqual(self.source.state()['sessions'][0]['name'],'Short name')
        server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(self.source,'http://murmuratio.localhost:1355',config=chats_config()))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            for peer,expected in [('::1',200),('::ffff:127.0.0.1',200),('127.0.0.1, 203.0.113.10',403)]:
                h=http.client.HTTPConnection('127.0.0.1',server.server_port)
                h.request('GET','/api/codex/state',headers={'Host':'murmuratio.localhost:1355','X-Forwarded-For':peer})
                r=h.getresponse();r.read();self.assertEqual(r.status,expected);h.close()
        finally:server.shutdown();server.server_close();thread.join()

    def test_model_reroute_and_subagent_filter_metadata(self):
        self.add('event_msg',{'type':'model_rerouted','from_model':'A','to_model':'B','reason':'explicit'})
        d=self.source.session('chat');self.assertIn('explicit',d['events'][0]['blocks'][0]['text'])
        self.assertFalse(self.source.state()['sessions'][0]['is_agent'])

if __name__=='__main__':unittest.main()
