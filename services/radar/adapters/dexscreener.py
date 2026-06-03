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
    'avalanche', 'fantom', 'blast', 'linea', 'scroll',
)
# Every chain in SUPPORTED_CHAINS except 'solana' uses 0x-prefixed 40-hex
# addresses. Used by address_looks_valid + auto-detect-chain on bare inputs.
EVM_CHAINS = tuple(c for c in SUPPORTED_CHAINS if c != 'solana')

_EVM_ADDR_RE  = re.compile(r'^0x[0-9a-fA-F]{40}$')
_SOL_ADDR_RE  = re.compile(r'^[1-9A-HJ-NP-Za-km-z]{32,44}$')

# Recognized dexscreener.com URL shapes:
#   https://dexscreener.com/<chain>/<pair-or-token-address>
_URL_RE = re.compile(
    r'^https?://(?:www\.)?dexscreener\.com/([a-zA-Z]+)/([0-9a-zA-Z]+)',
    re.IGNORECASE,
)


def parse_meme_input(s: str) -> Optional[tuple[Optional[str], str]]:
    """Accept any of:
      • https://dexscreener.com/<chain>/<address> URL
      • 'chain:address'
      • a bare EVM or Solana address (chain will be auto-detected later)

    Returns (chain_or_None, address). When chain is None, the caller is
    expected to query DEXScreener and pick the highest-liquidity pair
    across all chains."""
    if not s:
        return None
    raw = s.strip()
    if not raw:
        return None
    m = _URL_RE.match(raw)
    if m:
        chain = m.group(1).lower()
        addr  = m.group(2)
        return chain, addr
    if ':' in raw:
        chain, _, addr = raw.partition(':')
        chain = chain.strip().lower()
        addr  = addr.strip()
        if chain and addr:
            return chain, addr
    # Bare address — let the resolver auto-detect.
    if _EVM_ADDR_RE.match(raw):
        return None, raw
    if _SOL_ADDR_RE.match(raw):
        return 'solana', raw      # Solana is unambiguous from its alphabet
    return None


def address_looks_valid(chain: Optional[str], addr: str) -> bool:
    """Validate an address against the expected shape for a chain. When
    chain is None we only verify the address matches one of our known
    shapes (the resolver decides the chain)."""
    a = addr or ''
    if chain is None:
        return bool(_EVM_ADDR_RE.match(a) or _SOL_ADDR_RE.match(a))
    chain = chain.lower()
    if chain in EVM_CHAINS:
        return bool(_EVM_ADDR_RE.match(a))
    if chain == 'solana':
        return bool(_SOL_ADDR_RE.match(a))
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
            status = resp.status_code
            body_snip = resp.text[:200] if status != 200 else ''
            if status != 200:
                print(f'[radar/dexscreener] HTTP {status} '
                      f'chain={chain} address={address} body={body_snip!r}')
                return None
            data = resp.json() or {}
        except (httpx.HTTPError, ValueError, asyncio.TimeoutError) as e:
            print(f'[radar/dexscreener] error chain={chain} address={address}: '
                  f'{type(e).__name__}: {e}')
            return None
        pairs = data.get('pairs') or []
        if not isinstance(pairs, list) or not pairs:
            print(f'[radar/dexscreener] no pairs for chain={chain} address={address}')
            return None
        pairs = [p for p in pairs if isinstance(p, dict)]

        # If a chain hint was provided, filter; otherwise scan across every
        # chain DEXScreener returned for the address and pick the most
        # liquid pair globally. This is what the bare-address path needs.
        if chain:
            chain_pairs = [p for p in pairs
                           if str(p.get('chainId', '')).lower() == chain.lower()]
            if not chain_pairs:
                # Asked for a specific chain but the address only exists
                # elsewhere — surface that distinctly in logs but still
                # pick the best available pair so the resolve at least
                # returns something usable.
                print(f'[radar/dexscreener] chain={chain} requested but '
                      f'address only lists '
                      f'{sorted({p.get("chainId","?") for p in pairs})}')
                chain_pairs = pairs
        else:
            chain_pairs = pairs

        def _liq(p):
            try:
                return float((p.get('liquidity') or {}).get('usd') or 0.0)
            except (TypeError, ValueError):
                return 0.0
        chain_pairs.sort(key=_liq, reverse=True)
        pair = chain_pairs[0]
        # Use the pair's actual chainId as the canonical chain — this is
        # what's saved on the watchlist identifier.
        resolved_chain = str(pair.get('chainId', chain or '')).lower()
        return _normalize_pair(resolved_chain, address, pair)


