"""Small in-process burst guard for authenticated Live Flow endpoints."""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque


class SlidingWindowLimiter:
    def __init__(self) -> None:
        self._events: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def check(
        self,
        identity: str,
        bucket: str,
        *,
        limit: int,
        window_s: float,
        now: float | None = None,
    ) -> float:
        """Return 0 when allowed, otherwise seconds until the next slot."""
        current = time.monotonic() if now is None else float(now)
        cutoff = current - window_s
        key = (identity, bucket)
        async with self._lock:
            events = self._events[key]
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= limit:
                return max(0.001, events[0] + window_s - current)
            events.append(current)
            # Bound idle-key retention without a separate housekeeping task.
            if len(self._events) > 2048:
                stale = [k for k, values in self._events.items() if not values or values[-1] <= cutoff]
                for stale_key in stale[:512]:
                    self._events.pop(stale_key, None)
            return 0.0

    async def clear(self) -> None:
        async with self._lock:
            self._events.clear()


LIMITER = SlidingWindowLimiter()
