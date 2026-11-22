"""The server's self-heal for a wedged iperf3.

The numbers these tests use are the ones measured on 2026-11-22 against iperf3
3.16: a healthy server's main thread never above 2% of a core, whatever it was
doing, and a wedged one at 99%.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

import viabot_iperf_heal as heal  # noqa: E402

TPS = 100                      # clock ticks a second, as on every Linux box here
T0 = 1_800_000_000.0


def _feed(watch, share, seconds, start=T0, pid=4242, ticks=0):
    """Give the watch readings every POLL_S at a steady CPU share."""
    verdicts = []
    now = start
    watch.observe(pid, ticks, now)
    for _ in range(int(seconds / heal.POLL_S)):
        now += heal.POLL_S
        ticks += int(share * TPS * heal.POLL_S)
        verdicts.append(watch.observe(pid, ticks, now))
    return verdicts, now, ticks


def test_a_wedged_server_is_restarted_after_half_a_minute():
    verdicts, _, _ = _feed(heal.Watch(TPS), share=0.994, seconds=40)
    assert verdicts.index(True) == int(heal.PINNED_FOR_S / heal.POLL_S) - 1


@pytest.mark.parametrize("share", [0.0, 0.006, 0.02])
def test_a_working_server_is_never_touched(share):
    """Idle, receiving a 3 Mbit/s uplink test, and sending a 30 Mbit/s
    downlink: the highest the main thread was ever measured at is 2%."""
    verdicts, _, _ = _feed(heal.Watch(TPS), share=share, seconds=3600)
    assert not any(verdicts)


def test_a_wedge_is_still_caught_on_a_box_that_is_busy_with_something_else():
    """One core, and the receiver taking an upload at the same moment: the
    wedged thread no longer gets the whole core, and must still be caught."""
    verdicts, _, _ = _feed(heal.Watch(TPS), share=0.6, seconds=40)
    assert any(verdicts)


def test_a_burst_of_work_that_passes_is_not_a_wedge():
    watch = heal.Watch(TPS)
    verdicts, now, ticks = _feed(watch, share=0.99, seconds=20)
    more, _, _ = _feed(watch, share=0.01, seconds=60, start=now, ticks=ticks)
    assert not any(verdicts + more)
    assert watch.pinned_since is None


def test_a_restarted_server_starts_with_a_clean_slate():
    """The new process has a new pid and its counter starts from nothing.
    Comparing it with the old one would read as a burst of negative work or,
    worse, as a full core."""
    watch = heal.Watch(TPS)
    _, now, _ = _feed(watch, share=0.99, seconds=25)
    assert watch.observe(5555, 3, now + heal.POLL_S) is False
    assert watch.pinned_since is None
    verdicts, _, _ = _feed(watch, share=0.0, seconds=60, start=now + heal.POLL_S,
                           pid=5555, ticks=3)
    assert not any(verdicts)


def test_a_server_that_is_not_running_is_left_to_systemd():
    watch = heal.Watch(TPS)
    assert watch.observe(None, None, T0) is False
    assert watch.observe(4242, None, T0 + 5) is False       # exited between reads


def test_it_does_not_restart_the_same_server_twice_in_a_row():
    """If a restart does not cure it, restarting every thirty seconds will
    not either, and it would bury whatever the real cause is."""
    watch = heal.Watch(TPS)
    verdicts, now, ticks = _feed(watch, share=0.99, seconds=40)
    assert any(verdicts)
    watch.restarted(now)
    again, _, _ = _feed(watch, share=0.99, seconds=60, start=now, pid=4243)
    assert not any(again)
    later, _, _ = _feed(watch, share=0.99, seconds=200, start=now + 60, pid=4243)
    assert any(later)


def test_only_the_main_threads_time_is_read(tmp_path, monkeypatch):
    """A server sending the downlink stream burns a whole core in a worker
    thread for the length of every walk. Reading the process's total would
    restart it mid-test, every time."""
    real_open = open

    def fake_open(path, *args, **kwargs):
        if path == "/proc/4242/task/4242/stat":
            stat = tmp_path / "stat"
            stat.write_text("4242 (iperf3 (x) y) S 1 4242 4242 0 -1 4194304 "
                            "100 0 0 0 7 5 0 0 20 0 2 0 123456 1 1 1\n")
            return real_open(stat, *args, **kwargs)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", fake_open)
    assert heal.main_thread_ticks(4242) == 12                # utime 7 + stime 5
    assert heal.main_thread_ticks(999_999_999) is None


def test_the_iperf3_version_is_read_from_its_banner():
    assert heal.parse_version("iperf 3.16 (cJSON 1.7.15)\nLinux ...") == (3, 16)
    assert heal.parse_version("iperf 3.9 (cJSON 1.7.13)") == (3, 9)
    assert heal.parse_version("command not found") is None
    # Before 3.16 the main thread does the sending, so nothing can be told.
    assert (3, 9) < heal.FIRST_THREADED <= (3, 16)


def _midnight(hour):
    return time.mktime(time.strptime(f"2026-11-24 {hour:02d}:30", "%Y-%m-%d %H:%M")) - time.timezone


def test_the_nightly_restart_happens_once_a_night():
    at_nine = _midnight(9)
    today = time.gmtime(at_nine).tm_yday
    assert heal.nightly_due(at_nine, None, 9) is True
    assert heal.nightly_due(at_nine, today, 9) is False       # already done
    assert heal.nightly_due(_midnight(14), None, 9) is False  # not the hour
    assert heal.nightly_due(at_nine, None, -1) is False       # switched off


def test_the_loop_restarts_only_the_instance_that_is_wedged():
    """Uplink wedged, downlink healthy and sending: one restart, of one unit."""
    clock = {"now": _midnight(14)}
    ticks = {"viabot-iperf3@5201": 0, "viabot-iperf3@5202": 0}
    pids = {"viabot-iperf3@5201": 100, "viabot-iperf3@5202": 200}
    by_pid = {100: "viabot-iperf3@5201", 200: "viabot-iperf3@5202"}
    restarted = []

    def sleep(seconds):
        clock["now"] += seconds
        ticks["viabot-iperf3@5201"] += int(0.99 * TPS * seconds)
        ticks["viabot-iperf3@5202"] += int(0.02 * TPS * seconds)

    heal.run([5201, 5202], get_pid=pids.get,
             get_ticks=lambda pid: ticks[by_pid[pid]],
             restart=lambda unit: restarted.append(unit) or True,
             clock=lambda: clock["now"], sleep=sleep, cycles=12)
    assert restarted == ["viabot-iperf3@5201"]


def test_starting_inside_the_nightly_hour_does_not_restart_working_servers():
    """A deploy at ten past nine must not restart servers somebody has just
    checked are working."""
    clock = {"now": _midnight(9)}
    restarted = []
    heal.run([5201], nightly_hour=9, get_pid=lambda unit: 100,
             get_ticks=lambda pid: 0,
             restart=lambda unit: restarted.append(unit) or True,
             clock=lambda: clock["now"],
             sleep=lambda s: clock.__setitem__("now", clock["now"] + s), cycles=4)
    assert restarted == []


def test_started_before_the_hour_it_still_runs_that_night():
    clock = {"now": _midnight(8) + 25 * 60}                   # 08:55 UTC
    restarted = []
    heal.run([5201, 5202], nightly_hour=9, get_pid=lambda unit: 100,
             get_ticks=lambda pid: 0,
             restart=lambda unit: restarted.append(unit) or True,
             clock=lambda: clock["now"],
             sleep=lambda s: clock.__setitem__("now", clock["now"] + s),
             cycles=int(900 / heal.POLL_S))
    assert restarted == ["viabot-iperf3@5201", "viabot-iperf3@5202"]
