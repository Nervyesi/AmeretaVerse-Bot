"""
levels.py — Activity-based XP / leveling.

Listens to on_message and grants xp_per_message XP per non-bot message, with a
per-(guild,user) cooldown. Posts a level-up announcement to a configured channel
(or the message channel) when a user crosses a level threshold. /levels shows
the top-10 board.

All knobs live on guild_settings:
  level_enabled, xp_per_message, xp_cooldown_seconds,
  level_up_message_enabled, level_up_channel_id
"""
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from database import (
    get_guild_settings,
    grant_xp,
    get_xp_leaderboard,
)
from cogs._branding import build_branded_embed

# In-memory cooldown — survives bot restart loss is acceptable for XP throttling.
_xp_cooldown: dict[tuple[int, int], datetime] = {}


class LevelsCog(commands.Cog, name='Levels'):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        guild_id = message.guild.id
        user_id  = message.author.id

        settings = get_guild_settings(guild_id) or {}
        if not int(settings.get('level_enabled', 1) or 0):
            return

        cooldown  = int(settings.get('xp_cooldown_seconds', 60) or 60)
        xp_amount = int(settings.get('xp_per_message', 15) or 15)
        if xp_amount <= 0:
            return

        now  = datetime.now(timezone.utc)
        last = _xp_cooldown.get((guild_id, user_id))
        if last and (now - last).total_seconds() < cooldown:
            return
        _xp_cooldown[(guild_id, user_id)] = now

        _new_xp, new_level, leveled_up = grant_xp(guild_id, user_id, xp_amount)

        if leveled_up and int(settings.get('level_up_message_enabled', 1) or 0):
            channel = None
            channel_id = settings.get('level_up_channel_id')
            if channel_id:
                try:
                    channel = message.guild.get_channel(int(channel_id))
                except (TypeError, ValueError):
                    channel = None
            channel = channel or message.channel
            try:
                await channel.send(
                    f'🎉 {message.author.mention} reached **Level {new_level}**!'
                )
            except discord.Forbidden:
                pass
            except Exception as e:
                print(f'[levels] level-up notify failed: {type(e).__name__}: {e}')

    @app_commands.command(name='levels', description='Show the top members by level and XP.')
    async def levels_cmd(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id or 0
        rows = get_xp_leaderboard(guild_id, limit=10)
        if not rows:
            await interaction.response.send_message(
                'No activity yet — start chatting to earn XP.', ephemeral=True,
            )
            return

        medals = ['🥇', '🥈', '🥉']
        lines  = []
        for i, r in enumerate(rows):
            rank = medals[i] if i < 3 else f'`{i + 1}.`'
            lines.append(
                f'{rank} <@{r["user_id"]}> — Level **{r["level"]}** ({r["xp"]} XP)'
            )

        embed = build_branded_embed(
            guild_id,
            title='Top Active Members',
            description='\n'.join(lines),
            cog_prefix='levels',
            use_thumbnail=True, use_footer=True,
        )
        await interaction.response.send_message(embed=embed)


async def setup(bot):
    await bot.add_cog(LevelsCog(bot))
