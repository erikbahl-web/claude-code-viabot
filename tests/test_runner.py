import time

from viabot_survey.runner import classify, slugify
from viabot_survey.workers import udpload


THRESHOLDS = {"degraded_loss_pct": 5, "degraded_rtt_ms": 200,
              "bad_loss_pct": 20, "bad_rtt_ms": 500, "dead_after_s": 5}


def test_clean_link_is_good():
    assert classify(0.0, 48.0, THRESHOLDS) == "good"


def test_latency_alone_degrades():
    assert classify(0.0, 250.0, THRESHOLDS) == "degraded"
    assert classify(0.0, 600.0, THRESHOLDS) == "bad"


def test_loss_alone_degrades():
    assert classify(10.0, 50.0, THRESHOLDS) == "degraded"
    assert classify(30.0, 50.0, THRESHOLDS) == "bad"


def test_total_loss_becomes_dead_only_after_the_configured_streak():
    """A one-second blip while walking past a pillar is not a dead zone."""
    assert classify(100.0, None, THRESHOLDS, dead_streak_s=1.0) == "bad"
    assert classify(100.0, None, THRESHOLDS, dead_streak_s=4.0) == "bad"
    assert classify(100.0, None, THRESHOLDS, dead_streak_s=5.0) == "dead"


def test_no_data_is_unknown_not_good():
    assert classify(None, None, THRESHOLDS) == "unknown"


def test_slugify_makes_safe_run_ids():
    assert slugify("Sunset Garage L2") == "sunset-garage-l2"
    assert slugify("  Ramp / Level 3!! ") == "ramp-level-3"
    assert slugify("") == ""
    assert len(slugify("x" * 100)) == 32


def test_sample_collection_records_signal_and_video_pointers(runner, storage, monkeypatch):
    monkeypatch.setattr("viabot_survey.runner.sysinfo.clock_synced", lambda: True)
    runner.ping.enabled = True
    monkeypatch.setattr(runner.ping, "snapshot",
                        lambda now=None: {"rtt_ms": 55.0, "loss_pct": 0.0, "jitter_ms": 2.0})
    runner.router.enabled = True
    monkeypatch.setattr(runner.router, "sample_fields",
                        lambda: {"rsrp": -92.0, "rsrq": None, "sinr": 9.0, "rssi": None,
                                 "band": "n41", "cell_id": "ABC", "tech": "5G"})
    monkeypatch.setattr(runner.camera, "locate", lambda ts: ("20260911-140000.mkv", 12.5))

    run = runner.start_run(label="test")
    runner.collect_sample()
    rows = list(storage.iter_samples(run["id"]))
    runner.stop_run()

    assert len(rows) == 1
    assert rows[0]["status"] == "good"
    assert rows[0]["rsrp"] == -92.0
    assert rows[0]["band"] == "n41"
    assert rows[0]["video_file"] == "20260911-140000.mkv"
    assert rows[0]["video_offset_s"] == 12.5
    assert rows[0]["clock_synced"] == 1


def test_dead_streak_accumulates_then_resets(runner, monkeypatch):
    runner.ping.enabled = True
    state = {"loss": 100.0}
    monkeypatch.setattr(runner.ping, "snapshot",
                        lambda now=None: {"rtt_ms": None, "loss_pct": state["loss"],
                                          "jitter_ms": None})
    for _ in range(6):
        sample = runner.collect_sample()
    assert sample["status"] == "dead"
    state["loss"] = 0.0
    assert runner.collect_sample()["dead_streak_s"] == 0.0


def test_second_run_is_refused_while_one_is_active(runner):
    runner.start_run(label="First")
    try:
        runner.start_run(label="Second")
    except RuntimeError as exc:
        assert "already in progress" in str(exc)
    finally:
        runner.stop_run()


