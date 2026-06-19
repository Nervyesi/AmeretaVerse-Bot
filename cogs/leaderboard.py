"""Unified /leaderboard command.

Combines each member's raid (community) points and engage points into one
per-guild ranking. Points are read live from the existing tables via
database.get_unified_points, so spending engage points naturally lowers the
combined total on the next read. The standalone /raid leaderboard and
/engage-leaderboard commands are untouched and stay available alongside this.

Visual style mirrors the existing /raid leaderboard and /engage-leaderboard:
medal/`n.` rank prefix, bold name, backtick "pts", em rule, branded embed. The
only addition is the raid + engage breakdown in parentheses, in the same shape
/engage-leaderboard already uses for its "(N engages)" suffix.
"""

import math

import discord
from discord import app_commands
from discord.ext import commands

from database import get_unified_points, log_event
from cogs._branding import build_branded_embed

_NO_PING = discord.AllowedMentions(roles=False, users=False, everyone=False)
_MEDALS = ['🥇', '🥈', '🥉']

PAGE_SIZE = 10
MAX_RANKS = 100  # top 100 ranks => 10 pages of 10


# ── Rendering ─────────────────────────────────────────────────────────────────

def _row(global_rank: int, entry: dict) -> str:
    """One leaderboard line, identical to the /raid + /engage row format.
    The raid/engage split lives in the title and in Find Me, not per row."""
    rank = _MEDALS[global_rank] if global_rank < 3 else f'`{global_rank + 1}.`'
    name = entry.get('username') or f'<@{entry["user_id"]}>'
    return f'{rank} **{name}** — `{entry["total"]:,} pts`'


def _build_leaderboard_embed(guild_id, combined, page) -> discord.Embed:
    start = page * PAGE_SIZE
    chunk = combined[start:start + PAGE_SIZE]
    lines = [_row(start + i, e) for i, e in enumerate(chunk)]
    return build_branded_embed(
        guild_id,
        title='🏆 AVbot Leaderboard (raid + engage)',
        description='\n'.join(lines),
        use_thumbnail=True,
        use_image=False,
        use_footer=True,
    )


def _position(sorted_list, uid):
    for idx, e in enumerate(sorted_list):
        if e['user_id'] == uid:
            return idx + 1
    return None


def _build_find_me_embed(guild_id, all_data, uid) -> discord.Embed:
    combined = sorted([d for d in all_data if d['total'] > 0],  key=lambda d: (-d['total'],  d['user_id']))
    engage   = sorted([d for d in all_data if d['engage'] > 0], key=lambda d: (-d['engage'], d['user_id']))
    raid     = sorted([d for d in all_data if d['raid'] > 0],   key=lambda d: (-d['raid'],   d['user_id']))
    me = next((d for d in all_data if d['user_id'] == uid), None)

    def block(label, sorted_list, value):
        pos = _position(sorted_list, uid)
        if me and value > 0 and pos:
            return f'**{label}**\nRank #{pos} of {len(sorted_list)} — {value:,} pts'
        return f'**{label}**\nNot ranked yet — 0 pts'

    desc = '\n\n'.join([
        block('Combined', combined, me['total'] if me else 0),
        block('Engage', engage, me['engage'] if me else 0),
        block('Raid', raid, me['raid'] if me else 0),
    ])
    return build_branded_embed(
        guild_id,
        title='Your Position',
        description=desc,
        use_thumbnail=True,
        use_image=False,
        use_footer=True,
    )


# ── Paginated view (locked to the caller, 10 minute timeout) ──────────────────

class LeaderboardView(discord.ui.View):
    def __init__(self, *, guild_id, combined, all_data, original_user_id, total_pages):
        super().__init__(timeout=600)
        self.guild_id = guild_id
        self.combined = combined
        self.all_data = all_data
        self.original_user_id = original_user_id
        self.total_pages = total_pages
        self.page = 0
        self.message = None

        self._prev = discord.ui.Button(label='◀ Previous', style=discord.ButtonStyle.secondary)
        self._prev.callback = self._prev_cb
        self.add_item(self._prev)

        self._next = discord.ui.Button(label='Next ▶', style=discord.ButtonStyle.secondary)
        self._next.callback = self._next_cb
        self.add_item(self._next)

        self._find = discord.ui.Button(label='Find me', style=discord.ButtonStyle.primary)
        self._find.callback = self._find_cb
        self.add_item(self._find)

        self._sync()

    def _sync(self):
        self._prev.disabled = self.page <= 0
        self._next.disabled = self.page >= self.total_pages - 1

    async def _guard(self, interaction) -> bool:
        if interaction.user.id != self.original_user_id:
            await interaction.response.send_message(
                'This leaderboard is not yours to page. Run `/leaderboard` to open your own.',
                ephemeral=True,
            )
            return False
        return True

    async def _render(self, interaction):
        self._sync()
        embed = _build_leaderboard_embed(self.guild_id, self.combined, self.page)
        await interaction.response.edit_message(embed=embed, view=self, allowed_mentions=_NO_PING)

    async def _prev_cb(self, interaction):
        if not await self._guard(interaction):
            return
        self.page = max(0, self.page - 1)
        await self._render(interaction)

    async def _next_cb(self, interaction):
        if not await self._guard(interaction):
            return
        self.page = min(self.total_pages - 1, self.page + 1)
        await self._render(interaction)

    async def _find_cb(self, interaction):
        if not await self._guard(interaction):
            return
        embed = _build_find_me_embed(self.guild_id, self.all_data, interaction.user.id)
        await interaction.response.send_message(embed=embed, ephemeral=True, allowed_mentions=_NO_PING)

    async def on_timeout(self):
        self._prev.disabled = True
        self._next.disabled = True
        self._find.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass


# ── Cog ───────────────────────────────────────────────────────────────────────

class LeaderboardCog(commands.Cog, name='Leaderboard'):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(
        name='leaderboard',
        description='Combined raid and engage leaderboard for this server.',
    )
    async def leaderboard_cmd(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message('Use this command inside a server.', ephemeral=True)
            return

        guild_id = guild.id
        data = get_unified_points(guild_id)
        combined = sorted(
            [d for d in data if d['total'] > 0],
            key=lambda d: (-d['total'], d['user_id']),
        )[:MAX_RANKS]

        log_event(
            guild_id, 'bot_activity', 'leaderboard_view',
            f'{interaction.user} viewed the unified leaderboard',
            actor_user_id=interaction.user.id,
            actor_username=str(interaction.user),
            module='leaderboard',
        )

        if not combined:
            await interaction.response.send_message(
                'No leaderboard data yet. Join a raid or an engage pool to get on the board. 🏆',
                allowed_mentions=_NO_PING,
            )
            return

        total_pages = max(1, math.ceil(len(combined) / PAGE_SIZE))
        view = LeaderboardView(
            guild_id=guild_id, combined=combined, all_data=data,
            original_user_id=interaction.user.id, total_pages=total_pages,
        )
        embed = _build_leaderboard_embed(guild_id, combined, 0)
        await interaction.response.send_message(embed=embed, view=view, allowed_mentions=_NO_PING)
        try:
            view.message = await interaction.original_response()
        except Exception:
            view.message = None


async def setup(bot):
    await bot.add_cog(LeaderboardCog(bot))
