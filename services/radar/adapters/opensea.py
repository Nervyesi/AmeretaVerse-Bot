"""
OpenSea API v2 adapter — NFT collections (multi-chain).

Replaces the dead Reservoir Protocol adapter (Reservoir shut down Oct 2025).
OpenSea v2 is the canonical NFT data source.

Env-gated. OPENSEA_API_KEY must be set in Railway for this adapter to become
live. When missing, `OpenSeaAdapter.disabled_reason` is set and the adapter is
registered as a stub — the fetcher / discovery / API skip it and the dashboard
surfaces the reason instead.

Endpoints (https://api.opensea.io/api/v2):
  GET /collections?chain=<chain>&order_by=seven_day_volume&limit=<n>
       — list collections by recent volume (trending discovery)
  GET /collections/{slug}                — collection metadata (name, image, contracts)
  GET /collections/{slug}/stats          — floor price, volume, volume change, sales

Watchlist identifier is '<chain>:<slug>' (chain lowercased, slug lowercased),
e.g. 'ethereum:pudgypenguins', 'solana:mad_lads', 'base:onchainmonkey'. A bare
slug with no chain prefix defaults to ethereum.

NOTE: floor prices and volumes from OpenSea are denominated in each chain's
native token (ETH, SOL, MATIC, ...), NOT USD. The snapshot stores the native
value in price_usd (the generic "display price" slot, same as the Frankfurter
forex adapter) and sets price_display_symbol to the native glyph.

Snapshot contract is kept compatible with what the old Reservoir adapter
produced so the digest, alert dispatcher and NFT discovery scanner need no
changes downstream:
  change_24h_pct           -> 24h volume change %  (primary NFT movement signal)
  raw.volume_change_24h_pct-> same value (discovery filter reads this)
  raw.volume_change_7d_pct -> 7d volume change %
  raw.sales_count_24h      -> sales in the last 24h
  raw.volume_7d            -> rolling 7d volume (native), for the next 7d diff

OpenSea's /stats does NOT return a volume-change field — its intervals are only
{interval, volume, sales}. So we compute the change ourselves by diffing the
current rolling one_day / seven_day volume against the previously cached
snapshot for the same collection. The first observation has no baseline and
reports None; deltas appear from the second tick onward.
"""
from __future__ import annotations

import os
import asyncio
from typing import Iterable, Optional

import httpx

from .base import AssetAdapter, common_snapshot


_BASE = 'https://api.opensea.io/api/v2'
_TIMEOUT = 12.0


# Our canonical chain key -> OpenSea v2 chain id. They mostly match; Polygon is
# 'polygon' in v2 (it was 'matic' on the legacy API). Unsupported chains simply
# fail the upstream call and are skipped gracefully.
CHAIN_IDS: dict[str, str] = {
    'ethereum':  'ethereum',
    'polygon':   'polygon',
    'base':      'base',
    'arbitrum':  'arbitrum',
    'optimism':  'optimism',
    'solana':    'solana',
    'avalanche': 'avalanche',
    'zora':      'zora',
    'blast':     'blast',
    'sei':       'sei',
}
SUPPORTED_CHAINS = tuple(CHAIN_IDS.keys())
DEFAULT_CHAIN = 'ethereum'

# Native-token display glyph per chain. ETH-settled L2s show Ξ; others show the
# token ticker (the digest formatter renders multi-char symbols as "12.5 SOL").
CHAIN_SYMBOL: dict[str, str] = {
    'ethereum':  'Ξ',
    'base':      'Ξ',
    'arbitrum':  'Ξ',
    'optimism':  'Ξ',
    'zora':      'Ξ',
    'blast':     'Ξ',
    'polygon':   'POL',
    'solana':    '◎',
    'avalanche': 'AVAX',
    'sei':       'SEI',
}

