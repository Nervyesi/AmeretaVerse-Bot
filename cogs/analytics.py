import discord
from discord.ext import commands, tasks
from datetime import datetime, date, timedelta, timezone

from database import get_connection


class Analytics(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.daily_snapshot.start()

    def cog_unload(self):
        self.daily_snapshot.cancel()

    # ── Intraday counters ─────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or not message.guild:
            return
        today = date.today().isoformat()
        with get_connection() as conn:
            conn.execute(
                """INSERT INTO message_counters (guild_id, date, message_count, joins, leaves)
                   VALUES (?, ?, 1, 0, 0)
                   ON CONFLICT(guild_id, date)
                   DO UPDATE SET message_count = message_count + 1""",
                (message.guild.id, today),
            )

    @commands.Cog.listener()
    async def on_member_join(self, member):
        today = date.today().isoformat()
        with get_connection() as conn:
            conn.execute(
                """INSERT INTO message_counters (guild_id, date, message_count, joins, leaves)
                   VALUES (?, ?, 0, 1, 0)
                   ON CONFLICT(guild_id, date)
                   DO UPDATE SET joins = joins + 1""",
                (member.guild.id, today),
            )

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        today = date.today().isoformat()
        with get_connection() as conn:
            conn.execute(
                """INSERT INTO message_counters (guild_id, date, message_count, joins, leaves)
                   VALUES (?, ?, 0, 0, 1)
                   ON CONFLICT(guild_id, date)
                   DO UPDATE SET leaves = leaves + 1""",
                (member.guild.id, today),
            )

    # ── Backfill on bot join to new guild ─────────────────────────────────────

    @commands.Cog.listener()
    async def on_guild_join(self, guild):
        await self._backfill_history(guild)

    # ── Backfill on startup for guilds with no snapshots ─────────────────────

    @commands.Cog.listener()
    async def on_ready(self):
        for guild in self.bot.guilds:
            with get_connection() as conn:
                count = conn.execute(
                    "SELECT COUNT(*) FROM analytics_snapshots WHERE guild_id=?",
                    (guild.id,)
                ).fetchone()[0]
            if count == 0:
                await self._backfill_history(guild)

    # ── Backfill: reconstruct 365 days of member growth from joined_at ────────

    async def _backfill_history(self, guild: discord.Guild):
        today_date = date.today()

        members = sorted(
            [m for m in guild.members if m.joined_at is not None],
            key=lambda m: m.joined_at,
        )

        verified_role = discord.utils.get(guild.roles, name='Verified')
        verified_ids = {m.id for m in verified_role.members} if verified_role else set()

        batch = []
        rows_inserted = 0

        for i in range(364, -1, -1):
            d = today_date - timedelta(days=i)
            d_str = d.isoformat()

            day_start = datetime.combine(d, datetime.min.time()).replace(tzinfo=timezone.utc)
            day_end   = datetime.combine(d + timedelta(days=1), datetime.min.time()).replace(tzinfo=timezone.utc)

            if i == 0:
                # Today: use live values
                member_count  = guild.member_count
                online_count  = sum(1 for m in guild.members if str(m.status) != 'offline')
                verified_count = len(verified_role.members) if verified_role else 0
            else:
                member_count  = sum(1 for m in members if m.joined_at <= day_end)
                online_count  = 0
                verified_count = sum(
                    1 for m in members
                    if m.id in verified_ids and m.joined_at <= day_end
                )

            joins_24h = sum(
                1 for m in members
                if day_start < m.joined_at <= day_end
            )

            batch.append((
                guild.id, d_str, member_count, online_count,
                verified_count, 0, joins_24h, 0,
            ))

            if len(batch) >= 100:
                with get_connection() as conn:
                    conn.executemany(
                        """INSERT OR IGNORE INTO analytics_snapshots
                           (guild_id, snapshot_date, member_count, online_count,
                            verified_count, message_count_24h, joins_24h, leaves_24h)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        batch,
                    )
                rows_inserted += len(batch)
                batch = []

        if batch:
            with get_connection() as conn:
                conn.executemany(
                    """INSERT OR IGNORE INTO analytics_snapshots
                       (guild_id, snapshot_date, member_count, online_count,
                        verified_count, message_count_24h, joins_24h, leaves_24h)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    batch,
                )
            rows_inserted += len(batch)

        print(f"Backfilled {rows_inserted} days for guild {guild.name} ({guild.id})")

    # ── Daily snapshot (fires at midnight UTC) ────────────────────────────────

    @tasks.loop(hours=24)
    async def daily_snapshot(self):
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        cutoff    = (date.today() - timedelta(days=3)).isoformat()

        for guild in self.bot.guilds:
            guild_id = guild.id

            member_count = guild.member_count
            online_count = sum(
                1 for m in guild.members if str(m.status) != 'offline'
            )

            verified_role  = discord.utils.get(guild.roles, name='Verified')
            verified_count = len(verified_role.members) if verified_role else 0

            with get_connection() as conn:
                row = conn.execute(
                    """SELECT message_count, joins, leaves
                       FROM message_counters WHERE guild_id=? AND date=?""",
                    (guild_id, yesterday),
                ).fetchone()

                msg_count  = row['message_count'] if row else 0
                joins_cnt  = row['joins']         if row else 0
                leaves_cnt = row['leaves']        if row else 0

                conn.execute(
                    """INSERT OR REPLACE INTO analytics_snapshots
                       (guild_id, snapshot_date, member_count, online_count,
                        verified_count, message_count_24h, joins_24h, leaves_24h)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (guild_id, yesterday, member_count, online_count,
                     verified_count, msg_count, joins_cnt, leaves_cnt),
                )

                conn.execute(
                    "DELETE FROM message_counters WHERE guild_id=? AND date < ?",
                    (guild_id, cutoff),
                )

    @daily_snapshot.before_loop
    async def before_daily_snapshot(self):
        await self.bot.wait_until_ready()
        now = datetime.now(timezone.utc)
        tomorrow_midnight = (
            now.replace(hour=0, minute=0, second=0, microsecond=0)
            + timedelta(days=1)
        )
        await discord.utils.sleep_until(tomorrow_midnight)


async def setup(bot):
    await bot.add_cog(Analytics(bot))
