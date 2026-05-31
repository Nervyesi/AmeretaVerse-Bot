"""
Adapter registry. Phase 1 registers CoinGecko (crypto) only. Phase 2 adds
Reservoir / DEXScreener / Frankfurter / Alpha Vantage by importing and
appending to ADAPTERS_BY_KIND.
"""
from .base        import AssetAdapter, CommonAssetSnapshot
from .coingecko    import CoinGeckoAdapter
from .reservoir    import ReservoirAdapter
from .dexscreener  import DexscreenerAdapter
from .frankfurter  import FrankfurterAdapter

# One instance per process. Reservoir is env-gated — when
# RESERVOIR_API_KEY is missing it stays in the registry but its
# disabled_reason is set; the fetcher and API endpoints check that flag.
ADAPTERS_BY_KIND: dict[str, AssetAdapter] = {
    'crypto': CoinGeckoAdapter(),
    'nft':    ReservoirAdapter(),
    'meme':   DexscreenerAdapter(),
    'forex':  FrankfurterAdapter(),
}

# Stocks remains unregistered until Phase 4 — its dashboard card stays
# "Coming soon" and POST /watchlist rejects 'stocks' with a clear message.
SUPPORTED_KINDS         = ('crypto', 'nft', 'meme', 'forex')
SUPPORTED_KINDS_PHASE_1 = SUPPORTED_KINDS  # legacy alias; kept for callers
ALL_KINDS               = ('crypto', 'nft', 'meme', 'forex', 'stocks')


def adapter_for(kind: str) -> AssetAdapter | None:
    """Return the registered adapter, or None if the kind is unregistered.
    Callers should additionally check `.disabled_reason` for env-gated
    adapters (currently only Reservoir)."""
    return ADAPTERS_BY_KIND.get((kind or '').lower())


__all__ = (
    'AssetAdapter', 'CommonAssetSnapshot',
    'ADAPTERS_BY_KIND', 'SUPPORTED_KINDS', 'SUPPORTED_KINDS_PHASE_1',
    'ALL_KINDS', 'adapter_for',
)
