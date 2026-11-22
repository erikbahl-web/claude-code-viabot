"""The UDP load test, in both directions.

The interval lines in here are verbatim iperf3 output captured from a real
client-server pair, not written by hand. Output formats are the thing a parser
gets quietly wrong, and a fixture invented alongside the regex only proves the
two agree with each other.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import time

import pytest

from viabot_survey.workers import udpload
from viabot_survey.workers.udpload import (DOWNLINK, UPLINK, UdpLoadWorker,
                                           parse_interval, parse_server_output)

# Real iperf3 3.16 output, UDP, 1200-byte datagrams.
RECEIVER_LINE = "[  5]   0.00-1.00   sec   123 KBytes  1.01 Mbits/sec  0.062 ms  0/105 (0%)  "
LOSSY_LINE = "[  5]   1.00-2.00   sec   122 KBytes   998 Kbits/sec  0.027 ms  3/104 (2.9%)  "
SENDER_LINE = "[  5]   0.00-1.00   sec   123 KBytes  1.01 Mbits/sec  105  "
SUMMARY = "[  5]   0.00-6.00   sec   732 KBytes  1.00 Mbits/sec  0.030 ms  0/625 (0%)  receiver"
HEADER = "[ ID] Interval           Transfer     Bitrate         Jitter    Lost/Total Datagrams"

# What --get-server-output relays back after an uplink block: the far end's own
# per-second view, which is the only place uplink loss exists.
SERVER_OUTPUT = """Connecting to host 10.0.0.1, port 5201
[  5]   0.00-1.00   sec   123 KBytes  1.01 Mbits/sec  105
[  5]   1.00-2.00   sec   122 KBytes   998 Kbits/sec  104

