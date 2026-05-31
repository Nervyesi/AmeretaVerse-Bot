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


# News-y defaults — used when the per-guild override field is empty.
DEFAULT_DIGEST_TITLE  = "Today's Market Beat"
DEFAULT_DIGEST_INTRO  = "Here's how your tracked assets are moving today."
THUMBNAIL_MODES       = ('brand', 'first_coin', 'off')
DATE_MODES            = ('off', 'date_only', 'date_tz')

# Per-kind section headings in fixed display order.
_KIND_ORDER  = ('crypto', 'nft', 'meme', 'forex')
_KIND_LABELS = {
    'crypto': 'Crypto',
    'nft':    'NFT',
    'meme':   'Memecoin',
    'forex':  'Forex',
}


def _format_snap_price(snap: dict) -> str:
    """Format a snapshot price using its price_display_symbol (e.g. '$', 'USD',
    'EUR'). Forex pairs use the quote currency code as their symbol."""
    p = snap.get('price_usd')
    if p is None:
        return '—'
    try:
        n = float(p)
    except (TypeError, ValueError):
        return '—'
    sym = snap.get('price_display_symbol') or '$'
    # Currency-code symbols ('USD', 'EUR') render as a 3-letter suffix;
    # single-glyph symbols ('$', '€') stay as a prefix.
    if len(sym) == 1 or sym in ('$', '€', '£', '¥'):
        if n >= 1000:  return f'{sym}{n:,.2f}'
        if n >= 1:     return f'{sym}{n:,.4f}'
        return f'{sym}{n:.6f}'
    # 3-letter currency code (forex): "1.0934 USD"
    if n >= 1000:  return f'{n:,.2f} {sym}'
    if n >= 1:     return f'{n:.4f} {sym}'
    return f'{n:.6f} {sym}'


def _parse_hex_color(s: str) -> Optional[int]:
    """Accept '#RRGGBB' or 'RRGGBB' (case-insensitive); return int or None."""
    if not s:
        return None
    v = str(s).strip().lstrip('#')
    if len(v) != 6:
        return None
    try:
        return int(v, 16) & 0xFFFFFF
    except ValueError:
        return None


def _format_date_suffix(settings: dict) -> str:
    """Return the title's date/timezone suffix or '' depending on the
    per-guild digest_date_mode setting."""
    mode = (settings.get('digest_date_mode') or 'date_tz').strip().lower()
    if mode not in DATE_MODES:
        mode = 'date_tz'
    if mode == 'off':
        return ''
    tz_offset = int(settings.get('timezone_offset') or 0)
    local_now = _local_now(tz_offset)
    date_label = local_now.strftime('%b %d')
    if mode == 'date_only':
        return f' — {date_label}'
    sign = '+' if tz_offset >= 0 else '-'
    hh   = abs(tz_offset) // 60
    mm   = abs(tz_offset) % 60
    tz_label = f'UTC{sign}{hh:02d}:{mm:02d}'
    return f' — {date_label} ({tz_label})'


def _section_lines_watchlist(rows_snaps: list[tuple[dict, dict]]) -> list[str]:
    out: list[str] = []
    for row, snap in rows_snaps[:25]:
        sym = (snap.get('symbol_display') or row.get('display_name')
               or row.get('asset_identifier') or '').upper()
        price = _format_snap_price(snap)
        ch24  = _format_pct(snap.get('change_24h_pct'))
        out.append(f'`{sym:<10}` {price:<16} {ch24}')
    return out


def _section_lines_top(snaps: list[dict]) -> list[str]:
    out: list[str] = []
    for snap in snaps:
        sym  = (snap.get('symbol_display') or snap.get('identifier') or '').upper()
        price = _format_snap_price(snap)
        ch24  = _format_pct(snap.get('change_24h_pct'))
        out.append(f'`{sym:<6}` {price:<16} {ch24}')
    return out


def _watch_sections_by_kind(guild_id: int, kinds: tuple[str, ...]) -> dict:
    """For each requested kind, return [(row, snapshot), ...] in the order
    the user added them. Empty kinds are omitted from the result."""
    out: dict = {}
    rows_all = list_radar_watchlist(guild_id)
    for k in kinds:
        items: list[tuple[dict, dict]] = []
        for row in rows_all:
            if (row.get('asset_kind') or '').lower() != k:
                continue
            ident = (row.get('asset_identifier') or '').lower()
            snap  = CACHE.get_snapshot(k, ident)
            if snap:
                items.append((row, snap))
        if items:
            out[k] = items
    return out


def _crypto_top10_fallback() -> list[dict]:
    """Top-10 crypto by market cap, sorted from the leaderboard cache. Used
    only when the guild has NO watchlist entries in any kind."""
    rows: list[dict] = []
    for cs in CACHE.all_for_kind('crypto'):
        snap = cs.snapshot
        if snap.get('rank') is None:
            continue
        rows.append(snap)
    rows.sort(key=lambda s: int(s.get('rank') or 9999))
    return rows[:10]


