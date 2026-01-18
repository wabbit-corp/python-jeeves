import asyncio
import time

import pytest
from hypothesis import given
from hypothesis import strategies as st

from servant.api import rate_limit


class _FakeClock:
    def __init__(self) -> None:
        self.now = 100.0
        self.sleep_calls: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.sleep_calls.append(delay)
        self.now += delay


def _patch_clock(patch: pytest.MonkeyPatch, clock: _FakeClock) -> None:
    patch.setattr(time, "monotonic", clock.monotonic)
    patch.setattr(asyncio, "sleep", clock.sleep)


@given(st.floats(min_value=0.1, max_value=50.0, allow_nan=False, allow_infinity=False))
def test_from_limits_uses_qps(qps: float) -> None:
    limiter = rate_limit.RateLimiter.from_limits(qps=qps)
    assert limiter is not None
    assert limiter._min_interval_s == pytest.approx(1.0 / qps)
    assert limiter._max_calls is None


@given(st.integers(min_value=1, max_value=10_000))
def test_from_limits_uses_rpm(rpm: int) -> None:
    limiter = rate_limit.RateLimiter.from_limits(rpm=rpm)
    assert limiter is not None
    assert limiter._max_calls == rpm
    assert limiter._period_s == pytest.approx(60.0)


@given(
    st.one_of(st.none(), st.floats(max_value=0.0, allow_nan=False, allow_infinity=False)),
    st.one_of(st.none(), st.integers(max_value=0)),
)
def test_from_limits_returns_none_without_limits(qps: float | None, rpm: int | None) -> None:
    limiter = rate_limit.RateLimiter.from_limits(qps=qps, rpm=rpm)
    assert limiter is None


def test_from_limits_prefers_explicit_max_calls() -> None:
    limiter = rate_limit.RateLimiter.from_limits(rpm=10, max_calls=3)
    assert limiter is not None
    assert limiter._max_calls == 3


@given(st.floats(min_value=0.05, max_value=2.0, allow_nan=False, allow_infinity=False))
def test_wait_enforces_min_interval(min_interval: float) -> None:
    clock = _FakeClock()
    patch = pytest.MonkeyPatch()
    try:
        _patch_clock(patch, clock)
        limiter = rate_limit.RateLimiter(min_interval_s=min_interval)

        asyncio.run(limiter.wait())
        assert clock.sleep_calls == []

        asyncio.run(limiter.wait())
        assert clock.sleep_calls[-1] == pytest.approx(min_interval)
        assert limiter._last_call == pytest.approx(clock.now)
    finally:
        patch.undo()


@given(st.integers(min_value=1, max_value=5))
def test_wait_enforces_max_calls(max_calls: int) -> None:
    clock = _FakeClock()
    patch = pytest.MonkeyPatch()
    try:
        _patch_clock(patch, clock)
        limiter = rate_limit.RateLimiter(max_calls=max_calls, period_s=10.0)

        for _ in range(max_calls):
            asyncio.run(limiter.wait())
        assert clock.sleep_calls == []

        asyncio.run(limiter.wait())
        assert clock.sleep_calls[-1] == pytest.approx(10.0)
    finally:
        patch.undo()
