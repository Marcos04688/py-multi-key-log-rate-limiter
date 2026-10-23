"""Tests for py_multi_key_log_rate_limiter.core.

Every clock-dependent test uses an injected fake clock so assertions never depend on
wall-clock time. Counts are compared as ints; no float equality is asserted.
"""

import unittest

from py_multi_key_log_rate_limiter.core import (
    MultiKeyLogRateLimiter,
    RateLimitDecision,
)


class _FakeClock:
    """Deterministic, manually-advanced clock for token-bucket tests."""

    def __init__(self, start: float = 0.0):
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, dt: float) -> None:
        self._t += dt


class _Rec:
    """Minimal record used to exercise key extraction."""

    def __init__(self, name: str):
        self.name = name


class TestConstruction(unittest.TestCase):
    def test_rejects_non_positive_rate(self):
        with self.assertRaises(ValueError):
            MultiKeyLogRateLimiter(key_fn=lambda r: r.name, rate=0.0, capacity=5.0)
        with self.assertRaises(ValueError):
            MultiKeyLogRateLimiter(key_fn=lambda r: r.name, rate=-1.0, capacity=5.0)

    def test_rejects_capacity_below_one(self):
        with self.assertRaises(ValueError):
            MultiKeyLogRateLimiter(key_fn=lambda r: r.name, rate=1.0, capacity=0.5)
        with self.assertRaises(ValueError):
            MultiKeyLogRateLimiter(key_fn=lambda r: r.name, rate=1.0, capacity=0.0)

    def test_accepts_capacity_of_one(self):
        # capacity==1 means a bucket can hold one token, i.e. the very first record
        # is allowed and subsequent ones are suppressed until a refill occurs.
        limiter = MultiKeyLogRateLimiter(
            key_fn=lambda r: r.name, rate=1.0, capacity=1.0
        )
        self.assertIsInstance(limiter, MultiKeyLogRateLimiter)

    def test_accepts_integer_arguments(self):
        # Ints are coerced to floats; behaviour should be unchanged.
        limiter = MultiKeyLogRateLimiter(
            key_fn=lambda r: "k", rate=1, capacity=1
        )
        d = limiter.decide(_Rec("a"))
        self.assertTrue(d.emit)


class TestBasicBehaviour(unittest.TestCase):
    def test_first_record_emitted_with_zero_suppressed(self):
        clock = _FakeClock(0.0)
        limiter = MultiKeyLogRateLimiter(
            key_fn=lambda r: r.name, rate=1.0, capacity=5.0, clock=clock
        )
        d = limiter.decide(_Rec("a"))
        self.assertTrue(d.emit)
        self.assertEqual(d.suppressed, 0)
        self.assertEqual(d.key, "a")

    def test_distinct_keys_have_independent_buckets(self):
        clock = _FakeClock(0.0)
        # capacity=2 lets each key emit two records immediately; a third is suppressed.
        limiter = MultiKeyLogRateLimiter(
            key_fn=lambda r: r.name, rate=1.0, capacity=2.0, clock=clock
        )
        a1 = limiter.decide(_Rec("a"))
        b1 = limiter.decide(_Rec("b"))
        a2 = limiter.decide(_Rec("a"))
        b2 = limiter.decide(_Rec("b"))
        a3 = limiter.decide(_Rec("a"))
        self.assertTrue(a1.emit and a2.emit and b1.emit and b2.emit)
        self.assertFalse(a3.emit)
        # Suppression on 'a' must not leak into 'b':
        self.assertEqual(a3.suppressed, 1)
        self.assertEqual(b1.suppressed, 0)

    def test_decision_type_is_rate_limit_decision(self):
        clock = _FakeClock(0.0)
        limiter = MultiKeyLogRateLimiter(
            key_fn=lambda r: r.name, rate=1.0, capacity=5.0, clock=clock
        )
        d = limiter.decide(_Rec("a"))
        self.assertIsInstance(d, RateLimitDecision)

    def test_suppressed_count_excludes_current_when_emitted(self):
        clock = _FakeClock(0.0)
        # capacity=2 -> two emits, then suppress until refill.
        limiter = MultiKeyLogRateLimiter(
            key_fn=lambda r: r.name, rate=1.0, capacity=2.0, clock=clock
        )
        self.assertTrue(limiter.decide(_Rec("a")).emit)
        self.assertTrue(limiter.decide(_Rec("a")).emit)
        s1 = limiter.decide(_Rec("a"))
        self.assertFalse(s1.emit)
        self.assertEqual(s1.suppressed, 1)
        s2 = limiter.decide(_Rec("a"))
        self.assertFalse(s2.emit)
        self.assertEqual(s2.suppressed, 2)
        # Refill one token; emit carries the suppressed count as zero.
        clock.advance(1.0)
        e = limiter.decide(_Rec("a"))
        self.assertTrue(e.emit)
        self.assertEqual(e.suppressed, 0)

    def test_refill_is_continuous_and_capped_at_capacity(self):
        clock = _FakeClock(0.0)
        # capacity=3, rate=1 token per unit time.
        limiter = MultiKeyLogRateLimiter(
            key_fn=lambda r: r.name, rate=1.0, capacity=3.0, clock=clock
        )
        # Drain the bucket.
        for _ in range(3):
            self.assertTrue(limiter.decide(_Rec("a")).emit)
        # Wait 10 units; that would add 10 tokens but cap is 3.
        clock.advance(10.0)
        # Only 3 more emits available before suppression resumes.
        self.assertTrue(limiter.decide(_Rec("a")).emit)
        self.assertTrue(limiter.decide(_Rec("a")).emit)
        self.assertTrue(limiter.decide(_Rec("a")).emit)
        self.assertFalse(limiter.decide(_Rec("a")).emit)


