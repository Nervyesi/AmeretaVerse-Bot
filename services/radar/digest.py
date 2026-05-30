"""
Daily-digest scheduler.

Loops every 60s. For each guild with daily_enabled=1, computes the guild's
local time using timezone_offset (minutes from UTC) and posts the digest
ONCE per local day when local time crosses daily_time.

State is persisted in radar_settings.last_daily_sent_date ('YYYY-MM-DD' in
the guild's local timezone). That means a restart at 09:00 UTC won't
re-send a digest that already went out today.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional

import discord

from database import (
    get_radar_settings,
    list_guilds_with_radar,
    list_radar_watchlist,
    update_radar_settings,
)
from cogs._branding import build_branded_embed
from .cache import CACHE


def _local_now(offset_minutes: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=int(offset_minutes or 0))


def _parse_hhmm(s: str) -> Optional[tuple[int, int]]:
    try:
        h, m = (s or '').strip().split(':')
        hh, mm = int(h), int(m)
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            return None
        return hh, mm
    except (ValueError, AttributeError):
        return None


def _format_pct(v) -> str:
    if v is None:
        return '—'
    try:
        return f'{float(v):+.2f}%'
    except (TypeError, ValueError):
        return '—'


def _format_price(v) -> str:
    if v is None:
        return '—'
    try:
        n = float(v)
    except (TypeError, ValueError):
        return '—'
    if n >= 1000:
        return f'${n:,.2f}'
    if n >= 1:
        return f'${n:,.4f}'
    return f'${n:.6f}'


def _build_digest_embed(guild_id: int, settings: dict) -> Optional[discord.Embed]:
    """Compose the per-guild crypto digest from cache. Returns None if
    there's nothing useful to show (empty watchlist AND no top-10 cache)."""
    top_section: list[dict] = []
    for cs in CACHE.all_for_kind('crypto'):
        snap = cs.snapshot
        rank = snap.get('rank')
        if rank is None:
            continue
        top_section.append(snap)
    top_section.sort(key=lambda s: int(s.get('rank') or 9999))
    top_section = top_section[:10]

    watchlist_rows = list_radar_watchlist(guild_id, asset_kind='crypto')
    watch_section: list[tuple[dict, dict]] = []
    for row in watchlist_rows:
        ident = (row.get('asset_identifier') or '').lower()
        snap  = CACHE.get_snapshot('crypto', ident)
        if snap:
            watch_section.append((row, snap))

    if not top_section and not watch_section:
        return None

    tz_offset = int(settings.get('timezone_offset') or 0)
    local_now = _local_now(tz_offset)
    sign = '+' if tz_offset >= 0 else '-'
    hh   = abs(tz_offset) // 60
    mm   = abs(tz_offset) % 60
    tz_label = f'UTC{sign}{hh:02d}:{mm:02d}'

    title = f'📊 Daily Market — {local_now.strftime("%b %d")} ({tz_label})'

    e = build_branded_embed(
        int(guild_id),
        title=title,
        description='Crypto snapshot from your Radar configuration.',
        cog_prefix='',
        use_thumbnail=True,
        use_image=False,
        use_footer=True,
    )

    if top_section:
        lines: list[str] = []
        for snap in top_section:
            sym  = (snap.get('symbol_display') or snap.get('identifier') or '').upper()
            price = _format_price(snap.get('price_usd'))
            ch24  = _format_pct(snap.get('change_24h_pct'))
            lines.append(f'`{sym:<6}` {price:<13} {ch24}')
        e.add_field(
            name=f'Crypto Top {len(top_section)}',
            value='\n'.join(lines)[:1024],
            inline=False,
        )

    if watch_section:
        lines = []
        for row, snap in watch_section[:20]:
            sym  = (snap.get('symbol_display') or row.get('display_name')
                    or row.get('asset_identifier') or '').upper()
            price = _format_price(snap.get('price_usd'))
            ch24  = _format_pct(snap.get('change_24h_pct'))
            lines.append(f'`{sym:<8}` {price:<13} {ch24}')
        e.add_field(
            name='Your Watchlist',
            value='\n'.join(lines)[:1024],
            inline=False,
        )

    return e


async def _maybe_send_for(
    bot, guild_id: int, settings: dict,
) -> bool:
    if not bot.is_ready():
        return False
    if not int(settings.get('daily_enabled') or 0):
        return False
    ch_id = settings.get('daily_channel_crypto')
    if not ch_id:
        return False
    daily_time = settings.get('daily_time') or '08:00'
    hm = _parse_hhmm(daily_time)
    if hm is None:
        return False

    tz_offset = int(settings.get('timezone_offset') or 0)
    local_now = _local_now(tz_offset)
    local_today = local_now.strftime('%Y-%m-%d')

    if str(settings.get('last_daily_sent_date') or '') == local_today:
        return False  # already sent today (guild-local day)

    target_h, target_m = hm
    target_dt = local_now.replace(
        hour=target_h, minute=target_m, second=0, microsecond=0,
    )
    # Window: fire when local time is between target and target+30 min.
    # The loop tick is 60s, so we'll hit this within a minute of the target.
    delta = (local_now - target_dt).total_seconds()
    if not (0 <= delta < 30 * 60):
        return False

    embed = _build_digest_embed(guild_id, settings)
    if embed is None:
        print(f'[radar/digest] g={guild_id} skipping — no cache + empty watchlist')
        # Still mark as "sent" for today so we don't loop on this all day.
        update_radar_settings(guild_id, last_daily_sent_date=local_today)
        return False

    try:
        guild = bot.get_guild(int(guild_id))
        if guild is None:
            return False
        channel = guild.get_channel(int(ch_id))
        if channel is None:
            print(f'[radar/digest] g={guild_id} channel {ch_id} missing — skip')
            update_radar_settings(guild_id, last_daily_sent_date=local_today)
            return False
        await channel.send(embed=embed)
    except discord.Forbidden:
        print(f'[radar/digest] g={guild_id} forbidden in ch={ch_id}')
        update_radar_settings(guild_id, last_daily_sent_date=local_today)
        return False
    except Exception as e:  # noqa: BLE001
        print(f'[radar/digest] g={guild_id} send failed: {type(e).__name__}: {e}')
        return False

    update_radar_settings(guild_id, last_daily_sent_date=local_today)
    print(f'[radar/digest] g={guild_id} digest posted at {local_now.isoformat()}')
    return True


async def scheduler_loop(bot) -> None:
    """Long-running coroutine that checks every 60s if any guild is due."""
    print('[radar/digest] scheduler starting (60s tick)')
    while True:
        try:
            for gid in list_guilds_with_radar():
                try:
                    settings = get_radar_settings(gid)
                    await _maybe_send_for(bot, gid, settings)
                except Exception as e:  # noqa: BLE001
                    print(f'[radar/digest] g={gid} crashed: {type(e).__name__}: {e}')
        except Exception as e:  # noqa: BLE001
            print(f'[radar/digest] tick crashed: {type(e).__name__}: {e}')
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            print('[radar/digest] scheduler cancelled')
            raise
