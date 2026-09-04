"""Public-API Quartz input restricted to one observed frontmost target."""

from __future__ import annotations

import threading
import time
from typing import Callable, Protocol

from ok.device.capabilities import DeviceCapabilities
from ok.device.interaction_methods.base import BaseInteraction
from ok.device.interaction_methods.foreground_safety import (
    ForegroundGuard,
    ForegroundInputError,
    HeldInputState,
)
from ok.device.interaction_methods.macos_keys import macos_key_code
from ok.platform import MACOS, require_platform
from ok.util.logger import Logger


logger = Logger.get_logger(__name__)
_BUTTONS = frozenset({"left", "right", "middle"})


class QuartzEventSink(Protocol):
    def key_event(self, key_code: int, is_down: bool) -> None: ...
    def cursor_position(self) -> tuple[float, float]: ...
    def mouse_move(self, position: tuple[float, float], button: str | None = None) -> None: ...
    def mouse_button(self, button: str, is_down: bool, position: tuple[float, float]) -> None: ...
    def scroll(self, amount: int) -> None: ...


class PyObjCQuartzEventSink:
    """Small lazy PyObjC adapter; tests inject a deterministic fake sink."""

    def __init__(self):
        require_platform("Quartz foreground interaction", (MACOS,))
        import Quartz  # type: ignore[import-untyped]
        self.quartz = Quartz

    def _post(self, event) -> None:
        if event is None:
            raise RuntimeError("Quartz failed to create a CGEvent")
        self.quartz.CGEventPost(self.quartz.kCGHIDEventTap, event)

    def key_event(self, key_code: int, is_down: bool) -> None:
        self._post(self.quartz.CGEventCreateKeyboardEvent(None, key_code, is_down))

    def cursor_position(self) -> tuple[float, float]:
        event = self.quartz.CGEventCreate(None)
        if event is None:
            raise RuntimeError("Quartz failed to query the cursor")
        point = self.quartz.CGEventGetLocation(event)
        return float(point.x), float(point.y)

    def _mouse_event_type(self, button: str | None, is_down: bool | None = None):
        q = self.quartz
        if button is None:
            return q.kCGEventMouseMoved
        if is_down is None:
            return {
                "left": q.kCGEventLeftMouseDragged,
                "right": q.kCGEventRightMouseDragged,
                "middle": q.kCGEventOtherMouseDragged,
            }[button]
        return {
            ("left", True): q.kCGEventLeftMouseDown,
            ("left", False): q.kCGEventLeftMouseUp,
            ("right", True): q.kCGEventRightMouseDown,
            ("right", False): q.kCGEventRightMouseUp,
            ("middle", True): q.kCGEventOtherMouseDown,
            ("middle", False): q.kCGEventOtherMouseUp,
        }[(button, is_down)]

    def _mouse_button_code(self, button: str):
        return {
            "left": self.quartz.kCGMouseButtonLeft,
            "right": self.quartz.kCGMouseButtonRight,
            "middle": self.quartz.kCGMouseButtonCenter,
        }[button]

    def mouse_move(self, position: tuple[float, float], button: str | None = None) -> None:
        button_code = (
            self.quartz.kCGMouseButtonLeft
            if button is None else self._mouse_button_code(button))
        event = self.quartz.CGEventCreateMouseEvent(
            None, self._mouse_event_type(button), position, button_code)
        self._post(event)

    def mouse_button(self, button: str, is_down: bool, position: tuple[float, float]) -> None:
        event = self.quartz.CGEventCreateMouseEvent(
            None,
            self._mouse_event_type(button, is_down),
            position,
            self._mouse_button_code(button),
        )
        self._post(event)

    def scroll(self, amount: int) -> None:
        event = self.quartz.CGEventCreateScrollWheelEvent(
            None, self.quartz.kCGScrollEventUnitLine, 1, int(amount))
        self._post(event)


class QuartzForegroundCursorService:
    """Cursor seam routed through the same foreground gate as task input."""

    def __init__(self, interaction: "QuartzForegroundInteraction"):
        self.interaction = interaction

    @property
    def available(self) -> bool:
        return self.interaction.guard.is_open

    def get_position(self) -> tuple[int, int]:
        x, y = self.interaction.get_cursor_position()
        return round(x), round(y)

    def set_position(self, position: tuple[int, int]) -> None:
        self.interaction.set_cursor_position(position)


