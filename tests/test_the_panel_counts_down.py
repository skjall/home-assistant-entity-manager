"""A panel that closes itself says how long is left.

Auto-close used to take the panel away 1200ms after a flawless run, with no
warning and no way to keep it. The close button now counts down - "Schließen
(3)", "(2)" - so the run stays readable for as long as it takes to reach for
it, and opening the log keeps it open for good.
"""

import os

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="module")
def markup():
    with open(os.path.join(HERE, "templates", "index.html"), encoding="utf-8") as handle:
        return handle.read()


def test_the_button_shows_the_seconds_left(markup):
    assert "t('jobs.close') + (jobPanel.closing ? ' (' + jobPanel.closing + ')' : '')" in markup


def test_it_starts_at_three(markup):
    assert "this.jobPanel.closing = 3;" in markup


def test_a_flawless_run_starts_the_countdown_instead_of_closing_outright(markup):
    assert "this.startClosing(finished);" in markup
    # The old way, with nothing on screen to say it was about to happen.
    assert "if (this.jobPanel.job === finished) this.closeJobPanel();" not in markup


def test_opening_the_log_keeps_the_panel(markup):
    assert "jobPanel.showLog = !jobPanel.showLog; stopClosing()" in markup


def test_closing_the_panel_clears_the_timer(markup):
    """An interval left running would count down over the next run."""
    closing = markup.index("closeJobPanel() {")
    assert "this.stopClosing();" in markup[closing : closing + 400]


def test_the_countdown_is_dropped_when_another_job_takes_the_panel(markup):
    start = markup.index("startClosing(finished) {")
    body = markup[start : markup.index("stopClosing() {", start)]
    assert "this.jobPanel.job !== finished" in body
