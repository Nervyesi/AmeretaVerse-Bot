"""
run.py — Start the Discord bot and FastAPI server together in one process.

Usage:  python run.py
Both share the same asyncio event loop, so shared_bot.bot is live for the API.
"""
import asyncio
import os

import uvicorn
from dotenv import load_dotenv

from database import init_db
from shared_bot import bot

load_dotenv()


async def main():
    init_db()

    # Register bot events and load cogs
    import bot as _bot_module  # noqa: F401  — side-effectful import

    api_config = uvicorn.Config(
        'api:app',
        host='0.0.0.0',
        port=int(os.getenv('API_PORT', '8000')),
        loop='asyncio',
        log_level='info',
    )
    api_server = uvicorn.Server(api_config)

    token = os.getenv('DISCORD_TOKEN')
    if not token:
        raise RuntimeError('DISCORD_TOKEN is not set in .env')

    print('Starting AVbot + API server...')
    await asyncio.gather(
        bot.start(token),
        api_server.serve(),
    )


if __name__ == '__main__':
    asyncio.run(main())
