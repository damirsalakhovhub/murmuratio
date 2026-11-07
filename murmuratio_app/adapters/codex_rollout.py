"""Incremental read-only index for Codex rollout records."""
from collections import Counter
import hashlib
import json
import math
import time
from context_reset import has_fresh_window
from murmuratio_app.metrics.codex import analyze_context


def usage(v):
    if not isinstance(v, dict): return {}
    names = {'cache_read_tokens':'cached_input_tokens', 'cache_write_tokens':'cache_write_input_tokens', 'reasoning_tokens':'reasoning_output_tokens'}
    return {k:v.get(names.get(k,k)) for k in ['input_tokens','output_tokens','total_tokens','cache_read_tokens','cache_write_tokens','reasoning_tokens']}


def item_text(p):
    """Never expose encrypted reasoning, inline binary media or passthrough metadata."""
    t=p.get('type')
    if t=='reasoning': return '\n'.join(x.get('text','') for x in p.get('summary',[]) if isinstance(x,dict))
    if t=='compaction': return '[Encrypted compaction summary; token count unknown]'
    if t=='additional_tools': return json.dumps(p.get('tools',[]),ensure_ascii=False,indent=2)
    if t in ('function_call','custom_tool_call'): return json.dumps({'name':p.get('name'),'arguments':p.get('arguments',p.get('input'))},ensure_ascii=False,indent=2)
    if t in ('function_call_output','custom_tool_call_output'):
        v=p.get('output','')
        if isinstance(v,str):return v
        return '\n'.join(x.get('text','[Non-text result]') for x in v if isinstance(x,dict)) if isinstance(v,list) else ''
    content=p.get('content',[])
    if isinstance(content,str):return content
    if isinstance(content,list):return '\n'.join(x.get('text','[Non-text attachment]') for x in content if isinstance(x,dict))
    return ''


def item_kind(p):
    t=p.get('type')
    if t in ('message','agent_message'):
        r=p.get('role','assistant' if t=='agent_message' else '')
        return {'user':('user','You / saved user context'),'assistant':('assistant','Model reply')}.get(r,('context','Instructions / context'))
    return {'function_call':('tool_call','Tool call'),'custom_tool_call':('tool_call','Tool call'),
        'function_call_output':('tool_result','Tool result'),'custom_tool_call_output':('tool_result','Tool result'),
        'additional_tools':('context','Tool definitions'),'compaction':('context','Compaction summary'),
        'reasoning':('notification','Saved reasoning summary')}.get(t,('notification',str(t)))


