"""
Per-topic daily-digest scheduler + manual send.

Each (guild, topic) is independent: its own daily_enabled / daily_channel /
daily_time / mention roles / digest style / manual-send quota / once-per-day
idempotency. Topics covered: 'crypto' | 'nft' | 'meme' | 'forex'. Phase-3
will add 'stocks' and 'liquidation'.

The scheduler iterates every (guild, topic) pair every 60s and fires
post_digest_now when local-time matches the topic's daily_time and the
once-per-day token (`last_daily_sent_date`) hasn't been set for the
guild-local date.

post_digest_now is also called directly from the API's send-now endpoint.
Both paths produce ONE embed scoped to a single topic — there is no longer
a cross-topic combined embed.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone, timedelta
from typing import Optional

import discord

from database import (
    get_radar_settings,             # guild-global (timezone, Phase-3 reservations)
    get_radar_topic_settings,
    list_guilds_with_radar,
    list_radar_watchlist,
    update_radar_topic_settings,
)
from cogs._branding import build_branded_embed
from .cache import CACHE


_TOPICS = ('crypto', 'nft', 'meme', 'forex')
_KIND_LABELS = {
    'crypto': 'Crypto',
    'nft':    'NFT',
    'meme':   'Memecoin',
    'forex':  'Forex',
}

# News-y defaults — used per topic when the override field is empty.
DEFAULT_DIGEST_TITLE  = "Today's Market Beat"
DEFAULT_DIGEST_INTRO  = "Here's how your tracked assets are moving today."
THUMBNAIL_MODES       = ('brand', 'first_coin', 'off')
DATE_MODES            = ('off', 'date_only', 'date_tz')


def _parse_role_id_list(raw) -> list[str]:
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


def _format_snap_price(snap: dict) -> str:
    p = snap.get('price_usd')
    if p is None:
        return '—'
    try:
        n = float(p)
    except (TypeError, ValueError):
        return '—'
    sym = snap.get('price_display_symbol') or '$'
    if len(sym) == 1 or sym in ('$', '€', '£', '¥'):
        if n >= 1000:  return f'{sym}{n:,.2f}'
        if n >= 1:     return f'{sym}{n:,.4f}'
        return f'{sym}{n:.6f}'
    if n >= 1000:  return f'{n:,.2f} {sym}'
    if n >= 1:     return f'{n:.4f} {sym}'
    return f'{n:.6f} {sym}'


def _parse_hex_color(s: str) -> Optional[int]:
    if not s:
        return None
    v = str(s).strip().lstrip('#')
    if len(v) != 6:
        return None
    try:
        return int(v, 16) & 0xFFFFFF
    except ValueError:
        return None


def _resolve_channel_id(value) -> Optional[int]:
    if value is None or value == '':
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _format_date_suffix(topic_settings: dict, global_settings: dict) -> str:
    mode = (topic_settings.get('digest_date_mode') or 'date_tz').strip().lower()
    if mode not in DATE_MODES:
        mode = 'date_tz'
    if mode == 'off':
        return ''
    tz_offset = int(global_settings.get('timezone_offset') or 0)
    local_now = _local_now(tz_offset)
    date_label = local_now.strftime('%b %d')
    if mode == 'date_only':
        return f' — {date_label}'
    sign = '+' if tz_offset >= 0 else '-'
    hh   = abs(tz_offset) // 60
    mm   = abs(tz_offset) % 60
    return f' — {date_label} (UTC{sign}{hh:02d}:{mm:02d})'


def _topic_watchlist_with_snaps(guild_id: int, topic: str) -> list[tuple[dict, dict]]:
    rows = list_radar_watchlist(int(guild_id), asset_kind=topic)
    out: list[tuple[dict, dict]] = []
    for r in rows:
        ident = (r.get('asset_identifier') or '')
        # NFT/crypto are lowercased, meme/forex are case-significant —
        # cache keys match the saved identifier exactly.
        snap = CACHE.get_snapshot(topic, ident if topic in ('meme', 'forex') else ident.lower())
        if snap:
            out.append((r, snap))
    return out


def _crypto_top10_fallback() -> list[dict]:
    rows: list[dict] = []
    for cs in CACHE.all_for_kind('crypto'):
        snap = cs.snapshot
        if snap.get('rank') is None:
            continue
        rows.append(snap)
    rows.sort(key=lambda s: int(s.get('rank') or 9999))
    return rows[:10]


def _build_topic_embed(
    guild_id: int, topic: str,
    topic_settings: dict, global_settings: dict,
) -> Optional[discord.Embed]:
    """Single-topic embed. Returns None when the topic has no data to show
    AND it isn't crypto (only crypto has a top-10 leaderboard fallback)."""
    if topic not in _TOPICS:
        return None

    watch_section = _topic_watchlist_with_snaps(int(guild_id), topic)
    fallback_top: list[dict] = []
    if not watch_section and topic == 'crypto':
        fallback_top = _crypto_top10_fallback()
    if not watch_section and not fallback_top:
        return None

    custom_title  = (topic_settings.get('digest_title')  or '').strip()
    custom_intro  = (topic_settings.get('digest_intro')  or '').strip()
    custom_color  = _parse_hex_color(topic_settings.get('digest_color') or '')
    custom_footer = (topic_settings.get('digest_footer') or '').strip()
    thumb_mode    = (topic_settings.get('digest_thumbnail_mode') or 'brand').strip().lower()
    if thumb_mode not in THUMBNAIL_MODES:
        thumb_mode = 'brand'

    date_suffix = _format_date_suffix(topic_settings, global_settings)
    topic_label = _KIND_LABELS.get(topic, topic.capitalize())
    base_title  = custom_title or f"{topic_label} — {DEFAULT_DIGEST_TITLE}"
    title       = f'📊 {base_title}{date_suffix}'
    intro       = custom_intro or DEFAULT_DIGEST_INTRO

    e = build_branded_embed(
        int(guild_id),
        title=title,
        description=intro,
        cog_prefix='',
        use_thumbnail=(thumb_mode == 'brand'),
        use_image=False,
        use_footer=not bool(custom_footer),
    )
    if custom_color is not None:
        e.color = discord.Color(custom_color)
    if thumb_mode == 'first_coin':
        first_img = ''
        if watch_section:
            first_img = (watch_section[0][1].get('image_url') or '').strip()
        elif fallback_top:
            first_img = (fallback_top[0].get('image_url') or '').strip()
        if first_img:
            e.set_thumbnail(url=first_img)
    if custom_footer:
        e.set_footer(text=custom_footer[:2048])

    # Single section body. Watchlist is the primary content; crypto top-10
    # only appears as a fallback when this topic's watchlist is empty.
    if watch_section:
        lines: list[str] = []
        for row, snap in watch_section[:25]:
            sym = (snap.get('symbol_display') or row.get('display_name')
                   or row.get('asset_identifier') or '').upper()
            price = _format_snap_price(snap)
            ch24  = _format_pct(snap.get('change_24h_pct'))
            lines.append(f'`{sym:<10}` {price:<16} {ch24}')
        e.add_field(
            name=f'Your {topic_label} watchlist',
            value='\n'.join(lines)[:1024],
            inline=False,
        )
    else:
        # Crypto-only fallback.
        lines = []
        for snap in fallback_top:
            sym  = (snap.get('symbol_display') or snap.get('identifier') or '').upper()
            price = _format_snap_price(snap)
            ch24  = _format_pct(snap.get('change_24h_pct'))
            lines.append(f'`{sym:<6}` {price:<16} {ch24}')
        e.add_field(
            name=f'Top {len(fallback_top)} by Market Cap',
            value='\n'.join(lines)[:1024],
            inline=False,
        )

    return e


