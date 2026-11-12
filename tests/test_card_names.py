"""Display-name regressions using minimal synthetic launch metadata."""
import json
from pathlib import Path
import unittest

from murmuratio_app.naming import display_name
from murmuratio_app.adapters.parsing import consume_claude
from murmuratio_app.adapters.codex_board import consume_rollout
from murmuratio_app.grouping import merge_codex_sources


class CardNameTests(unittest.TestCase):
    def test_eight_synthetic_names(self):
        cases = json.loads((Path(__file__).parent / 'fixtures/card-names.json').read_text())
        for case in cases:
            with self.subTest(before=case['before']):
                origin = 'description' if case['source'] == 'description' else 'wrapper'
                self.assertEqual(display_name(titles=[(origin, case['before'])], text=case['brief'], project='Board'),
                                 (case['expected'], case['source']))

    def test_naming_fixtures_are_small_and_have_no_paths_or_commit_hashes(self):
        cases = json.loads((Path(__file__).parent / 'fixtures/card-names.json').read_text())
        for case in cases:
            self.assertLess(len(case['brief']), 200)
            self.assertNotRegex(json.dumps(case), r'/[A-Za-z]|\b[0-9a-f]{7,40}\b')

    def test_metadata_greetings_and_preamble_never_name_a_card(self):
        text = ('You are a worker started by an orchestrator.\nTask:\nHi!\nKind: impl\n'
                'Mode: DRAFT\nRepo: /sample\nBudget: 30 min\nAt the limit stop, commit what works.\n'
                '# Fix missing finished cards\nMore instructions')
        for title in ('Kind: impl', 'Mode: DRAFT', 'Pre-mortem: regression', 'Budget: 30 min',
                      'Model: gpt-6.1-sol', 'Time box: 15 min', 'Reasoning effort: high', ''):
            with self.subTest(title=title):
                self.assertEqual(display_name(titles=[('wrapper', title)], text=text),
                                 ('Fix missing finished cards', 'first_user'))

    def test_explicit_title_wins_and_source_is_truthful(self):
        self.assertEqual(display_name(titles=[('desktop', 'Fix polling'), ('registry', 'Other task')], text='# Brief'),
                         ('Fix polling', 'desktop'))
        self.assertEqual(display_name(text='Kind: impl\nBudget: 10 min'), ('Untitled', None))

    def test_heading_beats_preamble_sentence_for_both_providers(self):
        text = 'Mode: DRAFT\nSome setup sentence.\n# Fix the summary guard\nDetails'
        claude, codex = {}, {}
        consume_claude(claude, {'type': 'user', 'message': {'content': text}})
        consume_rollout(codex, {'type': 'event_msg', 'payload': {'type': 'user_message', 'message': text}})
        self.assertEqual(claude['first_user'], 'Fix the summary guard')
        self.assertEqual(codex['task'], claude['first_user'])

    def test_project_prefix_and_word_boundary(self):
        self.assertEqual(display_name(text='Fix finished cards. Run the checks.')[0], 'Fix finished cards.')
        self.assertEqual(display_name(titles=[('wrapper', 'Board cleanup 2-4')], project='Board')[0], 'Cleanup 2-4')
        self.assertEqual(display_name(titles=[('desktop', 'Board orchestrator 001')], project='Board')[0], 'Board orchestrator 001')
        self.assertEqual(display_name(text='Fix finished cards without losing parent links', limit=24)[0], 'Fix finished cards…')

    def test_card_preamble_is_not_a_task_heading(self):
        self.assertEqual(display_name(text='# Sample card (project rules)\nName: Sample\nRules here.\n# Fix polling'),
                         ('Fix polling', 'first_user'))

    def test_desktop_title_wins_when_wrapper_and_rollout_merge(self):
        common = {'thread': 'sample', 'status': 'working', 'seconds': 1, '_stopped_seconds': 1}
        wrapper = {**common, '_wrapper': True, 'task': 'Fix polling', 'title_source': 'wrapper'}
        native = {**common, '_rollout': True, 'task': 'Fix renamed task', 'title_source': 'desktop'}
        card, = merge_codex_sources([wrapper], [native])
        self.assertEqual((card['task'], card['title_source']), ('Fix renamed task', 'desktop'))
