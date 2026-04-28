from shared_bot import bot
from database import init_db, ensure_guild_defaults
import os

init_db()

@bot.event
async def setup_hook():
    await bot.load_extension('cogs.verify')
    await bot.load_extension('cogs.roleselect')
    await bot.load_extension('cogs.creatorticket')
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

if __name__ == '__main__':
    bot.run(os.getenv('DISCORD_TOKEN'))
