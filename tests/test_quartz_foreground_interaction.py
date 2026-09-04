from types import SimpleNamespace
import time

import pytest

from ok.device.interaction_methods.foreground_safety import ForegroundInputError
from ok.device.interaction_methods.macos_keys import macos_key_code
from ok.device.interaction_methods.quartz import (
    PyObjCQuartzEventSink,
    QuartzForegroundInteraction,
)
from ok.device.services import PermissionKind
from ok.task.TaskExecutor import TaskExecutor
from ok.util.handler import ExitEvent


class FakeGeometry:
    target_generation = 4
    capture_generation = 9

    def frame_pixel_to_global_point(self, x, y):
        if not (0 <= x <= 1920 and 0 <= y <= 1080):
            raise ValueError("outside frame")
        return 100 + x / 2, 50 + y / 2


class FakeCapture:
    def __init__(self):
        self.geometry = FakeGeometry()
        self.state = "running"
        self.last_error = None

    def diagnostics(self):
        return SimpleNamespace(
            state=SimpleNamespace(value=self.state),
            target_generation=self.geometry.target_generation,
            capture_generation=self.geometry.capture_generation,
            last_error=self.last_error,
        )


class FakeTarget:
    def __init__(self):
        self.present = True
        self.frontmost = True
        self.activation_requested = 0
        self.activation_observed = True
        self.on_foreground_check = None
        self.snapshot = SimpleNamespace(
            exists=True,
            generation=4,
            candidate=SimpleNamespace(process_id=123, window_id=456),
        )

    def exists(self):
        return self.present

    def is_foreground(self):
        if self.on_foreground_check is not None:
            callback, self.on_foreground_check = self.on_foreground_check, None
            callback()
        return self.frontmost

    def request_activation(self):
        self.activation_requested += 1
        return True

    def wait_for_observed_activation(self, _timeout):
        self.frontmost = self.activation_observed
        return self.activation_observed


class FakePermissionService:
    def __init__(self):
        self.granted = True
        self.revoked_kind = None
        self.error = None

    def status(self, kind):
        if self.error is not None:
            raise self.error
        granted = self.granted and kind is not self.revoked_kind
        return SimpleNamespace(
            granted=granted,
            detail="",
            state=SimpleNamespace(value="granted" if granted else "permission-revoked"),
        )


class FakeSink:
    def __init__(self):
        self.events = []
        self.position = (20.0, 30.0)
        self.fail_once = None

    def _record(self, event):
        if self.fail_once == event:
            self.fail_once = None
            raise RuntimeError(f"failed {event}")
        self.events.append(event)

    def key_event(self, key_code, is_down):
        self._record(("key", key_code, is_down))

    def cursor_position(self):
        self._record(("cursor",))
        return self.position

    def mouse_move(self, position, button=None):
        self.position = position
        self._record(("move", position, button))

    def mouse_button(self, button, is_down, position):
        self._record(("button", button, is_down, position))

    def scroll(self, amount):
        self._record(("scroll", amount))


@pytest.fixture
def interaction():
    value = QuartzForegroundInteraction(
        FakeCapture(),
        FakeTarget(),
        FakePermissionService(),
        event_sink=FakeSink(),
        monitor_interval=0.01,
        sleep=lambda _seconds: None,
    )
    value.on_run()
    yield value
    value.on_destroy()


def test_key_tap_and_duplicate_down_policy(interaction):
    w = macos_key_code("w")

    interaction.send_key("w")
    assert interaction.send_key_down("w") is True
    assert interaction.send_key_down("w") is False
    assert interaction.send_key_up("w") is True
    assert interaction.send_key_up("w") is False

    assert [event for event in interaction.event_sink.events if event[0] == "key"] == [
        ("key", w, True),
        ("key", w, False),
        ("key", w, True),
        ("key", w, False),
    ]
    assert interaction.held_state.snapshot().keys == ()


