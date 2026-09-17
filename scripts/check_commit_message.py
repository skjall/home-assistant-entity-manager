#!/usr/bin/env python3
"""Refuse a commit message the release notes cannot read.

Versions and the changelog are built from the commit messages on main, and the
changelog is what Home Assistant shows before an update. A message without a
type is invisible to that: a whole release's worth of work once arrived as one
line because the commits it was squashed from said nothing release-please could
use.

Installed as a commit-msg hook:

    pre-commit install --hook-type commit-msg
"""

import re
import sys

TYPES = ("feat", "fix", "docs", "chore", "ci", "test", "refactor", "perf", "build", "deps")

# type(scope)!: subject - the scope and the breaking "!" are optional.
SUBJECT = re.compile(r"^(" + "|".join(TYPES) + r")(\([a-z0-9][a-z0-9,\-\. ]*\))?!?: .+")

# Git writes these itself, or the user is amending something already written.
ALREADY_SETTLED = re.compile(r"^(Merge |Revert |fixup! |squash! )")

LIMIT = 100


def complaint(subject: str) -> str:
    """What is wrong with this subject line, or "" when nothing is."""
    if not subject:
        return "A commit needs a message."
    if ALREADY_SETTLED.match(subject):
        return ""
    if not SUBJECT.match(subject):
        return (
            f"{subject!r}\n\n"
            "A commit message starts with what kind of change it is, so the\n"
            "release notes can read it:\n\n"
            "    feat: say which entities a rule reaches\n"
            "    fix(swap): carry the area over to the new device\n"
            "    chore: update alpinejs\n\n"
            "One of: " + ", ".join(TYPES) + ".\n"
            'Add "!" after the type for a breaking change.'
        )
    if len(subject) > LIMIT:
        return f"The subject line is {len(subject)} characters; keep it to {LIMIT}.\n\n    {subject}"
    return ""


def main() -> int:
    if len(sys.argv) < 2:
        print("check_commit_message: no message file given", file=sys.stderr)
        return 1
    with open(sys.argv[1], encoding="utf-8") as handle:
        lines = [line for line in handle.read().splitlines() if not line.startswith("#")]
    subject = next((line for line in lines if line.strip()), "")
    said = complaint(subject.strip())
    if said:
        print("\n" + said + "\n", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
