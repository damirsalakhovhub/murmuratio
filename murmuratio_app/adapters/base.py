"""Uniform fault-isolated interface for local source adapters."""
from abc import ABC, abstractmethod
import fnmatch
import errno
import os
import sqlite3
import stat
import threading
from murmuratio_app.schema import AdapterResult, Observation


class UnavailableSource(Exception):
    """The source cannot currently be read."""


class AbsentSource(Exception):
    """The optional source has not been created."""


class UnsupportedSource(Exception):
    """The source exists but its format is not understood."""


def read_error_reason(error):
    """Return only fixed diagnostic codes, never exception text or source paths."""
    if isinstance(error, PermissionError):
        return 'permission_denied'
    return {errno.EACCES: 'permission_denied', errno.EPERM: 'permission_denied',
            errno.ENOENT: 'file_missing', errno.ENOTDIR: 'not_a_directory',
            errno.EISDIR: 'not_a_file'}.get(getattr(error, 'errno', None), 'read_failed')


class Adapter(ABC):
    name: str

    def __init__(self):
        self._read_lock = threading.RLock()
        self.status = self.last_status = 'available'
        self.reason = None

    def read(self, *args, **kwargs):
        with self._read_lock:
            self.status = 'available'
            self.reason = None
            observations = []
            try:
                for observation in self.collect(*args, **kwargs):
                    observations.append(observation)
            except AbsentSource:
                self.status = 'absent'
            except (UnavailableSource, OSError) as exc:
                self.status = 'unavailable'
                self.reason = read_error_reason(exc)
            except sqlite3.OperationalError as exc:
                # Missing schema is incompatible; locks and open failures are temporary.
                message = str(exc).lower()
                self.status = 'unsupported' if 'no such table' in message or 'no such column' in message else 'unavailable'
                self.reason = 'unrecognized_format' if self.status == 'unsupported' else 'read_failed'
            except (UnsupportedSource, ValueError, TypeError, KeyError, AttributeError,
                    UnicodeError, sqlite3.DatabaseError):
                self.status = 'unsupported'
                self.reason = 'unrecognized_format'
            except Exception:
                # Unexpected failures stay local and never expose paths or content.
                self.status = 'unavailable'
                self.reason = 'reader_failed'
            self.last_status = self.status
            return AdapterResult(self.name, self.status, observations, getattr(self, 'age_seconds', None), self.reason)

    def observation(self, value, timestamp=None, confidence='observed'):
        return Observation(self.name, timestamp, confidence, value)

    def unsupported(self):
        self.status = 'unsupported'
        self.reason = 'unrecognized_format'

    def unavailable(self, error=None):
        if self.status == 'available':
            self.status = 'unavailable'
            self.reason = read_error_reason(error)

    def files(self, pattern):
        """Walk a source without hiding directory access failures as empty results."""
        try:
            mode = self.root.stat().st_mode
        except FileNotFoundError:
            raise AbsentSource()
        if not stat.S_ISDIR(mode):
            raise NotADirectoryError(errno.ENOTDIR, 'Source is not a directory')
        parts = pattern.split('/')
        def walk(directory, depth):
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        if entry.name.startswith('.') or not fnmatch.fnmatchcase(entry.name, parts[depth]):
                            continue
                        path = directory / entry.name
                        if depth == len(parts) - 1:
                            yield path
                        elif entry.is_dir():
                            yield from walk(path, depth + 1)
            except OSError as exc:
                self.unavailable(exc)
        yield from walk(self.root, 0)

    @abstractmethod
    def collect(self, *args, **kwargs):
        """Yield normalized observations; unknown fields must be ignored."""
