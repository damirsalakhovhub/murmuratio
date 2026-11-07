import json
import tempfile
import unittest
from pathlib import Path

from murmuratio_app.adapters.wrapper import CodexWrapperAdapter


class WrapperTitleTests(unittest.TestCase):
    def test_threads_get_the_newest_run_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events = root / 'events.jsonl'
            events.write_text('{"type":"thread.started","thread_id":"t-new"}\n')
            runs = {
                'a': {'status': 'done', 'mode': 'new', 'thread': 't-1', 'title': 'First task', 'started': 10},
                'b': {'status': 'done', 'mode': 'resume', 'target': 't-1', 'title': 'Follow-up', 'started': 20},
                'c': {'status': 'running', 'mode': 'new', 'title': 'Fresh run', 'started': 30, 'events': str(events)},
                'd': {'status': 'done', 'mode': 'new', 'title': 'No thread', 'started': 40},
            }
            for name, run in runs.items():
                (root / f'{name}.json').write_text(json.dumps(run))
            (root / 'broken.json').write_text('{')
            adapter = CodexWrapperAdapter(root, None, None, None, None, None, None)
            self.assertEqual(adapter.titles(), {'t-1': 'Follow-up', 't-new': 'Fresh run'})
            self.assertEqual(adapter.titles(), {'t-1': 'Follow-up', 't-new': 'Fresh run'})


if __name__ == '__main__':
    unittest.main()
