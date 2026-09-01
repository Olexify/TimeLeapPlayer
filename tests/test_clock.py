"""Frame scheduling and A/V sync.

The prototype slept for `delay - work` after every frame, so every frame's
rounding error was added to the next frame's start and playback walked away
from the audio. These tests drive the clock through a fake time source whose
sleep deliberately overshoots -- exactly the Windows 15.6 ms timer problem --
and assert that the error stays put instead of accumulating.
"""
from __future__ import annotations

import time

import pytest

from timeleap.engine import clock as C
from timeleap.engine.clock import FrameClock, SyncedClock, precise_sleep

OVERSHOOT = 0.004       # every sleep lands 4 ms late, like a coarse OS timer


class FakeTime:
    """Stand-in for the `time` module: monotonic, and sleeps badly."""

    TICK = 1e-5         # reading the clock costs a little, so spins terminate

    def __init__(self, start: float = 1000.0, overshoot: float = OVERSHOOT) -> None:
        self.t = start
        self.overshoot = overshoot
        self.sleeps = 0

    def perf_counter(self) -> float:
        self.t += self.TICK
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps += 1
        self.t += max(0.0, seconds) + self.overshoot

    def advance(self, seconds: float) -> None:
        self.t += seconds


class FakeAudio:
    """Just enough of the AudioTrack protocol for `SyncedClock`."""

    def __init__(self, position: float = 0.0, playing: bool = True) -> None:
        self.position = position
        self.playing = playing


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeTime:
    """Swap the clock module's time source. Scoped, so nothing leaks."""
    stub = FakeTime()
    monkeypatch.setattr(C, "time", stub)
    return stub


# ---- no drift --------------------------------------------------------
def test_deadlines_are_absolute_offsets_from_the_anchor(fake: FakeTime) -> None:
    clock = FrameClock(fps=30.0, speed=1.0)
    clock.reset(0.0)
    base = clock.deadline_for(0)
    for f in range(1, 600):
        assert clock.deadline_for(f) - base == pytest.approx(f / 30.0, abs=1e-9)


def test_error_does_not_accumulate_over_many_frames(fake: FakeTime) -> None:
    """Every sleep overshoots by 4 ms. A relative-sleep loop would be 1.2 s
    late after 300 frames; absolute deadlines must stay within one overshoot."""
    clock = FrameClock(fps=30.0, speed=1.0)
    clock.reset(0.0)
    start = fake.t
    errors = []
    for f in range(1, 301):
        clock.wait_for(f)
        errors.append(fake.t - (start + f / 30.0))

    assert fake.sleeps >= 300, "the clock has to actually wait"
    assert max(errors) < OVERSHOOT + 0.001
    assert min(errors) > -0.001
    # The last frame is no later than the first: no accumulation at all.
    assert errors[-1] == pytest.approx(errors[0], abs=1e-4)
    naive_drift = 300 * OVERSHOOT
    assert max(errors) < naive_drift / 100


def test_position_tracks_the_frame_it_waited_for(fake: FakeTime) -> None:
    clock = FrameClock(fps=60.0, speed=1.5)
    clock.reset(0.0)
    for f in (1, 50, 200, 999):
        clock.wait_for(f, max_wait=1e6)
        assert clock.position_frames() == pytest.approx(f, abs=0.5)


def test_wait_for_returns_the_slack_and_does_not_wait_when_late(
        fake: FakeTime) -> None:
    clock = FrameClock(fps=30.0)
    clock.reset(0.0)
    early = clock.wait_for(10)
    assert early > 0.0

    fake.advance(5.0)                       # fall a long way behind
    before = fake.sleeps
    slack = clock.wait_for(11)
    assert slack < 0.0, "an overdue frame reports negative slack"
    assert fake.sleeps == before, "and must not sleep"


def test_position_is_linear_in_fps_and_speed(fake: FakeTime) -> None:
    for fps, speed in ((30.0, 1.0), (24.0, 2.0), (60.0, 0.25)):
        clock = FrameClock(fps=fps, speed=speed)
        clock.reset(0.0)
        fake.advance(2.0)
        assert clock.position_frames() == pytest.approx(2.0 * fps * speed, abs=0.01)
        assert clock.position_seconds() == pytest.approx(2.0 * speed, abs=0.01)