def test_absolute_buttons_scroll_and_guarded_cursor(interaction):
    interaction.click(1920, 1080, key="middle", down_time=0)
    interaction.mouse_down(0, 0, key="right")
    interaction.mouse_up(key="right")
    interaction.scroll(960, 540, -2)
    interaction.cursor_service.set_position((333, 444))

    events = interaction.event_sink.events
    assert ("move", (1060.0, 590.0), None) in events
    assert ("button", "middle", True, (1060.0, 590.0)) in events
    assert ("button", "middle", False, (1060.0, 590.0)) in events
    assert ("button", "right", True, (100.0, 50.0)) in events
    assert ("button", "right", False, (100.0, 50.0)) in events
    assert ("scroll", -2) in events
    assert events[-1] == ("move", (333.0, 444.0), None)


def test_focus_loss_rejects_ordinary_input_and_releases_held_key(interaction):
    w = macos_key_code("w")
    interaction.send_key_down("w")
    interaction.target.frontmost = False

    with pytest.raises(ForegroundInputError) as exc_info:
        interaction.send_key("f")

    assert exc_info.value.code == "MAC_GAME_NOT_FOREGROUND"
    assert ("key", w, False) in interaction.event_sink.events
    assert interaction.held_state.snapshot().keys == ()
    assert not interaction.guard.is_open


def test_watchdog_releases_without_another_ordinary_event(interaction):
    interaction.mouse_down(key="right")
    interaction.target.frontmost = False

    deadline = time.monotonic() + 0.5
    while interaction.held_state.snapshot().buttons and time.monotonic() < deadline:
        time.sleep(0.01)

    assert interaction.held_state.snapshot().buttons == ()
    assert not interaction.guard.is_open
    assert any(
        event[:3] == ("button", "right", False)
        for event in interaction.event_sink.events
    )


def test_watchdog_notifies_consumer_to_pause_once(interaction):
    reasons = []
    interaction._on_invalidated = reasons.append
    interaction.target.frontmost = False

    deadline = time.monotonic() + 0.5
    while interaction.guard.is_open and time.monotonic() < deadline:
        time.sleep(0.01)

    interaction.invalidate("duplicate invalidation")
    assert len(reasons) == 1
    assert "MAC_GAME_NOT_FOREGROUND" in reasons[0]


def test_exit_event_synchronously_closes_gate_and_prevents_reopen():
    exit_event = ExitEvent()
    value = QuartzForegroundInteraction(
        FakeCapture(),
        FakeTarget(),
        FakePermissionService(),
        event_sink=FakeSink(),
        exit_event=exit_event,
        sleep=lambda _seconds: None,
    )
    value.on_run()
    value.send_key_down("w")

    exit_event.set()
    assert value.held_state.snapshot().keys == ()
    with pytest.raises(ForegroundInputError, match="stopping"):
        value.send_key("f")
    with pytest.raises(ForegroundInputError, match="stopping"):
        value.on_run()
    value.on_destroy()


def test_focus_loss_during_key_tap_releases_and_reports_failure():
    target = FakeTarget()
    holder = {}

    def lose_focus(_seconds):
        target.frontmost = False
        deadline = time.monotonic() + 0.5
        while holder["interaction"].held_state.snapshot().keys and time.monotonic() < deadline:
            time.sleep(0.01)

    value = QuartzForegroundInteraction(
        FakeCapture(),
        target,
        FakePermissionService(),
        event_sink=FakeSink(),
        monitor_interval=0.01,
        sleep=lose_focus,
    )
    holder["interaction"] = value
    value.on_run()
    try:
        with pytest.raises(ForegroundInputError, match="MAC_INPUT_GATE_CLOSED"):
            value.send_key("w", down_time=0.2)
        assert value.held_state.snapshot().keys == ()
    finally:
        value.on_destroy()


def test_generation_change_and_permission_revoke_fail_closed(interaction):
    interaction.capture.geometry.target_generation = 5
    with pytest.raises(ForegroundInputError) as generation_error:
        interaction.move(1, 1)
    assert generation_error.value.code == "MAC_INPUT_GATE_CLOSED"

    interaction.capture.geometry.target_generation = 4
    interaction.on_run()
    interaction.permission_service.revoked_kind = PermissionKind.ACCESSIBILITY
    with pytest.raises(ForegroundInputError) as permission_error:
        interaction.scroll(1, 1, 1)
    assert permission_error.value.code == "MAC_ACCESSIBILITY_PERMISSION_REQUIRED"


