"""
CoinGecko adapter.

Free tier — no API key required. We stay under the documented ~30/min
limit via services.radar.rate_limiter (bucket: 'coingecko', capacity 25/min).

Endpoints used:
  GET /coins/markets
    ?vs_currency=usd
    &ids=<csv of coingecko ids>       (batch fetch)
    &per_page=100&order=market_cap_desc (leaderboard)
    &price_change_percentage=1h,24h    (returns 1h + 24h change fields)
  GET /search?query=<q>                (autocomplete for the dashboard)

Each item from /coins/markets is normalized via common_snapshot. We DO
record `price_change_percentage_1h_in_currency` natively when CoinGecko
includes it; alerts.py also keeps its own historical buffer in case the
field is ever missing.
"""
from __future__ import annotations

import asyncio
from typing import Iterable, Optional

import httpx

from .base import AssetAdapter, common_snapshot


_BASE = 'https://api.coingecko.com/api/v3'
_HEADERS = {
    'User-Agent': 'AVbot-Radar/1.0',
    'Accept':     'application/json',
}
_TIMEOUT = 10.0


class CoinGeckoAdapter(AssetAdapter):
    kind            = 'crypto'
    api_limit_name  = 'coingecko'

    async def fetch_batch(self, identifiers: Iterable[str]) -> list[dict]:
        ids = sorted({str(i).strip().lower() for i in identifiers if str(i).strip()})
        if not ids:
            return []
        # CoinGecko's /coins/markets accepts up to ~250 ids per call. We
        # batch in chunks of 100 to stay polite and parallelize lightly.
        out: list[dict] = []
        for i in range(0, len(ids), 100):
            chunk = ids[i:i + 100]
            data = await _markets_request(ids_csv=','.join(chunk))
            if data:
                out.extend(_normalize_markets_rows(data))
        return out

    async def fetch_one(self, identifier: str) -> Optional[dict]:
        rows = await self.fetch_batch([identifier])
        return rows[0] if rows else None

    async def fetch_top(self, *, per_page: int = 100, page: int = 1) -> list[dict]:
        """Top-N market-cap leaderboard. Used by the digest and the
        /topgainers and /toplosers slash commands."""
        per_page = max(1, min(int(per_page), 250))
        data = await _markets_request(per_page=per_page, page=page)
        return _normalize_markets_rows(data or [])

    async def search(self, query: str, limit: int = 10) -> list[dict]:
        q = (query or '').strip()
        if not q:
            return []
        try:
            from ..rate_limiter import LIMITER
            if not await LIMITER.allow(self.api_limit_name):
                print(f'[radar/coingecko] rate-limited search q={q!r}')
                return []
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.get(
                    f'{_BASE}/search', params={'query': q}, headers=_HEADERS,
                )
            if resp.status_code != 200:
                print(f'[radar/coingecko] search HTTP {resp.status_code}')
                return []
            data = resp.json() or {}
        except (httpx.HTTPError, ValueError) as e:
            print(f'[radar/coingecko] search error: {type(e).__name__}: {e}')
            return []

        coins = data.get('coins') or []
        out: list[dict] = []
        for c in coins[:max(1, int(limit))]:
            if not isinstance(c, dict):
                continue
            cid = (c.get('id') or '').strip()
            if not cid:
                continue
            out.append({
                'identifier':   cid,
                'symbol':       (c.get('symbol') or '').upper(),
                'name':         c.get('name') or cid,
                'market_cap_rank': c.get('market_cap_rank'),
                'thumb':        c.get('thumb') or c.get('large') or '',
            })
        return out


# ── HTTP helpers ────────────────────────────────────────────────────────

async def _markets_request(
    *,
    ids_csv:  Optional[str] = None,
    per_page: int           = 100,
    page:     int           = 1,
) -> Optional[list]:
    """One call to /coins/markets. Returns the raw list of dicts or None on
    any failure (so callers can degrade gracefully)."""
    from ..rate_limiter import LIMITER
    if not await LIMITER.allow('coingecko'):
        print('[radar/coingecko] markets call skipped — rate limiter exhausted')
        return None

    params: dict = {
        'vs_currency':              'usd',
        'order':                    'market_cap_desc',
        'per_page':                 max(1, min(int(per_page), 250)),
        'page':                     max(1, int(page)),
        'price_change_percentage':  '1h,24h',
        'sparkline':                'false',
    }
    if ids_csv:
        params['ids'] = ids_csv

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f'{_BASE}/coins/markets', params=params, headers=_HEADERS,
            )
        if resp.status_code == 429:
            print('[radar/coingecko] HTTP 429 (upstream rate-limited)')
            return None
        if resp.status_code != 200:
            print(f'[radar/coingecko] markets HTTP {resp.status_code}: {resp.text[:200]}')
            return None
        body = resp.json()
        if isinstance(body, list):
            return body
        print(f'[radar/coingecko] markets unexpected body type={type(body).__name__}')
        return None
    except (httpx.HTTPError, ValueError, asyncio.TimeoutError) as e:
        print(f'[radar/coingecko] markets error: {type(e).__name__}: {e}')
        return None


def _normalize_markets_rows(rows: list) -> list[dict]:
    out: list[dict] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        cid = (r.get('id') or '').strip().lower()
        if not cid:
            continue
        out.append(common_snapshot(
            identifier=     cid,
            kind=           'crypto',
            symbol_display= (r.get('symbol') or cid).upper(),
            price_usd=      r.get('current_price') or 0.0,
            change_1h_pct=  r.get('price_change_percentage_1h_in_currency'),
            change_24h_pct= r.get('price_change_percentage_24h_in_currency')
                            if r.get('price_change_percentage_24h_in_currency') is not None
                            else r.get('price_change_percentage_24h'),
            volume_24h_usd= r.get('total_volume'),
            market_cap_usd= r.get('market_cap'),
            rank=           r.get('market_cap_rank'),
            image_url=      r.get('image'),
            page_url=       f'https://www.coingecko.com/en/coins/{cid}',
            raw=            {
                'name':                          r.get('name'),
                'high_24h':                      r.get('high_24h'),
                'low_24h':                       r.get('low_24h'),
                'ath':                           r.get('ath'),
                'circulating_supply':            r.get('circulating_supply'),
            },
        ))
    return out
