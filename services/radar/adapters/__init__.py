"""
Adapter registry. Phase 1 registers CoinGecko (crypto) only. Phase 2 adds
Reservoir / DEXScreener / Frankfurter / Alpha Vantage by importing and
appending to ADAPTERS_BY_KIND.
"""
from .base       import AssetAdapter, CommonAssetSnapshot
from .coingecko  import CoinGeckoAdapter

# Single instance per process. The fetcher iterates ADAPTERS_BY_KIND to find
# the right adapter for a given asset_kind.
ADAPTERS_BY_KIND: dict[str, AssetAdapter] = {
    'crypto': CoinGeckoAdapter(),
}

SUPPORTED_KINDS_PHASE_1 = ('crypto',)
ALL_KINDS               = ('crypto', 'nft', 'meme', 'forex', 'stocks')

__all__ = (
    'AssetAdapter',
    'CommonAssetSnapshot',
    'ADAPTERS_BY_KIND',
    'SUPPORTED_KINDS_PHASE_1',
    'ALL_KINDS',
)
