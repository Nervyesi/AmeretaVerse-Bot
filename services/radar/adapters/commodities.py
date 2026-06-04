"""
Commodities adapter — Gold / Silver / Oil / Platinum, surfaced inside the
Forex topic. Frankfurter only covers fiat, so commodities come from Yahoo
Finance's public v8 chart endpoint (no auth, no key).

Watchlist identifier is 'BASE/USD' where BASE is one of the commodity codes
below (e.g. 'XAU/USD', 'WTI/USD'). Snapshots use the same CommonAssetSnapshot
shape as Frankfurter so the digest / alert dispatcher need no changes; price is
quoted in USD with a '$' display symbol.

change_24h_pct is derived from the day's previousClose (Yahoo gives no rolling
1h change for these symbols).
"""
from __future__ import annotations

import asyncio
from typing import Iterable, Optional

import httpx

from .base import AssetAdapter, common_snapshot


_BASE = 'https://query1.finance.yahoo.com/v8/finance/chart'
_TIMEOUT = 10.0
# Yahoo rejects empty/blank User-Agents.
_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (compatible; AVbot/1.0)',
    'Accept':     'application/json',
}

# base code -> (yahoo symbol, friendly name, quote currency)
# Yahoo's v8 chart endpoint serves COMEX/NYMEX futures (=F); the spot FX-style
# metal symbols (XAUUSD=X) return "delisted" there, so we use the near-month
# futures, which track spot closely.
COMMODITY_SYMBOLS: dict[str, tuple[str, str, str]] = {
    'XAU':   ('GC=F', 'Gold',      'USD'),
    'XAG':   ('SI=F', 'Silver',    'USD'),
    'WTI':   ('CL=F', 'Oil WTI',   'USD'),
    'BRENT': ('BZ=F', 'Oil Brent', 'USD'),
    'XPT':   ('PL=F', 'Platinum',  'USD'),
}
COMMODITY_BASES = frozenset(COMMODITY_SYMBOLS.keys())


def is_commodity(identifier: str) -> bool:
    """True when the identifier's base is a known commodity code."""
    base = (identifier or '').split('/')[0].strip().upper()
    return base in COMMODITY_BASES


def build_display_name(identifier: str) -> str:
    """'XAU/USD' -> 'Gold (XAU/USD)'. Fiat pairs (and anything unknown) are
    returned unchanged."""
    base = (identifier or '').split('/')[0].strip().upper()
    spec = COMMODITY_SYMBOLS.get(base)
    if spec:
        return f'{spec[1]} ({identifier})'
    return identifier


def _flt(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


class CommoditiesAdapter(AssetAdapter):
    # Shares the 'forex' topic with Frankfurter; routing picks the adapter by
    # identifier base (see adapters.forex_adapter_for).
    kind           = 'forex'
    api_limit_name = 'yahoo'

    def __init__(self) -> None:
        self.disabled_reason: Optional[str] = None

    async def fetch_batch(self, identifiers: Iterable[str]) -> list[dict]:
        out: list[dict] = []
        seen: set[str] = set()
        for ident in identifiers:
            key = str(ident).strip().upper()
            if not key or key in seen:
                continue
            seen.add(key)
            snap = await self.fetch_one(key)
            if snap:
                out.append(snap)
        return out

    async def fetch_one(self, identifier: str) -> Optional[dict]:
        base, _, quote = (identifier or '').partition('/')
        base = base.strip().upper()
        quote = (quote.strip().upper() or 'USD')
        spec = COMMODITY_SYMBOLS.get(base)
        if not spec:
            return None
        yahoo_symbol, name, _qcur = spec

        from ..rate_limiter import LIMITER
        if not await LIMITER.allow(self.api_limit_name):
            print(f'[radar/commodities] rate-limited base={base}')
            return None
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.get(
                    f'{_BASE}/{yahoo_symbol}',
                    params={'range': '1d', 'interval': '1h'},
                    headers=_HEADERS,
                )
            if resp.status_code != 200:
                print(f'[radar/commodities] HTTP {resp.status_code} '
                      f'symbol={yahoo_symbol} body={resp.text[:200]!r}')
                return None
            data = resp.json() or {}
        except (httpx.HTTPError, ValueError, asyncio.TimeoutError) as e:
            print(f'[radar/commodities] error symbol={yahoo_symbol}: '
                  f'{type(e).__name__}: {e}')
            return None

        results = (((data.get('chart') or {}).get('result')) or [])
        if not results or not isinstance(results[0], dict):
            print(f'[radar/commodities] empty result for {yahoo_symbol}')
            return None
        meta = results[0].get('meta') or {}
        price = _flt(meta.get('regularMarketPrice'))
        prev = _flt(meta.get('previousClose'))
        if prev is None:
            prev = _flt(meta.get('chartPreviousClose'))
        if price is None:
            print(f'[radar/commodities] no price for {yahoo_symbol}')
            return None

        change_24h = None
        if prev is not None and prev != 0:
            change_24h = (price - prev) / prev * 100.0

        ident = f'{base}/{quote}'
        return common_snapshot(
            identifier=     ident,
            kind=           'forex',
            symbol_display= ident,
            price_usd=      price,
            change_1h_pct=  None,
            change_24h_pct= change_24h,
            volume_24h_usd= None,
            market_cap_usd= None,
            image_url=      None,
            page_url=       f'https://finance.yahoo.com/quote/{yahoo_symbol}',
            raw=            {'name': name, 'base': base, 'quote': quote,
                            'yahoo_symbol': yahoo_symbol, 'previous_close': prev},
            price_display_symbol='$',
            display_name=   build_display_name(ident),   # 'Gold (XAU/USD)'
        )

    async def search(self, query: str, limit: int = 10) -> list[dict]:
        q = (query or '').strip().upper()
        out: list[dict] = []
        for base, (_sym, name, _qcur) in COMMODITY_SYMBOLS.items():
            if not q or q in base or q in name.upper():
                out.append({'identifier': f'{base}/USD', 'symbol': base, 'name': name})
            if len(out) >= max(1, int(limit)):
                break
        return out
