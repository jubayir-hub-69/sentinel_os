"""Emergency kill-switch for SentinelOS.

Once tripped, every tool call aborts, registered clients are closed, and
the CLI shuts down. There is no resume path in-process (fail-closed).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Final


class KillSwitchActivated(Exception):
    """Raised when the emergency kill-switch has been armed."""


class KillSwitch:
    """Process-wide latch. Trip once; every subsequent action fails closed."""

    def __init__(self) -> None:
        self._tripped = threading.Event()
        self._hooks: list[Callable[[], None]] = []
        self._hook_lock = threading.Lock()

    @property
    def tripped(self) -> bool:
        return self._tripped.is_set()

    def register_shutdown_hook(self, hook: Callable[[], None]) -> None:
        with self._hook_lock:
            if hook not in self._hooks:
                self._hooks.append(hook)

    def trip(self, reason: str = "user command") -> None:
        """Arm the switch, run shutdown hooks, and freeze trading."""
        already = self._tripped.is_set()
        self._tripped.set()
        if already:
            return

        from core.audit import audit

        audit("KILL_SWITCH", "Emergency kill-switch activated.", reason=reason)
        with self._hook_lock:
            hooks = list(self._hooks)
        for hook in hooks:
            try:
                hook()
            except Exception:
                continue

    def raise_if_tripped(self) -> None:
        if self._tripped.is_set():
            raise KillSwitchActivated(
                "Emergency kill-switch is active. All trading is halted. No order was sent."
            )


_SWITCH: Final[KillSwitch] = KillSwitch()


def get_kill_switch() -> KillSwitch:
    return _SWITCH