class DigestSendError(Exception):
    """Raised when a topic-scoped digest cannot be posted."""


async def post_digest_now(bot, guild_id: int, topic: str) -> dict:
    """Build + send a single-topic digest immediately. Used by both the
    manual send-now endpoint and the scheduled scheduler_loop (per topic).
    Raises DigestSendError on any failure — quota consumption / scheduler
    bookkeeping is the caller's responsibility."""
    t = (topic or '').strip().lower()
    if t not in _TOPICS:
        raise DigestSendError(f'Unknown topic "{topic}".')

    if not bot.is_ready():
        raise DigestSendError('Bot is starting up; try again in a moment.')

    guild = bot.get_guild(int(guild_id))
    if guild is None:
        raise DigestSendError('Bot is not in this server.')

    topic_settings  = get_radar_topic_settings(int(guild_id), t)
    global_settings = get_radar_settings(int(guild_id))

    ch_id = _resolve_channel_id(topic_settings.get('daily_channel'))
    if ch_id is None:
        raise DigestSendError(
            f'Configure a daily channel for {_KIND_LABELS.get(t, t)} first.'
        )

    try:
        channel = guild.get_channel(int(ch_id))
    except (TypeError, ValueError):
        channel = None
    if channel is None:
        raise DigestSendError(f'Daily channel for {_KIND_LABELS.get(t, t)} not found.')

    embed = _build_topic_embed(int(guild_id), t, topic_settings, global_settings)
    if embed is None:
        raise DigestSendError(
            f'No {_KIND_LABELS.get(t, t)} data to post yet. Either the '
            'cache is cold (fetcher runs every 5 minutes) or the '
            f'{_KIND_LABELS.get(t, t)} watchlist is empty.'
        )

    mentions = _parse_role_id_list(topic_settings.get('digest_mention_role_ids'))
    content  = _mention_content(mentions)
    allowed  = discord.AllowedMentions(roles=True, users=False, everyone=False)

    try:
        msg = await channel.send(content=content, embed=embed, allowed_mentions=allowed)
    except discord.Forbidden as e:
        raise DigestSendError(
            f'Bot lacks permission to post in #{getattr(channel, "name", ch_id)}.'
        ) from e
    except Exception as e:  # noqa: BLE001
        raise DigestSendError(f'Discord error: {type(e).__name__}: {e}') from e

    rows = list_radar_watchlist(int(guild_id), asset_kind=t)
    return {
        'topic':           t,
        'channel_id':      int(channel.id),
        'message_id':      int(msg.id),
        'watchlist_count': len(rows),
    }


