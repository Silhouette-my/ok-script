"""Small platform-selection helpers used by shared import boundaries.

Concrete operating-system implementations must stay in their platform modules.
This module intentionally imports no Win32 or PyObjC packages.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable


WINDOWS = "win32"
MACOS = "darwin"


class PlatformUnavailableError(RuntimeError):
    """Raised when a caller explicitly requests a platform-only capability."""


def is_windows(platform_name: str | None = None) -> bool:
    return (platform_name or sys.platform) == WINDOWS


def is_macos(platform_name: str | None = None) -> bool:
    return (platform_name or sys.platform) == MACOS


def require_platform(
        feature: str,
        supported_platforms: Iterable[str],
        platform_name: str | None = None) -> None:
    """Fail explicitly when *feature* is unavailable on the current platform."""
    current = platform_name or sys.platform
    supported = tuple(supported_platforms)
    if current in supported:
        return
    supported_text = ", ".join(supported)
    raise PlatformUnavailableError(
        f"{feature} is unavailable on platform {current!r}; "
        f"supported platform(s): {supported_text}")


def require_windows(feature: str, platform_name: str | None = None) -> None:
    require_platform(feature, (WINDOWS,), platform_name)
