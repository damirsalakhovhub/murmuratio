"""Uniform fault-isolated interface for local source adapters."""
from abc import ABC, abstractmethod
import sqlite3
import threading
from murmuratio_app.schema import AdapterResult, Observation


class UnavailableSource(Exception):
    """The source does not exist or cannot currently be read."""


class UnsupportedSource(Exception):
    """The source exists but its format is not understood."""


class Adapter(ABC):
    name: str

    def __init__(self):
        self._read_lock = threading.RLock()
        self.status = self.last_status = 'available'

    def read(self, *args, **kwargs):
        with self._read_lock:
            self.status = 'available'
            try:
                observations = list(self.collect(*args, **kwargs))
            except (UnavailableSource, OSError):
                self.status, observations = 'unavailable', []
            except sqlite3.OperationalError as exc:
                # Missing schema is incompatible; locks and open failures are temporary.
                message = str(exc).lower()
                self.status = 'unsupported' if 'no such table' in message or 'no such column' in message else 'unavailable'
                observations = []
            except (UnsupportedSource, ValueError, TypeError, KeyError, AttributeError,
                    UnicodeError, sqlite3.DatabaseError):
                self.status, observations = 'unsupported', []
            except Exception:
                # Unexpected failures stay local and never expose paths or content.
                self.status, observations = 'unavailable', []
            self.last_status = self.status
            return AdapterResult(self.name, self.status, observations, getattr(self, 'age_seconds', None))

    def observation(self, value, timestamp=None, confidence='observed'):
        return Observation(self.name, timestamp, confidence, value)

    def unsupported(self):
        self.status = 'unsupported'

    def unavailable(self):
        if self.status == 'available':
            self.status = 'unavailable'

    @abstractmethod
    def collect(self, *args, **kwargs):
        """Yield normalized observations; unknown fields must be ignored."""
