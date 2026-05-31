"""
Per-API token-bucket rate limiter.

The fetcher consults the limiter BEFORE every adapter call. When a bucket
is exhausted the caller logs and serves stale cache rather than blocking
the loop. Buckets refill smoothly over time so bursts are tolerated.

Defaults are tuned to stay safely under each free tier:
  • coingecko: 25/min (CoinGecko free tier is ~30/min; we leave headroom)
  • reservoir, dexscreener, frankfurter, alpha_vantage: reserved entries
    for Phase 2; the buckets exist so Phase-2 code doesn't need to revisit
    this file.
"""
from __future__ import annotations

import time
import asyncio
from dataclasses import dataclass, field
from typing import Dict


@dataclass
class _Bucket:
    capacity:   int
    refill_per_sec: float
    tokens:     float
    last:       float = field(default_factory=time.monotonic)

    def take(self, n: int = 1) -> bool:
        now      = time.monotonic()
        elapsed  = max(0.0, now - self.last)
        self.last    = now
        self.tokens  = min(self.capacity, self.tokens + elapsed * self.refill_per_sec)
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False


class RateLimiter:
    """One bucket per logical API name. Constructed once and held as a
    module-level singleton."""

    def __init__(self) -> None:
        self._buckets: Dict[str, _Bucket] = {}
        self._lock = asyncio.Lock()

    def configure(self, api: str, *, per_minute: int) -> None:
        """Create or replace a bucket. Capacity == per_minute so a fresh
        bucket can burst its whole minute's allowance immediately, then
        refills at per_minute/60 tokens/sec."""
        pm = max(1, int(per_minute))
        self._buckets[api] = _Bucket(
            capacity=pm,
            refill_per_sec=pm / 60.0,
            tokens=float(pm),
        )

    async def allow(self, api: str, n: int = 1) -> bool:
        """Try to take n tokens from `api`'s bucket. Returns True on
        success, False when the bucket is empty (caller should NOT call
        the API and should serve stale data)."""
        async with self._lock:
            b = self._buckets.get(api)
            if b is None:
                # Unknown api → permissive. We log this in fetcher so it's visible.
                return True
            return b.take(n)

    def snapshot(self) -> dict:
        return {
            api: {'tokens': round(b.tokens, 2),
                  'capacity': b.capacity,
                  'refill_per_sec': round(b.refill_per_sec, 3)}
            for api, b in self._buckets.items()
        }


# Singleton + per-adapter defaults. Buckets are independent — exhausting
# one source's per-minute does not pause the others.
LIMITER = RateLimiter()
LIMITER.configure('coingecko',    per_minute=25)
LIMITER.configure('reservoir',    per_minute=30)
LIMITER.configure('dexscreener',  per_minute=60)
LIMITER.configure('frankfurter',  per_minute=30)