# ---- pause / resume --------------------------------------------------
def test_a_paused_clock_stands_still(fake: FakeTime) -> None:
    clock = FrameClock(fps=30.0)
    clock.reset(0.0)
    fake.advance(1.0)
    clock.pause()
    at_pause = clock.position_frames()
    for _ in range(5):
        fake.advance(3.0)
        assert clock.position_frames() == at_pause
    assert clock.paused is True


def test_resume_picks_up_exactly_where_it_stopped(fake: FakeTime) -> None:
    clock = FrameClock(fps=30.0)
    clock.reset(0.0)
    fake.advance(1.0)
    clock.pause()
    at_pause = clock.position_frames()

    fake.advance(60.0)                      # a minute on the pause button
    clock.resume()
    assert clock.paused is False
    assert clock.position_frames() == pytest.approx(at_pause, abs=1e-3)

    fake.advance(1.0)
    assert clock.position_frames() == pytest.approx(at_pause + 30.0, abs=1e-2)


def test_pausing_twice_does_not_lose_time(fake: FakeTime) -> None:
    clock = FrameClock(fps=30.0)
    clock.reset(0.0)
    fake.advance(1.0)
    clock.pause()
    fake.advance(2.0)
    clock.pause()                           # a second press must be a no-op
    at_pause = clock.position_frames()
    fake.advance(2.0)
    clock.resume()
    assert clock.position_frames() == pytest.approx(at_pause, abs=1e-3)


def test_resume_without_pause_is_harmless(fake: FakeTime) -> None:
    clock = FrameClock(fps=30.0)
    clock.reset(0.0)
    fake.advance(1.0)
    before = clock.position_frames()
    clock.resume()
    assert clock.position_frames() == pytest.approx(before, abs=1e-3)


def test_reset_while_paused_stays_paused(fake: FakeTime) -> None:
    clock = FrameClock(fps=30.0)
    clock.pause()
    clock.reset(90.0)
    assert clock.paused is True
    fake.advance(5.0)
    assert clock.position_frames() == pytest.approx(90.0, abs=1e-3)


# ---- speed and fps ---------------------------------------------------
def test_set_speed_rebases_instead_of_rewriting_history(fake: FakeTime) -> None:
    """Doubling the speed two seconds in must not retroactively claim the
    playhead was always at 2x -- that would jump the picture."""
    clock = FrameClock(fps=30.0, speed=1.0)
    clock.reset(0.0)
    fake.advance(2.0)
    before = clock.position_frames()
    assert before == pytest.approx(60.0, abs=0.01)

    clock.set_speed(3.0)
    # `FakeTime` charges TICK per clock read, so this comparison cannot be
    # tighter than the reads it performs: one inside rebase() at the old speed
    # and one for the assertion at the new speed, i.e. TICK*fps*(1 + 3) frames.
    # Verified against a zero-cost clock, where the rebase is exactly lossless.
    budget = FakeTime.TICK * 30.0 * (1.0 + 3.0)
    assert clock.position_frames() == pytest.approx(before, abs=budget * 1.5)

    fake.advance(1.0)
    assert clock.position_frames() == pytest.approx(before + 90.0, abs=0.01)


def test_set_fps_rebases_too(fake: FakeTime) -> None:
    clock = FrameClock(fps=30.0)
    clock.reset(0.0)
    fake.advance(2.0)
    before = clock.position_frames()
    clock.set_fps(60.0)
    assert clock.position_frames() == pytest.approx(before, abs=1e-3)
    fake.advance(1.0)
    assert clock.position_frames() == pytest.approx(before + 60.0, abs=0.01)


def test_setting_the_same_speed_is_a_no_op(fake: FakeTime) -> None:
    clock = FrameClock(fps=30.0, speed=2.0)
    clock.reset(0.0)
    fake.advance(1.0)
    before = clock.position_frames()
    clock.set_speed(2.0)
    assert clock.position_frames() == pytest.approx(before, abs=1e-3)