def test_live_generation_change_during_frontmost_check_blocks_post(interaction):
    interaction.target.on_foreground_check = lambda: setattr(
        interaction.target.snapshot, "generation", 5)

    with pytest.raises(ForegroundInputError, match="generation changed"):
        interaction.send_key_down("w")

    assert not [event for event in interaction.event_sink.events if event[0] == "key"]


def test_invalid_runtime_window_identity_blocks_post(interaction):
    interaction.target.snapshot.candidate.window_id = 0

    with pytest.raises(ForegroundInputError, match="target is unavailable"):
        interaction.send_key_down("w")

    assert not [event for event in interaction.event_sink.events if event[0] == "key"]


@pytest.mark.parametrize("failure", ["target", "capture", "permission"])
def test_target_capture_and_permission_failure_release_all_without_new_down(failure):
    value = QuartzForegroundInteraction(
        FakeCapture(),
        FakeTarget(),
        FakePermissionService(),
        event_sink=FakeSink(),
        monitor_interval=10,
        sleep=lambda _seconds: None,
    )
    value.on_run()
    try:
        value.send_key_down("w")
        value.mouse_down(key="right")
        before_failure = len(value.event_sink.events)
        if failure == "target":
            value.target.present = False
        elif failure == "capture":
            value.capture.state = "fatal"
            value.capture.last_error = "stream stopped"
        else:
            value.permission_service.revoked_kind = PermissionKind.ACCESSIBILITY

        with pytest.raises(ForegroundInputError):
            value.move(1, 1)

        tail = value.event_sink.events[before_failure:]
        assert ("key", macos_key_code("w"), False) in tail
        assert any(event[:3] == ("button", "right", False) for event in tail)
        assert not any(
            event == ("key", macos_key_code("w"), True)
            or event[:3] == ("button", "right", True)
            for event in tail
        )
        assert value.held_state.snapshot().keys == ()
        assert value.held_state.snapshot().buttons == ()
    finally:
        value.on_destroy()


def test_release_all_continues_after_one_failure_and_always_clears(interaction):
    w = macos_key_code("w")
    interaction.send_key_down("w")
    interaction.mouse_down(key="left")
    interaction.event_sink.fail_once = ("key", w, False)

    assert interaction.release_all() is False
    assert interaction.held_state.snapshot().keys == ()
    assert interaction.held_state.snapshot().buttons == ()
    assert any(
        event[:3] == ("button", "left", False)
        for event in interaction.event_sink.events
    )
    assert interaction.release_all() is True


def test_mouse_up_cursor_failure_invalidates_and_clears_held_button(interaction):
    interaction.mouse_down(key="middle")
    interaction.event_sink.fail_once = ("cursor",)

    with pytest.raises(ForegroundInputError) as exc_info:
        interaction.mouse_up(key="middle")

    assert exc_info.value.code == "MAC_INPUT_POST_FAILED"
    assert interaction.held_state.snapshot().buttons == ()
    assert not interaction.guard.is_open


def test_watchdog_unknown_error_fails_closed_and_releases(interaction):
    interaction.send_key_down("w")
    interaction.permission_service.error = RuntimeError("permission service crashed")

    deadline = time.monotonic() + 0.5
    while interaction.guard.is_open and time.monotonic() < deadline:
        time.sleep(0.01)

    assert not interaction.guard.is_open
    assert interaction.held_state.snapshot().keys == ()


def test_swipe_duration_uses_framework_milliseconds():
    sleeps = []
    value = QuartzForegroundInteraction(
        FakeCapture(),
        FakeTarget(),
        FakePermissionService(),
        event_sink=FakeSink(),
        monitor_interval=1,
        sleep=sleeps.append,
    )
    value.on_run()
    try:
        value.swipe(0, 0, 100, 100, 500)
        assert sum(sleeps) == pytest.approx(0.5)
        assert len(sleeps) == 50
    finally:
        value.on_destroy()


