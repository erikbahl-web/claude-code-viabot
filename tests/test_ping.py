import pytest
import time

from viabot_survey.workers.ping import PingWorker


def make_worker(**kwargs):
    kwargs.setdefault("target", "8.8.8.8")
    kwargs.setdefault("window_s", 10)
    return PingWorker(**kwargs)


def test_command_pins_the_uplink_interface():
    cmd = make_worker(interface="eth0").build_command()
    assert cmd[0] == "ping"
    # -O is what gives us per-packet loss in real time rather than at the end.
    assert "-O" in cmd and "-D" in cmd and "-n" in cmd
    assert cmd[cmd.index("-I") + 1] == "eth0"
    assert cmd[-1] == "8.8.8.8"


def test_command_omits_interface_when_unset():
    assert "-I" not in make_worker(interface=None).build_command()


def test_parses_replies_and_computes_stats():
    worker = make_worker()
    now = time.time()
    for i in range(5):
        worker.handle_line(
            f"[{now + i:.6f}] 64 bytes from 8.8.8.8: icmp_seq={i} ttl=118 time={40 + i * 2}.0 ms")
    snapshot = worker.snapshot(now=now + 5)
    assert snapshot["sent"] == 5
    assert snapshot["received"] == 5
    assert snapshot["loss_pct"] == 0.0
    assert snapshot["rtt_ms"] == 48.0
    assert snapshot["avg_rtt_ms"] == 44.0
    assert snapshot["jitter_ms"] == 2.0


def test_counts_unanswered_packets_as_loss():
    worker = make_worker()
    now = time.time()
    worker.handle_line(f"[{now:.6f}] 64 bytes from 8.8.8.8: icmp_seq=1 ttl=118 time=40.0 ms")
    worker.handle_line(f"[{now + 1:.6f}] no answer yet for icmp_seq=2")
    worker.handle_line(f"[{now + 2:.6f}] no answer yet for icmp_seq=3")
    worker.handle_line(f"[{now + 3:.6f}] 64 bytes from 8.8.8.8: icmp_seq=4 ttl=118 time=44.0 ms")
    snapshot = worker.snapshot(now=now + 3)
    assert snapshot["sent"] == 4
    assert snapshot["received"] == 2
    assert snapshot["loss_pct"] == 50.0


def test_late_reply_supersedes_the_no_answer_line():
    """ping prints 'no answer yet' at timeout and then the reply if it arrives.
    Counting both would invent packet loss that never happened."""
    worker = make_worker()
    now = time.time()
    worker.handle_line(f"[{now:.6f}] no answer yet for icmp_seq=7")
    worker.handle_line(f"[{now + 1:.6f}] 64 bytes from 8.8.8.8: icmp_seq=7 ttl=118 time=900.0 ms")
    snapshot = worker.snapshot(now=now + 1)
    assert snapshot["sent"] == 1
    assert snapshot["received"] == 1
    assert snapshot["loss_pct"] == 0.0


def test_unreachable_after_a_reply_is_not_double_counted():
    worker = make_worker()
    now = time.time()
    worker.handle_line(f"[{now:.6f}] 64 bytes from 8.8.8.8: icmp_seq=3 ttl=118 time=40.0 ms")
    worker.handle_line(f"[{now + 1:.6f}] From 192.168.1.1 icmp_seq=3 Destination Host Unreachable")
    snapshot = worker.snapshot(now=now + 1)
    assert snapshot["received"] == 1
    assert snapshot["loss_pct"] == 0.0


def test_total_silence_reports_full_loss_not_a_stale_rtt():
    """Walking into a dead zone means ping simply stops printing. The dashboard
    must not keep showing the last good RTT."""
    worker = make_worker(interval_s=1.0)
    now = time.time()
    worker.handle_line(f"[{now:.6f}] 64 bytes from 8.8.8.8: icmp_seq=1 ttl=118 time=40.0 ms")
    snapshot = worker.snapshot(now=now + 30)
    assert snapshot["rtt_ms"] is None
    assert snapshot["loss_pct"] == 100.0
    assert snapshot["silent_for_s"] == 30.0


def test_empty_window_reports_unknown_rather_than_zero():
    assert make_worker().snapshot(now=time.time())["loss_pct"] is None


def test_fatal_resolver_error_marks_the_worker_failed():
    worker = make_worker(target="nope.invalid")
    worker.handle_line("ping: nope.invalid: Name or service not known")
    assert worker.state == "failed"
    assert "Name or service not known" in worker.error


