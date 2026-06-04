"""
Radar slash commands (Phase 1 — crypto).

Commands:
  /price token <symbol_or_id>    — anyone can run; ephemeral CoinGecko lookup
  /topgainers crypto             — top 24h gainers from the fetched leaderboard
  /toplosers crypto              — top 24h losers
  /watchlist                     — show this guild's crypto watchlist
  /radar add <kind> <id> [name]  — admin only (kind=crypto in Phase 1)
  /radar remove <kind> <id>      — admin only

All commands defer immediately and respond via followup so the Discord 3s
window never bites. No command crashes on adapter failure — they
gracefully report "unavailable" instead.

Other asset kinds (nft, meme, forex, stocks) are accepted at the slash
layer but the cog responds with a clear "coming in a later phase" message
until those adapters ship. The DB still records the watchlist row if
admins choose to seed entries early.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from database import (
    log_event,
    list_radar_watchlist,
    add_radar_watchlist_entry,
    remove_radar_watchlist_by_identifier,
)
from cogs._branding import build_branded_embed

from services.radar.cache import CACHE
from services.radar.adapters import ADAPTERS_BY_KIND, SUPPORTED_KINDS_PHASE_1
from services.radar.chain_badges import chain_badge, chain_from_identifier


# ── Choice lists ────────────────────────────────────────────────────────────
_KIND_CHOICES = [
    app_commands.Choice(name='Crypto',       value='crypto'),
    app_commands.Choice(name='NFT (coming soon)',   value='nft'),
    app_commands.Choice(name='Meme (coming soon)',  value='meme'),
    app_commands.Choice(name='Forex (coming soon)', value='forex'),
    app_commands.Choice(name='Stocks (coming soon)',value='stocks'),
]
_GAINLOSS_KIND_CHOICES = [
    app_commands.Choice(name='Crypto', value='crypto'),
]


# ── Embed builders shared with api.py preview ──────────────────────────────

def build_price_embed(guild_id: int, snap: dict) -> discord.Embed:
    sym  = (snap.get('symbol_display') or snap.get('identifier') or '').upper()
    name = snap.get('raw', {}).get('name') or sym
    title = f'{sym} — {name}'

    desc: list[str] = []
    price = snap.get('price_usd')
    if price is not None:
        desc.append(f'**Price:** {_fmt_price(price)}')
    ch1 = snap.get('change_1h_pct')
    if ch1 is not None:
        desc.append(f'**1h:** {ch1:+.2f}%')
    ch24 = snap.get('change_24h_pct')
    if ch24 is not None:
        desc.append(f'**24h:** {ch24:+.2f}%')
    vol = snap.get('volume_24h_usd')
    if vol:
        desc.append(f'**24h volume:** ${vol:,.0f}')
    mc = snap.get('market_cap_usd')
    if mc:
        desc.append(f'**Market cap:** ${mc:,.0f}')
    rank = snap.get('rank')
    if rank:
        desc.append(f'**Rank:** #{rank}')
    # No upstream-source attribution link — brand footer is the only credit.

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


def _fmt_price(n) -> str:
    try:
        v = float(n)
    except (TypeError, ValueError):
        return '—'
    if v >= 1000:  return f'${v:,.2f}'
    if v >= 1:     return f'${v:,.4f}'
    return f'${v:.6f}'


async def _resolve_crypto_snapshot(symbol_or_id: str) -> Optional[dict]:
    """Cache-first single-coin lookup. Falls back to CoinGecko search +
    fetch when the input doesn't match a cached id directly."""
    q = (symbol_or_id or '').strip().lower()
    if not q:
        return None
    # Direct id hit?
    snap = CACHE.get_snapshot('crypto', q)
    if snap:
        return snap

    adapter = ADAPTERS_BY_KIND.get('crypto')
    if not adapter:
        return None

    # Try resolving as a CoinGecko id directly.
    snap = await adapter.fetch_one(q)
    if snap:
        CACHE.put('crypto', snap['identifier'], snap)
        return snap

    # Fall back to search → first result → fetch.
    suggestions = await adapter.search(q, limit=1)
    if suggestions:
        cid = suggestions[0].get('identifier')
        if cid:
            snap = await adapter.fetch_one(cid)
            if snap:
                CACHE.put('crypto', snap['identifier'], snap)
                return snap
    return None


