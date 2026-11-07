"""Read normalized usage observations from Claude and Codex session files."""
from pathlib import Path
from murmuratio_app.adapters.base import Adapter, UnavailableSource
from murmuratio_app.adapters.parsing import consume_codex_meter, consume_usage
from murmuratio_app.adapters.shared import JsonlSummary


class ClaudeUsageAdapter(Adapter):
    name = 'claude_usage'

    def __init__(self, root):
        super().__init__()
        self.root = Path(root)
        self.cache = {}

    def collect(self, since, desktop, include_models):
        if not self.root.is_dir():
            raise UnavailableSource()
        seen = set()
        for path in [*self.root.glob('*/*.jsonl'), *self.root.glob('*/*/subagents/*.jsonl')]:
            try:
                if path.stat().st_mtime < since:
                    continue
                seen.add(path)
                summary = self.cache.setdefault(path, JsonlSummary())
                data, modified = summary.refresh(path, lambda d, r: consume_usage(d, r, include_models))
                if summary.malformed or (summary.record_types and not summary.record_types.intersection({'user', 'assistant'})):
                    self.unsupported()
                session = path.parent.parent.name if path.parent.name == 'subagents' else path.stem
                kind = 'worker' if desktop.get(session, {}).get('has_parent') else 'chat'
                yield self.observation(dict(kind=kind, hours=data.get('hours', {}), models=data.get('models', {})),
                                       modified, 'inferred')
            except OSError:
                self.unavailable()
            except (ValueError, TypeError, OverflowError, AttributeError):
                self.unsupported()
        self.cache = {p: v for p, v in self.cache.items() if p in seen}


class CodexMeterAdapter(Adapter):
    name = 'codex_meter'

    def __init__(self, root):
        super().__init__()
        self.root = Path(root)
        self.cache = {}

    def collect(self, now, include_credits, include_burn):
        if not self.root.is_dir():
            raise UnavailableSource()
        seen = set()
        for path in self.root.glob('*/*/*/rollout-*.jsonl'):
            try:
                if path.stat().st_mtime < now - 60 * 86400:
                    continue
                seen.add(path)
                summary = self.cache.setdefault(path, JsonlSummary())
                data, modified = summary.refresh(path, lambda d, r: consume_codex_meter(d, r, include_credits, include_burn))
                known = {'session_meta', 'event_msg', 'turn_context', 'response_item', 'token_usage_record', 'compacted'}
                if summary.malformed or (summary.record_types and not summary.record_types.intersection(known)):
                    self.unsupported()
                yield self.observation(dict(points=data.get('points', []), usage=data.get('usage', []),
                                            latest=data.get('latest')), modified, 'inferred')
            except OSError:
                self.unavailable()
            except (ValueError, TypeError, OverflowError, AttributeError):
                self.unsupported()
        self.cache = {p: v for p, v in self.cache.items() if p in seen}