async def _maybe_send_for_topic(
    bot, guild_id: int, topic: str,
    topic_settings: dict, global_settings: dict,
) -> bool:
    """Scheduled-tick handler for one (guild, topic). Idempotent within a
    guild-local day via last_daily_sent_date on the topic row. On any
    failure we still mark the day complete to avoid hammering for 30 min."""
    if not bot.is_ready():
        return False
    if not int(topic_settings.get('daily_enabled') or 0):
        return False
    if not topic_settings.get('daily_channel'):
        return False

    daily_time = topic_settings.get('daily_time') or '08:00'
    hm = _parse_hhmm(daily_time)
    if hm is None:
        return False

    tz_offset = int(global_settings.get('timezone_offset') or 0)
    local_now = _local_now(tz_offset)
    local_today = local_now.strftime('%Y-%m-%d')

    if str(topic_settings.get('last_daily_sent_date') or '') == local_today:
        return False

    target_h, target_m = hm
    target_dt = local_now.replace(
        hour=target_h, minute=target_m, second=0, microsecond=0,
    )
    delta = (local_now - target_dt).total_seconds()
    if not (0 <= delta < 30 * 60):
        return False

    try:
        await post_digest_now(bot, int(guild_id), topic)
    except DigestSendError as e:
        print(f'[radar/digest] g={guild_id} {topic} scheduled failed: {e}')
        update_radar_topic_settings(int(guild_id), topic,
                                    last_daily_sent_date=local_today)
        return False
    except Exception as e:  # noqa: BLE001
        print(f'[radar/digest] g={guild_id} {topic} scheduled crashed: '
              f'{type(e).__name__}: {e}')
        return False

    update_radar_topic_settings(int(guild_id), topic,
                                last_daily_sent_date=local_today)
    print(f'[radar/digest] g={guild_id} {topic} posted at {local_now.isoformat()}')
    return True


async def scheduler_loop(bot) -> None:
    """Long-running coroutine that checks every 60s if any (guild, topic)
    is due."""
    print('[radar/digest] scheduler starting (60s tick, per-topic)')
    while True:
        try:
            for gid in list_guilds_with_radar():
                try:
                    global_settings = get_radar_settings(gid)
                except Exception as e:  # noqa: BLE001
                    print(f'[radar/digest] g={gid} global read failed: '
                          f'{type(e).__name__}: {e}')
                    continue
                for topic in _TOPICS:
                    try:
                        topic_settings = get_radar_topic_settings(gid, topic)
                        await _maybe_send_for_topic(
                            bot, gid, topic, topic_settings, global_settings,
                        )
                    except Exception as e:  # noqa: BLE001
                        print(f'[radar/digest] g={gid} {topic} crashed: '
                              f'{type(e).__name__}: {e}')
        except Exception as e:  # noqa: BLE001
            print(f'[radar/digest] tick crashed: {type(e).__name__}: {e}')
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            print('[radar/digest] scheduler cancelled')
            raise
