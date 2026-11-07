"""Read-only Codex state database and rollout adapter."""
from collections import OrderedDict
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import threading
from murmuratio_app.adapters.base import Adapter, UnavailableSource, UnsupportedSource
from murmuratio_app.adapters.codex_rollout import Index, item_text
from murmuratio_app.common import timestamp
from murmuratio_app.adapters.parsing import consume_codex

LIMITATIONS = [
    'Сохранённая история Codex, не точная копия сетевого запроса. Скрытая версия модели неизвестна.',
    'Последний вход — измерение прошлого обращения, не прогноз следующего. После сжатия ждём новый замер.',
    'Размеры блоков — символы / 4, не токенизатор. Зашифрованные части и изображения не оценены.',
    'Кеш входит во вход, reasoning входит в выход. Счётчики не означают денежное списание.',
]

def stamp(v):
    return datetime.fromtimestamp(v, timezone.utc).isoformat() if isinstance(v, (int,float)) else v


class CodexSource(Adapter):
    name = 'codex_sessions'
    def __init__(self,home,sanitize,include_reset=True):
        super().__init__()
        self.home=Path(home).expanduser().resolve();self.sanitize=sanitize;self.include_reset=include_reset;self.cache=OrderedDict();self.lock=threading.RLock()
        self.sidebar_stamp=None;self.sidebar_preferences={}
    def connect(self):
        c=sqlite3.connect((self.home/'state_5.sqlite').as_uri()+'?mode=ro',uri=True,timeout=2)
        c.row_factory=sqlite3.Row;c.execute('PRAGMA temp_store=MEMORY');c.execute('PRAGMA query_only=ON');return c
    def rollout_path(self, value):
        path=Path(value).resolve()
        if not any(path.is_relative_to(self.home/x) for x in ['sessions','archived_sessions']):
            raise ValueError('Unexpected rollout path')
        return path
    def summary(self,r):
        d=dict(r)
        return self.sanitize({'id':d['id'],'name':d.get('name') or ((d.get('title') or 'Без названия')[:160] + ('…' if len(d.get('title') or '')>160 else '')),'updated_at':stamp(d.get('updated_at')),
            'cwd':d.get('cwd'),'model':d.get('model'),'archived':bool(d.get('archived')),
            'is_agent':bool(d.get('agent_path') and d.get('agent_path')!='/root') or 'subagent' in str(d.get('source','')).lower(),
            'totals':{'total_tokens':d.get('tokens_used')},'provider':'codex'})

    def sidebar_settings(self):
        """Read only sidebar preferences; never return other desktop state."""
        path=self.home/'.codex-global-state.json'
        with self.lock:
            try:
                stat=path.stat();revision=(stat.st_mtime_ns,stat.st_size)
                if revision==self.sidebar_stamp:return self.sidebar_preferences
                data=json.loads(path.read_text(encoding='utf-8'))
                atoms=data.get('electron-persisted-atom-state',{})
                if not isinstance(atoms,dict):raise ValueError('Invalid sidebar state')
                self.sidebar_preferences={
                    'custom':atoms.get('sidebar-custom-sections-v3',{}),
                    'collapsed':atoms.get('sidebar-collapsed-sections-v1',{}),
                    'project_expanded':{k:v for k,v in atoms.items() if k.startswith('sidebar-project-expanded-v1-codex:') and isinstance(v,bool)},
                    'legacy_ids':data.get('app-server-project-id-by-legacy-project-id-by-host',{}),
                    'assignments':data.get('thread-project-assignments',{}),
                    'pinned_ids':data.get('pinned-thread-ids',[]),
                }
                self.sidebar_stamp=revision
            except (OSError, ValueError, json.JSONDecodeError, AttributeError):
                # A concurrent desktop write must not erase the last good structure.
                pass
            return self.sidebar_preferences

    def sidebar(self,c,columns):
        tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        settings=self.sidebar_settings()
        collapsed=settings.get('collapsed',{})
        if not isinstance(collapsed,dict):collapsed={}
        sections=[]
        if 'thread_sections' in tables and 'thread_section_id' in columns:
            sections=[dict(r) for r in c.execute('SELECT id,name FROM thread_sections ORDER BY rowid')]
        section_ids={s['id'] for s in sections}
        custom=settings.get('custom',{})
        if not isinstance(custom,dict):custom={}
        # The desktop stores account-specific UI IDs and their corresponding local DB IDs.
        candidates=[v for v in custom.values() if isinstance(v,dict)]
        def score(account):
            items=account.get('sections',[])
            if not isinstance(items,list):return 0
            return sum(isinstance(s,dict) and isinstance(s.get('hostSectionIds'),dict) and s['hostSectionIds'].get('local') in section_ids for s in items)
        account=max(candidates,key=score,default={})
        ui_sections=account.get('sections',[]) if isinstance(account.get('sections'),list) else []
        ui_order=account.get('sectionOrder',[]) if isinstance(account.get('sectionOrder'),list) else []
        ui_collapsed=account.get('collapsedSectionIds',[]) if isinstance(account.get('collapsedSectionIds'),list) else []
        rank={key:i for i,key in enumerate(ui_order)}
        mapping={}
        for s in ui_sections:
            if not isinstance(s,dict) or not isinstance(s.get('hostSectionIds'),dict):continue
            db_id=s['hostSectionIds'].get('local');ui_id=s.get('id')
            if db_id in section_ids and isinstance(ui_id,str):
                mapping[db_id]=(rank.get('custom:'+ui_id,len(rank)),ui_id in ui_collapsed)
        sections.sort(key=lambda s: (0,0) if s['name']=='Pinned' else (1,mapping.get(s['id'],(len(rank),False))[0]))
        result_sections=[{'id':s['id'],'name':s['name'],'kind':'pinned' if s['name']=='Pinned' else 'section',
            'collapsed':bool(collapsed.get('pinned',False)) if s['name']=='Pinned' else mapping.get(s['id'],(0,False))[1]} for s in sections]
        projects=[]
        if 'projects' in tables and 'project_id' in columns:
            legacy=settings.get('legacy_ids',{})
            reverse={}
            if isinstance(legacy,dict):
                for host,mapping_by_id in legacy.items():
                    if str(host).startswith('local:') and isinstance(mapping_by_id,dict):
                        reverse.update({v:k for k,v in mapping_by_id.items() if isinstance(v,str)})
            expanded=settings.get('project_expanded',{})
            for r in c.execute('SELECT id,name,position FROM projects ORDER BY position,id'):
                d=dict(r);key='sidebar-project-expanded-v1-codex:'
                is_open=expanded.get(key+d['id'],expanded.get(key+reverse.get(d['id'],''),False))
                projects.append({'id':d['id'],'name':d['name'],'collapsed':not is_open})
        return self.sanitize({'sections':result_sections,'projects':projects,
            'projects_collapsed':bool(collapsed.get('threads',True)),
            'recents_collapsed':bool(collapsed.get('chats',False)),
            'legacy_pinned_ids':settings.get('pinned_ids',[]) if not any(s['kind']=='pinned' for s in result_sections) else []})

    def _state(self):
        c=self.connect()
        try:
            columns={r[1] for r in c.execute('PRAGMA table_info(threads)')}
            if not {'id','updated_at'}.issubset(columns):raise UnsupportedSource()
            fields=[x for x in ['id','name','title','updated_at','recency_at','cwd','model','archived','source','agent_path','tokens_used','thread_section_id','section_position','project_id','is_pinned'] if x in columns]
            sort='COALESCE(NULLIF(recency_at,0),updated_at)' if 'recency_at' in columns else 'updated_at'
            rows=c.execute('SELECT '+','.join(fields)+' FROM threads ORDER BY '+sort+' DESC,id').fetchall()
            settings=self.sidebar_settings();assignments=settings.get('assignments',{});legacy=settings.get('legacy_ids',{})
            if not isinstance(assignments,dict):assignments={}
            if not isinstance(legacy,dict):legacy={}
            local_map=legacy.get('local:'+str(self.home),{})
            if not isinstance(local_map,dict):local_map={}
            sessions=[]
            for r in rows:
                d=dict(r);assignment=assignments.get(d['id'])
                legacy_id=assignment.get('projectId') if isinstance(assignment,dict) and assignment.get('projectKind')=='local' else None
                project_id=d.get('project_id') or local_map.get(legacy_id) or legacy_id
                sessions.append(self.summary(r) | {'section_id':d.get('thread_section_id'),
                    'section_position':d.get('section_position'),'project_id':project_id,'is_pinned':bool(d.get('is_pinned'))})
            return {'source':{'connected':True,'limitations':LIMITATIONS},'sessions':sessions,'sidebar':self.sidebar(c,columns)}
        finally:c.close()
    def collect(self, operation='sessions', thread=None, summary=None, seen=None, cache=None):
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
            if cache is not None and cache[path].malformed:
                self.unsupported()
            if seen is not None:
                seen.add(path)
            yield self.observation(data, modified, 'inferred')
            return
        if operation != 'sessions':
            raise UnsupportedSource()
        state = self._state()
        self._sidebar = state['sidebar']
        for session in state['sessions']:
            yield self.observation(session, timestamp(session.get('updated_at')) or database.stat().st_mtime, 'inferred')

    def state(self):
        with self._read_lock:
            result = self.read()
            if result.status == 'available':
                return {'source': {'connected': True, 'limitations': LIMITATIONS},
                        'sessions': result.values(), 'sidebar': self._sidebar}
            return {'source': {'connected': False, 'status': result.status, 'limitations': LIMITATIONS},
                    'sessions': result.values(), 'sidebar': {}}

    def session(self,sid,before=None,after=None):
        with self.lock:
            c=self.connect()
            try:r=c.execute('SELECT * FROM threads WHERE id=?',(sid,)).fetchone()
            finally:c.close()
            if r is None:raise KeyError(sid)
            if 'rollout_path' not in r.keys():raise ValueError('Unsupported thread schema')
            path=self.rollout_path(r['rollout_path'])
            idx=self.cache.get(sid)
            if idx is None or idx.path!=path:idx=Index(path,self.sanitize,self.include_reset);self.cache[sid]=idx
            self.cache.move_to_end(sid)
            while len(self.cache)>3:self.cache.popitem(last=False)
            idx.refresh();events,next_before=idx.page(before,after=after)
            s=self.summary(r);s['totals']=idx.totals or s['totals'];s['requested_model']=idx.model
            result={'session':s,'events':events,'analysis':idx.analysis(),'next_before':next_before,
                    'event_count':len(idx.events),'more_after': bool(events and events[-1]['index'] < len(idx.events)-1),'revision':f'{idx.epoch}:{idx.offset}', 'limitations':LIMITATIONS}
            if idx.malformed or idx.unknown_format:
                result['source']={'status':'unsupported'}
            return result

    def event_text(self,sid,event_id):
        with self.lock:
            self.session(sid,before=0)
            idx=self.cache[sid]
            e=next((x for x in idx.events if x['id']==event_id),None)
            if e is None:raise KeyError(event_id)
            with idx.path.open('rb') as f:
                f.seek(e['offset']);p=json.loads(f.readline()).get('payload',{})
            return {'text':self.sanitize(item_text(p))}