def _flt(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _int(v) -> Optional[int]:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _pct_change(prev, cur) -> Optional[float]:
    """Percent change from prev to cur. None when there's no usable baseline
    (first observation, missing, or non-positive prior)."""
    p = _flt(prev)
    c = _flt(cur)
    if p is None or c is None or p <= 0:
        return None
    return (c - p) / p * 100.0


def parse_nft_input(s: str) -> Optional[tuple[str, str]]:
    """Accept '<chain>:<slug>', a bare slug (defaults to ethereum), or an
    opensea.io/collection/<slug> URL. Returns (chain, slug) lowercased, or
    None on empty/garbage input."""
    if not s:
        return None
    raw = s.strip()
    if not raw:
        return None
    low = raw.lower()
    # opensea.io/collection/<slug>  (optional /chain/ segment ignored — the
    # slug is globally unique on OpenSea)
    marker = 'opensea.io/'
    if marker in low:
        tail = low.split(marker, 1)[1]
        parts = [p for p in tail.split('/') if p]
        # .../collection/<slug>  or  .../assets/<chain>/<slug>
        if 'collection' in parts:
            i = parts.index('collection')
            if i + 1 < len(parts):
                return DEFAULT_CHAIN, parts[i + 1]
        if parts:
            return DEFAULT_CHAIN, parts[-1]
        return None
    if ':' in raw:
        chain, _, slug = raw.partition(':')
        chain = chain.strip().lower()
        slug = slug.strip().lower()
        if chain and slug:
            return chain, slug
        return None
    return DEFAULT_CHAIN, low


def chain_symbol(chain: str, floor_symbol: Optional[str] = None) -> str:
    """Display glyph for a chain's native token. Prefer OpenSea's reported
    floor_price_symbol when present (e.g. 'ETH' -> 'Ξ'), else the chain map."""
    fs = (floor_symbol or '').strip().upper()
    if fs == 'ETH':
        return 'Ξ'
    if fs == 'SOL':
        return '◎'
    if fs:
        return fs
    return CHAIN_SYMBOL.get((chain or '').lower(), '$')


class OpenSeaAdapter(AssetAdapter):
    kind           = 'nft'
    api_limit_name = 'opensea'

    def __init__(self) -> None:
        self.api_key = (os.getenv('OPENSEA_API_KEY') or '').strip()
        self.disabled_reason: Optional[str] = None
        if not self.api_key:
            self.disabled_reason = (
                'Set OPENSEA_API_KEY in Railway env to enable NFT topic.'
            )
            print('[radar/opensea] disabled — no OPENSEA_API_KEY')

    def _headers(self) -> dict:
        h = {'User-Agent': 'AVbot-Radar/1.0', 'Accept': 'application/json'}
        if self.api_key:
            h['X-API-KEY'] = self.api_key
        return h

    async def _get(self, path: str, params: Optional[dict] = None) -> Optional[dict]:
        from ..rate_limiter import LIMITER
        if not await LIMITER.allow(self.api_limit_name):
            print(f'[radar/opensea] rate-limited path={path}')
            return None
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.get(
                    f'{_BASE}{path}', params=params or {}, headers=self._headers(),
                )
            if resp.status_code != 200:
                print(f'[radar/opensea] HTTP {resp.status_code} path={path} '
                      f'body={resp.text[:200]!r}')
                return None
            return resp.json() or {}
        except (httpx.HTTPError, ValueError, asyncio.TimeoutError) as e:
            print(f'[radar/opensea] error path={path}: {type(e).__name__}: {e}')
            return None

    # ── AssetAdapter interface ──────────────────────────────────────────────

    async def fetch_batch(self, identifiers: Iterable[str]) -> list[dict]:
        if self.disabled_reason:
            return []
        seen: set[str] = set()
        out: list[dict] = []
        for ident in identifiers:
            key = str(ident).strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            snap = await self.fetch_one(key)
            if snap:
                out.append(snap)
        return out

    async def fetch_one(self, identifier: str) -> Optional[dict]:
        if self.disabled_reason:
            return None
        parsed = parse_nft_input(identifier)
        if parsed is None:
            return None
        chain, slug = parsed
        stats = await self._get(f'/collections/{slug}/stats')
        if stats is None:
            return None
        meta = await self._get(f'/collections/{slug}')
        prev_v24, prev_v7 = _prev_volumes(chain, slug)
        return _build_snapshot(chain, slug, meta or {}, stats, prev_v24, prev_v7)

    async def search(self, query: str, limit: int = 8) -> list[dict]:
        """OpenSea v2 has no fuzzy collection-name search. We treat the query
        as a slug (optionally '<chain>:<slug>') and resolve it directly,
        returning a single suggestion when it exists."""
        if self.disabled_reason:
            return []
        snap = await self.fetch_one(query)
        if not snap:
            return []
        raw = snap.get('raw') or {}
        return [{
            'identifier':  snap['identifier'],
            'name':        raw.get('name') or snap.get('symbol_display'),
            'chain':       raw.get('chain'),
            'image_url':   snap.get('image_url'),
            'floor_price': snap.get('price_usd'),
        }][:max(1, int(limit))]

    async def trending(
        self, chain: str = DEFAULT_CHAIN, period: str = 'one_day', limit: int = 20,
    ) -> list[dict]:
        """Top collections on a chain by recent volume, each as a full
        snapshot. Input for the NFT discovery scanner.

        OpenSea's /collections order_by has no one_day_volume option, so we
        rank by seven_day_volume (the most-liquid collections) and rely on the
        per-collection 24h stats for the actual surge filtering downstream."""
        if self.disabled_reason:
            return []
        os_chain = CHAIN_IDS.get((chain or '').lower(), (chain or '').lower())
        cap = max(1, min(int(limit), 30))
        data = await self._get('/collections', {
            'chain':    os_chain,
            'order_by': 'seven_day_volume',
            'limit':    min(100, cap * 2),
        })
        if not data:
            return []
        cols = data.get('collections') or []
        if not isinstance(cols, list):
            return []
        out: list[dict] = []
        for c in cols:
            if not isinstance(c, dict):
                continue
            slug = (c.get('collection') or c.get('slug') or '').strip().lower()
            if not slug:
                continue
            stats = await self._get(f'/collections/{slug}/stats')
            if stats is None:
                continue
            prev_v24, prev_v7 = _prev_volumes((chain or '').lower(), slug)
            snap = _build_snapshot((chain or '').lower(), slug, c, stats,
                                   prev_v24, prev_v7)
            if snap:
                out.append(snap)
            if len(out) >= cap:
                break
        return out


def _prev_volumes(chain: str, slug: str):
    """Look up the previously cached snapshot for this collection and return
    (prev_one_day_volume, prev_seven_day_volume) for the volume-change diff.
    get_snapshot returns the last stored value regardless of TTL, so a 10-min
    discovery cadence still finds the prior tick's sample. Returns (None, None)
    when there's no prior snapshot."""
    try:
        from ..cache import CACHE
        prev = CACHE.get_snapshot('nft', f'{(chain or "").lower()}:{(slug or "").lower()}')
    except Exception:  # noqa: BLE001
        prev = None
    if not prev:
        return None, None
    return prev.get('volume_24h_usd'), (prev.get('raw') or {}).get('volume_7d')


def _interval(stats: dict, name: str) -> dict:
    for it in (stats.get('intervals') or []):
        if isinstance(it, dict) and (it.get('interval') or '') == name:
            return it
    return {}


def _build_snapshot(
    chain: str, slug: str, meta: dict, stats: dict,
    prev_vol_24h=None, prev_vol_7d=None,
) -> Optional[dict]:
    """Normalize an OpenSea collection + stats pair into a CommonAssetSnapshot.
    Defensive against missing/renamed fields — never raises.

    OpenSea exposes no volume-change field, so the 24h/7d change percents are
    computed by diffing the current rolling volumes against `prev_vol_24h` /
    `prev_vol_7d` (the previously cached snapshot's volumes). They are None on
    the first observation."""
    chain = (chain or DEFAULT_CHAIN).lower()
    slug = (slug or '').lower()
    if not slug:
        return None

    total = stats.get('total') if isinstance(stats.get('total'), dict) else {}
    one_day = _interval(stats, 'one_day')
    seven_day = _interval(stats, 'seven_day')

    floor = _flt(total.get('floor_price'))
    floor_symbol = total.get('floor_price_symbol')
    vol_24h = _flt(one_day.get('volume'))
    vol_7d = _flt(seven_day.get('volume'))
    sales_24h = _int(one_day.get('sales'))
    vol_change_24h = _pct_change(prev_vol_24h, vol_24h)
    vol_change_7d = _pct_change(prev_vol_7d, vol_7d)

    name = (meta.get('name') or '').strip() or slug
    image = meta.get('image_url') or meta.get('image') or None

    return common_snapshot(
        identifier=     f'{chain}:{slug}',
        kind=           'nft',
        symbol_display= slug.upper(),
        price_usd=      floor if floor is not None else 0.0,
        change_24h_pct= vol_change_24h,   # 24h volume change % (NFT movement signal)
        volume_24h_usd= vol_24h,          # native-token rolling 24h volume (not USD)
        market_cap_usd= None,             # OpenSea /stats has no market cap
        image_url=      image,
        page_url=       f'https://opensea.io/collection/{slug}',
        raw=            {
            'name':                  name,
            'slug':                  slug,
            'chain':                 chain,
            'volume_change_24h_pct': vol_change_24h,
            'volume_change_7d_pct':  vol_change_7d,
            'sales_count_24h':       sales_24h,
            'volume_7d':             vol_7d,     # native 7d volume, for next diff
            'floor_symbol':          floor_symbol,
        },
        price_display_symbol=chain_symbol(chain, floor_symbol),
    )
