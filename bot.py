import discord
from discord.ext import commands
from dotenv import load_dotenv
import os

load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.presences = True

bot = commands.Bot(command_prefix='!', intents=intents)

async def load_cogs():
    await bot.load_extension('cogs.verify')
    await bot.load_extension('cogs.Maineroles')
    await bot.load_extension('cogs.creatorticket')

@bot.event
async def on_ready():
    await load_cogs()
    print(f'Bot is online: {bot.user}')

bot.run(TOKEN)