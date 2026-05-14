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
    await bot.load_extension('cogs.tickets')
    await bot.tree.sync()
    print('Cogs loaded and command tree synced.')

@bot.event
async def on_ready():
    for guild in bot.guilds:
        ensure_guild_defaults(guild.id)
    print(f'Bot is online: {bot.user}')

    print('[startup] Checking Apify configuration...')
    try:
        from cogs._twitter import APIFY_TOKEN, APIFY_ACTOR, lookup_twitter_user_by_login
        if APIFY_TOKEN:
            print(f'[startup] Apify token present, actor={APIFY_ACTOR}')
            test = await lookup_twitter_user_by_login('twitter')
            if test and test.get('username'):
                print(f'[startup] Apify TEST OK: got real user @{test["username"]} (id={test.get("id", "?")})')
            else:
                from cogs._twitter import SCRAPING_HEALTHY, _consecutive_failures
                print(f'[startup] Apify TEST returned None — verification will be inconclusive (healthy={SCRAPING_HEALTHY})')
                print('[startup] Possible causes: demo mode, plan limit, or actor permissions')
                print('[startup] Check: https://console.apify.com — verify plan supports the actor')
        else:
            print('[startup] APIFY_TOKEN not configured — verification disabled')
    except Exception as e:
        import traceback
        print(f'[startup] Apify check failed: {type(e).__name__}: {e}')
        traceback.print_exc()

if __name__ == '__main__':
    bot.run(os.getenv('DISCORD_TOKEN'))
