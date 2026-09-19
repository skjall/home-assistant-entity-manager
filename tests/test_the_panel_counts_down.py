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
    assert "jobPanel.showLog = !jobPanel.showLog; cancelClosing()" in markup


def test_a_log_that_is_already_open_never_starts_a_count(markup):
    """The log was opened while the run was still going: it stays open."""
    start = markup.index("startClosing(finished) {")
    body = markup[start : markup.index("tickClosing(finished) {", start)]
    assert "if (this.jobPanel.showLog) return;" in body


def test_filtering_the_log_keeps_the_panel(markup):
    start = markup.index("toggleJobLogFilter(step) {")
    assert "this.cancelClosing();" in markup[start : start + 300]


def test_the_pointer_and_the_focus_hold_the_count(markup):
    assert '@mouseenter="pauseClosing()"' in markup
    assert '@mouseleave="resumeClosing()"' in markup
    assert '@focusin="pauseClosing()"' in markup
    assert '@focusout="resumeClosing()"' in markup


def test_acting_on_the_panel_ends_the_count_for_good(markup):
    assert '@mousedown="cancelClosing()"' in markup
    assert '@keydown="cancelClosing()"' in markup
    start = markup.index("resumeClosing() {")
    body = markup[start : markup.index("cancelClosing() {", start)]
    assert "if (this.jobPanel.closingCancelled) return;" in body


def test_a_paused_count_keeps_its_seconds(markup):
    """Pausing clears the interval but leaves the number on the button."""
    start = markup.index("pauseClosing() {")
    body = markup[start : markup.index("resumeClosing() {", start)]
    assert "this.jobPanel.closing = null" not in body


def test_a_new_run_is_not_bound_by_the_last_cancellation(markup):
    start = markup.index("startJob(pollUrl, job,")
    body = markup[start : markup.index("async pollJob() {", start)]
    assert "this.jobPanel.closingCancelled = false;" in body


def test_closing_the_panel_clears_the_timer(markup):
    """An interval left running would count down over the next run."""
    closing = markup.index("closeJobPanel() {")
    assert "this.stopClosing();" in markup[closing : closing + 400]


def test_the_count_starts_before_the_entities_are_reloaded(markup):
    """Otherwise the button sits there saying "Schließen" through the reload.

    The panel reloads the list when a run ends, which takes as long as it
    takes. A count that starts after it reads as a second wait tacked on to a
    panel that already said it was done.
    """
    body = markup[markup.index("async finishJob() {") : markup.index("stopJobPolling() {")]
    assert body.index("this.startClosing(finished);") < body.index("if (cb) await cb(finished);")


def test_the_countdown_is_dropped_when_another_job_takes_the_panel(markup):
    start = markup.index("tickClosing(finished) {")
    body = markup[start : markup.index("pauseClosing() {", start)]
    assert "this.jobPanel.job !== finished" in body
