#!/usr/bin/env python3
"""Restart an iperf3 server that has wedged itself.

On 2026-10-15 the uplink iperf3 instance on the survey server was stuck for two
whole walks. Every test connected and none got an answer, the rig could not
tell that from a bad garage, and both walks came back with no uplink data. It
stayed that way until somebody rebooted the box.

How it gets there, reproduced on iperf3 3.16 on 2026-11-22: a UDP test whose
one "connect" datagram never arrives leaves the server waiting for it. If any
other TCP connection reaches the port while it waits (the rig's next burst, a
port scanner, anything) the server's main thread falls into a `select` loop
that never sleeps and never ends. It held for 14.5 minutes before the test was
stopped. A rig that loses its link just as a burst begins is all it takes, and
walking into places with no link is what the rig is for.

**This does not connect to the servers to check on them, and nothing here ever
should.** A connection is the trigger. A health probe would wedge the very
server it was asking after.

What it reads instead is the CPU time of each server's *main thread*, which
needs no connection at all. Measured on the same day:

    idle                              main thread  0.0%
    receiving a 3 Mbit/s uplink test  main thread  0.6%
    sending a 5 or 30 Mbit/s downlink main thread  2.0%   (a worker thread: 99.8%)
    wedged                            main thread 99.4%   (no worker thread at all)

The whole process's CPU is useless for this: an iperf3 server that is sending
paces its datagrams in a busy loop, so the downlink instance sits at a full
core for the length of every walk, perfectly healthy. Only the main thread
tells the two apart, and the gap between 2% and 99% is the whole margin.

That worker thread only exists from iperf3 3.16, which made the streams
threads. On anything older the main thread does the sending itself and looks
exactly like a wedge, so this refuses to act on older versions rather than
restart a server in the middle of a test.

It also restarts every instance once a night. A downlink test whose rig
vanished is never dropped by the server (measured: still transmitting at nobody
nineteen minutes later, and answering "busy" to everyone else), and nothing
visible from here separates that from a walk in progress. The nightly restart
is what stops yesterday's dead test from refusing tomorrow's walk.

Runs as root because it restarts other units. It reads /proc and calls
systemctl, and does nothing else.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from typing import Callable

#: Share of one core the main thread has to be using before it counts. A
#: healthy server's main thread was never seen above 2%, a wedged one never
#: below 99%, so this sits far from both: low enough to still catch a wedge on
#: a one-core box that is busy serving an upload at the same time.
PINNED_SHARE = 0.5

#: How long it has to stay there. Long enough that nothing transient reaches
#: it, short enough that the rig's next burst but one finds a working server.
PINNED_FOR_S = 30.0

POLL_S = 5.0

#: Never restart the same instance twice inside this, whatever it looks like.
RESTART_HOLDOFF_S = 120.0

#: 09:00 UTC is one or two in the morning on the Pacific coast and four or five
#: on the Atlantic. Nobody is walking a garage.
NIGHTLY_UTC_HOUR = 9

#: The first iperf3 whose streams are threads. See the module docstring.
FIRST_THREADED = (3, 16)

UNIT = "viabot-iperf3@{port}"


def main_thread_ticks(pid: int) -> int | None:
    """CPU time the process's *main* thread has used, in clock ticks.

    The main thread's id is the process id, and its own stat file counts only
    its own time; /proc/<pid>/stat would add every worker thread in.
    """
    try:
        with open(f"/proc/{pid}/task/{pid}/stat") as handle:
            stat = handle.read()
    except OSError:
        return None
    # The command name is in brackets and may contain spaces or brackets of
    # its own, so count fields from the last closing bracket.
    fields = stat.rsplit(")", 1)[-1].split()
    try:
        return int(fields[11]) + int(fields[12])      # utime + stime
    except (IndexError, ValueError):
        return None


def parse_version(text: str) -> tuple[int, int] | None:
    """(3, 16) out of "iperf 3.16 (cJSON 1.7.15)"."""
    match = re.search(r"iperf\s+(\d+)\.(\d+)", text)
    return (int(match.group(1)), int(match.group(2))) if match else None


class Watch:
    """One server instance, and whether its main thread has been spinning.

    Pure bookkeeping: it is told what it would have read and when, and says
    whether that amounts to a wedge. Reading /proc and restarting things is
    done by whoever owns it, so this can be tested with numbers.
    """

    def __init__(self, ticks_per_second: int) -> None:
        self.ticks_per_second = ticks_per_second
        self.pid: int | None = None
        self.ticks = 0
        self.at = 0.0
        self.pinned_since: float | None = None
        self.restarted_at = 0.0
        self.share: float | None = None

    def forget(self) -> None:
        self.pid = None
        self.pinned_since = None
        self.share = None

    def observe(self, pid: int | None, ticks: int | None, now: float) -> bool:
        """Take one reading. True means: this instance is wedged, restart it."""
        if pid is None or ticks is None:
            self.forget()
            return False
        if pid != self.pid or ticks < self.ticks or now <= self.at:
            # A different process, or a clock that went backwards: start again
            # rather than compare two readings that do not belong together.
            self.pid, self.ticks, self.at = pid, ticks, now
            self.pinned_since = None
            self.share = None
            return False

        began = self.at
        self.share = (ticks - self.ticks) / self.ticks_per_second / (now - began)
        self.ticks, self.at = ticks, now
        if self.share < PINNED_SHARE:
            self.pinned_since = None
            return False
        if self.pinned_since is None:
            # Pinned over the interval that just ended, so since its start.
            self.pinned_since = began
        if now - self.pinned_since < PINNED_FOR_S:
            return False
        if now - self.restarted_at < RESTART_HOLDOFF_S:
            return False
        return True

    def restarted(self, now: float) -> None:
        self.restarted_at = now
        self.forget()


def nightly_due(now: float, last_day: int | None, hour: int) -> bool:
    """Whether the once-a-night restart should happen at this moment."""
    if hour < 0:
        return False
    moment = time.gmtime(now)
    return moment.tm_hour == hour and moment.tm_yday != last_day


def unit_pid(unit: str) -> int | None:
    try:
        out = subprocess.run(["systemctl", "show", "-p", "MainPID", "--value", unit],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        pid = int(out.stdout.strip())
    except ValueError:
        return None
    return pid or None          # systemd reports 0 for "not running"


def restart_unit(unit: str) -> bool:
    try:
        done = subprocess.run(["systemctl", "restart", unit], timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def say(message: str) -> None:
    # stdout is the journal: `journalctl -u viabot-iperf3-heal` is the record
    # of how often this has had to step in.
    print(message, flush=True)


def run(ports: list[int], nightly_hour: int = NIGHTLY_UTC_HOUR,
        get_pid: Callable[[str], int | None] = unit_pid,
        get_ticks: Callable[[int], int | None] = main_thread_ticks,
        restart: Callable[[str], bool] = restart_unit,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        cycles: int | None = None) -> None:
    """Watch every instance until told to stop (``cycles`` is for tests)."""
    ticks_per_second = os.sysconf("SC_CLK_TCK")
    watches = {UNIT.format(port=port): Watch(ticks_per_second) for port in ports}
    # Started inside the nightly hour, that night is already spent: a deploy
    # at 09:10 UTC must not restart servers somebody has just checked.
    began = time.gmtime(clock())
    last_night: int | None = began.tm_yday if began.tm_hour >= nightly_hour else None
    say(f"watching {', '.join(watches)}; a main thread above "
        f"{PINNED_SHARE:.0%} of a core for {PINNED_FOR_S:.0f}s is a wedge")

    done = 0
    while cycles is None or done < cycles:
        done += 1
        now = clock()
        if nightly_due(now, last_night, nightly_hour):
            last_night = time.gmtime(now).tm_yday
            for unit, watch in watches.items():
                ok = restart(unit)
                watch.restarted(now)
                say(f"{unit}: nightly restart {'done' if ok else 'FAILED'}")
        for unit, watch in watches.items():
            pid = get_pid(unit)
            if watch.observe(pid, get_ticks(pid) if pid else None, now):
                share = watch.share or 0.0
                ok = restart(unit)
                watch.restarted(now)
                say(f"{unit}: main thread at {share:.0%} of a core for "
                    f"{PINNED_FOR_S:.0f}s, which a working server never does. "
                    f"Wedged; restart {'done' if ok else 'FAILED'}")
        sleep(POLL_S)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--ports", type=int, nargs="+", default=[5201, 5202])
    parser.add_argument("--nightly-utc-hour", type=int, default=NIGHTLY_UTC_HOUR,
                        help="hour (UTC) of the once-a-night restart; -1 for never")
    args = parser.parse_args(argv)

    try:
        text = subprocess.run(["iperf3", "--version"], capture_output=True,
                              text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        text = ""
    version = parse_version(text)
    if version is None or version < FIRST_THREADED:
        # Before 3.16 the main thread does the sending, so a healthy downlink
        # test looks exactly like a wedge. Doing nothing is the safe answer;
        # exiting would only make systemd start this again every ten seconds.
        say(f"iperf3 here is {version or 'unknown'}; a wedge cannot be told from "
            "a test in progress before 3.16, so nothing will be restarted")
        while True:
            time.sleep(3600)
    run(args.ports, args.nightly_utc_hour)
    return 0


if __name__ == "__main__":
    sys.exit(main())
