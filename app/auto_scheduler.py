"""Safe, disabled-by-default background scheduler for paper-only cycles."""
from __future__ import annotations
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Any

class PaperAutoScheduler:
    def __init__(self, *, enabled: Callable[[], bool], paused: Callable[[], bool],
                 run_cycle: Callable[[], Any], audit: Callable[[str, dict], None],
                 interval_seconds: int = 300) -> None:
        if interval_seconds < 60:
            raise ValueError("Scheduler interval must be at least 60 seconds.")
        self.enabled, self.paused = enabled, paused
        self.run_cycle, self.audit = run_cycle, audit
        self.interval_seconds = interval_seconds
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._cycle_lock = threading.Lock()
        self.last_cycle_at: str | None = None
        self.last_cycle_status = "never_run"
        self.last_cycle_error: str | None = None
        self.last_cycle_duration_seconds: float | None = None
        self.cycles_completed = 0
        self.cycles_failed = 0

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="paper-auto-scheduler", daemon=True)
        self._thread.start()
        self.audit("AUTO_SCHEDULER_STARTED", {"interval_seconds": self.interval_seconds, "live_trading": False})

    def stop(self, timeout: float = 10.0) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout))
        self.audit("AUTO_SCHEDULER_STOPPED", {"thread_stopped": not self.running, "live_trading": False})

    def run_once(self) -> dict:
        if not self.enabled():
            return {"status": "disabled", "reason": "Automated paper trading is disabled."}
        if self.paused():
            return {"status": "paused", "reason": "Paper trading is paused."}
        if not self._cycle_lock.acquire(blocking=False):
            return {"status": "skipped", "reason": "A paper cycle is already running."}
        started = time.monotonic()
        self.last_cycle_at = datetime.now(timezone.utc).isoformat()
        self.last_cycle_error = None
        try:
            result = self.run_cycle()
            self.last_cycle_status = "completed"
            self.cycles_completed += 1
            self.audit("AUTO_SCHEDULER_CYCLE", {"status": "completed", "live_trading": False})
            return {"status": "completed", "result": result}
        except Exception as exc:
            self.last_cycle_status = "failed"
            self.last_cycle_error = str(getattr(exc, "detail", "") or type(exc).__name__)[:240]
            self.cycles_failed += 1
            self.audit("AUTO_SCHEDULER_CYCLE_FAILED", {"error_type": type(exc).__name__, "live_trading": False})
            return {"status": "failed", "reason": type(exc).__name__}
        finally:
            self.last_cycle_duration_seconds = round(time.monotonic() - started, 3)
            self._cycle_lock.release()

    def _run(self) -> None:
        # Check enable/pause state each second. Run immediately on enable/resume,
        # then only when the configured cycle cadence is due.
        was_active = False
        last_run_monotonic: float | None = None
        while not self._stop_event.is_set():
            try:
                active = bool(self.enabled()) and not bool(self.paused())
                now = time.monotonic()
                due = last_run_monotonic is None or now - last_run_monotonic >= self.interval_seconds
                if active and (not was_active or due):
                    self.run_once()
                    last_run_monotonic = now
                was_active = active
            except Exception as exc:
                self.last_cycle_at = datetime.now(timezone.utc).isoformat()
                self.last_cycle_status = "failed"
                self.last_cycle_error = str(getattr(exc, "detail", "") or type(exc).__name__)[:240]
                self.cycles_failed += 1
                try:
                    self.audit("AUTO_SCHEDULER_TICK_FAILED", {
                        "error_type": type(exc).__name__, "live_trading": False,
                    })
                except Exception:
                    pass
            self._stop_event.wait(min(1.0, self.interval_seconds))