def test_pausing_stops_samples_being_recorded(runner, storage, monkeypatch):
    """Paused time must leave no trace at all — the headline result is a
    percentage of time walked, so standing still must not count as coverage."""
    runner.ping.enabled = True
    monkeypatch.setattr(runner.ping, "snapshot",
                        lambda now=None: {"rtt_ms": 50.0, "loss_pct": 0.0, "jitter_ms": 1.0})
    run = runner.start_run(label="Pause test")
    runner.collect_sample()
    runner.collect_sample()
    assert runner.pause() is True
    runner.collect_sample()
    runner.collect_sample()
    runner.collect_sample()
    assert runner.resume() is True
    runner.collect_sample()
    rows = list(storage.iter_samples(run["id"]))
    runner.stop_run()
    assert len(rows) == 3          # the three paused seconds are simply absent


def test_pause_is_idempotent_and_needs_a_run(runner):
    assert runner.pause() is False
    assert runner.resume() is False
    runner.start_run(label="L2")
    assert runner.pause() is True
    assert runner.pause() is False
    assert runner.resume() is True
    assert runner.resume() is False
    runner.stop_run()


def test_startup_closes_a_run_left_open_by_a_power_cut(config, storage):
    """A run still open in the database means the Pi lost power mid-walk.
    Reopening it would leave a gap in the timeline that reads like a dead zone."""
    from viabot_survey.runner import SurveyRunner

    storage.create_run("orphan", started_at=time.time() - 600)
    survey = SurveyRunner(config, storage)
    try:
        survey.start()
        assert storage.get_run("orphan")["ended_at"] is not None
        assert storage.active_run() is None
    finally:
        survey.shutdown()


def test_an_interrupted_run_is_analysed_not_discarded(config, storage, monkeypatch):
    """Power dies mid-walk. Everything recorded up to that point is good data,
    and throwing it away means driving back to the garage."""
    import threading

    from viabot_survey.runner import SurveyRunner

    base = time.time() - 300
    storage.create_run("cut-off", started_at=base)
    for offset in range(100):
        dead = 40 <= offset < 70
        storage.add_sample("cut-off", base + offset,
                           rtt_ms=None if dead else 45.0,
                           loss_pct=100.0 if dead else 0.0,
                           status="dead" if dead else "good")

    survey = SurveyRunner(config, storage)
    done = threading.Event()
    original = survey.analyse_run

    def watched(*args, **kwargs):
        try:
            return original(*args, **kwargs)
        finally:
            done.set()

    monkeypatch.setattr(survey, "analyse_run", watched)
    try:
        survey.start()
        assert done.wait(timeout=10), "the interrupted run was never analysed"
    finally:
        survey.shutdown()

    run = storage.get_run("cut-off")
    assert run["ended_at"] is not None
    assert run["runnable_pct"] == 70.0          # 30 of 100 seconds unusable
    zones = storage.list_dead_zones("cut-off")
    assert len(zones) == 1
    assert zones[0]["duration_s"] == 30.0
    assert any("cut off mid-walk" in e["message"] for e in storage.recent_events())


def test_recovery_is_silent_when_nothing_was_interrupted(config, storage):
    from viabot_survey.runner import SurveyRunner

    survey = SurveyRunner(config, storage)
    try:
        survey.start()
        assert storage.active_run() is None
    finally:
        survey.shutdown()
    assert not any("cut off" in e["message"] for e in storage.recent_events())


def test_deleting_a_run_frees_the_card_not_just_the_database(runner, storage, tmp_path):
    """The video is the bulk of a survey. Deleting the rows alone frees a few
    hundred kilobytes and leaves a few hundred megabytes — and a full card is
    what stops the next walk from recording at all."""
    run_id = "20250911-194640-level-2"
    storage.create_run(run_id, label="Level 2")
    storage.add_sample(run_id, 1000.0, rtt_ms=40.0)
    storage.add_event("something happened", run_id=run_id)

    from pathlib import Path
    for directory, name in ((runner.config.video_dir / run_id, "seg.mkv"),
                            (Path(runner.config.data_dir) / "clips" / run_id, "a.mp4"),
                            (Path(runner.config.data_dir) / "reports" / run_id, "index.html")):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / name).write_bytes(b"x" * 4096)

    result = runner.discard_run(run_id)

    assert storage.get_run(run_id) is None
    assert not (runner.config.video_dir / run_id).exists()
    assert not (Path(runner.config.data_dir) / "clips" / run_id).exists()
    assert not (Path(runner.config.data_dir) / "reports" / run_id).exists()
    assert result["freed_mb"] >= 0
    assert len(result["removed"]) == 3
    # And nothing is left pointing at a run that no longer exists.
    assert not [e for e in storage.recent_events(50) if e["run_id"] == run_id]


