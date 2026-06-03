"""
Reservoir adapter — NFT collections.

Env-gated. RESERVOIR_API_KEY must be set in Railway for this adapter to
become live. When missing, `ReservoirAdapter.disabled_reason` is set and
the adapter is registered as a stub — the fetcher / API skip it and the
dashboard surfaces the reason instead.

Endpoints:
  GET /collections/v7?id=<csv>     (batch lookup, ≤ 20 ids per call)
  GET /search/collections/v2?name= (autocomplete)
"""
from __future__ import annotations

import os
import asyncio
from typing import Iterable, Optional

import httpx

from .base import AssetAdapter, common_snapshot


_BASE = 'https://api.reservoir.tools'
_TIMEOUT = 10.0


class ReservoirAdapter(AssetAdapter):
    kind           = 'nft'
    api_limit_name = 'reservoir'

    def __init__(self) -> None:
        self.api_key = (os.getenv('RESERVOIR_API_KEY') or '').strip()
        self.disabled_reason: Optional[str] = None
        if not self.api_key:
            self.disabled_reason = (
                'Set RESERVOIR_API_KEY in Railway env to enable NFT topic.'
            )
            print('[radar/reservoir] disabled — no RESERVOIR_API_KEY')

    def _headers(self) -> dict:
        h = {'User-Agent': 'AVbot-Radar/1.0', 'Accept': 'application/json'}
        if self.api_key:
            h['x-api-key'] = self.api_key
        return h

    async def fetch_batch(self, identifiers: Iterable[str]) -> list[dict]:
        if self.disabled_reason:
            return []
        slugs = sorted({str(s).strip().lower() for s in identifiers
                        if str(s).strip()})
        if not slugs:
            return []
        out: list[dict] = []
        # Cap 20 ids per call per Reservoir docs.
        for i in range(0, len(slugs), 20):
            chunk = slugs[i:i + 20]
            data = await self._collections_v7(chunk)
            if data:
                out.extend(_normalize_collections(data))
        return out

    async def fetch_one(self, identifier: str) -> Optional[dict]:
        rows = await self.fetch_batch([identifier])
        return rows[0] if rows else None

    async def search(self, query: str, limit: int = 8) -> list[dict]:
        if self.disabled_reason:
            return []
        q = (query or '').strip()
        if not q:
            return []
        # Try the documented 'name' param first; some Reservoir API
        # versions accept 'q' instead, so fall back to that if the first
        # call returns zero results.
        for param_name in ('name', 'q'):
            results = await self._search_call(q, param_name, limit)
            if results:
                return results
        return []

    async def _search_call(
        self, q: str, param_name: str, limit: int,
    ) -> list[dict]:
        from ..rate_limiter import LIMITER
        if not await LIMITER.allow(self.api_limit_name):
            print(f'[radar/reservoir] rate-limited search q={q!r}')
            return []
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.get(
                    f'{_BASE}/search/collections/v2',
                    params={param_name: q, 'limit': max(1, min(int(limit), 20))},
                    headers=self._headers(),
                )
            status = resp.status_code
            if status != 200:
                print(f'[radar/reservoir] search HTTP {status} '
                      f'param={param_name} q={q!r} body={resp.text[:200]}')
                return []
            data = resp.json() or {}
        except (httpx.HTTPError, ValueError, asyncio.TimeoutError) as e:
            print(f'[radar/reservoir] search error param={param_name}: '
                  f'{type(e).__name__}: {e}')
            return []
        out: list[dict] = []
        for c in (data.get('collections') or [])[:limit]:
            if not isinstance(c, dict):
                continue
            # Reservoir returns both `collectionId` (canonical) and `slug`
            # on /search/collections/v2 entries. Prefer collectionId since
            # /collections/v7 keys off it; fall back to slug for back-compat.
            cid = (c.get('collectionId') or c.get('id') or c.get('slug') or '').strip()
            if not cid:
                continue
            floor = c.get('floorAskPrice') or {}
            floor_usd = None
            if isinstance(floor, dict):
                amt = floor.get('amount')
                if isinstance(amt, dict):
                    floor_usd = amt.get('usd')
                elif isinstance(amt, (int, float)):
                    floor_usd = amt
            out.append({
                'identifier':       cid,
                'name':             c.get('name') or cid,
                'image':            c.get('image') or c.get('imageUrl') or '',
                'floor_price_usd':  floor_usd,
                'slug':             c.get('slug') or '',
            })
        return out

    async def trending(self, limit: int = 20) -> list[dict]:
        """Top collections by 24h volume — input for the NFT Discovery
        scanner. Returns full normalized snapshots so we can filter
        without a second round-trip."""
        if self.disabled_reason:
            return []
        from ..rate_limiter import LIMITER
        if not await LIMITER.allow(self.api_limit_name):
            return []
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.get(
                    f'{_BASE}/collections/v7',
                    params={'sortBy': '1DayVolume',
                            'limit':  max(1, min(int(limit), 20))},
                    headers=self._headers(),
                )
            if resp.status_code != 200:
                print(f'[radar/reservoir] trending HTTP {resp.status_code}: '
                      f'{resp.text[:200]}')
                return []
            return _normalize_collections(resp.json() or {})
        except (httpx.HTTPError, ValueError, asyncio.TimeoutError) as e:
            print(f'[radar/reservoir] trending error: {type(e).__name__}: {e}')
            return []

    async def _collections_v7(self, ids: list[str]) -> Optional[dict]:
        from ..rate_limiter import LIMITER
        if not await LIMITER.allow(self.api_limit_name):
            print('[radar/reservoir] collections call skipped — rate limiter exhausted')
            return None
        # Reservoir accepts repeated `id=` params; httpx renders a list as that.
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.get(
                    f'{_BASE}/collections/v7',
                    params=[('id', x) for x in ids] + [('includeTopBid', 'true')],
                    headers=self._headers(),
                )
            if resp.status_code != 200:
                print(f'[radar/reservoir] collections HTTP {resp.status_code}: '
                      f'{resp.text[:200]}')
                return None
            return resp.json()
        except (httpx.HTTPError, ValueError, asyncio.TimeoutError) as e:
            print(f'[radar/reservoir] collections error: {type(e).__name__}: {e}')
            return None