def _build_digest_embed(
    guild_id: int, settings: dict,
    *,
    kinds: tuple[str, ...] = _KIND_ORDER,
) -> Optional[discord.Embed]:
    """Compose the per-guild market-update embed from cache.

    `kinds` lets the topic-channel router request a single-kind embed (e.g.
    a guild routes NFT-only to daily_channel_nft). The default value covers
    every supported kind, and the per-kind sections are emitted in fixed
    display order. Empty kinds are silently skipped.

    Content rule: if any of the requested kinds has watchlist content, the
    embed shows only those sections — NO Top-10 leaderboard. If every
    requested kind is empty AND the requested set contains crypto, fall
    back to the crypto Top-10 by market cap so the post is never blank.
    Returns None when nothing useful can be assembled."""
    watch_by_kind = _watch_sections_by_kind(guild_id, kinds)
    fallback_top: list[dict] = []
    if not watch_by_kind and 'crypto' in kinds:
        fallback_top = _crypto_top10_fallback()
    if not watch_by_kind and not fallback_top:
        return None

    # Custom template overrides.
    custom_title    = (settings.get('digest_title')  or '').strip()
    custom_intro    = (settings.get('digest_intro')  or '').strip()
    custom_color    = _parse_hex_color(settings.get('digest_color') or '')
    custom_footer   = (settings.get('digest_footer') or '').strip()
    thumb_mode      = (settings.get('digest_thumbnail_mode') or 'brand').strip().lower()
    if thumb_mode not in THUMBNAIL_MODES:
        thumb_mode = 'brand'

    date_suffix = _format_date_suffix(settings)
    title = f'📊 {custom_title or DEFAULT_DIGEST_TITLE}{date_suffix}'
    intro = (custom_intro or DEFAULT_DIGEST_INTRO)

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
        for k in _KIND_ORDER:
            items = watch_by_kind.get(k)
            if items:
                first_img = (items[0][1].get('image_url') or '').strip()
                if first_img:
                    break
        if not first_img and fallback_top:
            first_img = (fallback_top[0].get('image_url') or '').strip()
        if first_img:
            e.set_thumbnail(url=first_img)

    if custom_footer:
        e.set_footer(text=custom_footer[:2048])

    # ── Per-kind sections in fixed display order ────────────────────────
    if watch_by_kind:
        for k in _KIND_ORDER:
            items = watch_by_kind.get(k)
            if not items:
                continue
            lines = _section_lines_watchlist(items)
            if not lines:
                continue
            e.add_field(
                name=_KIND_LABELS.get(k, k.capitalize()),
                value='\n'.join(lines)[:1024],
                inline=False,
            )
    else:
        # Crypto-Top-10 leaderboard fallback.
        lines = _section_lines_top(fallback_top)
        e.add_field(
            name=f'Top {len(fallback_top)} by Market Cap',
            value='\n'.join(lines)[:1024],
            inline=False,
        )

    return e


class DigestSendError(Exception):
    """Raised when a digest cannot be posted. The message is admin-friendly
    so api.py can surface it directly. The string itself is also logged."""


_TOPIC_CHANNEL_KEYS = {
    'crypto': 'daily_channel_crypto',
    'nft':    'daily_channel_nft',
    'meme':   'daily_channel_meme',
    'forex':  'daily_channel_forex',
}


def _resolve_channel_id(value) -> Optional[int]:
    """Channel ids may be stored as int (legacy) or str (polish-A path).
    Coerce both shapes to int; empty/None → None."""
    if value is None or value == '':
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


