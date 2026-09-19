#!/usr/bin/env python3
"""Refuse a template whose inline script does not parse.

The panel is one page with its behaviour written into it, and a stray bracket
there is not a failing test - it is a blank screen. Nothing else looks: the
Python tests never load the template, and the frontend job only builds the
stylesheet. So the script blocks are handed to node, which is the same parser
the browser uses.

Skipped where node is not installed, since a missing tool must not turn into a
refusal to commit; the CI has node and runs this too.
"""

from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent.parent
TEMPLATES = HERE / "templates"

# Enough to be a program rather than a one-line handler or a JSON blob.
SUBSTANTIAL = 400


def without_jinja(body: str) -> str:
    """The script as the browser will see it, roughly.

    A template expression is not JavaScript and never reaches the browser as
    itself, so it stands in as a literal; a template statement drops out. What
    is left is what the page will actually run, which is what has to parse.
    """
    body = re.sub(r"\{\{.*?\}\}", "0", body, flags=re.S)
    return re.sub(r"\{%.*?%\}", "", body, flags=re.S)


def blocks(text: str):
    """Every inline script in the page, in order, with its line number."""
    for match in re.finditer(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", text, re.S):
        body = match.group(1)
        if len(body.strip()) < SUBSTANTIAL:
            continue
        yield text[: match.start()].count("\n") + 1, without_jinja(body)


def check(path: Path) -> list:
    text = path.read_text(encoding="utf-8")
    problems = []
    for line, body in blocks(text):
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
            handle.write(body)
            temporary = handle.name
        try:
            done = subprocess.run(["node", "--check", temporary], capture_output=True, text=True)
        finally:
            Path(temporary).unlink(missing_ok=True)
        if done.returncode:
            first = (done.stderr.strip().splitlines() or ["node said no more"])[:4]
            problems.append("%s, script starting at line %d:\n    %s" % (path.name, line, "\n    ".join(first)))
    return problems


def main() -> int:
    if shutil.which("node") is None:
        print("node is not installed here, so the templates were not parsed")
        return 0

    problems = []
    for path in sorted(TEMPLATES.glob("*.html")):
        problems.extend(check(path))

    if problems:
        print("An inline script does not parse:\n")
        for problem in problems:
            print("  " + problem)
        return 1

    print("every inline script parses")
    return 0


if __name__ == "__main__":
    sys.exit(main())