@pytest.mark.parametrize("asked,expected", [
    (0.0, 0.01), (-4.0, 0.01), (100.0, 16.0), (0.5, 0.5), (16.0, 16.0),
])
def test_speed_is_clamped_to_a_playable_range(asked, expected, fake) -> None:
    clock = FrameClock(fps=30.0)
    clock.set_speed(asked)
    assert clock.speed == pytest.approx(expected)


def test_fps_is_never_zero() -> None:
    assert FrameClock(fps=0.0).fps > 0.0
    assert FrameClock(fps=-5.0).fps > 0.0
    clock = FrameClock(fps=30.0)
    clock.set_fps(0.0)
    assert clock.fps > 0.0


def test_advance_warp_shifts_without_re_anchoring(fake: FakeTime) -> None:
    clock = FrameClock(fps=30.0)
    clock.reset(0.0)
    fake.advance(1.0)
    before = clock.position_frames()
    clock.advance_warp(5.0)
    assert clock.position_frames() == pytest.approx(before + 5.0, abs=1e-3)
    # The deadline function has to agree, or the wait would fight the shift.
    assert clock.deadline_for(clock.position_frames()) == pytest.approx(fake.t, abs=1e-3)


def test_reset_clears_the_warp_accumulator(fake: FakeTime) -> None:
    clock = FrameClock(fps=30.0)
    clock.advance_warp(20.0)
    clock.reset(7.0)
    assert clock.position_frames() == pytest.approx(7.0, abs=1e-3)


# ---- precise_sleep ---------------------------------------------------
def test_precise_sleep_never_returns_early() -> None:
    """Real time here: the point of the spin is sub-millisecond accuracy."""
    for wanted in (0.0, 0.002, 0.01):
        start = time.perf_counter()
        precise_sleep(wanted)
        assert time.perf_counter() - start >= wanted - 0.0005


def test_precise_sleep_ignores_non_positive_durations() -> None:
    start = time.perf_counter()
    precise_sleep(-1.0)
    precise_sleep(0.0)
    assert time.perf_counter() - start < 0.05


def test_high_resolution_requests_are_reference_counted(monkeypatch) -> None:
    """Nested players must not have the first `end` undo everyone's request."""
    calls: list[str] = []

    class FakeWin32:
        IS_WINDOWS = True

        @staticmethod
        def timeBeginPeriod(ms: int) -> None:
            calls.append(f"begin{ms}")

        @staticmethod
        def timeEndPeriod(ms: int) -> None:
            calls.append(f"end{ms}")

    monkeypatch.setattr(C, "w", FakeWin32)
    monkeypatch.setattr(C, "_period_refs", 0)

    C.begin_high_resolution()
    C.begin_high_resolution()
    assert calls == ["begin1"]
    C.end_high_resolution()
    assert calls == ["begin1"]
    C.end_high_resolution()
    assert calls == ["begin1", "end1"]
    C.end_high_resolution()             # unbalanced extra must not go negative
    assert calls == ["begin1", "end1"]


# ---- SyncedClock -----------------------------------------------------
def test_the_video_clock_is_master_when_there_is_no_audio(fake: FakeTime) -> None:
    sc = SyncedClock(fps=30.0)
    sc.reset(0.0)
    fake.advance(1.0)
    assert sc.position_frames() == pytest.approx(30.0, abs=0.01)
    assert sc.drift == 0.0
    assert sc.position_seconds() == pytest.approx(1.0, abs=0.01)


def test_a_small_gap_is_followed_without_snapping(fake: FakeTime) -> None:
    """Inside the snap window the video simply reports the audio position and
    the video clock is left alone, so the correction is smooth."""
    sc = SyncedClock(fps=30.0)
    sc.reset(0.0)
    fake.advance(1.0)
    sc.attach_audio(FakeAudio(position=0.95))       # 50 ms behind

    video_before = sc.clock.position_frames()
    got = sc.position_frames()

    assert got == pytest.approx(0.95 * 30.0, abs=0.01)
    assert sc.drift == pytest.approx(0.05, abs=0.002)
    assert sc.clock.position_frames() == pytest.approx(video_before, abs=1e-3), \
        "a small gap must not re-anchor the video clock"