def test_a_second_under_our_own_uplink_load_is_recorded_as_such(runner, storage, monkeypatch):
    """The whole exclusion rests on this flag reaching the database. Without it
    the report has no way to tell a garage's dead zone from the queue the rig
    built in its own modem."""
    runner.ping.enabled = True
    monkeypatch.setattr(runner.ping, "snapshot",
                        lambda now=None: {"rtt_ms": 1400.0, "loss_pct": 90.0,
                                          "jitter_ms": 200.0})
    runner.udp_up.enabled = True
    monkeypatch.setattr(runner.udp_up, "start", lambda: None)
    monkeypatch.setattr(type(runner.udp_up), "load_state",
                        property(lambda self: udpload.LOAD_SENDING))

    run = runner.start_run(label="loaded")
    runner.collect_sample()
    rows = list(storage.iter_samples(run["id"]))
    runner.stop_run()

    assert rows[0]["uplink_loaded"] == udpload.LOAD_SENDING


def test_a_loaded_second_never_becomes_a_dead_zone_end_to_end(runner, storage, monkeypatch):
    """From the sample loop through to the stored summary: a walk that is
    unusable only while the rig was loading the uplink must not come back as a
    garage full of dead zones. This is the failure that produced 42.3%
    runnable at a spot with good signal."""
    runner.ping.enabled = True
    state = {"loaded": True}
    monkeypatch.setattr(runner.ping, "snapshot",
                        lambda now=None: {"rtt_ms": 2400.0 if state["loaded"] else 45.0,
                                          "loss_pct": 95.0 if state["loaded"] else 0.0,
                                          "jitter_ms": 5.0})
    runner.udp_up.enabled = True
    monkeypatch.setattr(runner.udp_up, "start", lambda: None)
    monkeypatch.setattr(
        type(runner.udp_up), "load_state",
        property(lambda self: udpload.LOAD_SENDING if state["loaded"]
                 else udpload.LOAD_IDLE))

    run = runner.start_run(label="duty cycle")
    for i in range(20):
        state["loaded"] = i < 10
        runner.collect_sample()
    summary = runner.analyse_run(run["id"])["summary"]
    runner.stop_run()

    assert summary["dead_zone_count"] == 0
    assert summary["load_excluded_s"] == 10.0
    assert summary["judged_s"] == 10.0
    assert summary["walked_s"] == 20.0
    assert summary["runnable_pct"] == 100.0


# ---- the load test's chip ---------------------------------------------------
#
# On 2026-10-15 the server's uplink service was stuck for two whole walks.
# Every reading on the phone looked healthy, and both walks came back with no
# uplink data. The chip is what would have said so in the first minute.

def _with_load_test(runner, monkeypatch):
    for worker in runner.udp_load:
        worker.enabled = True
        monkeypatch.setattr(worker, "start", lambda: None)
    return runner


def _outcome(worker, kind, fault=False):
    from viabot_survey.workers import udpload
    state = (udpload.OUTCOME_OK if kind == "ok"
             else udpload.OUTCOME_FAIL if fault else udpload.OUTCOME_WARN)
    if kind == "ok":
        worker._note_ok()
    else:
        worker._set_outcome(state, kind, fault)


def test_the_chip_is_off_when_the_load_test_is_switched_off(runner):
    assert runner.health()["load"] == {"state": "off", "detail": "disabled in config"}


def test_the_chip_is_idle_between_walks(runner, monkeypatch):
    """The load test only runs during a walk, so before one there is nothing
    it can honestly say about the server."""
    _with_load_test(runner, monkeypatch)
    assert runner.health()["load"]["state"] == "idle"


def test_the_chip_is_grey_not_green_until_the_first_burst_reports(runner, monkeypatch):
    _with_load_test(runner, monkeypatch).start_run(label="Level 2")
    load = runner.health()["load"]
    assert load == {"state": "idle", "detail": "first test running"}


