"""
NFT Trending Discovery scanner.

Runs every 10 minutes. Pulls OpenSea's top collections per chain (ranked by
recent volume) and applies the guild's quality filters — 24h volume change %,
sales count, and a minimum native floor price (dust filter). Per-collection
24h cooldown via radar_alerts_log.

Floor prices and volumes from OpenSea are in each chain's native token, so the
old USD volume gate is gone; surge detection rides on volume_change_24h_pct +
sales + a native floor floor.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import discord

from database import (
    get_radar_topic_settings,
    last_radar_alert_at,
    list_guilds_with_radar,
    record_radar_alert,
)
from cogs._branding import build_branded_embed
from .adapters import ADAPTERS_BY_KIND
from .cache import CACHE


_INTERVAL_S = 600                          # 10 min tick
_PER_COLLECTION_COOLDOWN_S = 24 * 3600     # 24h per (guild, collection)
_ALERT_TYPE = 'discovery_nft'

# Chains scanned each tick. OpenSea floor/volume are native-denominated, so we
# keep the scan set small to stay within the 60/min rate budget (each chain is
# one list call plus one stats call per candidate collection).
_DISCOVERY_CHAINS = ('ethereum', 'base', 'solana')
_PER_CHAIN_LIMIT = 10
# Minimum native floor price to count as real (dust filter). Native units, so
# ~0.005 ETH on ETH chains; applied uniformly as a small positive floor.
_MIN_FLOOR_NATIVE = 0.005


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
    raw = snap.get('raw') or {}
    floor          = snap.get('price_usd')               # native floor price
    vol_change_pct = raw.get('volume_change_24h_pct')
    sales_24h      = raw.get('sales_count_24h')

    try:
        min_vchg  = float(ts.get('discovery_min_volume_change_24h_pct') or 0)
        min_sales = int(ts.get('discovery_min_sales_24h') or 0)
    except (TypeError, ValueError):
        return False

    if vol_change_pct is None or vol_change_pct < min_vchg:   return False
    if sales_24h is None or sales_24h < min_sales:            return False
    if floor is None or floor < _MIN_FLOOR_NATIVE:           return False  # dust
    return True


def _build_embed(guild_id: int, snap: dict) -> discord.Embed:
    raw = snap.get('raw') or {}
    name = snap.get('raw', {}).get('name') or snap.get('symbol_display') or 'Collection'
    chain = (raw.get('chain') or '').upper()
    sym = snap.get('price_display_symbol') or ''
    title = f'🎨 NFT Heating Up: {name}'

    def _amt(v, decimals):
        # Native-token amount with the chain glyph (Ξ12.50 / 12.50 SOL).
        if len(sym) == 1:
            return f'{sym}{v:,.{decimals}f}'
        return f'{v:,.{decimals}f} {sym}'

    desc: list[str] = []
    if chain:
        desc.append(f'**Chain:** {chain}')
    floor = snap.get('price_usd')
    if floor:
        desc.append(f'**Floor:** {_amt(floor, 3)}')
    vol = snap.get('volume_24h_usd')
    if vol:
        desc.append(f'**24h volume:** {_amt(vol, 2)}')
    vol_chg = raw.get('volume_change_24h_pct')
    if vol_chg is not None:
        desc.append(f'**Volume change 24h:** {vol_chg:+.1f}%')
    sales = raw.get('sales_count_24h')
    if sales is not None:
        desc.append(f'**Sales last 24h:** {sales}')
    if snap.get('page_url'):
        desc.append(f'\n[Open collection]({snap["page_url"]})')

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
        print(f'[radar/discovery_nft] g={guild_id} channel {ch_id} missing — skip')
        return 0

    mention_role_ids = _parse_role_ids(ts.get('discovery_mention_role_ids'))
    content = (' '.join(f'<@&{rid}>' for rid in mention_role_ids[:25])
               if mention_role_ids else None)
    allowed = discord.AllowedMentions(roles=True, users=False, everyone=False)

    sent = 0
    now_utc = datetime.now(timezone.utc)
    for snap in candidates:
        identifier = snap.get('identifier')
        if not identifier or not _passes_filters(snap, ts):
            continue
        last = last_radar_alert_at(guild_id, identifier, _ALERT_TYPE)
        if last:
            try:
                last_dt = datetime.fromisoformat(str(last).replace('Z', '+00:00'))
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                if (now_utc - last_dt).total_seconds() < _PER_COLLECTION_COOLDOWN_S:
                    continue
            except (TypeError, ValueError):
                pass

        embed = _build_embed(guild_id, snap)
        try:
            await channel.send(content=content, embed=embed,
                               allowed_mentions=allowed)
        except discord.Forbidden:
            print(f'[radar/discovery_nft] g={guild_id} forbidden ch={ch_id}')
            return sent
        except Exception as e:  # noqa: BLE001
            print(f'[radar/discovery_nft] g={guild_id} send failed: '
                  f'{type(e).__name__}: {e}')
            continue

        raw = snap.get('raw') or {}
        record_radar_alert(
            guild_id, 'nft', identifier, _ALERT_TYPE,
            {
                'floor_usd':       snap.get('price_usd'),
                'volume_24h_usd':  snap.get('volume_24h_usd'),
                'volume_change_24h_pct': raw.get('volume_change_24h_pct'),
                'sales_24h':       raw.get('sales_count_24h'),
            },
        )
        sent += 1
        await asyncio.sleep(1.0)
    return sent


async def discovery_nft_loop(bot) -> None:
    print(f'[radar/discovery_nft] loop starting (interval={_INTERVAL_S}s)')
    while True:
        try:
            adapter = ADAPTERS_BY_KIND.get('nft')
            disabled = getattr(adapter, 'disabled_reason', None) if adapter else 'no_adapter'
            if disabled:
                # Adapter env-gated off — sleep and try again next tick.
                if int(__import__('time').time()) % 3600 < _INTERVAL_S:
                    print(f'[radar/discovery_nft] adapter disabled: {disabled}')
            else:
                candidates: list[dict] = []
                for ch in _DISCOVERY_CHAINS:
                    try:
                        rows = await adapter.trending(
                            chain=ch, period='one_day', limit=_PER_CHAIN_LIMIT,
                        )
                        candidates.extend(rows)
                    except Exception as e:  # noqa: BLE001
                        print(f'[radar/discovery_nft] trending chain={ch} failed: '
                              f'{type(e).__name__}: {e}')
                print(f'[radar/discovery_nft] tick: chains={len(_DISCOVERY_CHAINS)} '
                      f'candidates={len(candidates)}')
                # Cache each candidate so the NEXT tick has a prior volume to
                # diff against (OpenSea gives no volume-change field, so the
                # surge signal is built up from successive observations).
                for snap in candidates:
                    ident = snap.get('identifier')
                    if ident:
                        CACHE.put('nft', ident, snap)

                if candidates and bot.is_ready():
                    for gid in list_guilds_with_radar():
                        try:
                            ts = get_radar_topic_settings(gid, 'nft')
                        except Exception as e:  # noqa: BLE001
                            print(f'[radar/discovery_nft] g={gid} settings read failed: '
                                  f'{type(e).__name__}: {e}')
                            continue
                        if not int(ts.get('discovery_enabled') or 0):
                            continue
                        try:
                            n = await _send_for_guild(bot, gid, ts, candidates)
                            if n:
                                print(f'[radar/discovery_nft] g={gid} sent={n}')
                        except Exception as e:  # noqa: BLE001
                            print(f'[radar/discovery_nft] g={gid} send loop crashed: '
                                  f'{type(e).__name__}: {e}')
        except Exception as e:  # noqa: BLE001
            print(f'[radar/discovery_nft] tick crashed: {type(e).__name__}: {e}')

        try:
            await asyncio.sleep(_INTERVAL_S)
        except asyncio.CancelledError:
            print('[radar/discovery_nft] loop cancelled')
            raise