class Index:
    def __init__(self,path,sanitize,include_reset=True):
        self.include_reset=include_reset
        self.epoch=time.time_ns(); self.path=path; self.sanitize=sanitize; self.offset=0; self.inode=None; self.mtime=None
        self.events=[]; self.group='initial'; self.model=None; self.effort=None; self.limit=None
        self.totals={}; self.last={}; self.last_at=None; self.last_origin=None; self.last_limit=None; self.quota=None
        self.has_records=False; self.response_ids=set(); self.last_legacy=None
        self.points=[]; self.compactions=[]; self.top=[]; self.categories=Counter(); self.hashes=Counter()
        self.base=''; self.pending_compact=False; self.malformed=0; self.unknown_format=False

    def add(self,offset,kind,title,at,**extra):
        event={'id':f'codex:{offset}:{kind}', 'index':len(self.events),'offset':offset,'kind':kind,'title':title,
               'time':at,'group_id':self.group,'group_basis':'saved_turn_or_user_order',**extra}
        self.events.append(event);return event

    def account_text(self,text,title,category,ref):
        if not text:return
        size=len(text);self.categories[category]+=size
        digest=hashlib.sha256(text.encode()).hexdigest()
        # Bounded exact-text duplicate hints; not cache inference.
        if digest in self.hashes or len(self.hashes)<10000:self.hashes[digest]+=1
        entry={'title':title,'category':category,'characters':size,'estimated_tokens':math.ceil(size/4),
               'preview':self.sanitize(text[:2000]),'digest':digest,'event_id':ref}
        self.top.append(entry);self.top.sort(key=lambda x:x['characters'],reverse=True);del self.top[12:]

    def record_usage(self,p,offset,at,legacy=False):
        raw=p.get('usage',{}) if not legacy else p.get('last_token_usage',{})
        u=usage(raw)
        total=p.get('thread_token_usage') if not legacy else p.get('total_token_usage')
        if total:self.totals=usage(total)
        self.last=u;self.last_at=at;self.last_origin='token_count' if legacy else 'token_usage_record'
        if legacy and self.pending_compact:return # synthetic post-compaction count is not a new response
        self.pending_compact=False;self.last_limit=self.limit
        e=self.add(offset,'model_record','Model request',at,usage=u,
            inference={'provider':'codex','requestedModel':self.model,'resolvedModel':p.get('model'),'reasoning':self.effort})
        self.points.append({'event_id':e['id'],'time':at,'input':u.get('input_tokens'),'output':u.get('output_tokens'),
            'cached':u.get('cache_read_tokens'),'model':self.model,'limit':self.limit,'segment':len(self.compactions)})

    def refresh(self):
        st=self.path.stat()
        if self.inode is not None and (st.st_ino!=self.inode or st.st_size<self.offset or (st.st_size==self.offset and st.st_mtime_ns!=self.mtime)):
            self.__init__(self.path,self.sanitize,self.include_reset)
        self.inode=st.st_ino
        with self.path.open('rb') as f:
            f.seek(self.offset)
            while True:
                offset=f.tell();line=f.readline()
                if not line or not line.endswith(b'\n'):break # retry partial append on next poll
                self.offset=f.tell()
                try:d=json.loads(line)
                except (ValueError,UnicodeError):self.malformed+=1;continue
                if not isinstance(d,dict):self.malformed+=1;continue
                try:
                    p=d.get('payload',{});t=d.get('type');at=d.get('timestamp')
                    if t not in ('session_meta','turn_context','response_item','token_usage_record','compacted','event_msg'):
                        self.unknown_format=True
                    if not isinstance(p,dict):self.malformed+=1;continue
                    if t=='session_meta':
                        b=p.get('base_instructions',{})
                        self.base=b.get('text','') if isinstance(b,dict) else str(b or '')
                        self.account_text(self.base,'Base instructions','Instructions','base')
                    elif t=='turn_context':
                        old=self.model;self.model=p.get('model');self.effort=p.get('effort')
                        self.group=p.get('turn_id') or self.group
                        if old and old!=self.model:self.add(offset,'model_change','Requested model changed',at,text=f'{old} → {self.model}')
                    elif t=='response_item':
                        kind,title=item_kind(p);txt=item_text(p)
                        if kind=='user' and self.group=='initial':self.group=f'user:{offset}'
                        if not txt:continue
                        e=self.add(offset,kind,title,at)
                        cat={'tool_result':'Tool results','tool_call':'Tool calls','user':'User messages','assistant':'Model replies','context':'Instructions / context'}.get(kind,'Reasoning summary')
                        if p.get('type')!='compaction':self.account_text(txt,title,cat,e['id'])
                    elif t=='token_usage_record':
                        rid=p.get('response_id')
                        if rid and rid in self.response_ids:continue
                        if rid:self.response_ids.add(rid)
                        self.has_records=True;self.record_usage(p,offset,at)
                    elif t=='compacted':
                        before=self.last.get('input_tokens')
                        reset=has_fresh_window(p)
                        mode='reset' if reset and self.include_reset else 'boundary' if reset else 'compaction'
                        self.compactions.append({'mode':'boundary'} if mode=='boundary' else
                            {'time':at,'before_input':before,'event_index':len(self.events),'mode':mode})
                        if mode=='reset':
                            self.add(offset,'context_reset','Context reset',at,text='New context window created.')
                        elif mode=='compaction':
                            self.add(offset,'compaction','Context compaction',at,text='History replaced with the saved compaction result.')
                        self.pending_compact=True;self.top=[];self.categories=Counter();self.hashes=Counter()
                        self.account_text(self.base,'Base instructions','Instructions','base')
                        for x in p.get('replacement_history',[]) or []:
                            if not isinstance(x,dict) or x.get('type')=='compaction':continue
                            kind,title=item_kind(x)
                            self.account_text(item_text(x),('After change: ' if mode=='boundary' else 'After compaction: ')+title,
                                              'Saved after change' if mode=='boundary' else 'Saved during compaction',None)
                    elif t=='event_msg':
                        et=p.get('type')
                        if et=='task_started':
                            self.group=p.get('turn_id') or self.group;self.limit=p.get('model_context_window',self.limit)
                        elif et=='token_count':
                            info=p.get('info') or {};self.quota=p.get('rate_limits') or self.quota
                            if not isinstance(info,dict):self.malformed+=1;continue
                            self.limit=info.get('model_context_window',self.limit)
                            if not self.has_records and info:
                                fingerprint=json.dumps(info.get('total_token_usage'),sort_keys=True)
                                if fingerprint!=self.last_legacy:
                                    self.record_usage(info,offset,at,True);self.last_legacy=fingerprint
                        elif et in ('model_rerouted','model/rerouted'):
                            self.add(offset,'model_change','Server reroute',at,text=json.dumps({k:p.get(k) for k in ['from_model','to_model','fromModel','toModel','reason'] if k in p},ensure_ascii=False))
                        elif et in ('error','turn_aborted'):
                            self.add(offset,'error' if et=='error' else 'notification','Error' if et=='error' else 'Turn aborted',at,text=str(p.get('message') or p.get('reason') or et))
                except (ValueError, TypeError, AttributeError, KeyError, OverflowError):
                    self.malformed += 1
        self.mtime=st.st_mtime_ns

    def page(self,before=None,limit=120,after=None):
        end=min(before,len(self.events)) if before is not None else len(self.events)
        start=max(0,end-limit)
        if after is not None:
            start=min(max(0,after+1),len(self.events));end=min(len(self.events),start+limit)
        out=[]
        with self.path.open('rb') as f:
            for e in self.events[start:end]:
                v={k:x for k,x in e.items() if k!='offset'}
                txt=e.get('text','')
                if e['kind'] not in ('model_record','model_change','compaction','context_reset','error') and not txt:
                    f.seek(e['offset'])
                    try:txt=item_text(json.loads(f.readline()).get('payload',{}))
                    except (ValueError,UnicodeError):txt='Could not read record'
                clean=self.sanitize(txt)
                v['blocks']=[{'id':'content','text':clean[:16000], 'title':e['title'],
                    'characters':len(txt),'estimated_tokens':math.ceil(len(txt)/4),'truncated':len(clean)>16000}] if txt else []
                out.append(v)
        return out, start if start else None

    def analysis(self):
        return analyze_context({name: getattr(self, name) for name in ['pending_compact', 'compactions', 'categories', 'last_limit', 'malformed', 'last_at', 'effort', 'points', 'hashes', 'quota', 'model', 'last', 'top']})
