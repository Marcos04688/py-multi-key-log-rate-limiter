"""py-multi-key-log-rate-limiter: group log records by a configurable key and apply independent per-group rate limits."""

from .core import MultiKeyLogRateLimiter, RateLimitDecision, RateLimitResult

__all__ = ["MultiKeyLogRateLimiter", "RateLimitDecision", "RateLimitResult"]
__version__ = "1.0.0"
