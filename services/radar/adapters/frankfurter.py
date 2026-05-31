"""
Frankfurter adapter — Forex pairs.

No API key. Free, daily-cadence (24/7). Endpoints:
  GET /latest?from=<BASE>&to=<QUOTE,QUOTE,...>
  GET /<YYYY-MM-DD>?from=<BASE>&to=<QUOTE,QUOTE,...>   (for yesterday)
  GET /currencies                                       (currency list)

Watchlist identifier is 'BASE/QUOTE' (both 3-letter ISO). We group entries
by base currency so one call covers every pair that shares a base.

change_1h_pct is always None for forex (daily cadence). alerts.py falls
back to change_24h_pct for that kind via _change_pct_for_alert.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

import httpx

from .base import AssetAdapter, common_snapshot


_BASE = 'https://api.frankfurter.app'
_TIMEOUT = 10.0
_HEADERS = {'User-Agent': 'AVbot-Radar/1.0', 'Accept': 'application/json'}


# Process-level currency list cache. Refreshed at most every 6 hours.
_currencies_cache: dict = {'ts': 0.0, 'data': {}}


def split_pair(identifier: str) -> Optional[tuple[str, str]]:
    """'EUR/USD' -> ('EUR', 'USD'). Returns None on bad input."""
    if not identifier or '/' not in identifier:
        return None
    base, _, quote = identifier.partition('/')
    base  = base.strip().upper()
    quote = quote.strip().upper()
    if len(base) != 3 or len(quote) != 3:
        return None
    if not base.isalpha() or not quote.isalpha():
        return None
    return base, quote


class FrankfurterAdapter(AssetAdapter):
    kind           = 'forex'
    api_limit_name = 'frankfurter'

    def __init__(self) -> None:
        self.disabled_reason: Optional[str] = None

    async def fetch_batch(self, identifiers: Iterable[str]) -> list[dict]:
        # Group requested pairs by base currency so we make one /latest call
        # plus one /<yesterday> call per base, instead of one pair at a time.
        by_base: dict[str, set[str]] = {}
        for ident in identifiers:
            parsed = split_pair(str(ident))
            if not parsed:
                continue
            base, quote = parsed
            if base == quote:
                # Same currency on both sides — fixed at 1.0.
                continue
            by_base.setdefault(base, set()).add(quote)
        if not by_base:
            return []
        yesterday = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
        out: list[dict] = []
        for base, quotes in by_base.items():
            quotes_csv = ','.join(sorted(quotes))
            today_data    = await self._rates(path='latest',  base=base, to=quotes_csv)
            yesterday_data = await self._rates(path=yesterday, base=base, to=quotes_csv)
            today_rates    = (today_data    or {}).get('rates') or {}
            yesterday_rates = (yesterday_data or {}).get('rates') or {}
            for quote in quotes:
                rate_now = today_rates.get(quote)
                rate_old = yesterday_rates.get(quote)
                if rate_now is None:
                    continue
                try:
                    rate_now = float(rate_now)
                except (TypeError, ValueError):
                    continue
                ch24 = None
                if rate_old is not None:
                    try:
                        rate_old = float(rate_old)
                        if rate_old > 0:
                            ch24 = ((rate_now - rate_old) / rate_old) * 100.0
                    except (TypeError, ValueError):
                        pass
                out.append(common_snapshot(
                    identifier=     f'{base}/{quote}',
                    kind=           'forex',
                    symbol_display= f'{base}/{quote}',
                    price_usd=      rate_now,    # NOTE: not USD strictly; quote currency.
                    change_1h_pct=  None,
                    change_24h_pct= ch24,
                    volume_24h_usd= None,
                    market_cap_usd= None,
                    image_url=      None,
                    page_url=       f'https://www.frankfurter.app/{base}/{quote}',
                    raw=            {'base': base, 'quote': quote, 'rate_now': rate_now,
                                     'rate_yesterday': rate_old},
                    price_display_symbol=quote,
                ))
        return out

    async def fetch_one(self, identifier: str) -> Optional[dict]:
        rows = await self.fetch_batch([identifier])
        return rows[0] if rows else None

    async def search(self, query: str, limit: int = 20) -> list[dict]:
        currencies = await self.currencies()
        q = (query or '').strip().upper()
        out: list[dict] = []
        for code, name in currencies.items():
            if not q or q in code or q in name.upper():
                out.append({'identifier': code, 'symbol': code, 'name': name})
            if len(out) >= max(1, int(limit)):
                break
        return out

    async def currencies(self) -> dict:
        """Cached /currencies fetch. Returns {code: name}."""
        import time
        now = time.monotonic()
        if (now - _currencies_cache['ts'] < 6 * 3600
                and _currencies_cache['data']):
            return _currencies_cache['data']
        from ..rate_limiter import LIMITER
        if not await LIMITER.allow(self.api_limit_name):
            return _currencies_cache['data'] or {}
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.get(
                    f'{_BASE}/currencies', headers=_HEADERS,
                )
            if resp.status_code != 200:
                return _currencies_cache['data'] or {}
            data = resp.json() or {}
            if isinstance(data, dict):
                _currencies_cache['data'] = {str(k).upper(): str(v)
                                             for k, v in data.items()}
                _currencies_cache['ts'] = now
        except (httpx.HTTPError, ValueError, asyncio.TimeoutError) as e:
            print(f'[radar/frankfurter] currencies error: {type(e).__name__}: {e}')
        return _currencies_cache['data'] or {}

    async def _rates(self, *, path: str, base: str, to: str) -> Optional[dict]:
        from ..rate_limiter import LIMITER
        if not await LIMITER.allow(self.api_limit_name):
            print(f'[radar/frankfurter] rate-limited path={path}')
            return None
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.get(
                    f'{_BASE}/{path}',
                    params={'from': base, 'to': to},
                    headers=_HEADERS,
                )
            if resp.status_code != 200:
                print(f'[radar/frankfurter] {path} HTTP {resp.status_code}')
                return None
            return resp.json()
        except (httpx.HTTPError, ValueError, asyncio.TimeoutError) as e:
            print(f'[radar/frankfurter] {path} error: {type(e).__name__}: {e}')
            return None
