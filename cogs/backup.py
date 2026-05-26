"""
cogs/backup.py — Automatic weekly DB backup to R2.

Mirrors the task-loop pattern used by raidbot's hourly_auto_end_task: the loop
is started in __init__, waits for the bot to be ready, then runs on its interval.
Every backup is wrapped in try/except and logged via log_event, so a failure can
never crash the bot.
"""
import asyncio

from discord.ext import commands, tasks

from database import log_event
from backup_service import upload_backup_to_r2

# Owner's home guild — used purely for log_event attribution (backups are a
# global, cross-guild operation, not tied to any one server).
_OWNER_GUILD_ID = 1199707792706117642


class BackupCog(commands.Cog, name='Backup'):
    def __init__(self, bot):
        self.bot = bot
        self.weekly_backup_task.start()
        print('[backup] weekly R2 backup task started (interval=168h)')

    def cog_unload(self):
        self.weekly_backup_task.cancel()

    @tasks.loop(hours=168)  # weekly
    async def weekly_backup_task(self):
        await self.run_backup(trigger='weekly')

    @weekly_backup_task.before_loop
    async def before_weekly_backup(self):
        await self.bot.wait_until_ready()

    async def run_backup(self, trigger: str) -> dict | None:
        """Make a consistent copy and upload it to R2. Returns the result dict on
        success or None on failure. Never raises."""
        try:
            # Run the blocking sqlite3 backup + boto3 upload off the event loop.
            result = await asyncio.to_thread(upload_backup_to_r2)
            print(f'[backup] {trigger} R2 backup OK: {result["key"]} '
                  f'({result["size"]} bytes, pruned {len(result["pruned"])})')
            log_event(
                _OWNER_GUILD_ID, 'admin_action', 'db_backup_success',
                f'DB backup uploaded to R2: {result["key"]}',
                module='backup', severity='info',
                details={'trigger': trigger, **result},
            )
            return result
        except Exception as e:
            print(f'[backup] {trigger} R2 backup FAILED: {type(e).__name__}: {e}')
            log_event(
                _OWNER_GUILD_ID, 'admin_action', 'db_backup_failed',
                f'DB backup to R2 failed: {type(e).__name__}: {e}',
                module='backup', severity='error',
                details={'trigger': trigger, 'error': f'{type(e).__name__}: {e}'},
            )
            return None


async def setup(bot):
    await bot.add_cog(BackupCog(bot))
