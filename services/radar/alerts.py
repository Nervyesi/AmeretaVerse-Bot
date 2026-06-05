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
    recent_alert_magnitude,
    recent_alert_exists,
    record_radar_alert,
)
from cogs._branding import build_branded_embed
from .cache import CACHE
from .chain_badges import chain_badge, chain_from_identifier


def _meme_chain_field(embed: discord.Embed, snap: dict) -> None:
    """Add a Chain field (badge) to a memecoin alert embed. No-op for other
    kinds. Chain is read from the snapshot, falling back to the 'chain:address'
    identifier prefix when the cached snapshot predates the chain field."""
    if (snap.get('kind') or '').lower() != 'meme':
        return
    chain = ((snap.get('raw') or {}).get('chain')
             or chain_from_identifier(snap.get('identifier')))
    if chain:
        embed.add_field(name='Chain', value=chain_badge(chain), inline=True)


_HISTORY_LOOKBACK_S  = 3600.0    # ~1h ago for the in-memory change_1h fallback


# ── 24h dedup with doubling exception ───────────────────────────────────────
# Per (guild, asset, alert_kind) within a rolling 24h window:
#   • 1st alert fires freely
#   • a later alert ONLY fires if its magnitude >= 2x the last fired magnitude
#   • after 24h from the most recent fire the chain naturally resets (the
#     lookup is windowed)
# alert_kind is direction-independent ('movement', not '..._up'), so a pump then
# a dump share one doubling chain. The kinds are: 'movement' (all of 1h/24h/7d
# price change collapsed into one alert) and 'volume_surge_24h' (separate).

def should_fire_alert(
    guild_id: int, identifier: str, alert_kind: str, magnitude: float,
) -> bool:
    last = recent_alert_magnitude(guild_id, identifier, alert_kind, within_hours=24)
    if last is not None:
        # A prior alert of this kind exists within 24h with a known magnitude:
        # only re-fire if this one is at least double it.
        return abs(float(magnitude)) >= last * 2.0
    # No known magnitude. 'last is None' is ambiguous: either no recent alert at
    # all (fire freely) OR a recent alert whose magnitude wasn't recorded (legacy
    # row / partial migration). In the latter case we already alerted this window,
    # so suppress — never silently re-fire just because the prior magnitude is NULL.
    if recent_alert_exists(guild_id, identifier, alert_kind, within_hours=24):
        return False
    return True


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

    # Forex is daily-cadence (Frankfurter) so a "1h" change is never
    # meaningful — fall back to the 24h delta directly so movement alerts
    # still work on currency pairs.
    if kind == 'forex':
        v24 = snap.get('change_24h_pct')
        if v24 is not None:
            try:
                return float(v24)
            except (TypeError, ValueError):
                pass
        return None

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

