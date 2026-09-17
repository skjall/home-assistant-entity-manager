#!/usr/bin/env python3
"""Rewriting one entity id in a configuration file, and nothing else.

A user's YAML is theirs: the comments explaining why a threshold is 45 seconds,
the blank lines grouping three scripts, the anchors, the quoting style, the
order of keys. Reading it into Python and writing it back out would return a
technically equivalent file with all of that gone, and that is not a repair.

So nothing here parses and re-serialises. The file is read as lines, one
occurrence of one id is replaced on one known line, and every other byte is
written back exactly as it came - line endings included. The result must still
parse before it is kept, and the original is copied aside first.

Writing happens only when the user has asked for it. Without that, this module
reports what it would do and touches nothing.
"""

from datetime import datetime, timezone
import logging
import os
import re
import shutil
import tempfile
from typing import Dict, Optional

import yaml

from yaml_structure import Forgiving

logger = logging.getLogger(__name__)

# What may not stand next to an id for it to be that id: `sensor.a` inside
# `sensor.a_b` is not a match, and neither is the `a.b` of `x.a.b`.
_PART_OF_A_LONGER_NAME = re.compile(r"[A-Za-z0-9_.]")

# A rewrite is refused rather than risked, and each refusal says why.
REFUSALS = {
    "not_allowed": "writing is switched off",
    "no_root": "the configuration directory is not mounted",
    "read_only": "the configuration directory is mounted read-only",
    "outside": "that path is not inside the configuration directory",
    "missing": "no such file",
    "unreadable": "the file could not be read as UTF-8",
    "no_such_line": "the file does not have that line",
    "not_there": "that line does not name the entity any more",
    "would_not_parse": "the change would leave the file unparseable",
    "failed": "the file could not be written",
}


class Outcome:
    """What happened, or what would have happened."""

    __slots__ = ("changed", "reason", "path", "line", "before", "after", "backup")

    def __init__(self, changed: bool, reason: str = "", **rest):
        self.changed = changed
        self.reason = reason
        self.path = rest.get("path")
        self.line = rest.get("line")
        self.before = rest.get("before")
        self.after = rest.get("after")
        self.backup = rest.get("backup")

    def to_dict(self) -> Dict:
        return {
            "changed": self.changed,
            "reason": self.reason,
            "explanation": REFUSALS.get(self.reason, self.reason),
            "path": self.path,
            "line": self.line,
            "before": self.before,
            "after": self.after,
            "backup": self.backup,
        }

    def __repr__(self) -> str:  # pragma: no cover - for test output
        return f"Outcome(changed={self.changed}, reason={self.reason!r})"


def _swap_in_line(line: str, old_id: str, new_id: str) -> Optional[str]:
    """The line with every standalone occurrence of the id replaced.

    None when the id is not in the line as an id of its own: a line that has
    moved on since it was read is left alone rather than guessed at.
    """
    rebuilt = []
    at = 0
    found_any = False
    while True:
        found = line.find(old_id, at)
        if found < 0:
            rebuilt.append(line[at:])
            break
        before = line[found - 1] if found else ""
        after = line[found + len(old_id) : found + len(old_id) + 1]
        standalone = not (_PART_OF_A_LONGER_NAME.match(before) or _PART_OF_A_LONGER_NAME.match(after))
        if standalone:
            rebuilt.append(line[at:found])
            rebuilt.append(new_id)
            found_any = True
        else:
            rebuilt.append(line[at : found + len(old_id)])
        at = found + len(old_id)
    return "".join(rebuilt) if found_any else None