class QuartzForegroundInteraction(BaseInteraction):
    """Fail-closed Quartz backend with monitored focus and held-state release."""

    capabilities = DeviceCapabilities(
        keyboard_tap=True,
        keyboard_hold=True,
        absolute_mouse=True,
        mouse_left=True,
        mouse_right=True,
        mouse_middle=True,
        mouse_button_hold=True,
        scroll=True,
        relative_mouse=False,
        foreground_only=True,
    )

    def __init__(
            self,
            capture,
            target,
            permission_service,
            *,
            event_sink: QuartzEventSink | None = None,
            activation_timeout: float = 3.0,
            monitor_interval: float = 0.05,
            exit_event=None,
            on_invalidated: Callable[[str], None] | None = None,
            sleep=time.sleep):
        super().__init__(capture)
        if activation_timeout <= 0 or monitor_interval <= 0:
            raise ValueError("activation timeout and monitor interval must be positive")
        self.target = target
        self.permission_service = permission_service
        self.event_sink = event_sink or PyObjCQuartzEventSink()
        self.activation_timeout = activation_timeout
        self.monitor_interval = monitor_interval
        self.exit_event = exit_event
        self._on_invalidated = on_invalidated
        self._sleep = sleep
        self._input_lock = threading.RLock()
        self.guard = ForegroundGuard(
            target,
            capture,
            permission_service,
            lock=self._input_lock,
            stop_requested=(exit_event.is_set if exit_event is not None else None),
        )
        self.held_state = HeldInputState(self._input_lock)
        self.cursor_service = QuartzForegroundCursorService(self)
        self._monitor_stop = threading.Event()
        self._monitor_thread: threading.Thread | None = None
        if exit_event is not None:
            binder = getattr(exit_event, "bind_stop", None)
            if callable(binder):
                binder(self)

    def _button(self, key: object) -> str:
        button = str(key).strip().lower()
        if button not in _BUTTONS:
            raise ValueError(f"unsupported macOS mouse button: {key!r}")
        return button

    def _invalidate_and_release(self, reason: str, *, shutdown: bool = False) -> None:
        was_open = self.guard.invalidate(reason, shutdown=shutdown)
        self.release_all()
        if was_open and self._on_invalidated is not None:
            try:
                self._on_invalidated(str(reason))
            except Exception as error:
                logger.error(f"macOS input invalidation callback failed: {error}")

    def invalidate(
            self, reason: str = "MAC_INPUT_GATE_CLOSED", *,
            shutdown: bool = False) -> None:
        self._invalidate_and_release(reason, shutdown=shutdown)

    def stop(self) -> None:
        """ExitEvent callback: synchronously close the gate and release input."""
        self._monitor_stop.set()
        self._invalidate_and_release("application or executor is stopping", shutdown=True)

    def _ordinary(self, callback):
        try:
            return self.guard.run_ordinary(callback)
        except ForegroundInputError as error:
            self._invalidate_and_release(str(error))
            raise
        except Exception as error:
            failure = ForegroundInputError("MAC_INPUT_POST_FAILED", str(error))
            self._invalidate_and_release(str(failure))
            raise failure from error

    def _global_point(self, geometry, x, y) -> tuple[float, float]:
        return geometry.frame_pixel_to_global_point(float(x), float(y))

    def send_key(self, key, down_time=0.02):
        super().send_key(key, down_time)
        pressed = self.send_key_down(key)
        if not pressed:
            return False
        try:
            self._sleep(max(0.0, float(down_time)))
        finally:
            released = self.send_key_up(key)
        if not released and not self.guard.is_open:
            raise ForegroundInputError("MAC_INPUT_GATE_CLOSED", self.guard.reason)
        return True

    def send_key_down(self, key):
        key_code = macos_key_code(key)

        def press(_geometry):
            if self.held_state.has_key(key_code):
                return False
            self.event_sink.key_event(key_code, True)
            self.held_state.hold_key(key_code)
            return True

        return self._ordinary(press)

    def send_key_up(self, key):
        key_code = macos_key_code(key)
        with self._input_lock:
            if not self.held_state.has_key(key_code):
                return False
            try:
                self.event_sink.key_event(key_code, False)
            except Exception as error:
                failure = ForegroundInputError("MAC_INPUT_POST_FAILED", str(error))
                self._invalidate_and_release(str(failure))
                raise failure from error
            else:
                self.held_state.release_key(key_code)
            return True

    def move(self, x, y):
        return self._ordinary(
            lambda geometry: self.event_sink.mouse_move(
                self._global_point(geometry, x, y)))

    def get_cursor_position(self) -> tuple[float, float]:
        return self._ordinary(lambda _geometry: self.event_sink.cursor_position())

    def set_cursor_position(self, position: tuple[int, int]) -> None:
        point = float(position[0]), float(position[1])
        self._ordinary(lambda _geometry: self.event_sink.mouse_move(point))

    def click(
            self, x=-1, y=-1, move_back=False, name=None, move=True,
            down_time=0.02, key="left"):
        del name
        button = self._button(key)
        previous = self.get_cursor_position() if move_back else None
        if move:
            pressed = self.mouse_down(x, y, key=button)
        else:
            pressed = self.mouse_down(key=button)
        if not pressed:
            return False
        try:
            self._sleep(max(0.0, float(down_time)))
        finally:
            released = self.mouse_up(key=button)
        if not released and not self.guard.is_open:
            raise ForegroundInputError("MAC_INPUT_GATE_CLOSED", self.guard.reason)
        if previous is not None:
            self.set_cursor_position((round(previous[0]), round(previous[1])))
        return True

    def mouse_down(self, x=-1, y=-1, name=None, key="left"):
        del name
        button = self._button(key)

        def press(geometry):
            if self.held_state.has_button(button):
                return False
            position = self.event_sink.cursor_position()
            if x != -1 and y != -1:
                position = self._global_point(geometry, x, y)
                self.event_sink.mouse_move(position)
            self.event_sink.mouse_button(button, True, position)
            self.held_state.hold_button(button)
            return True

        return self._ordinary(press)

    def mouse_up(self, key="left"):
        button = self._button(key)
        with self._input_lock:
            if not self.held_state.has_button(button):
                return False
            try:
                position = self.event_sink.cursor_position()
                self.event_sink.mouse_button(button, False, position)
            except Exception as error:
                failure = ForegroundInputError("MAC_INPUT_POST_FAILED", str(error))
                self._invalidate_and_release(str(failure))
                raise failure from error
            else:
                self.held_state.release_button(button)
            return True

    def scroll(self, x, y, scroll_amount):
        amount = int(scroll_amount)

        def scroll_batch(geometry):
            if x != -1 and y != -1:
                self.event_sink.mouse_move(self._global_point(geometry, x, y))
            if amount:
                self.event_sink.scroll(amount)

        return self._ordinary(scroll_batch)

    def swipe(self, from_x, from_y, to_x, to_y, duration, settle_time=0):
        self.mouse_down(from_x, from_y, key="left")
        try:
            duration_seconds = max(0.0, float(duration)) / 1000.0
            steps = max(1, round(duration_seconds / 0.01))
            for index in range(1, steps + 1):
                ratio = index / steps
                x = from_x + (to_x - from_x) * ratio
                y = from_y + (to_y - from_y) * ratio
                self._ordinary(
                    lambda geometry, x=x, y=y: self.event_sink.mouse_move(
                        self._global_point(geometry, x, y), "left"))
                self._sleep(duration_seconds / steps)
        finally:
            self.mouse_up(key="left")
        if settle_time:
            self._sleep(max(0.0, float(settle_time)))

    def release_all(self):
        errors: list[Exception] = []
        with self._input_lock:
            snapshot = self.held_state.snapshot()
            for key_code in snapshot.keys:
                try:
                    self.event_sink.key_event(key_code, False)
                except Exception as error:
                    errors.append(error)
            try:
                position = self.event_sink.cursor_position()
            except Exception as error:
                errors.append(error)
                position = (0.0, 0.0)
            for button in snapshot.buttons:
                try:
                    self.event_sink.mouse_button(button, False, position)
                except Exception as error:
                    errors.append(error)
            self.held_state.clear()
        for release_error in errors:
            logger.error(f"Quartz held-input release failed: {release_error}")
        return not errors

    def _monitor(self):
        while not self._monitor_stop.wait(self.monitor_interval):
            if not self.guard.is_open:
                continue
            try:
                self.guard.check()
            except ForegroundInputError as error:
                logger.error(f"macOS foreground input invalidated: {error}")
                self._invalidate_and_release(str(error))
            except Exception as error:
                failure = ForegroundInputError(
                    "MAC_INPUT_GATE_CLOSED",
                    f"foreground watchdog failed: {error}",
                )
                logger.error(f"macOS foreground input invalidated: {failure}")
                self._invalidate_and_release(str(failure))

    def _ensure_monitor(self):
        if self._monitor_thread is not None and self._monitor_thread.is_alive():
            return
        self._monitor_stop.clear()
        self._monitor_thread = threading.Thread(
            target=self._monitor,
            name="QuartzForegroundGuard",
            daemon=True,
        )
        self._monitor_thread.start()

    def on_run(self):
        if not self.target.is_foreground():
            if not self.target.request_activation():
                raise ForegroundInputError(
                    "MAC_GAME_NOT_FOREGROUND", "game activation request was rejected")
            if not self.target.wait_for_observed_activation(self.activation_timeout):
                raise ForegroundInputError(
                    "MAC_GAME_NOT_FOREGROUND", "game did not become frontmost")
        target_generation, _capture_generation = self.guard.open()
        self.held_state.begin(threading.get_ident(), target_generation)
        self._ensure_monitor()

    def should_capture(self):
        return self.guard.is_open

    def on_destroy(self):
        self.stop()
        unbinder = getattr(self.exit_event, "unbind_stop", None)
        if callable(unbinder):
            unbinder(self)
        thread = self._monitor_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.2, self.monitor_interval * 4))


__all__ = [
    "PyObjCQuartzEventSink",
    "QuartzEventSink",
    "QuartzForegroundCursorService",
    "QuartzForegroundInteraction",
]