# ── Cog ─────────────────────────────────────────────────────────────────────

class Radar(commands.Cog):
    """Radar slash commands. The cog itself owns NOTHING long-running —
    the fetcher / alerts / digest loops are launched from bot.py via
    services.radar.* directly."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    price_group = app_commands.Group(
        name='price', description='Look up live prices'
    )
    radar_group = app_commands.Group(
        name='radar', description='Radar watchlist + settings (admin only)'
    )

    # ── /price token ──────────────────────────────────────────────────────
    @price_group.command(name='token', description='Look up a crypto price by symbol or CoinGecko id')
    @app_commands.describe(symbol_or_id='e.g. bitcoin, btc, ethereum, eth')
    async def price_token(
        self, interaction: discord.Interaction, symbol_or_id: str,
    ):
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.HTTPException:
            pass
        try:
            snap = await _resolve_crypto_snapshot(symbol_or_id)
        except Exception as e:  # noqa: BLE001
            print(f'[radar/price_token] {type(e).__name__}: {e}')
            snap = None
        if not snap:
            await interaction.followup.send(
                f'Could not find a price for **{symbol_or_id}** right now. '
                'Try a CoinGecko id like `bitcoin` or a ticker like `btc`.',
                ephemeral=True,
            )
            return
        gid = interaction.guild_id or 0
        await interaction.followup.send(
            embed=build_price_embed(gid, snap), ephemeral=True,
        )

    # ── /price (placeholder commands for future topics) ───────────────────
    @price_group.command(name='nft', description='Coming soon: NFT collection lookup')
    @app_commands.describe(collection='OpenSea collection, e.g. ethereum:pudgypenguins')
    async def price_nft(self, interaction: discord.Interaction, collection: str):
        await self._coming_soon(interaction, 'NFT')

    @price_group.command(name='meme', description='Coming soon: DEX memecoin lookup')
    @app_commands.describe(chain_address='e.g. ethereum:0x... or solana:...')
    async def price_meme(self, interaction: discord.Interaction, chain_address: str):
        await self._coming_soon(interaction, 'Memecoin')

    @price_group.command(name='forex', description='Coming soon: forex pair lookup')
    @app_commands.describe(pair='e.g. EUR/USD')
    async def price_forex(self, interaction: discord.Interaction, pair: str):
        await self._coming_soon(interaction, 'Forex')

    @price_group.command(name='stock', description='Coming soon: stock ticker lookup')
    @app_commands.describe(ticker='e.g. TSLA')
    async def price_stock(self, interaction: discord.Interaction, ticker: str):
        await self._coming_soon(interaction, 'Stocks')

    async def _coming_soon(self, interaction: discord.Interaction, label: str) -> None:
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.HTTPException:
            pass
        await interaction.followup.send(
            f'**{label}** is part of the Radar module but ships in a later phase. '
            'Crypto is live now via `/price token` and `/topgainers crypto`.',
            ephemeral=True,
        )

    # ── /topgainers and /toplosers ────────────────────────────────────────
    @app_commands.command(name='topgainers', description='Top 10 24h gainers')
    @app_commands.choices(kind=_GAINLOSS_KIND_CHOICES)
    async def topgainers(
        self, interaction: discord.Interaction, kind: app_commands.Choice[str],
    ):
        await self._send_top(interaction, kind.value, gainers=True)

    @app_commands.command(name='toplosers', description='Top 10 24h losers')
    @app_commands.choices(kind=_GAINLOSS_KIND_CHOICES)
    async def toplosers(
        self, interaction: discord.Interaction, kind: app_commands.Choice[str],
    ):
        await self._send_top(interaction, kind.value, gainers=False)

    async def _send_top(
        self, interaction: discord.Interaction, kind: str, *, gainers: bool,
    ) -> None:
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.HTTPException:
            pass
        if kind != 'crypto':
            await interaction.followup.send(
                'Only crypto is live this phase.', ephemeral=True,
            )
            return

        rows = [c.snapshot for c in CACHE.all_for_kind('crypto')
                if c.snapshot.get('change_24h_pct') is not None]
        if not rows:
            # Cold cache — trigger a one-shot fetch so first run still works.
            try:
                from services.radar.fetcher import fetch_once
                await fetch_once()
            except Exception as e:  # noqa: BLE001
                print(f'[radar/top] cold-fetch failed: {type(e).__name__}: {e}')
            rows = [c.snapshot for c in CACHE.all_for_kind('crypto')
                    if c.snapshot.get('change_24h_pct') is not None]

        if not rows:
            await interaction.followup.send(
                'Crypto data not ready yet. Try again in a minute.', ephemeral=True,
            )
            return

        rows.sort(
            key=lambda s: float(s.get('change_24h_pct') or 0),
            reverse=gainers,
        )
        top = rows[:10]

        title = '🚀 Top 10 24h gainers' if gainers else '📉 Top 10 24h losers'
        lines = []
        for s in top:
            sym = (s.get('symbol_display') or s.get('identifier') or '').upper()
            price = _fmt_price(s.get('price_usd'))
            ch24 = s.get('change_24h_pct') or 0
            lines.append(f'`{sym:<6}` {price:<13} {ch24:+.2f}%')

        gid = interaction.guild_id or 0
        e = build_branded_embed(
            int(gid),
            title=title,
            description='\n'.join(lines)[:4000],
            cog_prefix='',
            use_thumbnail=True,
            use_image=False,
            use_footer=True,
        )
        await interaction.followup.send(embed=e, ephemeral=True)

    # ── /watchlist ────────────────────────────────────────────────────────
    @app_commands.command(name='watchlist', description='Show this server\'s Radar watchlist')
    async def watchlist_cmd(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.HTTPException:
            pass
        gid = interaction.guild_id or 0
        rows = list_radar_watchlist(gid)
        if not rows:
            await interaction.followup.send(
                'This server has no Radar watchlist yet. An admin can use '
                '`/radar add` or the dashboard to start tracking.',
                ephemeral=True,
            )
            return

        # Group by asset_kind so the listing reads naturally.
        by_kind: dict[str, list[dict]] = {}
        for r in rows:
            by_kind.setdefault(r.get('asset_kind') or 'unknown', []).append(r)

        sections: list[str] = []
        for kind, items in by_kind.items():
            lines = []
            for r in items:
                ident = r.get('asset_identifier') or '?'
                name  = r.get('display_name') or ident
                # Crypto cache keys are lowercased; meme keys match the saved
                # 'chain:address' identifier exactly.
                if kind == 'crypto':
                    snap = CACHE.get_snapshot('crypto', ident.lower())
                elif kind == 'meme':
                    snap = CACHE.get_snapshot('meme', ident)
                else:
                    snap = None
                price = _fmt_price(snap.get('price_usd')) if snap else '—'
                ch24  = (f'{snap["change_24h_pct"]:+.2f}%'
                         if snap and snap.get('change_24h_pct') is not None else '—')
                if kind == 'meme':
                    chain = ((snap.get('raw', {}) or {}).get('chain') if snap else '') \
                        or chain_from_identifier(ident)
                    lines.append(f'{chain_badge(chain)} `{name[:12]:<12}` {price:<13} {ch24}')
                else:
                    lines.append(f'`{name[:18]:<18}` {price:<13} {ch24}')
            sections.append(f'**{kind.capitalize()}**\n' + '\n'.join(lines))

        e = build_branded_embed(
            int(gid),
            title='📡 Radar watchlist',
            description='\n\n'.join(sections)[:4000],
            cog_prefix='',
            use_thumbnail=True,
            use_image=False,
            use_footer=True,
        )
        await interaction.followup.send(embed=e, ephemeral=True)

    # ── /radar add / remove (admin only) ──────────────────────────────────
    @radar_group.command(name='add', description='Add an asset to the Radar watchlist (admin)')
    @app_commands.describe(
        kind='Asset kind',
        identifier='CoinGecko id (crypto), e.g. bitcoin / ethereum / solana',
        display_name='Optional pretty name to show in lists',
    )
    @app_commands.choices(kind=_KIND_CHOICES)
    async def radar_add(
        self, interaction: discord.Interaction,
        kind: app_commands.Choice[str],
        identifier: str,
        display_name: str = '',
    ):
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.HTTPException:
            pass
        if not interaction.user.guild_permissions.administrator:
            await interaction.followup.send('Admin only.', ephemeral=True)
            return

        kind_val = kind.value
        ident = (identifier or '').strip().lower()
        if not ident:
            await interaction.followup.send('Identifier is required.', ephemeral=True)
            return

        if kind_val != 'crypto' and kind_val not in SUPPORTED_KINDS_PHASE_1:
            await interaction.followup.send(
                f'Watchlist entries for **{kind_val}** are accepted, but live data '
                'for that topic ships in a later phase. The entry will be stored and '
                'will activate as soon as the adapter is live.',
                ephemeral=True,
            )

        if kind_val == 'crypto':
            # Resolve the id via CoinGecko so we store a real id, not a typo.
            adapter = ADAPTERS_BY_KIND.get('crypto')
            snap    = None
            if adapter:
                try:
                    snap = await adapter.fetch_one(ident)
                    if not snap:
                        suggestions = await adapter.search(ident, limit=1)
                        if suggestions:
                            ident = suggestions[0].get('identifier') or ident
                            snap  = await adapter.fetch_one(ident)
                except Exception as e:  # noqa: BLE001
                    print(f'[radar/add] resolve failed: {type(e).__name__}: {e}')
            if snap:
                CACHE.put('crypto', snap['identifier'], snap)
                if not display_name:
                    display_name = (snap.get('symbol_display')
                                    or snap.get('raw', {}).get('name')
                                    or ident).upper()

        gid = interaction.guild_id or 0
        try:
            row_id = add_radar_watchlist_entry(
                gid, kind_val, ident,
                display_name=display_name or ident,
                added_by=interaction.user.id,
            )
        except sqlite3.IntegrityError:
            await interaction.followup.send(
                f'**{ident}** is already on the {kind_val} watchlist.', ephemeral=True,
            )
            return
        except Exception as e:  # noqa: BLE001
            print(f'[radar/add] insert failed: {type(e).__name__}: {e}')
            await interaction.followup.send(
                'Could not save the watchlist entry. Try again shortly.',
                ephemeral=True,
            )
            return

        log_event(
            gid, 'admin_action', 'radar_watchlist_added',
            f'Radar watchlist: {kind_val} {ident} added by {interaction.user}',
            actor_user_id=interaction.user.id,
            actor_username=str(interaction.user),
            module='radar', severity='info',
            details={'entry_id': row_id, 'kind': kind_val, 'identifier': ident},
        )
        await interaction.followup.send(
            f'Added **{display_name or ident}** ({kind_val}) to the Radar watchlist.',
            ephemeral=True,
        )

    @radar_group.command(name='remove', description='Remove an asset from the Radar watchlist (admin)')
    @app_commands.describe(
        kind='Asset kind',
        identifier='Same identifier you added',
    )
    @app_commands.choices(kind=_KIND_CHOICES)
    async def radar_remove(
        self, interaction: discord.Interaction,
        kind: app_commands.Choice[str], identifier: str,
    ):
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.HTTPException:
            pass
        if not interaction.user.guild_permissions.administrator:
            await interaction.followup.send('Admin only.', ephemeral=True)
            return
        gid = interaction.guild_id or 0
        ident = (identifier or '').strip().lower()
        ok = remove_radar_watchlist_by_identifier(gid, kind.value, ident)
        if not ok:
            await interaction.followup.send(
                f'**{ident}** is not on the {kind.value} watchlist.', ephemeral=True,
            )
            return
        log_event(
            gid, 'admin_action', 'radar_watchlist_removed',
            f'Radar watchlist: {kind.value} {ident} removed by {interaction.user}',
            actor_user_id=interaction.user.id,
            actor_username=str(interaction.user),
            module='radar', severity='info',
            details={'kind': kind.value, 'identifier': ident},
        )
        await interaction.followup.send(
            f'Removed **{ident}** from the {kind.value} watchlist.', ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Radar(bot))
