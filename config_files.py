#!/usr/bin/env python3
"""The YAML under /config that Home Assistant reads but its API cannot write.

An entity named in `automations.yaml`, `scripts.yaml` or `scenes.yaml` reaches
us through the configuration API, and a rename is carried into it. An entity
named in a package, an include or a YAML-mode dashboard does not: the API
answers 404 for those objects, so nothing carries the rename and the reference
goes dead without a word.

This reads those files - read-only, through the `homeassistant_config:ro` mount
declared in config.json - and says which file and which line names an entity, so
the interface can name the edit the user has to make by hand.

config.json asks for that mount at `/config`, which is what Home Assistant's own
container calls the directory and what the user sees in the interface and in
error messages. `/homeassistant` is where the Supervisor puts it when no path is
asked for, so it is looked at too, and paths are reported relative to the root
and shown under `/config` either way.
"""

import logging
import os
import re
import time
from typing import Dict, Iterable, List, Optional

from yaml_structure import Structures

logger = logging.getLogger(__name__)

# What a default installation puts behind `automation:`, `script:` and `scene:`.
# Home Assistant rewrites these when the user edits in the interface, and the
# configuration API both reads and writes them, so a rename is carried into them
# and they are not this module's business.
WRITTEN_BY_THE_INTERFACE = {"automations.yaml", "scripts.yaml", "scenes.yaml"}

# `automation: !include automations.yaml` is editable in the interface;
# `!include_dir_merge_list automations/` is not, and Home Assistant leaves those
# files alone. Only the plain include names a file we can skip.
_PLAIN_INCLUDE = re.compile(r"^(?P<key>automation|script|scene)(?:\s+\w+)?\s*:\s*!include\s+(?P<target>[^\s#]+)\s*$")

# Nothing in these holds a reference the user would edit by hand.
SKIPPED_DIRECTORIES = {
    ".cloud",
    ".git",
    ".storage",
    "backups",
    "custom_components",
    "deps",
    "node_modules",
    "tts",
    "www",
}

# Where config.json asks for the mount, and where the Supervisor puts it when a
# version of config.json asked for no path.
LIKELY_ROOTS = ("/config", "/homeassistant")

_ENTITY_ID = re.compile(r"\b([a-z_]+\.[a-z0-9_]+)\b")

# `service: automation.turn_on` names a service, not an entity, and reading it
# as one reported every script that turns an automation on as broken.
_CALLS_A_SERVICE = re.compile(r"(?:service|action|service_template)\s*:\s*[\"\']?$")

# What a card template reads off an entity it was handed: `lock.state` in a
# dashboard is an attribute, not an entity called "state" in the lock domain.
# These are Home Assistant's own field names, not anything installation-specific.
READ_OFF_AN_ENTITY = {
    "attributes",
    "context",
    "domain",
    "entity_id",
    "last_changed",
    "last_updated",
    "name",
    "object_id",
    "state",
}

# How a file pulls in another one. Only these reach Home Assistant; a file
# nothing includes is not part of the configuration, and reporting its dead
# references would bury the ones that matter.
_INCLUDES = re.compile(
    r"!include(?P<kind>_dir_list|_dir_named|_dir_merge_list|_dir_merge_named)?\s+(?P<target>[^\s#]+)"
)

# A YAML-mode dashboard is not included, it is named: `lovelace:` and each entry
# under `dashboards:` point at a file with `filename:`.
_NAMES_A_FILE = re.compile(r"filename\s*:\s*[\"\']?(?P<target>[^\s\"\'#]+\.ya?ml)")

# What `lovelace: mode: yaml` reads when no filename says otherwise.
DEFAULT_DASHBOARD = "ui-lovelace.yaml"

# Where a comment starts: a `#` at the start of the line, or one with a space
# in front of it. That is YAML's own rule, and it keeps `color: \'#ffc107\'` -
# where the `#` follows a quote - out of it.
_COMMENT_STARTS = re.compile(r"(?:^|(?<=\s))#")

# A line long enough to be a minified dashboard blob is not a line a user edits.
_LONGEST_USEFUL_LINE = 4000

# How long one reading of the directory stands. A batch of renames asks about
# every entity in turn and must not re-read the files each time; a user who is
# editing those files while working here must not be answered from yesterday.
INDEX_HOLDS_FOR = 60.0


