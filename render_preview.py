#!/usr/bin/env python3
"""Render the LaunchAgent template for this checkout; never install it."""
from pathlib import Path
import plistlib
import sys


def render():
    root = Path(__file__).resolve().parent
    template = (root / 'preview.plist').read_text()
    # Substitute parsed strings so paths containing XML metacharacters remain valid.
    def resolve(value):
        if isinstance(value, str):
            return value.replace('__PROJECT_ROOT__', str(root))
        if isinstance(value, list):
            return [resolve(item) for item in value]
        if isinstance(value, dict):
            return {key: resolve(item) for key, item in value.items()}
        return value
    return plistlib.dumps(resolve(plistlib.loads(template.encode())))


if __name__ == '__main__':
    sys.stdout.buffer.write(render())
