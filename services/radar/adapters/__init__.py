"""
Adapter registry. CoinGecko (crypto), OpenSea (nft), DEXScreener (meme) and
Frankfurter (forex) are registered here, one instance per process.
"""
from .base        import AssetAdapter, CommonAssetSnapshot
from .coingecko    import CoinGeckoAdapter
from .opensea      import OpenSeaAdapter
from .dexscreener  import DexscreenerAdapter
from .frankfurter  import FrankfurterAdapter
from .commodities  import CommoditiesAdapter, is_commodity

# One instance per process. OpenSea is env-gated — when OPENSEA_API_KEY is
# missing it stays in the registry but its disabled_reason is set; the fetcher,
# discovery scanner and API endpoints check that flag.
ADAPTERS_BY_KIND: dict[str, AssetAdapter] = {
    'crypto': CoinGeckoAdapter(),
    'nft':    OpenSeaAdapter(),
    'meme':   DexscreenerAdapter(),
    'forex':  FrankfurterAdapter(),
}

# Commodities (Gold/Silver/Oil/Platinum) live inside the 'forex' topic but come
# from a different source (Yahoo) than fiat (Frankfurter). One instance, picked
# per-identifier by forex_adapter_for.
COMMODITIES_ADAPTER = CommoditiesAdapter()


def forex_adapter_for(identifier: str) -> AssetAdapter:
    """Route a forex-topic identifier to the right source: commodity bases
    (XAU/XAG/WTI/BRENT/XPT) go to Yahoo, everything else to Frankfurter."""
    if is_commodity(identifier):
        return COMMODITIES_ADAPTER
    return ADAPTERS_BY_KIND['forex']

# Stocks remains unregistered until Phase 4 — its dashboard card stays
# "Coming soon" and POST /watchlist rejects 'stocks' with a clear message.
SUPPORTED_KINDS         = ('crypto', 'nft', 'meme', 'forex')
SUPPORTED_KINDS_PHASE_1 = SUPPORTED_KINDS  # legacy alias; kept for callers
ALL_KINDS               = ('crypto', 'nft', 'meme', 'forex', 'stocks')


def adapter_for(kind: str) -> AssetAdapter | None:
    """Return the registered adapter, or None if the kind is unregistered.
    Callers should additionally check `.disabled_reason` for env-gated
    adapters (currently only OpenSea)."""
    return ADAPTERS_BY_KIND.get((kind or '').lower())


__all__ = (
    'AssetAdapter', 'CommonAssetSnapshot',
    'ADAPTERS_BY_KIND', 'SUPPORTED_KINDS', 'SUPPORTED_KINDS_PHASE_1',
    'ALL_KINDS', 'adapter_for',
    'COMMODITIES_ADAPTER', 'forex_adapter_for', 'is_commodity',
)