class Mention:
    """One line of one file that names an entity."""

    __slots__ = ("path", "line", "text", "entity_id")

    def __init__(self, path: str, line: int, text: str, entity_id: str):
        self.path = path
        self.line = line
        self.text = text
        self.entity_id = entity_id

    def to_dict(self) -> Dict:
        return {
            "path": self.path,
            "shown_as": "/config/" + self.path.replace(os.sep, "/"),
            "line": self.line,
            "text": self.text,
            "entity_id": self.entity_id,
        }

    def __repr__(self) -> str:  # pragma: no cover - for test output
        return f"Mention({self.path}:{self.line} {self.entity_id})"


def find_the_root() -> Optional[str]:
    """The mounted configuration directory, or None when it is not mounted.

    HA_CONFIG_DIR overrides it, for a deployment that mounts it elsewhere.
    """
    named = os.environ.get("HA_CONFIG_DIR")
    if named:
        return named if os.path.isdir(named) else None
    for candidate in LIKELY_ROOTS:
        # configuration.yaml is what makes it the configuration directory rather
        # than some other directory that happens to carry the name.
        if os.path.isfile(os.path.join(candidate, "configuration.yaml")):
            return candidate
    return None


class ConfigFiles:
    """Reads the configuration directory for entity names."""

    def __init__(self, root: Optional[str] = None, domains: Optional[Iterable[str]] = None):
        self.root = root if root is not None else find_the_root()
        self._files: Optional[List[str]] = None
        self._managed: Optional[set] = None
        self._index: Optional[Dict[str, List[Mention]]] = None
        self._read_at = 0.0
        # What holds a line is worked out only for the lines actually reported,
        # because parsing every file costs far more than reading it.
        self.structures = Structures(self.root, domains)

    def available(self) -> bool:
        """Whether the mount is there and readable."""
        return bool(self.root) and os.path.isdir(self.root) and os.access(self.root, os.R_OK)

    def shown_as(self, relative: str) -> str:
        """The path as the user knows it, which is the one inside Home Assistant."""
        return "/config/" + relative.replace(os.sep, "/")

    def holder_of(self, mention: "Mention"):
        """The object a mention sits in, or None when the file will not parse."""
        return self.structures.what_holds(mention.path, mention.line)

    def forget(self) -> None:
        self._files = None
        self._managed = None
        self._index = None
        self._read_at = 0.0
        self.structures.forget()

    def managed_files(self) -> set:
        """The files the interface writes, as configuration.yaml sets them up."""
        if self._managed is not None:
            return self._managed
        managed = set(WRITTEN_BY_THE_INTERFACE)
        for _number, line in self._lines("configuration.yaml"):
            found = _PLAIN_INCLUDE.match(line.strip())
            if found:
                managed.add(found.group("target").strip("\"'"))
        self._managed = managed
        return managed

    def _yaml_under(self, relative_dir: str) -> List[str]:
        """Every YAML file under a directory an include names."""
        full = os.path.join(self.root, relative_dir)
        found: List[str] = []
        if not os.path.isdir(full):
            return found
        for folder, directories, names in os.walk(full, followlinks=False):
            directories[:] = [one for one in directories if one not in SKIPPED_DIRECTORIES and not one.startswith(".")]
            for name in names:
                if name.endswith((".yaml", ".yml")):
                    found.append(os.path.relpath(os.path.join(folder, name), self.root).replace(os.sep, "/"))
        return found

    def included_files(self) -> List[str]:
        """Every file configuration.yaml reaches, directly or through another.

        A directory left over from an earlier layout still holds entity names,
        and Home Assistant never reads it. Following the includes keeps those
        out of what the user is asked to repair.
        """
        start = "configuration.yaml"
        if not os.path.isfile(os.path.join(self.root, start)):
            return []
        reached = [start]
        seen = {start}
        if os.path.isfile(os.path.join(self.root, DEFAULT_DASHBOARD)):
            reached.append(DEFAULT_DASHBOARD)
            seen.add(DEFAULT_DASHBOARD)
        index = 0
        while index < len(reached):
            current = reached[index]
            index += 1
            for _number, line in self._lines(current):
                if len(line) > _LONGEST_USEFUL_LINE:
                    continue
                found = _INCLUDES.search(line) or _NAMES_A_FILE.search(line)
                if not found:
                    continue
                target = found.group("target").strip("\"'")
                # An include is written relative to the configuration directory.
                kind = found.groupdict().get("kind")
                targets = self._yaml_under(target) if kind else [target.replace(os.sep, "/")]
                for one in targets:
                    if one in seen:
                        continue
                    seen.add(one)
                    if os.path.isfile(os.path.join(self.root, one)):
                        reached.append(one)
        return reached

    def files(self) -> List[str]:
        """Every YAML file a user maintains by hand, relative to the root."""
        if self._files is not None:
            return self._files
        found: List[str] = []
        if not self.available():
            self._files = found
            return found
        managed = self.managed_files()
        for relative in self.included_files():
            if relative in managed:
                continue
            found.append(relative)
        found.sort()
        self._files = found
        return found

    def _lines(self, relative: str) -> Iterable:
        full = os.path.join(self.root, relative)
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as handle:
                for number, line in enumerate(handle, 1):
                    yield number, line
        except OSError as error:  # noqa: BLE001 - one unreadable file leaves the rest
            logger.debug("Could not read %s: %s", relative, error)

    def index(self) -> Dict[str, List[Mention]]:
        """Every entity named in those files, read once and kept.

        A rename asks about one entity at a time, and a batch of two hundred
        would otherwise read every file two hundred times.
        """
        if self._index is None or time.monotonic() - self._read_at > INDEX_HOLDS_FOR:
            self.forget()
            self._index = self.every_mention()
            self._read_at = time.monotonic()
        return self._index

    def mentions(self, entity_id: str) -> List[Mention]:
        """Every line naming this entity, across the files kept by hand."""
        return list(self.index().get(entity_id, []))

    def every_mention(self, domains: Optional[Iterable[str]] = None) -> Dict[str, List[Mention]]:
        """Every entity named anywhere in those files, with where it is named."""
        allowed = set(domains) if domains else None
        found: Dict[str, List[Mention]] = {}
        for relative in self.files():
            for number, line in self._lines(relative):
                if len(line) > _LONGEST_USEFUL_LINE:
                    continue
                # Home Assistant does not read a comment, so nothing in one is
                # a reference: nothing breaks there and there is nothing to fix.
                comment = _COMMENT_STARTS.search(line)
                if comment:
                    line = line[: comment.start()]
                stripped = line.strip()
                if not stripped:
                    continue
                seen_here = set()
                for match in _ENTITY_ID.finditer(line):
                    entity_id = match.group(1)
                    if entity_id in seen_here:
                        continue
                    domain, _, object_id = entity_id.partition(".")
                    if allowed is not None and domain not in allowed:
                        continue
                    if object_id in READ_OFF_AN_ENTITY:
                        continue
                    if _CALLS_A_SERVICE.search(line[: match.start()]):
                        continue
                    seen_here.add(entity_id)
                    found.setdefault(entity_id, []).append(Mention(relative, number, stripped, entity_id))
        return found


_shared: Optional[ConfigFiles] = None


def shared(domains: Optional[Iterable[str]] = None) -> ConfigFiles:
    """One reading of the directory for the whole process, aged out by itself."""
    global _shared
    if _shared is None:
        _shared = ConfigFiles(domains=domains)
    return _shared


def out_of_reach(old_entity_id: str, new_entity_id: str, files: Optional[ConfigFiles] = None) -> List[Dict]:
    """Where a rename leaves the old id behind, and what to put there instead.

    The configuration API does not write these files, so nothing carries the
    rename into them. Each entry is one line of one file, so the user can go
    straight there.
    """
    reading = files if files is not None else shared()
    if not reading.available() or old_entity_id == new_entity_id:
        return []
    found = []
    for one in reading.mentions(old_entity_id):
        holder = reading.holder_of(one)
        found.append(
            {
                "path": reading.shown_as(one.path),
                "line": one.line,
                "text": one.text,
                "replace": old_entity_id,
                "with": new_entity_id,
                # What the line belongs to, as far as the file says. Empty where
                # the file does not parse or names nothing.
                "in_object": holder.described() if holder else "",
                "object_id": (holder.object_id if holder else None),
                "trail": " → ".join(holder.trail) if holder else "",
            }
        )
    return found
