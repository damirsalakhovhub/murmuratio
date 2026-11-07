"""Context analysis computed from an indexed, normalized timeline."""
import math
from murmuratio_app.schema import ContextTimeline


def analyze_context(snapshot: ContextTimeline):
    points=snapshot['points'][-100:]
    comps=[]
    segments={}
    for point in snapshot['points']:
        segments.setdefault(point['segment'], []).append(point)
    previous=None;number=0
    for i,c in enumerate(snapshot['compactions']):
        if c['mode']=='boundary':
            previous=None
            continue
        measured=segments.get(i+1, [])
        first=measured[0] if measured else {}
        after=first.get('input')
        if c['mode']=='compaction':number+=1
        comparable=c['mode']=='compaction' and previous is not None and first.get('model') is not None and first.get('limit') is not None and first.get('model')==previous.get('model') and first.get('limit')==previous.get('limit')
        delta=after-previous['input'] if comparable and after is not None and previous.get('input') is not None else None
        hidden_boundary = i+1<len(snapshot['compactions']) and snapshot['compactions'][i+1]['mode']=='boundary'
        comps.append({**c,'number':number if c['mode']=='compaction' else None,'after_input':after,'after_limit':first.get('limit'),
            'after_model':first.get('model'),'after_delta':delta,'responses_after':None if hidden_boundary else len(measured),
            'interval_complete':None if hidden_boundary else i+1<len(snapshot['compactions'])})
        previous=first if measured and c['mode']=='compaction' else None
    return {'last_usage':snapshot['last'],'last_at':snapshot['last_at'],'context_limit':snapshot['last_limit'],
        'pending_compaction':snapshot['pending_compact'] and (not snapshot['compactions'] or snapshot['compactions'][-1]['mode']!='boundary'),
        'pending_context':snapshot['pending_compact'] and bool(snapshot['compactions'] and snapshot['compactions'][-1]['mode']=='boundary'),
        'model':snapshot['model'],'effort':snapshot['effort'],
        'points':points,'point_count':len(snapshot['points']),'compactions':comps,'compaction_count':number,
        'largest_blocks':[{**x,'duplicates':snapshot['hashes'][x['digest']]} for x in snapshot['top']],
        'categories':[{'name':k,'characters':v,'estimated_tokens':math.ceil(v/4)} for k,v in snapshot['categories'].items()],
        'rate_limits':snapshot['quota'],'malformed_records':snapshot['malformed'],
        'context_basis':'Сохранённые текстовые части после последнего сжатия, включая replacement history и базовые инструкции. Не точный запрос; зашифрованное резюме не оценено.'}