class TestKeyExtraction(unittest.TestCase):
    def test_key_fn_exception_routes_to_default_bucket(self):
        clock = _FakeClock(0.0)

        def flaky_key(record):
            if record.name == "boom":
                raise RuntimeError("extractor failed")
            return record.name

        limiter = MultiKeyLogRateLimiter(
            key_fn=flaky_key, rate=1.0, capacity=1.0, clock=clock
        )
        # A broken-extractor record and a normal record that maps to () must share
        # the default bucket, so the second is suppressed.
        d1 = limiter.decide(_Rec("boom"))
        d2 = limiter.decide(_Rec("boom"))
        self.assertTrue(d1.emit)
        self.assertFalse(d2.emit)
        self.assertEqual(d1.key, d2.key)

    def test_non_hashable_key_routes_to_default_bucket(self):
        clock = _FakeClock(0.0)

        # Returns a list (not hashable) for the 'listy' record.
        def key_fn(record):
            if record.name == "listy":
                return [record.name]
            return record.name

        limiter = MultiKeyLogRateLimiter(
            key_fn=key_fn, rate=1.0, capacity=1.0, clock=clock
        )
        d1 = limiter.decide(_Rec("listy"))
        d2 = limiter.decide(_Rec("listy"))
        self.assertTrue(d1.emit)
        self.assertFalse(d2.emit)
        self.assertEqual(d1.key, d2.key)

    def test_same_key_reuses_bucket(self):
        clock = _FakeClock(0.0)
        limiter = MultiKeyLogRateLimiter(
            key_fn=lambda r: r.name, rate=1.0, capacity=2.0, clock=clock
        )
        d1 = limiter.decide(_Rec("x"))
        d2 = limiter.decide(_Rec("x"))
        d3 = limiter.decide(_Rec("x"))
        self.assertEqual(d1.key, d2.key)
        self.assertEqual(d2.key, d3.key)
        self.assertTrue(d1.emit and d2.emit)
        self.assertFalse(d3.emit)


class TestClockAdvancement(unittest.TestCase):
    def test_clock_not_advancing_does_not_refill(self):
        clock = _FakeClock(5.0)
        limiter = MultiKeyLogRateLimiter(
            key_fn=lambda r: r.name, rate=1.0, capacity=1.0, clock=clock
        )
        # First record consumes the only token.
        self.assertTrue(limiter.decide(_Rec("a")).emit)
        # Without advancing the clock, the second is suppressed.
        self.assertFalse(limiter.decide(_Rec("a")).emit)

    def test_clock_going_backwards_does_not_refill(self):
        # A clock that reports an earlier time on the second call must not award
        # negative tokens; the bucket stays empty.
        clock = _FakeClock(5.0)
        limiter = MultiKeyLogRateLimiter(
            key_fn=lambda r: r.name, rate=1.0, capacity=1.0, clock=clock
        )
        self.assertTrue(limiter.decide(_Rec("a")).emit)
        # Simulate the clock jumping backwards.
        clock.advance(-3.0)
        self.assertFalse(limiter.decide(_Rec("a")).emit)


class TestDecisionEquality(unittest.TestCase):
    def test_equality_and_repr_are_well_formed(self):
        a = RateLimitDecision(emit=True, suppressed=0, key="x")
        b = RateLimitDecision(emit=True, suppressed=0, key="x")
        c = RateLimitDecision(emit=False, suppressed=3, key="x")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertEqual(hash(a), hash(b))
        r = repr(a)
        self.assertIn("RateLimitDecision", r)
        self.assertIn("emit=True", r)

    def test_suppressed_is_always_int(self):
        clock = _FakeClock(0.0)
        limiter = MultiKeyLogRateLimiter(
            key_fn=lambda r: r.name, rate=1.0, capacity=1.0, clock=clock
        )
        limiter.decide(_Rec("a"))
        d = limiter.decide(_Rec("a"))
        self.assertFalse(d.emit)
        self.assertIsInstance(d.suppressed, int)


if __name__ == "__main__":
    unittest.main()
