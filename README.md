# py-multi-key-log-rate-limiter

Groups log records by a configurable key extraction function and enforces an independent per-group rate limit.

## Usage

```python
from py_multi_key_log_rate_limiter import MultiKeyLogRateLimiter

# Key on the logger name; 10 records per second per logger, burst of 20.
limiter = MultiKeyLogRateLimiter(
    key_fn=lambda record: record.name,
    rate=10.0,
    capacity=20.0,
)

def handle(record):
    decision = limiter.decide(record)
    if decision.emit:
        emit(record)
    elif decision.suppressed:
        # Optionally flush the suppressed count periodically; the limiter
        # itself never touches the logging system.
        pass
```

## Why it exists

A single global rate limit starves noisy sources and lets a quiet one be drowned out. Grouping by logger name, request id, or tenant lets each group burn its own budget. The trade-off is that the caller owns the side effects: this library returns a decision, it does not call handlers or hold a lock on the logging pipeline.

A token bucket is used rather than a fixed window. Fixed windows spike at boundaries, which is the exact pattern a log limiter should absorb. The bucket gives a smooth, bounded rate.

The clock is an injected callable (default `time.monotonic`) so tests are deterministic and so callers can share a clock across limiters.

## Awkward edges

If the key function raises or returns a non-hashable value, the record is routed to a shared default bucket rather than dropped. This means a broken extractor degrades to one noisy group instead of silently losing records or unbounded memory. If that is not what you want, fix the extractor before you wire the limiter.
