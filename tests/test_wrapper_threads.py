import json
from pathlib import Path
import tempfile
import unittest

from murmuratio_app.adapters.wrapper import CodexWrapperAdapter


class WrapperThreadTests(unittest.TestCase):
    def test_thread_of_finds_thread_from_explicit_id_resume_or_first_event(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            events = root / 'events.jsonl'
            events.write_text('{"type":"thread.started","thread_id":"event-thread"}\n')
            for fields, expected in (
                ({'thread': 'explicit-thread', 'mode': 'resume', 'target': 'resume-thread'}, 'explicit-thread'),
                ({'mode': 'resume', 'target': 'resume-thread'}, 'resume-thread'),
                ({'mode': 'new'}, 'event-thread'),
            ):
                with self.subTest(fields=fields):
                    run = root / 'run.json'
                    run.write_text(json.dumps({**fields, 'title': 'Saved task', 'started': 20, 'events': str(events)}))
                    self.assertEqual(CodexWrapperAdapter._thread_of(run), expected)

    def test_thread_of_tolerates_missing_or_invalid_thread_events(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            events = root / 'events.jsonl'
            run = root / 'run.json'
            run.write_text(json.dumps({'mode': 'new', 'title': 'Saved task', 'started': 20, 'events': str(events)}))
            self.assertEqual(CodexWrapperAdapter._thread_of(run), None)
            for content in ('{', '[]\n', '{"type":"turn.started","thread_id":"wrong-event"}\n'):
                with self.subTest(content=content):
                    events.write_text(content)
                    self.assertEqual(CodexWrapperAdapter._thread_of(run), None)
