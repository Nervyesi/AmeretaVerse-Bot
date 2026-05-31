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
import json
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


def _parse_role_id_list(raw) -> list[str]:
    """Module-internal helper to read the JSON-array role-id column. The
    canonical normalization (commas/spaces/newlines → JSON string) is done
    by api._normalize_role_id_list on write; here we only have to read a
    well-formed JSON array."""
    if raw is None or raw == '':
        return []
    try:
        data = raw if isinstance(raw, list) else json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [str(v).strip() for v in data if str(v).strip()]


def _mention_content(role_ids: list[str]) -> Optional[str]:
    """' '.join(<@&id>) — None when no roles. Caller pairs this with
    AllowedMentions(roles=True) so Discord actually delivers the pings."""
    if not role_ids:
        return None
    return ' '.join(f'<@&{rid}>' for rid in role_ids[:25])


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


class DigestSendError(Exception):
    """Raised when a digest cannot be posted. The message is admin-friendly
    so api.py can surface it directly. The string itself is also logged."""


async def post_digest_now(bot, guild_id: int, settings: dict) -> dict:
    """Build + send the crypto digest right now. Used by both the manual
    send-now endpoint AND by the scheduled scheduler_loop after its window
    check. Raises DigestSendError on any failure path — caller decides
    whether that consumes a quota.

    Returns {'channel_id': int, 'message_id': int, 'watchlist_count': int}
    on success.
    """
    if not bot.is_ready():
        raise DigestSendError('Bot is starting up; try again in a moment.')

    ch_id = settings.get('daily_channel_crypto')
    if not ch_id:
        raise DigestSendError('Configure a crypto digest channel first.')

    embed = _build_digest_embed(guild_id, settings)
    if embed is None:
        raise DigestSendError(
            'No data to post yet. Either the cache is cold (fetcher runs '
            'every 5 minutes) or the watchlist is empty and the leaderboard '
            'has not loaded.'
        )

    guild = bot.get_guild(int(guild_id))
    if guild is None:
        raise DigestSendError('Bot is not in this server.')

    try:
        channel = guild.get_channel(int(ch_id))
    except (TypeError, ValueError):
        channel = None
    if channel is None:
        raise DigestSendError(f'Digest channel {ch_id} not found.')

    mentions = _parse_role_id_list(settings.get('digest_mention_role_ids'))
    content  = _mention_content(mentions)

    try:
        msg = await channel.send(
            content=content,
            embed=embed,
            allowed_mentions=discord.AllowedMentions(
                roles=True, users=False, everyone=False,
            ),
        )
    except discord.Forbidden as e:
        raise DigestSendError('Bot lacks permission to post in that channel.') from e
    except Exception as e:  # noqa: BLE001
        raise DigestSendError(f'Discord error: {type(e).__name__}: {e}') from e

    rows = list_radar_watchlist(int(guild_id), asset_kind='crypto')
    return {
        'channel_id':      int(channel.id),
        'message_id':      int(msg.id),
        'watchlist_count': len(rows),
    }


async def _maybe_send_for(
    bot, guild_id: int, settings: dict,
) -> bool:
    """Scheduled-tick handler. Idempotent within a guild-local day via
    last_daily_sent_date. On any failure (forbidden / channel gone / no
    data) it still marks the day complete to avoid hammering for 30 min."""
    if not bot.is_ready():
        return False
    if not int(settings.get('daily_enabled') or 0):
        return False
    if not settings.get('daily_channel_crypto'):
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
    delta = (local_now - target_dt).total_seconds()
    if not (0 <= delta < 30 * 60):
        return False

    try:
        await post_digest_now(bot, guild_id, settings)
    except DigestSendError as e:
        print(f'[radar/digest] g={guild_id} scheduled send failed: {e}')
        # Mark date so we don't retry every 60s for the rest of the window.
        update_radar_settings(guild_id, last_daily_sent_date=local_today)
        return False
    except Exception as e:  # noqa: BLE001
        print(f'[radar/digest] g={guild_id} scheduled send crashed: '
              f'{type(e).__name__}: {e}')
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