def test_old_results_are_pruned():
    worker = make_worker(window_s=5)
    now = time.time()
    for i in range(300):
        worker.handle_line(
            f"[{now + i:.6f}] 64 bytes from 8.8.8.8: icmp_seq={i} ttl=118 time=40.0 ms")
    # Retention is a multiple of the window, so memory cannot grow without bound.
    assert len(worker._results) <= 70


def test_the_link_chip_clears_once_replies_come_back():
    """A dead zone makes ping go quiet, which is correct. What is not correct is
    staying that way: the operator walks out of the dead zone, the link is fine,
    and the phone still says there is a link problem for the rest of the walk.

    Nothing else can clear it — the base class sets RUNNING once before
    run_once(), and ping's run_once() streams for the life of the worker.
    """
    from viabot_survey.workers.base import STATE_DEGRADED, STATE_RUNNING

    worker = PingWorker(target="8.8.8.8", interval_s=1.0, window_s=10.0)
    now = 1_000_000.0

    # A reply, then silence long enough to count as a dead link.
    worker._results[1] = {"ts": now, "rtt": 45.0}
    worker._last_reply_ts = now
    worker.snapshot(now + 30)
    assert worker.state == STATE_DEGRADED

    # Out of the dead zone: replies resume.
    worker._results[2] = {"ts": now + 31, "rtt": 48.0}
    worker._last_reply_ts = now + 31
    worker.snapshot(now + 32)
    assert worker.state == STATE_RUNNING


def test_a_worker_that_has_never_had_a_reply_is_not_called_healthy():
    """Clearing on 'not currently silent' would report a link that has never
    worked as fine, because there is no last reply to be silent since."""
    from viabot_survey.workers.base import STATE_DEGRADED

    worker = PingWorker(target="8.8.8.8", interval_s=1.0)
    worker._set_state(STATE_DEGRADED, "never got a reply")
    worker.snapshot(1_000_000.0)
    assert worker.state == STATE_DEGRADED


# ---- leaving out pings that queued behind the rig's own uplink test ---------
#
# Ping is averaged over ten seconds, and the gap between uplink bursts is
# about that long. Read whole, the window held pings that had sat in the
# modem's queue behind the last burst for the entire gap, and on 2026-11-24
# the phone showed bad reception in a spot known to be good for nearly the
# whole of an office test.

def _feed(worker, now, lines):
    for offset, body in lines:
        worker.handle_line(f"[{now + offset:.6f}] {body}")


def test_pings_sent_before_the_test_let_go_are_left_out():
    worker = make_worker(window_s=10)
    now = time.time()
    _feed(worker, now, [
        # Sent during the burst: queued, 1.4 s round trips, two lost.
        (1.4, "64 bytes from 8.8.8.8: icmp_seq=1 ttl=117 time=1400 ms"),
        (2.0, "no answer yet for icmp_seq=2"),
        (3.0, "no answer yet for icmp_seq=3"),
        # Sent after it let go at now + 4: a clean link.
        (5.05, "64 bytes from 8.8.8.8: icmp_seq=5 ttl=117 time=45.0 ms"),
        (6.05, "64 bytes from 8.8.8.8: icmp_seq=6 ttl=117 time=47.0 ms"),
    ])
    whole = worker.snapshot(now=now + 6.5)
    clean = worker.snapshot(now=now + 6.5, since=now + 4)
    assert whole["loss_pct"] == 40.0
    assert clean["sent"] == 2
    assert clean["loss_pct"] == 0.0
    assert clean["rtt_ms"] == 47.0


def test_a_ping_is_dated_by_when_it_was_sent_not_when_it_came_back():
    """A reply that left during the burst and arrived after it was queued
    behind the test, however late it lands."""
    worker = make_worker(window_s=10)
    now = time.time()
    _feed(worker, now, [(4.5, "64 bytes from 8.8.8.8: icmp_seq=1 ttl=117 time=1500 ms")])
    assert worker.snapshot(now=now + 5, since=now + 4)["sent"] == 0
    assert worker.snapshot(now=now + 5, since=now + 2.9)["sent"] == 1


def test_a_late_reply_replaces_its_no_answer_and_its_send_time():
    worker = make_worker(window_s=10)
    now = time.time()
    _feed(worker, now, [
        (1.0, "no answer yet for icmp_seq=1"),
        (1.2, "64 bytes from 8.8.8.8: icmp_seq=1 ttl=117 time=1200 ms"),
    ])
    snapshot = worker.snapshot(now=now + 2)
    assert snapshot["loss_pct"] == 0.0
    assert worker._results[1]["sent"] == pytest.approx(now, abs=0.01)
