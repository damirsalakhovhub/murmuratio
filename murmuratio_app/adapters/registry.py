"""Read-only adapter for the agents registry that launch wrappers append to.

Schema v1 is owned upstream (task-ledger docs/agents-registry.md): {id, kind, project, role, name,
parent_id, cwd, started_at} plus optional app_id, via, run, v. Here only id and kind are required;
via, run, v and unknown fields are ignored and optional fields of the
wrong type count as missing. The last record for an id wins. A malformed line is
skipped and counted; a file with only malformed records is unsupported.
See docs/specs/agents-registry.md.
"""
import json
from pathlib import Path

from murmuratio_app.adapters.base import AbsentSource, Adapter
from murmuratio_app.common import one_line
from murmuratio_app.agent_registry import FIELDS, validate

MAX_LINE = 64 * 1024
TEXT_FIELDS = {'project': 160, 'role': 40, 'name': 240, 'parent_id': 200, 'app_id': 200, 'cwd': 1024}


def parse_record(raw):
    """A normalized record, or None when the line is not a usable registry record."""
    if not isinstance(raw, dict):
        return None
    if raw.get('via') in ('launcher', 'manual', 'hook') and 'provider_id' in raw:
        try:
            return validate({key: raw.get(key) for key in FIELDS})
        except (ValueError, TypeError, KeyError):
            return None
    record_id, kind = one_line(raw.get('id')), one_line(raw.get('kind')).lower()
    if not record_id or not kind:
        return None
    record = {'id': record_id[:200], 'kind': kind[:40]}
    for key, limit in TEXT_FIELDS.items():
        value = one_line(raw.get(key))[:limit]
        if value:
            record[key] = value
    if 'role' in record:
        record['role'] = record['role'].lower()
    started = raw.get('started_at')
    if type(started) in (int, float) and started == started and abs(started) < 1e11:
        record['started_at'] = float(started)
    return record


class AgentsRegistryAdapter(Adapter):
    name = 'agents_registry'

    def __init__(self, path):
        super().__init__()
        self.path = Path(path)
        self.malformed = 0
        self._cache = None  # (signature, records, malformed)

    def collect(self):
        self.malformed = 0
        try:
            st = self.path.stat()
        except FileNotFoundError:
            self.malformed = 0
            self._cache = None
            raise AbsentSource()
        signature = (st.st_ino, st.st_size, st.st_mtime_ns)
        if self._cache is None or self._cache[0] != signature:
            records, malformed = self.parse(self.path)
            self._cache = (signature, records, malformed)
        _, records, self.malformed = self._cache
        if self.malformed and not records:
            self.unsupported()
        for record in records.values():
            yield self.observation(record, record.get('started_at'))

    @staticmethod
    def parse(path):
        records, malformed = {}, 0
        with open(path, 'rb') as stream:
            for line in stream:
                if not line.strip():
                    continue
                record = None
                if len(line.removesuffix(b'\n')) <= MAX_LINE:
                    try:
                        record = parse_record(json.loads(line))
                    except (ValueError, UnicodeError, RecursionError):
                        pass
                if record is None:
                    # An incomplete final line can still be in the middle of an append.
                    malformed += int(line.endswith(b'\n'))
                    continue
                records.pop(record['id'], None)  # keep file order of the latest record
                records[record['id']] = record
        return records, malformed
