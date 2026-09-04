"""Persistent ScreenCaptureKit selected-window capture for macOS."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import Enum
import math
import threading
import time
from typing import Callable, Protocol

from ok.device.capture_methods.base import BaseCaptureMethod
from ok.device.capture_methods.screencapturekit_core import (
    CaptureGeometry,
    LatestFrameSlot,
    PublishedFrame,
    StreamFrameMetadata,
    bgra_to_owned_bgr,
    content_rect_to_pixels,
)
from ok.device.services import PermissionKind
from ok.device.window_target.base import (
    WindowCoordinateSpace,
    WindowGeometry,
    WindowTargetSnapshot,
)
from ok.platform import MACOS, require_platform


_PYOBJC_OUTPUT_CLASS = None
_PYOBJC_DELEGATE_CLASS = None


class CaptureStreamState(str, Enum):
    INITIAL = "initial"
    STARTING = "starting"
    RUNNING = "running"
    TARGET_UNAVAILABLE = "target-unavailable"
    PERMISSION_REQUIRED = "permission-required"
    PERMISSION_REVOKED = "permission-revoked"
    FATAL = "fatal"
    CLOSED = "closed"


class ScreenCaptureKitCaptureError(RuntimeError):
    pass


@dataclass(frozen=True)
class CaptureDiagnostics:
    state: CaptureStreamState
    target_generation: int
    capture_generation: int
    frames_received: int
    frames_published: int
    frames_overwritten: int
    frames_dropped_incomplete: int
    frames_dropped_stale: int
    frame_conversion_errors: int
    geometry_invalidations: int
    rebuilds: int
    fps: float
    frame_age_seconds: float | None
    storage_size: int
    geometry: CaptureGeometry | None
    last_error: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "target_generation": self.target_generation,
            "capture_generation": self.capture_generation,
            "frames_received": self.frames_received,
            "frames_published": self.frames_published,
            "frames_overwritten": self.frames_overwritten,
            "frames_dropped_incomplete": self.frames_dropped_incomplete,
            "frames_dropped_stale": self.frames_dropped_stale,
            "frame_conversion_errors": self.frame_conversion_errors,
            "geometry_invalidations": self.geometry_invalidations,
            "rebuilds": self.rebuilds,
            "fps": self.fps,
            "frame_age_seconds": self.frame_age_seconds,
            "storage_size": self.storage_size,
            "geometry": self.geometry.to_dict() if self.geometry else None,
            "last_error": self.last_error,
        }


class ScreenCaptureKitBackend(Protocol):
    def start_stream(
            self,
            target_snapshot: WindowTargetSnapshot,
            on_sample: Callable[[object, int, int, int, StreamFrameMetadata], None],
            on_stopped: Callable[[str], None],
            on_sample_error: Callable[[str], None],
            *,
            frames_per_second: int,
            timeout: float): ...

    def stop_stream(self, binding, *, timeout: float) -> None: ...


@dataclass
class _PyObjCStreamBinding:
    stream: object
    output: object
    delegate: object
    queue: object


def _objc_value(instance, name: str):
    value = getattr(instance, name)
    return value() if callable(value) else value


def _error_description(error) -> str:
    if error is None:
        return ""
    try:
        return str(_objc_value(error, "localizedDescription"))
    except AttributeError:
        return str(error)


def _rect_components(rect) -> tuple[float, float, float, float]:
    if hasattr(rect, "CGRectValue"):
        rect = rect.CGRectValue()
    try:
        return (
            float(rect.origin.x),
            float(rect.origin.y),
            float(rect.size.width),
            float(rect.size.height),
        )
    except AttributeError:
        (x, y), (width, height) = rect
        return float(x), float(y), float(width), float(height)


def _surface_rect(rect) -> WindowGeometry:
    x, y, width, height = _rect_components(rect)
    return WindowGeometry(x, y, width, height, WindowCoordinateSpace.UNKNOWN)


def _with_locked_bgra_pixel_buffer(quartz, pixel_buffer, consumer):
    """Expose a bounded BGRA view only for the duration of ``consumer``."""
    if quartz.CVPixelBufferIsPlanar(pixel_buffer):
        raise ScreenCaptureKitCaptureError(
            "ScreenCaptureKit returned an unexpected planar pixel buffer")
    pixel_format = quartz.CVPixelBufferGetPixelFormatType(pixel_buffer)
    if pixel_format != quartz.kCVPixelFormatType_32BGRA:
        raise ScreenCaptureKitCaptureError(
            f"ScreenCaptureKit returned pixel format {pixel_format}, expected BGRA")
    lock_flags = quartz.kCVPixelBufferLock_ReadOnly
    lock_status = quartz.CVPixelBufferLockBaseAddress(pixel_buffer, lock_flags)
    if lock_status != 0:
        raise ScreenCaptureKitCaptureError(
            f"CVPixelBufferLockBaseAddress failed with {lock_status}")
    try:
        width = int(quartz.CVPixelBufferGetWidth(pixel_buffer))
        height = int(quartz.CVPixelBufferGetHeight(pixel_buffer))
        bytes_per_row = int(quartz.CVPixelBufferGetBytesPerRow(pixel_buffer))
        if width <= 0 or height <= 0 or bytes_per_row < width * 4:
            raise ScreenCaptureKitCaptureError(
                "CVPixelBuffer returned invalid dimensions or row stride")
        base_address = quartz.CVPixelBufferGetBaseAddress(pixel_buffer)
        if base_address is None:
            raise ScreenCaptureKitCaptureError(
                "CVPixelBuffer returned a null base address")
        view = base_address.as_buffer(bytes_per_row * height)
        return consumer(view, width, height, bytes_per_row)
    finally:
        quartz.CVPixelBufferUnlockBaseAddress(pixel_buffer, lock_flags)


class PyObjCScreenCaptureKitBackend:
    """Thin public-API PyObjC adapter; imported only on Darwin."""

    def __init__(self):
        require_platform("ScreenCaptureKit capture", (MACOS,))
        import CoreMedia
        import Foundation
        import Quartz
        import ScreenCaptureKit
        import dispatch
        import objc

        self._core_media = CoreMedia
        self._foundation = Foundation
        self._quartz = Quartz
        self._screen_capture_kit = ScreenCaptureKit
        self._dispatch = dispatch
        self._objc = objc
        self._output_class = self._make_output_class()
        self._delegate_class = self._make_delegate_class()

    def _make_output_class(self):
        global _PYOBJC_OUTPUT_CLASS
        if _PYOBJC_OUTPUT_CLASS is not None:
            return _PYOBJC_OUTPUT_CLASS
        foundation = self._foundation
        objc = self._objc

        class OKScreenCaptureKitStreamOutput(
                foundation.NSObject,
                protocols=[objc.protocolNamed("SCStreamOutput")]):
            def initWithBackend_callbacks_(self, backend, callbacks):
                self = objc.super(OKScreenCaptureKitStreamOutput, self).init()
                if self is not None:
                    self._backend = backend
                    self._callbacks = callbacks
                return self

            def stream_didOutputSampleBuffer_ofType_(self, _stream, sample_buffer, output_type):
                on_sample, on_sample_error = self._callbacks
                try:
                    with self._backend._objc.autorelease_pool():
                        self._deliver(sample_buffer, output_type, on_sample)
                except Exception as error:
                    on_sample_error(f"ScreenCaptureKit sample conversion failed: {error}")

            @objc.python_method
            def _deliver(self, sample_buffer, output_type, on_sample):
                owner = self._backend
                screen_capture_kit = owner._screen_capture_kit
                core_media = owner._core_media
                quartz = owner._quartz
                if output_type != screen_capture_kit.SCStreamOutputTypeScreen:
                    return
                if (
                        not core_media.CMSampleBufferIsValid(sample_buffer)
                        or not core_media.CMSampleBufferDataIsReady(sample_buffer)):
                    on_sample(None, 0, 0, 0, StreamFrameMetadata(False))
                    return
                attachments = core_media.CMSampleBufferGetSampleAttachmentsArray(
                    sample_buffer, False) or ()
                frame_info = attachments[0] if attachments else {}
                status = frame_info.get(screen_capture_kit.SCStreamFrameInfoStatus)
                complete = (
                    status is not None
                    and int(status) == int(screen_capture_kit.SCFrameStatusComplete)
                )
                content_rect = frame_info.get(
                    screen_capture_kit.SCStreamFrameInfoContentRect)
                scale_factor = frame_info.get(
                    screen_capture_kit.SCStreamFrameInfoScaleFactor)
                content_scale = frame_info.get(
                    screen_capture_kit.SCStreamFrameInfoContentScale)
                metadata = StreamFrameMetadata(
                    complete=complete,
                    content_rect_points=(
                        _surface_rect(content_rect) if content_rect is not None else None),
                    display_scale=(
                        float(scale_factor) if scale_factor is not None else None),
                    content_scale=(
                        float(content_scale) if content_scale is not None else None),
                )
                if not complete:
                    on_sample(None, 0, 0, 0, metadata)
                    return
                pixel_buffer = core_media.CMSampleBufferGetImageBuffer(sample_buffer)
                if pixel_buffer is None:
                    raise ScreenCaptureKitCaptureError(
                        "complete sample has no CVPixelBuffer")
                _with_locked_bgra_pixel_buffer(
                    quartz,
                    pixel_buffer,
                    lambda view, width, height, bytes_per_row: on_sample(
                        view, width, height, bytes_per_row, metadata),
                )

        _PYOBJC_OUTPUT_CLASS = OKScreenCaptureKitStreamOutput
        return _PYOBJC_OUTPUT_CLASS

    def _make_delegate_class(self):
        global _PYOBJC_DELEGATE_CLASS
        if _PYOBJC_DELEGATE_CLASS is not None:
            return _PYOBJC_DELEGATE_CLASS
        foundation = self._foundation
        objc = self._objc

        class OKScreenCaptureKitStreamDelegate(
                foundation.NSObject,
                protocols=[objc.protocolNamed("SCStreamDelegate")]):
            def initWithCallback_(self, callback):
                self = objc.super(OKScreenCaptureKitStreamDelegate, self).init()
                if self is not None:
                    self._callback = callback
                return self

            def stream_didStopWithError_(self, _stream, error):
                self._callback(_error_description(error))

        _PYOBJC_DELEGATE_CLASS = OKScreenCaptureKitStreamDelegate
        return _PYOBJC_DELEGATE_CLASS

    def _shareable_content(self, timeout: float):
        completed = threading.Event()
        result: dict[str, object] = {}

        def completion(content, error):
            result["content"] = content
            result["error"] = error
            completed.set()

        self._screen_capture_kit.SCShareableContent.getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(
            True, True, completion)
        if not completed.wait(timeout):
            raise ScreenCaptureKitCaptureError(
                f"ScreenCaptureKit source resolution timed out after {timeout:.1f}s")
        error = result.get("error")
        if error is not None:
            raise ScreenCaptureKitCaptureError(
                f"ScreenCaptureKit source resolution failed: {_error_description(error)}")
        content = result.get("content")
        if content is None:
            raise ScreenCaptureKitCaptureError(
                "ScreenCaptureKit returned no shareable content")
        return content

    @staticmethod
    def _intersection_area(first, second) -> float:
        fx, fy, fw, fh = _rect_components(first)
        sx, sy, sw, sh = _rect_components(second)
        width = max(0.0, min(fx + fw, sx + sw) - max(fx, sx))
        height = max(0.0, min(fy + fh, sy + sh) - max(fy, sy))
        return width * height

    def _display_scale(self, content, window) -> float:
        window_frame = _objc_value(window, "frame")
        selected = None
        selected_area = 0.0
        for display in _objc_value(content, "displays"):
            display_frame = _objc_value(display, "frame")
            area = self._intersection_area(window_frame, display_frame)
            if area > selected_area:
                selected = display
                selected_area = area
        if selected is None:
            raise ScreenCaptureKitCaptureError(
                "could not determine the display containing the selected window")
        _, _, logical_width, logical_height = _rect_components(
            _objc_value(selected, "frame"))
        scales = []
        if logical_width > 0:
            scales.append(float(_objc_value(selected, "width")) / logical_width)
        if logical_height > 0:
            scales.append(float(_objc_value(selected, "height")) / logical_height)
        if not scales or any(not math.isfinite(value) or value <= 0 for value in scales):
            raise ScreenCaptureKitCaptureError(
                "selected display returned an invalid pixel-to-point scale")
        if len(scales) == 2 and abs(scales[0] - scales[1]) > 0.01:
            raise ScreenCaptureKitCaptureError(
                "selected display returned inconsistent horizontal/vertical scale")
        return sum(scales) / len(scales)

    def start_stream(
            self,
            target_snapshot: WindowTargetSnapshot,
            on_sample,
            on_stopped,
            on_sample_error,
            *,
            frames_per_second: int,
            timeout: float):
        candidate = target_snapshot.candidate
        if candidate is None or not target_snapshot.exists:
            raise ScreenCaptureKitCaptureError("macOS window target is unavailable")
        content = self._shareable_content(timeout)
        selected_window = None
        for window in _objc_value(content, "windows"):
            application = _objc_value(window, "owningApplication")
            if application is None:
                continue
            if (
                    int(_objc_value(window, "windowID") or 0) == candidate.window_id
                    and int(_objc_value(application, "processID") or 0)
                    == candidate.process_id):
                selected_window = window
                break
        if selected_window is None:
            raise ScreenCaptureKitCaptureError(
                "selected macOS window disappeared before capture stream creation")

        screen_capture_kit = self._screen_capture_kit
        configuration = screen_capture_kit.SCStreamConfiguration.alloc().init()
        scale = self._display_scale(content, selected_window)
        _, _, logical_width, logical_height = _rect_components(
            _objc_value(selected_window, "frame"))
        configuration.setWidth_(max(1, round(logical_width * scale)))
        configuration.setHeight_(max(1, round(logical_height * scale)))
        configuration.setPixelFormat_(self._quartz.kCVPixelFormatType_32BGRA)
        configuration.setMinimumFrameInterval_(
            self._core_media.CMTimeMake(1, frames_per_second))
        configuration.setQueueDepth_(3)
        configuration.setShowsCursor_(False)
        if hasattr(configuration, "setCapturesAudio_"):
            configuration.setCapturesAudio_(False)
        if hasattr(configuration, "setScalesToFit_"):
            configuration.setScalesToFit_(True)
        if hasattr(configuration, "setIgnoreShadowsSingleWindow_"):
            configuration.setIgnoreShadowsSingleWindow_(True)

        content_filter = screen_capture_kit.SCContentFilter.alloc().initWithDesktopIndependentWindow_(
            selected_window)
        delegate = self._delegate_class.alloc().initWithCallback_(on_stopped)
        output = self._output_class.alloc().initWithBackend_callbacks_(
            self, (on_sample, on_sample_error))
        stream = screen_capture_kit.SCStream.alloc().initWithFilter_configuration_delegate_(
            content_filter, configuration, delegate)
        queue = self._dispatch.dispatch_queue_create(
            b"com.ok-script.screencapturekit.frames", None)
        added, error = stream.addStreamOutput_type_sampleHandlerQueue_error_(
            output,
            screen_capture_kit.SCStreamOutputTypeScreen,
            queue,
            None,
        )
        if not added:
            raise ScreenCaptureKitCaptureError(
                f"failed to add ScreenCaptureKit stream output: {_error_description(error)}")

        completed = threading.Event()
        result: dict[str, object] = {}

        def started(error):
            result["error"] = error
            completed.set()

        stream.startCaptureWithCompletionHandler_(started)
        if not completed.wait(timeout):
            stream.stopCaptureWithCompletionHandler_(None)
            raise ScreenCaptureKitCaptureError(
                f"ScreenCaptureKit start timed out after {timeout:.1f}s")
        error = result.get("error")
        if error is not None:
            stream.stopCaptureWithCompletionHandler_(None)
            raise ScreenCaptureKitCaptureError(
                f"ScreenCaptureKit failed to start: {_error_description(error)}")
        return _PyObjCStreamBinding(stream, output, delegate, queue)

    def stop_stream(self, binding, *, timeout: float) -> None:
        completed = threading.Event()
        result: dict[str, object] = {}

        def stopped(error):
            result["error"] = error
            completed.set()

        binding.stream.stopCaptureWithCompletionHandler_(stopped)
        if not completed.wait(timeout):
            raise ScreenCaptureKitCaptureError(
                f"ScreenCaptureKit stop timed out after {timeout:.1f}s")
        error = result.get("error")
        if error is not None:
            raise ScreenCaptureKitCaptureError(
                f"ScreenCaptureKit failed to stop: {_error_description(error)}")


class ScreenCaptureKitCaptureMethod(BaseCaptureMethod):
    """One persistent stream bound to one immutable target generation."""

    name = "ScreenCaptureKit"

    def __init__(
            self,
            exit_event,
            target,
            permission_service,
            *,
            backend: ScreenCaptureKitBackend | None = None,
            frames_per_second: int = 30,
            lifecycle_timeout: float = 10.0,
            monotonic: Callable[[], float] = time.monotonic):
        super().__init__()
        if frames_per_second <= 0:
            raise ValueError("frames_per_second must be positive")
        if lifecycle_timeout <= 0:
            raise ValueError("lifecycle_timeout must be positive")
        self.exit_event = exit_event
        self.target = target
        self.permission_service = permission_service
        self.backend = backend or PyObjCScreenCaptureKitBackend()
        self.frames_per_second = frames_per_second
        self.lifecycle_timeout = lifecycle_timeout
        self._monotonic = monotonic
        self._slot = LatestFrameSlot()
        self._state_lock = threading.RLock()
        self._lifecycle_lock = threading.Lock()
        self._stream = None
        self._unconfirmed_stream = None
        self._state = CaptureStreamState.INITIAL
        self._target_generation = -1
        self._capture_generation = 0
        self._blocked_generation: int | None = None
        self._frames_received = 0
        self._dropped_incomplete = 0
        self._dropped_stale = 0
        self._conversion_errors = 0
        self._rebuilds = 0
        self._sequence = 0
        self._frame_times: deque[float] = deque(maxlen=120)
        self._latest_geometry: CaptureGeometry | None = None
        self._geometry_signature: tuple[object, ...] | None = None
        self._needs_rebuild = False
        self._latest_frame_time: float | None = None
        self._last_error: str | None = None
        self._geometry_invalidations = 0
        self._synchronize_stream()

    def _permission_status(self):
        return self.permission_service.status(PermissionKind.SCREEN_RECORDING)

    def _set_unavailable(self, state: CaptureStreamState, detail: str) -> None:
        with self._state_lock:
            self._state = state
            self._last_error = detail
            self._latest_geometry = None
            self._geometry_signature = None
            self._latest_frame_time = None
            self._frame_times.clear()
            self._size = (0, 0)
        self._slot.clear()

    def _detach_stream(self):
        with self._state_lock:
            stream = self._stream
            self._stream = None
            self._capture_generation += 1
            self._latest_geometry = None
            self._geometry_signature = None
            self._latest_frame_time = None
            self._frame_times.clear()
            self._size = (0, 0)
        self._slot.clear()
        return stream

    def _stop_binding(self, stream) -> str | None:
        if stream is None:
            return None
        try:
            self.backend.stop_stream(stream, timeout=self.lifecycle_timeout)
        except Exception as error:
            detail = f"ScreenCaptureKit stop failed: {error}"
            with self._state_lock:
                self._unconfirmed_stream = stream
                self._last_error = detail
            return detail
        with self._state_lock:
            if self._unconfirmed_stream is stream:
                self._unconfirmed_stream = None
        return None

    def _set_fatal(
            self,
            detail: str,
            *,
            blocked_generation: int | None = None,
            expected_capture_generation: int | None = None) -> bool:
        with self._state_lock:
            if (
                    expected_capture_generation is not None
                    and expected_capture_generation != self._capture_generation):
                return False
            self._stream = None
            self._capture_generation += 1
            self._state = CaptureStreamState.FATAL
            self._blocked_generation = (
                self._target_generation
                if blocked_generation is None else blocked_generation)
            self._last_error = detail
            self._latest_geometry = None
            self._geometry_signature = None
            self._latest_frame_time = None
            self._frame_times.clear()
            self._size = (0, 0)
        self._slot.clear()
        return True

    def invalidate(self, reason: str = "capture-invalidated") -> None:
        """Immediately reject the current generation before refresh/rebind."""
        with self._lifecycle_lock:
            stream = self._detach_stream()
            with self._state_lock:
                if self._state is not CaptureStreamState.CLOSED:
                    self._state = CaptureStreamState.TARGET_UNAVAILABLE
                    self._blocked_generation = self._target_generation
                    self._last_error = reason
            stop_error = self._stop_binding(stream)
            if stop_error is not None:
                self._set_fatal(stop_error)

    def _synchronize_stream(self) -> None:
        with self._lifecycle_lock:
            with self._state_lock:
                if self._state is CaptureStreamState.CLOSED:
                    return
                if self._unconfirmed_stream is not None:
                    self._state = CaptureStreamState.FATAL
                    return
            permission = self._permission_status()
            if not permission.granted:
                stream = self._detach_stream() if self._stream is not None else None
                state = (
                    CaptureStreamState.PERMISSION_REVOKED
                    if permission.state.value == "permission-revoked"
                    else CaptureStreamState.PERMISSION_REQUIRED)
                self._set_unavailable(
                    state,
                    permission.detail or (
                        f"{permission.state.value}: grant Screen Recording at "
                        f"{permission.settings_path}"),
                )
                stop_error = self._stop_binding(stream)
                if stop_error is not None:
                    self._set_fatal(stop_error)
                return

            try:
                target_exists = bool(self.target.exists())
            except Exception as error:
                stream = self._detach_stream() if self._stream is not None else None
                self._set_unavailable(
                    CaptureStreamState.TARGET_UNAVAILABLE,
                    f"failed to verify selected macOS window target: {error}",
                )
                stop_error = self._stop_binding(stream)
                if stop_error is not None:
                    self._set_fatal(stop_error)
                return

            snapshot = self.target.snapshot
            if not target_exists or not snapshot.exists or snapshot.candidate is None:
                stream = self._detach_stream() if self._stream is not None else None
                self._set_unavailable(
                    CaptureStreamState.TARGET_UNAVAILABLE,
                    "selected macOS window target is unavailable",
                )
                stop_error = self._stop_binding(stream)
                if stop_error is not None:
                    self._set_fatal(stop_error)
                return

            with self._state_lock:
                current_stream = self._stream
                current_generation = self._target_generation
                blocked = self._blocked_generation == snapshot.generation
                needs_rebuild = self._needs_rebuild
            if (
                    current_stream is not None
                    and current_generation == snapshot.generation
                    and not needs_rebuild):
                return
            if blocked:
                return

            old_stream = self._detach_stream() if current_stream is not None else None
            stop_error = self._stop_binding(old_stream)
            if stop_error is not None:
                self._set_fatal(stop_error, blocked_generation=snapshot.generation)
                return
            with self._state_lock:
                if self._state in (
                        CaptureStreamState.CLOSED,
                        CaptureStreamState.FATAL):
                    return
                self._needs_rebuild = False
                self._capture_generation += 1
                capture_generation = self._capture_generation
                self._target_generation = snapshot.generation
                self._state = CaptureStreamState.STARTING
                self._last_error = None
                if current_generation >= 0:
                    self._rebuilds += 1

            def on_sample(buffer, width, height, bytes_per_row, metadata):
                self._on_sample(
                    capture_generation,
                    snapshot,
                    buffer,
                    width,
                    height,
                    bytes_per_row,
                    metadata,
                )

            try:
                stream = self.backend.start_stream(
                    snapshot,
                    on_sample,
                    lambda detail: self._on_stream_stopped(
                        capture_generation, detail),
                    lambda detail: self._on_sample_error(
                        capture_generation, detail),
                    frames_per_second=self.frames_per_second,
                    timeout=self.lifecycle_timeout,
                )
            except Exception as error:
                detail = str(error)
                self._set_fatal(
                    detail,
                    blocked_generation=snapshot.generation,
                    expected_capture_generation=capture_generation,
                )
                return
            with self._state_lock:
                if (
                        capture_generation != self._capture_generation
                        or self._state is not CaptureStreamState.STARTING):
                    stale_stream = stream
                else:
                    self._stream = stream
                    self._state = CaptureStreamState.RUNNING
                    self._blocked_generation = None
                    stale_stream = None
            self._stop_binding(stale_stream)

    def _on_sample(
            self,
            capture_generation: int,
            target_snapshot: WindowTargetSnapshot,
            buffer,
            width: int,
            height: int,
            bytes_per_row: int,
            metadata: StreamFrameMetadata) -> None:
        now = self._monotonic()
        with self._state_lock:
            self._frames_received += 1
            if not metadata.complete:
                self._dropped_incomplete += 1
                return
            live_snapshot = self.target.snapshot
            if (
                    capture_generation != self._capture_generation
                    or self._state not in (
                        CaptureStreamState.STARTING,
                        CaptureStreamState.RUNNING)
                    or target_snapshot.generation != live_snapshot.generation
                    or not live_snapshot.exists):
                self._dropped_stale += 1
                self._slot.clear()
                return
        candidate = target_snapshot.candidate
        if candidate is None:
            return
        try:
            if metadata.content_rect_points is None:
                raise ValueError("complete frame has no ScreenCaptureKit content rect")
            if metadata.display_scale is None:
                raise ValueError("complete frame has no ScreenCaptureKit scale factor")
            display_scale = metadata.display_scale
            crop = content_rect_to_pixels(
                metadata.content_rect_points,
                display_scale,
                width,
                height,
            )
            frame = bgra_to_owned_bgr(
                buffer,
                width=width,
                height=height,
                bytes_per_row=bytes_per_row,
                crop=crop,
            )
            global_content = candidate.content_geometry or candidate.outer_geometry
            geometry = CaptureGeometry(
                target_generation=target_snapshot.generation,
                capture_generation=capture_generation,
                outer_geometry=candidate.outer_geometry,
                global_content_geometry=global_content,
                raw_frame_width=width,
                raw_frame_height=height,
                content_rect_pixels=crop,
                frame_width=frame.shape[1],
                frame_height=frame.shape[0],
                display_scale=display_scale,
                content_scale=metadata.content_scale,
            )
            geometry_signature = (
                width,
                height,
                crop,
                display_scale,
                metadata.content_scale,
                global_content,
            )
        except Exception as error:
            self._on_sample_error(
                capture_generation,
                f"ScreenCaptureKit frame normalization failed: {error}",
            )
            return

        with self._state_lock:
            live_snapshot = self.target.snapshot
            if (
                    capture_generation != self._capture_generation
                    or self._state not in (
                        CaptureStreamState.STARTING,
                        CaptureStreamState.RUNNING)
                    or target_snapshot.generation != live_snapshot.generation
                    or not live_snapshot.exists):
                self._dropped_stale += 1
                self._slot.clear()
                return
            if (
                    self._geometry_signature is not None
                    and self._geometry_signature != geometry_signature):
                # The current stream configuration/coordinate mapping is no
                # longer immutable.  Reject this sample and every late sample;
                # the next consumer poll rebuilds the persistent stream.
                self._capture_generation += 1
                self._needs_rebuild = True
                self._state = CaptureStreamState.TARGET_UNAVAILABLE
                self._geometry_invalidations += 1
                self._latest_geometry = None
                self._latest_frame_time = None
                self._frame_times.clear()
                self._size = (0, 0)
                self._last_error = "ScreenCaptureKit frame geometry changed; rebuilding stream"
                self._slot.clear()
                return
            self._sequence += 1
            published = PublishedFrame(frame, geometry, self._sequence, now)
            self._latest_geometry = geometry
            self._geometry_signature = geometry_signature
            self._latest_frame_time = now
            self._frame_times.append(now)
            self._size = (frame.shape[1], frame.shape[0])
            self._slot.publish(published)

    def _on_sample_error(self, capture_generation: int, detail: str) -> None:
        with self._state_lock:
            if capture_generation != self._capture_generation:
                return
            self._conversion_errors += 1
            self._last_error = detail
        # Never let a consumer unknowingly reuse an older frame after the
        # current complete sample could not be normalized safely.
        self._slot.clear()

    def _on_stream_stopped(self, capture_generation: int, detail: str) -> None:
        self._set_fatal(
            detail or "ScreenCaptureKit stream stopped unexpectedly",
            expected_capture_generation=capture_generation,
        )

    def get_frame_packet(self) -> PublishedFrame | None:
        """Return the latest frame together with its immutable geometry."""
        self._synchronize_stream()
        with self._state_lock:
            snapshot = self.target.snapshot
            state = self._state
            target_generation = self._target_generation
            capture_generation = self._capture_generation
            detail = self._last_error
            if state in (
                    CaptureStreamState.PERMISSION_REQUIRED,
                    CaptureStreamState.PERMISSION_REVOKED,
                    CaptureStreamState.FATAL):
                raise ScreenCaptureKitCaptureError(detail or state.value)
            if snapshot.generation != target_generation or not snapshot.exists:
                self._slot.clear()
                return None
            return self._slot.read(
                target_generation=target_generation,
                capture_generation=capture_generation,
            )

    def do_get_frame(self):
        published = self.get_frame_packet()
        return published.frame if published is not None else None

    def connected(self):
        self._synchronize_stream()
        with self._state_lock:
            return bool(
                self._state is CaptureStreamState.RUNNING
                and self._stream is not None
                and self.target.snapshot.exists
            )

    @property
    def geometry(self) -> CaptureGeometry | None:
        with self._state_lock:
            return self._latest_geometry

    def frame_pixel_to_global_point(
            self,
            x: float,
            y: float,
            *,
            geometry: CaptureGeometry | None = None) -> tuple[float, float]:
        geometry = geometry or self.geometry
        if geometry is None:
            raise ScreenCaptureKitCaptureError("no current capture geometry")
        return geometry.frame_pixel_to_global_point(x, y)

    def diagnostics(self) -> CaptureDiagnostics:
        now = self._monotonic()
        with self._state_lock:
            times = tuple(self._frame_times)
            if len(times) >= 2 and times[-1] > times[0]:
                fps = (len(times) - 1) / (times[-1] - times[0])
            else:
                fps = 0.0
            frame_age = (
                max(0.0, now - self._latest_frame_time)
                if self._latest_frame_time is not None else None)
            return CaptureDiagnostics(
                state=self._state,
                target_generation=self._target_generation,
                capture_generation=self._capture_generation,
                frames_received=self._frames_received,
                frames_published=self._slot.published,
                frames_overwritten=self._slot.overwritten,
                frames_dropped_incomplete=self._dropped_incomplete,
                frames_dropped_stale=self._dropped_stale,
                frame_conversion_errors=self._conversion_errors,
                geometry_invalidations=self._geometry_invalidations,
                rebuilds=self._rebuilds,
                fps=fps,
                frame_age_seconds=frame_age,
                storage_size=self._slot.storage_size,
                geometry=self._latest_geometry,
                last_error=self._last_error,
            )

    def close(self):
        with self._lifecycle_lock:
            with self._state_lock:
                if self._state is CaptureStreamState.CLOSED:
                    return
                self._state = CaptureStreamState.CLOSED
            stream = self._detach_stream()
            with self._state_lock:
                unconfirmed_stream = self._unconfirmed_stream
                self._unconfirmed_stream = None
            with self._state_lock:
                self._state = CaptureStreamState.CLOSED
            self._stop_binding(stream)
            if unconfirmed_stream is not None and unconfirmed_stream is not stream:
                self._stop_binding(unconfirmed_stream)
            with self._state_lock:
                # Closing is terminal. Drop our final references even when the
                # native stop completion could not be confirmed.
                self._unconfirmed_stream = None
