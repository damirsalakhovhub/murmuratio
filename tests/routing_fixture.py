"""Replay synthetic quota inputs without reading or writing a real home."""
import copy
import json
from pathlib import Path
from unittest.mock import patch
from murmuratio_app.config import Config
from murmuratio_app.now import NowSource


def replay(home, sample, config_path):
    config = Config.load(home, config_path)
    config.limits_history.parent.mkdir(parents=True, exist_ok=True)
    config.limits_history.write_text(''.join(json.dumps(row) + '\n' for row in sample['history']))
    config.codex_resets = tuple(sample['resets'])
    config.work_hours = tuple(sample['work_hours'])
    source = NowSource(home, config=config, clock=lambda: sample['now'])
    source.claude_limits = lambda: copy.deepcopy(sample['limits']['claude'])
    source.codex_limits = lambda: copy.deepcopy(sample['limits']['codex'])
    credits = copy.deepcopy(sample['credits'])
    points = copy.deepcopy(sample.get('credit_points', []))
    source.usage_cache['codex_meter'] = dict(key=(True, False), at=sample['now'], status='available', value=(credits, [], points))
    state = source.state()
    return dict(routing_file=config.routing_file.read_text(),
                routing=json.dumps(state['routing'], ensure_ascii=False),
                calculator=json.dumps(state['calculator'], ensure_ascii=False))


def example_policy():
    return Config.load(Path('/unused'), Path(__file__).parent / 'fixtures/routing-policy.toml').routing_policy