Server output:
Accepted connection from 203.0.113.7, port 40040
[  5] local 10.0.0.1 port 5201 connected to 203.0.113.7 port 44991
[ ID] Interval           Transfer     Bitrate         Jitter    Lost/Total Datagrams
[  5]   0.00-1.00   sec   123 KBytes  1.01 Mbits/sec  0.075 ms  0/105 (0%)
[  5]   1.00-2.00   sec   100 KBytes   820 Kbits/sec  8.244 ms  19/104 (18%)
- - - - - - - - - - - - - - - - - - - - - - - - -
[ ID] Interval           Transfer     Bitrate         Jitter    Lost/Total Datagrams
[  5]   0.00-2.00   sec   223 KBytes   915 Kbits/sec  4.018 ms  19/209 (9.1%)  receiver
"""


# ---- parsing ---------------------------------------------------------------

def test_a_receiving_line_carries_jitter_and_loss():
    reading = parse_interval(RECEIVER_LINE)
    assert reading["jitter_ms"] == 0.062
    assert reading["loss_pct"] == 0.0
    assert round(reading["mbps"], 2) == 1.01


def test_a_sending_line_carries_neither():
    """This is the whole reason uplink is measured differently: the end doing
    the sending cannot see what arrived."""
    reading = parse_interval(SENDER_LINE)
    assert reading["jitter_ms"] is None
    assert reading["loss_pct"] is None
    assert reading["total"] == 105


def test_kilobit_rates_are_scaled_like_megabit_ones():
    """iperf3 switches units on its own once a link degrades, and a bad garage
    is exactly where it starts printing Kbits/sec."""
    assert round(parse_interval(LOSSY_LINE)["mbps"], 3) == 0.998
    assert parse_interval(LOSSY_LINE)["loss_pct"] == 2.9


def test_summary_lines_are_not_mistaken_for_readings():
    """They have the identical shape and cover the whole test, so counting one
    would put a run-length average onto a single second."""
    assert parse_interval(SUMMARY) is None
    assert parse_interval(HEADER) is None
    assert parse_interval("iperf Done.") is None
    assert parse_interval("") is None


def test_the_far_ends_readings_are_recovered_from_a_block():
    readings = parse_server_output(SERVER_OUTPUT)
    assert len(readings) == 2
    assert readings[1]["loss_pct"] == 18.0
    assert readings[1]["jitter_ms"] == 8.244


def test_the_rigs_own_lines_are_not_counted_as_the_far_ends():
    """Both appear in the same captured output. Reading the rig's own would
    report what it sent as though it had arrived — which is exactly the error
    that makes a garage look better than it is."""
    readings = parse_server_output(SERVER_OUTPUT)
    assert all(r["loss_pct"] is not None for r in readings)
    assert parse_server_output("no server section here") == []


# ---- the commands ----------------------------------------------------------

def test_uplink_runs_in_blocks_and_asks_what_arrived():
    argv = UdpLoadWorker("10.0.0.1", UPLINK, block_s=30).build_command()
    assert "-u" in argv and "-R" not in argv          # the rig sends
    assert "--get-server-output" in argv              # ...so ask the far end
    assert argv[argv.index("-t") + 1] == "30"


def test_downlink_streams_for_the_whole_walk():
    argv = UdpLoadWorker("10.0.0.1", DOWNLINK).build_command()
    assert "-R" in argv                               # the server sends
    assert argv[argv.index("-t") + 1] == "0"          # no end
    assert "--get-server-output" not in argv          # the rig can see it itself


def test_both_directions_ask_for_rtp_sized_datagrams():
    """iperf3 defaults to 32 KB datagrams, which IP fragments into two dozen
    packets. Losing any one marks the whole datagram lost, so loss reads several
    times worse than a real video packet would see."""
    for direction in (UPLINK, DOWNLINK):
        argv = UdpLoadWorker("10.0.0.1", direction).build_command()
        assert argv[argv.index("-l") + 1] == "1200"
        assert "--forceflush" in argv


def test_the_two_directions_take_different_ports():
    """One iperf3 server runs one test at a time, so they cannot share one."""
    up = UdpLoadWorker("10.0.0.1", UPLINK, port=5201)
    down = UdpLoadWorker("10.0.0.1", DOWNLINK, port=5202)
    assert up.name != down.name
    assert up.build_command()[4] != down.build_command()[4]


# ---- authentication --------------------------------------------------------

def test_the_password_never_reaches_the_command_line():
    """A walk runs this for half an hour; argv is readable from ps throughout."""
    worker = UdpLoadWorker("10.0.0.1", UPLINK, username="rig", password="hunter2",
                           public_key_path="/etc/viabot/iperf.pub")
    assert not any("hunter2" in arg for arg in worker.build_command())
    assert worker.build_env()["IPERF3_PASSWORD"] == "hunter2"


def test_credentials_are_only_sent_when_both_halves_are_configured():
    argv = UdpLoadWorker("10.0.0.1", UPLINK, username="rig").build_command()
    assert "--username" not in argv
    assert UdpLoadWorker("10.0.0.1", UPLINK, username="rig").build_env() is None


# ---- the data ceiling ------------------------------------------------------

def test_the_run_budget_is_a_hard_stop():
    worker = UdpLoadWorker("10.0.0.1", UPLINK, run_data_budget_mb=1)
    assert not worker.budget_spent
    for _ in range(10):
        worker._consume("[  5] 0.00-1.00 sec 200 KBytes 1.64 Mbits/sec 170")
    assert worker.budget_spent


def test_each_walk_gets_a_fresh_allowance():
    worker = UdpLoadWorker("10.0.0.1", UPLINK, run_data_budget_mb=1)
    for _ in range(10):
        worker._consume("[  5] 0.00-1.00 sec 200 KBytes 1.64 Mbits/sec 170")
    worker.begin_run()
    assert not worker.budget_spent


def test_no_budget_means_no_ceiling():
    assert not UdpLoadWorker("10.0.0.1", UPLINK, run_data_budget_mb=None).budget_spent


# ---- what reaches a sample -------------------------------------------------

def test_downlink_readings_are_named_for_their_direction():
    worker = UdpLoadWorker("10.0.0.1", DOWNLINK)
    worker._latest = parse_interval(RECEIVER_LINE)
    worker._latest_ts = time.time()
    fields = worker.sample_fields()
    assert fields["udp_down_loss_pct"] == 0.0
    assert "udp_up_loss_pct" not in fields


def test_a_stale_reading_is_dropped_rather_than_repeated():
    """When the link dies badly the iperf3 control channel — which is TCP —
    dies with it and the stream stops. Carrying the last good number forward
    would paint the worst spot in the garage as the healthiest."""
    worker = UdpLoadWorker("10.0.0.1", DOWNLINK)
    worker._latest = parse_interval(RECEIVER_LINE)
    worker._latest_ts = time.time() - 30
    assert worker.sample_fields()["udp_down_loss_pct"] is None


def test_uplink_never_reports_through_the_live_path():
    """Its numbers do not exist at the rig until a block ends, so they are
    written onto their samples afterwards instead."""
    worker = UdpLoadWorker("10.0.0.1", UPLINK)
    worker._latest = parse_interval(RECEIVER_LINE)
    worker._latest_ts = time.time()
    assert worker.sample_fields() == {"udp_up_jitter_ms": None,
                                      "udp_up_loss_pct": None,
                                      "udp_up_mbps": None}


def test_a_finished_block_is_placed_on_the_seconds_it_covers():
    """The far end reports elapsed offsets, which know nothing about the time
    spent setting the test up. The wall clock recorded as each of the rig's own
    lines arrived is what puts the readings on the right samples."""
    collected = []
    worker = UdpLoadWorker("10.0.0.1", UPLINK, on_backfill=collected.extend)
    now = time.time()
    worker._absorb_server_output(SERVER_OUTPUT, started=now - 5,
                                 interval_ends=[now - 1.0, now])

    assert [round(ts - now, 1) for ts, _ in collected] == [-1.5, -0.5]
    assert collected[1][1]["udp_up_loss_pct"] == 18.0
    assert collected[1][1]["udp_up_jitter_ms"] == 8.244


def test_a_block_the_server_said_nothing_about_degrades_rather_than_invents():
    worker = UdpLoadWorker("10.0.0.1", UPLINK, on_backfill=lambda r: None)
    worker._absorb_server_output("no server section", started=time.time(),
                                 interval_ends=[])
    assert worker.state == "degraded"


def test_a_worker_with_no_server_configured_stays_off():
    assert not UdpLoadWorker(None, UPLINK).enabled


# ---- when it is allowed to run ---------------------------------------------
#
# The stream is a deliberate, continuous load on the uplink. Left running
# between walks it would burn the link for nothing, and fight the publisher for
# the same uplink while that is trying to send the last run's clips.

@pytest.fixture
def loaded_runner(config, storage):
    """A runner with both directions switched on, pointed at an address that
    does not answer. Whether iperf3 connects is beside the point here — the
    question is only whether the rig lets the workers run at all."""
    from viabot_survey.runner import SurveyRunner

    config._data["udp_load"]["enabled"] = True
    config._data["udp_load"]["server"] = "192.0.2.1"     # TEST-NET-1, unroutable
    survey = SurveyRunner(config, storage)
    survey.start()
    yield survey
    survey.shutdown()


def _streaming(worker) -> bool:
    thread = worker._thread
    return thread is not None and thread.is_alive()


def test_neither_direction_runs_between_walks(loaded_runner):
    assert all(w.enabled for w in loaded_runner.udp_load)
    assert not any(_streaming(w) for w in loaded_runner.udp_load)


def test_both_directions_run_for_the_length_of_a_walk(loaded_runner):
    loaded_runner.start_run(label="Level 2")
    assert all(_streaming(w) for w in loaded_runner.udp_load)

    loaded_runner.stop_run()
    assert not any(_streaming(w) for w in loaded_runner.udp_load)


def test_pausing_stops_the_load_too(loaded_runner):
    """Pause means this time did not happen. Streaming while the operator
    stands still spends the uplink on the one period it tells you nothing
    about, and leaves data on seconds the report says never happened."""
    loaded_runner.start_run(label="Level 2")
    loaded_runner.pause()
    assert not any(_streaming(w) for w in loaded_runner.udp_load)

    loaded_runner.resume()
    assert all(_streaming(w) for w in loaded_runner.udp_load)


def test_a_block_landing_after_a_pause_is_discarded(loaded_runner):
    """A block in flight when the operator pauses describes seconds the report
    says did not happen."""
    loaded_runner.start_run(label="Level 2")
    run_id = loaded_runner.active_run_id
    loaded_runner.pause()

    loaded_runner._backfill_uplink([(time.time(), {"udp_up_loss_pct": 50.0})])
    assert not any(s["udp_up_loss_pct"] is not None
                   for s in loaded_runner.storage.iter_samples(run_id))


# ---- against a real iperf3, when there is one ------------------------------

requires_iperf3 = pytest.mark.skipif(
    shutil.which("iperf3") is None, reason="iperf3 is not installed")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _serve(port: int) -> subprocess.Popen:
    proc = subprocess.Popen(["iperf3", "-s", "-p", str(port)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.5)
    return proc


def _wait_for(predicate, seconds: float = 30) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.25)
    return False


@requires_iperf3
def test_downlink_readings_flow_from_a_real_server():
    """The flags are what is most likely to be subtly wrong — a missing
    --forceflush produces nothing for the whole walk and then everything at
    once, which no parser test would notice."""
    port = _free_port()
    server = _serve(port)
    worker = UdpLoadWorker("127.0.0.1", DOWNLINK, port=port, bitrate="1M",
                           run_data_budget_mb=None)
    try:
        worker.start()
        assert _wait_for(
            lambda: worker.sample_fields()["udp_down_loss_pct"] is not None), \
            f"no reading arrived; last line: {worker._last_line!r}"
        fields = worker.sample_fields()
        assert fields["udp_down_jitter_ms"] >= 0
        assert fields["udp_down_mbps"] > 0
    finally:
        worker.stop()
        server.terminate()
        server.wait(timeout=5)


@requires_iperf3
def test_uplink_loss_comes_back_from_the_far_end_and_lands_on_samples():
    """The novel half: the rig cannot see uplink loss, so it runs a block and
    asks. This exercises the whole path — block, server output, per-second
    readings, timestamps — against the real binary."""
    port = _free_port()
    server = _serve(port)
    collected: list = []
    worker = UdpLoadWorker("127.0.0.1", UPLINK, port=port, bitrate="1M",
                           block_s=5, run_data_budget_mb=None,
                           on_backfill=collected.extend)
    started = time.time()
    try:
        worker.start()
        assert _wait_for(lambda: len(collected) >= 3, seconds=40), \
            f"no block came back; last line: {worker._last_line!r}"
    finally:
        worker.stop()
        server.terminate()
        server.wait(timeout=5)

    # Per-second readings, each carrying what the far end actually received,
    # stamped inside the window the block ran in.
    for ts, fields in collected[:3]:
        assert started <= ts <= time.time()
        assert fields["udp_up_loss_pct"] is not None
        assert fields["udp_up_jitter_ms"] is not None
    assert worker.snapshot()["backfilled_samples"] >= 3


# ---- binding to the link that is actually being measured -------------------

def test_an_address_is_picked_out_of_ip_output():
    """iperf3's -B takes an address where ping's -I takes a name, so the
    interface has to be resolved before the test can be pinned to it."""
    assert udpload.address_of(
        "3: eth0    inet 192.168.1.42/24 brd 192.168.1.255 scope global eth0"
    ) == "192.168.1.42"


def test_an_interface_with_no_address_resolves_to_nothing():
    """The modem can be between leases. There is then nothing to bind to."""
    assert udpload.address_of("1: lo    inet6 ::1/128 scope host") is None
    assert udpload.address_of("") is None


def test_the_load_test_is_pinned_to_the_measured_link(monkeypatch):
    """The rig broadcasts its own Wi-Fi and measures a different interface.
    Without -B the test follows the routing table, which is right today and
    stops being right the moment the Pi gains a second route."""
    monkeypatch.setattr(udpload, "interface_address", lambda name: "10.0.0.7")
    worker = udpload.UdpLoadWorker(server="example.test", interface="eth0")
    cmd = worker.build_command()
    assert "-B" in cmd and cmd[cmd.index("-B") + 1] == "10.0.0.7"
    # Never the interface name: iperf3 rejects that outright and the test
    # would never start.
    assert "eth0" not in cmd


def test_an_unresolvable_interface_falls_back_to_the_routing_table(monkeypatch):
    """Worse than binding, but far better than a test that refuses to run."""
    monkeypatch.setattr(udpload, "interface_address", lambda name: None)
    worker = udpload.UdpLoadWorker(server="example.test", interface="eth0")
    assert "-B" not in worker.build_command()


# ---- saying why a test produced nothing ------------------------------------

def test_an_authorization_failure_names_both_of_its_causes():
    """iperf3's own message names neither, and one of them — a clock this Pi
    cannot keep on its own — is not the one anybody checks first. Measured
    against iperf3 3.16: a client 10s out authenticates, 11s out is rejected,
    with exactly the message a wrong password gets."""
    reason = udpload.parse_error("iperf3: error - test authorization failed")
    assert "username/password" in reason
    assert "RTC" in reason


def test_other_failures_are_passed_through_as_iperf3_worded_them():
    assert udpload.parse_error(
        "iperf3: error - unable to connect to server: Connection refused"
    ) == "unable to connect to server: Connection refused"


def test_ordinary_output_is_not_mistaken_for_a_failure():
    assert udpload.parse_error(RECEIVER_LINE) is None
    assert udpload.parse_error("") is None


# ---- the padding change in iperf3 3.17 -------------------------------------

def test_a_rejection_makes_the_worker_try_the_older_padding(monkeypatch):
    """3.17 changed the credential encryption from PKCS#1 to OAEP, and the two
    do not interoperate. The server reports the mismatch as an authorization
    failure — identical to a wrong password — and says "padding check failed"
    only in its own log. Nobody should have to read a log on another machine,
    so the rejection itself is what settles it."""
    monkeypatch.setattr(udpload, "iperf_supports_pkcs1", lambda: True)
    worker = UdpLoadWorker(server="example.test", username="rig",
                           public_key_path="/k")
    assert udpload.PKCS1_FLAG not in worker.build_command()

    worker._fail_with_reason("iperf3: error - test authorization failed", "quiet")
    assert udpload.PKCS1_FLAG in worker.build_command()


def test_an_older_iperf3_is_never_given_a_flag_it_lacks(monkeypatch):
    """Before 3.17 there is no flag at all — passing it would break a setup
    that was working."""
    monkeypatch.setattr(udpload, "iperf_supports_pkcs1", lambda: False)
    worker = UdpLoadWorker(server="example.test", username="rig",
                           public_key_path="/k")
    worker._fail_with_reason("iperf3: error - test authorization failed", "quiet")
    assert udpload.PKCS1_FLAG not in worker.build_command()


def test_the_fallback_is_tried_once_and_then_reported(monkeypatch):
    """If the older padding is refused too, it is a real credential problem
    and must be said out loud rather than retried forever."""
    monkeypatch.setattr(udpload, "iperf_supports_pkcs1", lambda: True)
    said: list[tuple[str, str]] = []
    worker = UdpLoadWorker(server="example.test", username="rig",
                           public_key_path="/k",
                           on_event=lambda level, msg: said.append((level, msg)))
    worker._fail_with_reason("iperf3: error - test authorization failed", "quiet")
    worker._fail_with_reason("iperf3: error - test authorization failed", "quiet")
    assert any(level == "error" for level, _ in said)


def test_the_padding_can_be_pinned_when_negotiation_is_not_wanted():
    forced = UdpLoadWorker(server="s", username="u", public_key_path="/k",
                           auth_padding="pkcs1")
    assert udpload.PKCS1_FLAG in forced.build_command()
    modern = UdpLoadWorker(server="s", username="u", public_key_path="/k",
                           auth_padding="oaep")
    assert udpload.PKCS1_FLAG not in modern.build_command()


# ---- the duty cycle --------------------------------------------------------

def test_the_uplink_claims_the_link_for_the_burst_plus_the_settling_seconds():
    """The modem holds roughly 1.75 Mbit of buffer, measured from a real walk's
    own median round trip, so ping keeps queueing behind the test for a second
    or two after the last datagram is handed over. A sample taken then is still
    describing the test."""
    worker = UdpLoadWorker(server="example.invalid", direction=UPLINK,
                           block_s=10, idle_s=20, settle_s=3)
    assert worker.loading is False
    worker._load_until = time.time() + 2
    assert worker.loading is True
    worker._load_until = time.time() - 0.01
    assert worker.loading is False


def test_downlink_never_claims_the_uplink():
    """-R has the *server* send, so the rig's uplink stays empty. Marking those
    seconds would throw away two thirds of a walk for nothing."""
    worker = UdpLoadWorker(server="example.invalid", direction=DOWNLINK)
    worker._load_until = time.time() + 30
    assert worker.loading is False


def test_the_offered_rate_is_carried_for_the_report():
    """The report needs it to tell a spot that could not carry the load apart
    from one that carried it cleanly, and the config string is the only place
    the rate is written down."""
    worker = UdpLoadWorker(server="example.invalid", direction=UPLINK, bitrate="750k")
    assert worker.offered_mbps == pytest.approx(0.75)
    assert worker.snapshot()["offered_mbps"] == pytest.approx(0.75)


def test_the_block_length_is_still_floored():
    """A burst too short to establish reports nothing at all."""
    worker = UdpLoadWorker(server="example.invalid", direction=UPLINK, block_s=1)
    assert worker.block_s == 5.0


def test_a_negative_gap_is_not_a_gap():
    worker = UdpLoadWorker(server="example.invalid", direction=UPLINK,
                           idle_s=-5, settle_s=-1)
    assert worker.idle_s == 0.0
    assert worker.settle_s == 0.0


# ---- the hang that was invisible -------------------------------------------

def test_a_test_that_stops_talking_is_killed_rather_than_hanging_the_worker(monkeypatch):
    """Reading a pipe has no timeout, so a hung iperf3 held run_once open for
    the rest of the walk. The worker then never returned, never restarted and
    never said anything, which looks exactly like working. On 2026-09-18 that
    cost a nine-minute walk: udp_up logged nothing and recorded 13 seconds of
    load out of 566."""
    monkeypatch.setattr(udpload, "STALL_AFTER_S", 1.0)
    events = []
    worker = UdpLoadWorker(server="example.invalid", direction=UPLINK,
                           on_event=lambda level, msg: events.append((level, msg)))
    monkeypatch.setattr(worker, "build_command", lambda: ["sleep", "30"])
    monkeypatch.setattr(worker, "build_env", lambda: None)

    started = time.time()
    proc = worker._launch()
    proc.wait(timeout=15)                  # the watchdog must end this, not sleep
    elapsed = time.time() - started

    assert elapsed < 10, f"the hung process ran for {elapsed:.1f}s"
    assert worker._stalls == 1
    assert any("no answer from the server" in message for _, message in events)


def test_a_test_that_keeps_talking_is_left_alone(monkeypatch):
    """The watchdog must not shoot a healthy long-running test. Downlink runs
    with -t 0 and is meant to stream for the whole walk."""
    monkeypatch.setattr(udpload, "STALL_AFTER_S", 2.0)
    worker = UdpLoadWorker(server="example.invalid", direction=DOWNLINK)
    monkeypatch.setattr(
        worker, "build_command",
        lambda: ["sh", "-c", "for i in 1 2 3 4 5 6; do echo tick; sleep 0.5; done"])
    monkeypatch.setattr(worker, "build_env", lambda: None)

    proc = worker._launch()
    for line in proc.stdout:
        worker._consume(line)
    proc.wait(timeout=5)

    assert proc.returncode == 0, "a talking process was killed"
    assert worker._stalls == 0


def test_the_stall_count_is_visible(monkeypatch):
    """A worker that keeps being shot is a different problem from one that
    never starts, and the dashboard should be able to tell them apart."""
    worker = UdpLoadWorker(server="example.invalid", direction=UPLINK)
    assert worker.snapshot()["stalls"] == 0


# ---- telling a stuck server from a dead zone -------------------------------
#
# On 2026-10-15 the server's uplink service was stuck for two whole walks. The
# rig logged "the test stopped responding" once a minute, which is also what a
# dead zone looks like, and both walks came back with no uplink data. The
# strings below are what iperf3 3.16 really printed in each case, reproduced
# on 2026-11-22: a wedged server and a blackholed port both print nothing at
# all, and only --connect-timeout makes the second one end by itself.

CONNECT_TIMED_OUT = (
    "iperf3: error - unable to connect to server - server may have stopped "
    "running or use a different port, firewall issue, etc.: Connection timed out")
CONNECT_REFUSED = (
    "iperf3: error - unable to connect to server - server may have stopped "
    "running or use a different port, firewall issue, etc.: Connection refused")
SERVER_BUSY = "iperf3: error - the server is busy running a test. try again later"
STREAM_LOST = (
    "iperf3: error - unable to read from stream socket: "
    "Resource temporarily unavailable")
# A reverse stream that has stopped delivering still prints a line a second.
EMPTY_SECOND = "[  5]   3.00-4.00   sec  0.00 Bytes  0.00 bits/sec  0.012 ms  0/0 (0%)  "


def _said(text: str) -> str:
    return udpload.ERROR_RE.search(text).group("message")


def test_a_dead_link_is_made_to_end_the_attempt_by_itself():
    """Without this a connect into a dead link is silent for as long as the
    kernel keeps retrying, which is exactly what a wedged server looks like.
    With it, silence can only mean the connection was made."""
    for direction in (UPLINK, DOWNLINK):
        argv = UdpLoadWorker("10.0.0.1", direction).build_command()
        assert argv[argv.index("--connect-timeout") + 1] == str(udpload.CONNECT_TIMEOUT_MS)
    assert udpload.CONNECT_TIMEOUT_MS < udpload.STALL_AFTER_S * 1000


@pytest.mark.parametrize("text, link, kind", [
    (CONNECT_TIMED_OUT, "dead", "no_link"),       # a dead zone
    (CONNECT_TIMED_OUT, "poor", "weak_link"),     # one ping in five: the place
    (CONNECT_TIMED_OUT, None, "no_connect"),      # nobody can say, so nobody is blamed
    (CONNECT_TIMED_OUT, "good", "unreachable"),   # ping is clean, the server is gone
    (CONNECT_REFUSED, "dead", "refused"),
    (CONNECT_REFUSED, "good", "refused"),
    (SERVER_BUSY, "good", "busy"),
    (STREAM_LOST, "good", "interrupted"),
    ("iperf3: error - test authorization failed", "good", "auth"),
])
def test_the_same_words_mean_different_things_on_a_different_link(text, link, kind):
    assert udpload.error_kind(_said(text), link) == kind


def test_a_test_that_never_spoke_is_not_called_an_error():
    assert udpload.error_kind(None, "good") == "no_readings"


def _watched(good=0, poor=0, dead=0):
    """A worker whose attempt was watched for that many seconds of each."""
    worker = UdpLoadWorker("10.0.0.1", UPLINK)
    worker._attempt_link = {"good": good, "poor": poor, "dead": dead}
    return worker


def test_silence_with_the_link_up_is_blamed_on_the_server():
    """The connection was made, ping was getting through for the whole
    silence, and the server never set the test up. That is a stuck server, and
    it is the one case somebody has to act on."""
    worker = _watched(good=44, dead=1)
    kind, words = worker._stall_verdict()
    assert kind == "silent"
    assert "restart the server" in words
    assert "stopped responding" not in words


def test_silence_with_no_link_is_blamed_on_the_place():
    kind, words = _watched(good=2, dead=43)._stall_verdict()
    assert kind == "no_link"
    assert "the server is not the problem" in words


def test_silence_on_a_poor_link_is_not_blamed_on_the_server():
    """A link getting one ping in five through is not dead, and it is not one
    a handshake can be expected to finish over. iperf3 says nothing until a
    second into the test, so a handshake that spends fifteen seconds in TCP
    retries and then loses the one datagram that starts the test is silent
    for forty-five. That is the garage. Calling it a stuck server would tell
    the operator to restart a healthy one, in the marginal places a survey is
    for, and put that in a report that cannot be corrected."""
    kind, words = _watched(poor=45)._stall_verdict()
    assert kind == "no_answer"
    assert "not the server" in words
    assert _watched(good=30, poor=15)._stall_verdict()[0] == "no_answer"


def test_silence_nobody_can_explain_blames_nobody():
    """With ping switched off, or a link that came and went, there is no
    witness. Blaming a healthy server costs somebody a restart mid-walk."""
    assert _watched()._stall_verdict()[0] == "no_answer"
    assert _watched(good=22, dead=23)._stall_verdict()[0] == "no_answer"
    assert _watched(good=3)._stall_verdict()[0] == "no_answer"


def test_a_test_that_stalls_part_way_is_the_link_dropping():
    """Once the server has set the test up, going quiet is what a dead zone
    does to it. That must never be reported as a server fault."""
    worker = _watched(good=45)
    worker._consume("[  5] local 10.0.0.2 port 40112 connected to 10.0.0.1 port 5201")
    assert worker._stall_verdict()[0] == "interrupted"


def test_connecting_is_not_the_same_as_the_server_setting_the_test_up():
    worker = UdpLoadWorker("10.0.0.1", UPLINK)
    worker._consume("Connecting to host 10.0.0.1, port 5201")
    assert worker._engaged is False
    worker._consume(SENDER_LINE)
    assert worker._engaged is True


def _stalled_worker(monkeypatch, link_quality):
    """Run the real watchdog against a process that never says anything."""
    monkeypatch.setattr(udpload, "STALL_AFTER_S", 2.0)
    monkeypatch.setattr(udpload, "MIN_LINK_SAMPLES", 1)
    events = []
    worker = UdpLoadWorker(server="example.invalid", direction=UPLINK,
                           link_quality=link_quality,
                           on_event=lambda level, msg: events.append((level, msg)))
    monkeypatch.setattr(worker, "build_command", lambda: ["sleep", "30"])
    monkeypatch.setattr(worker, "build_env", lambda: None)
    worker._launch().wait(timeout=15)
    return worker, events


def test_a_stuck_server_turns_the_chip_red_and_says_what_to_do(monkeypatch):
    worker, events = _stalled_worker(monkeypatch, link_quality=lambda: "good")
    assert events == [("error", events[0][1])]
    assert "restart the server" in events[0][1]
    outcome = worker.current_outcome()
    assert outcome["state"] == udpload.OUTCOME_FAIL
    assert outcome["detail"].startswith("server not answering")
    assert worker.state == "failed"


def test_a_dead_zone_only_ever_turns_it_amber(monkeypatch):
    worker, events = _stalled_worker(monkeypatch, link_quality=lambda: "dead")
    assert [level for level, _ in events] == ["warning"]
    assert worker.current_outcome()["state"] == udpload.OUTCOME_WARN
    assert worker.current_outcome()["detail"] == "no link"


def test_a_patchy_link_only_ever_turns_it_amber_too(monkeypatch):
    worker, events = _stalled_worker(monkeypatch, link_quality=lambda: "poor")
    assert [level for level, _ in events] == ["warning"]
    assert worker.current_outcome()["state"] == udpload.OUTCOME_WARN
    assert worker.current_outcome()["detail"] == "no answer from the server"


def test_a_witness_that_falls_over_is_treated_as_absent(monkeypatch):
    def broken():
        raise RuntimeError("ping is not there")
    worker, events = _stalled_worker(monkeypatch, link_quality=broken)
    assert worker.current_outcome()["kind"] == "no_answer"


def test_a_killed_test_is_explained_once_not_twice(monkeypatch):
    """The watchdog says why it killed the test. The reader then wakes with an
    empty transcript, and must not overwrite that with "no readings"."""
    monkeypatch.setattr(udpload, "STALL_AFTER_S", 1.0)
    events = []
    worker = UdpLoadWorker(server="example.invalid", direction=UPLINK, idle_s=0,
                           on_event=lambda level, msg: events.append((level, msg)))
    monkeypatch.setattr(worker, "build_command", lambda: ["sleep", "30"])
    monkeypatch.setattr(worker, "build_env", lambda: None)
    worker._run_block()
    assert len(events) == 1
    assert worker.current_outcome()["kind"] == "no_answer"


def test_stopping_mid_burst_is_not_reported_as_a_failure(monkeypatch):
    """Every End and every Pause lands inside a burst as often as not. That is
    the operator's doing, and it must not put a warning in the log."""
    import threading
    events = []
    worker = UdpLoadWorker(server="example.invalid", direction=UPLINK, idle_s=0,
                           on_event=lambda level, msg: events.append((level, msg)))
    monkeypatch.setattr(worker, "build_command", lambda: ["sleep", "30"])
    monkeypatch.setattr(worker, "build_env", lambda: None)
    thread = threading.Thread(target=worker._run_block)
    thread.start()
    assert _wait_for(lambda: worker._proc is not None, seconds=5)
    worker._stop.set()
    worker.on_stop()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert events == []
    assert worker.current_outcome() is None