def _normalize_pair(chain: str, address: str, pair: dict) -> dict:
    base = pair.get('baseToken') or {}
    symbol = (base.get('symbol') or '').strip() or 'TOKEN'
    name   = (base.get('name')   or '').strip() or symbol
    try:
        price_usd = float(pair.get('priceUsd') or 0.0)
    except (TypeError, ValueError):
        price_usd = 0.0

    def _pct(v):
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None
    def _flt(v):
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None
    def _int(v):
        try:
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    pc      = pair.get('priceChange') or {}
    volume  = pair.get('volume') or {}
    txns    = pair.get('txns') or {}
    liq     = pair.get('liquidity') or {}

    image_url = (pair.get('info') or {}).get('imageUrl') or None
    page_url  = pair.get('url') or f'https://dexscreener.com/{chain}/{address}'

    # Pair age in hours — DEXScreener reports pairCreatedAt as ms since epoch.
    import time as _time
    pair_created_ms = pair.get('pairCreatedAt')
    pair_age_hours = None
    if pair_created_ms is not None:
        try:
            pair_age_hours = max(0.0,
                (_time.time() * 1000 - float(pair_created_ms)) / 3_600_000.0)
        except (TypeError, ValueError):
            pair_age_hours = None

    h1_txns = txns.get('h1') if isinstance(txns, dict) else None
    buys_h1  = _int((h1_txns or {}).get('buys'))
    sells_h1 = _int((h1_txns or {}).get('sells'))

    return common_snapshot(
        identifier=     f'{chain.lower()}:{address}',
        kind=           'meme',
        symbol_display= symbol.upper(),
        price_usd=      price_usd,
        change_1h_pct=  _pct(pc.get('h1')),
        change_24h_pct= _pct(pc.get('h24')),
        volume_24h_usd= _flt(volume.get('h24')),
        market_cap_usd= _flt(pair.get('fdv')),
        image_url=      image_url,
        page_url=       page_url,
        raw=            {
            'name':            name,
            'chain':           chain,
            'address':         address,
            'pair_address':    pair.get('pairAddress'),
            'dex_id':          pair.get('dexId'),
            'liquidity_usd':   _flt(liq.get('usd')),
            'volume_h1_usd':   _flt(volume.get('h1')),
            'buys_h1':         buys_h1,
            'sells_h1':        sells_h1,
            'pair_age_hours':  pair_age_hours,
            'change_6h_pct':   _pct(pc.get('h6')),
        },
        price_display_symbol='$',
    )


async def trending_pairs(chains: tuple[str, ...]) -> list[dict]:
    """Fetch top pairs per chain for the Memecoin Discovery scanner.
    DEXScreener exposes /latest/dex/pairs/<chain> which returns pairs
    sorted by volume; we pick a handful from each chain so a slow chain
    can't starve the scan.

    Returns a flat list of normalized snapshots."""
    from ..rate_limiter import LIMITER
    out: list[dict] = []
    for chain in chains:
        if not await LIMITER.allow('dexscreener'):
            print(f'[radar/dexscreener] trending rate-limited at {chain}')
            break
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.get(
                    f'{_BASE}/latest/dex/search',
                    params={'q': chain}, headers=_HEADERS,
                )
            if resp.status_code != 200:
                print(f'[radar/dexscreener] trending {chain} HTTP {resp.status_code}')
                continue
            data = resp.json() or {}
        except (httpx.HTTPError, ValueError, asyncio.TimeoutError) as e:
            print(f'[radar/dexscreener] trending {chain} error: '
                  f'{type(e).__name__}: {e}')
            continue
        pairs = data.get('pairs') or []
        if not isinstance(pairs, list):
            continue
        # Keep only pairs on the requested chain and sort by 24h volume.
        chain_pairs = [
            p for p in pairs
            if isinstance(p, dict)
            and str(p.get('chainId', '')).lower() == chain.lower()
        ]

        def _vol(p):
            try:
                return float((p.get('volume') or {}).get('h24') or 0.0)
            except (TypeError, ValueError):
                return 0.0
        chain_pairs.sort(key=_vol, reverse=True)
        for p in chain_pairs[:30]:
            base_addr = (p.get('baseToken') or {}).get('address') or ''
            if not base_addr:
                continue
            out.append(_normalize_pair(chain, base_addr, p))
    return out
