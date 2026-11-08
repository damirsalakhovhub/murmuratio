"""Small shared helpers for the Python tests."""
from murmuratio_app.config import Config


def chats_config(**modules):
    """A config with the saved Codex chats module on (and any other modules given)."""
    config = Config()
    config.modules.update(codex_chats=True, **modules)
    return config