# ---- when trouble becomes a fault ------------------------------------------

def _since(worker, kind, first_seen_ago, last_seen_ago=0.0):
    """Pretend this kind of trouble began that long ago."""
    now = time.time()
    worker._trouble_since[kind] = (now - first_seen_ago, now - last_seen_ago)


def test_a_rejected_login_is_a_fault_at_once():
    worker = UdpLoadWorker("10.0.0.1", UPLINK, auth_padding="pkcs1")
    worker._fail_with_reason("iperf3: error - test authorization failed", "quiet")
    assert worker.current_outcome()["state"] == udpload.OUTCOME_FAIL
    assert worker.state == "failed"


def test_a_busy_server_is_only_a_fault_once_it_has_been_busy_too_long():
    """Measured on 3.16, a server answers "busy" for 46.6 s after a rig vanishes
    mid-test. That is normal after any dead zone. Busy for minutes is a test
    nobody is on the other end of, and only a restart ends it."""
    events = []
    worker = UdpLoadWorker("10.0.0.1", DOWNLINK,
                           on_event=lambda level, msg: events.append((level, msg)))
    worker._fail_with_reason(SERVER_BUSY, "quiet")
    assert worker.current_outcome()["state"] == udpload.OUTCOME_WARN
    assert worker.current_outcome()["detail"] == "server busy with another test"

    _since(worker, "busy", udpload.ESCALATE_AFTER_S["busy"] + 1)
    worker._fail_with_reason(SERVER_BUSY, "quiet")
    assert worker.current_outcome()["state"] == udpload.OUTCOME_FAIL
    assert [level for level, _ in events] == ["warning", "error"]
    assert "restart the server" in events[-1][1]


