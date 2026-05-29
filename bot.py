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
    await bot.load_extension('cogs.tickets')
    await bot.load_extension('cogs.levels')
    await bot.load_extension('cogs.backup')
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