async def post_digest_now(bot, guild_id: int, settings: dict) -> dict:
    """Build + send the market-update embeds right now. Routes to per-topic
    channels when configured: each kind with a configured topic-channel gets
    its own topic-scoped embed; kinds without a topic-channel but with
    watchlist content go to the main daily_channel_crypto channel as a
    combined embed. If neither path produces a send, raises DigestSendError.

    Returns {'channel_id': int, 'message_id': int, 'watchlist_count': int,
    'posts': int} — channel_id/message_id reference the first successful
    post for backwards compatibility, and `posts` is the total count."""
    if not bot.is_ready():
        raise DigestSendError('Bot is starting up; try again in a moment.')

    guild = bot.get_guild(int(guild_id))
    if guild is None:
        raise DigestSendError('Bot is not in this server.')

    main_ch_raw = settings.get('daily_channel_crypto')
    main_ch_id  = _resolve_channel_id(main_ch_raw)

    # Per-topic channel map. Drop entries with no channel id.
    topic_channels: dict[str, int] = {}
    for k, key in _TOPIC_CHANNEL_KEYS.items():
        cid = _resolve_channel_id(settings.get(key))
        if cid is not None:
            topic_channels[k] = cid

    if not topic_channels and main_ch_id is None:
        raise DigestSendError('Configure a crypto digest channel first.')

    # For routing decisions we need to know which kinds the guild has
    # content for right now.
    watch_by_kind = _watch_sections_by_kind(int(guild_id), _KIND_ORDER)

    # Topics with their own channel → topic-scoped embed.
    # Topics without their own channel but with content AND a main channel
    # → folded into the combined embed sent to the main channel.
    topic_posts: list[tuple[str, int, tuple]] = []   # (kind, channel_id, (kind,))
    combined_kinds: list[str] = []
    for k in _KIND_ORDER:
        if k in topic_channels:
            # Always post the topic embed for explicitly-routed kinds, even
            # if their section ends up empty (the body will fall back to
            # crypto top-10 for the crypto channel, or yield None for the
            # other kinds — in that case we just skip the send).
            topic_posts.append((k, topic_channels[k], (k,)))
        elif k in watch_by_kind:
            combined_kinds.append(k)

    if combined_kinds and main_ch_id is None:
        # Some kinds need posting but no main channel exists. They'd be
        # silently dropped — note it but don't fail the whole send.
        print(f'[radar/digest] g={guild_id} dropping kinds with no channel: '
              f'{combined_kinds}')

    mentions = _parse_role_id_list(settings.get('digest_mention_role_ids'))
    content  = _mention_content(mentions)
    allowed  = discord.AllowedMentions(roles=True, users=False, everyone=False)

    sent_posts: list[dict] = []
    first_topic_skip_reason = None

    # Per-topic posts.
    for kind, ch_id, ks in topic_posts:
        try:
            channel = guild.get_channel(int(ch_id))
        except (TypeError, ValueError):
            channel = None
        if channel is None:
            print(f'[radar/digest] g={guild_id} {kind} channel {ch_id} missing — skip')
            continue
        embed = _build_digest_embed(int(guild_id), settings, kinds=ks)
        if embed is None:
            print(f'[radar/digest] g={guild_id} {kind} embed empty — skip')
            if first_topic_skip_reason is None:
                first_topic_skip_reason = (
                    f'No {kind} data to post yet. Either the cache is cold '
                    '(fetcher runs every 5 minutes) or your watchlist is '
                    'empty for that kind.'
                )
            continue
        try:
            msg = await channel.send(content=content, embed=embed,
                                     allowed_mentions=allowed)
            sent_posts.append({'kind': kind, 'channel_id': int(channel.id),
                               'message_id': int(msg.id)})
        except discord.Forbidden:
            raise DigestSendError(
                f'Bot lacks permission to post in #{getattr(channel, "name", ch_id)}.'
            )
        except Exception as e:  # noqa: BLE001
            raise DigestSendError(f'Discord error: {type(e).__name__}: {e}') from e

    # Combined embed for the main channel (covers kinds without explicit
    # channels). Also fires when no per-topic channels were configured at
    # all — in that case the main channel gets the full multi-kind digest.
    main_kinds: tuple[str, ...] = ()
    if main_ch_id is not None:
        if not topic_channels:
            # No per-topic routing at all — main channel gets the whole digest.
            main_kinds = _KIND_ORDER
        elif combined_kinds:
            main_kinds = tuple(combined_kinds)

    if main_kinds:
        try:
            channel = guild.get_channel(int(main_ch_id))
        except (TypeError, ValueError):
            channel = None
        if channel is None:
            print(f'[radar/digest] g={guild_id} main channel {main_ch_id} missing — skip')
        else:
            embed = _build_digest_embed(int(guild_id), settings, kinds=main_kinds)
            if embed is not None:
                try:
                    msg = await channel.send(content=content, embed=embed,
                                             allowed_mentions=allowed)
                    sent_posts.append({'kind': 'combined',
                                       'channel_id': int(channel.id),
                                       'message_id': int(msg.id)})
                except discord.Forbidden:
                    raise DigestSendError(
                        f'Bot lacks permission to post in #{getattr(channel, "name", main_ch_id)}.'
                    )
                except Exception as e:  # noqa: BLE001
                    raise DigestSendError(f'Discord error: {type(e).__name__}: {e}') from e

    if not sent_posts:
        raise DigestSendError(first_topic_skip_reason or (
            'No data to post yet. Either the cache is cold (fetcher runs '
            'every 5 minutes) or every watchlist is empty.'
        ))

    rows = list_radar_watchlist(int(guild_id))
    first = sent_posts[0]
    return {
        'channel_id':      first['channel_id'],
        'message_id':      first['message_id'],
        'watchlist_count': len(rows),
        'posts':           len(sent_posts),
        'all_posts':       sent_posts,
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