def test_the_chip_is_green_once_both_directions_are_measuring(runner, monkeypatch):
    _with_load_test(runner, monkeypatch).start_run(label="Level 2")
    for worker in runner.udp_load:
        _outcome(worker, "ok")
    assert runner.health()["load"] == {"state": "ok", "detail": "measuring"}


def test_a_stuck_server_is_red_and_names_the_direction(runner, monkeypatch):
    _with_load_test(runner, monkeypatch).start_run(label="Level 2")
    _outcome(runner.udp_up, "silent", fault=True)
    _outcome(runner.udp_down, "ok")
    load = runner.health()["load"]
    assert load == {"state": "fail",
                    "detail": "server not answering (uplink). Restart the server."}


def test_a_dead_zone_is_amber_and_never_red(runner, monkeypatch):
    """A dead zone takes the load test down too. That is the garage's doing,
    and a chip that goes red in every dead zone teaches its operator to stop
    looking at it."""
    _with_load_test(runner, monkeypatch).start_run(label="Level 2")
    _outcome(runner.udp_up, "no_link")
    _outcome(runner.udp_down, "interrupted")
    load = runner.health()["load"]
    assert load["state"] == "warn"
    assert load["detail"] == "no link (uplink); test cut off (downlink)"


def test_a_fault_in_one_direction_outranks_a_dead_zone_in_the_other(runner, monkeypatch):
    _with_load_test(runner, monkeypatch).start_run(label="Level 2")
    _outcome(runner.udp_up, "no_link")
    _outcome(runner.udp_down, "busy", fault=True)
    assert runner.health()["load"]["state"] == "fail"


def test_the_same_fault_both_ways_is_said_once(runner, monkeypatch):
    _with_load_test(runner, monkeypatch).start_run(label="Level 2")
    _outcome(runner.udp_up, "silent", fault=True)
    _outcome(runner.udp_down, "silent", fault=True)
    assert runner.health()["load"]["detail"] == (
        "server not answering (uplink, downlink). Restart the server.")


def test_a_paused_walk_shows_the_chip_as_idle(runner, monkeypatch):
    _with_load_test(runner, monkeypatch).start_run(label="Level 2")
    _outcome(runner.udp_up, "silent", fault=True)
    runner.pause()
    assert runner.health()["load"]["state"] == "idle"


def test_a_broken_chip_never_takes_the_phone_down(runner, monkeypatch):
    """Health feeds the only screen the operator has. One chip failing to work
    itself out must cost that chip and nothing else."""
    def boom(run_active):
        raise RuntimeError("no")
    monkeypatch.setattr(runner, "_load_health", boom)
    health = runner.health()
    assert health["load"] == {"state": "unknown", "detail": "not readable"}
    assert {"camera", "link", "power", "disk", "clock"} <= set(health)


def test_ping_is_the_witness_for_how_good_the_link_is(runner):
    """Three answers, not two. Only a link the banner would call usable is
    evidence against the server; one that is merely not dead is not."""
    assert runner._link_quality() is None          # ping is off in this fixture
    runner.ping.enabled = True
    assert runner._link_quality() is None          # ...and has not reported yet
    runner._last_sample = {"loss_pct": 100.0, "status": "bad"}
    assert runner._link_quality() == "dead"
    runner._last_sample = {"loss_pct": 60.0, "status": "bad"}
    assert runner._link_quality() == "poor"
    runner._last_sample = {"loss_pct": 0.0, "rtt_ms": 900.0, "status": "bad"}
    assert runner._link_quality() == "poor"
    runner._last_sample = {"loss_pct": 8.0, "status": "degraded"}
    assert runner._link_quality() == "good"
    runner._last_sample = {"loss_pct": 0.0, "status": "good"}
    assert runner._link_quality() == "good"
    assert runner.udp_up._link_now() == "good"     # the workers ask the runner


