"""
voice_tracker.py — records voice channel sessions into voice_sessions.

One row per session. The row is opened when a member joins voice (or moves
into voice from disconnect), updated to track AFK/self-mute/self-deaf flips,
and closed when the member leaves voice (or moves to a new channel — close
old, open new).

Bot restart safety: on_ready sweeps any sessions left open by a crash and
closes them with left_at=NOW (duration measured from the original joined_at
up to NOW). This is conservative — better to over-credit a few minutes once
per restart than to throw out the entire session.
"""
from datetime import datetime, timezone

import discord
from discord.ext import commands

from database import get_connection


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _open_session(
    guild_id: int, user_id: int, channel_id: int,
    *, afk: bool = False, self_mute: bool = False, self_deaf: bool = False,
) -> None:
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO voice_sessions
               (guild_id, user_id, channel_id, joined_at, left_at,
                duration_seconds, afk, self_mute, self_deaf)
               VALUES (?, ?, ?, ?, NULL, 0, ?, ?, ?)""",
            (
                str(guild_id), str(user_id), str(channel_id), _now_iso(),
                1 if afk else 0, 1 if self_mute else 0, 1 if self_deaf else 0,
            ),
        )


def _close_open_sessions(
    guild_id: int, user_id: int,
) -> int:
    """Close every open session for this (guild, user) and return how many were
    closed. Uses julianday math so the duration is computed from the row's
    joined_at, not from any external clock — restart-safe."""
    with get_connection() as conn:
        cur = conn.execute(
            """UPDATE voice_sessions
                  SET left_at = ?,
                      duration_seconds = CAST(
                          (julianday(?) - julianday(joined_at)) * 86400 AS INTEGER
                      )
                WHERE guild_id = ? AND user_id = ? AND left_at IS NULL""",
            (_now_iso(), _now_iso(), str(guild_id), str(user_id)),
        )
        return cur.rowcount or 0


def _update_open_flags(
    guild_id: int, user_id: int,
    *, afk: bool, self_mute: bool, self_deaf: bool,
) -> None:
    """Best-effort flag refresh on the still-open session. Multiple toggles
    inside one session collapse to "ever true" for analytics purposes."""
    with get_connection() as conn:
        conn.execute(
            """UPDATE voice_sessions
                  SET afk       = MAX(afk,       ?),
                      self_mute = MAX(self_mute, ?),
                      self_deaf = MAX(self_deaf, ?)
                WHERE guild_id = ? AND user_id = ? AND left_at IS NULL""",
            (
                1 if afk else 0, 1 if self_mute else 0, 1 if self_deaf else 0,
                str(guild_id), str(user_id),
            ),
        )


class VoiceTracker(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self):
        # Close every session that was left open by the previous bot process.
        # We can't recover the true leave time so we close with NOW — a small
        # over-count once per restart is far better than dropping the session.
        with get_connection() as conn:
            cur = conn.execute(
                """UPDATE voice_sessions
                      SET left_at = ?,
                          duration_seconds = CAST(
                              (julianday(?) - julianday(joined_at)) * 86400 AS INTEGER
                          )
                    WHERE left_at IS NULL""",
                (_now_iso(), _now_iso()),
            )
            n = cur.rowcount or 0
        if n:
            print(f'[voice] startup sweep closed {n} sessions left open by previous run')

        # Open sessions for everyone currently in voice (so a restart while
        # people are in voice still credits the post-restart minutes).
        opened = 0
        for guild in self.bot.guilds:
            for vc in guild.voice_channels:
                afk_id = guild.afk_channel.id if guild.afk_channel else None
                is_afk = (vc.id == afk_id)
                for m in vc.members:
                    if m.bot:
                        continue
                    _open_session(
                        guild.id, m.id, vc.id,
                        afk=is_afk,
                        self_mute=bool(m.voice and m.voice.self_mute),
                        self_deaf=bool(m.voice and m.voice.self_deaf),
                    )
                    opened += 1
        if opened:
            print(f'[voice] startup sweep opened {opened} ongoing sessions')

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        # Bots' voice transitions are not user activity.
        if member.bot or not member.guild:
            return

        guild = member.guild
        afk_id = guild.afk_channel.id if guild.afk_channel else None

        before_ch = before.channel.id if before.channel else None
        after_ch  = after.channel.id  if after.channel  else None

        if before_ch is None and after_ch is not None:
            # JOIN
            _open_session(
                guild.id, member.id, after_ch,
                afk=(after_ch == afk_id),
                self_mute=bool(after.self_mute),
                self_deaf=bool(after.self_deaf),
            )
            return

        if before_ch is not None and after_ch is None:
            # LEAVE / disconnect
            _close_open_sessions(guild.id, member.id)
            return

        if before_ch is not None and after_ch is not None and before_ch != after_ch:
            # MOVE — end old session, start new one in the new channel.
            _close_open_sessions(guild.id, member.id)
            _open_session(
                guild.id, member.id, after_ch,
                afk=(after_ch == afk_id),
                self_mute=bool(after.self_mute),
                self_deaf=bool(after.self_deaf),
            )
            return

        # Same channel — just a mute/deaf/AFK flag flip. Update the open row.
        if before_ch is not None and after_ch is not None and before_ch == after_ch:
            _update_open_flags(
                guild.id, member.id,
                afk=(after_ch == afk_id),
                self_mute=bool(after.self_mute),
                self_deaf=bool(after.self_deaf),
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(VoiceTracker(bot))
