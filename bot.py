from shared_bot import bot
from database import init_db, ensure_guild_defaults
import os

init_db()

@bot.event
async def setup_hook():
    await bot.load_extension('cogs.verify')
    await bot.load_extension('cogs.roleselect')
    await bot.load_extension('cogs.forms')
    await bot.load_extension('cogs.raidbot')
    await bot.load_extension('cogs.engage')
    await bot.load_extension('cogs.protection')
    await bot.load_extension('cogs.analytics')
    await bot.load_extension('cogs.voice_tracker')
    await bot.load_extension('cogs.giveaway')
    await bot.load_extension('cogs.tickets')
    await bot.load_extension('cogs.levels')
    await bot.load_extension('cogs.backup')
    await bot.load_extension('cogs.radar')
    await bot.tree.sync()
    print('Cogs loaded and command tree synced.')

@bot.event
async def on_ready():
    for guild in bot.guilds:
        ensure_guild_defaults(guild.id)
    print(f'Bot is online: {bot.user}')

    backup_cog = bot.get_cog('Backup')
    if backup_cog and backup_cog.weekly_backup_task.is_running():
        print('[startup] Weekly R2 DB backup task is running.')
    else:
        print('[startup] WARNING: Weekly R2 DB backup task is NOT running.')

    # Radar background tasks. Phase 1: fetcher (chains alerts dispatcher
    # after each tick) + digest scheduler. Each loop is self-guarding —
    # adapter errors are caught inside the loop, so a bad upstream never
    # crashes the bot. Started exactly once per process.
    import asyncio as _asyncio
    if not getattr(bot, '_radar_tasks_started', False):
        bot._radar_tasks_started = True
        try:
            from services.radar.fetcher import fetch_loop
            from services.radar.digest  import scheduler_loop
            from services.radar.discovery_meme import discovery_meme_loop
            from services.radar.discovery_nft  import discovery_nft_loop
            _asyncio.create_task(fetch_loop(bot),         name='radar.fetcher')
            _asyncio.create_task(scheduler_loop(bot),     name='radar.digest')
            _asyncio.create_task(discovery_meme_loop(bot), name='radar.discovery.meme')
            _asyncio.create_task(discovery_nft_loop(bot),  name='radar.discovery.nft')
            print('[startup] Radar background loops started '
                  '(fetcher + digest + meme discovery + nft discovery)')
        except Exception as e:
            import traceback as _tb
            print(f'[startup] Radar tasks failed to start: {type(e).__name__}: {e}')
            _tb.print_exc()

    print('[startup] Checking TwitterAPI.io configuration...')
    try:
        from cogs._twitter import API_KEY, lookup_twitter_user_by_login
        if API_KEY:
            print(f'[startup] TwitterAPI.io key present (length={len(API_KEY)})')
            test = await lookup_twitter_user_by_login('twitter')
            if test and test.get('username'):
                print(f'[startup] API TEST OK: got @{test["username"]} (id={test.get("id", "?")})')
            else:
                from cogs._twitter import SCRAPING_HEALTHY, _consecutive_failures
                print(f'[startup] API TEST returned None — healthy={SCRAPING_HEALTHY} failures={_consecutive_failures}')
                print('[startup] Check TWITTER_API_IO_KEY is valid at https://twitterapi.io')
        else:
            print('[startup] TWITTER_API_IO_KEY not set — verification disabled')
    except Exception as e:
        import traceback
        print(f'[startup] TwitterAPI.io check failed: {type(e).__name__}: {e}')
        traceback.print_exc()

if __name__ == '__main__':
    bot.run(os.getenv('DISCORD_TOKEN'))