def test_a_walk_that_ended_inside_a_burst_is_logged_as_too_short(runner, storage, monkeypatch):
    """Every second of it was the rig's own upload burst, so none could be
    judged. That is not "no samples recorded", which is what it used to say."""
    monkeypatch.setattr(runner.ping, "enabled", True)
    monkeypatch.setattr(runner.ping, "snapshot",
                        lambda now=None: {"loss_pct": 0.0, "rtt_ms": 40.0})
    runner.udp_up.enabled = True
    monkeypatch.setattr(runner.udp_up, "start", lambda: None)
    monkeypatch.setattr(type(runner.udp_up), "load_state",
                        property(lambda self: 2))
    run_id = runner.start_run(label="Level 2")["id"]
    for _ in range(4):
        runner.collect_sample()
    runner.stop_run()

    said = [e["message"] for e in storage.recent_events(run_id=run_id)]
    assert any("too short to judge" in message for message in said)
    assert not any("no samples recorded" in message for message in said)


# ---- where the uplink fell short --------------------------------------------

def _store_bursts(storage, run_id, rates, base=None, block=10, idle=20):
    """One stored uplink burst per entry: its delivered rate, or None."""
    base = base or time.time() - 600
    storage.create_run(run_id, label="Level 2", started_at=base)
    ts = base
    for rate in rates:
        for _ in range(block):
            fields = {} if rate is None else {
                "udp_up_mbps": rate, "udp_up_loss_pct": 1.0, "udp_up_jitter_ms": 3.0}
            storage.add_sample(run_id, ts, rtt_ms=45.0, loss_pct=0.0, status="good",
                               uplink_loaded=2, **fields)
            ts += 1
        for _ in range(idle):
            storage.add_sample(run_id, ts, rtt_ms=45.0, loss_pct=0.0, status="good",
                               uplink_loaded=0)
            ts += 1
    return base


def test_the_summary_says_where_the_uplink_fell_short(runner, storage):
    _store_bursts(storage, "r1", [2.9, None, 0.5, 2.8])
    summary = runner.analyse_run("r1")["summary"]
    found = summary["uplink_spots"]
    assert found["bursts"] == 4
    assert [s["median_mbps"] for s in found["spots"]] == [None, 0.5]
    # And it is stored, so the report built later reads the same thing.
    import json
    stored = json.loads(storage.get_run("r1")["summary_json"])
    assert stored["uplink_spots"]["bursts"] == 4


def test_a_walk_without_the_load_test_has_no_uplink_spots(runner, storage):
    """Saying "none fell short" would claim a measurement nobody made."""
    storage.create_run("r1", label="Level 2")
    storage.add_sample("r1", time.time(), rtt_ms=45.0, loss_pct=0.0, status="good")
    assert "uplink_spots" not in runner.analyse_run("r1")["summary"]


def test_a_failing_uplink_step_never_costs_the_walk(runner, storage, monkeypatch):
    """Hard requirement. Dead zones, summary, report and publishing all go
    ahead exactly as before, and the failure is said once."""
    base = time.time() - 600
    storage.create_run("r1", label="Level 2", started_at=base)
    for offset in range(100):
        dead = 40 <= offset < 70
        storage.add_sample("r1", base + offset, rtt_ms=None if dead else 45.0,
                           loss_pct=100.0 if dead else 0.0,
                           status="dead" if dead else "good")

    def boom(*args, **kwargs):
        raise RuntimeError("synthetic")
    monkeypatch.setattr("viabot_survey.runner.report.uplink_spots", boom)
    queued = []
    monkeypatch.setattr(runner, "queue_for_publishing", queued.append)

    result = runner.analyse_run("r1")
    assert result["summary"]["dead_zone_count"] == 1
    assert "uplink_spots" not in result["summary"]
    assert storage.get_run("r1")["runnable_pct"] == 70.0
    assert len(storage.list_dead_zones("r1")) == 1
    assert queued == ["r1"]
    warnings = [e for e in storage.recent_events(run_id="r1")
                if "uplink fell short" in e["message"]]
    assert len(warnings) == 1 and warnings[0]["level"] == "warning"


