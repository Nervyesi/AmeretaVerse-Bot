"""
shared_bot.py — single bot instance shared between bot.py and api.py.
Import `bot` from here; never create a second commands.Bot().
"""
import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.presences = True

bot = commands.Bot(command_prefix='!', intents=intents)
