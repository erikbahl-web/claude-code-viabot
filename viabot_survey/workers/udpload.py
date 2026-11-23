"""Measure the link the way teleoperation actually uses it, in both directions.

Everything else here measures an idle link. Ping sends sixty bytes a second and
asks how long they take; that finds a garage where nothing gets through. It does
not find the more interesting failure: a spot that looks fine empty and falls
apart the moment you put a video stream on it. Carriers shape UDP differently
from TCP and drop it first under contention, so a link that passes a TCP speed
test can still be useless for teleop.

A real session is asymmetric, and both halves matter for different reasons:

**Uplink** carries the robot's video to the operator. It is the heavy stream,
and cellular uplink is typically the weaker direction, so this is usually what
decides whether a spot is workable. Measuring only downlink would flatter every
garage.

**Downlink** carries the operator's commands back. It is small, but if it
collapses the robot stops taking orders, which is its own kind of failure.

iperf3 only reports jitter and loss at the *receiving* end, which shapes how
each direction is measured. Downlink is received by the rig, so its numbers
arrive live, once a second. Uplink is received by the server, so it runs in
blocks and asks for the server's own per-second output at the end of each one.
The readings are then backfilled onto the samples they belong to. Nothing is
lost by the delay: the report is built when the run ends.

**Uplink runs in short bursts with silence between them.** The load is real
traffic on a link with a deep buffer in the modem, and an offered rate above
what the link can carry fills that buffer in about a second — after which ping,
sharing the same queue, reports latency the garage did not cause. A short block
bounds how long that can last, and the idle gap gives the buffer time to drain
so the next stretch of ping measures the place rather than the test. Samples
taken inside a burst, and for a few seconds after one, are marked as such and
are not used to decide where the dead zones were.

Three deliberate choices about realism:

**Datagrams are RTP-sized, not iperf3's default.** iperf3 sends 32 KB UDP
datagrams unless told otherwise, which the IP layer then fragments into two
dozen packets. Lose any one and the whole datagram counts as lost, so the loss
figure comes out several times worse than what a real 1200-byte video packet
would see. The default would make every garage look terrible.

**Each direction gets the bitrate that direction really carries.** Loading the
downlink with video-sized traffic would measure a session nobody runs.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
import time
from collections import deque
from functools import lru_cache
from typing import Any, Callable

from .base import STATE_DEGRADED, STATE_FAILED, Worker

log = logging.getLogger(__name__)

#: A per-second line from the receiving end, carrying jitter and loss:
#:
#:   [  5]   0.00-1.00   sec   123 KBytes  1.01 Mbits/sec  0.062 ms  0/105 (0%)
RECEIVER_RE = re.compile(
    r"^\[\s*\d+\]\s+"
    r"(?P<start>[\d.]+)-(?P<end>[\d.]+)\s+sec\s+"
    r"(?P<transfer>[\d.]+)\s+(?P<transfer_unit>[KMGT]?)Bytes\s+"
    r"(?P<rate>[\d.]+)\s+(?P<rate_unit>[KMGT]?)bits/sec\s+"
    r"(?P<jitter>[\d.]+)\s+ms\s+"
    r"(?P<lost>\d+)/(?P<total>\d+)\s+\((?P<loss_pct>[\d.]+)%\)\s*$"
)

#: The sending end's line has no jitter or loss — only what it put on the wire:
#:
#:   [  5]   0.00-1.00   sec   123 KBytes  1.01 Mbits/sec  105
SENDER_RE = re.compile(
    r"^\[\s*\d+\]\s+"
    r"(?P<start>[\d.]+)-(?P<end>[\d.]+)\s+sec\s+"
    r"(?P<transfer>[\d.]+)\s+(?P<transfer_unit>[KMGT]?)Bytes\s+"
    r"(?P<rate>[\d.]+)\s+(?P<rate_unit>[KMGT]?)bits/sec\s+"
    r"(?P<total>\d+)\s*$"
)

_BYTE_SCALE = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}
_BIT_SCALE = {"": 1e-6, "K": 1e-3, "M": 1.0, "G": 1e3, "T": 1e6}

#: Bytes per datagram. Real-time video is sent in packets that fit inside a path
#: MTU; 1200 is the figure WebRTC implementations settle on.
DEFAULT_DATAGRAM_BYTES = 1200

#: Readings older than this are not current any more.
STALE_AFTER_S = 3.0

#: A test that has said nothing for this long is hung, not slow.
#:
#: iperf3 prints an interval line every second while a test is running. The one
#: legitimate silence is the end-of-test exchange with the server, which takes
#: seconds. Reading its output with no timeout means a hung test blocks
#: `run_once` forever, and because the worker never returns it is never
#: restarted and never announces anything: it just looks healthy. Measured
#: 2026-09-18, run `aew-test-03`: `udp_up` logged nothing at all for a
#: nine-minute walk and recorded 13 seconds of load out of 566.
STALL_AFTER_S = 45.0

#: How long iperf3 may spend opening its control connection before giving up.
#:
#: This is what tells a dead link apart from a dead server. Measured 2026-11-22
#: against iperf3 3.16: without it, a connect into a blackholed port and a
#: connect to a wedged server both print *nothing at all* for 50 s, not even
#: "Connecting to host", which only appears after the server's first reply. With
#: it, the dead link ends by itself ("unable to connect to server ...
#: Connection timed out", 5.02 s against a 5000 ms timeout), so silence that
#: lasts until the watchdog can only mean the connection was made and the
#: server never spoke. Do not remove it to "simplify" the command.
CONNECT_TIMEOUT_MS = 10_000

UPLINK, DOWNLINK = "upload", "download"

#: What the phone's load-test chip can say about a direction's last attempt.
OUTCOME_OK, OUTCOME_WARN, OUTCOME_FAIL = "ok", "warn", "fail"

#: How long each kind of trouble has to last before it is a fault somebody must
#: fix rather than a moment that will pass. Anything not listed never becomes a
#: fault: a dead zone kills the test too, and a rig that shouts "fault" in
#: every dead zone trains its operator to stop reading the screen.
#:
#: * ``silent`` and ``auth`` are faults at once. A silent verdict already has
#:   :data:`STALL_AFTER_S` of evidence behind it, and a login does not fix
#:   itself.
#: * ``refused`` gets a moment, because a server that is being restarted
#:   refuses connections for a second or two.
#: * ``unreachable`` gets a minute, which is three failed connects in a row on
#:   a link ping calls clean. One can be bad luck.
#: * ``busy`` gets two minutes. Measured on 3.16, a server answers "busy" for
#:   46.6 s after a client vanishes mid-test; busy for much longer than that is
#:   a test nobody is on the other end of.
#:
#: The clock for a kind runs from when it was first seen until something
#: succeeds, not until the next different failure. A server stuck on a dead
#: test answers "busy" between dead zones for a whole walk, and a clock that
#: restarted at every dead zone would never reach two minutes.
ESCALATE_AFTER_S = {"silent": 0.0, "auth": 0.0, "missing": 0.0,
                    "refused": 20.0, "unreachable": 60.0, "busy": 120.0}

#: A kind of trouble not seen for this long is a new episode when it returns.
TROUBLE_FORGOTTEN_AFTER_S = 300.0

#: What the link witness can say. "Poor" matters as much as the other two: a
#: link that gets one ping in five through is not dead, and it is not a link
#: an iperf3 handshake can be expected to finish over either. Blaming the
#: server needs a link that was *good*, not merely one that was not dead.
LINK_GOOD, LINK_POOR, LINK_DEAD = "good", "poor", "dead"

#: Share of an attempt's seconds the link has to have been good (or dead) for
#: before that is taken as what the link was, and the fewest seconds worth
#: judging from.
LINK_UP_SHARE = 0.8
MIN_LINK_SAMPLES = 5

#: A downlink reading older than this is no longer "measuring".
STALE_DOWNLINK_OUTCOME_S = 15.0

#: What a sample's ``uplink_loaded`` column means. Both are truthy, because
#: both are seconds ping cannot be believed about; they are told apart so the
#: report can say "the rig was sending and nothing arrived" — a real failure —
#: rather than counting the deliberate silence between bursts as one.
LOAD_SENDING, LOAD_SETTLING, LOAD_IDLE = 2, 1, 0

#: Sample columns each direction fills.
FIELDS = {
    UPLINK: ("udp_up_jitter_ms", "udp_up_loss_pct", "udp_up_mbps"),
    DOWNLINK: ("udp_down_jitter_ms", "udp_down_loss_pct", "udp_down_mbps"),
}


#: iperf3 prints its failures on stderr, which is folded into stdout here, so
#: the reason a test produced nothing is already in hand — it just has to be
#: read rather than thrown away.
ERROR_RE = re.compile(r"^iperf3: error - (?P<message>.+?)\s*$", re.MULTILINE)

#: Measured against iperf3 3.16: a client clock 10 s out authenticates, 11 s
#: out is rejected, and the message is the same one a wrong password gets.
AUTH_SKEW_TOLERANCE_S = 10

#: iperf3 3.17 changed the authentication crypto from PKCS#1 v1.5 padding to
#: OAEP. A newer client cannot authenticate to an older server: the server
#: fails to decrypt and reports it as an authorization failure, so a padding
#: mismatch is indistinguishable from a wrong password at the client. The
#: server's own log says "padding check failed", which is the only place the
#: truth appears.
PKCS1_FLAG = "--use-pkcs1-padding"

AUTH_HINT = (
    "the server rejected the credentials. Three things do that: "
    "udp_load.username/password not matching the server's; the rig's clock "
    f"being more than {AUTH_SKEW_TOLERANCE_S}s out, because iperf3 signs every "
    "test with a timestamp and this Pi has no RTC; or this iperf3 being newer "
    "than the server's, which changed the encryption padding in 3.17"
)


@lru_cache(maxsize=1)
def iperf_supports_pkcs1() -> bool:
    """Whether the local iperf3 can fall back to the older padding.

    Only 3.17 and later have the flag at all — earlier versions speak PKCS#1
    and nothing else, so passing it to them would break a working setup.
    """
    try:
        result = subprocess.run(["iperf3", "--help"], capture_output=True,
                                text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return PKCS1_FLAG in (result.stdout + result.stderr)


def is_auth_failure(text: str) -> bool:
    return "authorization failed" in text.lower()


def parse_error(text: str) -> str | None:
    """The reason iperf3 gave for failing, in words worth showing an operator.

    Authorization failures get spelled out because the message iperf3 prints
    names neither of its two causes, and one of them — a clock the Pi cannot
    keep on its own — is not the one anybody checks first.
    """
    match = ERROR_RE.search(text)
    if not match:
        return None
    message = match.group("message")
    if "authorization" in message.lower():
        return AUTH_HINT
    return message


def error_kind(message: str | None, link: str | None) -> str:
    """Which kind of trouble an iperf3 failure is.

    The same words mean different things depending on the link. "Unable to
    connect" in a dead zone is the dead zone; on a link that was getting one
    ping in five through it is a link too weak to finish a handshake; only on
    a link ping called *good* is it a server that has gone. ``link`` is one of
    :data:`LINK_GOOD`, :data:`LINK_POOR`, :data:`LINK_DEAD`, or None when
    nothing can say, and anything short of good is read the harmless way:
    blaming a healthy server costs somebody a restart in the middle of a walk.
    """
    if not message:
        return "no_readings"
    text = message.lower()
    if "authorization" in text:
        return "auth"
    if "busy" in text:
        return "busy"
    if "connection refused" in text:
        return "refused"
    if "unable to connect" in text:
        return {LINK_GOOD: "unreachable", LINK_DEAD: "no_link",
                None: "no_connect"}.get(link, "weak_link")
    return "interrupted"


def phone_words(kind: str, fault: bool) -> tuple[str, str]:
    """A kind of trouble as (what is wrong, what to do about it).

    The chip on the phone has room for about fifteen characters, so the first
    half has to say what happened in its first two or three words. The second
    half is only ever read in the full-width alert, and is empty for anything
    the operator cannot fix by acting.
    """
    restart = "Restart the server."
    if kind == "busy":
        return (("server stuck on a dead test", restart) if fault
                else ("server busy with another test", ""))
    if kind == "refused":
        return (("server's test service is not running", restart) if fault
                else ("server refused the connection", ""))
    if kind == "unreachable":
        return (("cannot reach the server, though the link is up",
                 "Check that the server is running.") if fault
                else ("cannot reach the server", ""))
    return {
        "silent": ("server not answering", restart),
        "auth": ("server rejected the login",
                 "Check the password, the rig's clock and the iperf3 version."),
        "missing": ("iperf3 is not installed on the rig", ""),
        "no_link": ("no link", ""),
        "weak_link": ("link too weak for the test", ""),
        "no_connect": ("cannot connect to the server", ""),
        "no_data": ("connected, nothing arriving", ""),
        "no_answer": ("no answer from the server", ""),
        "interrupted": ("test cut off", ""),
        "no_readings": ("no readings", ""),
        "budget": ("data budget reached", ""),
    }.get(kind, (kind, ""))


#: What goes in the event log for trouble iperf3 describes badly. Its own
#: words for a connect that timed out are "server may have stopped running or
#: use a different port, firewall issue", which blames the server for a dead
#: zone. The few of its words worth keeping go in brackets after ours.
_PLAIN_WORDS = {
    "no_link": ("could not connect to the server, and ping was not getting "
                "through either. There is no link here; the server is not the "
                "problem"),
    "weak_link": ("could not connect to the server, and ping was only getting "
                  "through poorly. The link here is too weak for the test; the "
                  "server is not the problem"),
    "no_connect": "could not connect to the server",
    "unreachable": ("could not connect to the server although ping was getting "
                    "through cleanly"),
    "refused": "the server refused the connection",
    "interrupted": "the test was cut off",
}


def plain_reason(kind: str, said: str | None, fallback: str) -> str:
    """What to log for a failed attempt: our words first, iperf3's after."""
    plain = _PLAIN_WORDS.get(kind)
    if not plain or not said:
        return fallback
    # Everything before the last colon of a connect error is boilerplate.
    detail = said.rsplit(": ", 1)[-1] if "unable to connect" in said else said
    return f"{plain} (iperf3: {detail})"


