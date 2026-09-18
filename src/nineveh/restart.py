from __future__ import annotations

import os
import signal
import threading


class DisabledRestartController:
    enabled = False

    def request_restart(self) -> None:
        raise RuntimeError("Automatic restart is not enabled for this deployment")


class ProcessRestartController:
    """Gracefully terminates the service after its HTTP response has been sent."""

    enabled = True

    def __init__(self, delay_seconds: float = 0.75) -> None:
        self._delay_seconds = delay_seconds

    def request_restart(self) -> None:
        timer = threading.Timer(
            self._delay_seconds, os.kill, args=(os.getpid(), signal.SIGTERM)
        )
        timer.daemon = True
        timer.start()
