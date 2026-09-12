"""Writing the add-on's JSON files without losing an edit or truncating a file.

Every store here follows the same shape: read the whole file into memory, change
something, write the whole file back. Two of those running at once would have
the second one write a copy of the state the first had already replaced, and a
write interrupted halfway leaves a file that cannot be read at all.

``atomically`` writes beside the file and renames, so a reader sees either the
old content or the new one. ``guarded`` puts a store's own lock around a method
that reads and writes, so the read-change-write sequence stays one step even
when several requests arrive at the same time.
"""

import functools
import json
import logging
import os
from pathlib import Path
import threading
from typing import Any, Callable, Union

logger = logging.getLogger(__name__)


def atomically(path: Union[str, Path], data: Any) -> None:
    """Write ``data`` as JSON so a reader never sees a half-written file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # A name of its own per write: two writers must not share a scratch file.
    temporary = path.with_suffix(f"{path.suffix}.{os.getpid()}.{threading.get_ident()}.tmp")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, ensure_ascii=False)
        file.flush()
        os.fsync(file.fileno())
    temporary.replace(path)


def guarded(method: Callable) -> Callable:
    """Run a method that reads and writes under its store's ``_lock``.

    The lock is reentrant, so a guarded method may call another one.
    """

    @functools.wraps(method)
    def under_lock(self, *args, **kwargs):
        lock = getattr(self, "_lock", None)
        if lock is None:
            lock = threading.RLock()
            self._lock = lock
        with lock:
            return method(self, *args, **kwargs)

    return under_lock


def new_lock() -> threading.RLock:
    """A store's own lock; reentrant so guarded methods can nest."""
    return threading.RLock()
