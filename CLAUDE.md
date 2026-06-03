# AmeretaVerse Bot — Claude Context

## Project
AVbot is a Discord bot for Web3 communities. **LIVE** in production with real users in 6 guilds (AmeretaVerse 10k+ members plus 5 others). Treat every change with production-grade care.

- API: api.avbot.app (Railway + Cloudflare)
- Dashboard: www.avbot.app (Vercel, separate repo at D:\Vibecoding\avbot-website)
- Database: SQLite at /data/ameretaverse.db (Railway volume)
- CDN: cdn.avbot.app → R2 bucket avbot-assets
- TwitterAPI.io paid plan (~1M credits)

## Critical constraints — apply to EVERY change

1. **No dashes (— – -) or "premium/upgrade/paid" wording** in user-facing copy. Bot launched free. "Web3" never hyphenated.
2. **cogs/_twitter.py is OFF LIMITS**. The verification logic (user-side via /twitter/user/last_tweets?includeReplies=true as primary, tweet-side safety-net fallback, eager probe cached per process, conversation_id catches threaded replies) is stable, validated against production. Do not touch unless explicitly asked.
3. **Per-guild isolation**: every query filters by guild_id. No cross-guild data leaks.
4. **Module gating**: features behind require_module_access(user, server_id, '<module_key>'). Keys: raid, engage, embed_message, giveaway, radar, voice_tracker, protection, verify, forms.
5. **Owner-only operations**: gated by require_global_admin which checks _GLOBAL_ADMIN_IDS (currently {461460143343927306}). Never gate by guild admin alone.
6. **Snowflake IDs as STRINGS end-to-end**. Discord IDs are 17-20 digits and exceed JS Number.MAX_SAFE_INTEGER. Backend returns/accepts strings; never Number()-coerce in round-trips.
7. **Idempotent migrations**: CREATE TABLE IF NOT EXISTS + ALTER TABLE ADD COLUMN with try/except OperationalError pass. Never DROP or rename without explicit ask.
8. **AllowedMentions** on every role-mentioning post: discord.AllowedMentions(roles=True, users=False, everyone=False).
9. **Rate limit + log_event** on every admin endpoint.

## Key paths

bot.py                  # entry; loads cogs; starts background tasks in on_ready
api.py                  # FastAPI (auth, dashboard endpoints, owner endpoints)
database.py             # SQLite schemas + helpers, all guild-scoped
config.py               # DEFAULT_BOT_THUMBNAIL_URL, _GLOBAL_ADMIN_IDS, REVERIFY_COOLDOWN_SECONDS=6
backup_service.py       # weekly R2 backup + retention

cogs/
  raidbot.py            # /raid create modal (Mode partial|all aliases normalized); live verify
  engage.py             # /submit, multi-pool, finalize, engage-for-engage
  _twitter.py           # STABLE — OFF LIMITS
  embed_message.py      # CRUD embed messages
  giveaway.py           # persistent DynamicItem buttons, atomic enter, reroll with derived seed
  radar.py              # /price, /topgainers, /watchlist, /radar add|remove
  voice_tracker.py      # voice_sessions tracking, restart-safe sweep
  protection.py         # link delete + domain whitelist + role whitelist (role check first)
  verify.py             # Discord membership CAPTCHA
  forms.py              # approval flow with role grant + DM + auto-close
  backup.py             # @tasks.loop(hours=168) weekly R2 backup

services/radar/
  fetcher.py            # 5-min loop, union-batched across guilds
  cache.py              # TTL cache per kind with history buffer
  rate_limiter.py       # token bucket per API
  alerts.py             # movement/volume alerts with 1h cooldown per (guild, asset, type, direction)
  digest.py             # per-guild timezone-scheduled daily digest
  adapters/
    base.py             # AssetAdapter ABC + CommonAssetSnapshot
    coingecko.py        # crypto, no key needed (25/min limit)
    reservoir.py        # NFT, requires RESERVOIR_API_KEY env (env-gated, disables cleanly if missing)
    dexscreener.py      # memecoin, no key, multi-chain
    frankfurter.py      # forex, no key

## Reusable helpers (use these, don't fork)

- `_normalize_role_id_list(values, *, field=...)` in api.py — tolerant comma/space/newline separator, 17-20 digit validation, dedupes
- `_radar_channel(value, *, field=...)` in api.py — snowflake string validator
- `build_branded_embed(guild_id, title, description, use_thumbnail, use_footer)` — branded embed with guild's brand
- `log_event(category, module, action, guild_id, user_id, payload)` — audit logging
- `require_global_admin(user)` and `require_module_access(user, server_id, module_key)` — auth gates

## Patterns

### Module endpoint
```python
@router.get("/api/servers/{server_id}/<module>/<resource>")
async def endpoint(server_id: int, ...):
    user = get_authenticated_user_or_raise(...)
    require_module_access(user, server_id, '<module_key>')
    rate_limit(f"<module>:<action>:{server_id}", 30, 60)
    rows = database.list_<resource>(guild_id=server_id)
    log_event(category='admin_action', module='<module>', action='<verb>', guild_id=server_id, user_id=user['id'])
    return rows
```

### Idempotent ADD COLUMN
```python
try:
    conn.execute('ALTER TABLE <table> ADD COLUMN <name> <type> NOT NULL DEFAULT <default>')
except sqlite3.OperationalError:
    pass
```

## Module states (current)

- **raid**: live verify with 6s reverify cooldown; Mode aliases (all/a/every/required → 'all'; partial/p/any → 'partial')
- **engage**: multi-pool engage-for-engage; verify chain via _twitter.py
- **embed_message**: CRUD + live edit
- **giveaway**: persistent buttons, atomic enter via BEGIN IMMEDIATE, reroll excludes previous winners, multi-mention support
- **radar**: Phase 1 (crypto) + Phase 2 (NFT, Meme, Forex) live; Phase 3 (multi-timeframe + discovery scanners) being built; Phase 4 (stocks) and Phase 5 (liquidations) reserved
- **voice_tracker**: voice_sessions with restart-safe sweep
- **protection**: domain whitelist + role whitelist (role check FIRST, then domain)
- **verify**: Discord membership CAPTCHA (distinct from raid verify)
- **forms**: approval flow with role grant
- **backup**: owner-only weekly R2, WAF-blocked at edge, uuid keys un-enumerable

## Brand
- Gold primary: #94730D
- Gold lighter: #c89a1f
- Gold hot: #e8c869
- Background: #0a0a0a

## Development workflow

Before commit:
```bash
python -c "import ast; [ast.parse(open(f, encoding='utf-8').read()) for f in ['api.py','database.py','bot.py','cogs/<changed>.py']]; print('parse ok')"
git diff --stat   # confirm cogs/_twitter.py NOT in changes
```

Commit format: short imperative + scope. Push to origin main.

## Key constants
- Owner Discord ID: 461460143343927306 (Nervyesi, X handle @Nervyesi)
- AmeretaVerse guild ID: 1199707792706117642
- LIVE_VERIFICATION_GUILD_IDS includes AmeretaVerse
- REVERIFY_COOLDOWN_SECONDS = 6
- _GLOBAL_ADMIN_IDS = {461460143343927306}