"""Public-API macOS window discovery and foreground observation.

ScreenCaptureKit is used here only for low-frequency discovery/rebinding.  The
persistent ``SCStream`` implementation remains isolated in the capture layer.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Protocol

from ok.device.window_target.base import (
    BaseDesktopWindowTarget,
    StableWindowHint,
    WindowCandidate,
    WindowCoordinateSpace,
    WindowGeometry,
    WindowRefreshResult,
    WindowRefreshStatus,
    WindowTargetSnapshot,
)
from ok.device.window_target.selection import (
    WindowMatchHints,
    WindowSelectionResult,
    WindowSelectionStatus,
    select_window_candidate,
)
from ok.platform import MACOS, require_platform


class WindowDiscoveryError(RuntimeError):
    """Raised when the operating system cannot provide shareable windows."""


class WindowDiscoveryTimeoutError(WindowDiscoveryError):
    """Raised when ScreenCaptureKit does not complete discovery in time."""


class MacOSWindowSystem(Protocol):
    def enumerate_windows(self, timeout: float) -> tuple[WindowCandidate, ...]: ...

    def frontmost_process_id(self) -> int | None: ...

    def process_exists(self, process_id: int) -> bool: ...

    def window_exists(self, process_id: int, window_id: int) -> bool: ...

    def request_activation(self, process_id: int) -> bool: ...


def _objc_value(instance, name: str):
    value = getattr(instance, name)
    return value() if callable(value) else value


def _geometry_from_rect(
        rect,
        coordinate_space: WindowCoordinateSpace = (
            WindowCoordinateSpace.UNKNOWN)) -> WindowGeometry:
    try:
        return WindowGeometry(
            float(rect.origin.x),
            float(rect.origin.y),
            float(rect.size.width),
            float(rect.size.height),
            coordinate_space,
        )
    except AttributeError:
        try:
            (x, y), (width, height) = rect
            return WindowGeometry(
                float(x), float(y), float(width), float(height),
                coordinate_space)
        except (TypeError, ValueError) as error:
            raise WindowDiscoveryError(
                f"unsupported ScreenCaptureKit window frame {rect!r}") from error


class PyObjCMacOSWindowSystem:
    """Thin adapter around AppKit and ScreenCaptureKit public APIs."""

    def __init__(self):
        require_platform("PyObjC macOS window system", (MACOS,))
        import AppKit
        import Quartz
        import ScreenCaptureKit

        self._appkit = AppKit
        self._quartz = Quartz
        self._screen_capture_kit = ScreenCaptureKit
        self._workspace = AppKit.NSWorkspace.sharedWorkspace()

    def enumerate_windows(self, timeout: float) -> tuple[WindowCandidate, ...]:
        if timeout <= 0:
            raise ValueError("window discovery timeout must be positive")

        completed = threading.Event()
        result: dict[str, object] = {}

        def on_content(content, error):
            result["content"] = content
            result["error"] = error
            completed.set()

        self._screen_capture_kit.SCShareableContent.getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(
            True,
            True,
            on_content,
        )
        if not completed.wait(timeout):
            raise WindowDiscoveryTimeoutError(
                f"ScreenCaptureKit discovery did not finish within {timeout:.1f}s")

        error = result.get("error")
        if error is not None:
            try:
                description = _objc_value(error, "localizedDescription")
            except AttributeError:
                description = str(error)
            raise WindowDiscoveryError(
                f"ScreenCaptureKit discovery failed: {description}")
        content = result.get("content")
        if content is None:
            raise WindowDiscoveryError("ScreenCaptureKit returned no shareable content")

        frontmost_process_id = self.frontmost_process_id()
        candidates = []
        for window in _objc_value(content, "windows"):
            application = _objc_value(window, "owningApplication")
            if application is None:
                continue
            process_id = int(_objc_value(application, "processID") or 0)
            window_id = int(_objc_value(window, "windowID") or 0)
            if process_id <= 0 or window_id <= 0:
                continue
            candidates.append(WindowCandidate(
                process_id=process_id,
                window_id=window_id,
                bundle_identifier=(
                    str(_objc_value(application, "bundleIdentifier") or "").strip()
                    or None
                ),
                application_name=str(
                    _objc_value(application, "applicationName") or "").strip(),
                title=str(_objc_value(window, "title") or "").strip(),
                layer=int(_objc_value(window, "windowLayer") or 0),
                outer_geometry=_geometry_from_rect(
                    _objc_value(window, "frame"),
                    WindowCoordinateSpace.MACOS_GLOBAL_LOGICAL_POINTS,
                ),
                # Discovery metadata cannot establish content/capture pixels.
                # The capture layer derives them from actual SCStream frames.
                content_geometry=None,
                capture_geometry=None,
                display_scale=None,
                frontmost=process_id == frontmost_process_id,
            ))
        return tuple(candidates)

    def _running_application(self, process_id: int):
        return self._appkit.NSRunningApplication.runningApplicationWithProcessIdentifier_(
            process_id)

    def frontmost_process_id(self) -> int | None:
        application = self._workspace.frontmostApplication()
        if application is None:
            return None
        return int(application.processIdentifier())

    def process_exists(self, process_id: int) -> bool:
        application = self._running_application(process_id)
        return bool(application is not None and not application.isTerminated())

    def window_exists(self, process_id: int, window_id: int) -> bool:
        if process_id <= 0 or window_id <= 0:
            return False
        windows = self._quartz.CGWindowListCopyWindowInfo(
            self._quartz.kCGWindowListOptionIncludingWindow,
            window_id,
        )
        if not windows:
            return False
        return any(
            int(info.get(self._quartz.kCGWindowNumber, 0) or 0) == window_id
            and int(info.get(self._quartz.kCGWindowOwnerPID, 0) or 0) == process_id
            for info in windows
        )

    def request_activation(self, process_id: int) -> bool:
        application = self._running_application(process_id)
        if application is None or application.isTerminated():
            return False
        return bool(application.activateWithOptions_(
            self._appkit.NSApplicationActivateIgnoringOtherApps))


class MacOSWindowDiscovery:
    def __init__(
            self,
            system: MacOSWindowSystem | None = None,
            *,
            discovery_timeout: float = 10.0):
        self.system = system or PyObjCMacOSWindowSystem()
        if discovery_timeout <= 0:
            raise ValueError("discovery_timeout must be positive")
        self.discovery_timeout = discovery_timeout

    def enumerate_candidates(self) -> tuple[WindowCandidate, ...]:
        return self.system.enumerate_windows(self.discovery_timeout)

    def select(
            self,
            hints: WindowMatchHints,
            *,
            stable_hint: StableWindowHint | None = None,
            manual_window_id: int | None = None) -> WindowSelectionResult:
        return select_window_candidate(
            self.enumerate_candidates(),
            hints,
            stable_hint=stable_hint,
            manual_window_id=manual_window_id,
        )

    def bind(
            self,
            selected: WindowCandidate,
            hints: WindowMatchHints,
            *,
            stable_hint: StableWindowHint | None = None,
            monotonic: Callable[[], float] = time.monotonic,
            sleep: Callable[[float], None] = time.sleep) -> "MacOSWindowTarget":
        # Selection and binding are deliberately separate user-visible steps.
        # Re-enumerate here so the target is built from current metadata rather
        # than the possibly stale object shown during selection.
        current_candidates = self.enumerate_candidates()
        current_selection = select_window_candidate(
            current_candidates,
            hints,
            stable_hint=stable_hint,
            manual_window_id=selected.window_id,
        )
        current = current_selection.selected
        if (
                current is None
                or current.runtime_identity != selected.runtime_identity
                or not self.system.process_exists(current.process_id)
                or not self.system.window_exists(
                    current.process_id, current.window_id)):
            raise WindowDiscoveryError(
                "selected macOS window disappeared before it could be bound")
        return MacOSWindowTarget(
            self,
            current,
            hints,
            stable_hint=stable_hint or current.stable_hint(),
            monotonic=monotonic,
            sleep=sleep,
        )


class MacOSWindowTarget(BaseDesktopWindowTarget):
    def __init__(
            self,
            discovery: MacOSWindowDiscovery,
            candidate: WindowCandidate,
            hints: WindowMatchHints,
            *,
            stable_hint: StableWindowHint,
            monotonic: Callable[[], float] = time.monotonic,
            sleep: Callable[[float], None] = time.sleep):
        self.discovery = discovery
        self.hints = hints
        self.stable_hint = stable_hint
        self._monotonic = monotonic
        self._sleep = sleep
        self._state_lock = threading.RLock()
        self._refresh_lock = threading.Lock()
        super().__init__(candidate)

    @property
    def snapshot(self) -> WindowTargetSnapshot:
        with self._state_lock:
            return super().snapshot

    def _candidate_exists(self, candidate: WindowCandidate) -> bool:
        return bool(
            self.discovery.system.process_exists(candidate.process_id)
            and self.discovery.system.window_exists(
                candidate.process_id, candidate.window_id)
        )

    def refresh(self) -> WindowRefreshResult:
        with self._refresh_lock:
            with self._state_lock:
                previous = super().snapshot
                refresh_generation = previous.generation + 1
                self._snapshot = WindowTargetSnapshot(
                    candidate=None,
                    generation=refresh_generation,
                    exists=False,
                )
            try:
                candidates = self.discovery.enumerate_candidates()
                current_identity = (
                    previous.candidate.runtime_identity
                    if previous.candidate else None)
                exact = next(
                    (candidate for candidate in candidates
                     if candidate.runtime_identity == current_identity),
                    None,
                )
                if exact is not None and self._candidate_exists(exact):
                    with self._state_lock:
                        changed = (
                            previous.candidate is None
                            or previous.candidate.binding_signature
                            != exact.binding_signature
                        )
                        self._snapshot = WindowTargetSnapshot(
                            exact, refresh_generation, True)
                        status = (
                            WindowRefreshStatus.REBOUND
                            if not previous.exists
                            else (
                                WindowRefreshStatus.UPDATED
                                if changed else WindowRefreshStatus.UNCHANGED
                            )
                        )
                        return WindowRefreshResult(
                            status, previous, super().snapshot, candidates)

                selection = select_window_candidate(
                    candidates,
                    self.hints,
                    stable_hint=self.stable_hint,
                )
                selected = selection.selected
                if selected is not None and self._candidate_exists(selected):
                    with self._state_lock:
                        self._snapshot = WindowTargetSnapshot(
                            selected, refresh_generation, True)
                        return WindowRefreshResult(
                            WindowRefreshStatus.REBOUND,
                            previous,
                            super().snapshot,
                            selection.candidates,
                        )

                with self._state_lock:
                    status = (
                        WindowRefreshStatus.MANUAL_SELECTION_REQUIRED
                        if selection.status
                        is WindowSelectionStatus.MANUAL_SELECTION_REQUIRED
                        else WindowRefreshStatus.LOST
                    )
                    return WindowRefreshResult(
                        status, previous, super().snapshot,
                        selection.candidates)
            except Exception:
                raise

    def exists(self) -> bool:
        snapshot = self.snapshot
        candidate = snapshot.candidate
        if not snapshot.exists or candidate is None:
            return False
        try:
            exists = self._candidate_exists(candidate)
        except Exception as error:
            with self._state_lock:
                current = super().snapshot
                if (
                        current.generation == snapshot.generation
                        and current.candidate is not None
                        and current.candidate.runtime_identity
                        == candidate.runtime_identity):
                    self._update_candidate(None, exists=False)
            raise WindowDiscoveryError(
                "failed to verify macOS window liveness") from error
        if not exists:
            with self._state_lock:
                current = super().snapshot
                if (
                        current.generation == snapshot.generation
                        and current.candidate is not None
                        and current.candidate.runtime_identity
                        == candidate.runtime_identity):
                    self._update_candidate(None, exists=False)
        return exists

    def is_foreground(self) -> bool:
        return bool(
            self.exists()
            and self.discovery.system.frontmost_process_id() == self.process_id
        )

    def request_activation(self) -> bool:
        if not self.exists():
            return False
        # Returning True means only that the request was accepted.  Callers must
        # still wait for observed frontmost state.
        return self.discovery.system.request_activation(self.process_id)

    def wait_for_observed_activation(
            self, timeout: float, poll_interval: float = 0.05) -> bool:
        if timeout < 0 or poll_interval <= 0:
            raise ValueError("timeout must be non-negative and poll_interval positive")
        deadline = self._monotonic() + timeout
        while True:
            if self.is_foreground():
                return True
            if not self.exists() or self._monotonic() >= deadline:
                return False
            self._sleep(min(poll_interval, max(0, deadline - self._monotonic())))