def _safe_get(obj, *path, default=None):
    cur = obj
    for p in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(p)
    return cur if cur is not None else default


def _safe_float(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _safe_int(v) -> Optional[int]:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _normalize_collections(data: dict) -> list[dict]:
    cols = data.get('collections') if isinstance(data, dict) else None
    if not isinstance(cols, list):
        return []
    out: list[dict] = []
    for c in cols:
        if not isinstance(c, dict):
            continue
        # Prefer collectionId for the canonical identifier; fall back to
        # slug / id. The watchlist add path now passes collectionId in.
        ident = (c.get('id') or c.get('collectionId') or c.get('slug') or '').strip()
        if not ident:
            continue
        slug = (c.get('slug') or '').strip().lower() or ident.lower()
        name = c.get('name') or ident
        floor_usd = _safe_get(c, 'floorAsk', 'price', 'amount', 'usd')
        change_24h_pct = _safe_float(_safe_get(c, 'floorSale', '1day'))
        vol_24h        = _safe_float(_safe_get(c, 'volume', '1day'))
        # Discovery scanner needs the volume-change-24h pct and sales count
        # in the last 24h. Reservoir provides these under volumeChange.1day
        # and salesCount.1day on /collections/v7 with sortBy=1DayVolume.
        vol_change_24h_pct = _safe_float(_safe_get(c, 'volumeChange', '1day'))
        sales_24h          = _safe_int(_safe_get(c, 'salesCount', '1day'))
        image = c.get('image') or _safe_get(c, 'metadata', 'imageUrl') or ''
        out.append(common_snapshot(
            identifier=     ident,
            kind=           'nft',
            symbol_display= name,
            price_usd=      floor_usd or 0.0,
            change_24h_pct= change_24h_pct,
            volume_24h_usd= vol_24h,
            market_cap_usd= None,
            image_url=      image or None,
            page_url=       f'https://www.opensea.io/collection/{slug or ident}',
            raw=            {
                'name':                name,
                'slug':                slug,
                'volume_change_24h_pct': vol_change_24h_pct,
                'sales_count_24h':       sales_24h,
            },
            price_display_symbol='$',
        ))
    return out
