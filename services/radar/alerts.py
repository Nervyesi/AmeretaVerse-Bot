"""
Alert evaluator + Discord dispatcher.

Three alert types this phase:
  • movement_up      — change_1h_pct >=  guild.movement_threshold_pct
  • movement_down    — change_1h_pct <= -guild.movement_threshold_pct
  • volume_spike     — 1h volume estimate >= guild.volume_multiplier_threshold × baseline

Dedup / cooldown
  Per (guild_id, asset_identifier, alert_type) we do NOT send the same alert
  type again within 1 hour. The lookup hits radar_alerts_log via the
  cooldown index. Every successful send writes a new row.

The dispatcher is invoked at the end of each fetcher tick (chained, not on
a separate timer) so an alert never fires on stale data.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from typing import Optional

import discord

from database import (
    get_radar_settings,
    list_guilds_with_radar,
    list_radar_watchlist,
    last_radar_alert_at,
    record_radar_alert,
)
from cogs._branding import build_branded_embed
from .cache import CACHE


_COOLDOWN_MOVEMENT_S = 3600.0    # 1 hour per (guild, asset, direction)
_COOLDOWN_VOLUME_S   = 3600.0
_HISTORY_LOOKBACK_S  = 3600.0    # ~1h ago for the in-memory change_1h fallback


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        s2 = str(s).replace('Z', '+00:00')
        d  = datetime.fromisoformat(s2)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d
    except (TypeError, ValueError):
        return None


def _cooled_down(
    guild_id: int, identifier: str, alert_type: str,
    cooldown_seconds: float,
) -> bool:
    """True when an alert of this type for this asset was sent recently
    enough that we should suppress this one."""
    last = _parse_iso(last_radar_alert_at(guild_id, identifier, alert_type))
    if last is None:
        return False
    return (datetime.now(timezone.utc) - last).total_seconds() < cooldown_seconds


# ── Change-1h fallback ──────────────────────────────────────────────────────
# CoinGecko returns price_change_percentage_1h_in_currency on /coins/markets
# when we request it via price_change_percentage=1h. If for any reason that
# field is missing on a row, we fall back to comparing against the cache's
# ~1h-old snapshot.

def _change_1h_pct(snap: dict, kind: str, identifier: str) -> Optional[float]:
    v = snap.get('change_1h_pct')
    if v is not None:
        try:
            return float(v)
        except (TypeError, ValueError):
            pass

    older = CACHE.snapshot_about(kind, identifier, _HISTORY_LOOKBACK_S)
    if not older:
        return None
    p_now = snap.get('price_usd')
    p_old = older.get('price_usd')
    if not p_now or not p_old or p_old == 0:
        return None
    try:
        return (float(p_now) - float(p_old)) / float(p_old) * 100.0
    except (TypeError, ValueError, ZeroDivisionError):
        return None


# ── Volume baseline ─────────────────────────────────────────────────────────
# We don't have a true "last 1h volume" field from CoinGecko (only 24h
# rolling). For Phase 1 we approximate: spike = current 24h volume >=
# multiplier × the median 24h volume across the recent in-memory history.
# That catches genuine sustained spikes without flapping on tiny ticks.

def _volume_spike(snap: dict, kind: str, identifier: str, multiplier: float) -> bool:
    vol_now = snap.get('volume_24h_usd')
    if not vol_now or vol_now <= 0 or multiplier <= 1.0:
        return False
    hist = CACHE.history(kind, identifier)
    if len(hist) < 6:    # need a few samples before we'll call anything a spike
        return False
    vols = sorted(
        float(h[1].get('volume_24h_usd') or 0)
        for h in hist
        if h[1].get('volume_24h_usd')
    )
    if not vols:
        return False
    median = vols[len(vols) // 2]
    if median <= 0:
        return False
    return float(vol_now) >= float(multiplier) * median


# ── Discord embed builders ──────────────────────────────────────────────────

def _alert_movement_embed(guild_id: int, snap: dict, change_pct: float) -> discord.Embed:
    is_up = change_pct >= 0
    arrow = '🚀' if is_up else '📉'
    direction = 'pumping' if is_up else 'dumping'
    title = f'{arrow} {snap.get("symbol_display") or snap.get("identifier")} {direction}'

    price = snap.get('price_usd')
    vol_24h = snap.get('volume_24h_usd')
    name = snap.get('raw', {}).get('name') or snap.get('symbol_display')

    desc_lines = []
    if price is not None:
        desc_lines.append(f'**Price:** ${price:,.4f}')
    desc_lines.append(f'**1h change:** {change_pct:+.2f}%')
    ch24 = snap.get('change_24h_pct')
    if ch24 is not None:
        desc_lines.append(f'**24h change:** {ch24:+.2f}%')
    if vol_24h:
        desc_lines.append(f'**24h volume:** ${vol_24h:,.0f}')
    # No upstream-source attribution link — brand footer is the only credit.

    e = build_branded_embed(
        int(guild_id),
        title=title,
        description='\n'.join(desc_lines),
        cog_prefix='',
        use_thumbnail=False,
        use_image=False,
        use_footer=True,
    )
    img = snap.get('image_url')
    if img:
        e.set_thumbnail(url=img)
    if name and name != snap.get('symbol_display'):
        e.add_field(name='Asset', value=str(name), inline=True)
    return e


def _alert_volume_embed(guild_id: int, snap: dict) -> discord.Embed:
    title = f'📊 {snap.get("symbol_display") or snap.get("identifier")} volume spike'
    price = snap.get('price_usd')
    vol = snap.get('volume_24h_usd')

    desc = []
    if price is not None:
        desc.append(f'**Price:** ${price:,.4f}')
    if vol:
        desc.append(f'**24h volume:** ${vol:,.0f}')
    ch24 = snap.get('change_24h_pct')
    if ch24 is not None:
        desc.append(f'**24h change:** {ch24:+.2f}%')
    # No upstream-source attribution link.

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


# ── Dispatcher ──────────────────────────────────────────────────────────────

def _parse_role_id_list(raw) -> list[str]:
    """Read the JSON-array role-id column. The canonical normalization
    (commas/spaces/newlines → JSON string) happens on write in api.py via
    _normalize_role_id_list; here we just decode and stringify."""
    if raw is None or raw == '':
        return []
    try:
        data = raw if isinstance(raw, list) else json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [str(v).strip() for v in data if str(v).strip()]


async def _send_alert(
    bot, guild_id: int, channel_id: int, embed: discord.Embed,
    *, mention_role_ids: Optional[list[str]] = None,
) -> bool:
    """Attempt to send a single alert embed. Returns True on success.
    Soft-fails on every Discord exception so an alert flood from one
    misconfigured guild never breaks the dispatcher.

    mention_role_ids → content becomes ' '.join(<@&id>). AllowedMentions
    restricts pings to roles only (no users / everyone)."""
    try:
        guild = bot.get_guild(int(guild_id))
        if guild is None:
            print(f'[radar/alerts] guild {guild_id} not in bot cache — skip')
            return False
        channel = guild.get_channel(int(channel_id))
        if channel is None:
            print(f'[radar/alerts] channel {channel_id} not found in guild {guild_id} — skip')
            return False
        content = None
        if mention_role_ids:
            content = ' '.join(f'<@&{rid}>' for rid in mention_role_ids[:25])
        await channel.send(
            content=content,
            embed=embed,
            allowed_mentions=discord.AllowedMentions(
                roles=True, users=False, everyone=False,
            ),
        )
        return True
    except discord.Forbidden:
        print(f'[radar/alerts] forbidden in channel={channel_id} guild={guild_id}')
        return False
    except Exception as e:  # noqa: BLE001
        print(f'[radar/alerts] send failed g={guild_id} ch={channel_id}: '
              f'{type(e).__name__}: {e}')
        return False


async def dispatch_alerts(bot) -> dict:
    """Walk every guild with radar settings, evaluate each watchlist asset
    against the latest cache snapshot, send alerts that aren't on
    cooldown. Returns a small per-guild summary for logging.
    """
    summary: dict = {}
    if not bot.is_ready():
        print('[radar/alerts] bot not ready — skipping dispatch')
        return summary

    for gid in list_guilds_with_radar():
        try:
            settings = get_radar_settings(gid)
        except Exception as e:  # noqa: BLE001
            print(f'[radar/alerts] settings read failed g={gid}: {type(e).__name__}: {e}')
            continue
        if not int(settings.get('alerts_enabled') or 0):
            continue
        ch_id = settings.get('alerts_channel')
        if not ch_id:
            continue

        try:
            move_thr = float(settings.get('movement_threshold_pct') or 5.0)
            vol_mul  = float(settings.get('volume_multiplier_threshold') or 3.0)
        except (TypeError, ValueError):
            move_thr, vol_mul = 5.0, 3.0

        alert_mentions = _parse_role_id_list(settings.get('alerts_mention_role_ids'))

        try:
            watchlist = list_radar_watchlist(gid, asset_kind='crypto')
        except Exception as e:  # noqa: BLE001
            print(f'[radar/alerts] watchlist read failed g={gid}: '
                  f'{type(e).__name__}: {e}')
            continue

        sent = 0
        for row in watchlist:
            identifier = (row.get('asset_identifier') or '').lower()
            if not identifier:
                continue
            snap = CACHE.get_snapshot('crypto', identifier)
            if not snap:
                continue

            # Movement
            ch1h = _change_1h_pct(snap, 'crypto', identifier)
            if ch1h is not None and move_thr > 0:
                if ch1h >= move_thr:
                    if not _cooled_down(gid, identifier, 'movement_up', _COOLDOWN_MOVEMENT_S):
                        embed = _alert_movement_embed(gid, snap, ch1h)
                        if await _send_alert(bot, gid, int(ch_id), embed,
                                             mention_role_ids=alert_mentions):
                            record_radar_alert(
                                gid, 'crypto', identifier, 'movement_up',
                                {'change_1h_pct': ch1h,
                                 'price_usd':     snap.get('price_usd'),
                                 'volume_24h_usd': snap.get('volume_24h_usd')},
                            )
                            sent += 1
                elif ch1h <= -move_thr:
                    if not _cooled_down(gid, identifier, 'movement_down', _COOLDOWN_MOVEMENT_S):
                        embed = _alert_movement_embed(gid, snap, ch1h)
                        if await _send_alert(bot, gid, int(ch_id), embed,
                                             mention_role_ids=alert_mentions):
                            record_radar_alert(
                                gid, 'crypto', identifier, 'movement_down',
                                {'change_1h_pct': ch1h,
                                 'price_usd':     snap.get('price_usd'),
                                 'volume_24h_usd': snap.get('volume_24h_usd')},
                            )
                            sent += 1

            # Volume spike
            if vol_mul > 1.0 and _volume_spike(snap, 'crypto', identifier, vol_mul):
                if not _cooled_down(gid, identifier, 'volume_spike', _COOLDOWN_VOLUME_S):
                    embed = _alert_volume_embed(gid, snap)
                    if await _send_alert(bot, gid, int(ch_id), embed,
                                         mention_role_ids=alert_mentions):
                        record_radar_alert(
                            gid, 'crypto', identifier, 'volume_spike',
                            {'volume_24h_usd': snap.get('volume_24h_usd'),
                             'price_usd':      snap.get('price_usd')},
                        )
                        sent += 1

        if sent:
            summary[str(gid)] = sent

    if summary:
        print(f'[radar/alerts] dispatch sent={summary}')
    return summary