class YamlEditor:
    """Replaces an entity id in the configuration directory, line by line."""

    def __init__(self, root: Optional[str], allowed: bool = False, backup_dir: Optional[str] = None):
        self.root = root
        self.allowed = allowed
        self.backup_dir = backup_dir

    def writable(self) -> bool:
        """Whether a rewrite could go through at all."""
        return bool(self.allowed and self.root and os.path.isdir(self.root) and os.access(self.root, os.W_OK))

    def _inside(self, relative: str) -> Optional[str]:
        """The full path, or None when it would leave the configuration directory."""
        if not self.root:
            return None
        full = os.path.realpath(os.path.join(self.root, relative))
        root = os.path.realpath(self.root)
        if full != root and not full.startswith(root + os.sep):
            return None
        return full

    def _keep_a_copy(self, full: str, relative: str) -> Optional[str]:
        """The original, put aside where it does not clutter the user's directory."""
        if not self.backup_dir:
            return None
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        flat = relative.replace(os.sep, "__").replace("/", "__")
        target = os.path.join(self.backup_dir, f"{flat}.{stamp}")
        try:
            os.makedirs(self.backup_dir, exist_ok=True)
            shutil.copy2(full, target)
            return target
        except OSError as error:  # noqa: BLE001 - said in the outcome, not raised
            logger.warning("Could not keep a copy of %s: %s", relative, error)
            return None

    def replace(self, relative: str, line_number: int, old_id: str, new_id: str, dry_run: bool = False) -> Outcome:
        """Replace one id on one line, leaving the rest of the file untouched."""
        if not dry_run and not self.allowed:
            return Outcome(False, "not_allowed")
        if not self.root:
            return Outcome(False, "no_root")
        full = self._inside(relative)
        if full is None:
            return Outcome(False, "outside")
        if not os.path.isfile(full):
            return Outcome(False, "missing")

        try:
            # newline="" keeps each line's own ending, so a file written on
            # another system is not quietly converted.
            with open(full, "r", encoding="utf-8", newline="") as handle:
                lines = handle.readlines()
        except (OSError, UnicodeDecodeError) as error:
            logger.warning("Could not read %s: %s", relative, error)
            return Outcome(False, "unreadable")

        if not 1 <= line_number <= len(lines):
            return Outcome(False, "no_such_line", path=relative, line=line_number)

        before = lines[line_number - 1]
        after = _swap_in_line(before, old_id, new_id)
        if after is None:
            return Outcome(False, "not_there", path=relative, line=line_number, before=before.rstrip("\r\n"))

        changed = list(lines)
        changed[line_number - 1] = after
        whole = "".join(changed)

        # A file Home Assistant cannot read is worse than a dead reference.
        try:
            yaml.compose(whole, Loader=Forgiving)
        except yaml.YAMLError as error:
            logger.warning("Refusing to write %s: %s", relative, error)
            return Outcome(False, "would_not_parse", path=relative, line=line_number)

        outcome = Outcome(
            True,
            "",
            path=relative,
            line=line_number,
            before=before.rstrip("\r\n"),
            after=after.rstrip("\r\n"),
        )
        if dry_run:
            outcome.changed = False
            outcome.reason = "dry_run"
            return outcome
        if not self.writable():
            return Outcome(False, "read_only", path=relative, line=line_number)

        outcome.backup = self._keep_a_copy(full, relative)
        try:
            self._write_whole(full, whole)
        except OSError as error:
            logger.error("Could not write %s: %s", relative, error)
            return Outcome(False, "failed", path=relative, line=line_number)
        return outcome

    def _write_whole(self, full: str, whole: str) -> None:
        """Put the file in place in one step, keeping its permissions.

        A half-written configuration file is a broken installation, so the new
        content is built beside it and moved over in one operation.
        """
        folder = os.path.dirname(full)
        handle, temporary = tempfile.mkstemp(dir=folder, prefix=".entity-manager-", suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="") as writing:
                writing.write(whole)
            existing = os.stat(full)
            os.chmod(temporary, existing.st_mode)
            try:
                os.chown(temporary, existing.st_uid, existing.st_gid)
            except (AttributeError, PermissionError):
                # Not every platform or user can, and the mode is what matters.
                pass
            os.replace(temporary, full)
        except BaseException:
            if os.path.exists(temporary):
                os.unlink(temporary)
            raise