def test_a_large_gap_snaps_the_video_clock(fake: FakeTime) -> None:
    sc = SyncedClock(fps=30.0)
    sc.reset(0.0)
    fake.advance(1.0)
    sc.attach_audio(FakeAudio(position=0.5))        # 500 ms behind: a seek

    got = sc.position_frames()
    assert got == pytest.approx(15.0, abs=0.01)
    assert abs(sc.drift) > SyncedClock.SNAP_SECONDS
    assert sc.clock.position_frames() == pytest.approx(15.0, abs=0.01), \
        "a large gap must re-anchor rather than crawl back"

    # Having snapped, the next read is in sync and stays smooth.
    fake.advance(0.1)
    sc.audio.position = 0.6
    sc.position_frames()
    assert abs(sc.drift) < SyncedClock.SNAP_SECONDS


def test_the_snap_boundary_is_the_documented_threshold(fake: FakeTime) -> None:
    for gap, snaps in ((SyncedClock.SNAP_SECONDS - 0.05, False),
                       (SyncedClock.SNAP_SECONDS + 0.05, True)):
        sc = SyncedClock(fps=30.0)
        sc.reset(0.0)
        fake.advance(1.0)
        sc.attach_audio(FakeAudio(position=1.0 - gap))
        video_before = sc.clock.position_frames()
        sc.position_frames()
        moved = abs(sc.clock.position_frames() - video_before) > 1.0
        assert moved is snaps, gap


@pytest.mark.parametrize("track", [
    FakeAudio(position=0.5, playing=False),
    FakeAudio(position=-1.0, playing=True),
    None,
])
def test_an_unusable_track_leaves_the_video_in_charge(track, fake: FakeTime) -> None:
    sc = SyncedClock(fps=30.0)
    sc.reset(0.0)
    fake.advance(1.0)
    if track is not None:
        sc.attach_audio(track)
    assert sc.position_frames() == pytest.approx(30.0, abs=0.01)
    assert sc.drift == 0.0


def test_sync_can_be_disabled(fake: FakeTime) -> None:
    sc = SyncedClock(fps=30.0)
    sc.reset(0.0)
    sc.attach_audio(FakeAudio(position=0.0))
    fake.advance(1.0)
    sc.enabled = False
    assert sc.position_frames() == pytest.approx(30.0, abs=0.01)
    assert sc.drift == 0.0


def test_detaching_audio_clears_the_drift(fake: FakeTime) -> None:
    sc = SyncedClock(fps=30.0)
    sc.reset(0.0)
    fake.advance(1.0)
    sc.attach_audio(FakeAudio(position=0.9))
    sc.position_frames()
    assert sc.drift != 0.0
    sc.detach_audio()
    assert sc.audio is None and sc.drift == 0.0
    assert sc.position_frames() == pytest.approx(30.0, abs=0.01)


def test_synced_clock_passes_transport_calls_through(fake: FakeTime) -> None:
    sc = SyncedClock(fps=30.0)
    sc.reset(10.0)
    sc.pause()
    assert sc.paused is True and sc.clock.paused is True
    fake.advance(5.0)
    assert sc.position_frames() == pytest.approx(10.0, abs=1e-3)
    sc.resume()
    assert sc.paused is False

    sc.set_speed(2.0)
    assert sc.clock.speed == 2.0
    sc.set_fps(48.0)
    assert sc.clock.fps == 48.0
    assert sc.wait_for(sc.clock.position_frames()) <= 0.001


def test_audio_drift_is_reported_in_seconds(fake: FakeTime) -> None:
    sc = SyncedClock(fps=25.0)
    sc.reset(0.0)
    fake.advance(2.0)                       # video at 2.0 s
    sc.attach_audio(FakeAudio(position=1.9))
    sc.position_frames()
    assert sc.drift == pytest.approx(0.1, abs=0.002)

    sc.audio.position = 2.1                 # audio ahead: drift goes negative
    sc.position_frames()
    assert sc.drift == pytest.approx(-0.1, abs=0.002)