#: What goes in the event log once trouble of these kinds has become a fault.
#: iperf3's own words describe the symptom; by then the cause is known.
_FAULT_WORDS = {
    "busy": ("the server has answered \"busy\" for over two minutes. A test "
             "that nobody is on the other end of is holding it: restart the "
             "server"),
    "refused": ("the server is refusing connections, so its test service is "
                "not running. Restart the server"),
    "unreachable": ("cannot reach the server although ping is getting through. "
                    "The server is down, or its address or firewall changed"),
}


def address_of(text: str) -> str | None:
    """The IPv4 address out of `ip -o -4 addr show dev eth0` output.

        3: eth0    inet 192.168.1.42/24 brd ... scope global eth0

    Parsed rather than shelled out to in a test, so this can be checked
    without the interface existing.
    """
    for line in text.splitlines():
        parts = line.split()
        if "inet" in parts:
            candidate = parts[parts.index("inet") + 1].split("/")[0]
            if candidate:
                return candidate
    return None


def interface_address(interface: str) -> str | None:
    """What address to bind to, or None if the interface has none right now.

    iperf3's -B takes an *address*, unlike ping's -I which takes an interface
    name — passing it "eth0" fails with "Name or service not known" and the
    whole test never runs. The modem can also be between leases, in which case
    there is nothing to bind to and the routing table is a better answer than
    failing.
    """
    try:
        out = subprocess.run(["ip", "-o", "-4", "addr", "show", "dev", interface],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return address_of(out.stdout) if out.returncode == 0 else None


def parse_bitrate_mbps(value: Any) -> float | None:
    """iperf3's own bitrate spelling ("1.5M", "300k", "750000") as Mbit/s.

    The report needs the *offered* rate to draw against what arrived, and the
    configured string is the only place it is written down.
    """
    if value is None:
        return None
    text = str(value).strip().rstrip("Bb")
    match = re.fullmatch(r"([\d.]+)\s*([KMGT]?)", text, re.IGNORECASE)
    if not match:
        return None
    try:
        return float(match.group(1)) * _BIT_SCALE[match.group(2).upper()]
    except (ValueError, KeyError):
        return None


def _scale(match: re.Match) -> dict[str, Any]:
    fields = match.groupdict()
    return {
        "mbps": float(fields["rate"]) * _BIT_SCALE[fields["rate_unit"]],
        "bytes": int(float(fields["transfer"]) * _BYTE_SCALE[fields["transfer_unit"]]),
        "start_s": float(fields["start"]),
        "end_s": float(fields["end"]),
    }


def parse_interval(line: str) -> dict[str, Any] | None:
    """One iperf3 interval line, from either end, or None if it is not one.

    The per-run summary lines have exactly the same shape as a per-second one
    and are distinguished only by a trailing word, so counting one would put a
    whole-run average into a single second.
    """
    line = line.strip()
    if line.endswith(("sender", "receiver")):
        return None
    match = RECEIVER_RE.match(line)
    if match:
        reading = _scale(match)
        reading.update(jitter_ms=float(match.group("jitter")),
                       loss_pct=float(match.group("loss_pct")),
                       lost=int(match.group("lost")),
                       total=int(match.group("total")))
        return reading
    match = SENDER_RE.match(line)
    if match:
        reading = _scale(match)
        reading.update(jitter_ms=None, loss_pct=None, lost=None,
                       total=int(match.group("total")))
        return reading
    return None


def parse_server_output(text: str) -> list[dict[str, Any]]:
    """The per-second receiver readings iperf3 relays back from the server.

    This is the only way to see uplink jitter and loss from the rig: the
    receiving end is the one that can measure them, and for uplink that end is
    in Dallas.
    """
    _, _, tail = text.partition("Server output:")
    if not tail:
        return []
    readings = []
    for line in tail.splitlines():
        reading = parse_interval(line)
        if reading and reading.get("loss_pct") is not None:
            readings.append(reading)
    return readings


class UdpLoadWorker(Worker):
    """One direction of the teleop load. Two of these make a session."""

    def __init__(self, server: str | None, direction: str = UPLINK,
                 port: int = 5201, bitrate: str = "2M",
                 datagram_bytes: int = DEFAULT_DATAGRAM_BYTES,
                 block_s: float = 10.0, idle_s: float = 0.0,
                 settle_s: float = 3.0, interface: str | None = None,
                 auth_padding: str = "auto",
                 run_data_budget_mb: float | None = 2000.0,
                 username: str = "", password: str = "", public_key_path: str = "",
                 on_backfill: Callable[[list[tuple[float, dict]]], None] | None = None,
                 link_quality: Callable[[], str | None] | None = None,
                 enabled: bool = True, **kwargs: Any) -> None:
        super().__init__(enabled=enabled and bool(server), **kwargs)
        self.direction = direction
        self.server = server
        self.port = int(port)
        self.bitrate = str(bitrate)
        self.datagram_bytes = int(datagram_bytes)
        self.block_s = max(5.0, float(block_s))
        self.idle_s = max(0.0, float(idle_s))
        self.settle_s = max(0.0, float(settle_s))
        self.interface = interface
        self.run_data_budget_mb = run_data_budget_mb
        self.username = username
        self.password = password
        self.public_key_path = public_key_path
        self._on_backfill = on_backfill
        self._proc: subprocess.Popen | None = None
        self._latest: dict[str, Any] | None = None
        self._latest_ts: float = 0.0
        self._run_bytes = 0
        self._total_bytes = 0
        self._backfilled = 0
        self._last_line: str | None = None
        self._warned_no_address = False
        self._last_reason: tuple[str, bool] | None = None
        # How ping is doing, asked of whoever owns ping. It is the only
        # witness to whether a silent server is the server's doing.
        self._link_quality = link_quality
        # What that witness said, second by second, during this attempt.
        self._attempt_link = {LINK_GOOD: 0, LINK_POOR: 0, LINK_DEAD: 0}
        # The last finished attempt, for the phone. Kept between blocks so the
        # chip does not flicker back to "unknown" every thirty seconds.
        self._outcome: dict[str, Any] | None = None
        # kind -> (first seen, last seen), for every kind of trouble met since
        # anything last worked.
        self._trouble_since: dict[str, tuple[float, float]] = {}
        # When this worker was last started, so that a direction which never
        # reports at all does not sit on "first test running" for a whole walk.
        self._started_at: float | None = None
        # Set by the stall watchdog once it has said why it killed a test, so
        # the reader does not then report the same death as "no readings".
        self._stalled = False
        # Whether the server got as far as setting the test up this attempt.
        self._engaged = False
        # Wall clock through which this worker's own traffic still owns the
        # uplink: until then, nothing ping says is about the garage alone.
        self._load_until: float = 0.0
        self._sending_from: float = 0.0
        self._sending_until: float = 0.0
        self._last_output_ts: float = 0.0
        self._stalls = 0
        self._offered_mbps = parse_bitrate_mbps(self.bitrate)
        # None = not yet known. "auto" settles it on the first rejection, which
        # is the only moment the answer is observable from this end.
        self._pkcs1: bool | None = {"pkcs1": True, "oaep": False}.get(
            str(auth_padding).lower())

    @property
    def name(self) -> str:  # type: ignore[override]
        return f"udp_{'up' if self.direction == UPLINK else 'down'}"

    # -- command -------------------------------------------------------------

    def build_command(self) -> list[str]:
        cmd = [
            "iperf3", "-c", str(self.server), "-p", str(self.port),
            "-u",                             # UDP: what teleop actually uses
            "-b", self.bitrate,
            "-l", str(self.datagram_bytes),
            "-i", "1",                        # one reading a second
            "--forceflush",                   # or they arrive in a lump at the end
            # A dead link must end the attempt by itself, or it is silent in
            # exactly the way a wedged server is. See CONNECT_TIMEOUT_MS.
            "--connect-timeout", str(CONNECT_TIMEOUT_MS),
        ]
        if self.direction == DOWNLINK:
            # -R has the server send and the rig receive. The rig is then the
            # receiving end, so jitter and loss come back live.
            cmd += ["-R", "-t", "0"]          # -t 0: for the whole walk
        else:
            # Uplink: the rig sends, so only the server can see what arrived.
            # Run a block and ask it what it saw.
            cmd += ["-t", str(int(self.block_s)), "--get-server-output"]
        # The measured link is the modem's interface, not the Wi-Fi the phone
        # is on. Bind to it so a second route can never quietly send the test
        # somewhere else. If it has no address there is nothing to bind to, and
        # the routing table is a better answer than a test that will not start.
        if self.interface:
            address = interface_address(self.interface)
            if address:
                cmd += ["-B", address]
            elif not self._warned_no_address:
                self._warned_no_address = True
                log.warning("%s has no IPv4 address; letting the routing table "
                            "choose which interface the load test uses",
                            self.interface)
        if self.username and self.public_key_path:
            cmd += ["--username", self.username,
                    "--rsa-public-key-path", self.public_key_path]
            if self._pkcs1:
                cmd.append(PKCS1_FLAG)
        return cmd

    def build_env(self) -> dict[str, str] | None:
        if not (self.username and self.password):
            return None
        return {**os.environ, "IPERF3_PASSWORD": self.password}

    # -- loop ----------------------------------------------------------------

    def run_once(self) -> None:
        if shutil.which("iperf3") is None:
            self._note_trouble("missing", "iperf3 binary not found; install iperf3")
            self.wait(60)
            return
        if self.budget_spent:
            self._set_outcome(OUTCOME_WARN, "budget")
            self.wait(30)
            return
        if self.direction == DOWNLINK:
            self._run_stream()
        else:
            self._run_block()

    def _launch(self) -> subprocess.Popen:
        self._proc = subprocess.Popen(
            self.build_command(), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            text=True, errors="replace", bufsize=1, env=self.build_env())
        self._last_output_ts = time.time()
        self._stalled = False
        self._engaged = False
        self._attempt_link = {LINK_GOOD: 0, LINK_POOR: 0, LINK_DEAD: 0}
        self._watch_for_stall(self._proc)
        return self._proc

    def _watch_for_stall(self, proc: subprocess.Popen) -> None:
        """Kill a test that has stopped producing output.

        Reading a pipe has no timeout of its own, so without this a hung
        iperf3 holds `run_once` open for the rest of the walk. The worker then
        never returns, never restarts, and never says anything, which is
        indistinguishable from working. Killing the process ends the read,
        which lets the normal restart path run and put a reason in the event
        log.
        """
        # How the link was doing for every second this attempt ran. Counted
        # across the whole attempt rather than read once at the end: a link
        # that comes back in the last second must not turn 44 seconds of dead
        # zone into a verdict against the server.
        tally = self._attempt_link

        def watch() -> None:
            while proc.poll() is None:
                if self._stop.wait(1.0):
                    return
                quality = self._link_now()
                if quality in tally:
                    tally[quality] += 1
                if time.time() - self._last_output_ts < STALL_AFTER_S:
                    continue
                if proc.poll() is not None:
                    return
                self._stalls += 1
                log.warning("%s produced no output for %.0fs; killing it",
                            self.name, STALL_AFTER_S)
                # Say why before the kill. The reader wakes the moment the
                # process dies and must find the verdict already given, or it
                # reports the same death a second time as "no readings".
                self._note_trouble(*self._stall_verdict())
                self._stalled = True
                try:
                    proc.kill()
                except OSError:
                    pass
                return

        threading.Thread(target=watch, name=f"{self.name}-stall",
                         daemon=True).start()

    def _link_now(self) -> str | None:
        """How ping is doing this second, or None when nothing can say."""
        if self._link_quality is None:
            return None
        try:
            return self._link_quality()
        except Exception:  # noqa: BLE001 - a witness that falls over is just absent
            return None

    def _link_during_attempt(self) -> str | None:
        """What the link was for the attempt that has just ended.

        Good or dead only if it was that for nearly all of it; anything mixed
        is "poor". An attempt too short to have been watched falls back to how
        the link is at this moment.
        """
        tally = self._attempt_link
        seen = sum(tally.values())
        if seen < MIN_LINK_SAMPLES:
            return self._link_now()
        for quality in (LINK_GOOD, LINK_DEAD):
            if tally[quality] / seen >= LINK_UP_SHARE:
                return quality
        return LINK_POOR

    def _stall_verdict(self) -> tuple[str, str]:
        """Why a test went silent, as (kind of trouble, words for the log).

        Three different things look identical from here, and only one of them
        is somebody's job to fix:

        * The test was running and stopped: the link dropped under it. That is
          a dead zone doing what dead zones do.
        * The server never set the test up, and ping was not getting through
          either: there was no link to set it up over.
        * The server never set the test up while ping was *good*. The
          connection was made (a connect that cannot complete ends by itself,
          see :data:`CONNECT_TIMEOUT_MS`) and nothing came of it. That is a
          stuck server. On 2026-10-15 it went on for two whole walks, logged
          once a minute as "stopped responding", and nobody could tell it from
          a bad garage.

        Good, not merely alive. iperf3 prints nothing until its first interval
        line, a second into the test, so silence means only that the handshake
        never finished, and on a link getting one ping in five through a
        handshake can spend a quarter of a minute in TCP retries and then lose
        the single datagram that starts the test. That is the garage. Calling
        it a stuck server would tell an operator to restart a healthy one, in
        exactly the marginal places a survey is for, and would put that in a
        report that cannot be corrected afterwards.
        """
        side = "uplink" if self.direction == UPLINK else "downlink"
        quiet = f"{STALL_AFTER_S:.0f}s"
        if self._engaged:
            return ("interrupted",
                    "the test went quiet part-way through and was restarted "
                    f"after {quiet}; the link most likely dropped")
        link = self._link_during_attempt()
        if link == LINK_GOOD:
            return ("silent",
                    f"the test connected to the server and then heard nothing "
                    f"back for {quiet}, while ping was getting through cleanly. "
                    f"The server's {side} test service is stuck: restart the "
                    f"server. There will be no {side} readings until then")
        if link == LINK_DEAD:
            return ("no_link",
                    f"no answer from the server for {quiet}, and ping was not "
                    "getting through either. There is no link here; the server "
                    "is not the problem")
        return ("no_answer",
                f"no answer from the server for {quiet}"
                + (", on a link ping was only getting through poorly. Most "
                   "likely the place, not the server" if link == LINK_POOR else ""))

    # -- what happened to the last attempt -----------------------------------

    def _set_outcome(self, state: str, kind: str, fault: bool = False) -> None:
        detail, advice = phone_words(kind, fault)
        self._outcome = {"state": state, "kind": kind, "detail": detail,
                         "advice": advice, "ts": time.time()}

    def _note_ok(self) -> None:
        """Readings arrived: whatever was wrong is over."""
        recovered = bool(self._last_reason and self._last_reason[1])
        self._trouble_since.clear()
        self._last_reason = None
        self._outcome = {"state": OUTCOME_OK, "kind": "ok", "detail": "measuring",
                         "advice": "", "ts": time.time()}
        if recovered:
            self.emit("info", f"{self.name}: working again")

    def _note_trouble(self, kind: str, reason: str) -> None:
        """Record a failed attempt, for the phone, the log and the worker.

        Trouble becomes a *fault* only once it has lasted as long as
        :data:`ESCALATE_AFTER_S` says its kind must, and most kinds never do.
        Each is said once, and once more if it turns into a fault. Uplink
        attempts come round twice a minute, so anything repeated per attempt
        would bury the rest of the event log within one walk.
        """
        now = time.time()
        first, last = self._trouble_since.get(kind, (now, now))
        if now - last > TROUBLE_FORGOTTEN_AFTER_S:
            first = now
        self._trouble_since[kind] = (first, now)
        wait = ESCALATE_AFTER_S.get(kind)
        fault = wait is not None and now - first >= wait
        if fault:
            reason = _FAULT_WORDS.get(kind, reason)
        self._set_state(STATE_FAILED if fault else STATE_DEGRADED, reason)
        self._set_outcome(OUTCOME_FAIL if fault else OUTCOME_WARN, kind, fault)
        if (kind, fault) != self._last_reason:
            self._last_reason = (kind, fault)
            self.emit("error" if fault else "warning", f"{self.name}: {reason}")

    def current_outcome(self, now: float | None = None) -> dict[str, Any] | None:
        """The last finished attempt, or None before there has been one.

        A good result does not stay good for ever. Uplink reports once per
        burst, so its last result may be most of a minute old and still
        current; downlink reports every second, so silence of a quarter of a
        minute means the stream has stopped delivering.
        """
        now = time.time() if now is None else now
        outcome = self._outcome
        if outcome is None:
            # Nothing yet is normal for the first burst, and not for a whole
            # walk. Past the longest a first verdict can take, say so, or the
            # chip would sit on "first test running" until End.
            waited = now - self._started_at if self._started_at else 0.0
            patience = STALL_AFTER_S + 15.0 + (
                self.block_s + self.idle_s if self.direction == UPLINK else 0.0)
            if waited <= patience:
                return None
            return {"state": OUTCOME_WARN, "kind": "stale",
                    "detail": f"nothing yet after {waited:.0f}s", "advice": "",
                    "ts": self._started_at}
        if outcome["state"] != OUTCOME_OK:
            return outcome
        age = now - outcome["ts"]
        limit = (STALE_DOWNLINK_OUTCOME_S if self.direction == DOWNLINK
                 else self.block_s + self.idle_s + STALL_AFTER_S + 15.0)
        if age <= limit:
            return outcome
        return {"state": OUTCOME_WARN, "kind": "stale",
                "detail": f"no readings for {age:.0f}s", "advice": "",
                "ts": outcome["ts"]}

    def start(self) -> None:
        # A fresh start has nothing to say yet. Carrying the last walk's
        # verdict into this one would show a fault that may be long gone, or
        # sit on top of one that is new.
        # What stays is how long each kind of trouble has been going on: a
        # server that was "busy" before a pause and is still busy after it has
        # been busy all that time, and a pause must not let it off.
        if self.enabled and not (self._thread and self._thread.is_alive()):
            self._outcome = None
            self._last_reason = None
            self._started_at = time.time()
        super().start()

    def _run_stream(self) -> None:
        """Downlink: one long-lived stream, read a line at a time."""
        proc = self._launch()
        # Enough of a tail to hold whatever iperf3 said on its way out. A
        # stream that never yields a reading has a reason, and it is here.
        tail: deque[str] = deque(maxlen=20)
        measured = False
        empty_since: float | None = None
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                if self.stopping:
                    return
                tail.append(line)
                reading = self._consume(line)
                if reading and reading.get("loss_pct") is not None:
                    measured = True
                    # A stream that has stopped delivering still prints a line
                    # a second: no bytes, no datagrams, and "0%" lost, because
                    # none of nothing went missing. Taking that as a reading
                    # recorded the deadest seconds of a walk as loss-free, and
                    # would keep the chip green. It is no reading at all; the
                    # last real one goes stale and the second is left empty,
                    # which the report counts as no stream.
                    if reading.get("total"):
                        self._latest = reading
                        self._latest_ts = time.time()
                        empty_since = None
                        self._note_ok()
                    else:
                        # Still connected, still printing, and nothing in it.
                        # The stream is left alone, because it picks up by
                        # itself when the link returns and a reconnect from
                        # inside a dead zone can find the server still holding
                        # the old test. But the phone is told.
                        empty_since = empty_since or time.time()
                        if time.time() - empty_since >= STALE_DOWNLINK_OUTCOME_S:
                            self._note_trouble(
                                "no_data",
                                "the stream is connected but nothing has "
                                f"arrived for {STALE_DOWNLINK_OUTCOME_S:.0f}s")
                if self.budget_spent:
                    self.emit("warning", f"{self.name}: run data budget reached")
                    self._set_outcome(OUTCOME_WARN, "budget")
                    return
            # A test the watchdog killed has already been explained, and one
            # that was stopped on purpose has nothing to explain.
            if self._stalled or self.stopping:
                return
            self._fail_with_reason(
                "".join(tail),
                "the stream ended" if measured else "the stream produced no readings")
        finally:
            self._terminate()

    def _fail_with_reason(self, text: str, fallback: str) -> None:
        """Report why a test produced nothing, saying it out loud once.

        Once, because uplink blocks restart every 30 seconds: a misconfigured
        credential would otherwise write the same line into the event log twice
        a minute for the length of a walk and bury everything else in it.
        """
        # A rejection is the one moment this end can learn that the server is
        # older than this iperf3 and wants the pre-3.17 padding. Try it once,
        # rather than making someone read the *server's* log to find out.
        if (is_auth_failure(text) and self._pkcs1 is None
                and self.username and iperf_supports_pkcs1()):
            self._pkcs1 = True
            self._set_state(STATE_DEGRADED, "retrying with pre-3.17 padding")
            self.emit("warning",
                      f"{self.name}: the server would not accept this iperf3's "
                      "encryption; retrying with the padding used before 3.17")
            return

        # What iperf3 said decides what kind of trouble this is, and how the
        # link was while it tried decides whose: "unable to connect" in a dead
        # zone is the dead zone, and the same words on a clean link are a
        # server that has gone. The link over the whole attempt, not at its
        # last instant: ten seconds of nothing with one ping reply at the end
        # is a dead zone ending.
        match = ERROR_RE.search(text)
        said = match.group("message") if match else None
        kind = error_kind(said, self._link_during_attempt())
        self._note_trouble(kind, plain_reason(kind, said, parse_error(text) or fallback))

    def _run_block(self) -> None:
        """Uplink: a fixed block, then recover what the far end actually saw.

        The rig's own lines say only what it put on the wire. They are still
        worth reading as they arrive, because the wall-clock time each one
        lands is what lets the server's readings — which come back all at once
        at the end — be placed on the right seconds afterwards.
        """
        started = time.time()
        interval_ends: list[float] = []
        output: list[str] = []
        sending_from: float | None = None
        proc = self._launch()
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                if self.stopping:
                    return
                output.append(line)
                reading = self._consume(line)
                now = time.time()
                began: float | None = None
                if reading and reading.get("loss_pct") is None:
                    interval_ends.append(now)
                    # An interval line says how far into the test it ends, so
                    # each one is an estimate of when the test began. iperf3
                    # prints nothing at all until the first of them, a second
                    # in, so "now" would be a second late, and lines can be
                    # held up further but never arrive early: the earliest
                    # estimate is the right one.
                    began = now - reading["end_s"]
                elif self._engaged and sending_from is None:
                    began = now
                if began is not None and (sending_from is None
                                          or began < sending_from - 0.1):
                    # The server has set the test up and datagrams are going
                    # out. Only from here does this test own the uplink: until
                    # the modem's buffer has drained again, whatever ping sees
                    # is partly the test's doing rather than the garage's.
                    #
                    # Not from launch. An attempt that never gets this far
                    # (a dead link, a server that does not answer) puts
                    # nothing on the uplink, and marking its seconds as loaded
                    # took them out of dead-zone detection in exactly the
                    # places it is there for. It also counted every handshake
                    # as a second in which the rig "sent and nothing arrived".
                    sending_from = began
                    self._sending_from = sending_from
                    self._sending_until = sending_from + self.block_s
                    self._load_until = self._sending_until + self.settle_s
                if self.budget_spent:
                    self.emit("warning", f"{self.name}: run data budget reached")
                    return
            proc.wait(timeout=10)
        finally:
            self._terminate()
            if sending_from is not None:
                # Sending stopped when the test did, which is early if it was
                # cut off; the buffer then needs its few seconds to empty.
                now = time.time()
                self._sending_until = min(self._sending_until, now)
                self._load_until = now + self.settle_s

        # A test the watchdog killed has already been explained, and reading
        # its output again would only replace that with "no readings". One
        # that was stopped on purpose has nothing to explain: every End and
        # every Pause lands in the middle of a burst as often as not.
        if self._stalled or self.stopping:
            return
        self._absorb_server_output("".join(output), started, interval_ends)
        # Then shut up for a while. The block is short and the gap is long so
        # that ping spends most of the walk measuring the garage instead of the
        # queue this test just built in the modem: at 3 Mbit/s into a link that
        # can carry 1.3, the buffer fills in about a second and everything
        # sharing it reads as a dead zone until it drains.
        if self.idle_s:
            self.wait(self.idle_s)

    def _absorb_server_output(self, text: str, started: float,
                              interval_ends: list[float]) -> None:
        readings = parse_server_output(text)
        if not readings:
            self._fail_with_reason(text, "the server returned no readings")
            return

        jitter, loss, mbps = FIELDS[UPLINK]
        # When the test began, from when the rig's own interval lines landed.
        # Line n ends n seconds in, so each is an estimate of the start, and
        # the earliest is the one to believe: a line can be held up, and the
        # last of every burst is, until the end-of-test exchange has gone
        # round, but none arrives early. Timing each reading by its own line
        # instead put that last second one sample late on any real link, and
        # left a second in every burst marked "sent" with nothing on it. That
        # absorbs the control-channel setup too, which ``started`` does not.
        if interval_ends:
            began = min(end - (n + 1) for n, end in enumerate(interval_ends))
        else:
            began = started
        backfill: list[tuple[float, dict]] = []
        for reading in readings:
            ts = began + (reading["start_s"] + reading["end_s"]) / 2
            backfill.append((ts, {
                jitter: reading["jitter_ms"],
                loss: reading["loss_pct"],
                mbps: round(reading["mbps"], 3),
                # A second the server received datagrams in is a second the
                # rig was sending, whatever was known when the sample was
                # written. The first second of every burst is not known then:
                # iperf3 says nothing until it is over.
                "uplink_loaded": LOAD_SENDING,
            }))

        self._latest = readings[-1]
        self._latest_ts = time.time()
        self._backfilled += len(backfill)
        if self._on_backfill:
            self._on_backfill(backfill)
        self._note_ok()

    def _consume(self, line: str) -> dict[str, Any] | None:
        # Any output at all, parseable or not, means the test is still alive.
        self._last_output_ts = time.time()
        self._last_line = line.strip() or self._last_line
        reading = parse_interval(line)
        if reading is None:
            # "[  5] local ... connected to ..." is the first line that needs
            # the server to have set the test up. "Connecting to host" does
            # not count: it only says the server replied once.
            if "connected to" in line:
                self._engaged = True
            return None
        self._engaged = True
        self._run_bytes += reading["bytes"]
        self._total_bytes += reading["bytes"]
        return reading

    def _terminate(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    def on_stop(self) -> None:
        self._terminate()

    # -- who owns the link right now -----------------------------------------

    @property
    def loading(self) -> bool:
        """Whether this worker's own traffic is on the uplink at this moment.

        The settling seconds count too. The modem holds roughly 1.75 Mbit of
        buffer (measured 2026-09-18 from a run's own median RTT), so ping keeps
        seeing the queue for a second or two after the last datagram was handed
        over, and a sample taken then is still describing the test.

        Downlink never claims the link: ``-R`` has the *server* send, so the
        rig's uplink stays empty and the direction that fills up is the one
        with room to spare.
        """
        return self.load_state != LOAD_IDLE

    @property
    def load_state(self) -> int:
        """Sending, settling, or neither — see :data:`LOAD_SENDING`."""
        if self.direction != UPLINK:
            return LOAD_IDLE
        now = time.time()
        if now >= self._load_until:
            return LOAD_IDLE
        sending = self._sending_from <= now < self._sending_until
        return LOAD_SENDING if sending else LOAD_SETTLING

    @property
    def offered_mbps(self) -> float | None:
        """The rate this worker asks the link for, in Mbit/s.

        The report needs it to tell a spot that could not carry the offered
        load apart from one that carried it cleanly.
        """
        return self._offered_mbps

    # -- budget --------------------------------------------------------------

    @property
    def budget_spent(self) -> bool:
        if self.run_data_budget_mb is None:
            return False
        return self._run_bytes >= float(self.run_data_budget_mb) * 1024 ** 2

    def begin_run(self) -> None:
        self._run_bytes = 0
        self._backfilled = 0
        self._trouble_since.clear()

    # -- readings ------------------------------------------------------------

    def sample_fields(self) -> dict[str, Any]:
        """This second's reading, for the directions that can report live.

        Uplink cannot: its numbers only exist at the far end and arrive at the
        end of a block, so they are written onto their samples afterwards
        rather than through here.
        """
        jitter, loss, mbps = FIELDS[self.direction]
        if (self.direction == UPLINK or self._latest is None
                or self._latest.get("loss_pct") is None
                or (time.time() - self._latest_ts) > STALE_AFTER_S):
            return {jitter: None, loss: None, mbps: None}
        return {
            jitter: self._latest["jitter_ms"],
            loss: self._latest["loss_pct"],
            mbps: round(self._latest["mbps"], 3),
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "direction": self.direction,
            "server": self.server,
            "port": self.port,
            "bitrate": self.bitrate,
            "offered_mbps": self._offered_mbps,
            "block_s": self.block_s if self.direction == UPLINK else None,
            "idle_s": self.idle_s if self.direction == UPLINK else None,
            "loading": self.loading,
            "load_state": self.load_state,
            "pkcs1_padding": self._pkcs1,
            "live": self.direction == DOWNLINK,
            "loss_pct": (self._latest or {}).get("loss_pct"),
            "jitter_ms": (self._latest or {}).get("jitter_ms"),
            "backfilled_samples": self._backfilled,
            "outcome": self.current_outcome(),
            "stalls": self._stalls,
            "run_mb": round(self._run_bytes / 1024 ** 2, 1),
            "total_mb": round(self._total_bytes / 1024 ** 2, 1),
            "budget_mb": self.run_data_budget_mb,
            "budget_spent": self.budget_spent,
            "last_line": self._last_line,
        }
