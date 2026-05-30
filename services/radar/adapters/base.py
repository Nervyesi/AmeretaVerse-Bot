"""
Adapter contract: all data sources normalize their responses into
CommonAssetSnapshot dicts so the cache + alerts + digest layers don't have
to know about source-specific schemas.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional, Iterable


def common_snapshot(
    *,
    identifier:        str,
    kind:              str,
    symbol_display:    str,
    price_usd:         float,
    change_1h_pct:     float | None = None,
    change_24h_pct:    float | None = None,
    volume_24h_usd:    float | None = None,
    market_cap_usd:    float | None = None,
    rank:              int | None   = None,
    image_url:         str | None   = None,
    page_url:          str | None   = None,
    raw:               dict | None  = None,
) -> dict:
    """Constructor for the normalized snapshot. Adapter modules call this
    so we never accidentally drift on field names."""
    return {
        'identifier':     str(identifier),
        'kind':           str(kind),
        'symbol_display': str(symbol_display),
        'price_usd':      float(price_usd) if price_usd is not None else None,
        'change_1h_pct':  None if change_1h_pct  is None else float(change_1h_pct),
        'change_24h_pct': None if change_24h_pct is None else float(change_24h_pct),
        'volume_24h_usd': None if volume_24h_usd is None else float(volume_24h_usd),
        'market_cap_usd': None if market_cap_usd is None else float(market_cap_usd),
        'rank':           None if rank          is None else int(rank),
        'image_url':      image_url,
        'page_url':       page_url,
        'raw':            raw or {},
    }


# Type alias for documentation only — runtime is just a dict.
CommonAssetSnapshot = dict


class AssetAdapter(ABC):
    """Each adapter knows how to (a) batch-fetch multiple identifiers in one
    upstream call, (b) lookup a single identifier on demand, and (optionally)
    (c) resolve a user-typed search query into candidate identifiers."""

    kind: str = ''            # override in subclass: 'crypto' / 'nft' / etc.
    api_limit_name: str = ''  # rate_limiter bucket name

    @abstractmethod
    async def fetch_batch(self, identifiers: Iterable[str]) -> list[dict]:
        """Fetch every identifier in one upstream call where possible.
        Returns a list of CommonAssetSnapshot dicts. Identifiers that don't
        resolve are simply omitted (caller decides what 'missing' means)."""

    @abstractmethod
    async def fetch_one(self, identifier: str) -> Optional[dict]:
        """Single-identifier convenience. Adapters may implement this on top
        of fetch_batch."""

    async def search(self, query: str, limit: int = 10) -> list[dict]:
        """Optional search. Default returns no suggestions; CoinGecko
        overrides this for the dashboard autocomplete UI."""
        return []