def test_dead_zones_in_between_do_not_let_a_stuck_server_off():
    """A server stuck on a dead test answers "busy" whenever the rig can reach
    it, and the rig cannot reach it in a dead zone. If each dead zone restarted
    the clock, busy would never last its two minutes on a walk with dead
    zones in it, which is every walk worth doing."""
    worker = UdpLoadWorker("10.0.0.1", DOWNLINK, link_quality=lambda: "dead")
    worker._fail_with_reason(SERVER_BUSY, "quiet")
    _since(worker, "busy", udpload.ESCALATE_AFTER_S["busy"] + 5, last_seen_ago=60)
    worker._fail_with_reason(CONNECT_TIMED_OUT, "quiet")          # a dead zone
    assert worker.current_outcome()["state"] == udpload.OUTCOME_WARN
    worker._fail_with_reason(SERVER_BUSY, "quiet")                # still busy after it
    assert worker.current_outcome()["state"] == udpload.OUTCOME_FAIL


def test_a_pause_does_not_let_a_stuck_server_off_either(monkeypatch):
    from viabot_survey.workers.base import Worker
    monkeypatch.setattr(Worker, "start", lambda self: None)
    worker = UdpLoadWorker("10.0.0.1", DOWNLINK)
    worker._fail_with_reason(SERVER_BUSY, "quiet")
    _since(worker, "busy", udpload.ESCALATE_AFTER_S["busy"] + 5, last_seen_ago=90)
    worker.start()                                                # Resume
    worker._fail_with_reason(SERVER_BUSY, "quiet")
    assert worker.current_outcome()["state"] == udpload.OUTCOME_FAIL
    # A new walk is a new question.
    worker.begin_run()
    worker._fail_with_reason(SERVER_BUSY, "quiet")
    assert worker.current_outcome()["state"] == udpload.OUTCOME_WARN


