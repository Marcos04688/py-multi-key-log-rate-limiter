"""Core implementation of a multi-key log rate limiter.

The limiter groups records by a user-provided key extraction function and keeps an
independent token bucket per group. The decision is whether to emit the record and
how many it was suppressed, which makes it straightforward to wire into a logging
handler that periodically flushes the suppression count.

Design decisions that are not obvious from the code:

* A token bucket is used instead of a fixed window because the bucket gives a smooth,
  bounded rate without boundary spikes. A fixed window can let through a full burst at
  the edge of the window, which is precisely the pattern a log rate limiter is supposed
  to absorb.

* The clock is an injected callable rather than reading time.monotonic() internally.
  This keeps tests deterministic and lets callers share a single clock across multiple
  limiters if they care about ordering.

* The key extractor is allowed to raise or return a value that is not hashable. In both
cases the record is routed to a dedicated default bucket whose key is the sentinel
_DEFAULT_KEY. Routing failures to a single bucket means a broken extractor degrades to a
single noisy group rather than silently swallowing records or leaking memory by making a
new bucket per exception.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Hashable, Optional, Tuple


_DEFAULT_KEY: Tuple[()] = ()  # Sentinel default bucket key; tuple() is a stable singleton.


class RateLimitDecision:
    """Result of asking the limiter whether a record should be emitted.

    Fields:
        emit: True when the caller should write the record; False when suppressed.
        suppressed: Number of records suppressed since the last emit for this key,
            excluding the current one. Always zero when emit is True.
        key: The bucket key the decision was made for. Exposed so callers can
            report which group was limited without re-running the extractor.
    """

    __slots__ = ("emit", "suppressed", "key")

    def __init__(self, emit: bool, suppressed: int, key: Hashable):
        # suppressed is an int because float arithmetic on counters invites
        # rounding surprises; counts of records are whole by definition.
        self.emit = emit
        self.suppressed = int(suppressed)
        self.key = key

    def __repr__(self) -> str:
        return (
            f"RateLimitDecision(emit={self.emit!r}, "
            f"suppressed={self.suppressed!r}, key={self.key!r})"
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, RateLimitDecision):
            return NotImplemented
        return (
            self.emit == other.emit
            and self.suppressed == other.suppressed
            and self.key == other.key
        )

    def __hash__(self) -> int:
        return hash((self.emit, self.suppressed, self.key))


# Type alias for readability; kept simple so callers can subclass without generics gymnastics.
RateLimitResult = RateLimitDecision


class _TokenBucket:
    """A single per-key token bucket.

    A bucket holds at most ``capacity`` tokens and refills at ``rate`` tokens per
clock unit. consume() withdraws one token if available and reports the running
    suppressed count.
    """

    __slots__ = ("tokens", "capacity", "rate", "last_refill", "suppressed")

    def __init__(
        self,
        capacity: float,
        rate: float,
        now: float,
        suppressed: int = 0,
    ):
        # Start full so the first burst within capacity is allowed; this is the
        # conventional and least surprising initial state.
        self.tokens = capacity
        self.capacity = capacity
        self.rate = rate
        self.last_refill = now
        self.suppressed = suppressed

    def _refill(self, now: float) -> None:
        elapsed = now - self.last_refill
        if elapsed <= 0:
            # Clock did not advance (or went backwards); do not refill and do not
            # update last_refill, so we never award negative time.
            return
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        self.last_refill = now

    def consume(self, now: float) -> Tuple[bool, int]:
        """Try to consume one token. Returns (emitted, suppressed_before_this)."""
        self._refill(now)
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            self.suppressed = 0
            return True, 0
        # Not enough tokens: suppress this record and bump the counter.
        self.suppressed += 1
        return False, self.suppressed


class MultiKeyLogRateLimiter:
    """Group records by key and enforce an independent rate limit per group.

    Args:
        key_fn: Callable extracting a hashable group key from a record. If it
            raises or returns a non-hashable value, the record is routed to a
            shared default bucket (see module docstring).
        rate: Tokens added per clock unit. Must be positive.
        capacity: Maximum tokens a bucket can hold (the burst size). Must be >= 1.
        clock: Zero-argument callable returning a float clock value. Defaults to
            time.monotonic. Injected for deterministic tests.

    Decisions are made per record via :meth:`decide`. The caller is responsible for
    actually emitting or suppressing the record based on the returned decision; this
    object never touches the logging system.
    """

    def __init__(
        self,
        key_fn: Callable[[Any], Hashable],
        rate: float,
        capacity: float,
        clock: Optional[Callable[[], float]] = None,
    ):
        if rate <= 0:
            raise ValueError(f"rate must be positive, got {rate!r}")
        if capacity < 1.0:
            # capacity below 1 means even the first record is suppressed, which is
            # not a useful limiter and almost certainly a misconfiguration.
            raise ValueError(f"capacity must be >= 1.0, got {capacity!r}")
        self._key_fn = key_fn
        self._rate = float(rate)
        self._capacity = float(capacity)
        # Imported lazily so the module remains import-safe in environments where
        # time is monkeypatched at import time; also keeps the dependency surface
        # explicit in __init__ rather than at module load.
        if clock is None:
            import time
            clock = time.monotonic
        self._clock = clock
        self._buckets: Dict[Hashable, _TokenBucket] = {}

    @staticmethod
    def _is_hashable(value: object) -> bool:
        try:
            hash(value)
        except TypeError:
            return False
        return True

    def _key_for(self, record: Any) -> Hashable:
        try:
            key = self._key_fn(record)
        except Exception:
            return _DEFAULT_KEY
        if not self._is_hashable(key):
            return _DEFAULT_KEY
        return key

    def _bucket_for(self, key: Hashable, now: float) -> _TokenBucket:
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _TokenBucket(self._capacity, self._rate, now)
            self._buckets[key] = bucket
        return bucket

    def decide(self, record: Any) -> RateLimitDecision:
        """Decide whether ``record`` should be emitted.

        Returns a :class:`RateLimitDecision` with ``emit`` True to emit or False to
        suppress. ``suppressed`` is the number of records suppressed for the same key
        since the last emit, excluding the current one; it is zero whenever ``emit``
        is True.
        """
        now = self._clock()
        key = self._key_for(record)
        bucket = self._bucket_for(key, now)
        emitted, suppressed = bucket.consume(now)
        return RateLimitDecision(emit=emitted, suppressed=suppressed, key=key)