def test_uplink_clips_are_published_beside_the_dead_zone_clips(runner, storage,
                                                               tmp_path, monkeypatch):
    _store_bursts(storage, "r1", [None, 2.9])
    clip = tmp_path / "uplink-01-x-10s.mp4"
    clip.write_bytes(b"x" * 100)
    runner.analyse_run("r1")                    # publishing is off here
    runner.config._data["publish"]["enabled"] = True
    import json
    run = storage.get_run("r1")
    summary = json.loads(run["summary_json"])
    summary["uplink_spots"]["spots"][0]["clip_path"] = str(clip)
    storage.set_run_summary("r1", summary)

    queued = []
    monkeypatch.setattr(storage, "enqueue_upload",
                        lambda run_id, kind, path, name, size, **kw:
                        queued.append((kind, name)))
    runner.queue_for_publishing("r1")
    assert ("clip", "clips/uplink-01-x-10s.mp4") in queued


requires_ffmpeg = __import__("pytest").mark.skipif(
    __import__("shutil").which("ffmpeg") is None, reason="ffmpeg is not installed")


@requires_ffmpeg
def test_the_worst_uplink_bursts_get_real_clips_that_do_not_collide(runner, storage):
    """Cut through the same machinery as dead-zone clips, named so the two can
    share a directory, and capped so a bad garage cannot become a hundred."""
    import subprocess
    from pathlib import Path

    base = float(int(time.time()) - 600)
    runner.config._data["udp_load"]["uplink_spot_clips"] = 2
    _store_bursts(storage, "r1", [None, 0.5, 1.0, 2.9], base=base, block=5, idle=5)
    video = runner.config.video_dir / "r1"
    video.mkdir(parents=True)
    name = time.strftime("%Y%m%dT%H%M%SZ.mkv", time.gmtime(base - 5))
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=size=160x90:rate=5", "-t", "60",
         "-c:v", "libx264", "-preset", "ultrafast", "-g", "10",
         "-pix_fmt", "yuv420p", str(video / name)], check=True, capture_output=True)
    runner.config._data["camera"]["segment_s"] = 60

    spots = runner.analyse_run("r1")["summary"]["uplink_spots"]["spots"]
    clipped = [s for s in spots if s.get("clip_path")]
    assert [s["worst_rank"] for s in clipped] == [1, 2]
    assert all(Path(s["clip_path"]).name.startswith("uplink-") for s in clipped)
    assert all(Path(s["clip_path"]).stat().st_size > 0 for s in clipped)
    assert not spots[2].get("clip_path")        # the third worst: over the cap


# ---- the chip's numbers, and the schedule on the phone ----------------------

def test_a_healthy_chip_shows_the_numbers_not_a_word(runner, monkeypatch):
    _with_load_test(runner, monkeypatch).start_run(label="Level 2")
    runner.udp_up._absorb_server_output(
        "Server output:\n" + "\n".join(
            f"[  5] {i}.00-{i + 1}.00 sec  {rate} KBytes  {rate * 8 / 1000:.2f} Mbits/sec"
            f"  1.0 ms  0/100 (0%)" for i, rate in enumerate([350, 362, 380])),
        time.time(), [])
    runner.udp_down._latest = {"mbps": 4.98, "loss_pct": 0.0}
    runner.udp_down._latest_ts = time.time()
    runner.udp_down._note_ok()
    detail = runner.health()["load"]["detail"]
    assert detail == "↑2.9 ↓5.0 Mbit/s"
    assert len(detail) <= 16


def test_the_chip_shows_one_direction_when_only_one_has_a_number(runner, monkeypatch):
    _with_load_test(runner, monkeypatch).start_run(label="Level 2")
    for worker in runner.udp_load:
        _outcome(worker, "ok")
    runner.udp_down._latest = {"mbps": 5.0, "loss_pct": 0.0}
    runner.udp_down._latest_ts = time.time()
    assert runner.health()["load"] == {"state": "ok", "detail": "↓5.0 Mbit/s"}


def test_the_phone_is_told_the_burst_schedule_in_force(runner, monkeypatch):
    """So the owner can confirm from the phone that an update took effect."""
    assert runner.status()["uplink_schedule"] is None      # off in the fixture
    runner.udp_up.enabled = True
    assert runner.status()["uplink_schedule"] == {"on_s": 20.0, "off_s": 10.0}
