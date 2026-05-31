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
                'Set RESERVOIR_API_KEY in Railway env to enable NFT.'
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
        try:
            from ..rate_limiter import LIMITER
            if not await LIMITER.allow(self.api_limit_name):
                print('[radar/reservoir] rate-limited search')
                return []
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.get(
                    f'{_BASE}/search/collections/v2',
                    params={'name': q, 'limit': max(1, min(int(limit), 20))},
                    headers=self._headers(),
                )
            if resp.status_code != 200:
                print(f'[radar/reservoir] search HTTP {resp.status_code}')
                return []
            data = resp.json() or {}
        except (httpx.HTTPError, ValueError, asyncio.TimeoutError) as e:
            print(f'[radar/reservoir] search error: {type(e).__name__}: {e}')
            return []
        out: list[dict] = []
        for c in (data.get('collections') or [])[:limit]:
            if not isinstance(c, dict):
                continue
            slug = (c.get('slug') or c.get('collectionId') or '').strip().lower()
            if not slug:
                continue
            out.append({
                'identifier':       slug,
                'name':             c.get('name') or slug,
                'image':            c.get('image') or c.get('imageUrl') or '',
                'floor_price_usd':  (c.get('floorAskPrice', {}) or {}).get('amount', {}).get('usd'),
            })
        return out

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


def _normalize_collections(data: dict) -> list[dict]:
    cols = data.get('collections') if isinstance(data, dict) else None
    if not isinstance(cols, list):
        return []
    out: list[dict] = []
    for c in cols:
        if not isinstance(c, dict):
            continue
        slug = (c.get('slug') or c.get('id') or c.get('collectionId') or '').strip().lower()
        if not slug:
            continue
        name = c.get('name') or slug
        floor_usd = _safe_get(c, 'floorAsk', 'price', 'amount', 'usd')
        change_24h = _safe_get(c, 'floorSale', '1day')
        try:
            change_24h_pct = float(change_24h) if change_24h is not None else None
        except (TypeError, ValueError):
            change_24h_pct = None
        vol_24h = _safe_get(c, 'volume', '1day')
        try:
            vol_24h = float(vol_24h) if vol_24h is not None else None
        except (TypeError, ValueError):
            vol_24h = None
        image = c.get('image') or _safe_get(c, 'metadata', 'imageUrl') or ''
        out.append(common_snapshot(
            identifier=     slug,
            kind=           'nft',
            symbol_display= name,
            price_usd=      floor_usd or 0.0,
            change_24h_pct= change_24h_pct,
            volume_24h_usd= vol_24h,
            market_cap_usd= None,
            image_url=      image or None,
            page_url=       f'https://www.opensea.io/collection/{slug}',
            raw=            {'name': name},
            price_display_symbol='$',
        ))
    return out
