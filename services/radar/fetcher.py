"""
Global fetcher loop.

Every RADAR_FETCH_INTERVAL_S seconds (default 300 = 5 min) the loop:
  1. Pulls a cross-guild UNION of identifiers per asset_kind.
  2. Asks the adapter to batch-fetch all of them in as few API calls as
     possible (CoinGecko: one call per 100 ids).
  3. Also fetches the top-100 leaderboard once (used by digests and by
     /topgainers // /toplosers slash commands).
  4. Writes every snapshot to the cache.
  5. Chains the alert dispatcher so movement and volume-spike alerts fire
     on the freshest snapshot the second they cross thresholds.

Single failure in one kind (or one guild) never crashes the loop — every
adapter call is guarded by try/except + the rate-limiter check.
"""
from __future__ import annotations

import os
import asyncio
from typing import Iterable

from database import list_all_radar_watchlists
from .adapters import ADAPTERS_BY_KIND, SUPPORTED_KINDS
from .cache import CACHE


_INTERVAL = max(60, int(os.getenv('RADAR_FETCH_INTERVAL_S', '300') or 300))

# Top-N leaderboard size. Watchlist additions can reference any of these by
# their CoinGecko id; the leaderboard cache also powers /topgainers and
# /toplosers and the digest's "Top 10" section.
_LEADERBOARD_SIZE = max(10, int(os.getenv('RADAR_LEADERBOARD_SIZE', '100') or 100))


def _watchlist_union(kind: str) -> set[str]:
    """All identifiers requested by any guild for this kind. The watchlist
    table is the only thing the fetcher consults — guilds opt INTO data
    pull just by having a row."""
    rows = list_all_radar_watchlists(kind)
    return {(r.get('asset_identifier') or '').strip().lower()
            for r in rows
            if (r.get('asset_identifier') or '').strip()}


async def fetch_once() -> dict:
    """Single fetcher tick. Returns a small per-kind summary for logging.

    Caller wraps this in a loop OR triggers it on demand (the slash
    commands fall back to triggering a fetch when the cache is cold).
    One kind failing never stops the others — each block is independently
    guarded by try/except."""
    summary: dict = {}

    # ── Crypto: leaderboard + watchlist extras ──────────────────────────
    try:
        adapter = ADAPTERS_BY_KIND.get('crypto')
        if adapter is not None:
            try:
                top = await adapter.fetch_top(per_page=_LEADERBOARD_SIZE)
            except Exception as e:  # noqa: BLE001
                print(f'[radar/fetcher] crypto top failed: {type(e).__name__}: {e}')
                top = []
            for snap in top:
                CACHE.put('crypto', snap['identifier'], snap)
            wanted = _watchlist_union('crypto')
            already = {snap['identifier'] for snap in top}
            extras = sorted(wanted - already)
            if extras:
                try:
                    rows = await adapter.fetch_batch(extras)
                except Exception as e:  # noqa: BLE001
                    print(f'[radar/fetcher] crypto extras failed: {type(e).__name__}: {e}')
                    rows = []
                for snap in rows:
                    CACHE.put('crypto', snap['identifier'], snap)
                summary['crypto_extras'] = len(rows)
            summary['crypto_top'] = len(top)
            summary['crypto_wanted'] = len(wanted)
    except Exception as e:  # noqa: BLE001
        print(f'[radar/fetcher] crypto block crashed: {type(e).__name__}: {e}')

    # ── NFT, Memecoin, Forex: one batched fetch per registered adapter ──
    for kind in ('nft', 'meme', 'forex'):
        try:
            adapter = ADAPTERS_BY_KIND.get(kind)
            if adapter is None:
                continue
            if getattr(adapter, 'disabled_reason', None):
                # e.g. OpenSea without OPENSEA_API_KEY
                continue
            wanted = _watchlist_union(kind)
            if not wanted:
                summary[f'{kind}_wanted'] = 0
                continue
            try:
                rows = await adapter.fetch_batch(sorted(wanted))
            except Exception as e:  # noqa: BLE001
                print(f'[radar/fetcher] {kind} fetch failed: {type(e).__name__}: {e}')
                rows = []
            for snap in rows:
                CACHE.put(kind, snap['identifier'], snap)
            summary[f'{kind}_wanted'] = len(wanted)
            summary[f'{kind}_got']    = len(rows)
        except Exception as e:  # noqa: BLE001
            print(f'[radar/fetcher] {kind} block crashed: {type(e).__name__}: {e}')

    return summary


async def fetch_loop(bot) -> None:
    """Long-running coroutine that ticks every _INTERVAL seconds. Chains
    the alert dispatcher so a fresh fetch immediately drives alerts."""
    from .alerts import dispatch_alerts
    print(f'[radar/fetcher] loop starting (interval={_INTERVAL}s, leaderboard={_LEADERBOARD_SIZE})')
    while True:
        try:
            summary = await fetch_once()
            print(f'[radar/fetcher] tick summary={summary} cache={CACHE.stats()}')
        except Exception as e:  # noqa: BLE001
            print(f'[radar/fetcher] tick crashed: {type(e).__name__}: {e}')

        try:
            await dispatch_alerts(bot)
        except Exception as e:  # noqa: BLE001
            print(f'[radar/fetcher] alerts dispatch crashed: {type(e).__name__}: {e}')

        try:
            await asyncio.sleep(_INTERVAL)
        except asyncio.CancelledError:
            print('[radar/fetcher] loop cancelled')
            raise