def test_post_failure_closes_gate_and_retries_matching_release(interaction):
    w = macos_key_code("w")
    interaction.event_sink.fail_once = ("key", w, False)

    with pytest.raises(ForegroundInputError) as exc_info:
        interaction.send_key("w")

    assert exc_info.value.code == "MAC_INPUT_POST_FAILED"
    assert not interaction.guard.is_open
    assert interaction.held_state.snapshot().keys == ()
    assert ("key", w, False) in interaction.event_sink.events


def test_on_run_requests_activation_once_and_observes_frontmost():
    target = FakeTarget()
    target.frontmost = False
    value = QuartzForegroundInteraction(
        FakeCapture(),
        target,
        FakePermissionService(),
        event_sink=FakeSink(),
        monitor_interval=0.01,
    )
    try:
        value.on_run()
        assert target.activation_requested == 1
        assert value.guard.is_open
    finally:
        value.on_destroy()


def test_activation_request_without_observed_frontmost_keeps_gate_closed():
    target = FakeTarget()
    target.frontmost = False
    target.activation_observed = False
    value = QuartzForegroundInteraction(
        FakeCapture(), target, FakePermissionService(), event_sink=FakeSink())

    with pytest.raises(ForegroundInputError, match="did not become frontmost"):
        value.on_run()

    assert target.activation_requested == 1
    assert not value.guard.is_open
    assert value.event_sink.events == []
    value.on_destroy()


def test_production_sink_rechecks_immediately_before_ordinary_post():
    class FakeQuartz:
        kCGHIDEventTap = "hid"
        posts = []

        @staticmethod
        def CGEventCreateKeyboardEvent(_source, key_code, is_down):
            return ("key", key_code, is_down)

        @classmethod
        def CGEventPost(cls, tap, event):
            cls.posts.append((tap, event))

    checks = []

    def reject():
        checks.append("checked")
        raise ForegroundInputError("MAC_GAME_NOT_FOREGROUND", "focus changed")

    sink = PyObjCQuartzEventSink(reject, quartz=FakeQuartz)
    with pytest.raises(ForegroundInputError, match="focus changed"):
        sink.key_event(macos_key_code("w"), True)
    assert checks == ["checked"]
    assert FakeQuartz.posts == []

    # Matching releases intentionally bypass the ordinary-event gate.
    sink.key_event(macos_key_code("w"), False)
    assert FakeQuartz.posts == [
        ("hid", ("key", macos_key_code("w"), False)),
    ]


def test_task_executor_stop_releases_held_input_and_prevents_reopen():
    exit_event = ExitEvent()
    value = QuartzForegroundInteraction(
        FakeCapture(),
        FakeTarget(),
        FakePermissionService(),
        event_sink=FakeSink(),
        exit_event=exit_event,
        monitor_interval=10,
    )
    value.on_run()
    value.send_key_down("w")
    value.mouse_down(key="middle")
    executor = TaskExecutor.__new__(TaskExecutor)
    executor.device_manager = SimpleNamespace(interaction=value)
    executor.exit_event = exit_event
    executor._wake_executor = lambda: None

    executor.stop()

    assert value.held_state.snapshot().keys == ()
    assert value.held_state.snapshot().buttons == ()
    assert exit_event.is_set()
    with pytest.raises(ForegroundInputError, match="stopping"):
        value.on_run()
    value.on_destroy()


def test_global_cursor_position_rejects_non_finite_values(interaction):
    with pytest.raises(ValueError, match="finite"):
        interaction.set_cursor_position((float("nan"), 10))
    with pytest.raises(ValueError, match="finite"):
        interaction.set_cursor_position((10, float("inf")))


@pytest.mark.parametrize("key", ["w", "a", "s", "d", "e", "q", "r", "f", "t",
                                  "space", "shift", "tab", "f2", "b", "1", "2", "3",
                                  "esc", "enter", "alt"])
def test_stage_e_logical_key_set_is_mapped(key):
    assert isinstance(macos_key_code(key), int)


def test_unknown_key_fails_before_posting(interaction):
    with pytest.raises(ValueError, match="unsupported macOS key"):
        interaction.send_key("not-a-real-key")
    assert not [event for event in interaction.event_sink.events if event[0] == "key"]
