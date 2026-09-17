"""What a commit may say, and where it may land.

Versions and the changelog are built from the commit messages on main, and that
changelog is what Home Assistant shows before an update. A release's worth of
work once arrived there as a single line, because the commits it was squashed
from said nothing the release notes could read. These two checks refuse that at
the point it happens rather than after the release.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import check_branch_name  # noqa: E402
import check_commit_message  # noqa: E402


@pytest.mark.parametrize(
    "subject",
    [
        "feat: say which entities a rule reaches",
        "fix(swap): carry the area over to the new device",
        "chore: update alpinejs",
        "feat!: rename every entity on first run",
        "deps: bump swagger-ui-dist",
        "refactor(naming, rules): one place to ask which rule applies",
    ],
)
def test_a_message_the_release_notes_can_read_passes(subject):
    assert check_commit_message.complaint(subject) == ""


@pytest.mark.parametrize(
    "subject",
    [
        "Count a rule's reach the way the naming decides it",
        "update the changelog",
        "WIP",
        "feat something",
        "feat:no space after the colon is not a subject",
        "",
    ],
)
def test_a_message_without_a_type_is_refused(subject):
    assert check_commit_message.complaint(subject)


def test_git_writes_its_own_messages_and_they_stand():
    """A merge or a revert is git's wording, not the author's."""
    assert check_commit_message.complaint("Merge branch 'main' into feat/rule-filters") == ""
    assert check_commit_message.complaint('Revert "feat: say which entities a rule reaches"') == ""
    assert check_commit_message.complaint("fixup! feat: say which entities a rule reaches") == ""


def test_a_subject_that_runs_on_is_refused():
    assert check_commit_message.complaint("feat: " + "a" * 200)


def test_the_message_file_is_read_without_its_comments(tmp_path):
    written = tmp_path / "COMMIT_EDITMSG"
    written.write_text("# Please enter the commit message\nfeat: say it\n", encoding="utf-8")
    sys.argv = ["check_commit_message.py", str(written)]

    assert check_commit_message.main() == 0


def test_a_message_file_holding_nothing_but_comments_is_refused(tmp_path):
    written = tmp_path / "COMMIT_EDITMSG"
    written.write_text("# Please enter the commit message\n#\n", encoding="utf-8")
    sys.argv = ["check_commit_message.py", str(written)]

    assert check_commit_message.main() == 1


@pytest.mark.parametrize(
    "branch",
    ["feat/rule-filters", "fix/device-swap", "chore/remove-stale-bot", "ci/build-the-assets", "deps/alpinejs-3-17"],
)
def test_a_branch_named_after_its_work_passes(branch):
    assert check_branch_name.complaint(branch) == ""


@pytest.mark.parametrize("branch", ["main", "master"])
def test_the_trunk_is_not_a_branch_to_commit_on(branch):
    assert check_branch_name.complaint(branch)


def test_the_deploy_branch_is_not_one_to_commit_on():
    """Work has landed on it twice and had to be picked back out."""
    assert check_branch_name.complaint("tmp/deploy-all")


@pytest.mark.parametrize("branch", ["Feat/Rule-Filters", "rule-filters", "feature/rules", "feat/", "feat/rules_here"])
def test_a_branch_that_says_nothing_of_the_kind_is_refused(branch):
    assert check_branch_name.complaint(branch)


def test_release_please_commits_to_its_own_branch():
    assert check_branch_name.complaint("release-please--branches--main") == ""


def test_a_detached_head_is_left_alone():
    """Mid-rebase or mid-bisect, git is the one doing the committing."""
    assert check_branch_name.complaint("HEAD") == ""
    assert check_branch_name.complaint("") == ""