def test_trouble_from_long_ago_is_a_new_episode_when_it_comes_back():
    worker = UdpLoadWorker("10.0.0.1", UPLINK)
    worker._fail_with_reason(CONNECT_REFUSED, "quiet")
    _since(worker, "refused", 3600, last_seen_ago=udpload.TROUBLE_FORGOTTEN_AFTER_S + 60)
    worker._fail_with_reason(CONNECT_REFUSED, "quiet")
    assert worker.current_outcome()["state"] == udpload.OUTCOME_WARN


def test_a_server_that_refuses_for_a_moment_is_not_a_fault():
    """A server being restarted refuses connections for a second or two."""
    worker = UdpLoadWorker("10.0.0.1", UPLINK)
    worker._fail_with_reason(CONNECT_REFUSED, "quiet")
    assert worker.current_outcome()["state"] == udpload.OUTCOME_WARN

    _since(worker, "refused", udpload.ESCALATE_AFTER_S["refused"] + 1)
    worker._fail_with_reason(CONNECT_REFUSED, "quiet")
    assert worker.current_outcome()["state"] == udpload.OUTCOME_FAIL


def test_one_failed_connect_on_a_good_link_is_not_yet_a_missing_server():
    """Three in a row on a link ping calls clean, about a minute, is a server
    that has gone. One or two can be bad luck, and red tells the operator to
    go and look at a server."""
    worker = UdpLoadWorker("10.0.0.1", UPLINK, link_quality=lambda: "good")
    worker._fail_with_reason(CONNECT_TIMED_OUT, "quiet")
    _since(worker, "unreachable", 25)
    worker._fail_with_reason(CONNECT_TIMED_OUT, "quiet")
    assert worker.current_outcome()["state"] == udpload.OUTCOME_WARN
    _since(worker, "unreachable", udpload.ESCALATE_AFTER_S["unreachable"] + 1)
    worker._fail_with_reason(CONNECT_TIMED_OUT, "quiet")
    assert worker.current_outcome()["state"] == udpload.OUTCOME_FAIL


