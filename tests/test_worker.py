"""The worker restart loop.

Every measurement source runs inside this loop, so a mistake here is a mistake
in all of them at once — and it shows up as missing data rather than as an
error, which is the hardest kind to notice.
"""

from __future__ import annotations

import time

from viabot_survey.workers.base import Worker


class Blocky(Worker):
    """Stands in for the uplink load test, which finishes a block and returns.

    Timings are scaled down so the test is quick; the shape is what matters.
    """

    name = "blocky"
    healthy_run_s = 0.05
    restart_delay_s = 0.02
    max_restart_delay_s = 0.5

    def __init__(self, block_s: float) -> None:
        super().__init__()
        self.block_s = block_s
        self.starts: list[float] = []
        self.events: list[tuple[str, str]] = []
        self._on_event = lambda level, message: self.events.append((level, message))

    def run_once(self) -> None:
        self.starts.append(time.monotonic())
        time.sleep(self.block_s)


def drive(block_s: float, seconds: float = 0.6) -> Blocky:
    worker = Blocky(block_s)
    worker.start()
    time.sleep(seconds)
    worker.stop()
    return worker


def test_work_that_finishes_normally_does_not_get_backed_off():
    """The uplink load test returns after every 30-second block by design.
    Treating that as a failure doubled the gap each time until it reached a
    minute, so two thirds of a walk would have had no uplink reading — and it
    would have looked like coverage gaps, not like a scheduling artefact."""
    worker = drive(0.08)
    gaps = [b - a - 0.08 for a, b in zip(worker.starts, worker.starts[1:])]
    assert len(worker.starts) >= 4
    # Every gap stays at the base delay instead of doubling away.
    assert max(gaps) < worker.restart_delay_s * 3


def test_something_failing_immediately_still_backs_off():
    """The backoff is there for a reason — a worker that cannot start must not
    spin. Only a run that lasted long enough to have done its job resets it."""
    worker = drive(0.0)
    gaps = [b - a for a, b in zip(worker.starts, worker.starts[1:])]
    assert len(gaps) >= 3
    assert gaps[-1] > gaps[0] * 1.5


def test_finishing_a_block_is_not_announced_as_a_problem():
    """Twice a minute for the length of a walk would bury every real warning
    in the operator's event list."""
    assert not drive(0.08).events


def test_an_immediate_exit_is_still_announced():
    assert any("restarting" in message for _, message in drive(0.0).events)


class SlowToDie(Worker):
    """A worker whose current job takes a moment to notice it was stopped,
    the way a subprocess takes a moment to die on a loaded Pi."""

    name = "slow"
    restart_delay_s = 0.01

    def __init__(self, dying_s: float) -> None:
        super().__init__()
        self.dying_s = dying_s
        self.runs = 0

    def run_once(self) -> None:
        self.runs += 1
        self._stop.wait(30)
        time.sleep(self.dying_s)


def test_a_worker_restarted_while_the_old_one_is_still_dying_does_restart():
    """stop() gives up waiting after a timeout. Starting again used to see the
    old thread still alive, return without doing anything, and leave the stop
    flag set, so the old thread then exited and nothing replaced it. A Resume
    pressed soon after a Pause could leave the camera or the load test off for
    the rest of the walk, with nothing said."""
    worker = SlowToDie(dying_s=0.6)
    worker.start()
    time.sleep(0.1)
    worker.stop(timeout=0.05)              # gives up before the thread is gone
    assert worker._thread.is_alive()

    worker.start()                         # Resume
    time.sleep(0.2)
    assert worker._thread.is_alive()
    assert not worker.stopping
    assert worker.runs == 2
    worker.stop()


def test_starting_a_worker_that_is_already_running_changes_nothing():
    worker = SlowToDie(dying_s=0.0)
    worker.start()
    time.sleep(0.05)
    thread = worker._thread
    worker.start()
    assert worker._thread is thread
    assert worker.runs == 1
    worker.stop()
