"""Identify lifecycle fixtures as launched agents even when their launcher is absent."""
import json
from pathlib import Path


def fixture_parent(home, session, kind='claude', role=None):
    path = Path(home) / '.claude/tools/agents-registry.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    record = dict(id=session, kind=kind, parent_id='fixture-launcher')
    if role:
        record['role'] = role
        if role == 'orchestrator':
            record['parent_id'] = None
    with path.open('a') as stream:
        stream.write(json.dumps(record) + '\n')
