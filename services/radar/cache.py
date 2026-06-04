"""
Process-level TTL cache for asset snapshots, keyed by (asset_kind,
asset_identifier). The fetcher writes; slash commands and alert/digest
dispatchers read.

The cache also keeps a small *history* of the last N snapshots per
identifier so the alerts module can compute change_1h vs an actual ~1h-old
sample, even when an upstream adapter (CoinGecko) only returns a 24h delta
field. Phase 2 uses the same history slot for volume-spike baselines.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional


# TTL per kind. Forex moves slower than crypto; stocks rarely move and the
# Alpha Vantage free tier punishes frequent calls; NFT is cheap to recheck.
_TTL_DEFAULT = 300.0       # seconds; CoinGecko-friendly default
TTL_BY_KIND = {
    'crypto':  300.0,      # 5 min   — CoinGecko
    'nft':     600.0,      # 10 min, OpenSea
    'meme':    300.0,      # 5 min   — DEXScreener
    'forex':  1800.0,      # 30 min  — Frankfurter (daily-cadence rates)
    'stocks': 1800.0,      # 30 min  — Alpha Vantage (Phase 4 reservation)
}

# Per-identifier history length. ~24 samples × 5 min ≈ 2h, which is enough
# to compute change_1h with one extra sample of margin AND to estimate a
# rough volume baseline for spike detection.
_HISTORY_MAX = 24


@dataclass
class CachedSnapshot:
    """The fetcher writes one of these per (kind, identifier). `snapshot`
    is the CommonAssetSnapshot dict produced by the adapter; `ts` is the
    wall-clock seconds when it landed."""
    snapshot: dict
    ts:       float
    kind:     str


class RadarCache:
    """In-memory cache. Single instance per process — instantiated in
    fetcher.py and held as a module-level global. No locking needed because
    every read/write happens on the bot's asyncio loop thread."""

    def __init__(self) -> None:
        self._store:   dict[tuple[str, str], CachedSnapshot] = {}
        self._history: dict[tuple[str, str], deque]          = {}

    # ── Cache writes ────────────────────────────────────────────────────
    def put(self, kind: str, identifier: str, snapshot: dict) -> None:
        key = (kind, identifier)
        now = time.monotonic()
        self._store[key] = CachedSnapshot(snapshot=snapshot, ts=now, kind=kind)
        hist = self._history.setdefault(key, deque(maxlen=_HISTORY_MAX))
        hist.append((now, snapshot))

    # ── Reads ───────────────────────────────────────────────────────────
    def get(self, kind: str, identifier: str) -> Optional[CachedSnapshot]:
        return self._store.get((kind, identifier))

    def get_snapshot(self, kind: str, identifier: str) -> Optional[dict]:
        c = self.get(kind, identifier)
        return c.snapshot if c else None

    def is_fresh(self, kind: str, identifier: str) -> bool:
        c = self.get(kind, identifier)
        if c is None:
            return False
        ttl = TTL_BY_KIND.get(kind, _TTL_DEFAULT)
        return (time.monotonic() - c.ts) < ttl

    def all_for_kind(self, kind: str) -> list[CachedSnapshot]:
        return [c for (k, _), c in self._store.items() if k == kind]

    def history(self, kind: str, identifier: str) -> list[tuple[float, dict]]:
        """Return the per-identifier history list. Each element is
        (monotonic_ts, snapshot_dict)."""
        return list(self._history.get((kind, identifier), ()))

    def snapshot_about(
        self, kind: str, identifier: str, seconds_ago: float,
    ) -> Optional[dict]:
        """Return the snapshot closest to `seconds_ago` from now, or None if
        no history that old exists. Used by alerts.py to compute change_1h
        when the adapter doesn't provide it natively."""
        hist = self._history.get((kind, identifier))
        if not hist:
            return None
        now    = time.monotonic()
        target = now - max(0.0, seconds_ago)
        best   = None
        best_d = float('inf')
        for ts, snap in hist:
            d = abs(ts - target)
            if d < best_d:
                best, best_d = snap, d
        return best

    def stats(self) -> dict:
        return {
            'entries':       len(self._store),
            'history_keys':  len(self._history),
            'by_kind':       {
                k: sum(1 for (kk, _) in self._store if kk == k)
                for k in TTL_BY_KIND
            },
        }


# Module-level singleton. Other services import this directly.
CACHE = RadarCache()
