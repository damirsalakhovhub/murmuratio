"""Scope fixed-clock fixtures without leaking their timezone to other tests."""
from contextlib import contextmanager
import os
import time


@contextmanager
def local_timezone(name):
    previous = os.environ.get('TZ')
    try:
        os.environ['TZ'] = name
        time.tzset()
        yield
    finally:
        if previous is None:
            os.environ.pop('TZ', None)
        else:
            os.environ['TZ'] = previous
        time.tzset()