def _volume_multiple(snap: dict, kind: str, identifier: str) -> Optional[float]:
    """Current 24h volume as a multiple of the recent median, or None when
    there isn't enough history. The caller compares it to the guild's
    multiplier threshold and uses it as the alert magnitude."""
    vol_now = snap.get('volume_24h_usd')
    if not vol_now or vol_now <= 0:
        return None
    hist = CACHE.history(kind, identifier)
    if len(hist) < 6:    # need a few samples before we'll call anything a spike
        return None
    vols = sorted(
        float(h[1].get('volume_24h_usd') or 0)
        for h in hist
        if h[1].get('volume_24h_usd')
    )
    if not vols:
        return None
    median = vols[len(vols) // 2]
    if median <= 0:
        return None
    return float(vol_now) / median


# ── Discord embed builders ──────────────────────────────────────────────────

def _price_str(snap: dict) -> Optional[str]:
    """Format a snapshot price. NFT floors are in chain-native units (ETH, SOL,
    ...) so they use the snapshot's display symbol; crypto/meme are USD."""
    p = snap.get('price_usd')
    if p is None:
        return None
    if (snap.get('kind') or '').lower() == 'nft':
        sym = snap.get('price_display_symbol') or 'Ξ'
        return f'{sym}{p:,.4f}' if len(sym) == 1 else f'{p:,.4f} {sym}'
    return f'${p:,.4f}'


def _volume_str(snap: dict) -> Optional[str]:
    """Format a snapshot's 24h volume. NFT volumes are chain-native (ETH on
    Ethereum collections, etc.); crypto/meme are USD."""
    v = snap.get('volume_24h_usd')
    if not v:
        return None
    if (snap.get('kind') or '').lower() == 'nft':
        sym = snap.get('price_display_symbol') or 'Ξ'
        return f'{sym}{v:,.2f}' if len(sym) == 1 else f'{v:,.2f} {sym}'
    return f'${v:,.0f}'


def _alert_movement_embed(
    guild_id: int, snap: dict,
    *, primary_tf: str, direction: str,
    ch1h: Optional[float] = None, ch24: Optional[float] = None,
    ch7d: Optional[float] = None,
) -> discord.Embed:
    """Unified movement embed: ONE alert covering all available timeframes.
    `primary_tf` is the highest-priority triggering timeframe (1h > 24h > 7d)
    and sets the title; the body lists every timeframe with a real change. A
    None or exactly-0.0 change is omitted (cold cache, not a signal) — never
    rendered as +0.00%."""
    arrow = '🚀' if direction == 'up' else '📉'
    verb = 'pumping' if direction == 'up' else 'dumping'
    sym = snap.get('display_name') or snap.get('symbol_display') or snap.get('identifier')
    title = f'{arrow} {sym} {verb} ({primary_tf})'

    name = snap.get('raw', {}).get('name') or snap.get('symbol_display')

    desc_lines = []
    if snap.get('price_usd') is not None:
        desc_lines.append(f'**Price:** {_price_str(snap)}')
    for _lbl, _ch in (('1h', ch1h), ('24h', ch24), ('7d', ch7d)):
        if _ch is not None and _ch != 0.0:
            desc_lines.append(f'**{_lbl} change:** {_ch:+.2f}%')
    if snap.get('volume_24h_usd'):
        desc_lines.append(f'**24h volume:** {_volume_str(snap)}')
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
    _meme_chain_field(e, snap)
    return e


def _alert_volume_embed(guild_id: int, snap: dict) -> discord.Embed:
    _label = snap.get('display_name') or snap.get('symbol_display') or snap.get('identifier')
    title = f'📊 {_label} volume surge (24h)'
    price = snap.get('price_usd')
    vol = snap.get('volume_24h_usd')

    desc = []
    if price is not None:
        desc.append(f'**Price:** {_price_str(snap)}')
    if vol:
        desc.append(f'**24h volume:** {_volume_str(snap)}')
    ch24 = snap.get('change_24h_pct')
    if ch24 is not None and ch24 != 0.0:
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
    _meme_chain_field(e, snap)
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


_TOPICS = ('crypto', 'nft', 'meme', 'forex')


async def dispatch_alerts(bot) -> dict:
    """Per-(guild, topic) alert dispatcher. Each topic owns its own
    alerts_channel, movement_threshold_pct, volume_multiplier_threshold,
    and alerts_mention_role_ids — read from radar_topic_settings, not the
    legacy guild-flat radar_settings."""
    from database import get_radar_topic_settings
    summary: dict = {}
    if not bot.is_ready():
        print('[radar/alerts] bot not ready — skipping dispatch')
        return summary

    for gid in list_guilds_with_radar():
        try:
            full_watchlist = list_radar_watchlist(gid)
        except Exception as e:  # noqa: BLE001
            print(f'[radar/alerts] watchlist read failed g={gid}: '
                  f'{type(e).__name__}: {e}')
            continue

        per_topic_summary: dict = {}

        for topic in _TOPICS:
            try:
                ts = get_radar_topic_settings(gid, topic)
            except Exception as e:  # noqa: BLE001
                print(f'[radar/alerts] g={gid} {topic} settings read failed: '
                      f'{type(e).__name__}: {e}')
                continue
            if not int(ts.get('alerts_enabled') or 0):
                continue
            ch_id = ts.get('alerts_channel')
            if not ch_id:
                continue
            # Per-timeframe thresholds + enable flags (Phase 3). Each
            # threshold has a dedicated 1h cooldown per (asset, type).
            def _float(k, default):
                try:
                    return float(ts.get(k)) if ts.get(k) is not None else default
                except (TypeError, ValueError):
                    return default

            thr_1h   = _float('alert_1h_threshold_pct',   3.0)
            thr_24h  = _float('alert_24h_threshold_pct',  8.0)
            thr_7d   = _float('alert_7d_threshold_pct',  20.0)
            vol_mul  = _float('alert_volume_multiplier',  2.5)
            en_1h    = int(ts.get('alert_1h_enabled')     or 0) == 1
            en_24h   = int(ts.get('alert_24h_enabled')    or 0) == 1
            en_7d    = int(ts.get('alert_7d_enabled')     or 0) == 1
            en_vol   = int(ts.get('alert_volume_enabled') or 0) == 1
            alert_mentions = _parse_role_id_list(ts.get('alerts_mention_role_ids'))

            sent = 0
            for row in full_watchlist:
                if (row.get('asset_kind') or '').lower() != topic:
                    continue
                identifier_raw = (row.get('asset_identifier') or '')
                if not identifier_raw:
                    continue
                ident = (identifier_raw.lower()
                         if topic in ('crypto', 'nft')
                         else identifier_raw)
                snap = CACHE.get_snapshot(topic, ident)
                if not snap:
                    if topic == 'meme':
                        print(f'[radar/alerts] meme_eval g={gid} asset={ident} '
                              f'snapshot=MISSING (cache cold or fetch failing)')
                    continue

                # Observability for the memecoin smoke test: log every meme
                # evaluation with the values + thresholds that gate it. Read-only.
                if topic == 'meme':
                    _d1h = _change_1h_pct(snap, topic, ident)
                    _d24 = snap.get('change_24h_pct')
                    print(
                        f'[radar/alerts] meme_eval g={gid} asset={ident} '
                        f'1h={_d1h if _d1h is not None else "n/a"} '
                        f'24h={_d24 if _d24 is not None else "n/a"} '
                        f'thr_1h={thr_1h} thr_24h={thr_24h} '
                        f'en_1h={en_1h} en_24h={en_24h} en_vol={en_vol}'
                    )

                # ── Unified price MOVEMENT alert ────────────────────────
                # 1h/24h/7d are collapsed into ONE 'movement' alert_kind per
                # 24h (doubling dedup) so a coin can't spam multiple timeframe
                # alerts in a day. A None or exactly-0.0 change is a cold-cache
                # non-signal: it never triggers and never renders as +0.00%.
                # volume_surge_24h stays a SEPARATE alert below.
                def _num(v):
                    try:
                        return float(v) if v is not None else None
                    except (TypeError, ValueError):
                        return None

                ch1h = _num(_change_1h_pct(snap, topic, ident))
                ch24 = _num(snap.get('change_24h_pct'))
                ch7d = _num((snap.get('raw') or {}).get('change_7d_pct')) if topic == 'crypto' else None

                # Triggering timeframes in priority order (1h > 24h > 7d).
                triggered = []
                for _lbl, _ch, _thr, _en in (
                    ('1h',  ch1h, thr_1h,  en_1h),
                    ('24h', ch24, thr_24h, en_24h),
                    ('7d',  ch7d, thr_7d,  en_7d and topic == 'crypto'),
                ):
                    if _en and _thr > 0 and _ch is not None and _ch != 0.0 and abs(_ch) >= _thr:
                        triggered.append((_lbl, _ch))

                if triggered:
                    primary_tf, primary_change = triggered[0]
                    max_mag = max(abs(c) for _, c in triggered)
                    direction = 'up' if primary_change > 0 else 'down'
                    fire = should_fire_alert(gid, ident, 'movement', max_mag)
                    print(
                        f'[radar/alerts] dedup_check g={gid} asset={ident} '
                        f'kind=movement mag={max_mag:.2f} dir={direction} fire={fire}'
                    )
                    if fire:
                        embed = _alert_movement_embed(
                            gid, snap, primary_tf=primary_tf, direction=direction,
                            ch1h=ch1h, ch24=ch24, ch7d=ch7d,
                        )
                        if await _send_alert(bot, gid, int(ch_id), embed,
                                             mention_role_ids=alert_mentions):
                            record_radar_alert(
                                gid, topic, ident, 'movement',
                                {'change_1h_pct': ch1h, 'change_24h_pct': ch24,
                                 'change_7d_pct': ch7d, 'price_usd': snap.get('price_usd'),
                                 'volume_24h_usd': snap.get('volume_24h_usd')},
                                magnitude=max_mag, direction=direction,
                            )
                            sent += 1

                # ── Volume surge — forex skipped, others use multiplier ──
                if topic == 'forex' or not en_vol:
                    continue
                mult = _volume_multiple(snap, topic, ident)
                if mult is not None and vol_mul > 1.0 and mult >= vol_mul:
                    fire = should_fire_alert(gid, ident, 'volume_surge_24h', mult)
                    print(
                        f'[radar/alerts] dedup_check g={gid} asset={ident} '
                        f'kind=volume_surge_24h mag={mult:.2f} dir=up fire={fire}'
                    )
                    if fire:
                        embed = _alert_volume_embed(gid, snap)
                        if await _send_alert(bot, gid, int(ch_id), embed,
                                             mention_role_ids=alert_mentions):
                            record_radar_alert(
                                gid, topic, ident, 'volume_surge_24h',
                                {'volume_24h_usd': snap.get('volume_24h_usd'),
                                 'price_usd':      snap.get('price_usd'),
                                 'volume_multiple': mult},
                                magnitude=mult, direction='up',
                            )
                            sent += 1

            if sent:
                per_topic_summary[topic] = sent

        if per_topic_summary:
            summary[str(gid)] = per_topic_summary

    if summary:
        print(f'[radar/alerts] dispatch sent={summary}')
    return summary
