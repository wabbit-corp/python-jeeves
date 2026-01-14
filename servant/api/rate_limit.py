from __future__ import annotations

import asyncio
import time
from collections import deque


class RateLimiter:
    def __init__(
        self,
        *,
        min_interval_s: float | None = None,
        max_calls: int | None = None,
        period_s: float = 60.0,
    ) -> None:
        self._min_interval_s = min_interval_s if (min_interval_s or 0) > 0 else None
        self._max_calls = max_calls if (max_calls or 0) > 0 else None
        self._period_s = float(period_s)
        self._lock = asyncio.Lock()
        self._last_call = 0.0
        self._calls: deque[float] = deque()

    @classmethod
    def from_limits(
        cls,
        *,
        qps: float | None = None,
        rpm: int | None = None,
        min_interval_s: float | None = None,
        max_calls: int | None = None,
        period_s: float = 60.0,
    ) -> "RateLimiter | None":
        if min_interval_s is None and qps is not None:
            try:
                qps_val = float(qps)
                if qps_val > 0:
                    min_interval_s = 1.0 / qps_val
            except Exception:
                min_interval_s = None

        if max_calls is None and rpm is not None:
            try:
                max_calls = int(rpm)
                period_s = 60.0
            except Exception:
                max_calls = None

        if (min_interval_s is None or min_interval_s <= 0) and (max_calls is None or max_calls <= 0):
            return None
        return cls(
            min_interval_s=min_interval_s,
            max_calls=max_calls,
            period_s=period_s,
        )

    async def wait(self) -> None:
        if self._min_interval_s is None and self._max_calls is None:
            return
        async with self._lock:
            now = time.monotonic()
            delay = 0.0

            if self._min_interval_s:
                next_ok = self._last_call + self._min_interval_s
                if next_ok > now:
                    delay = max(delay, next_ok - now)

            if self._max_calls:
                while self._calls and now - self._calls[0] >= self._period_s:
                    self._calls.popleft()
                if len(self._calls) >= self._max_calls:
                    oldest = self._calls[0]
                    delay = max(delay, self._period_s - (now - oldest))

            if delay > 0:
                await asyncio.sleep(delay)
                now = time.monotonic()
                if self._max_calls:
                    while self._calls and now - self._calls[0] >= self._period_s:
                        self._calls.popleft()

            now = time.monotonic()
            self._last_call = now
            if self._max_calls:
                self._calls.append(now)
