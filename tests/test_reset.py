"""Reset tests use temporary data; some start the bundled server without model calls."""
import http.client
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from http.server import ThreadingHTTPServer

from murmuratio_app.adapters.codex_sessions import CodexSource
from murmuratio_app.context_reset import AppServer, CODEX, MODEL, VERIFIED_VERSION, ResetController, ResetError, _flags, has_fresh_window
from murmuratio_app.server import make_handler, sanitize
from tests.helpers import chats_config

ID = '01234567-0123-4567-89ab-0123456789ab'


class FakeAppServer:
    instances = []
    rollout = None
    mode = ''

    def __init__(self, home, cwd, executable):
        self.proc = SimpleNamespace(pid=999999)
        self.calls = []
        self.turns = 0
        self.__class__.instances.append(self)

    def write(self, kind, payload):
        with self.rollout.open('a') as f:
            f.write(json.dumps({'type': kind, 'payload': payload}) + '\n')

    def initialize(self):
        pass

    def rpc(self, method, params, timeout=90):
        self.calls.append((method, params))
        if method == 'thread/resume':
            model=params.get('model','original')
            if self.mode=='wrong_model' and 'model' in params: model='wrong'
            if self.mode=='restore_mismatch' and len(self.instances)>=3: model='wrong'
            provider='other' if self.mode=='wrong_provider' else 'openai'
            return {'thread': {'id': ID, 'status': {'type':'idle'}}, 'model':model,
                    'modelProvider':provider,'reasoningEffort':'high', 'approvalPolicy':'never', 'sandbox':{'type':'readOnly'},
                    'activePermissionProfile':{'id':':workspace','extends':None}}
        if method == 'thread/compact/start':
            if self.mode == 'unexpected':
                raise RuntimeError('internal')
            if self.mode == 'unconfirmed':
                self.write('compacted', {'compaction_response_id':'normal'})
            elif self.mode == 'unknown_compaction':
                self.write('compacted', {'replacement_history':[]})
            else:
                self.write('compacted', {'previous_window_id':'old','window_id':'new'})
            if self.mode == 'crash_after_reset':
                raise RuntimeError('after window creation')
            return {}
        return {}

    def wait(self, predicate, timeout=90):
        return {'params': {'threadId': ID, 'turn': {'status':'completed'}}}

    def turn(self, thread_id):
        raise AssertionError('The reset must not send a model prompt')

    def close(self):
        pass


class ResetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        (self.home/'sessions').mkdir(); self.rollout = self.home/'sessions/chat.jsonl'; self.rollout.touch()
        self.exe = self.home/'codex'; self.exe.touch()
        c = sqlite3.connect(self.home/'state_5.sqlite')
        c.execute('CREATE TABLE threads(id TEXT,title TEXT,cwd TEXT,archived INTEGER,rollout_path TEXT)')
        c.execute('INSERT INTO threads VALUES(?,?,?,?,?)',(ID,'Test',str(self.home),0,str(self.rollout)))
        c.commit(); c.close()
        with self.rollout.open('a') as f:
            for kind,payload in [('turn_context',{'turn_id':'before','model':'original'}),
                    ('event_msg',{'type':'task_started','turn_id':'before','model_context_window':1000}),
                    ('token_usage_record',{'response_id':'before','usage':{'input_tokens':500}})]:
                f.write(json.dumps({'type':kind,'payload':payload})+'\n')
        self.codex = CodexSource(self.home,sanitize)
        self.directory = self.home/'reset-state'
        self.controller = ResetController(self.codex,self.directory,busy_check=lambda ignored=():False,executable=self.exe,
                                          version_check=lambda _:VERIFIED_VERSION)
        FakeAppServer.instances=[]; FakeAppServer.rollout=self.rollout; FakeAppServer.mode=''

    def worker(self):
        job={'state':'running','stage':'Preparing','reset_applied':False,'thread_id':ID,'model':MODEL}
        with patch('murmuratio_app.context_reset.AppServer',FakeAppServer):
            self.controller._run(ID,self.rollout,self.home,job)
        return job

    def test_success_measures_same_thread_and_restores_original_settings(self):
        job=self.worker()
        self.assertEqual(job['state'],'completed');self.assertTrue(job['reset_applied'])
        self.assertEqual(job['before']['input_tokens'],500);self.assertNotIn('after',job)
        self.assertEqual(job['before']['context_limit'],1000);self.assertEqual(job['thread_id'],ID)
        calls=[call for server in FakeAppServer.instances for call in server.calls]
        self.assertIn(('thread/compact/start',{'threadId':ID}),calls)
        restore=next(params for method,params in calls if method=='thread/settings/update')
        self.assertEqual((restore['model'],restore['effort'],restore['permissions']),('original','high',':workspace'))
        self.assertNotIn('sandboxPolicy',restore)
        self.assertFalse(any(method=='turn/start' for method,_ in calls))
        self.assertEqual(self.controller._load()[ID]['before']['input_tokens'],500)

    def test_wrong_model_never_starts_reset(self):
        FakeAppServer.mode='wrong_model';job=self.worker()
        self.assertEqual(job['state'],'error');self.assertFalse(job['reset_applied'])
        self.assertFalse(any(method=='thread/compact/start' for s in FakeAppServer.instances for method,_ in s.calls))
        self.assertTrue(any(method=='thread/settings/update' for s in FakeAppServer.instances for method,_ in s.calls))

    def test_unconfirmed_reset_never_reported_as_success(self):
        FakeAppServer.mode='unconfirmed';job=self.worker()
        self.assertEqual(job['state'],'error');self.assertFalse(job['reset_applied'])

    def test_unknown_compaction_is_reported_as_unknown(self):
        FakeAppServer.mode='unknown_compaction';job=self.worker()
        self.assertEqual(job['state'],'error');self.assertIsNone(job['reset_applied'])

    def test_wrong_provider_never_starts_reset(self):
        FakeAppServer.mode='wrong_provider';job=self.worker()
        self.assertEqual(job['state'],'error');self.assertFalse(job['reset_applied'])
        self.assertFalse(any(method=='thread/compact/start' for s in FakeAppServer.instances for method,_ in s.calls))

    def test_unexpected_failure_never_reported_as_success(self):
        FakeAppServer.mode='unexpected';job=self.worker()
        self.assertEqual(job['state'],'error');self.assertIsNone(job['reset_applied'])
        self.assertTrue(any(method=='thread/settings/update' for s in FakeAppServer.instances for method,_ in s.calls))

    def test_window_change_is_reported_even_if_compact_call_fails(self):
        FakeAppServer.mode='crash_after_reset';job=self.worker()
        self.assertEqual(job['state'],'error');self.assertTrue(job['reset_applied'])

    def test_mismatched_restored_settings_are_reported(self):
        FakeAppServer.mode='restore_mismatch';job=self.worker()
        self.assertEqual(job['state'],'error');self.assertIn('original',job['restore_error'])
        self.assertEqual(job['original_settings']['activePermissionProfile']['id'],':workspace')

    def test_missing_baseline_blocks_operation(self):
        self.rollout.write_text('')
        self.assertFalse(self.controller.readiness(ID)['ready'])

    def test_interrupted_job_is_reported_as_unknown_after_restart(self):
        original={'model':'original','reasoningEffort':'high','approvalPolicy':'never','sandbox':{'type':'readOnly'}}
        self.controller.results[ID]={'thread_id':ID,'state':'running','stage':'Resetting context','reset_applied':False,'original_settings':original}
        self.controller._save()
        restarted=ResetController(self.codex,self.directory,busy_check=lambda ignored=():False,executable=self.exe,
                                  version_check=lambda _:VERIFIED_VERSION)
        result=restarted.status(ID)['result']
        self.assertEqual(result['state'],'error');self.assertIsNone(result['reset_applied'])
        self.assertIn('original',result['restore_error'])
        self.assertFalse(restarted.readiness(ID)['ready'])

    def test_recovery_snapshot_cannot_be_overwritten_by_retry(self):
        original={'model':'original','reasoningEffort':'high','sandbox':{'type':'readOnly'},'approvalPolicy':'never'}
        self.controller.results[ID]={'state':'error','original_settings':original}
        ready=self.controller.prepare(ID)
        self.assertFalse(ready['ready']);self.assertIn('original',ready['reason']);self.assertIn('readOnly',ready['reason'])
        with self.assertRaises(ResetError):self.controller.start(ID,ready['token'])
        self.assertEqual(self.controller.results[ID]['original_settings'],original)

    def test_unverified_codex_version_is_blocked(self):
        self.controller.version_check=lambda _: 'codex-cli changed'
        self.assertFalse(self.controller.readiness(ID)['ready'])

    def test_denied_client_tool_request_stops_rpc(self):
        server=AppServer.__new__(AppServer);server.seq=0;server.blocked_tool=True
        server.send=lambda value:None;server.wait=lambda predicate,timeout:{'id':1,'result':{}}
        with self.assertRaisesRegex(ResetError,'unsupported action'):server.rpc('test',{})

    def test_readiness_and_bound_one_time_challenge(self):
        original=self.rollout.read_text()
        self.controller.busy_check=lambda ignored=():True
        self.assertFalse(self.controller.readiness(ID)['ready'])
        ready=self.controller.prepare(ID)
        with self.assertRaises(ResetError): self.controller.start(ID,ready['token'])
        self.controller.busy_check=lambda ignored=():False
        with self.assertRaises(ResetError): self.controller.start(ID,ready['token'])
        ready=self.controller.prepare(ID)
        self.rollout.write_text('{}\n')
        with self.assertRaises(ResetError): self.controller.start(ID,ready['token'])
        self.rollout.write_text(original)
        ready=self.controller.prepare(ID)
        with patch('murmuratio_app.context_reset.threading.Thread') as thread:
            self.controller.start(ID,ready['token'])
            thread.return_value.start.assert_called_once()
        with self.assertRaises(ResetError): self.controller.start(ID,ready['token'])

    def test_target_and_active_goal_are_blocked(self):
        with self.assertRaises(ResetError): self.controller.prepare('../other')
        c=sqlite3.connect(self.home/'state_5.sqlite')
        c.execute('UPDATE threads SET rollout_path=?',(str(self.home/'outside.jsonl'),));c.commit();c.close()
        with self.assertRaises(ResetError): self.controller.prepare(ID)
        c=sqlite3.connect(self.home/'state_5.sqlite')
        c.execute('UPDATE threads SET rollout_path=?',(str(self.rollout),));c.commit();c.close()
        c=sqlite3.connect(self.home/'goals_1.sqlite')
        c.execute('CREATE TABLE thread_goals(thread_id TEXT,status TEXT)')
        c.execute('INSERT INTO thread_goals VALUES (?,?)',(ID,'active'));c.commit();c.close()
        self.assertFalse(self.controller.readiness(ID)['ready'])

    def test_fresh_window_marker_is_distinct_from_compaction(self):
        self.assertFalse(has_fresh_window({'compaction_response_id':'normal','previous_window_id':'a','window_id':'b'}))
        self.assertFalse(has_fresh_window({'previous_window_id':'a','window_id':'b','replacement_history':[{'type':'compaction'}]}))
        self.assertTrue(has_fresh_window({'previous_window_id':'a','window_id':'b'}))

    def test_config_is_process_local_and_escaped(self):
        config='[mcp_servers."odd.name"]\ncommand="x"\n[plugins."special-id"]\nenabled=true\n'
        (self.home/'config.toml').write_text(config)
        flags=_flags(self.home)
        self.assertIn('mcp_servers={"odd.name"={enabled=false}}',flags)
        self.assertIn('plugins={"special-id"={enabled=false}}',flags)
        self.assertEqual((self.home/'config.toml').read_text(),config)

    @unittest.skipUnless(CODEX.is_file(), 'Bundled Codex is unavailable')
    def test_real_codex_disables_configured_tools_with_dotted_names(self):
        home=self.home/'engine-home';home.mkdir()
        marker=self.home/'mcp-started'
        config=('[mcp_servers."odd.name"]\ncommand="/usr/bin/false"\n'
                '[mcp_servers.plain]\ncommand="/usr/bin/touch"\nargs=['+json.dumps(str(marker))+']\n'
                '[mcp_servers."already-off"]\ncommand="/usr/bin/false"\nenabled=false\n'
                '[mcp_servers."url-server"]\nurl="https://127.0.0.1:1/mcp"\n'
                '[plugins."one.two@source"]\nenabled=true\n'
                '[apps."specific.app"]\nenabled=true\n')
        (home/'config.toml').write_text(config)
        server=AppServer(home,self.home)
        try:
            server.initialize()
            result=server.rpc('config/read',{'cwd':str(self.home),'includeLayers':False})['config']
            self.assertEqual({k:v.get('enabled',True) for k,v in result['mcp_servers'].items()},
                             {'odd.name':False,'plain':False,'already-off':False,'url-server':False})
            self.assertEqual(result['plugins']['one.two@source']['enabled'],False)
            self.assertEqual(result['apps']['specific.app']['enabled'],False)
            self.assertEqual(result['apps']['_default']['enabled'],False)
        finally:
            server.close()
        self.assertFalse(marker.exists())
        self.assertEqual((home/'config.toml').read_text(),config)

    @unittest.skipUnless(CODEX.is_file(), 'Bundled Codex is unavailable')
    def test_project_level_mcp_fails_closed_before_opening_chat(self):
        home=self.home/'engine-home';home.mkdir()
        project=self.home/'project';project.mkdir();(project/'.codex').mkdir()
        marker=self.home/'project-mcp-started'
        (home/'config.toml').write_text('[projects.'+json.dumps(str(project))+']\ntrust_level="trusted"\n')
        (project/'.codex/config.toml').write_text('[mcp_servers.project_server]\ncommand="/usr/bin/touch"\nargs=['+json.dumps(str(marker))+']\n')
        server=AppServer(home,project)
        try:
            with self.assertRaisesRegex(ResetError,'MCP'):
                server.initialize()
        finally:
            server.close()
        self.assertFalse(marker.exists())

    def test_reset_state_directory_permissions_and_symlink_guard(self):
        supplied = self.home / 'existing-directory'
        supplied.mkdir(mode=0o755)
        supplied.chmod(0o755)
        controller = ResetController(self.codex, supplied)
        self.assertEqual(supplied.stat().st_mode & 0o777, 0o755)
        controller._save()
        self.assertEqual(supplied.stat().st_mode & 0o777, 0o755)
        self.directory.chmod(0o755)
        self.controller.results['test'] = {'state': 'completed'}
        self.controller._save()
        self.assertEqual(self.directory.stat().st_mode & 0o777, 0o755)
        self.assertEqual((self.directory / 'reset').stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.directory / 'reset/reset-results.json').stat().st_mode & 0o777, 0o600)
        target = self.home / 'unrelated'
        target.mkdir()
        link = self.home / 'linked-state'
        link.symlink_to(target)
        controller = ResetController(self.codex, link, busy_check=lambda ignored=(): False)
        with self.assertRaises(OSError):
            controller._save()
        self.assertFalse((target / 'reset-results.json').exists())

    def test_legacy_reset_results_remain_readable_and_are_preserved(self):
        legacy = self.directory / 'reset-results.json'
        original = json.dumps({'old': {'state': 'completed'}})
        legacy.write_text(original)
        legacy.chmod(0o600)
        controller = ResetController(self.codex, self.directory)
        self.assertEqual(controller.results['old']['state'], 'completed')
        controller.results['new'] = {'state': 'completed'}
        controller._save()
        self.assertEqual(legacy.read_text(), original)
        self.assertEqual(ResetController(self.codex, self.directory).results, controller.results)

    def test_http_reset_requires_local_origin(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(self.codex,reset=self.controller,config=chats_config(context_reset=True)))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(lambda:(server.shutdown(),server.server_close(),thread.join()))
        authority=f'127.0.0.1:{server.server_port}'
        def post(origin=None):
            c=http.client.HTTPConnection('127.0.0.1',server.server_port)
            headers={'Content-Type':'application/json','X-Murmuratio-Action':'1'}
            if origin: headers['Origin']=origin
            c.request('POST','/api/codex/reset/prepare',json.dumps({'id':ID}),headers)
            r=c.getresponse();status=r.status;data=json.loads(r.read());c.close();return status,data
        self.assertEqual(post()[0],403)
        self.assertEqual(post('http://evil.example')[0],403)
        status,data=post('http://'+authority);self.assertEqual(status,200);self.assertTrue(data['ready'])


if __name__=='__main__': unittest.main()
