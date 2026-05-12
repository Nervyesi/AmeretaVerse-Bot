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

    print('[startup] Initializing Twitter API pool...')
    try:
        from cogs._twitter import get_api
        await get_api()
        print('[startup] Twitter API pool initialization complete')
    except Exception as e:
        import traceback
        print(f'[startup] Twitter API init FAILED: {type(e).__name__}: {e}')
        traceback.print_exc()

if __name__ == '__main__':
    bot.run(os.getenv('DISCORD_TOKEN'))
