"""Run the test suite with an interpreter that actually has pytest.

The hook used to call `pytest` by name. pre-commit runs a system hook with the
PATH of the shell that started git, and the documented install puts pytest in
.venv - so unless that venv happened to be active, the hook died with
"Executable `pytest` not found" and every commit had to be forced through.

So the interpreter is looked up rather than assumed: the project's own venv
first, then the one running this script.
"""

from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent


def roots():
    """Where a venv for this project could be: here, and the main checkout.

    A worktree has no venv of its own, so the one in the checkout it was cut
    from counts too - otherwise the hook fails on every branch worked on that
    way.
    """
    yield ROOT
    common = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True,
        cwd=ROOT,
        text=True,
    )
    if common.returncode == 0:
        main_checkout = Path(common.stdout.strip()).parent
        if main_checkout != ROOT:
            yield main_checkout


def interpreters():
    """The interpreters to try, in the order they are worth trying."""
    for root in roots():
        for venv in (".venv", "venv"):
            candidate = root / venv / "bin" / "python"
            if candidate.exists():
                yield candidate
    yield Path(sys.executable)


def has_pytest(python: Path) -> bool:
    return (
        subprocess.run(
            [str(python), "-c", "import pytest"],
            capture_output=True,
        ).returncode
        == 0
    )


def main() -> int:
    for python in interpreters():
        if has_pytest(python):
            return subprocess.run([str(python), "-m", "pytest", "tests/"], cwd=ROOT).returncode

    print(
        "pytest is not installed in any interpreter this hook can reach.\n"
        "  python3 -m venv .venv\n"
        "  .venv/bin/pip install -r requirements.txt -r requirements-dev.txt -r requirements-test.txt",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
