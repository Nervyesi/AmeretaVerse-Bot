# AVbot

AVbot is a Discord bot built for Web3 communities. It bundles fourteen modules into one bot: verification, role selection, forms, tickets, X (Twitter) engagement, raid coordination, giveaways with role based ticket multipliers, multi chain wallet collection, Web3 market intelligence (Radar), protection (anti spam, anti raid, anti scam), analytics, embed messages, logs, and server settings.

Website: https://www.avbot.app
Documentation: https://www.avbot.app/docs

## What this repository is

This is the Python backend: the Discord bot (`discord.py` cogs), the FastAPI service that powers the dashboard and public endpoints, and the SQLite data layer. The marketing site and dashboard frontend live in a separate repository.

The source is published for transparency. See the License section below before reusing it.

## Tech stack

- Python 3.13
- `discord.py` for the bot
- FastAPI plus Uvicorn for the API
- SQLite for storage

## Self hosting

You will need your own Discord application and bot token.

1. Clone the repository and install dependencies:

   ```
   pip install -r requirements.txt
   ```

2. Copy `.env.example` to `.env` and fill in your own values:

   ```
   cp .env.example .env
   ```

   Required variables (see `.env.example` for the full list):

   - `DISCORD_TOKEN` your bot token
   - `DISCORD_CLIENT_ID` and `DISCORD_CLIENT_SECRET` from the Discord developer portal
   - `DISCORD_REDIRECT_URI` your OAuth callback URL
   - `JWT_SECRET` a random secret (generate with `python -c "import secrets; print(secrets.token_hex(32))"`)
   - `FRONTEND_URL` where your dashboard frontend is served
   - `DB_PATH` optional, path to the SQLite database file

3. Run the bot and API:

   ```
   python run.py
   ```

The database is created automatically on first run. Never commit your `.env` file or the database file; both are gitignored.

## Configuration

Most behavior is configured per server through the dashboard at https://www.avbot.app/dashboard. Every server's data is isolated from every other server.

## Security

To report a security vulnerability, please follow the disclosure policy at https://www.avbot.app/security. Do not open public issues for security reports.

## Contact

- Discord DM: `nervyesi1`
- Email: ameretaverse@gmail.com
- Community: https://discord.com/invite/ameretaverse

## License

The AVbot name, logo, and source code are the property of the AVbot operator. The source is made available for transparency and review. It is not licensed for redistribution, for forking into a competing or production service, or for rebranding. See the Terms of Service at https://www.avbot.app/terms for details.