def test_failed_connects_on_a_patchy_link_never_blame_the_server():
    """Four SYNs in ten seconds all lost is likely at sixty per cent loss, and
    ping still gets the odd reply through. That is a weak link, for as long as
    it lasts."""
    worker = UdpLoadWorker("10.0.0.1", UPLINK, link_quality=lambda: "poor")
    worker._fail_with_reason(CONNECT_TIMED_OUT, "quiet")
    _since(worker, "weak_link", 3600)
    worker._fail_with_reason(CONNECT_TIMED_OUT, "quiet")
    outcome = worker.current_outcome()
    assert (outcome["state"], outcome["detail"]) == (
        udpload.OUTCOME_WARN, "link too weak for the test")


def test_a_connect_is_judged_by_the_link_while_it_was_trying_not_at_the_end():
    """Ten seconds of dead link and a ping reply in the last one is a dead
    zone ending, not a server that could not be reached on a good link."""
    worker = UdpLoadWorker("10.0.0.1", UPLINK, link_quality=lambda: "good")
    worker._attempt_link = {"good": 1, "poor": 0, "dead": 9}
    worker._fail_with_reason(CONNECT_TIMED_OUT, "quiet")
    assert worker.current_outcome()["kind"] == "no_link"


def test_a_dead_zone_never_becomes_a_fault_however_long_it_lasts():
    worker = UdpLoadWorker("10.0.0.1", UPLINK, link_quality=lambda: "dead")
    worker._fail_with_reason(CONNECT_TIMED_OUT, "quiet")
    _since(worker, "no_link", 3600)
    worker._fail_with_reason(CONNECT_TIMED_OUT, "quiet")
    assert worker.current_outcome()["state"] == udpload.OUTCOME_WARN
    assert worker.state == "degraded"


