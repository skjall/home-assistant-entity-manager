#!/usr/bin/env python3
"""Refuse a commit on a branch that is not one to work on.

A branch says what its commits are for, and the ones that are not meant to be
worked on are the ones a commit lands on by accident: main itself, and the
throwaway branch a deploy is assembled on - work has been committed to that one
more than once and had to be picked back out.
"""

import re
import subprocess
import sys

TYPES = ("feat", "fix", "docs", "chore", "ci", "test", "refactor", "perf", "build", "deps")

WORKABLE = re.compile(r"^(" + "|".join(TYPES) + r")/[a-z0-9]+([a-z0-9\-\.]*[a-z0-9])?$")

# release-please owns this one and commits to it are what it is for.
BY_ARRANGEMENT = ("release-please--branches--main",)

PROTECTED = ("main", "master")


def branch_now() -> str:
    try:
        found = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return ""
    return found.stdout.strip()


def complaint(branch: str) -> str:
    """What is wrong with committing here, or "" when nothing is."""
    if not branch or branch == "HEAD":
        # Detached, mid-rebase, mid-bisect: git knows what it is doing.
        return ""
    if branch in BY_ARRANGEMENT:
        return ""
    if branch in PROTECTED:
        return f"{branch!r} is not a branch to commit on. Branch off it and open a pull request."
    if not WORKABLE.match(branch):
        return (
            f"{branch!r} is not a branch to commit on.\n\n"
            "A branch is named after what its commits are for:\n\n"
            "    feat/rule-filters\n"
            "    fix/device-swap\n"
            "    chore/update-alpinejs\n\n"
            "One of: " + ", ".join(TYPES) + ", then a slash and lower-case words\n"
            "joined by hyphens."
        )
    return ""


def main() -> int:
    said = complaint(branch_now())
    if said:
        print("\n" + said + "\n", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
