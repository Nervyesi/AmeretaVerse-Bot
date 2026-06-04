"""
Memecoin Trending Discovery scanner.

A separate subsystem from the watchlist: it scans DEXScreener for tokens
that are pumping right now, applies the guild's quality filters, and
posts buy-signal alerts to the topic's discovery_channel. Per-token 12h
cooldown via radar_alerts_log prevents the same coin reappearing all
afternoon.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone, timedelta
from typing import Optional

import discord

from database import (
    get_radar_topic_settings,
    last_radar_alert_at,
    list_guilds_with_radar,
    record_radar_alert,
)
from cogs._branding import build_branded_embed
from .adapters import ADAPTERS_BY_KIND
from .adapters.dexscreener import SUPPORTED_CHAINS, trending_pairs


_INTERVAL_S = 300                       # 5 min tick
_PER_TOKEN_COOLDOWN_S = 12 * 3600       # 12h per (guild, token)
_ALERT_TYPE = 'discovery_meme'


def _parse_role_ids(raw) -> list[str]:
    if raw is None or raw == '':
        return []
    try:
        data = raw if isinstance(raw, list) else json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [str(v).strip() for v in data if str(v).strip()]


def _passes_filters(snap: dict, ts: dict) -> bool:
    """Per-guild threshold check. All thresholds default to the schema's
    conservative values; admins tune them in the dashboard."""
    raw = snap.get('raw') or {}
    liq = raw.get('liquidity_usd')
    vol = snap.get('volume_24h_usd')
    age = raw.get('pair_age_hours')
    ch1 = snap.get('change_1h_pct')
    buys = raw.get('buys_h1')
    sells = raw.get('sells_h1')

    try:
        min_liq = float(ts.get('discovery_min_liquidity_usd') or 0)
        min_vol = float(ts.get('discovery_min_volume_24h_usd') or 0)
        min_age = float(ts.get('discovery_min_age_hours') or 0)
        min_ch1 = float(ts.get('discovery_min_change_1h_pct') or 0)
    except (TypeError, ValueError):
        return False

    if liq is None or liq < min_liq:                     return False
    if vol is None or vol < min_vol:                     return False
    if age is None or age < min_age:                     return False
    if ch1 is None or ch1 < min_ch1 or ch1 <= 0:         return False  # buy signal only
    # Buy pressure: more buys than 1.5x sells in the last hour. If either
    # value is missing we fall through (DEXScreener occasionally omits txns
    # on lightly-traded pairs); admins can always raise other thresholds.
    if buys is not None and sells is not None and buys < sells * 1.5:
        return False
    return True


def _build_embed(guild_id: int, snap: dict) -> discord.Embed:
    raw = snap.get('raw') or {}
    sym  = snap.get('symbol_display') or 'TOKEN'
    name = raw.get('name') or sym
    chain = raw.get('chain') or '?'
    title = f'🚀 Memecoin Pumping — ${sym}'

    desc: list[str] = []
    price = snap.get('price_usd')
    if price is not None:
        desc.append(f'**Price:** ${price:.6f}' if price < 1 else f'**Price:** ${price:,.4f}')
    ch1 = snap.get('change_1h_pct')
    if ch1 is not None:
        desc.append(f'**1h change:** {ch1:+.1f}%')
    ch6 = raw.get('change_6h_pct')
    if ch6 is not None:
        desc.append(f'**6h change:** {ch6:+.1f}%')
    if raw.get('liquidity_usd'):
        desc.append(f'**Liquidity:** ${raw["liquidity_usd"]:,.0f}')
    vol_24h = snap.get('volume_24h_usd')
    if vol_24h:
        desc.append(f'**24h volume:** ${vol_24h:,.0f}')
    age = raw.get('pair_age_hours')
    if age is not None:
        if age >= 24:
            desc.append(f'**Pair age:** {age/24:.1f} days')
        else:
            desc.append(f'**Pair age:** {age:.1f} h')
    desc.append(f'**Chain:** {chain.upper()}')
    if raw.get('dex_id'):
        desc.append(f'**DEX:** {raw["dex_id"]}')
    if snap.get('page_url'):
        desc.append(f'\n[Open on DEXScreener]({snap["page_url"]})')

    e = build_branded_embed(
        int(guild_id),
        title=title,
        description='\n'.join(desc),
        cog_prefix='',
        use_thumbnail=False,
        use_image=False,
        use_footer=True,
    )
    img = snap.get('image_url')
    if img:
        e.set_thumbnail(url=img)
    return e


async def _send_for_guild(bot, guild_id: int, ts: dict, candidates: list[dict]) -> int:
    """Send up to N discovery alerts to this guild's discovery_channel,
    one embed per candidate, respecting the 12h per-token cooldown."""
    ch_id = ts.get('discovery_channel')
    if not ch_id:
        return 0
    guild = bot.get_guild(int(guild_id))
    if guild is None:
        return 0
    try:
        channel = guild.get_channel(int(ch_id))
    except (TypeError, ValueError):
        channel = None
    if channel is None:
        print(f'[radar/discovery_meme] g={guild_id} channel {ch_id} missing — skip')
        return 0

    mention_role_ids = _parse_role_ids(ts.get('discovery_mention_role_ids'))
    content = (' '.join(f'<@&{rid}>' for rid in mention_role_ids[:25])
               if mention_role_ids else None)
    allowed = discord.AllowedMentions(roles=True, users=False, everyone=False)

    sent = 0
    passed = 0            # candidates that cleared the guild's quality filters
    cooldown_skipped = 0  # passed filters but still inside the 12h per-token window
    now_utc = datetime.now(timezone.utc)
    for snap in candidates:
        identifier = snap.get('identifier')
        if not identifier:
            continue
        if not _passes_filters(snap, ts):
            continue
        passed += 1
        # Per-token 12h cooldown.
        last = last_radar_alert_at(guild_id, identifier, _ALERT_TYPE)
        if last:
            try:
                last_dt = datetime.fromisoformat(str(last).replace('Z', '+00:00'))
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                if (now_utc - last_dt).total_seconds() < _PER_TOKEN_COOLDOWN_S:
                    cooldown_skipped += 1
                    continue
            except (TypeError, ValueError):
                pass

        embed = _build_embed(guild_id, snap)
        try:
            await channel.send(content=content, embed=embed,
                               allowed_mentions=allowed)
        except discord.Forbidden:
            print(f'[radar/discovery_meme] g={guild_id} forbidden ch={ch_id}')
            return sent
        except Exception as e:  # noqa: BLE001
            print(f'[radar/discovery_meme] g={guild_id} send failed: '
                  f'{type(e).__name__}: {e}')
            continue

        record_radar_alert(
            guild_id, 'meme', identifier, _ALERT_TYPE,
            {
                'price_usd':     snap.get('price_usd'),
                'change_1h_pct': snap.get('change_1h_pct'),
                'liquidity_usd': (snap.get('raw') or {}).get('liquidity_usd'),
                'volume_24h_usd': snap.get('volume_24h_usd'),
                'chain':         (snap.get('raw') or {}).get('chain'),
            },
        )
        sent += 1
        # Pace within a single tick to avoid bursting the channel.
        await asyncio.sleep(1.0)
    # Observability for the discovery smoke test: how the candidate funnel
    # narrowed for this guild on this tick. Read-only.
    print(f'[radar/discovery_meme] scan g={guild_id} candidates={len(candidates)} '
          f'passed_filters={passed} cooldown_skipped={cooldown_skipped} sent={sent}')
    return sent


async def discovery_meme_loop(bot) -> None:
    """5-minute background tick: pull trending pairs across every supported
    DEXScreener chain, evaluate per-guild filters, post alerts to each
    guild's discovery_channel. Every tick + guild is wrapped in try/except
    so one bad config never crashes the loop."""
    print(f'[radar/discovery_meme] loop starting (interval={_INTERVAL_S}s)')
    while True:
        try:
            # One round-trip per chain — keeps inside the DEXScreener
            # 60/min rate limit even when scanning 12 chains.
            candidates = await trending_pairs(SUPPORTED_CHAINS)
            print(f'[radar/discovery_meme] tick: candidates={len(candidates)}')

            if candidates and bot.is_ready():
                for gid in list_guilds_with_radar():
                    try:
                        ts = get_radar_topic_settings(gid, 'meme')
                    except Exception as e:  # noqa: BLE001
                        print(f'[radar/discovery_meme] g={gid} settings read failed: '
                              f'{type(e).__name__}: {e}')
                        continue
                    if not int(ts.get('discovery_enabled') or 0):
                        continue
                    try:
                        n = await _send_for_guild(bot, gid, ts, candidates)
                        if n:
                            print(f'[radar/discovery_meme] g={gid} sent={n}')
                    except Exception as e:  # noqa: BLE001
                        print(f'[radar/discovery_meme] g={gid} send loop crashed: '
                              f'{type(e).__name__}: {e}')
        except Exception as e:  # noqa: BLE001
            print(f'[radar/discovery_meme] tick crashed: {type(e).__name__}: {e}')

        try:
            await asyncio.sleep(_INTERVAL_S)
        except asyncio.CancelledError:
            print('[radar/discovery_meme] loop cancelled')
            raise