def test_a_different_kind_of_trouble_starts_its_own_clock():
    worker = UdpLoadWorker("10.0.0.1", UPLINK)
    _since(worker, "busy", 3600)
    worker._fail_with_reason(CONNECT_REFUSED, "quiet")
    assert worker.current_outcome()["state"] == udpload.OUTCOME_WARN


def test_the_same_trouble_is_said_once_and_again_after_it_has_been_fixed():
    events = []
    worker = UdpLoadWorker("10.0.0.1", UPLINK, on_backfill=lambda r: None,
                           on_event=lambda level, msg: events.append((level, msg)))
    for _ in range(4):
        worker._fail_with_reason(STREAM_LOST, "quiet")
    assert len(events) == 1

    worker._absorb_server_output(SERVER_OUTPUT, started=time.time(), interval_ends=[])
    worker._fail_with_reason(STREAM_LOST, "quiet")
    assert len(events) == 2


def test_recovering_from_a_fault_is_announced():
    events = []
    worker = UdpLoadWorker("10.0.0.1", UPLINK, auth_padding="pkcs1",
                           on_backfill=lambda r: None,
                           on_event=lambda level, msg: events.append((level, msg)))
    worker._fail_with_reason("iperf3: error - test authorization failed", "quiet")
    worker._absorb_server_output(SERVER_OUTPUT, started=time.time(), interval_ends=[])
    assert events[-1] == ("info", "udp_up: working again")
    assert worker.current_outcome()["state"] == udpload.OUTCOME_OK


# ---- what the phone is told between attempts -------------------------------

def test_a_good_burst_stays_good_until_the_next_one_reports():
    """Uplink reports once per burst. Showing "unknown" for the twenty seconds
    between bursts would make the chip blink for the whole walk."""
    worker = UdpLoadWorker("10.0.0.1", UPLINK, block_s=10, idle_s=20,
                           on_backfill=lambda r: None)
    assert worker.current_outcome() is None
    worker._absorb_server_output(SERVER_OUTPUT, started=time.time(), interval_ends=[])
    now = time.time()
    assert worker.current_outcome(now + 30)["state"] == udpload.OUTCOME_OK
    assert worker.current_outcome(now + 75)["state"] == udpload.OUTCOME_OK


def test_but_it_does_not_stay_good_for_ever():
    worker = UdpLoadWorker("10.0.0.1", UPLINK, block_s=10, idle_s=20,
                           on_backfill=lambda r: None)
    worker._absorb_server_output(SERVER_OUTPUT, started=time.time(), interval_ends=[])
    stale = worker.current_outcome(time.time() + 300)
    assert stale["state"] == udpload.OUTCOME_WARN
    assert stale["detail"].startswith("no readings for")


def test_downlink_goes_stale_in_seconds_because_it_reports_every_second():
    worker = UdpLoadWorker("10.0.0.1", DOWNLINK)
    worker._note_ok()
    now = time.time()
    assert worker.current_outcome(now + 5)["state"] == udpload.OUTCOME_OK
    assert worker.current_outcome(now + 30)["state"] == udpload.OUTCOME_WARN


def test_a_stream_delivering_nothing_does_not_count_as_working(monkeypatch):
    """A reverse stream that has stopped delivering still prints a line every
    second, with no datagrams in it. Those lines must not keep the chip green."""
    worker = UdpLoadWorker("10.0.0.1", DOWNLINK)
    script = "printf '%s\\n' \"$0\" \"$0\" \"$0\"; sleep 0.2"
    monkeypatch.setattr(worker, "build_command", lambda: ["sh", "-c", script, EMPTY_SECOND])
    monkeypatch.setattr(worker, "build_env", lambda: None)
    oks = []
    monkeypatch.setattr(worker, "_note_ok", lambda: oks.append(1))
    worker._run_stream()
    assert oks == []
    # Nor is it a measurement. "0% of nothing was lost" used to be stored as a
    # loss-free second, in exactly the seconds where nothing got through.
    assert worker.sample_fields()["udp_down_loss_pct"] is None
    assert worker._latest is None

    monkeypatch.setattr(worker, "build_command", lambda: ["sh", "-c", script, RECEIVER_LINE])
    worker._run_stream()
    assert len(oks) == 3


def test_a_new_walk_starts_with_nothing_to_say(monkeypatch):
    """Carrying the last walk's verdict into this one would show a fault that
    may be long gone, or sit on top of one that is new."""
    from viabot_survey.workers.base import Worker
    monkeypatch.setattr(Worker, "start", lambda self: None)
    worker = UdpLoadWorker("10.0.0.1", UPLINK)
    worker._fail_with_reason(SERVER_BUSY, "quiet")
    assert worker.current_outcome() is not None
    worker.start()
    assert worker.current_outcome() is None
    assert worker.snapshot()["outcome"] is None


def test_the_few_words_for_the_phone_lead_with_what_is_wrong():
    """The chip cuts its text off after a dozen characters, so those have to
    be the ones that say what happened. What to do about it is read in the
    alert, which shows the whole sentence."""
    assert udpload.phone_words("silent", True) == ("server not answering",
                                                   "Restart the server.")
    assert udpload.phone_words("busy", False) != udpload.phone_words("busy", True)
    # Nothing the operator can do about a dead zone, so nothing is asked.
    assert udpload.phone_words("no_link", False) == ("no link", "")


# ---- which seconds the test owns the uplink for ----------------------------

def _run_fake_block(worker, monkeypatch, script):
    monkeypatch.setattr(worker, "build_command", lambda: ["sh", "-c", script])
    monkeypatch.setattr(worker, "build_env", lambda: None)
    states = []
    real = worker._consume

    def spy(line):
        result = real(line)
        states.append((line.strip()[:24], worker.load_state))
        return result
    monkeypatch.setattr(worker, "_consume", spy)
    worker._run_block()
    return states


