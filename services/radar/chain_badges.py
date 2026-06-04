"""
Chain badge helpers for memecoin display.

Memecoin watchlist identifiers are stored as 'chain:address', so the chain is
always recoverable from the identifier itself — no extra column needed. These
helpers render a Discord-native emoji + short tag for each supported chain and
are shared by the /watchlist command, the daily digest, and the alert embeds.
"""
from __future__ import annotations

from typing import Optional

# chain (lowercased) -> (emoji, short label). Emojis are standard unicode
# squares/circles so they render everywhere without custom guild emoji.
CHAIN_BADGES: dict[str, tuple[str, str]] = {
    'solana':    ('🟣', 'SOL'),
    'ethereum':  ('⚪', 'ETH'),
    'base':      ('🔵', 'BASE'),
    'arbitrum':  ('🟦', 'ARB'),
    'polygon':   ('🟪', 'POLY'),
    'bsc':       ('🟡', 'BSC'),
    'optimism':  ('🔴', 'OP'),
    'avalanche': ('🟥', 'AVAX'),
    'fantom':    ('🔷', 'FTM'),
    'blast':     ('🟨', 'BLAST'),
    'linea':     ('⬛', 'LINEA'),
    'scroll':    ('🟫', 'SCROLL'),
}


def chain_badge(chain: Optional[str]) -> str:
    """'solana' -> '🟣 `SOL`'. Unknown chains fall back to a black circle and
    the uppercased chain name (or '?' when empty)."""
    c = (chain or '').lower()
    emoji, label = CHAIN_BADGES.get(c, ('⚫', c.upper() or '?'))
    return f'{emoji} `{label}`'


def chain_from_identifier(identifier: Optional[str]) -> str:
    """Memecoin identifiers are 'chain:address'. Return the chain prefix
    (lowercased), or '' when the identifier has no chain segment."""
    if not identifier or ':' not in identifier:
        return ''
    return identifier.split(':', 1)[0].strip().lower()
