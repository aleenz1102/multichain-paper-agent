"""Disabled-by-default, audit-only automatic paper-trading monitor.

This module intentionally cannot submit orders or modify portfolio balances.
It requires genuine chronological candle data supplied by its caller.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock
from typing import Callable

from app.strategy import combined_signal


@dataclass(frozen=True)
class MonitorConfig:
    interval_seconds: int = 300
    enabled: bool = False

    def __post_init__(self):
        if self.interval_seconds < 60:
            raise ValueError("Monitor interval must be at least 60 seconds.")


class PaperSignalMonitor:
    """Evaluate signals and audit results; never execute orders."""

    def __init__(
        self,
        candle_provider: Callable[[str], list[dict]],
        audit: Callable[[str, dict], None],
        config: MonitorConfig = MonitorConfig(),
    ):
        self.candle_provider = candle_provider
        self.audit = audit
        self.config = config
        self._lock = Lock()

    def evaluate(self, symbols: list[str]) -> dict:
        if not self.config.enabled:
            return {
                "status": "disabled",
                "mode": "AUDIT_ONLY",
                "evaluated": 0,
                "message": "Monitor is disabled; no evaluation or order was performed.",
            }

        # Prevent two scheduler/manual invocations from evaluating simultaneously.
        if not self._lock.acquire(blocking=False):
            return {
                "status": "skipped",
                "mode": "AUDIT_ONLY",
                "reason": "Evaluation already in progress.",
                "evaluated": 0,
            }

        results = []
        try:
            for raw_symbol in symbols:
                symbol = raw_symbol.strip().upper()
                if not symbol:
                    continue

                timestamp = datetime.now(timezone.utc).isoformat()
                candle_timestamp = None
                try:
                    candles = self.candle_provider(symbol)
                    if isinstance(candles, list) and candles and isinstance(candles[-1], dict):
                        candle_timestamp = candles[-1].get("timestamp")
                    if not isinstance(candles, list) or len(candles) < 30:
                        result = {
                            "symbol": symbol,
                            "signal": "HOLD",
                            "reason": "Insufficient candle history; at least 30 candles are required.",
                        }
                    else:
                        # Public candle data includes timestamps. If any are present,
                        # require every timestamp and strict chronological uniqueness.
                        times = [c.get("timestamp") for c in candles]
                        if any(t is not None for t in times):
                            if any(not isinstance(t, str) or not t.strip() for t in times):
                                result = {
                                    "symbol": symbol,
                                    "signal": "HOLD",
                                    "reason": "Candle timestamps are missing or invalid.",
                                }
                            elif times != sorted(times) or len(set(times)) != len(times):
                                result = {
                                    "symbol": symbol,
                                    "signal": "HOLD",
                                    "reason": "Candle timestamps are not strictly chronological.",
                                }
                            else:
                                signal = combined_signal(candles)
                                result = {"symbol": symbol, **signal}
                        else:
                            signal = combined_signal(candles)
                            result = {"symbol": symbol, **signal}

                except (KeyError, TypeError, ValueError) as exc:
                    result = {
                        "symbol": symbol,
                        "signal": "HOLD",
                        "reason": f"Invalid candle data: {exc}",
                    }
                except Exception as exc:
                    result = {
                        "symbol": symbol,
                        "signal": "HOLD",
                        "reason": f"Candle provider failed: {type(exc).__name__}.",
                    }

                event = {
                    "timestamp": timestamp,
                    "candle_timestamp": candle_timestamp,
                    "mode": "AUDIT_ONLY",
                    "orders_submitted": False,
                    **result,
                }
                self.audit("AUTO_SIGNAL_EVALUATED", event)
                results.append(event)

            return {
                "status": "completed",
                "mode": "AUDIT_ONLY",
                "orders_submitted": False,
                "evaluated": len(results),
                "results": results,
            }
        finally:
            self._lock.release()