def test_an_attempt_that_never_starts_does_not_claim_the_uplink(monkeypatch):
    """A connect that fails puts nothing on the uplink. Marking its seconds as
    loaded took them out of dead-zone detection in a dead zone, which is the
    one place the detection is for, and counted every failed handshake as the
    rig having sent a burst that never arrived."""
    worker = UdpLoadWorker("10.0.0.1", UPLINK, block_s=10, idle_s=0, settle_s=3)
    states = _run_fake_block(
        worker, monkeypatch,
        "echo 'iperf3: error - unable to connect to server: Connection timed out'")
    assert [state for _, state in states] == [udpload.LOAD_IDLE]
    assert worker.load_state == udpload.LOAD_IDLE
    assert worker.loading is False


def test_the_uplink_is_claimed_from_the_moment_the_server_sets_the_test_up(monkeypatch):
    worker = UdpLoadWorker("10.0.0.1", UPLINK, block_s=10, idle_s=0, settle_s=3,
                           on_backfill=lambda rows: None)
    script = ("echo 'Connecting to host 10.0.0.1, port 5201'; "
              "echo '[  5] local 10.0.0.2 port 40112 connected to 10.0.0.1 port 5201'; "
              f"echo '{SENDER_LINE}'")
    states = _run_fake_block(worker, monkeypatch, script)
    # Read as each line is taken in, so before that line has been acted on:
    # idle until the server has set the test up, sending from the line after.
    assert [state for _, state in states] == [
        udpload.LOAD_IDLE, udpload.LOAD_IDLE, udpload.LOAD_SENDING]
    # It ended at once, so it is no longer sending, but the buffer it filled
    # still needs its few seconds to drain.
    assert worker.load_state == udpload.LOAD_SETTLING
    assert worker.loading is True


def test_a_dead_zone_is_logged_in_words_that_do_not_blame_the_server():
    """iperf3's own words for a connect that timed out are "server may have
    stopped running or use a different port, firewall issue". In a dead zone
    that sends somebody to look at a healthy server."""
    events = []
    worker = UdpLoadWorker("10.0.0.1", UPLINK, link_quality=lambda: "dead",
                           on_event=lambda level, msg: events.append((level, msg)))
    worker._fail_with_reason(CONNECT_TIMED_OUT, "quiet")
    level, message = events[0]
    assert level == "warning"
    assert "the server is not the problem" in message
    assert "server may have stopped running" not in message
    assert message.endswith("(iperf3: Connection timed out)")


def test_iperf3s_own_words_are_kept_where_they_are_the_only_clue():
    assert udpload.plain_reason("interrupted", _said(STREAM_LOST), "quiet") == (
        "the test was cut off (iperf3: unable to read from stream socket: "
        "Resource temporarily unavailable)")
    # Nothing was said, so there is nothing to translate.
    assert udpload.plain_reason("no_readings", None, "the server returned no readings") == (
        "the server returned no readings")
    # A rejected login keeps its own, longer explanation.
    assert udpload.plain_reason("auth", "test authorization failed", udpload.AUTH_HINT) == (
        udpload.AUTH_HINT)


# ---- a direction that never says anything at all ---------------------------

def test_first_test_running_does_not_last_the_whole_walk():
    """Nothing yet is normal for the first burst. A direction that connects
    and then never reports, never fails and never stalls would otherwise leave
    the chip on "first test running" until End."""
    worker = UdpLoadWorker("10.0.0.1", DOWNLINK)
    worker._started_at = time.time()
    assert worker.current_outcome() is None
    late = worker.current_outcome(time.time() + 300)
    assert late["state"] == udpload.OUTCOME_WARN
    assert late["detail"].startswith("nothing yet after")


def test_uplink_is_given_long_enough_for_a_whole_first_burst():
    worker = UdpLoadWorker("10.0.0.1", UPLINK, block_s=20, idle_s=10)
    worker._started_at = time.time()
    assert worker.current_outcome(time.time() + 60) is None


def test_a_stream_that_is_connected_and_empty_says_so(monkeypatch):
    """It prints a line a second, so it never stalls and never fails. Without
    this it never reports either."""
    monkeypatch.setattr(udpload, "STALE_DOWNLINK_OUTCOME_S", 0.0)
    events = []
    worker = UdpLoadWorker("10.0.0.1", DOWNLINK,
                           on_event=lambda level, msg: events.append((level, msg)))
    seen = []
    script = "printf '%s\\n' \"$0\" \"$0\" \"$0\"; sleep 0.2"
    monkeypatch.setattr(worker, "build_command", lambda: ["sh", "-c", script, EMPTY_SECOND])
    monkeypatch.setattr(worker, "build_env", lambda: None)
    real = worker._note_trouble
    monkeypatch.setattr(worker, "_note_trouble",
                        lambda kind, reason: (seen.append(kind), real(kind, reason)))
    worker._run_stream()
    assert seen[0] == "no_data"
    assert ("warning", "udp_down: the stream is connected but nothing has "
                       "arrived for 0s") in events
    assert udpload.phone_words("no_data", False) == ("connected, nothing arriving", "")


# ---- putting each reading on the second it describes -----------------------

def test_a_line_held_up_by_the_end_of_test_exchange_does_not_move_its_reading():
    """The last interval line of every burst only arrives once the end-of-test
    exchange has gone round, which on a real link is most of a second. Timing
    each reading by its own line put that second one sample late, and left a
    second in every burst marked "sent" with nothing on it: a clean two-minute
    walk showed four seconds of "sent, nothing arrived", one a burst."""
    collected = []
    worker = UdpLoadWorker("10.0.0.1", UPLINK, on_backfill=collected.extend)
    began = time.time() - 10
    worker._absorb_server_output(SERVER_OUTPUT, started=began - 3,
                                 interval_ends=[began + 1.0, began + 2.0 + 0.8])
    assert [round(ts - began, 2) for ts, _ in collected] == [0.5, 1.5]


def test_a_second_the_server_received_in_is_a_second_the_rig_was_sending():
    """iperf3 prints nothing until a second into the test, so the first sample
    of every burst is written before anything says the burst has begun. The
    reading that comes back for it is what marks it."""
    collected = []
    worker = UdpLoadWorker("10.0.0.1", UPLINK, on_backfill=collected.extend)
    worker._absorb_server_output(SERVER_OUTPUT, started=time.time(), interval_ends=[])
    assert all(fields["uplink_loaded"] == udpload.LOAD_SENDING
               for _, fields in collected)


def test_a_burst_is_dated_from_when_it_began_not_from_its_first_line(monkeypatch):
    """The first thing iperf3 prints is the first interval, a second in."""
    worker = UdpLoadWorker("10.0.0.1", UPLINK, block_s=10, idle_s=0, settle_s=3,
                           on_backfill=lambda rows: None)
    launched = time.time()
    _run_fake_block(worker, monkeypatch, f"sleep 0.3; echo '{SENDER_LINE}'")
    assert worker._sending_from == pytest.approx(launched + 0.3 - 1.0, abs=0.25)
