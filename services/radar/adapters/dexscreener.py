"""
DEXScreener adapter — Memecoin / DEX tokens.

No API key. Endpoints:
  GET /latest/dex/tokens/{address}

Watchlist identifier is 'chain:address'. Admins may also paste a full
dexscreener.com URL — the resolver below parses the chain + address out.
We pick the pair with the highest liquidity when an address has several.
"""
from __future__ import annotations

import re
import asyncio
from typing import Iterable, Optional

import httpx

from .base import AssetAdapter, common_snapshot


_BASE = 'https://api.dexscreener.com'
_TIMEOUT = 10.0
_HEADERS = {'User-Agent': 'AVbot-Radar/1.0', 'Accept': 'application/json'}

# Chains we accept on the watchlist add path. Match against DEXScreener's
# chainId values. EVM chains use 0x-prefixed 40-hex addresses; Solana uses
# base58 (~32-44 chars).
SUPPORTED_CHAINS = (
    'ethereum', 'solana', 'polygon', 'arbitrum', 'base', 'bsc', 'optimism',
)

_EVM_ADDR_RE  = re.compile(r'^0x[0-9a-fA-F]{40}$')
_SOL_ADDR_RE  = re.compile(r'^[1-9A-HJ-NP-Za-km-z]{32,44}$')

# Recognized dexscreener.com URL shapes:
#   https://dexscreener.com/<chain>/<pair-or-token-address>
_URL_RE = re.compile(
    r'^https?://(?:www\.)?dexscreener\.com/([a-zA-Z]+)/([0-9a-zA-Z]+)',
    re.IGNORECASE,
)


def parse_meme_input(s: str) -> Optional[tuple[str, str]]:
    """Accept either 'chain:address' or a dexscreener.com URL. Returns
    (chain, address) lowercased, or None on bad input."""
    if not s:
        return None
    raw = s.strip()
    if not raw:
        return None
    m = _URL_RE.match(raw)
    if m:
        chain = m.group(1).lower()
        addr  = m.group(2)
        # The URL second segment may be either a pair id or a token address.
        # DEXScreener accepts both in /latest/dex/tokens/, so we pass through.
        return chain, addr
    if ':' in raw:
        chain, _, addr = raw.partition(':')
        chain = chain.strip().lower()
        addr  = addr.strip()
        if chain and addr:
            return chain, addr
    return None


def address_looks_valid(chain: str, addr: str) -> bool:
    chain = (chain or '').lower()
    if chain in ('ethereum', 'polygon', 'arbitrum', 'base', 'bsc', 'optimism'):
        return bool(_EVM_ADDR_RE.match(addr or ''))
    if chain == 'solana':
        return bool(_SOL_ADDR_RE.match(addr or ''))
    return False


class DexscreenerAdapter(AssetAdapter):
    kind           = 'meme'
    api_limit_name = 'dexscreener'

    def __init__(self) -> None:
        self.disabled_reason: Optional[str] = None

    async def fetch_batch(self, identifiers: Iterable[str]) -> list[dict]:
        # No native batch endpoint — issue one call per identifier and let
        # the rate limiter pace them. Watchlists for meme tokens are
        # typically small per guild so this is fine.
        ids = [str(i).strip() for i in identifiers if str(i).strip()]
        out: list[dict] = []
        for ident in ids:
            snap = await self.fetch_one(ident)
            if snap:
                out.append(snap)
        return out

    async def fetch_one(self, identifier: str) -> Optional[dict]:
        parsed = parse_meme_input(identifier)
        if parsed is None:
            return None
        chain, address = parsed
        from ..rate_limiter import LIMITER
        if not await LIMITER.allow(self.api_limit_name):
            print(f'[radar/dexscreener] rate-limited address={address}')
            return None
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.get(
                    f'{_BASE}/latest/dex/tokens/{address}',
                    headers=_HEADERS,
                )
            if resp.status_code != 200:
                print(f'[radar/dexscreener] HTTP {resp.status_code} '
                      f'address={address}')
                return None
            data = resp.json() or {}
        except (httpx.HTTPError, ValueError, asyncio.TimeoutError) as e:
            print(f'[radar/dexscreener] error address={address}: '
                  f'{type(e).__name__}: {e}')
            return None
        pairs = data.get('pairs') or []
        if not isinstance(pairs, list) or not pairs:
            return None
        # Filter to the requested chain if the payload mixes chains.
        chain_pairs = [p for p in pairs
                       if isinstance(p, dict)
                       and str(p.get('chainId', '')).lower() == chain.lower()]
        if not chain_pairs:
            chain_pairs = [p for p in pairs if isinstance(p, dict)]
        # Highest-liquidity pair wins.
        def _liq(p):
            try:
                return float((p.get('liquidity') or {}).get('usd') or 0.0)
            except (TypeError, ValueError):
                return 0.0
        chain_pairs.sort(key=_liq, reverse=True)
        pair = chain_pairs[0]
        return _normalize_pair(chain, address, pair)


def _normalize_pair(chain: str, address: str, pair: dict) -> dict:
    base = pair.get('baseToken') or {}
    symbol = (base.get('symbol') or '').strip() or 'TOKEN'
    name   = (base.get('name')   or '').strip() or symbol
    try:
        price_usd = float(pair.get('priceUsd') or 0.0)
    except (TypeError, ValueError):
        price_usd = 0.0
    pc = pair.get('priceChange') or {}
    def _pct(v):
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None
    vol_24h = (pair.get('volume') or {}).get('h24')
    try:
        vol_24h = float(vol_24h) if vol_24h is not None else None
    except (TypeError, ValueError):
        vol_24h = None
    fdv = pair.get('fdv')
    try:
        fdv = float(fdv) if fdv is not None else None
    except (TypeError, ValueError):
        fdv = None
    image_url = (pair.get('info') or {}).get('imageUrl') or None
    page_url  = pair.get('url') or f'https://dexscreener.com/{chain}/{address}'

    return common_snapshot(
        identifier=     f'{chain.lower()}:{address}',
        kind=           'meme',
        symbol_display= symbol.upper(),
        price_usd=      price_usd,
        change_1h_pct=  _pct(pc.get('h1')),
        change_24h_pct= _pct(pc.get('h24')),
        volume_24h_usd= vol_24h,
        market_cap_usd= fdv,
        image_url=      image_url,
        page_url=       page_url,
        raw=            {'name': name, 'chain': chain, 'address': address,
                         'pair_address': pair.get('pairAddress')},
        price_display_symbol='$',
    )
