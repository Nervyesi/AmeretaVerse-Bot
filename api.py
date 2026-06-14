"""
api.py — AVbot Dashboard API
FastAPI server that runs alongside the Discord bot in the same asyncio loop.
The bot instance is shared via shared_bot.py so guild data is available live.
"""
# DISCORD ID HANDLING:
# Discord Snowflakes are 17-19 digit ints that overflow JS Number precision.
# All Discord ID fields in Pydantic models are typed as `str` to accept either
# raw IDs from the frontend (sent as strings to avoid JS truncation) or names
# (resolved by resolve_channel / resolve_role in cogs/_utils.py). Inside
# endpoints, always use resolve_channel/resolve_role rather than calling
# guild.get_channel(int(x)) directly on user-supplied values.
# Internal DB IDs (panel_id, button_id, ticket_id, etc.) are auto-increment
# SQLite integers and remain typed as int — they are always small and safe.

import os
import re
import json
import time
import asyncio
import sqlite3
from datetime import datetime, timezone, timedelta, date
from typing import Optional

import aiohttp
import jwt
from fastapi import FastAPI, Depends, HTTPException, Request, status, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, JSONResponse, FileResponse
from starlette.background import BackgroundTask
from dotenv import load_dotenv

import discord
from pydantic import BaseModel

from database import (
    get_connection,
    get_config as db_get_config,
    set_config as db_set_config,
    get_all_config as db_get_all_config,
    get_panels as db_get_panels,
    get_panel as db_get_panel,
    create_panel as db_create_panel,
    update_panel as db_update_panel,
    delete_panel as db_delete_panel,
    get_buttons as db_get_buttons,
    get_button as db_get_button,
    create_button as db_create_button,
    update_button as db_update_button,
    delete_button as db_delete_button,
    list_guild_assets as db_list_guild_assets,
    create_asset_record as db_create_asset_record,
    soft_delete_asset as db_soft_delete_asset,
    get_asset_by_id as db_get_asset_by_id,
    list_forms as db_list_forms,
    get_form as db_get_form,
    create_form as db_create_form,
    update_form as db_update_form,
    delete_form as db_delete_form,
    list_form_fields as db_list_form_fields,
    create_form_field as db_create_form_field,
    update_form_field as db_update_form_field,
    delete_form_field as db_delete_form_field,
    get_raid_settings as db_get_raid_settings,
    upsert_raid_settings as db_upsert_raid_settings,
    create_guild_raid as db_create_guild_raid,
    get_guild_raid as db_get_guild_raid,
    list_guild_raids as db_list_guild_raids,
    update_guild_raid as db_update_guild_raid,
    end_raid as db_end_raid,
    get_raid_leaderboard as db_get_raid_leaderboard,
    get_raid_verification_log as db_get_raid_verification_log,
    get_raid_participation as db_get_raid_participation,
    check_reset_manual_count as db_check_reset_manual_count,
    upsert_raid_settings,
    get_user_x_username as db_get_user_x_username,
    get_guild_settings,
    update_guild_settings,
    list_module_access,
    set_module_access,
    user_can_access_module,
    MODULES,
    log_event,
    list_events,
    count_events,
    list_flags,
    resolve_flag,
    list_embed_messages as db_list_embed_messages,
    get_embed_message as db_get_embed_message,
    create_embed_message as db_create_embed_message,
    update_embed_message as db_update_embed_message,
    delete_embed_message as db_delete_embed_message,
    list_giveaways as db_list_giveaways,
    get_giveaway as db_get_giveaway,
    create_giveaway as db_create_giveaway,
    update_giveaway as db_update_giveaway,
    delete_giveaway as db_delete_giveaway,
    list_giveaway_entries as db_list_giveaway_entries,
    count_giveaway_entries as db_count_giveaway_entries,
    refund_giveaway_entries as db_refund_giveaway_entries,
    get_radar_settings as db_get_radar_settings,
    update_radar_settings as db_update_radar_settings,
    list_radar_watchlist as db_list_radar_watchlist,
    add_radar_watchlist_entry as db_add_radar_watchlist_entry,
    remove_radar_watchlist_entry as db_remove_radar_watchlist_entry,
    list_recent_radar_alerts as db_list_recent_radar_alerts,
    get_radar_topic_settings as db_get_radar_topic_settings,
    update_radar_topic_settings as db_update_radar_topic_settings,
    list_radar_topic_settings as db_list_radar_topic_settings,
    check_and_consume_topic_send_quota as db_check_and_consume_topic_send_quota,
)
from shared_bot import bot
from cogs._branding import PREMIUM_GUILD_IDS
# Unlimited manual check exemption — independent of PREMIUM_GUILD_IDS.
# Only the bot owner's dev/test server; do NOT add guilds here for premium plans.
_UNLIMITED_MC_GUILDS: frozenset = frozenset({1199707792706117642})
from r2_client import upload_file as r2_upload, delete_file as r2_delete, ALLOWED_EXTENSIONS

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

CLIENT_ID       = os.getenv('DISCORD_CLIENT_ID')
CLIENT_SECRET   = os.getenv('DISCORD_CLIENT_SECRET')
REDIRECT_URI    = os.getenv('DISCORD_REDIRECT_URI')
JWT_SECRET      = os.getenv('JWT_SECRET',            'change-me-in-production')
FRONTEND_URL    = os.getenv('FRONTEND_URL',          'http://localhost:3000')

print(f"DISCORD_CLIENT_ID loaded: {bool(CLIENT_ID)}")
print(f"DISCORD_REDIRECT_URI: {REDIRECT_URI}")
JWT_EXPIRE_DAYS = 7

DISCORD_API     = 'https://discord.com/api/v10'
DISCORD_CDN     = 'https://cdn.discordapp.com'
OAUTH_SCOPES    = 'identify guilds'

# ── App setup ─────────────────────────────────────────────────────────────────

app = FastAPI(title='AVbot Dashboard API', version='1.0.0')

_cors_extra   = [o.strip() for o in os.getenv('CORS_ORIGINS', '').split(',') if o.strip()]
_allow_origins = list({FRONTEND_URL, 'http://localhost:3000', 'http://localhost:3001'} | set(_cors_extra))

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allow_origins,
    allow_credentials=True,
    allow_methods=['*'],
    allow_headers=['*'],
)

# ── Rate limiting (in-process, no external deps) ─────────────────────────────
# Sliding-window counter keyed by an arbitrary string (caller decides whether to
# key by client IP, user id, or both). Lightweight and good enough to blunt
# hammering / abuse and protect downstream (Twitter API, DB). Launch-day safe
# defaults; tune via the call sites.
import time as _time
import threading as _threading
from collections import deque as _deque

_rl_buckets: dict = {}
_rl_lock = _threading.Lock()


def _client_ip(request: Request) -> str:
    """Best-effort client IP, honoring a single proxy hop (X-Forwarded-For)."""
    xff = request.headers.get('x-forwarded-for', '')
    if xff:
        return xff.split(',')[0].strip()
    return request.client.host if request.client else 'unknown'


def rate_limit(key: str, max_calls: int, window_secs: float) -> None:
    """Raise HTTP 429 if `key` exceeds max_calls within window_secs.

    Pure in-memory sliding window. Buckets self-trim; a restart simply resets
    all counters (acceptable for abuse protection).
    """
    now = _time.monotonic()
    cutoff = now - window_secs
    with _rl_lock:
        dq = _rl_buckets.get(key)
        if dq is None:
            dq = _deque()
            _rl_buckets[key] = dq
        while dq and dq[0] < cutoff:
            dq.popleft()
        if len(dq) >= max_calls:
            retry = max(1, int(dq[0] + window_secs - now) + 1)
            raise HTTPException(
                status_code=429,
                detail='Too many requests. Please slow down and try again shortly.',
                headers={'Retry-After': str(retry)},
            )
        dq.append(now)
        # Opportunistic memory cap: keep the bucket table from growing without
        # bound under a flood of distinct keys.
        if len(_rl_buckets) > 50000:
            for k in [k for k, d in list(_rl_buckets.items()) if not d or d[-1] < cutoff][:10000]:
                _rl_buckets.pop(k, None)


def rate_limit_public(request: Request, name: str, max_calls: int = 30, window_secs: float = 60.0) -> None:
    """Convenience wrapper: rate limit a public (unauthenticated) endpoint by IP."""
    rate_limit(f'pub:{name}:{_client_ip(request)}', max_calls, window_secs)


# ── Global exception handler (no stack-trace / secret leakage) ───────────────
from fastapi.exceptions import RequestValidationError as _RequestValidationError


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception):
    # HTTPException is handled by FastAPI's own handler; this catches the rest.
    import traceback as _tb
    print(f'[api] UNHANDLED {request.method} {request.url.path}: '
          f'{type(exc).__name__}: {exc}')
    _tb.print_exc()
    return JSONResponse(status_code=500, content={'detail': 'Internal server error'})


@app.exception_handler(_RequestValidationError)
async def _validation_exception_handler(request: Request, exc: _RequestValidationError):
    # Do not echo back the full validation internals (can include payload shape);
    # return a generic, safe message.
    return JSONResponse(status_code=422, content={'detail': 'Invalid request payload'})


# ── Input validation helpers ────────────────────────────────────────────────
def validate_snowflake(value, field: str = 'id') -> int:
    """Validate a Discord snowflake (guild/user/channel id). Reject malformed.

    Discord snowflakes are positive 64-bit ints. We bound to a sane range to
    reject negatives, zero, overflow and non-numeric junk from crafted input.
    """
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f'Invalid {field}')
    if n <= 0 or n > 9_223_372_036_854_775_807:
        raise HTTPException(status_code=400, detail=f'Invalid {field}')
    return n


# Hard bound on any single point award/removal to prevent integer abuse / overflow.
MAX_POINT_DELTA = 1_000_000


def validate_point_amount(value, field: str = 'amount') -> int:
    """Validate a point delta magnitude: integer, non-zero-safe, bounded."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f'Invalid {field}')
    if abs(n) > MAX_POINT_DELTA:
        raise HTTPException(status_code=400, detail=f'{field} out of allowed range')
    return n


# ── JWT helpers ────────────────────────────────────────────────────────────────

def create_jwt(payload: dict) -> str:
    payload['exp'] = datetime.now(timezone.utc) + timedelta(days=JWT_EXPIRE_DAYS)
    return jwt.encode(payload, JWT_SECRET, algorithm='HS256')


def decode_jwt(token: str) -> dict:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=['HS256'])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail='Token expired')
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail='Invalid token')


def get_bearer(request: Request) -> str:
    auth = request.headers.get('Authorization', '')
    if not auth.startswith('Bearer '):
        raise HTTPException(status_code=401, detail='Missing authorization header')
    return auth[7:]


async def get_current_user(request: Request) -> dict:
    token = get_bearer(request)
    return decode_jwt(token)

# ── Discord OAuth helpers ─────────────────────────────────────────────────────

async def discord_token_exchange(code: str) -> dict:
    async with aiohttp.ClientSession() as session:
        data = {
            'client_id':     CLIENT_ID,
            'client_secret': CLIENT_SECRET,
            'grant_type':    'authorization_code',
            'code':          code,
            'redirect_uri':  REDIRECT_URI,
        }
        async with session.post(f'{DISCORD_API}/oauth2/token', data=data) as r:
            if r.status != 200:
                text = await r.text()
                raise HTTPException(status_code=400, detail=f'Discord token exchange failed: {text}')
            return await r.json()


async def discord_get(endpoint: str, access_token: str) -> dict | list:
    async with aiohttp.ClientSession() as session:
        headers = {'Authorization': f'Bearer {access_token}'}
        async with session.get(f'{DISCORD_API}{endpoint}', headers=headers) as r:
            if r.status == 401:
                raise HTTPException(status_code=401, detail='Discord token invalid or expired')
            return await r.json()


def avatar_url(user_id: str, avatar_hash: Optional[str], discriminator: str = '0') -> str:
    if avatar_hash:
        ext = 'gif' if avatar_hash.startswith('a_') else 'png'
        return f'{DISCORD_CDN}/avatars/{user_id}/{avatar_hash}.{ext}?size=128'
    idx = (int(discriminator) % 5) if discriminator.isdigit() else (int(user_id) >> 22) % 6
    return f'{DISCORD_CDN}/embed/avatars/{idx}.png'

# ── Session store ─────────────────────────────────────────────────────────────

def store_session(user_id: int, access_token: str, refresh_token: str, expires_in: int):
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO oauth_sessions (user_id, access_token, refresh_token, expires_at)
               VALUES (?,?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET
                   access_token=excluded.access_token,
                   refresh_token=excluded.refresh_token,
                   expires_at=excluded.expires_at,
                   updated_at=CURRENT_TIMESTAMP""",
            (user_id, access_token, refresh_token, expires_at.isoformat()),
        )


def get_session(user_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            'SELECT * FROM oauth_sessions WHERE user_id=?', (user_id,)
        ).fetchone()
    return dict(row) if row else None

# ── Auth check helper ─────────────────────────────────────────────────────────

def _get_bot_instance():
    """Return the running discord.Bot instance, or raise 503."""
    try:
        from shared_bot import bot as _bot
    except ImportError:
        try:
            from bot import bot as _bot
        except ImportError:
            raise HTTPException(status_code=503, detail='Bot module not available')
    if not _bot or not _bot.is_ready():
        raise HTTPException(status_code=503, detail='Bot not ready')
    return _bot


def require_guild_admin(user: dict, server_id: int) -> dict:
    """Raise HTTPException if user is not the guild owner or does not have Administrator.
    Returns permission info dict on success.
    Uses discord.py's in-memory guild/member cache — updated in real-time by Discord gateway."""
    user_id = int(user.get('user_id') or user.get('id') or 0)
    if not user_id:
        raise HTTPException(status_code=401, detail='Not authenticated')

    bot_instance = _get_bot_instance()
    guild = bot_instance.get_guild(int(server_id))
    if not guild:
        raise HTTPException(status_code=404, detail='Bot is not in this guild')

    is_owner = (str(guild.owner_id) == str(user_id))

    member = guild.get_member(user_id)
    is_admin = bool(member and member.guild_permissions.administrator)
    has_access = is_owner or is_admin

    if not has_access:
        print(f'[security] DENIED: user_id={user_id} attempted access to guild={server_id} — not owner, not admin')
        raise HTTPException(status_code=403, detail='You need the Administrator permission (or be the server owner) to access this server\'s dashboard.')

    return {'is_owner': is_owner, 'is_admin': is_admin, 'has_access': True}


def require_module_access(user: dict, server_id: int, module: str) -> dict:
    """Guild admin check + module-level role grant check."""
    perm = require_guild_admin(user, server_id)
    user_id = int(user.get('user_id') or user.get('id') or 0)
    bot = _get_bot_instance()
    if not user_can_access_module(server_id, user_id, module, bot):
        print(f'[security] DENIED: user_id={user_id} module={module} guild={server_id} — role not granted')
        raise HTTPException(status_code=403,
            detail=f'You do not have access to the {module} module in this server.')
    return perm

# ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ──
#  AUTH ENDPOINTS
# ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ──

@app.get('/auth/login')
async def auth_login():
    """Redirect browser to Discord OAuth2 consent screen."""
    url = (
        f'https://discord.com/oauth2/authorize'
        f'?client_id={CLIENT_ID}'
        f'&redirect_uri={REDIRECT_URI}'
        f'&response_type=code'
        f'&scope={OAUTH_SCOPES.replace(" ", "%20")}'
    )
    return RedirectResponse(url)


@app.get('/auth/callback')
async def auth_callback(code: str):
    """Exchange OAuth2 code for access token, create JWT, redirect to frontend."""
    token_data = await discord_token_exchange(code)
    access_token  = token_data['access_token']
    refresh_token = token_data.get('refresh_token', '')
    expires_in    = token_data.get('expires_in', 604800)

    user_data  = await discord_get('/users/@me',        access_token)
    guild_data = await discord_get('/users/@me/guilds',  access_token)

    user_id = int(user_data['id'])
    store_session(user_id, access_token, refresh_token, expires_in)

    # Slim guild list: only servers where user is admin
    admin_guilds = [
        {'id': g['id'], 'name': g['name'], 'icon': g.get('icon'), 'permissions': g.get('permissions', '0')}
        for g in guild_data
        if int(g.get('permissions', 0)) & 0x8
    ]

    avatar_hash = user_data.get('avatar')
    avatar_full = avatar_url(user_data['id'], avatar_hash, user_data.get('discriminator', '0'))

    jwt_payload = {
        'user_id':     user_id,
        'username':    user_data.get('global_name') or user_data['username'],
        'avatar':      avatar_full,  # legacy field: full URL (kept for compat)
        'avatar_hash': avatar_hash,
        'avatar_url':  avatar_full,
        'guilds':      admin_guilds,
    }
    token = create_jwt(jwt_payload)
    return RedirectResponse(f'{FRONTEND_URL}/dashboard?token={token}')


@app.get('/auth/me')
async def auth_me(user: dict = Depends(get_current_user)):
    """Return current user info decoded from JWT."""
    # Defensive: older JWTs may only carry `avatar` (the full URL); synthesize avatar_url.
    avatar_full = user.get('avatar_url') or user.get('avatar')
    return {
        'user_id':     user['user_id'],
        'id':          str(user['user_id']),
        'username':    user['username'],
        'avatar':      avatar_full,
        'avatar_hash': user.get('avatar_hash'),
        'avatar_url':  avatar_full,
        'guilds':      user.get('guilds', []),
    }

# ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ──
#  SERVER ENDPOINTS
# ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ──

@app.get('/api/servers')
async def list_servers(user: dict = Depends(get_current_user)):
    """Return servers where the bot is present AND the user is the guild owner or has Administrator."""
    bot_instance = _get_bot_instance()
    user_id = int(user.get('user_id') or user.get('id') or 0)
    result = []
    for guild in bot_instance.guilds:
        is_owner = (str(guild.owner_id) == str(user_id))
        icon_url = str(guild.icon.url) if guild.icon else None
        if is_owner:
            result.append({
                'id':         str(guild.id),
                'name':       guild.name,
                'icon':       icon_url,
                'members':    guild.member_count,
                'is_premium': guild.id in PREMIUM_GUILD_IDS,
                'role':       'owner',
            })
            continue
        member = guild.get_member(user_id)
        if member and member.guild_permissions.administrator:
            result.append({
                'id':         str(guild.id),
                'name':       guild.name,
                'icon':       icon_url,
                'members':    guild.member_count,
                'is_premium': guild.id in PREMIUM_GUILD_IDS,
                'role':       'admin',
            })
    return result


@app.get('/api/servers/{server_id}/stats')
async def server_stats(server_id: int, user: dict = Depends(get_current_user)):
    """Live stats for a guild via the bot's cache."""
    require_guild_admin(user, server_id)

    if not bot.is_ready():
        raise HTTPException(status_code=503, detail='Bot not ready yet')

    guild = bot.get_guild(server_id)
    if guild is None:
        raise HTTPException(status_code=404, detail='Bot is not in this server')

    online = sum(
        1 for m in guild.members
        if m.status not in (None,) and str(m.status) != 'offline'
    )

    return {
        'id':           str(guild.id),
        'name':         guild.name,
        'icon':         str(guild.icon.url) if guild.icon else None,
        'member_count': guild.member_count,
        'online_count': online,
        'channel_count': len(guild.channels),
        'role_count':   len(guild.roles),
        'text_channels': [
            {'id': str(c.id), 'name': c.name}
            for c in guild.text_channels
        ],
        'voice_channels': [
            {'id': str(c.id), 'name': c.name}
            for c in guild.voice_channels
        ],
    }


def _build_channel_messages(server_id: int, chan_rows) -> list:
    """Resolve channel ids to live names; drop channels that no longer exist."""
    guild = bot.get_guild(server_id) if bot and bot.is_ready() else None
    out = []
    for r in chan_rows:
        cid = int(r['channel_id'])
        ch = guild.get_channel(cid) if guild else None
        if ch is None and guild is not None:
            # Channel deleted — skip it rather than show a dangling id.
            continue
        out.append({
            'channel_id': str(cid),
            'name': f'#{ch.name}' if ch else f'#{cid}',
            'count': int(r['count'] or 0),
        })
    return out


def _build_active_hours(hour_dist_rows) -> list:
    """Return a full 24-slot hour-of-day distribution (0 for unseen hours)."""
    by_hour = {int(r['hour']): int(r['count'] or 0) for r in hour_dist_rows}
    return [{'hour': h, 'label': f'{h:02d}:00', 'count': by_hour.get(h, 0)}
            for h in range(24)]


@app.get('/api/servers/{server_id}/analytics')
async def server_analytics(
    server_id: int,
    timeframe: str = 'week',
    user: dict = Depends(get_current_user),
):
    """
    Multi-timeframe analytics. Returns all four timeframe arrays at once.
    Reads from analytics_snapshots + message_counters (populated by cogs/analytics.py).
    """
    require_module_access(user, server_id, 'analytics')

    today = date.today()

    def day_label(d):   return d.strftime('%a')
    def date_label(d):  return d.strftime('%b ') + str(d.day)
    def month_label(d): return d.strftime('%b')
    def hour_label(h):  return f'{h:02d}:00'

    leaves_tracking_started = db_get_config(server_id, 'analytics_leaves_tracking_started', '') or ''

    with get_connection() as conn:
        # ── Pull snapshots ────────────────────────────────────────────────
        snaps_7 = conn.execute("""
            SELECT snapshot_date, member_count, joins_24h, leaves_24h, message_count_24h
            FROM analytics_snapshots WHERE guild_id=?
            ORDER BY snapshot_date DESC LIMIT 7
        """, (server_id,)).fetchall()

        snaps_30 = conn.execute("""
            SELECT snapshot_date, member_count, joins_24h, leaves_24h, message_count_24h
            FROM analytics_snapshots WHERE guild_id=?
            ORDER BY snapshot_date DESC LIMIT 30
        """, (server_id,)).fetchall()

        snaps_12mo = conn.execute("""
            SELECT strftime('%Y-%m', snapshot_date) as ym,
                   AVG(member_count) as avg_members,
                   SUM(message_count_24h) as total_msgs,
                   SUM(joins_24h) as total_joins,
                   SUM(leaves_24h) as total_leaves
            FROM analytics_snapshots WHERE guild_id=?
            GROUP BY ym ORDER BY ym DESC LIMIT 13
        """, (server_id,)).fetchall()

        first_snap = conn.execute("""
            SELECT MIN(snapshot_date) as first_date
            FROM analytics_snapshots WHERE guild_id=?
        """, (server_id,)).fetchone()

        # ── Stat card aggregates ──────────────────────────────────────────
        today_mc = conn.execute("""
            SELECT message_count, joins, leaves FROM message_counters
            WHERE guild_id=? AND date=?
        """, (server_id, today.isoformat())).fetchone()

        week_sums = conn.execute("""
            SELECT COALESCE(SUM(joins_24h),0) as week_joins,
                   COALESCE(SUM(message_count_24h),0) as week_msgs
            FROM analytics_snapshots
            WHERE guild_id=? AND snapshot_date >= date('now','-7 days')
        """, (server_id,)).fetchone()

        month_sums = conn.execute("""
            SELECT COALESCE(SUM(joins_24h),0) as month_joins,
                   COALESCE(SUM(message_count_24h),0) as month_msgs
            FROM analytics_snapshots
            WHERE guild_id=? AND snapshot_date >= date('now','-30 days')
        """, (server_id,)).fetchone()

        year_sums = conn.execute("""
            SELECT COALESCE(SUM(message_count_24h),0) as year_msgs,
                   COUNT(*) as days_count
            FROM analytics_snapshots
            WHERE guild_id=? AND snapshot_date >= date('now','-365 days')
        """, (server_id,)).fetchone()

        # ── Hourly message counts for day view ────────────────────────────
        hourly_msgs = conn.execute("""
            SELECT hour, count FROM message_hourly
            WHERE guild_id=? AND date=?
        """, (server_id, today.isoformat())).fetchall()

        # ── First message tracked date ────────────────────────────────────
        first_msg_row = conn.execute("""
            SELECT MIN(date) as first_date FROM message_counters WHERE guild_id=?
        """, (server_id,)).fetchone()

        # ── Leaderboard + raids + engage (always live) ────────────────────
        leaderboard = conn.execute("""
            SELECT user_id, username, total_points FROM users
            ORDER BY total_points DESC LIMIT 10
        """).fetchall()

        raid_stats = conn.execute("""
            SELECT COUNT(*) as total_raids,
                   SUM(CASE WHEN status='active' THEN 1 ELSE 0 END) as active_raids,
                   SUM(total_points) as total_points_offered
            FROM raids
            WHERE guild_id=?
        """, (server_id,)).fetchone()

        # ── Engage-for-Engage (real per-guild tables) ─────────────────────
        # Legacy engage_links / engage_participation are global, schema-less
        # for guild_id, and unused by the live cog. The live engage flow writes
        # to engage_submissions / engage_actions / engage_user_points; query
        # those, strictly scoped by guild_id.
        gid_s = str(server_id)

        e4e_submissions = conn.execute("""
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN status='active' THEN 1 ELSE 0 END) AS active
            FROM engage_submissions WHERE guild_id=?
        """, (gid_s,)).fetchone()

        e4e_claims = conn.execute("""
            SELECT COUNT(*) AS total,
                   COALESCE(SUM(points_earned), 0) AS points_from_actions
            FROM engage_actions WHERE guild_id=?
        """, (gid_s,)).fetchone()

        e4e_points_row = conn.execute("""
            SELECT COALESCE(SUM(points), 0) AS points_balance,
                   COALESCE(SUM(total_engaged),   0) AS total_engaged,
                   COALESCE(SUM(total_submitted), 0) AS total_submitted
            FROM engage_user_points WHERE guild_id=?
        """, (gid_s,)).fetchone()

        e4e_participants = conn.execute("""
            SELECT COUNT(DISTINCT engager_user_id) AS engagers,
                   COUNT(DISTINCT submission_id)   AS submissions_engaged
            FROM engage_actions WHERE guild_id=?
        """, (gid_s,)).fetchone()

        e4e_contributors = conn.execute("""
            SELECT COUNT(DISTINCT submitter_user_id) AS contributors
            FROM engage_submissions WHERE guild_id=?
        """, (gid_s,)).fetchone()

        e4e_top_engagers = conn.execute("""
            SELECT a.engager_user_id AS user_id,
                   COALESCE(u.username,
                            MAX(a.engager_x_username)) AS username,
                   COUNT(*) AS claims,
                   COALESCE(SUM(a.points_earned), 0) AS points
            FROM engage_actions a
            LEFT JOIN users u ON u.user_id = CAST(a.engager_user_id AS INTEGER)
            WHERE a.guild_id=?
            GROUP BY a.engager_user_id
            ORDER BY points DESC, claims DESC
            LIMIT 10
        """, (gid_s,)).fetchall()

        e4e_top_contributors = conn.execute("""
            SELECT s.submitter_user_id AS user_id,
                   COALESCE(u.username,
                            MAX(s.submitter_x_username)) AS username,
                   COUNT(*) AS submissions
            FROM engage_submissions s
            LEFT JOIN users u ON u.user_id = CAST(s.submitter_user_id AS INTEGER)
            WHERE s.guild_id=?
            GROUP BY s.submitter_user_id
            ORDER BY submissions DESC
            LIMIT 10
        """, (gid_s,)).fetchall()

        e4e_per_pool = conn.execute("""
            SELECT p.pool_id,
                   COALESCE(p.display_name, p.name) AS name,
                   (SELECT COUNT(*) FROM engage_submissions s
                     WHERE s.pool_id=p.pool_id AND s.guild_id=?) AS submissions,
                   (SELECT COUNT(*) FROM engage_actions a
                     WHERE a.pool_id=p.pool_id AND a.guild_id=?) AS claims,
                   (SELECT COALESCE(SUM(points_earned),0) FROM engage_actions a
                     WHERE a.pool_id=p.pool_id AND a.guild_id=?) AS points
            FROM engage_pools p
            WHERE p.guild_id=?
            ORDER BY claims DESC, submissions DESC
        """, (gid_s, gid_s, gid_s, gid_s)).fetchall()

        # 30-day raw daily aggregates for engage activity. Python fills the
        # gaps with zeros so the chart always has 30 contiguous days.
        e4e_subs_daily = conn.execute("""
            SELECT date(submitted_at) AS day, COUNT(*) AS subs
            FROM engage_submissions
            WHERE guild_id=? AND submitted_at >= date('now','-30 days')
            GROUP BY date(submitted_at)
        """, (gid_s,)).fetchall()

        e4e_claims_daily = conn.execute("""
            SELECT date(created_at) AS day,
                   COUNT(*) AS claims,
                   COALESCE(SUM(points_earned),0) AS points
            FROM engage_actions
            WHERE guild_id=? AND created_at >= date('now','-30 days')
            GROUP BY date(created_at)
        """, (gid_s,)).fetchall()

        # ── Messages per channel (cumulative, never pruned) ───────────────
        chan_rows = conn.execute("""
            SELECT channel_id, count FROM message_channel_counters
            WHERE guild_id=? ORDER BY count DESC LIMIT 15
        """, (server_id,)).fetchall()

        # ── Top chatters (cumulative per-user counts) ─────────────────────
        chatter_rows = conn.execute("""
            SELECT muc.user_id, muc.count AS msg_count, u.username
            FROM message_user_counters muc
            LEFT JOIN users u ON u.user_id = muc.user_id
            WHERE muc.guild_id=? ORDER BY muc.count DESC LIMIT 10
        """, (server_id,)).fetchall()

        # ── Most active hours (cumulative hour-of-day distribution) ───────
        hour_dist_rows = conn.execute("""
            SELECT hour, count FROM message_hourly_dist WHERE guild_id=?
        """, (server_id,)).fetchall()

        # ── Voice activity (per-guild) ────────────────────────────────────
        voice_totals = conn.execute("""
            SELECT COALESCE(SUM(duration_seconds), 0) AS total_seconds,
                   COUNT(*) AS total_sessions,
                   COUNT(DISTINCT user_id)    AS distinct_users,
                   COUNT(DISTINCT channel_id) AS distinct_channels
            FROM voice_sessions
            WHERE guild_id=? AND left_at IS NOT NULL
        """, (gid_s,)).fetchone()

        voice_top_users = conn.execute("""
            SELECT v.user_id,
                   COALESCE(u.username, 'User ' || v.user_id) AS username,
                   COALESCE(SUM(v.duration_seconds), 0) AS seconds,
                   COUNT(*) AS sessions
            FROM voice_sessions v
            LEFT JOIN users u ON u.user_id = CAST(v.user_id AS INTEGER)
            WHERE v.guild_id=? AND v.left_at IS NOT NULL
            GROUP BY v.user_id
            ORDER BY seconds DESC
            LIMIT 10
        """, (gid_s,)).fetchall()

        voice_top_channels = conn.execute("""
            SELECT channel_id,
                   COALESCE(SUM(duration_seconds), 0) AS seconds,
                   COUNT(*) AS sessions
            FROM voice_sessions
            WHERE guild_id=? AND left_at IS NOT NULL
            GROUP BY channel_id
            ORDER BY seconds DESC
            LIMIT 10
        """, (gid_s,)).fetchall()

        voice_daily = conn.execute("""
            SELECT date(joined_at) AS day,
                   COALESCE(SUM(duration_seconds), 0) AS seconds
            FROM voice_sessions
            WHERE guild_id=? AND left_at IS NOT NULL
              AND joined_at >= date('now','-30 days')
            GROUP BY date(joined_at)
        """, (gid_s,)).fetchall()

        # ── Community Points engagement (raids + community points) ────────
        comm_part = conn.execute("""
            SELECT COUNT(DISTINCT user_id) AS participants,
                   COALESCE(SUM(points_earned),0) AS points_awarded,
                   COUNT(*) AS participations
            FROM raid_participation WHERE guild_id=?
        """, (server_id,)).fetchone()

        comm_holders = conn.execute("""
            SELECT rup.user_id, u.username, rup.total_points, rup.raids_completed
            FROM raid_user_points rup LEFT JOIN users u ON u.user_id=rup.user_id
            WHERE rup.guild_id=? ORDER BY rup.total_points DESC LIMIT 10
        """, (server_id,)).fetchall()

    # ── Stat card values ──────────────────────────────────────────────────
    _guild = bot.get_guild(server_id)
    live_count = _guild.member_count if _guild else 0
    verified_role = discord.utils.get(_guild.roles, name='Verified') if _guild else None
    live_verified = len(verified_role.members) if verified_role else 0

    today_joins  = today_mc['joins']         if today_mc else 0
    today_leaves = today_mc['leaves']        if today_mc else 0
    today_msgs   = today_mc['message_count'] if today_mc else 0
    week_joins  = (week_sums['week_joins']  if week_sums  else 0) + today_joins
    month_joins = (month_sums['month_joins'] if month_sums else 0) + today_joins
    week_msgs   = (week_sums['week_msgs']   if week_sums  else 0) + today_msgs
    month_msgs  = (month_sums['month_msgs'] if month_sums else 0) + today_msgs
    year_msgs   = (year_sums['year_msgs']   if year_sums  else 0) + today_msgs
    days_count  = (year_sums['days_count']  if year_sums  else 0) + 1
    avg_per_day = year_msgs // days_count if days_count > 0 else 0

    has_data = len(snaps_7) > 0 or len(snaps_30) > 0

    # ── Helpers ───────────────────────────────────────────────────────────

    def build_week():
        by_date = {r['snapshot_date']: r for r in snaps_7}
        mg, jl, msgs = [], [], []
        prev_mc = live_count
        for i in range(6, -1, -1):
            d       = today - timedelta(days=i)
            snap    = by_date.get(d.isoformat())
            is_today = (i == 0)
            if is_today:
                mc  = live_count
                j   = today_joins
                lv  = today_leaves
                msg = today_msgs
            elif snap:
                mc  = snap['member_count']
                j   = snap['joins_24h']
                lv  = snap['leaves_24h']
                msg = snap['message_count_24h']
            else:
                mc  = prev_mc  # forward-fill gaps
                j   = 0
                lv  = 0
                msg = 0
            prev_mc = mc
            mg.append({'label': day_label(d), 'value': mc})
            jl.append({'label': day_label(d), 'joins': j, 'leaves': lv})
            msgs.append({'label': day_label(d), 'value': msg})
        return mg, jl, msgs

    def build_month():
        by_date = {r['snapshot_date']: r for r in snaps_30}
        mg, jl, msgs = [], [], []
        prev_mc = live_count
        for i in range(29, -1, -1):
            d       = today - timedelta(days=i)
            snap    = by_date.get(d.isoformat())
            is_today = (i == 0)
            if is_today:
                mc  = live_count
                j   = today_joins
                lv  = today_leaves
                msg = today_msgs
            elif snap:
                mc  = snap['member_count']
                j   = snap['joins_24h']
                lv  = snap['leaves_24h']
                msg = snap['message_count_24h']
            else:
                mc  = prev_mc  # forward-fill gaps
                j   = 0
                lv  = 0
                msg = 0
            prev_mc = mc
            mg.append({'label': date_label(d), 'value': mc})
            jl.append({'label': date_label(d), 'joins': j, 'leaves': lv})
            msgs.append({'label': date_label(d), 'value': msg})
        return mg, jl, msgs

    def build_year():
        """Yearly view: only emit months from the first snapshot month → now.

        The old behavior forced a 12-month window padded with zeros for months
        before any real data, which produced an unreadable vertical drop. The
        new behavior is honest: if the guild has 2 months of data, the chart
        shows 2 months. The response carries data_start_date so the frontend
        can show a small "showing all available data since X" note when the
        span is short."""
        by_ym = {r['ym']: r for r in snaps_12mo}
        if not first_snap or not first_snap['first_date']:
            return [], [], []
        first_date = first_snap['first_date']  # 'YYYY-MM-DD'
        try:
            fy, fm = int(first_date[:4]), int(first_date[5:7])
        except (TypeError, ValueError):
            return [], [], []

        # Walk from (fy, fm) forward to today's month, inclusive.
        mg, jl, msgs = [], [], []
        y, m = fy, fm
        while (y, m) <= (today.year, today.month):
            d  = date(y, m, 1)
            ym = d.strftime('%Y-%m')
            snap = by_ym.get(ym)
            mg.append({
                'label': month_label(d),
                'value': int(snap['avg_members'] or 0) if snap else None,
            })
            jl.append({
                'label':  month_label(d),
                'joins':  int(snap['total_joins']  or 0) if snap else 0,
                'leaves': int(snap['total_leaves'] or 0) if snap else 0,
            })
            msgs.append({
                'label': month_label(d),
                'value': int(snap['total_msgs'] or 0) if snap else 0,
            })
            m += 1
            if m > 12:
                m = 1
                y += 1
        return mg, jl, msgs

    def build_day():
        guild       = bot.get_guild(server_id)
        cur_mc      = guild.member_count if guild else 0
        now_utc_h   = datetime.now(timezone.utc).hour
        hourly_by_h = {r['hour']: r['count'] for r in hourly_msgs}
        mg   = [{'label': hour_label(h), 'value': cur_mc if h <= now_utc_h else None} for h in range(24)]
        jl   = [{'label': hour_label(h), 'joins': 0, 'leaves': 0} for h in range(24)]
        msgs = [{'label': hour_label(h), 'value': hourly_by_h.get(h, 0) if h <= now_utc_h else None} for h in range(24)]
        return mg, jl, msgs

    if not has_data:
        member_growth = {'day': [], 'week': [], 'month': [], 'year': []}
        joins_leaves  = {'day': [], 'week': [], 'month': [], 'year': []}
        messages      = {'day': [], 'week': [], 'month': [], 'year': []}
    else:
        mg_week,  jl_week,  msgs_week  = build_week()
        mg_month, jl_month, msgs_month = build_month()
        mg_year,  jl_year,  msgs_year  = build_year()
        mg_day,   jl_day,   msgs_day   = build_day()
        member_growth = {'day': mg_day, 'week': mg_week, 'month': mg_month, 'year': mg_year}
        joins_leaves  = {'day': jl_day, 'week': jl_week, 'month': jl_month, 'year': jl_year}
        messages      = {'day': msgs_day, 'week': msgs_week, 'month': msgs_month, 'year': msgs_year}

    # ── Voice rollups ─────────────────────────────────────────────────────
    voice_total_seconds = int(voice_totals['total_seconds'] or 0) if voice_totals else 0
    voice_top_channel_list = _build_channel_messages(
        server_id,
        [{'channel_id': r['channel_id'], 'count': int(r['seconds'] or 0)}
         for r in voice_top_channels],
    )
    # Re-key channel rollups so the frontend can render minutes/sessions clearly.
    for ch, r in zip(voice_top_channel_list, voice_top_channels):
        ch['seconds']  = int(r['seconds'] or 0)
        ch['minutes']  = int((r['seconds'] or 0) // 60)
        ch['sessions'] = int(r['sessions'] or 0)
        ch.pop('count', None)

    # Voice daily fill: build a 30-slot dense series for the chart.
    voice_by_day = {r['day']: int(r['seconds'] or 0) for r in voice_daily}
    voice_timeseries = []
    for i in range(29, -1, -1):
        d = today - timedelta(days=i)
        voice_timeseries.append({
            'label':   date_label(d),
            'date':    d.isoformat(),
            'minutes': voice_by_day.get(d.isoformat(), 0) // 60,
        })

    # ── Engage timeseries fill (30 contiguous days) ───────────────────────
    e4e_subs_by_day   = {r['day']: int(r['subs']   or 0) for r in e4e_subs_daily}
    e4e_claims_by_day = {r['day']: (int(r['claims'] or 0), int(r['points'] or 0))
                         for r in e4e_claims_daily}
    e4e_series = []
    for i in range(29, -1, -1):
        d  = today - timedelta(days=i)
        cl, pt = e4e_claims_by_day.get(d.isoformat(), (0, 0))
        e4e_series.append({
            'label':       date_label(d),
            'date':        d.isoformat(),
            'submissions': e4e_subs_by_day.get(d.isoformat(), 0),
            'claims':      cl,
            'points':      pt,
        })

    # ── Yearly span metadata (so the frontend can show an honest note) ────
    data_start_date = first_snap['first_date'] if first_snap and first_snap['first_date'] else None
    months_covered  = 0
    if data_start_date:
        try:
            fy, fm = int(data_start_date[:4]), int(data_start_date[5:7])
            months_covered = (today.year - fy) * 12 + (today.month - fm) + 1
            if months_covered < 0:
                months_covered = 0
        except (TypeError, ValueError):
            months_covered = 0

    return {
        'member_growth':             member_growth,
        'joins_leaves':              joins_leaves,
        'messages':                  messages,
        'yearly_meta': {
            'data_start_date':   data_start_date,
            'months_covered':    months_covered,
            'is_sparse':         months_covered < 3,
        },
        'leaves_tracking_started':   leaves_tracking_started or None,
        'first_message_tracked_date': first_msg_row['first_date'] if first_msg_row else None,
        'raids': {
            'total':          raid_stats['total_raids'],
            'active':         raid_stats['active_raids'],
            'points_offered': raid_stats['total_points_offered'] or 0,
        },
        'leaderboard':         [dict(r) for r in leaderboard],
        'data_started':        data_start_date,
        'first_snapshot_date': data_start_date,
        'has_any_data':        has_data,

        # ── Messages per channel (real, cumulative) ───────────────────────
        'messages_per_channel': _build_channel_messages(server_id, chan_rows),

        # ── Top chatters (real, cumulative) ───────────────────────────────
        'top_chatters': [
            {'user_id': str(r['user_id']),
             'username': r['username'] or f"User {r['user_id']}",
             'count': int(r['msg_count'] or 0)}
            for r in chatter_rows
        ],

        # ── Most active hours (real, cumulative 24h distribution) ─────────
        'active_hours': _build_active_hours(hour_dist_rows),

        # ── Voice activity (real, per-guild) ──────────────────────────────
        'voice': {
            'available':       voice_total_seconds > 0,
            'total_seconds':   voice_total_seconds,
            'total_minutes':   voice_total_seconds // 60,
            'total_hours':     round(voice_total_seconds / 3600, 1),
            'total_sessions':  int(voice_totals['total_sessions']   or 0) if voice_totals else 0,
            'distinct_users':  int(voice_totals['distinct_users']    or 0) if voice_totals else 0,
            'distinct_channels': int(voice_totals['distinct_channels'] or 0) if voice_totals else 0,
            'top_users': [
                {'user_id': str(r['user_id']),
                 'username': r['username'] or f"User {r['user_id']}",
                 'minutes': int((r['seconds'] or 0) // 60),
                 'sessions': int(r['sessions'] or 0)}
                for r in voice_top_users
            ],
            'top_channels': voice_top_channel_list,
            'timeseries':   voice_timeseries,
        },

        # ── Engagement, split into two independent sections ───────────────
        'community_points': {
            'total_raids':     raid_stats['total_raids'] or 0,
            'active_raids':    raid_stats['active_raids'] or 0,
            'points_offered':  raid_stats['total_points_offered'] or 0,
            'participants':    comm_part['participants'] or 0,
            'points_awarded':  comm_part['points_awarded'] or 0,
            'participations':  comm_part['participations'] or 0,
            'top_holders': [
                {'user_id': str(r['user_id']),
                 'username': r['username'] or f"User {r['user_id']}",
                 'points': int(r['total_points'] or 0),
                 'raids_completed': int(r['raids_completed'] or 0)}
                for r in comm_holders
            ],
        },
        'engage_engagement': {
            'total_submissions':  int(e4e_submissions['total']  or 0) if e4e_submissions else 0,
            'active_submissions': int(e4e_submissions['active'] or 0) if e4e_submissions else 0,
            'total_claims':       int(e4e_claims['total'] or 0) if e4e_claims else 0,
            'total_points':       int(e4e_points_row['points_balance'] or 0) if e4e_points_row else 0,
            'points_from_actions': int(e4e_claims['points_from_actions'] or 0) if e4e_claims else 0,
            'total_engaged':      int(e4e_points_row['total_engaged']   or 0) if e4e_points_row else 0,
            'total_submitted':    int(e4e_points_row['total_submitted'] or 0) if e4e_points_row else 0,
            'engagers':           int(e4e_participants['engagers']      or 0) if e4e_participants else 0,
            'contributors':       int(e4e_contributors['contributors']  or 0) if e4e_contributors else 0,
            'top_engagers': [
                {'user_id': str(r['user_id']),
                 'username': r['username'] or f"User {r['user_id']}",
                 'claims':   int(r['claims'] or 0),
                 'points':   int(r['points'] or 0)}
                for r in e4e_top_engagers
            ],
            'top_contributors': [
                {'user_id': str(r['user_id']),
                 'username': r['username'] or f"User {r['user_id']}",
                 'submissions': int(r['submissions'] or 0)}
                for r in e4e_top_contributors
            ],
            'pools': [
                {'pool_id':     int(r['pool_id']),
                 'name':        r['name'] or 'Pool',
                 'submissions': int(r['submissions'] or 0),
                 'claims':      int(r['claims']      or 0),
                 'points':      int(r['points']      or 0)}
                for r in e4e_per_pool
            ],
            'timeseries':  e4e_series,
        },
        'stat_cards': {
            'total_members':      live_count,
            'today_joins':        today_joins,
            'week_joins':         week_joins,
            'month_joins':        month_joins,
            'verified_count':     live_verified,
            'messages_today':     today_msgs,
            'messages_week':      week_msgs,
            'messages_month':     month_msgs,
            'messages_year':      year_msgs,
            'messages_avg_per_day': avg_per_day,
        },
    }


@app.get('/api/servers/{server_id}/config')
async def get_server_config(server_id: int, user: dict = Depends(get_current_user)):
    """Return all config key/value pairs for this guild (defaults merged with overrides)."""
    require_guild_admin(user, server_id)
    result = db_get_all_config(server_id)
    user_id = int(user.get('user_id') or user.get('id') or 0)
    bot_instance = _get_bot_instance()
    accessible = [m for m in MODULES if user_can_access_module(server_id, user_id, m, bot_instance)]
    result['user_accessible_modules'] = accessible
    return result


@app.post('/api/servers/{server_id}/config')
async def set_server_config(server_id: int, body: dict, user: dict = Depends(get_current_user)):
    """Update one or more config values for this guild. Body: {"key": "value", ...}"""
    require_guild_admin(user, server_id)
    for key, value in body.items():
        db_set_config(server_id, key, str(value))
    return {'updated': list(body.keys())}

# ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ──
#  PROTECTION ENDPOINTS
# ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ──

@app.get('/api/servers/{server_id}/protection/stats')
async def protection_stats(server_id: int, user: dict = Depends(get_current_user)):
    require_module_access(user, server_id, 'protection')
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT action_type, COUNT(*) as count
            FROM protection_actions
            WHERE guild_id=?
            GROUP BY action_type
            ORDER BY count DESC
        """, (server_id,)).fetchall()

        week_rows = conn.execute("""
            SELECT action_type, COUNT(*) as count
            FROM protection_actions
            WHERE guild_id=? AND created_at >= date('now','-7 days')
            GROUP BY action_type
        """, (server_id,)).fetchall()

    all_time = {r['action_type']: r['count'] for r in rows}
    last_week = {r['action_type']: r['count'] for r in week_rows}

    return {
        'all_time':  all_time,
        'last_week': last_week,
        'totals': {
            'messages_deleted': (
                all_time.get('link_delete', 0) +
                all_time.get('phishing_delete', 0) +
                all_time.get('banned_word', 0)
            ),
            'users_flagged':   all_time.get('suspicious_flag', 0),
            'users_kicked':    all_time.get('suspicious_kick', 0),
            'users_banned':    all_time.get('suspicious_ban', 0),
            'spam_mutes':      all_time.get('spam_mute', 0),
            'phishing_deleted':all_time.get('phishing_delete', 0),
            'raids_blocked':   all_time.get('anti_raid_lockdown', 0),
            'banned_words_hit':all_time.get('banned_word', 0),
        },
        'week_totals': {
            'messages_deleted': (
                last_week.get('link_delete', 0) +
                last_week.get('phishing_delete', 0) +
                last_week.get('banned_word', 0)
            ),
            'users_flagged':    last_week.get('suspicious_flag', 0),
            'spam_mutes':       last_week.get('spam_mute', 0),
            'phishing_deleted': last_week.get('phishing_delete', 0),
            'raids_blocked':    last_week.get('anti_raid_lockdown', 0),
        },
    }


@app.post('/api/servers/{server_id}/protection/send-message')
async def protection_send_message(server_id: int, user: dict = Depends(get_current_user)):
    """Send the configured protection embed to the configured channel."""
    require_module_access(user, server_id, 'protection')

    if not bot.is_ready():
        raise HTTPException(status_code=503, detail='Bot not ready yet')

    guild = bot.get_guild(server_id)
    if guild is None:
        raise HTTPException(status_code=404, detail='Bot is not in this server')

    title       = db_get_config(server_id, 'protection_main_embed_title',       '🛡️ Server Protection')
    description = db_get_config(server_id, 'protection_main_embed_description', 'This server is protected by AVbot.')
    ch_value    = db_get_config(server_id, 'protection_main_embed_channel',     '') or ''

    if not ch_value.strip():
        raise HTTPException(status_code=400, detail='protection_main_embed_channel is not configured')

    # Resolve channel by ID or name
    channel = None
    try:
        channel = guild.get_channel(int(ch_value.strip()))
    except (ValueError, TypeError):
        pass
    if channel is None:
        channel = discord.utils.get(guild.text_channels, name=ch_value.strip())

    if channel is None:
        raise HTTPException(status_code=400, detail=f'Channel not found: {ch_value}')

    embed = discord.Embed(
        title=title,
        description=description,
        color=0x94730D,
    )
    embed.set_footer(text='AmeretaVerse • Protection System')

    try:
        msg = await channel.send(embed=embed)
    except discord.Forbidden:
        raise HTTPException(status_code=400, detail='Bot lacks permission to send in that channel')
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {'ok': True, 'message_id': str(msg.id), 'channel_id': str(channel.id), 'channel_name': channel.name}


@app.get('/api/servers/{server_id}/protection/log')
async def protection_log(
    server_id: int,
    limit: int = 50,
    user: dict = Depends(get_current_user),
):
    require_module_access(user, server_id, 'protection')
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT id, action_type, user_id, detail, created_at
            FROM protection_actions
            WHERE guild_id=?
            ORDER BY created_at DESC
            LIMIT ?
        """, (server_id, min(limit, 200))).fetchall()
    return [dict(r) for r in rows]

# ── Unified logs + flags ──────────────────────────────────────────────────────

@app.get('/api/servers/{server_id}/logs')
async def logs_list(
    server_id: int,
    category: Optional[str] = None,
    module: Optional[str] = None,
    severity: Optional[str] = None,
    search: Optional[str] = None,
    target_user_id: Optional[str] = None,
    actor_user_id: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    user: dict = Depends(get_current_user),
):
    require_module_access(user, server_id, 'logs')
    limit  = max(1, min(int(limit or 50), 500))
    offset = max(0, int(offset or 0))
    events = list_events(
        server_id,
        category=category, module=module, severity=severity,
        search=search,
        target_user_id=target_user_id or None,
        actor_user_id=actor_user_id or None,
        since_iso=since, until_iso=until,
        limit=limit, offset=offset,
    )
    total = count_events(
        server_id,
        category=category, module=module, severity=severity,
        search=search,
        target_user_id=target_user_id or None,
        actor_user_id=actor_user_id or None,
        since_iso=since, until_iso=until,
    )
    return {'events': events, 'total': total, 'limit': limit, 'offset': offset}


@app.get('/api/servers/{server_id}/logs/export')
async def logs_export(
    server_id: int,
    category: Optional[str] = None,
    module: Optional[str] = None,
    user: dict = Depends(get_current_user),
):
    require_module_access(user, server_id, 'logs')
    import io as _io
    import csv as _csv
    from fastapi.responses import Response as _Response

    events = list_events(server_id, category=category, module=module, limit=1000)
    buf = _io.StringIO()
    writer = _csv.DictWriter(buf, fieldnames=[
        'created_at', 'category', 'event_type', 'module', 'severity',
        'actor_username', 'target_username', 'summary',
    ])
    writer.writeheader()
    for e in events:
        writer.writerow({
            'created_at':      e.get('created_at') or '',
            'category':        e.get('category') or '',
            'event_type':      e.get('event_type') or '',
            'module':          e.get('module') or '',
            'severity':        e.get('severity') or '',
            'actor_username':  e.get('actor_username') or '',
            'target_username': e.get('target_username') or '',
            'summary':         e.get('summary') or '',
        })

    return _Response(
        content='﻿' + buf.getvalue(),  # BOM so Excel reads UTF-8 cleanly
        media_type='text/csv',
        headers={'Content-Disposition': f'attachment; filename="logs_{server_id}.csv"'},
    )


@app.get('/api/servers/{server_id}/flags')
async def flags_list(
    server_id: int,
    status: str = 'active',
    module: Optional[str] = None,
    user: dict = Depends(get_current_user),
):
    require_module_access(user, server_id, 'logs')
    return list_flags(server_id, status=status, module=module)


@app.post('/api/servers/{server_id}/flags/{flag_id}/resolve')
async def flag_resolve(
    server_id: int,
    flag_id: int,
    body: dict,
    user: dict = Depends(get_current_user),
):
    require_module_access(user, server_id, 'logs')
    note = (body.get('note') or '').strip() or None
    actor_id = user.get('user_id') or user.get('id') or 0
    ok = resolve_flag(flag_id, actor_id, note)
    if not ok:
        raise HTTPException(status_code=404, detail='Flag not found')
    return {'ok': True}


@app.get('/api/servers/{server_id}/flagged')
async def flagged_users(server_id: int, user: dict = Depends(get_current_user)):
    """Flagged raid participants for this guild."""
    require_module_access(user, server_id, 'flagged')
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT u.user_id, u.username, u.x_username,
                   COUNT(*) as flag_count,
                   MAX(rp.confirmed_at) as last_flagged,
                   GROUP_CONCAT(rp.raid_id) as flagged_raids
            FROM raid_participation rp
            JOIN users u ON u.user_id = rp.user_id
            WHERE rp.flagged = 1
            GROUP BY rp.user_id
            ORDER BY flag_count DESC
            LIMIT 100
        """).fetchall()
    result = []
    for r in rows:
        rd = dict(r)
        rd['flagged_raids'] = (
            [int(x) for x in rd['flagged_raids'].split(',')]
            if rd['flagged_raids'] else []
        )
        result.append(rd)
    return result


def _module_from_action(action_type: str) -> str:
    if action_type in ('link_delete', 'phishing_delete'):
        return 'protection.links'
    if action_type == 'banned_word':
        return 'protection.words'
    if action_type.startswith('suspicious_'):
        return 'protection.suspicious'
    if action_type == 'spam_mute':
        return 'protection.spam'
    if action_type == 'anti_raid_lockdown':
        return 'protection.anti-raid'
    return action_type


@app.get('/api/servers/{server_id}/audit-log')
async def audit_log(
    server_id: int,
    limit: int = 50,
    user: dict = Depends(get_current_user),
):
    """
    Normalized view of protection_actions — field names match what the frontend expects.
    Does NOT replace /protection/log; that endpoint remains unchanged.
    """
    require_module_access(user, server_id, 'audit_log')
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT id, action_type, user_id, detail, created_at
            FROM protection_actions
            WHERE guild_id=?
            ORDER BY created_at DESC
            LIMIT ?
        """, (server_id, min(limit, 200))).fetchall()
    return [
        {
            'id':     r['id'],
            'action': r['action_type'],
            'module': _module_from_action(r['action_type']),
            'target': str(r['user_id']) if r['user_id'] else '',
            'detail': r['detail'],
            'time':   r['created_at'],
        }
        for r in rows
    ]

# ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ──
#  ENGAGEMENT ENDPOINTS
# ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ──

@app.get('/api/servers/{server_id}/raids')
async def list_raids(
    server_id: int,
    active_only: bool = False,
    user: dict = Depends(get_current_user),
):
    require_guild_admin(user, server_id)
    with get_connection() as conn:
        query = 'SELECT * FROM raids'
        if active_only:
            query += ' WHERE active=1'
        query += ' ORDER BY created_at DESC LIMIT 50'
        rows = conn.execute(query).fetchall()

        result = []
        for r in rows:
            rd = dict(r)
            # participation summary
            p = conn.execute("""
                SELECT COUNT(DISTINCT user_id) as participants,
                       SUM(points_earned) as total_points_given
                FROM raid_participation WHERE raid_id=?
            """, (rd['raid_id'],)).fetchone()
            rd['participants']        = p['participants'] or 0
            rd['total_points_given']  = p['total_points_given'] or 0
            result.append(rd)
    return result


@app.get('/api/servers/{server_id}/engage')
async def engage_stats(server_id: int, user: dict = Depends(get_current_user)):
    require_guild_admin(user, server_id)
    with get_connection() as conn:
        links = conn.execute("""
            SELECT l.link_id, l.user_id, u.username, l.tweet_link,
                   l.submitted_at, l.expires_at, l.active,
                   COUNT(p.id) as engagement_count,
                   COALESCE(SUM(p.points_earned),0) as points_distributed
            FROM engage_links l
            LEFT JOIN users u ON u.user_id = l.user_id
            LEFT JOIN engage_participation p ON p.link_id = l.link_id
            GROUP BY l.link_id
            ORDER BY l.submitted_at DESC
            LIMIT 50
        """).fetchall()

        totals = conn.execute("""
            SELECT COUNT(*) as total_links,
                   SUM(CASE WHEN active=1 THEN 1 ELSE 0 END) as active_links,
                   (SELECT COUNT(*) FROM engage_participation) as total_engagements,
                   (SELECT COALESCE(SUM(points_earned),0) FROM engage_participation) as total_points
            FROM engage_links
        """).fetchone()

    return {
        'totals': dict(totals),
        'links':  [dict(r) for r in links],
    }


@app.get('/api/servers/{server_id}/leaderboard')
async def leaderboard(
    server_id: int,
    limit: int = 25,
    user: dict = Depends(get_current_user),
):
    require_guild_admin(user, server_id)
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT user_id, username, total_points,
                   engage_points, creator_engage_points
            FROM users
            ORDER BY total_points DESC
            LIMIT ?
        """, (min(limit, 100),)).fetchall()
    return [dict(r) for r in rows]

# ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ──
#  TICKETS ENDPOINTS
# ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ──

@app.post('/api/servers/{server_id}/tickets/send-panel')
async def tickets_send_panel(server_id: int, user: dict = Depends(get_current_user)):
    """Post the Open Ticket panel embed to the configured panel channel."""
    require_module_access(user, server_id, 'tickets')

    if not bot.is_ready():
        raise HTTPException(status_code=503, detail='Bot not ready yet')

    guild = bot.get_guild(server_id)
    if guild is None:
        raise HTTPException(status_code=404, detail='Bot is not in this server')

    panel_ch_val = (db_get_config(server_id, 'tickets_panel_channel', '') or '').strip()
    if not panel_ch_val:
        raise HTTPException(status_code=400, detail='tickets_panel_channel is not configured')

    channel = None
    try:
        channel = guild.get_channel(int(panel_ch_val))
    except (ValueError, TypeError):
        pass
    if channel is None:
        channel = discord.utils.get(guild.text_channels, name=panel_ch_val)
    if channel is None:
        raise HTTPException(status_code=400, detail=f'Channel not found: {panel_ch_val}')

    title   = db_get_config(server_id, 'tickets_panel_title',       'Support Tickets') or 'Support Tickets'
    desc    = db_get_config(server_id, 'tickets_panel_description',  'Click below to open a ticket.') or ''
    btn_lbl = db_get_config(server_id, 'tickets_panel_button_label', 'Open Ticket') or 'Open Ticket'

    from cogs._branding import build_branded_embed
    from cogs.tickets import OpenTicketView
    embed = build_branded_embed(
        server_id,
        title=title,
        description=desc,
        cog_prefix='tickets',
        use_thumbnail=True,
        use_image=True,
        use_footer=True,
    )

    view = OpenTicketView(button_label=btn_lbl)

    try:
        msg = await channel.send(embed=embed, view=view)
    except discord.Forbidden:
        raise HTTPException(status_code=400, detail='Bot lacks permission to send in that channel')
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    bot.add_view(view, message_id=msg.id)
    print(f'[tickets] registered persistent view for new panel msg {msg.id}')

    return {'ok': True, 'message_id': str(msg.id), 'channel_id': str(channel.id), 'channel_name': channel.name}


@app.get('/api/servers/{server_id}/tickets/list')
async def list_tickets(
    server_id: int,
    status: str = 'open',
    limit: int = 50,
    user: dict = Depends(get_current_user),
):
    """Return tickets for this guild, filtered by status."""
    require_module_access(user, server_id, 'tickets')
    limit = min(limit, 200)
    with get_connection() as conn:
        if status == 'all':
            rows = conn.execute(
                "SELECT * FROM tickets WHERE guild_id=? ORDER BY opened_at DESC LIMIT ?",
                (server_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM tickets WHERE guild_id=? AND status=? ORDER BY opened_at DESC LIMIT ?",
                (server_id, status, limit),
            ).fetchall()
    return [dict(r) for r in rows]


@app.get('/api/servers/{server_id}/tickets/stats')
async def tickets_stats(server_id: int, user: dict = Depends(get_current_user)):
    """Aggregate ticket metrics for this guild."""
    require_module_access(user, server_id, 'tickets')
    with get_connection() as conn:
        open_count = conn.execute(
            "SELECT COUNT(*) FROM tickets WHERE guild_id=? AND status='open'",
            (server_id,),
        ).fetchone()[0]

        closed_today = conn.execute(
            "SELECT COUNT(*) FROM tickets WHERE guild_id=? AND status='closed' "
            "AND closed_at >= date('now')",
            (server_id,),
        ).fetchone()[0]

        closed_week = conn.execute(
            "SELECT COUNT(*) FROM tickets WHERE guild_id=? AND status='closed' "
            "AND closed_at >= date('now','-7 days')",
            (server_id,),
        ).fetchone()[0]

        avg_row = conn.execute(
            "SELECT AVG((julianday(closed_at) - julianday(opened_at)) * 24) "
            "FROM tickets WHERE guild_id=? AND status='closed' AND closed_at IS NOT NULL",
            (server_id,),
        ).fetchone()[0]

        oldest_row = conn.execute(
            "SELECT MAX((julianday('now') - julianday(opened_at)) * 24) "
            "FROM tickets WHERE guild_id=? AND status='open'",
            (server_id,),
        ).fetchone()[0]

    return {
        'open_count':              open_count,
        'closed_today':            closed_today,
        'closed_week':             closed_week,
        'avg_open_duration_hours': round(avg_row,    1) if avg_row    else 0,
        'oldest_open_age_hours':   round(oldest_row, 1) if oldest_row else 0,
    }


# ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ──
#  VERIFY ENDPOINTS
# ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ──

class _VerifySendBody(BaseModel):
    channel_id: str


@app.post('/api/servers/{server_id}/verify/send-message')
async def verify_send_message(
    server_id: int,
    body: _VerifySendBody,
    user: dict = Depends(get_current_user),
):
    """Post the verification embed with Verify button to the specified channel."""
    require_module_access(user, server_id, 'verify')

    if not bot.is_ready():
        raise HTTPException(status_code=503, detail='Bot not ready yet')
    guild = bot.get_guild(server_id)
    if guild is None:
        raise HTTPException(status_code=404, detail='Bot is not in this server')

    from cogs._utils import resolve_channel
    channel = resolve_channel(guild, body.channel_id)
    if channel is None:
        raise HTTPException(status_code=400, detail=f'Channel not found: {body.channel_id}')

    title        = db_get_config(server_id, 'verify_embed_title')        or '🔒 Verify to Enter'
    description  = db_get_config(server_id, 'verify_embed_description')  or 'Click the button below and solve the CAPTCHA to access the server.'
    button_label = db_get_config(server_id, 'verify_embed_button_label') or 'Verify'

    from cogs._branding import build_branded_embed
    from cogs.verify import VerifyView
    embed = build_branded_embed(
        server_id,
        title=title,
        description=description,
        cog_prefix='verify',
        use_thumbnail=True,
        use_image=True,
        use_footer=True,
    )

    view = VerifyView(button_label=button_label)

    try:
        msg = await channel.send(embed=embed, view=view)
    except discord.Forbidden:
        raise HTTPException(status_code=400, detail='Bot lacks permission to send in that channel')
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    bot.add_view(view, message_id=msg.id)
    print(f'[verify] registered persistent view for new panel msg {msg.id}')

    return {'ok': True, 'message_id': str(msg.id), 'channel_id': str(channel.id)}


# ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ──
#  ROLE SELECT ENDPOINTS
# ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ──

# ── Pydantic request models ───────────────────────────────────────────────────

class _PanelCreate(BaseModel):
    title: str = '🎯 Role Selection'
    description: str = ''
    style: str = 'buttons'
    thumbnail_url: str = ''
    image_url: str = ''
    color: str = ''
    footer_text: str = ''


class _PanelUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    style: Optional[str] = None
    channel_id: Optional[str] = None
    thumbnail_url: Optional[str] = None
    image_url: Optional[str] = None
    color: Optional[str] = None
    footer_text: Optional[str] = None


class _ButtonCreate(BaseModel):
    label: str = 'Click me'
    emoji: str = ''
    role: str
    mode: str = 'toggle'
    confirm_give_enabled: int = 0
    confirm_give_message: str = 'Are you sure you want this role?'
    confirm_take_enabled: int = 0
    confirm_take_message: str = 'Are you sure you want to remove this role?'
    dm_give_enabled: int = 0
    dm_give_message: str = 'You received the {role} role in {server}.'
    dm_take_enabled: int = 0
    dm_take_message: str = 'You no longer have the {role} role in {server}.'


class _ButtonUpdate(BaseModel):
    label: Optional[str] = None
    emoji: Optional[str] = None
    role: Optional[str] = None
    mode: Optional[str] = None
    confirm_give_enabled: Optional[int] = None
    confirm_give_message: Optional[str] = None
    confirm_take_enabled: Optional[int] = None
    confirm_take_message: Optional[str] = None
    dm_give_enabled: Optional[int] = None
    dm_give_message: Optional[str] = None
    dm_take_enabled: Optional[int] = None
    dm_take_message: Optional[str] = None


class _SendPanelBody(BaseModel):
    channel_id: str


# ── Panel ownership guard ─────────────────────────────────────────────────────

def _get_rs_panel(panel_id: int, server_id: int) -> dict:
    panel = db_get_panel(panel_id)
    if panel is None:
        raise HTTPException(status_code=404, detail='Panel not found')
    if panel['guild_id'] != server_id:
        raise HTTPException(status_code=403, detail='Panel does not belong to this server')
    return panel


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get('/api/servers/{server_id}/roleselect/panels')
async def rs_list_panels(server_id: int, user: dict = Depends(get_current_user)):
    require_module_access(user, server_id, 'roleselect')
    panels = db_get_panels(server_id)
    for panel in panels:
        panel['buttons'] = db_get_buttons(panel['panel_id'])
    return {'panels': panels}


@app.post('/api/servers/{server_id}/roleselect/panels')
async def rs_create_panel(
    server_id: int,
    body: _PanelCreate,
    user: dict = Depends(get_current_user),
):
    require_module_access(user, server_id, 'roleselect')
    if body.style not in ('buttons', 'dropdown'):
        raise HTTPException(status_code=400, detail="style must be 'buttons' or 'dropdown'")
    panel_id = db_create_panel(server_id, body.title, body.description, body.style)
    panel = db_get_panel(panel_id)
    panel['buttons'] = []
    return panel


@app.patch('/api/servers/{server_id}/roleselect/panels/{panel_id}')
async def rs_update_panel(
    server_id: int,
    panel_id: int,
    body: _PanelUpdate,
    user: dict = Depends(get_current_user),
):
    require_module_access(user, server_id, 'roleselect')
    _get_rs_panel(panel_id, server_id)
    updates = {k: v for k, v in body.model_dump(exclude_none=True).items()}
    if 'style' in updates and updates['style'] not in ('buttons', 'dropdown'):
        raise HTTPException(status_code=400, detail="style must be 'buttons' or 'dropdown'")
    # Convert channel_id string to int for DB (drop silently if not numeric — name lookup
    # only happens at send time via resolve_channel)
    if 'channel_id' in updates:
        chid_str = str(updates['channel_id']).strip()
        if chid_str.isdigit():
            updates['channel_id'] = int(chid_str)
        else:
            del updates['channel_id']
    if updates:
        db_update_panel(panel_id, **updates)
    panel = db_get_panel(panel_id)
    panel['buttons'] = db_get_buttons(panel_id)
    return panel


@app.delete('/api/servers/{server_id}/roleselect/panels/{panel_id}')
async def rs_delete_panel(
    server_id: int,
    panel_id: int,
    user: dict = Depends(get_current_user),
):
    require_module_access(user, server_id, 'roleselect')
    panel = _get_rs_panel(panel_id, server_id)

    if panel.get('message_id') and panel.get('channel_id') and bot.is_ready():
        guild = bot.get_guild(server_id)
        if guild:
            channel = guild.get_channel(int(panel['channel_id']))
            if channel:
                try:
                    msg = await channel.fetch_message(int(panel['message_id']))
                    await msg.delete()
                except Exception:
                    pass

    db_delete_panel(panel_id)
    return {'ok': True}


@app.post('/api/servers/{server_id}/roleselect/panels/{panel_id}/buttons')
async def rs_create_button(
    server_id: int,
    panel_id: int,
    body: _ButtonCreate,
    user: dict = Depends(get_current_user),
):
    require_module_access(user, server_id, 'roleselect')
    _get_rs_panel(panel_id, server_id)
    if body.mode not in ('give', 'take', 'toggle'):
        raise HTTPException(status_code=400, detail="mode must be 'give', 'take', or 'toggle'")
    existing = db_get_buttons(panel_id)
    if len(existing) >= 20:
        raise HTTPException(status_code=400, detail='Maximum 20 buttons per panel')
    button_id = db_create_button(panel_id, **body.model_dump())
    return db_get_button(button_id)


@app.patch('/api/servers/{server_id}/roleselect/panels/{panel_id}/buttons/{button_id}')
async def rs_update_button(
    server_id: int,
    panel_id: int,
    button_id: int,
    body: _ButtonUpdate,
    user: dict = Depends(get_current_user),
):
    require_module_access(user, server_id, 'roleselect')
    _get_rs_panel(panel_id, server_id)
    btn = db_get_button(button_id)
    if btn is None or btn['panel_id'] != panel_id:
        raise HTTPException(status_code=404, detail='Button not found on this panel')
    updates = {k: v for k, v in body.model_dump(exclude_none=True).items()}
    if 'mode' in updates and updates['mode'] not in ('give', 'take', 'toggle'):
        raise HTTPException(status_code=400, detail="mode must be 'give', 'take', or 'toggle'")
    if updates:
        db_update_button(button_id, **updates)
    return db_get_button(button_id)


@app.delete('/api/servers/{server_id}/roleselect/panels/{panel_id}/buttons/{button_id}')
async def rs_delete_button(
    server_id: int,
    panel_id: int,
    button_id: int,
    user: dict = Depends(get_current_user),
):
    require_module_access(user, server_id, 'roleselect')
    _get_rs_panel(panel_id, server_id)
    btn = db_get_button(button_id)
    if btn is None or btn['panel_id'] != panel_id:
        raise HTTPException(status_code=404, detail='Button not found on this panel')
    db_delete_button(button_id)
    return {'ok': True}


@app.post('/api/servers/{server_id}/roleselect/panels/{panel_id}/send')
async def rs_send_panel(
    server_id: int,
    panel_id: int,
    body: _SendPanelBody,
    user: dict = Depends(get_current_user),
):
    require_module_access(user, server_id, 'roleselect')
    panel = _get_rs_panel(panel_id, server_id)

    if not bot.is_ready():
        raise HTTPException(status_code=503, detail='Bot not ready yet')
    guild = bot.get_guild(server_id)
    if guild is None:
        raise HTTPException(status_code=404, detail='Bot is not in this server')

    from cogs._utils import resolve_channel
    from cogs.roleselect import build_panel_view
    from cogs._branding import build_branded_embed

    channel = resolve_channel(guild, body.channel_id)
    if channel is None:
        raise HTTPException(status_code=400, detail=f'Channel not found: {body.channel_id}')

    embed = build_branded_embed(
        server_id,
        title=panel['title'],
        description=panel['description'] or '',
        cog_prefix='roleselect',
        use_thumbnail=not bool(panel.get('thumbnail_url')),
        use_image=not bool(panel.get('image_url')),
        use_footer=not bool(panel.get('footer_text')),
    )
    if panel.get('thumbnail_url'):
        embed.set_thumbnail(url=panel['thumbnail_url'])
    if panel.get('image_url'):
        embed.set_image(url=panel['image_url'])
    if panel.get('footer_text'):
        embed.set_footer(text=panel['footer_text'])
    if panel.get('color'):
        try:
            embed.color = discord.Color(int(panel['color'].lstrip('#'), 16))
        except (ValueError, AttributeError):
            pass
    view = build_panel_view(panel_id)

    # Edit the existing Discord message only if it's in the same channel the user is targeting
    existing_channel_id = panel.get('channel_id')
    if panel.get('message_id') and existing_channel_id and int(existing_channel_id) == channel.id:
        existing_ch = guild.get_channel(int(existing_channel_id))
        if existing_ch:
            try:
                msg = await existing_ch.fetch_message(int(panel['message_id']))
                await msg.edit(embed=embed, view=view)
                db_update_panel(panel_id, channel_id=channel.id, message_id=msg.id)
                return {'ok': True, 'message_id': str(msg.id), 'channel_id': str(channel.id)}
            except discord.NotFound:
                print(f'[api] Panel {panel_id} message {panel["message_id"]} not found in Discord, sending fresh')
            except discord.Forbidden:
                print(f'[api] Panel {panel_id}: bot lacks permission to edit existing message')
                raise HTTPException(status_code=400, detail='Bot lacks permission to edit existing message')
            except Exception as e:
                print(f'[api] Panel {panel_id} edit failed: {type(e).__name__}: {e}')

    try:
        msg = await channel.send(embed=embed, view=view)
    except discord.Forbidden:
        raise HTTPException(status_code=400, detail='Bot lacks permission to send in that channel')
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    db_update_panel(panel_id, channel_id=channel.id, message_id=msg.id)
    return {'ok': True, 'message_id': str(msg.id), 'channel_id': str(channel.id)}


@app.post('/api/servers/{server_id}/roleselect/panels/{panel_id}/refresh')
async def rs_refresh_panel(
    server_id: int,
    panel_id: int,
    user: dict = Depends(get_current_user),
):
    require_module_access(user, server_id, 'roleselect')
    panel = _get_rs_panel(panel_id, server_id)

    if not panel.get('message_id') or not panel.get('channel_id'):
        return {'ok': False, 'detail': 'Panel has not been sent yet'}

    if not bot.is_ready():
        raise HTTPException(status_code=503, detail='Bot not ready yet')
    guild = bot.get_guild(server_id)
    if guild is None:
        raise HTTPException(status_code=404, detail='Bot is not in this server')

    channel = guild.get_channel(int(panel['channel_id']))
    if channel is None:
        raise HTTPException(status_code=400, detail='Panel channel no longer exists')

    from cogs.roleselect import build_panel_view
    from cogs._branding import build_branded_embed

    embed = build_branded_embed(
        server_id,
        title=panel['title'],
        description=panel['description'] or '',
        cog_prefix='roleselect',
        use_thumbnail=not bool(panel.get('thumbnail_url')),
        use_image=not bool(panel.get('image_url')),
        use_footer=not bool(panel.get('footer_text')),
    )
    if panel.get('thumbnail_url'):
        embed.set_thumbnail(url=panel['thumbnail_url'])
    if panel.get('image_url'):
        embed.set_image(url=panel['image_url'])
    if panel.get('footer_text'):
        embed.set_footer(text=panel['footer_text'])
    if panel.get('color'):
        try:
            embed.color = discord.Color(int(panel['color'].lstrip('#'), 16))
        except (ValueError, AttributeError):
            pass
    view = build_panel_view(panel_id)

    try:
        msg = await channel.fetch_message(int(panel['message_id']))
        await msg.edit(embed=embed, view=view)
    except discord.NotFound:
        raise HTTPException(status_code=404, detail='Panel message no longer exists in Discord')
    except discord.Forbidden:
        raise HTTPException(status_code=400, detail='Bot lacks permission to edit that message')
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {
        'ok': True,
        'message_id': str(panel['message_id']),
        'channel_id': str(panel['channel_id']),
    }


# ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ──
#  FORMS ENDPOINTS
# ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ──

_VALID_FIELD_TYPES = {'short_text', 'long_text'}


class _FormCreate(BaseModel):
    name: str


class _FormUpdate(BaseModel):
    name: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    button_label: Optional[str] = None
    thumbnail_url: Optional[str] = None
    image_url: Optional[str] = None
    color: Optional[str] = None
    footer_text: Optional[str] = None
    channel_id: Optional[str] = None
    ticket_category: Optional[str] = None
    staff_roles: Optional[str] = None
    ping_role: Optional[str] = None
    approve_role: Optional[str] = None
    approve_dm_enabled: Optional[int] = None
    approve_dm_message: Optional[str] = None
    reject_dm_enabled: Optional[int] = None
    reject_dm_message: Optional[str] = None
    enabled: Optional[int] = None
    auto_close_on_decision: Optional[int] = None


class _FieldCreate(BaseModel):
    label: str
    field_type: str
    placeholder: str = ''
    required: int = 1
    options: str = ''
    max_length: Optional[int] = None
    position: int = 0


class _FieldUpdate(BaseModel):
    position: Optional[int] = None
    label: Optional[str] = None
    field_type: Optional[str] = None
    placeholder: Optional[str] = None
    required: Optional[int] = None
    options: Optional[str] = None
    max_length: Optional[int] = None


class _FormSendBody(BaseModel):
    channel_id: str


def _validate_field_type(ft: str):
    if ft not in _VALID_FIELD_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"field_type must be one of: {', '.join(sorted(_VALID_FIELD_TYPES))}"
        )


def _check_form_owner(form_id: int, server_id: int) -> dict:
    form = db_get_form(form_id, server_id)
    if form is None:
        raise HTTPException(status_code=404, detail='Form not found')
    return form


@app.get('/api/servers/{server_id}/forms')
async def forms_list(server_id: int, user: dict = Depends(get_current_user)):
    require_module_access(user, server_id, 'forms')
    forms = db_list_forms(server_id)
    for f in forms:
        f['fields'] = db_list_form_fields(f['form_id'])
    return {'forms': forms}


@app.post('/api/servers/{server_id}/forms')
async def forms_create(
    server_id: int,
    body: _FormCreate,
    user: dict = Depends(get_current_user),
):
    require_module_access(user, server_id, 'forms')
    if not body.name.strip():
        raise HTTPException(status_code=400, detail='name is required')
    form_id = db_create_form(server_id, body.name.strip())
    form = db_get_form(form_id, server_id)
    form['fields'] = []
    return form


@app.get('/api/servers/{server_id}/forms/{form_id}')
async def forms_get(
    server_id: int,
    form_id: int,
    user: dict = Depends(get_current_user),
):
    require_module_access(user, server_id, 'forms')
    form = _check_form_owner(form_id, server_id)
    form['fields'] = db_list_form_fields(form_id)
    return form


@app.patch('/api/servers/{server_id}/forms/{form_id}')
async def forms_update(
    server_id: int,
    form_id: int,
    body: _FormUpdate,
    user: dict = Depends(get_current_user),
):
    require_module_access(user, server_id, 'forms')
    _check_form_owner(form_id, server_id)
    updates = {k: v for k, v in body.model_dump(exclude_none=True).items()}
    if updates:
        db_update_form(form_id, server_id, **updates)
    form = db_get_form(form_id, server_id)
    form['fields'] = db_list_form_fields(form_id)
    return form


@app.delete('/api/servers/{server_id}/forms/{form_id}')
async def forms_delete(
    server_id: int,
    form_id: int,
    user: dict = Depends(get_current_user),
):
    require_module_access(user, server_id, 'forms')
    _check_form_owner(form_id, server_id)
    db_delete_form(form_id, server_id)
    return {'ok': True}


@app.post('/api/servers/{server_id}/forms/{form_id}/fields')
async def forms_add_field(
    server_id: int,
    form_id: int,
    body: _FieldCreate,
    user: dict = Depends(get_current_user),
):
    require_module_access(user, server_id, 'forms')
    _check_form_owner(form_id, server_id)
    _validate_field_type(body.field_type)
    if not body.label.strip():
        raise HTTPException(status_code=400, detail='label is required')
    if len(body.label) > 45:
        raise HTTPException(status_code=400, detail='label must be ≤ 45 characters')
    if len(body.placeholder) > 100:
        raise HTTPException(status_code=400, detail='placeholder must be ≤ 100 characters')
    field_id = db_create_form_field(
        form_id=form_id,
        position=body.position,
        label=body.label.strip(),
        field_type=body.field_type,
        placeholder=body.placeholder,
        required=body.required,
        options=body.options,
        max_length=body.max_length,
    )
    form = db_get_form(form_id, server_id)
    form['fields'] = db_list_form_fields(form_id)
    return form


@app.patch('/api/servers/{server_id}/forms/{form_id}/fields/{field_id}')
async def forms_update_field(
    server_id: int,
    form_id: int,
    field_id: int,
    body: _FieldUpdate,
    user: dict = Depends(get_current_user),
):
    require_module_access(user, server_id, 'forms')
    _check_form_owner(form_id, server_id)
    updates = {k: v for k, v in body.model_dump(exclude_none=True).items()}
    if 'field_type' in updates:
        _validate_field_type(updates['field_type'])
    if 'label' in updates:
        if not updates['label'].strip():
            raise HTTPException(status_code=400, detail='label is required')
        if len(updates['label']) > 45:
            raise HTTPException(status_code=400, detail='label must be ≤ 45 characters')
    if 'placeholder' in updates and len(updates.get('placeholder', '')) > 100:
        raise HTTPException(status_code=400, detail='placeholder must be ≤ 100 characters')
    if updates:
        db_update_form_field(field_id, form_id, **updates)
    form = db_get_form(form_id, server_id)
    form['fields'] = db_list_form_fields(form_id)
    return form


@app.delete('/api/servers/{server_id}/forms/{form_id}/fields/{field_id}')
async def forms_delete_field(
    server_id: int,
    form_id: int,
    field_id: int,
    user: dict = Depends(get_current_user),
):
    require_module_access(user, server_id, 'forms')
    _check_form_owner(form_id, server_id)
    db_delete_form_field(field_id, form_id)
    form = db_get_form(form_id, server_id)
    form['fields'] = db_list_form_fields(form_id)
    return form


@app.post('/api/servers/{server_id}/forms/{form_id}/send')
async def forms_send(
    server_id: int,
    form_id: int,
    body: _FormSendBody,
    user: dict = Depends(get_current_user),
):
    require_module_access(user, server_id, 'forms')
    form = _check_form_owner(form_id, server_id)

    if not bot.is_ready():
        raise HTTPException(status_code=503, detail='Bot not ready yet')
    guild = bot.get_guild(server_id)
    if guild is None:
        raise HTTPException(status_code=404, detail='Bot is not in this server')

    from cogs._utils import resolve_channel
    from cogs._branding import build_branded_embed
    from cogs.forms import FormApplyButton, _build_panel_embed

    channel = resolve_channel(guild, body.channel_id)
    if channel is None:
        raise HTTPException(status_code=400, detail=f'Channel not found: {body.channel_id}')

    embed = _build_panel_embed(server_id, form)

    view = discord.ui.View(timeout=None)
    view.add_item(FormApplyButton(form_id, form.get('button_label') or 'Apply'))

    # Edit existing message if same channel
    existing_ch_id = form.get('channel_id', '')
    existing_msg_id = form.get('message_id', '')
    if existing_msg_id and existing_ch_id:
        try:
            ex_ch_int = int(existing_ch_id)
            ex_msg_int = int(existing_msg_id)
            if ex_ch_int == channel.id:
                ex_ch = guild.get_channel(ex_ch_int)
                if ex_ch:
                    try:
                        msg = await ex_ch.fetch_message(ex_msg_int)
                        await msg.edit(embed=embed, view=view)
                        db_update_form(form_id, server_id,
                                       channel_id=str(channel.id),
                                       message_id=str(msg.id))
                        return {'ok': True, 'message_id': str(msg.id), 'channel_id': str(channel.id)}
                    except discord.NotFound:
                        pass
                    except discord.Forbidden:
                        raise HTTPException(status_code=400, detail='Bot lacks permission to edit that message')
        except (ValueError, TypeError):
            pass

    try:
        msg = await channel.send(embed=embed, view=view)
    except discord.Forbidden:
        raise HTTPException(status_code=400, detail='Bot lacks permission to send in that channel')
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    db_update_form(form_id, server_id,
                   channel_id=str(channel.id),
                   message_id=str(msg.id))
    return {'ok': True, 'message_id': str(msg.id), 'channel_id': str(channel.id)}


# ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ──
#  EMBED MESSAGE ENDPOINTS
# ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ──
# Unlimited drafts per guild; admin-only via require_module_access('embed_message').
# Every read/write is filtered by (server_id, guild_id from the URL/JWT). The
# Discord-side render goes through build_branded_embed via
# cogs.embed_message.build_embed_from_row so brand consistency is automatic.

class _EmbedCreate(BaseModel):
    title: Optional[str] = ''
    description: Optional[str] = ''
    color: Optional[str] = None  # '#RRGGBB' string OR int; stored as int or NULL
    image_url: Optional[str] = ''
    thumbnail_url: Optional[str] = ''
    fields: Optional[list] = None  # list of {name,value,inline}
    channel_id: Optional[str] = ''


class _EmbedUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    color: Optional[str] = None
    image_url: Optional[str] = None
    thumbnail_url: Optional[str] = None
    fields: Optional[list] = None
    channel_id: Optional[str] = None


class _EmbedSendBody(BaseModel):
    channel_id: Optional[str] = None  # if absent, use the row's stored channel


_EMBED_TITLE_MAX        = 256
_EMBED_DESC_MAX         = 4000
_EMBED_FIELD_NAME_MAX   = 256
_EMBED_FIELD_VALUE_MAX  = 1024
_EMBED_FIELDS_MAX       = 10
_EMBED_TOTAL_MAX        = 6000


def _coerce_embed_color(value) -> int | None:
    """Accept '#RRGGBB', '0xRRGGBB', plain int, or None; return int|None."""
    if value is None or value == '':
        return None
    if isinstance(value, int):
        return value & 0xFFFFFF
    if isinstance(value, str):
        v = value.strip().lower().lstrip('#')
        if v.startswith('0x'):
            v = v[2:]
        if not v:
            return None
        try:
            return int(v, 16) & 0xFFFFFF
        except ValueError:
            raise HTTPException(status_code=400, detail='Invalid color — use #RRGGBB hex')
    raise HTTPException(status_code=400, detail='Invalid color')


def _validate_url(url: str, field: str) -> str:
    """Reject obviously malformed URLs; empty string is allowed (means unset)."""
    if not url:
        return ''
    url = url.strip()
    if not url:
        return ''
    if len(url) > 2048:
        raise HTTPException(status_code=400, detail=f'{field} too long')
    if not (url.startswith('http://') or url.startswith('https://')):
        raise HTTPException(status_code=400, detail=f'{field} must be http(s)://')
    return url


def _validate_fields(fields) -> str:
    """Coerce a fields list to canonical JSON string, enforcing caps. Returns
    a valid JSON array string (possibly '[]'). Total length check happens
    in _validate_embed_totals."""
    if fields is None:
        return '[]'
    if not isinstance(fields, list):
        raise HTTPException(status_code=400, detail='fields must be a list')
    if len(fields) > _EMBED_FIELDS_MAX:
        raise HTTPException(status_code=400,
            detail=f'too many fields (max {_EMBED_FIELDS_MAX})')
    out: list[dict] = []
    for i, f in enumerate(fields):
        if not isinstance(f, dict):
            raise HTTPException(status_code=400, detail=f'field {i} must be an object')
        name  = str(f.get('name')  or '')
        value = str(f.get('value') or '')
        if len(name)  > _EMBED_FIELD_NAME_MAX:
            raise HTTPException(status_code=400, detail=f'field {i} name too long')
        if len(value) > _EMBED_FIELD_VALUE_MAX:
            raise HTTPException(status_code=400, detail=f'field {i} value too long')
        out.append({
            'name':   name,
            'value':  value,
            'inline': bool(f.get('inline')),
        })
    return json.dumps(out)


def _validate_embed_totals(title: str, description: str, fields_str: str) -> None:
    """Enforce Discord's overall 6000-char embed limit (title + desc + all
    field names + all field values)."""
    try:
        flist = json.loads(fields_str or '[]')
    except (ValueError, TypeError):
        flist = []
    total = len(title or '') + len(description or '')
    for f in flist if isinstance(flist, list) else []:
        if isinstance(f, dict):
            total += len(str(f.get('name')  or ''))
            total += len(str(f.get('value') or ''))
    if total > _EMBED_TOTAL_MAX:
        raise HTTPException(
            status_code=400,
            detail=f'Total embed length {total} exceeds Discord limit of {_EMBED_TOTAL_MAX}',
        )


def _embed_row_to_dict(row: dict) -> dict:
    """Hydrate a DB row for the dashboard: parse fields_json, expose color as
    '#RRGGBB', leave id/channel/message ids as strings the JS side can handle."""
    fields_str = row.get('fields_json') or '[]'
    try:
        fields = json.loads(fields_str)
        if not isinstance(fields, list):
            fields = []
    except (ValueError, TypeError):
        fields = []
    color_int = row.get('color')
    color_hex = f'#{int(color_int):06x}' if color_int is not None else None
    return {
        'id':             int(row['id']),
        'guild_id':       str(row['guild_id']),
        'channel_id':     row.get('channel_id') or '',
        'message_id':     row.get('message_id') or '',
        'title':          row.get('title') or '',
        'description':    row.get('description') or '',
        'color':          color_hex,
        'image_url':      row.get('image_url') or '',
        'thumbnail_url':  row.get('thumbnail_url') or '',
        'fields':         fields,
        'created_by':     str(row.get('created_by')) if row.get('created_by') else None,
        'created_at':     row.get('created_at'),
        'updated_at':     row.get('updated_at'),
        'posted_at':      row.get('posted_at'),
        'status':         'posted' if row.get('message_id') else 'draft',
    }


def _check_embed_owner(embed_id: int, server_id: int) -> dict:
    row = db_get_embed_message(embed_id, server_id)
    if row is None:
        raise HTTPException(status_code=404, detail='Embed not found')
    return row


@app.get('/api/servers/{server_id}/embeds')
async def embeds_list(server_id: int, user: dict = Depends(get_current_user)):
    require_module_access(user, server_id, 'embed_message')
    rows = db_list_embed_messages(server_id)
    return {'embeds': [_embed_row_to_dict(r) for r in rows]}


@app.post('/api/servers/{server_id}/embeds')
async def embeds_create(
    server_id: int,
    body: _EmbedCreate,
    user: dict = Depends(get_current_user),
):
    require_module_access(user, server_id, 'embed_message')
    rate_limit(f'embed_create:{server_id}', 30, 60.0)

    title       = (body.title       or '')[:_EMBED_TITLE_MAX]
    description = (body.description or '')[:_EMBED_DESC_MAX]
    color_int   = _coerce_embed_color(body.color)
    image_url   = _validate_url(body.image_url     or '', 'image_url')
    thumb_url   = _validate_url(body.thumbnail_url or '', 'thumbnail_url')
    fields_str  = _validate_fields(body.fields)
    _validate_embed_totals(title, description, fields_str)

    embed_id = db_create_embed_message(
        server_id,
        created_by=int(user.get('user_id') or user.get('id') or 0) or None,
        title=title,
        description=description,
        color=color_int,
        image_url=image_url,
        thumbnail_url=thumb_url,
        fields_json=fields_str,
        channel_id=(body.channel_id or '').strip(),
    )

    log_event(
        server_id, 'admin_action', 'embed_created',
        f'Embed #{embed_id} created by {user.get("username")}',
        actor_user_id=user.get('user_id') or user.get('id'),
        actor_username=user.get('username'),
        module='embed_message', severity='info',
        details={'embed_id': embed_id, 'title': title[:80]},
    )

    row = db_get_embed_message(embed_id, server_id)
    return _embed_row_to_dict(row)


@app.patch('/api/servers/{server_id}/embeds/{embed_id}')
async def embeds_update(
    server_id: int,
    embed_id: int,
    body: _EmbedUpdate,
    user: dict = Depends(get_current_user),
):
    require_module_access(user, server_id, 'embed_message')
    rate_limit(f'embed_update:{server_id}', 120, 60.0)
    row = _check_embed_owner(embed_id, server_id)

    updates: dict = {}
    payload = body.model_dump(exclude_unset=True)

    if 'title' in payload:
        updates['title'] = (payload['title'] or '')[:_EMBED_TITLE_MAX]
    if 'description' in payload:
        updates['description'] = (payload['description'] or '')[:_EMBED_DESC_MAX]
    if 'color' in payload:
        updates['color'] = _coerce_embed_color(payload['color'])
    if 'image_url' in payload:
        updates['image_url'] = _validate_url(payload['image_url'] or '', 'image_url')
    if 'thumbnail_url' in payload:
        updates['thumbnail_url'] = _validate_url(payload['thumbnail_url'] or '', 'thumbnail_url')
    if 'fields' in payload:
        updates['fields_json'] = _validate_fields(payload['fields'])
    if 'channel_id' in payload:
        updates['channel_id'] = (payload['channel_id'] or '').strip() or None

    # Validate aggregate length against the would-be result, not just the diff.
    new_title = updates.get('title',       row.get('title') or '')
    new_desc  = updates.get('description', row.get('description') or '')
    new_fld   = updates.get('fields_json', row.get('fields_json') or '[]')
    _validate_embed_totals(new_title, new_desc, new_fld)

    if updates:
        db_update_embed_message(embed_id, server_id, **updates)

    fresh = db_get_embed_message(embed_id, server_id)

    # If the embed is already posted, push the edits to the live Discord message
    # so dashboard saves flow through automatically. Channel/message stays put;
    # explicit "Resend" is what creates a new message.
    live_edit_result = None
    if fresh and fresh.get('message_id') and fresh.get('channel_id'):
        try:
            if not bot.is_ready():
                live_edit_result = 'bot_not_ready'
            else:
                guild = bot.get_guild(server_id)
                if guild is None:
                    live_edit_result = 'bot_not_in_guild'
                else:
                    try:
                        ch_int  = int(fresh['channel_id'])
                        msg_int = int(fresh['message_id'])
                    except (TypeError, ValueError):
                        ch_int = msg_int = None
                    ch = guild.get_channel(ch_int) if ch_int else None
                    if ch is None:
                        live_edit_result = 'channel_not_found'
                    else:
                        from cogs.embed_message import build_embed_from_row
                        embed = build_embed_from_row(server_id, fresh)
                        try:
                            msg = await ch.fetch_message(msg_int)
                            await msg.edit(embed=embed)
                            live_edit_result = 'edited'
                        except discord.NotFound:
                            # The Discord message was deleted out-of-band — drop
                            # the pointer so the row reverts to draft state.
                            db_update_embed_message(
                                embed_id, server_id,
                                message_id=None, posted_at=None,
                            )
                            fresh = db_get_embed_message(embed_id, server_id)
                            live_edit_result = 'message_missing'
                        except discord.Forbidden:
                            live_edit_result = 'forbidden'
        except Exception as e:  # noqa: BLE001 — never break the save on edit fail
            print(f'[embeds] live-edit failed for embed_id={embed_id} '
                  f'guild={server_id}: {type(e).__name__}: {e}')
            live_edit_result = 'error'

    log_event(
        server_id, 'admin_action', 'embed_edited',
        f'Embed #{embed_id} edited by {user.get("username")}',
        actor_user_id=user.get('user_id') or user.get('id'),
        actor_username=user.get('username'),
        module='embed_message', severity='info',
        details={'embed_id': embed_id, 'live_edit': live_edit_result,
                 'changed': sorted(updates.keys())},
    )

    out = _embed_row_to_dict(fresh) if fresh else {'id': embed_id}
    out['live_edit'] = live_edit_result
    return out


@app.post('/api/servers/{server_id}/embeds/{embed_id}/send')
async def embeds_send(
    server_id: int,
    embed_id: int,
    body: _EmbedSendBody,
    user: dict = Depends(get_current_user),
):
    """Send (or resend) this embed to a channel. If the embed is already
    posted, sending again creates a NEW message; for in-place updates use
    PATCH (which edits the live message)."""
    require_module_access(user, server_id, 'embed_message')
    # Generous per-guild rate cap on a write-to-Discord op; per-user limit
    # blunts a single admin hammering "Send" by accident.
    rate_limit(f'embed_send:{server_id}', 30, 60.0)
    uid = int(user.get('user_id') or user.get('id') or 0)
    if uid:
        rate_limit(f'embed_send_u:{uid}:{server_id}', 15, 60.0)

    row = _check_embed_owner(embed_id, server_id)
    target_ch = (body.channel_id or row.get('channel_id') or '').strip()
    if not target_ch:
        raise HTTPException(status_code=400, detail='No target channel — set one first')

    if not bot.is_ready():
        raise HTTPException(status_code=503, detail='Bot not ready yet')
    guild = bot.get_guild(server_id)
    if guild is None:
        raise HTTPException(status_code=404, detail='Bot is not in this server')

    from cogs._utils import resolve_channel
    channel = resolve_channel(guild, target_ch)
    if channel is None:
        raise HTTPException(status_code=400, detail=f'Channel not found: {target_ch}')

    from cogs.embed_message import build_embed_from_row
    embed = build_embed_from_row(server_id, row)

    try:
        msg = await channel.send(embed=embed)
    except discord.Forbidden:
        raise HTTPException(status_code=400, detail='Bot lacks permission to send in that channel')
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    db_update_embed_message(
        embed_id, server_id,
        channel_id=str(channel.id),
        message_id=str(msg.id),
        posted_at=datetime.now(timezone.utc).isoformat(),
    )

    log_event(
        server_id, 'admin_action', 'embed_posted',
        f'Embed #{embed_id} posted to #{channel.name} by {user.get("username")}',
        actor_user_id=user.get('user_id') or user.get('id'),
        actor_username=user.get('username'),
        module='embed_message', severity='info',
        details={'embed_id': embed_id, 'channel_id': str(channel.id),
                 'message_id': str(msg.id)},
    )

    fresh = db_get_embed_message(embed_id, server_id)
    return _embed_row_to_dict(fresh)


@app.post('/api/servers/{server_id}/embeds/{embed_id}/delete-message')
async def embeds_delete_message(
    server_id: int,
    embed_id: int,
    user: dict = Depends(get_current_user),
):
    """Delete only the live Discord message; keep the draft row so the admin
    can edit and re-send. No-op (200) if the row was never posted."""
    require_module_access(user, server_id, 'embed_message')
    rate_limit(f'embed_dmsg:{server_id}', 30, 60.0)
    row = _check_embed_owner(embed_id, server_id)

    msg_id = row.get('message_id')
    ch_id  = row.get('channel_id')
    if not msg_id or not ch_id:
        return {'ok': True, 'note': 'not_posted'}

    if not bot.is_ready():
        raise HTTPException(status_code=503, detail='Bot not ready yet')
    guild = bot.get_guild(server_id)
    if guild is None:
        raise HTTPException(status_code=404, detail='Bot is not in this server')

    deleted = False
    try:
        ch = guild.get_channel(int(ch_id))
        if ch is not None:
            msg = await ch.fetch_message(int(msg_id))
            await msg.delete()
            deleted = True
    except discord.NotFound:
        deleted = True  # already gone — treat as success
    except discord.Forbidden:
        raise HTTPException(status_code=400, detail='Bot lacks permission to delete that message')
    except (ValueError, TypeError):
        pass
    except Exception as e:  # noqa: BLE001
        print(f'[embeds] delete-message failed embed_id={embed_id} '
              f'guild={server_id}: {type(e).__name__}: {e}')

    # Clear the live-message pointer either way — the row is back to a draft.
    db_update_embed_message(
        embed_id, server_id,
        message_id=None,
        posted_at=None,
    )

    log_event(
        server_id, 'admin_action', 'embed_message_deleted',
        f'Live message for embed #{embed_id} deleted by {user.get("username")}',
        actor_user_id=user.get('user_id') or user.get('id'),
        actor_username=user.get('username'),
        module='embed_message', severity='info',
        details={'embed_id': embed_id, 'discord_deleted': deleted},
    )

    fresh = db_get_embed_message(embed_id, server_id)
    return _embed_row_to_dict(fresh) if fresh else {'id': embed_id, 'status': 'draft'}


@app.delete('/api/servers/{server_id}/embeds/{embed_id}')
async def embeds_delete(
    server_id: int,
    embed_id: int,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Delete the draft row. Pass ?delete_message=true to ALSO delete the
    live Discord message; otherwise the posted message is left intact."""
    require_module_access(user, server_id, 'embed_message')
    row = _check_embed_owner(embed_id, server_id)

    qs_flag = (request.query_params.get('delete_message') or '').lower()
    also_delete_msg = qs_flag in ('1', 'true', 'yes', 'on')

    msg_deleted = None
    if also_delete_msg and row.get('message_id') and row.get('channel_id'):
        if bot.is_ready():
            guild = bot.get_guild(server_id)
            if guild is not None:
                try:
                    ch = guild.get_channel(int(row['channel_id']))
                    if ch is not None:
                        msg = await ch.fetch_message(int(row['message_id']))
                        await msg.delete()
                        msg_deleted = True
                except discord.NotFound:
                    msg_deleted = True
                except discord.Forbidden:
                    msg_deleted = False
                except Exception as e:  # noqa: BLE001
                    print(f'[embeds] full-delete msg failed embed_id={embed_id} '
                          f'guild={server_id}: {type(e).__name__}: {e}')
                    msg_deleted = False

    db_delete_embed_message(embed_id, server_id)

    log_event(
        server_id, 'admin_action', 'embed_deleted',
        f'Embed #{embed_id} deleted by {user.get("username")}',
        actor_user_id=user.get('user_id') or user.get('id'),
        actor_username=user.get('username'),
        module='embed_message', severity='info',
        details={'embed_id': embed_id, 'discord_message_deleted': msg_deleted},
    )

    return {'ok': True, 'discord_message_deleted': msg_deleted}


# ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ──
#  GIVEAWAY ENDPOINTS
# ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ──
# Dashboard CRUD + lifecycle (start / end-now / reroll / cancel). The Discord
# side (cogs/giveaway.py) owns the runtime scheduler, button handlers, and
# winner-draw; this section just lets admins compose, post, and manage the
# embed and entrants list.

# Lock these fields once status='active' so an admin can't shrink the prize
# pool or duration after entrants joined. description/image_url/thumbnail/
# color stay editable so a typo can be corrected mid-flight.
_GIVEAWAY_LOCKED_WHEN_ACTIVE = {
    'duration_seconds', 'winner_count', 'entry_cost_points',
    'allowed_role_ids', 'mention_role_id', 'mention_role_ids', 'channel_id',
    # cost_source is locked once live: flipping the pool after entries are
    # charged would split-brain the cancel refund. entry_tasks stays editable.
    'cost_source',
}

_GIVEAWAY_TITLE_MAX        = 256
_GIVEAWAY_DESC_MAX         = 4000
_GIVEAWAY_PRIZE_MAX        = 512
_GIVEAWAY_MIN_DURATION_S   = 60
_GIVEAWAY_MAX_DURATION_S   = 30 * 24 * 3600  # 30 days
_GIVEAWAY_MAX_WINNER_COUNT = 50


class _GiveawayCreate(BaseModel):
    title: Optional[str] = ''
    description: Optional[str] = ''
    prize: Optional[str] = ''
    image_url: Optional[str] = ''
    thumbnail_url: Optional[str] = ''
    color: Optional[str] = None
    duration_seconds: Optional[int] = 3600
    winner_count: Optional[int] = 1
    entry_cost_points: Optional[int] = 0
    # allowed_role_ids and mention_role_ids accept EITHER a list (each item
    # may itself be a comma/space-separated string) OR a single string —
    # _normalize_role_id_list reduces both shapes to a canonical JSON array.
    # mention_role_id (singular, str) is kept for back-compat with older
    # clients; the backend folds it into mention_role_ids on save.
    allowed_role_ids: Optional[object] = None
    mention_role_ids: Optional[object] = None
    mention_role_id:  Optional[str]    = None
    channel_id: Optional[str] = ''
    # entry_tasks: list of task dicts (twitter_follow/like/retweet,
    # discord_member/role). cost_source: 'community' | 'engage'.
    entry_tasks: Optional[object] = None
    cost_source: Optional[str]    = None


class _GiveawayUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    prize: Optional[str] = None
    image_url: Optional[str] = None
    thumbnail_url: Optional[str] = None
    color: Optional[str] = None
    duration_seconds: Optional[int] = None
    winner_count: Optional[int] = None
    entry_cost_points: Optional[int] = None
    allowed_role_ids: Optional[object] = None
    mention_role_ids: Optional[object] = None
    mention_role_id:  Optional[str]    = None
    channel_id: Optional[str] = None
    entry_tasks: Optional[object] = None
    cost_source: Optional[str]    = None


def _check_giveaway_owner(giveaway_id: int, server_id: int) -> dict:
    g = db_get_giveaway(giveaway_id, server_id)
    if g is None:
        raise HTTPException(status_code=404, detail='Giveaway not found')
    return g


_GIVEAWAY_TASK_TYPES = {
    'twitter_follow', 'twitter_like', 'twitter_retweet',
    'discord_member', 'discord_role',
}
_RE_X_USERNAME = re.compile(r'^[A-Za-z0-9_]{1,15}$')
_RE_SNOWFLAKE  = re.compile(r'^\d{17,19}$')
_RE_TWEET_ID   = re.compile(r'(?:status/)?(\d{10,25})')


def _validate_entry_tasks(value) -> str:
    """Validate + normalize the entry_tasks payload into a JSON string for
    storage. Accepts a list of task dicts. Raises HTTPException(400) with an
    inline-friendly message on any malformed task. Empty/None => '[]'."""
    if value in (None, ''):
        return '[]'
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail='entry_tasks must be a JSON array')
    if not isinstance(value, list):
        raise HTTPException(status_code=400, detail='entry_tasks must be a list')
    if len(value) > 10:
        raise HTTPException(status_code=400, detail='At most 10 entry tasks are allowed')

    out: list[dict] = []
    for idx, t in enumerate(value, 1):
        if not isinstance(t, dict):
            raise HTTPException(status_code=400, detail=f'Task {idx} is not an object')
        ttype  = str(t.get('type', '')).strip()
        target = str(t.get('target', '')).strip()
        label  = str(t.get('label', '') or '').strip()[:200]
        if ttype not in _GIVEAWAY_TASK_TYPES:
            raise HTTPException(status_code=400, detail=f'Task {idx}: unknown type "{ttype}"')
        if not target:
            raise HTTPException(status_code=400, detail=f'Task {idx}: target is required')

        if ttype == 'twitter_follow':
            handle = target.lstrip('@')
            if not _RE_X_USERNAME.match(handle):
                raise HTTPException(status_code=400,
                    detail=f'Task {idx}: "{target}" is not a valid X username')
            target = handle
        elif ttype in ('twitter_like', 'twitter_retweet'):
            if not _RE_TWEET_ID.search(target):
                raise HTTPException(status_code=400,
                    detail=f'Task {idx}: "{target}" is not a valid tweet URL or ID')
        elif ttype == 'discord_member':
            if not _RE_SNOWFLAKE.match(target):
                raise HTTPException(status_code=400,
                    detail=f'Task {idx}: "{target}" is not a valid Discord server ID')
        elif ttype == 'discord_role':
            parts = target.split(':', 1)
            if len(parts) != 2 or not _RE_SNOWFLAKE.match(parts[0].strip()) \
                    or not _RE_SNOWFLAKE.match(parts[1].strip()):
                raise HTTPException(status_code=400,
                    detail=f'Task {idx}: role target must be "serverID:roleID" (both numeric)')
            target = f'{parts[0].strip()}:{parts[1].strip()}'

        out.append({'type': ttype, 'target': target, 'label': label})
    return json.dumps(out)


def _coerce_cost_source(value) -> str:
    v = (str(value or '').strip().lower()) or 'community'
    if v not in ('community', 'engage'):
        raise HTTPException(status_code=400, detail="cost_source must be 'community' or 'engage'")
    return v


def _coerce_giveaway_color(value) -> int | None:
    if value is None or value == '':
        return None
    if isinstance(value, int):
        return value & 0xFFFFFF
    if isinstance(value, str):
        v = value.strip().lower().lstrip('#')
        if v.startswith('0x'):
            v = v[2:]
        if not v:
            return None
        try:
            return int(v, 16) & 0xFFFFFF
        except ValueError:
            raise HTTPException(status_code=400, detail='Invalid color — use #RRGGBB hex')
    raise HTTPException(status_code=400, detail='Invalid color')


def _validate_giveaway_url(url: str, field: str) -> str:
    if not url:
        return ''
    u = url.strip()
    if not u:
        return ''
    if len(u) > 2048:
        raise HTTPException(status_code=400, detail=f'{field} too long')
    if not (u.startswith('http://') or u.startswith('https://')):
        raise HTTPException(status_code=400, detail=f'{field} must be http(s)://')
    return u


import re as _gw_re
# Discord snowflakes are 17-19 digits today; 20-digit allows headroom for
# future ID-space growth without rejecting legitimate IDs.
_ROLE_ID_DIGIT_RE = _gw_re.compile(r'^\d{17,20}$')


def _normalize_role_id_list(values, *, field: str = 'role_ids') -> str:
    """Tolerant parser: accept either a list (items may themselves be
    strings containing several ids) OR a single string. Split on any
    combination of commas, whitespace, and newlines. Trim each token,
    drop empties, validate each remaining token is a 17–20-digit numeric
    Discord ID. Return a canonical JSON-array string."""
    if values is None or values == '':
        return '[]'

    if isinstance(values, str):
        raw_tokens = _gw_re.split(r'[,\s]+', values)
    elif isinstance(values, (list, tuple)):
        raw_tokens: list[str] = []
        for v in values:
            if v is None:
                continue
            s = str(v).strip()
            if not s:
                continue
            raw_tokens.extend(_gw_re.split(r'[,\s]+', s))
    else:
        raise HTTPException(
            status_code=400,
            detail=f'{field} must be a list or a comma-separated string',
        )

    out: list[str] = []
    seen: set = set()
    for tok in raw_tokens:
        s = (tok or '').strip()
        if not s:
            continue
        if not _ROLE_ID_DIGIT_RE.match(s):
            raise HTTPException(
                status_code=400,
                detail=(f'{field} entry "{s}" is not a valid Discord role id '
                        f'(expected 17-20 digits)'),
            )
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
    return json.dumps(out)


def _validate_role_id_list(values) -> str:
    """Back-compat alias for the previous name. New code should call
    _normalize_role_id_list with an explicit field= for cleaner errors."""
    return _normalize_role_id_list(values, field='allowed_role_ids')


def _validate_giveaway_payload(
    *, title: Optional[str], description: Optional[str], prize: Optional[str],
    duration_seconds: Optional[int], winner_count: Optional[int],
    entry_cost_points: Optional[int],
) -> None:
    if title is not None and len(title) > _GIVEAWAY_TITLE_MAX:
        raise HTTPException(status_code=400, detail=f'title too long (max {_GIVEAWAY_TITLE_MAX})')
    if description is not None and len(description) > _GIVEAWAY_DESC_MAX:
        raise HTTPException(status_code=400, detail=f'description too long (max {_GIVEAWAY_DESC_MAX})')
    if prize is not None and len(prize) > _GIVEAWAY_PRIZE_MAX:
        raise HTTPException(status_code=400, detail=f'prize too long (max {_GIVEAWAY_PRIZE_MAX})')
    if duration_seconds is not None:
        if (duration_seconds < _GIVEAWAY_MIN_DURATION_S
                or duration_seconds > _GIVEAWAY_MAX_DURATION_S):
            raise HTTPException(
                status_code=400,
                detail=(f'duration_seconds must be between {_GIVEAWAY_MIN_DURATION_S}s '
                        f'and {_GIVEAWAY_MAX_DURATION_S}s'),
            )
    if winner_count is not None:
        if winner_count < 1 or winner_count > _GIVEAWAY_MAX_WINNER_COUNT:
            raise HTTPException(status_code=400,
                detail=f'winner_count must be 1..{_GIVEAWAY_MAX_WINNER_COUNT}')
    if entry_cost_points is not None and entry_cost_points < 0:
        raise HTTPException(status_code=400, detail='entry_cost_points must be ≥ 0')


def _giveaway_row_to_dict(g: dict) -> dict:
    """Hydrate a DB row for the dashboard: parse JSON columns, expose color
    as '#RRGGBB', stringify snowflakes, mirror entry_count if present."""
    try:
        roles = json.loads(g.get('allowed_role_ids') or '[]')
        if not isinstance(roles, list):
            roles = []
    except (ValueError, TypeError):
        roles = []
    try:
        winners = json.loads(g.get('winners_json') or '[]')
        if not isinstance(winners, list):
            winners = []
    except (ValueError, TypeError):
        winners = []
    # mention_role_ids (new) is the source of truth; if a giveaway predates
    # the column (NULL) or was written without it, fall back to wrapping the
    # legacy single mention_role_id so the response shape stays consistent.
    try:
        mentions = json.loads(g.get('mention_role_ids') or '[]')
        if not isinstance(mentions, list):
            mentions = []
    except (ValueError, TypeError):
        mentions = []
    if not mentions and g.get('mention_role_id'):
        mentions = [str(g['mention_role_id'])]
    mentions = [str(m) for m in mentions if str(m).strip()]
    try:
        entry_tasks = json.loads(g.get('entry_tasks') or '[]')
        if not isinstance(entry_tasks, list):
            entry_tasks = []
    except (ValueError, TypeError):
        entry_tasks = []
    color_int = g.get('color')
    color_hex = f'#{int(color_int):06x}' if color_int is not None else None
    return {
        'id':                int(g['id']),
        'guild_id':          str(g['guild_id']),
        'entry_tasks':       entry_tasks,
        'cost_source':       (g.get('cost_source') or 'community'),
        'channel_id':        g.get('channel_id') or '',
        'message_id':        g.get('message_id') or '',
        'title':             g.get('title') or '',
        'description':       g.get('description') or '',
        'prize':             g.get('prize') or '',
        'image_url':         g.get('image_url') or '',
        'thumbnail_url':     g.get('thumbnail_url') or '',
        'color':             color_hex,
        'duration_seconds':  int(g.get('duration_seconds') or 0),
        'ends_at':           g.get('ends_at'),
        'winner_count':      int(g.get('winner_count') or 1),
        'entry_cost_points': int(g.get('entry_cost_points') or 0),
        'allowed_role_ids':  [str(r) for r in roles],
        'mention_role_id':   (mentions[0] if mentions else None),
        'mention_role_ids':  mentions,
        'status':            g.get('status') or 'draft',
        'created_by':        str(g.get('created_by')) if g.get('created_by') else None,
        'created_at':        g.get('created_at'),
        'started_at':        g.get('started_at'),
        'ended_at':          g.get('ended_at'),
        'winners':           [str(w) for w in winners],
        'entry_count':       int(g.get('entry_count') or 0),
    }


@app.get('/api/servers/{server_id}/giveaways')
async def giveaways_list(server_id: int, user: dict = Depends(get_current_user)):
    require_module_access(user, server_id, 'giveaway')
    rows = db_list_giveaways(server_id)
    return {'giveaways': [_giveaway_row_to_dict(r) for r in rows]}


@app.post('/api/servers/{server_id}/giveaways')
async def giveaways_create(
    server_id: int,
    body: _GiveawayCreate,
    user: dict = Depends(get_current_user),
):
    require_module_access(user, server_id, 'giveaway')
    rate_limit(f'giveaway_create:{server_id}', 30, 60.0)

    _validate_giveaway_payload(
        title=body.title, description=body.description, prize=body.prize,
        duration_seconds=body.duration_seconds, winner_count=body.winner_count,
        entry_cost_points=body.entry_cost_points,
    )

    color_int = _coerce_giveaway_color(body.color)
    image_url = _validate_giveaway_url(body.image_url     or '', 'image_url')
    thumb_url = _validate_giveaway_url(body.thumbnail_url or '', 'thumbnail_url')
    roles_str = _normalize_role_id_list(body.allowed_role_ids, field='allowed_role_ids')

    # Multi-mention: new mention_role_ids takes precedence; legacy single
    # mention_role_id is folded in for back-compat.
    if body.mention_role_ids is not None:
        mention_list_str = _normalize_role_id_list(body.mention_role_ids, field='mention_role_ids')
    elif body.mention_role_id:
        mention_list_str = _normalize_role_id_list(body.mention_role_id, field='mention_role_id')
    else:
        mention_list_str = '[]'
    # Keep the legacy single field populated with the first id (or None) so
    # any older read path that hasn't switched over still works.
    try:
        _first = json.loads(mention_list_str)
        mention_single = _first[0] if isinstance(_first, list) and _first else None
    except (TypeError, ValueError):
        mention_single = None

    gid = db_create_giveaway(
        server_id,
        created_by=int(user.get('user_id') or user.get('id') or 0) or None,
        title=(body.title or '')[:_GIVEAWAY_TITLE_MAX],
        description=(body.description or '')[:_GIVEAWAY_DESC_MAX],
        prize=(body.prize or '')[:_GIVEAWAY_PRIZE_MAX],
        image_url=image_url, thumbnail_url=thumb_url,
        color=color_int,
        duration_seconds=int(body.duration_seconds or 3600),
        winner_count=int(body.winner_count or 1),
        entry_cost_points=int(body.entry_cost_points or 0),
        allowed_role_ids=roles_str,
        mention_role_id=mention_single,
        channel_id=(body.channel_id or '').strip(),
    )
    # Persist the canonical multi-role list (create helper doesn't take it).
    if mention_list_str != '[]':
        db_update_giveaway(gid, server_id, mention_role_ids=mention_list_str)

    # entry_tasks + cost_source (create helper predates these columns).
    _post_updates: dict = {}
    if body.entry_tasks is not None:
        _post_updates['entry_tasks'] = _validate_entry_tasks(body.entry_tasks)
    if body.cost_source is not None:
        _post_updates['cost_source'] = _coerce_cost_source(body.cost_source)
    if _post_updates:
        db_update_giveaway(gid, server_id, **_post_updates)

    log_event(
        server_id, 'admin_action', 'giveaway_created',
        f'Giveaway #{gid} created by {user.get("username")}',
        actor_user_id=user.get('user_id') or user.get('id'),
        actor_username=user.get('username'),
        module='giveaway', severity='info',
        details={'giveaway_id': gid, 'title': (body.title or '')[:80]},
    )

    return _giveaway_row_to_dict(db_get_giveaway(gid, server_id))


@app.patch('/api/servers/{server_id}/giveaways/{giveaway_id}')
async def giveaways_update(
    server_id: int,
    giveaway_id: int,
    body: _GiveawayUpdate,
    user: dict = Depends(get_current_user),
):
    require_module_access(user, server_id, 'giveaway')
    rate_limit(f'giveaway_update:{server_id}', 120, 60.0)
    g = _check_giveaway_owner(giveaway_id, server_id)
    status = g.get('status') or 'draft'

    payload = body.model_dump(exclude_unset=True)
    if status not in ('draft', 'active'):
        raise HTTPException(status_code=400, detail=f'Cannot edit a {status} giveaway')

    if status == 'active':
        rejected = sorted(_GIVEAWAY_LOCKED_WHEN_ACTIVE & set(payload.keys()))
        if rejected:
            raise HTTPException(
                status_code=400,
                detail=(f'These fields are locked while a giveaway is active: '
                        f'{", ".join(rejected)}. Cancel and recreate to change them.'),
            )

    _validate_giveaway_payload(
        title=payload.get('title'),
        description=payload.get('description'),
        prize=payload.get('prize'),
        duration_seconds=payload.get('duration_seconds'),
        winner_count=payload.get('winner_count'),
        entry_cost_points=payload.get('entry_cost_points'),
    )

    updates: dict = {}
    if 'title' in payload:
        updates['title'] = (payload['title'] or '')[:_GIVEAWAY_TITLE_MAX]
    if 'description' in payload:
        updates['description'] = (payload['description'] or '')[:_GIVEAWAY_DESC_MAX]
    if 'prize' in payload:
        updates['prize'] = (payload['prize'] or '')[:_GIVEAWAY_PRIZE_MAX]
    if 'image_url' in payload:
        updates['image_url'] = _validate_giveaway_url(payload['image_url'] or '', 'image_url')
    if 'thumbnail_url' in payload:
        updates['thumbnail_url'] = _validate_giveaway_url(payload['thumbnail_url'] or '', 'thumbnail_url')
    if 'color' in payload:
        updates['color'] = _coerce_giveaway_color(payload['color'])
    if 'duration_seconds' in payload:
        updates['duration_seconds'] = int(payload['duration_seconds'])
    if 'winner_count' in payload:
        updates['winner_count'] = int(payload['winner_count'])
    if 'entry_cost_points' in payload:
        updates['entry_cost_points'] = int(payload['entry_cost_points'])
    if 'entry_tasks' in payload:
        updates['entry_tasks'] = _validate_entry_tasks(payload['entry_tasks'])
    if 'cost_source' in payload:
        updates['cost_source'] = _coerce_cost_source(payload['cost_source'])
    if 'allowed_role_ids' in payload:
        updates['allowed_role_ids'] = _normalize_role_id_list(
            payload['allowed_role_ids'], field='allowed_role_ids',
        )
    # Multi-mention update. Either field can be supplied; mention_role_ids
    # is the source of truth. The legacy mention_role_id stays mirrored to
    # the first id (or None) so old readers don't break.
    if 'mention_role_ids' in payload or 'mention_role_id' in payload:
        if 'mention_role_ids' in payload:
            mention_list_str = _normalize_role_id_list(
                payload['mention_role_ids'], field='mention_role_ids',
            )
        else:
            raw = payload.get('mention_role_id') or ''
            mention_list_str = _normalize_role_id_list(
                raw, field='mention_role_id',
            )
        updates['mention_role_ids'] = mention_list_str
        try:
            _first = json.loads(mention_list_str)
            updates['mention_role_id'] = _first[0] if isinstance(_first, list) and _first else None
        except (TypeError, ValueError):
            updates['mention_role_id'] = None
    if 'channel_id' in payload:
        updates['channel_id'] = (payload['channel_id'] or '').strip() or None

    if updates:
        db_update_giveaway(giveaway_id, server_id, **updates)

    fresh = db_get_giveaway(giveaway_id, server_id)

    # If already posted, refresh the live embed so dashboard edits flow through.
    live_edit_result = None
    if fresh and fresh.get('message_id') and fresh.get('channel_id'):
        try:
            if bot.is_ready():
                cog = bot.get_cog('Giveaway')
                if cog is not None:
                    await cog.refresh_embed(int(fresh['id']), int(fresh['guild_id']))
                    live_edit_result = 'edited'
                else:
                    live_edit_result = 'cog_unavailable'
            else:
                live_edit_result = 'bot_not_ready'
        except Exception as e:  # noqa: BLE001
            print(f'[giveaway] live-edit failed giveaway_id={giveaway_id} '
                  f'guild={server_id}: {type(e).__name__}: {e}')
            live_edit_result = 'error'

    log_event(
        server_id, 'admin_action', 'giveaway_edited',
        f'Giveaway #{giveaway_id} edited by {user.get("username")}',
        actor_user_id=user.get('user_id') or user.get('id'),
        actor_username=user.get('username'),
        module='giveaway', severity='info',
        details={'giveaway_id': giveaway_id, 'changed': sorted(updates.keys()),
                 'live_edit': live_edit_result},
    )

    out = _giveaway_row_to_dict(fresh) if fresh else {'id': giveaway_id}
    out['live_edit'] = live_edit_result
    return out


@app.post('/api/servers/{server_id}/giveaways/{giveaway_id}/start')
async def giveaways_start(
    server_id: int,
    giveaway_id: int,
    user: dict = Depends(get_current_user),
):
    """Post the branded giveaway embed to the configured channel, compute
    ends_at = now + duration_seconds, flip status to 'active'. Idempotent
    failure modes: if Discord post fails, status stays 'draft' and nothing
    permanent is written."""
    require_module_access(user, server_id, 'giveaway')
    rate_limit(f'giveaway_start:{server_id}', 30, 60.0)
    g = _check_giveaway_owner(giveaway_id, server_id)

    if g.get('status') not in ('draft',):
        raise HTTPException(status_code=400,
            detail=f'Cannot start a {g.get("status")} giveaway')
    if not (g.get('channel_id') or '').strip():
        raise HTTPException(status_code=400, detail='Set a target channel before starting.')
    if not (g.get('title') or '').strip():
        raise HTTPException(status_code=400, detail='Set a title before starting.')
    if not (g.get('prize') or '').strip():
        raise HTTPException(status_code=400, detail='Set a prize before starting.')

    if not bot.is_ready():
        raise HTTPException(status_code=503, detail='Bot not ready yet')
    guild = bot.get_guild(server_id)
    if guild is None:
        raise HTTPException(status_code=404, detail='Bot is not in this server')

    from cogs._utils import resolve_channel
    channel = resolve_channel(guild, g.get('channel_id'))
    if channel is None:
        raise HTTPException(status_code=400,
            detail=f'Channel not found: {g.get("channel_id")}')

    # Compute ends_at + seed BEFORE we update the row, so they're written in a
    # single update and never split-brain with the posted message.
    import secrets
    now      = datetime.now(timezone.utc)
    ends_at  = now + timedelta(seconds=int(g.get('duration_seconds') or 3600))
    seed     = secrets.token_hex(16)

    # Prime the row so the embed (which reads the row) sees the right state.
    db_update_giveaway(
        giveaway_id, server_id,
        status='active',
        started_at=now.isoformat(),
        ends_at=ends_at.isoformat(),
        random_seed=seed,
    )

    primed = db_get_giveaway(giveaway_id, server_id)
    entry_count = db_count_giveaway_entries(giveaway_id, server_id)

    from cogs.giveaway import build_giveaway_embed, build_giveaway_view
    embed = build_giveaway_embed(server_id, primed, entry_count)
    view  = build_giveaway_view(primed)

    # Multi-role mention. Prefer mention_role_ids (new); fall back to wrapping
    # the legacy single id for older draft rows.
    try:
        _mentions = json.loads(primed.get('mention_role_ids') or '[]')
        if not isinstance(_mentions, list):
            _mentions = []
    except (TypeError, ValueError):
        _mentions = []
    if not _mentions and primed.get('mention_role_id'):
        _mentions = [str(primed['mention_role_id'])]
    _mentions = [str(m).strip() for m in _mentions if str(m).strip()]
    content = ' '.join(f'<@&{r}>' for r in _mentions) if _mentions else None

    try:
        msg = await channel.send(
            content=content,
            embed=embed,
            view=view,
            allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False),
        )
    except discord.Forbidden:
        # Roll back the state flip so the admin can fix permissions and retry.
        db_update_giveaway(giveaway_id, server_id,
                           status='draft', started_at=None, ends_at=None, random_seed=None)
        raise HTTPException(status_code=400, detail='Bot lacks permission to send in that channel')
    except Exception as e:
        db_update_giveaway(giveaway_id, server_id,
                           status='draft', started_at=None, ends_at=None, random_seed=None)
        raise HTTPException(status_code=500, detail=str(e))

    db_update_giveaway(
        giveaway_id, server_id,
        channel_id=str(channel.id),
        message_id=str(msg.id),
    )

    log_event(
        server_id, 'admin_action', 'giveaway_started',
        f'Giveaway #{giveaway_id} started by {user.get("username")} '
        f'(ends {ends_at.isoformat()})',
        actor_user_id=user.get('user_id') or user.get('id'),
        actor_username=user.get('username'),
        module='giveaway', severity='info',
        details={'giveaway_id': giveaway_id, 'channel_id': str(channel.id),
                 'message_id': str(msg.id), 'ends_at': ends_at.isoformat(),
                 'duration_seconds': int(g.get('duration_seconds') or 0)},
    )

    return _giveaway_row_to_dict(db_get_giveaway(giveaway_id, server_id))


@app.post('/api/servers/{server_id}/giveaways/{giveaway_id}/end-now')
async def giveaways_end_now(
    server_id: int,
    giveaway_id: int,
    user: dict = Depends(get_current_user),
):
    """Force the draw immediately. The cog's _finalize_one handles the
    transition atomically (claim_giveaway_for_draw guards the race)."""
    require_module_access(user, server_id, 'giveaway')
    rate_limit(f'giveaway_end:{server_id}', 30, 60.0)
    g = _check_giveaway_owner(giveaway_id, server_id)

    if g.get('status') != 'active':
        raise HTTPException(status_code=400,
            detail=f'Cannot end a {g.get("status")} giveaway')
    if not bot.is_ready():
        raise HTTPException(status_code=503, detail='Bot not ready yet')

    cog = bot.get_cog('Giveaway')
    if cog is None:
        raise HTTPException(status_code=503, detail='Giveaway runtime not ready')

    try:
        await cog._finalize_one(g)
    except Exception as e:  # noqa: BLE001
        print(f'[giveaway] end-now failed gid={giveaway_id}: {type(e).__name__}: {e}')
        raise HTTPException(status_code=500, detail='Could not finalize the giveaway')

    log_event(
        server_id, 'admin_action', 'giveaway_ended_manual',
        f'Giveaway #{giveaway_id} ended early by {user.get("username")}',
        actor_user_id=user.get('user_id') or user.get('id'),
        actor_username=user.get('username'),
        module='giveaway', severity='info',
        details={'giveaway_id': giveaway_id},
    )

    return _giveaway_row_to_dict(db_get_giveaway(giveaway_id, server_id))


@app.post('/api/servers/{server_id}/giveaways/{giveaway_id}/reroll')
async def giveaways_reroll(
    server_id: int,
    giveaway_id: int,
    user: dict = Depends(get_current_user),
):
    """Re-draw winners from the SAME entrant pool, excluding previous winners.
    Uses a derived seed (random_seed + ':reroll:N') so the same admin clicking
    Reroll a second time gets a different result while a single click is still
    reproducible."""
    require_module_access(user, server_id, 'giveaway')
    rate_limit(f'giveaway_reroll:{server_id}', 10, 60.0)
    uid = int(user.get('user_id') or user.get('id') or 0)
    if uid:
        rate_limit(f'giveaway_reroll_u:{uid}:{server_id}', 5, 60.0)

    g = _check_giveaway_owner(giveaway_id, server_id)
    if g.get('status') != 'ended':
        raise HTTPException(status_code=400,
            detail='Reroll is only available after the giveaway has ended.')

    try:
        previous = json.loads(g.get('winners_json') or '[]')
        if not isinstance(previous, list):
            previous = []
    except (TypeError, ValueError):
        previous = []
    previous_ids = {str(w) for w in previous}

    entries = db_list_giveaway_entries(giveaway_id, server_id)
    pool = [int(e['user_id']) for e in entries
            if str(e['user_id']) not in previous_ids and e.get('user_id') is not None]
    if not pool:
        raise HTTPException(status_code=400,
            detail='No more entrants to reroll from.')

    winner_count = max(1, int(g.get('winner_count') or 1))
    seed = (g.get('random_seed') or '') + f':reroll:{len(previous_ids)}'
    import random as _random
    rng = _random.Random(seed)
    n = min(winner_count, len(pool))
    new_winners = rng.sample(pool, n)

    all_winners = [str(w) for w in previous] + [str(w) for w in new_winners]
    db_update_giveaway(
        giveaway_id, server_id,
        winners_json=json.dumps(all_winners),
    )

    # Refresh embed in-place + announce the new winners.
    fresh = db_get_giveaway(giveaway_id, server_id)
    cog = bot.get_cog('Giveaway') if bot.is_ready() else None
    if cog is not None:
        try:
            await cog.refresh_embed(giveaway_id, server_id)
            await cog._announce_winners(fresh, new_winners, len(entries))
        except Exception as e:  # noqa: BLE001
            print(f'[giveaway] reroll refresh/announce failed gid={giveaway_id}: '
                  f'{type(e).__name__}: {e}')

    log_event(
        server_id, 'admin_action', 'giveaway_rerolled',
        f'Giveaway #{giveaway_id} rerolled by {user.get("username")} '
        f'({len(new_winners)} new winner(s))',
        actor_user_id=user.get('user_id') or user.get('id'),
        actor_username=user.get('username'),
        module='giveaway', severity='info',
        details={'giveaway_id': giveaway_id,
                 'previous_winners': list(previous_ids),
                 'new_winners': [str(w) for w in new_winners]},
    )

    return _giveaway_row_to_dict(db_get_giveaway(giveaway_id, server_id))


@app.post('/api/servers/{server_id}/giveaways/{giveaway_id}/cancel')
async def giveaways_cancel(
    server_id: int,
    giveaway_id: int,
    user: dict = Depends(get_current_user),
):
    """Cancel a draft or active giveaway. If active AND entry_cost_points > 0,
    refund every entry's points_charged atomically through the existing
    community-points table before flipping status."""
    require_module_access(user, server_id, 'giveaway')
    rate_limit(f'giveaway_cancel:{server_id}', 30, 60.0)
    g = _check_giveaway_owner(giveaway_id, server_id)

    if g.get('status') not in ('draft', 'active'):
        raise HTTPException(status_code=400,
            detail=f'Cannot cancel a {g.get("status")} giveaway')

    refund_info = {'refunded_users': 0, 'refunded_points': 0}
    if g.get('status') == 'active' and int(g.get('entry_cost_points') or 0) > 0:
        try:
            refund_info = db_refund_giveaway_entries(giveaway_id, server_id)
        except Exception as e:  # noqa: BLE001
            print(f'[giveaway] refund failed gid={giveaway_id}: {type(e).__name__}: {e}')
            raise HTTPException(status_code=500, detail='Refund failed; cancel aborted')

    db_update_giveaway(
        giveaway_id, server_id,
        status='cancelled',
        ended_at=datetime.now(timezone.utc).isoformat(),
    )

    # Refresh the live embed so users see the cancellation immediately.
    cog = bot.get_cog('Giveaway') if bot.is_ready() else None
    if cog is not None:
        try:
            await cog.refresh_embed(giveaway_id, server_id)
        except Exception as e:  # noqa: BLE001
            print(f'[giveaway] cancel refresh failed gid={giveaway_id}: '
                  f'{type(e).__name__}: {e}')

    log_event(
        server_id, 'admin_action', 'giveaway_cancelled',
        f'Giveaway #{giveaway_id} cancelled by {user.get("username")}',
        actor_user_id=user.get('user_id') or user.get('id'),
        actor_username=user.get('username'),
        module='giveaway', severity='info',
        details={'giveaway_id': giveaway_id, **refund_info},
    )

    out = _giveaway_row_to_dict(db_get_giveaway(giveaway_id, server_id))
    out['refund'] = refund_info
    return out


@app.delete('/api/servers/{server_id}/giveaways/{giveaway_id}')
async def giveaways_delete(
    server_id: int,
    giveaway_id: int,
    user: dict = Depends(get_current_user),
):
    """Delete a DRAFT giveaway. Active / drawing / ended / cancelled rows are
    kept as an audit trail — admins should Cancel instead."""
    require_module_access(user, server_id, 'giveaway')
    g = _check_giveaway_owner(giveaway_id, server_id)
    if g.get('status') != 'draft':
        raise HTTPException(status_code=400,
            detail='Only draft giveaways can be deleted. Cancel an active one instead.')

    db_delete_giveaway(giveaway_id, server_id)
    log_event(
        server_id, 'admin_action', 'giveaway_deleted',
        f'Giveaway draft #{giveaway_id} deleted by {user.get("username")}',
        actor_user_id=user.get('user_id') or user.get('id'),
        actor_username=user.get('username'),
        module='giveaway', severity='info',
        details={'giveaway_id': giveaway_id},
    )
    return {'ok': True}


@app.get('/api/servers/{server_id}/giveaways/{giveaway_id}/entries')
async def giveaways_entries(
    server_id: int,
    giveaway_id: int,
    user: dict = Depends(get_current_user),
):
    require_module_access(user, server_id, 'giveaway')
    _check_giveaway_owner(giveaway_id, server_id)
    rows = db_list_giveaway_entries(giveaway_id, server_id)
    return {
        'entries': [
            {'id':             int(r['id']),
             'user_id':        str(r['user_id']),
             'entered_at':     r.get('entered_at'),
             'points_charged': int(r.get('points_charged') or 0)}
            for r in rows
        ],
        'total': len(rows),
    }


# ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ──
#  RADAR ENDPOINTS  (Phase 1 — crypto)
# ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ──
# Per-guild settings + watchlist CRUD + search + preview. Every endpoint is
# guild-scoped and module-gated through require_module_access('radar'). The
# fetcher / alerts / digest loops run elsewhere; this file just exposes
# config and reads from the cache.

_RADAR_SUPPORTED_KINDS  = ('crypto', 'nft', 'meme', 'forex')  # phase-2 live
_RADAR_FUTURE_KINDS     = ('stocks',)
_RADAR_ALL_KINDS        = _RADAR_SUPPORTED_KINDS + _RADAR_FUTURE_KINDS

# Threshold bounds — protect users from setting absurd values that would
# either spam every tick or never fire. Mirrors the spec.
_RADAR_MOVE_PCT_RANGE       = (0.5, 50.0)
_RADAR_VOL_MULT_RANGE       = (1.5, 20.0)
_RADAR_LIQ_MIN_USD_RANGE    = (100_000, 1_000_000_000)
_RADAR_TZ_OFFSET_RANGE      = (-12 * 60, 14 * 60)  # minutes; UTC-12 .. UTC+14
_RADAR_MANUAL_DIGEST_DAILY_CAP = 5         # per guild, per UTC day
_RADAR_MANUAL_DIGEST_COOLDOWN_S = 300.0    # 5 minutes between manual sends

# Digest template input bounds (FIX 5 round 2).
_RADAR_DIGEST_TITLE_MAX   = 256
_RADAR_DIGEST_INTRO_MAX   = 1000
_RADAR_DIGEST_FOOTER_MAX  = 2048
_RADAR_DIGEST_THUMB_MODES = {'brand', 'first_coin', 'off'}
_RADAR_DIGEST_DATE_MODES  = {'off', 'date_only', 'date_tz'}
_RADAR_DIGEST_COLOR_RE    = _gw_re.compile(r'^#?[0-9a-fA-F]{6}$')

# Phase 3 — multi-timeframe alert threshold ranges.
_RADAR_ALERT_1H_RANGE   = (1.0, 20.0)
_RADAR_ALERT_24H_RANGE  = (3.0, 50.0)
_RADAR_ALERT_7D_RANGE   = (10.0, 100.0)
_RADAR_ALERT_VOL_MUL    = (1.5, 10.0)
# Discovery thresholds — broad ranges so server admins can be aggressive
# or conservative without us second-guessing them.
_RADAR_DISCOVERY_LIQ_RANGE    = (1_000, 10_000_000)
_RADAR_DISCOVERY_VOL_RANGE    = (1_000, 100_000_000)
_RADAR_DISCOVERY_AGE_RANGE    = (0, 24 * 90)         # hours, 0..90 days
_RADAR_DISCOVERY_PCT_RANGE    = (5.0, 500.0)
_RADAR_DISCOVERY_VCHG_RANGE   = (10.0, 1000.0)
_RADAR_DISCOVERY_SALES_RANGE  = (1, 10_000)


def _radar_normalize_hex_color(s: str) -> str:
    """Return canonical '#RRGGBB' lowercase or '' for empty. 400s on
    anything that looks set but isn't valid hex."""
    if s is None:
        return ''
    raw = str(s).strip()
    if not raw:
        return ''
    if not _RADAR_DIGEST_COLOR_RE.match(raw):
        raise HTTPException(
            status_code=400,
            detail='digest_color must be a 6-character hex color (e.g. #C8A84E)',
        )
    return '#' + raw.lstrip('#').lower()


class _RadarTopicBlock(BaseModel):
    """All editable fields on a single (guild, topic) row. Every field is
    optional so PATCH can be sparse — only present keys are written."""
    daily_enabled:                Optional[int]    = None
    daily_channel:                Optional[str]    = None
    daily_time:                   Optional[str]    = None
    digest_mention_role_ids:      Optional[object] = None
    alerts_enabled:               Optional[int]    = None
    alerts_channel:               Optional[str]    = None
    movement_threshold_pct:       Optional[float]  = None
    volume_multiplier_threshold:  Optional[float]  = None
    alerts_mention_role_ids:      Optional[object] = None
    digest_title:                 Optional[str]    = None
    digest_intro:                 Optional[str]    = None
    digest_color:                 Optional[str]    = None
    digest_footer:                Optional[str]    = None
    digest_thumbnail_mode:        Optional[str]    = None
    digest_date_mode:             Optional[str]    = None
    # Phase 3 — multi-timeframe alerts.
    alert_1h_threshold_pct:       Optional[float]  = None
    alert_24h_threshold_pct:      Optional[float]  = None
    alert_7d_threshold_pct:       Optional[float]  = None
    alert_volume_multiplier:      Optional[float]  = None
    alert_1h_enabled:             Optional[int]    = None
    alert_24h_enabled:            Optional[int]    = None
    alert_7d_enabled:             Optional[int]    = None
    alert_volume_enabled:         Optional[int]    = None
    # Phase 3 — Trending Discovery (meme + nft).
    discovery_enabled:                  Optional[int]    = None
    discovery_channel:                  Optional[str]    = None
    discovery_mention_role_ids:         Optional[object] = None
    discovery_min_liquidity_usd:        Optional[int]    = None
    discovery_min_volume_24h_usd:       Optional[int]    = None
    discovery_min_age_hours:            Optional[int]    = None
    discovery_min_change_1h_pct:        Optional[float]  = None
    discovery_min_volume_change_24h_pct: Optional[float] = None
    discovery_min_sales_24h:            Optional[int]    = None


class _RadarGlobalBlock(BaseModel):
    """Guild-global radar settings — timezone (shared across all topics) +
    Phase-3 reserved fields (liquidation_* + stocks_alpha_vantage_key)."""
    timezone_offset:              Optional[int]    = None
    # Phase-3 reservations — accepted on write so we don't have to revisit
    # the model when those topics ship.
    liquidation_enabled:          Optional[int]    = None
    liquidation_channel:          Optional[str]    = None
    liquidation_min_usd:          Optional[int]    = None
    stocks_alpha_vantage_key:     Optional[str]    = None


class _RadarSettingsUpdate(BaseModel):
    """New PATCH body shape: { global: {...}, topics: { crypto: {...}, ... } }.
    Either side may be omitted; whichever is present is partially merged."""
    global_:                      Optional[_RadarGlobalBlock] = None
    topics:                       Optional[dict[str, _RadarTopicBlock]] = None

    # FastAPI / Pydantic v2 alias for the reserved Python word 'global'.
    model_config = {'populate_by_name': True}

    def __init__(self, **data):
        if 'global' in data and 'global_' not in data:
            data['global_'] = data.pop('global')
        super().__init__(**data)


class _RadarWatchlistCreate(BaseModel):
    asset_kind:       str
    asset_identifier: str
    display_name:     Optional[str] = ''


def _radar_clamp(v, low, high, *, kind: str, field: str):
    """Reject out-of-range thresholds with a clear error. Returns the
    coerced numeric value."""
    try:
        n = float(v) if kind == 'float' else int(v)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f'{field} must be numeric')
    if n < low or n > high:
        raise HTTPException(
            status_code=400,
            detail=f'{field} must be between {low} and {high}',
        )
    return n


def _radar_hhmm(s: str) -> str:
    """Validate "HH:MM" string. Empty/None falls back to a sane default."""
    val = (s or '').strip()
    if not val:
        return '08:00'
    try:
        hh, mm = val.split(':')
        h, m = int(hh), int(mm)
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError
        return f'{h:02d}:{m:02d}'
    except (ValueError, AttributeError):
        raise HTTPException(
            status_code=400,
            detail='daily_time must be a 24h HH:MM string (e.g. 08:00)',
        )


def _radar_channel(value, *, field: str) -> str | None:
    """Coerce an optional channel id. Empty/None → None. Otherwise must be
    a 17-20 digit numeric Discord snowflake. Returns a STRING so it
    survives the JS Number.MAX_SAFE_INTEGER round-trip without precision
    loss — the dashboard sends strings and reads strings."""
    if value is None or value == '':
        return None
    s = str(value).strip()
    if not s:
        return None
    if not s.isdigit() or not (17 <= len(s) <= 20):
        raise HTTPException(status_code=400,
            detail=f'{field} must be a numeric Discord channel id')
    return s


_RADAR_TOPIC_NAMES = ('crypto', 'nft', 'meme', 'forex')


def _radar_mask_global(s: dict) -> dict:
    """Guild-global block. Stringifies channel ids, masks Alpha Vantage key.
    Phase-3 reserved fields pass through so the dashboard can read state
    without a separate endpoint."""
    out = dict(s)
    key = out.get('stocks_alpha_vantage_key')
    if key:
        out['stocks_alpha_vantage_key'] = (f'••••{str(key)[-4:]}'
                                            if len(str(key)) > 4 else '••••')
    out['stocks_alpha_vantage_key_set'] = bool(key)
    for col in ('liquidation_channel',):
        v = out.get(col)
        out[col] = str(v) if (v is not None and v != '') else None
    # Strip legacy per-topic fields from the global view — they are owned
    # by radar_topic_settings now.
    for legacy in ('daily_enabled', 'daily_time', 'daily_channel_crypto',
                   'daily_channel_nft', 'daily_channel_meme',
                   'daily_channel_forex', 'daily_channel_stocks',
                   'alerts_channel', 'alerts_enabled',
                   'movement_threshold_pct', 'volume_multiplier_threshold',
                   'digest_title', 'digest_intro', 'digest_color',
                   'digest_footer', 'digest_thumbnail_mode',
                   'digest_date_mode', 'digest_mention_role_ids',
                   'alerts_mention_role_ids',
                   'manual_digests_used_today', 'manual_digests_reset_date',
                   'last_manual_digest_at', 'last_daily_sent_date'):
        out.pop(legacy, None)
    return out


def _radar_mask_topic(row: dict) -> dict:
    """Per-topic row: stringify channel ids, decode role-id JSON arrays,
    surface per-topic manual-send quota counters."""
    out = dict(row)
    for col in ('daily_channel', 'alerts_channel', 'discovery_channel'):
        v = out.get(col)
        out[col] = str(v) if (v is not None and v != '') else None
    for col in ('digest_mention_role_ids', 'alerts_mention_role_ids',
                'discovery_mention_role_ids'):
        raw = out.get(col) or '[]'
        try:
            arr = raw if isinstance(raw, list) else json.loads(raw)
            if not isinstance(arr, list):
                arr = []
        except (TypeError, ValueError):
            arr = []
        out[col] = [str(v).strip() for v in arr if str(v).strip()]

    today_utc = datetime.now(timezone.utc).date().isoformat()
    used = int(out.get('manual_digests_used_today') or 0)
    if str(out.get('manual_digests_reset_date') or '') != today_utc:
        used = 0
    cap = _RADAR_MANUAL_DIGEST_DAILY_CAP
    out['manual_digests_used_today'] = used
    out['manual_digests_remaining_today'] = max(0, cap - used)
    out['manual_digests_daily_cap'] = cap
    return out


@app.get('/api/servers/{server_id}/radar/settings')
async def radar_settings_get(
    server_id: int, user: dict = Depends(get_current_user),
):
    require_module_access(user, server_id, 'radar')
    glob = db_get_radar_settings(server_id)
    topics_raw = db_list_radar_topic_settings(server_id)
    return {
        'global': _radar_mask_global(glob),
        'topics': {t: _radar_mask_topic(topics_raw[t]) for t in _RADAR_TOPIC_NAMES},
    }


def _validate_topic_block(topic: str, raw: dict) -> dict:
    """Translate a sparse _RadarTopicBlock dict into a column-named updates
    dict suitable for db_update_radar_topic_settings. Each field present
    is validated; absent fields are not touched."""
    updates: dict = {}
    if 'daily_enabled' in raw:
        updates['daily_enabled'] = 1 if raw['daily_enabled'] else 0
    if 'daily_channel' in raw:
        updates['daily_channel'] = _radar_channel(raw['daily_channel'],
                                                   field=f'{topic}.daily_channel')
    if 'daily_time' in raw:
        updates['daily_time'] = _radar_hhmm(raw['daily_time'] or '')
    if 'digest_mention_role_ids' in raw:
        updates['digest_mention_role_ids'] = _normalize_role_id_list(
            raw['digest_mention_role_ids'],
            field=f'{topic}.digest_mention_role_ids',
        )
    if 'alerts_enabled' in raw:
        updates['alerts_enabled'] = 1 if raw['alerts_enabled'] else 0
    if 'alerts_channel' in raw:
        updates['alerts_channel'] = _radar_channel(raw['alerts_channel'],
                                                     field=f'{topic}.alerts_channel')
    if 'movement_threshold_pct' in raw:
        updates['movement_threshold_pct'] = _radar_clamp(
            raw['movement_threshold_pct'],
            _RADAR_MOVE_PCT_RANGE[0], _RADAR_MOVE_PCT_RANGE[1],
            kind='float', field=f'{topic}.movement_threshold_pct',
        )
    if 'volume_multiplier_threshold' in raw:
        updates['volume_multiplier_threshold'] = _radar_clamp(
            raw['volume_multiplier_threshold'],
            _RADAR_VOL_MULT_RANGE[0], _RADAR_VOL_MULT_RANGE[1],
            kind='float', field=f'{topic}.volume_multiplier_threshold',
        )
    if 'alerts_mention_role_ids' in raw:
        updates['alerts_mention_role_ids'] = _normalize_role_id_list(
            raw['alerts_mention_role_ids'],
            field=f'{topic}.alerts_mention_role_ids',
        )
    # Digest template overrides — empty string is meaningful (= use default).
    if 'digest_title' in raw:
        val = (raw['digest_title'] or '').strip()
        if len(val) > _RADAR_DIGEST_TITLE_MAX:
            raise HTTPException(status_code=400,
                detail=f'{topic}.digest_title too long')
        updates['digest_title'] = val
    if 'digest_intro' in raw:
        val = (raw['digest_intro'] or '').strip()
        if len(val) > _RADAR_DIGEST_INTRO_MAX:
            raise HTTPException(status_code=400,
                detail=f'{topic}.digest_intro too long')
        updates['digest_intro'] = val
    if 'digest_color' in raw:
        updates['digest_color'] = _radar_normalize_hex_color(raw['digest_color'])
    if 'digest_footer' in raw:
        val = (raw['digest_footer'] or '').strip()
        if len(val) > _RADAR_DIGEST_FOOTER_MAX:
            raise HTTPException(status_code=400,
                detail=f'{topic}.digest_footer too long')
        updates['digest_footer'] = val
    if 'digest_thumbnail_mode' in raw:
        v = (raw['digest_thumbnail_mode'] or 'brand').strip().lower()
        if v not in _RADAR_DIGEST_THUMB_MODES:
            raise HTTPException(status_code=400,
                detail=f'{topic}.digest_thumbnail_mode must be one of: '
                       f'{", ".join(sorted(_RADAR_DIGEST_THUMB_MODES))}')
        updates['digest_thumbnail_mode'] = v
    if 'digest_date_mode' in raw:
        v = (raw['digest_date_mode'] or 'date_tz').strip().lower()
        if v not in _RADAR_DIGEST_DATE_MODES:
            raise HTTPException(status_code=400,
                detail=f'{topic}.digest_date_mode must be one of: '
                       f'{", ".join(sorted(_RADAR_DIGEST_DATE_MODES))}')
        updates['digest_date_mode'] = v

    # ── Phase 3 — multi-timeframe alerts ────────────────────────────────
    for boolkey in ('alert_1h_enabled', 'alert_24h_enabled',
                    'alert_7d_enabled', 'alert_volume_enabled'):
        if boolkey in raw:
            updates[boolkey] = 1 if raw[boolkey] else 0
    if 'alert_1h_threshold_pct' in raw:
        updates['alert_1h_threshold_pct'] = _radar_clamp(
            raw['alert_1h_threshold_pct'],
            _RADAR_ALERT_1H_RANGE[0], _RADAR_ALERT_1H_RANGE[1],
            kind='float', field=f'{topic}.alert_1h_threshold_pct',
        )
    if 'alert_24h_threshold_pct' in raw:
        updates['alert_24h_threshold_pct'] = _radar_clamp(
            raw['alert_24h_threshold_pct'],
            _RADAR_ALERT_24H_RANGE[0], _RADAR_ALERT_24H_RANGE[1],
            kind='float', field=f'{topic}.alert_24h_threshold_pct',
        )
    if 'alert_7d_threshold_pct' in raw:
        updates['alert_7d_threshold_pct'] = _radar_clamp(
            raw['alert_7d_threshold_pct'],
            _RADAR_ALERT_7D_RANGE[0], _RADAR_ALERT_7D_RANGE[1],
            kind='float', field=f'{topic}.alert_7d_threshold_pct',
        )
    if 'alert_volume_multiplier' in raw:
        updates['alert_volume_multiplier'] = _radar_clamp(
            raw['alert_volume_multiplier'],
            _RADAR_ALERT_VOL_MUL[0], _RADAR_ALERT_VOL_MUL[1],
            kind='float', field=f'{topic}.alert_volume_multiplier',
        )

    # ── Phase 3 — Trending Discovery (meme + nft) ───────────────────────
    if 'discovery_enabled' in raw:
        updates['discovery_enabled'] = 1 if raw['discovery_enabled'] else 0
    if 'discovery_channel' in raw:
        updates['discovery_channel'] = _radar_channel(
            raw['discovery_channel'], field=f'{topic}.discovery_channel',
        )
    if 'discovery_mention_role_ids' in raw:
        updates['discovery_mention_role_ids'] = _normalize_role_id_list(
            raw['discovery_mention_role_ids'],
            field=f'{topic}.discovery_mention_role_ids',
        )
    if 'discovery_min_liquidity_usd' in raw:
        updates['discovery_min_liquidity_usd'] = int(_radar_clamp(
            raw['discovery_min_liquidity_usd'],
            _RADAR_DISCOVERY_LIQ_RANGE[0], _RADAR_DISCOVERY_LIQ_RANGE[1],
            kind='int', field=f'{topic}.discovery_min_liquidity_usd',
        ))
    if 'discovery_min_volume_24h_usd' in raw:
        updates['discovery_min_volume_24h_usd'] = int(_radar_clamp(
            raw['discovery_min_volume_24h_usd'],
            _RADAR_DISCOVERY_VOL_RANGE[0], _RADAR_DISCOVERY_VOL_RANGE[1],
            kind='int', field=f'{topic}.discovery_min_volume_24h_usd',
        ))
    if 'discovery_min_age_hours' in raw:
        updates['discovery_min_age_hours'] = int(_radar_clamp(
            raw['discovery_min_age_hours'],
            _RADAR_DISCOVERY_AGE_RANGE[0], _RADAR_DISCOVERY_AGE_RANGE[1],
            kind='int', field=f'{topic}.discovery_min_age_hours',
        ))
    if 'discovery_min_change_1h_pct' in raw:
        updates['discovery_min_change_1h_pct'] = _radar_clamp(
            raw['discovery_min_change_1h_pct'],
            _RADAR_DISCOVERY_PCT_RANGE[0], _RADAR_DISCOVERY_PCT_RANGE[1],
            kind='float', field=f'{topic}.discovery_min_change_1h_pct',
        )
    if 'discovery_min_volume_change_24h_pct' in raw:
        updates['discovery_min_volume_change_24h_pct'] = _radar_clamp(
            raw['discovery_min_volume_change_24h_pct'],
            _RADAR_DISCOVERY_VCHG_RANGE[0], _RADAR_DISCOVERY_VCHG_RANGE[1],
            kind='float', field=f'{topic}.discovery_min_volume_change_24h_pct',
        )
    if 'discovery_min_sales_24h' in raw:
        updates['discovery_min_sales_24h'] = int(_radar_clamp(
            raw['discovery_min_sales_24h'],
            _RADAR_DISCOVERY_SALES_RANGE[0], _RADAR_DISCOVERY_SALES_RANGE[1],
            kind='int', field=f'{topic}.discovery_min_sales_24h',
        ))
    return updates


def _validate_global_block(raw: dict) -> dict:
    updates: dict = {}
    if 'timezone_offset' in raw:
        updates['timezone_offset'] = int(_radar_clamp(
            raw['timezone_offset'],
            _RADAR_TZ_OFFSET_RANGE[0], _RADAR_TZ_OFFSET_RANGE[1],
            kind='int', field='global.timezone_offset',
        ))
    if 'liquidation_enabled' in raw:
        updates['liquidation_enabled'] = 1 if raw['liquidation_enabled'] else 0
    if 'liquidation_channel' in raw:
        updates['liquidation_channel'] = _radar_channel(
            raw['liquidation_channel'], field='global.liquidation_channel',
        )
    if 'liquidation_min_usd' in raw:
        updates['liquidation_min_usd'] = int(_radar_clamp(
            raw['liquidation_min_usd'],
            _RADAR_LIQ_MIN_USD_RANGE[0], _RADAR_LIQ_MIN_USD_RANGE[1],
            kind='int', field='global.liquidation_min_usd',
        ))
    if 'stocks_alpha_vantage_key' in raw:
        new_key = (raw['stocks_alpha_vantage_key'] or '').strip()
        if new_key and not new_key.startswith('••'):
            updates['stocks_alpha_vantage_key'] = new_key[:128]
    return updates


@app.patch('/api/servers/{server_id}/radar/settings')
async def radar_settings_patch(
    server_id: int,
    body: _RadarSettingsUpdate,
    user: dict = Depends(get_current_user),
):
    require_module_access(user, server_id, 'radar')
    rate_limit(f'radar_settings:{server_id}', 60, 60.0)

    payload = body.model_dump(exclude_unset=True, by_alias=False)
    glob = payload.get('global_') or payload.get('global') or {}
    topics_in = payload.get('topics') or {}

    changed_global: list = []
    changed_topics: dict = {}

    # Global block.
    if glob:
        g_updates = _validate_global_block(glob)
        if g_updates:
            db_update_radar_settings(server_id, **g_updates)
            changed_global = sorted(g_updates.keys())

    # Per-topic blocks.
    if topics_in:
        if not isinstance(topics_in, dict):
            raise HTTPException(status_code=400, detail='topics must be an object')
        for t_name, t_raw in topics_in.items():
            t = (t_name or '').strip().lower()
            if t not in _RADAR_TOPIC_NAMES:
                raise HTTPException(status_code=400,
                    detail=f'unknown topic in body: {t_name}')
            if not isinstance(t_raw, dict):
                raise HTTPException(status_code=400,
                    detail=f'topics.{t} must be an object')
            t_updates = _validate_topic_block(t, t_raw)
            if t_updates:
                db_update_radar_topic_settings(server_id, t, **t_updates)
                changed_topics[t] = sorted(t_updates.keys())

    log_event(
        server_id, 'admin_action', 'radar_settings_updated',
        f'Radar settings updated by {user.get("username")}',
        actor_user_id=user.get('user_id') or user.get('id'),
        actor_username=user.get('username'),
        module='radar', severity='info',
        details={'global': changed_global, 'topics': changed_topics},
    )

    glob_fresh   = db_get_radar_settings(server_id)
    topics_fresh = db_list_radar_topic_settings(server_id)
    return {
        'global': _radar_mask_global(glob_fresh),
        'topics': {t: _radar_mask_topic(topics_fresh[t]) for t in _RADAR_TOPIC_NAMES},
    }


@app.get('/api/servers/{server_id}/radar/watchlist')
async def radar_watchlist_list(
    server_id: int,
    asset_kind: Optional[str] = None,
    user: dict = Depends(get_current_user),
):
    require_module_access(user, server_id, 'radar')
    kind = (asset_kind or '').strip().lower() or None
    if kind and kind not in _RADAR_ALL_KINDS:
        raise HTTPException(status_code=400, detail=f'unknown asset_kind: {kind}')
    rows = db_list_radar_watchlist(server_id, asset_kind=kind)
    out = []
    # Hydrate each crypto row with the latest cache snapshot for the UI.
    try:
        from services.radar.cache import CACHE as _RADAR_CACHE
    except Exception:  # noqa: BLE001
        _RADAR_CACHE = None
    for r in rows:
        kind_v = (r.get('asset_kind') or '').lower()
        ident  = (r.get('asset_identifier') or '').lower()
        snap   = None
        # Crypto/NFT cache keys are lowercased identifiers; forex (incl.
        # commodities) keys are the raw 'BASE/QUOTE'. Hydrate all so their
        # live-preview cards show price + change.
        if _RADAR_CACHE is not None:
            if kind_v in ('crypto', 'nft'):
                snap = _RADAR_CACHE.get_snapshot(kind_v, ident)
            elif kind_v == 'forex':
                snap = _RADAR_CACHE.get_snapshot('forex', r.get('asset_identifier'))
        out.append({
            'id':               int(r['id']),
            'asset_kind':       kind_v,
            'asset_identifier': r.get('asset_identifier'),
            'display_name':     r.get('display_name') or r.get('asset_identifier'),
            'display_order':    int(r.get('display_order') or 0),
            'added_by':         str(r.get('added_by')) if r.get('added_by') else None,
            'added_at':         r.get('added_at'),
            'snapshot':         snap,
        })
    return {'watchlist': out}


@app.post('/api/servers/{server_id}/radar/watchlist')
async def radar_watchlist_add(
    server_id: int,
    body: _RadarWatchlistCreate,
    user: dict = Depends(get_current_user),
):
    require_module_access(user, server_id, 'radar')
    rate_limit(f'radar_watchlist:{server_id}', 30, 60.0)

    kind  = (body.asset_kind or '').strip().lower()
    # Crypto / nft slugs get lower-cased; forex pairs stay upper-cased
    # (BASE/QUOTE). Memecoin identifiers keep their chain prefix lower and
    # the address case-sensitive (Solana addresses are base58 with mixed case).
    raw_ident = (body.asset_identifier or '').strip()
    if not raw_ident:
        raise HTTPException(status_code=400, detail='asset_identifier is required')
    if kind not in _RADAR_ALL_KINDS:
        raise HTTPException(status_code=400, detail=f'unknown asset_kind: {kind}')
    if kind == 'liquidation':
        raise HTTPException(status_code=400, detail='Not a watchlist kind.')
    if kind == 'stocks':
        raise HTTPException(
            status_code=400,
            detail='Stocks ship in a later phase.',
        )

    ident = raw_ident.lower() if kind in ('crypto', 'nft') else raw_ident
    display_name = (body.display_name or '').strip() or ident

    from services.radar.adapters import ADAPTERS_BY_KIND, is_commodity
    from services.radar.cache    import CACHE as _RADAR_CACHE

    if kind == 'crypto':
        # Resolve CoinGecko id; fall back to search → first result.
        try:
            adapter = ADAPTERS_BY_KIND.get('crypto')
            snap = None
            if adapter:
                snap = await adapter.fetch_one(ident)
                if not snap:
                    sugg = await adapter.search(ident, limit=1)
                    if sugg:
                        ident = (sugg[0].get('identifier') or ident).lower()
                        snap = await adapter.fetch_one(ident)
            if snap:
                _RADAR_CACHE.put('crypto', snap['identifier'], snap)
                if display_name == raw_ident.lower() or display_name == ident:
                    display_name = (snap.get('symbol_display')
                                    or snap.get('raw', {}).get('name')
                                    or ident).upper()
        except Exception as e:  # noqa: BLE001
            print(f'[radar/api] resolve crypto failed for {ident}: '
                  f'{type(e).__name__}: {e}')

    elif kind == 'nft':
        adapter = ADAPTERS_BY_KIND.get('nft')
        if adapter is None or getattr(adapter, 'disabled_reason', None):
            raise HTTPException(
                status_code=503,
                detail=getattr(adapter, 'disabled_reason', None)
                       or 'NFT adapter not configured.',
            )
        try:
            snap = await adapter.fetch_one(ident)
            if snap is None:
                # Last-chance: search for the slug; first match wins.
                sugg = await adapter.search(ident, limit=1)
                if sugg:
                    ident = (sugg[0].get('identifier') or ident).lower()
                    snap = await adapter.fetch_one(ident)
            if snap is None:
                raise HTTPException(
                    status_code=404,
                    detail="Collection not found. Try the slug from the OpenSea "
                           "URL (e.g. 'boredapeyachtclub' from "
                           "opensea.io/collection/boredapeyachtclub), or paste "
                           "the contract address.",
                )
            _RADAR_CACHE.put('nft', snap['identifier'], snap)
            if display_name == raw_ident.lower() or display_name == ident:
                display_name = (snap.get('symbol_display') or ident)
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            print(f'[radar/api] resolve nft failed: {type(e).__name__}: {e}')
            raise HTTPException(status_code=503,
                detail='NFT lookup unavailable. Try again shortly.')

    elif kind == 'meme':
        # Accept either 'chain:address' or a dexscreener URL via the
        # adapter's parse_meme_input helper.
        from services.radar.adapters.dexscreener import (
            parse_meme_input, address_looks_valid, SUPPORTED_CHAINS,
        )
        parsed = parse_meme_input(raw_ident)
        if not parsed:
            raise HTTPException(
                status_code=400,
                detail='Memecoin identifier must be "chain:address" or a '
                       'dexscreener.com URL.',
            )
        chain, address = parsed
        # chain may be None for a bare EVM address — the adapter then scans
        # every chain DEXScreener lists for the address and picks the most
        # liquid pair. Only reject an explicitly-named unsupported chain.
        if chain is not None and chain not in SUPPORTED_CHAINS:
            raise HTTPException(
                status_code=400,
                detail=f'Chain "{chain}" is not supported. Use one of: '
                       f'{", ".join(SUPPORTED_CHAINS)}.',
            )
        if not address_looks_valid(chain, address):
            raise HTTPException(
                status_code=400,
                detail=f'Address does not look valid for chain "{chain or "auto"}".',
            )
        adapter = ADAPTERS_BY_KIND.get('meme')
        try:
            # Bare address → pass the address alone so the adapter auto-detects
            # the chain; otherwise pin to the named chain.
            snap = await adapter.fetch_one(f'{chain}:{address}' if chain else address)
            if snap is None:
                raise HTTPException(
                    status_code=404,
                    detail='No active pair found for this token on DEXScreener. Check the address.',
                )
            ident = snap['identifier']
            _RADAR_CACHE.put('meme', ident, snap)
            if display_name == raw_ident or display_name == ident:
                display_name = (snap.get('symbol_display')
                                or snap.get('raw', {}).get('name')
                                or address[:8])
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            print(f'[radar/api] resolve meme failed: {type(e).__name__}: {e}')
            raise HTTPException(status_code=503,
                detail='Memecoin lookup unavailable. Try again shortly.')

    elif kind == 'forex' and is_commodity(raw_ident):
        # Commodities (XAU/XAG/WTI/BRENT/XPT) are quoted in USD via Yahoo,
        # bypassing the fiat 3-letter validation (BRENT is 5 chars).
        from services.radar.adapters import COMMODITIES_ADAPTER
        base = raw_ident.split('/')[0].strip().upper()
        ident = f'{base}/USD'
        try:
            snap = await COMMODITIES_ADAPTER.fetch_one(ident)
        except Exception as e:  # noqa: BLE001
            print(f'[radar/api] commodity prefetch failed: {type(e).__name__}: {e}')
            snap = None
        if snap is None:
            raise HTTPException(status_code=503,
                detail='Commodity price source unavailable. Try again shortly.')
        _RADAR_CACHE.put('forex', ident, snap)
        if display_name == raw_ident or display_name == ident:
            display_name = (snap.get('display_name')
                            or snap.get('raw', {}).get('name') or ident)

    elif kind == 'forex':
        from services.radar.adapters.frankfurter import split_pair
        parsed = split_pair(raw_ident)
        if not parsed:
            raise HTTPException(
                status_code=400,
                detail='Forex identifier must be "BASE/QUOTE" with two 3-letter codes.',
            )
        base, quote = parsed
        if base == quote:
            raise HTTPException(status_code=400,
                detail='Base and quote currencies must differ.')
        adapter = ADAPTERS_BY_KIND.get('forex')
        try:
            currencies = await adapter.currencies()
        except Exception as e:  # noqa: BLE001
            print(f'[radar/api] forex currencies failed: {type(e).__name__}: {e}')
            currencies = {}
        if currencies and (base not in currencies or quote not in currencies):
            raise HTTPException(
                status_code=400,
                detail=f'Unknown currency code in {base}/{quote}.',
            )
        ident = f'{base}/{quote}'
        try:
            snap = await adapter.fetch_one(ident)
            if snap is not None:
                _RADAR_CACHE.put('forex', ident, snap)
                if display_name == raw_ident:
                    display_name = ident
        except Exception as e:  # noqa: BLE001
            print(f'[radar/api] forex prefetch failed: {type(e).__name__}: {e}')

    try:
        entry_id = db_add_radar_watchlist_entry(
            server_id, kind, ident,
            display_name=display_name,
            added_by=int(user.get('user_id') or user.get('id') or 0) or None,
        )
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409,
            detail=f'{ident} is already on the {kind} watchlist')
    except Exception as e:  # noqa: BLE001
        print(f'[radar/api] add failed: {type(e).__name__}: {e}')
        raise HTTPException(status_code=500, detail='Could not save the entry')

    if kind == 'nft':
        try:
            from database import set_radar_watchlist_platform
            set_radar_watchlist_platform(server_id, entry_id, 'opensea')
        except Exception as e:  # noqa: BLE001
            print(f'[radar/api] set platform failed: {type(e).__name__}: {e}')

    log_event(
        server_id, 'admin_action', 'radar_watchlist_added',
        f'Radar watchlist: {kind} {ident} added via dashboard',
        actor_user_id=user.get('user_id') or user.get('id'),
        actor_username=user.get('username'),
        module='radar', severity='info',
        details={'entry_id': entry_id, 'kind': kind, 'identifier': ident},
    )
    return {
        'id':               entry_id,
        'asset_kind':       kind,
        'asset_identifier': ident,
        'display_name':     display_name,
    }


@app.delete('/api/servers/{server_id}/radar/watchlist/{entry_id}')
async def radar_watchlist_delete(
    server_id: int, entry_id: int,
    user: dict = Depends(get_current_user),
):
    require_module_access(user, server_id, 'radar')
    ok = db_remove_radar_watchlist_entry(server_id, entry_id)
    if not ok:
        raise HTTPException(status_code=404, detail='entry not found')
    log_event(
        server_id, 'admin_action', 'radar_watchlist_removed',
        f'Radar watchlist entry #{entry_id} removed via dashboard',
        actor_user_id=user.get('user_id') or user.get('id'),
        actor_username=user.get('username'),
        module='radar', severity='info',
        details={'entry_id': entry_id},
    )
    return {'ok': True}


async def _resolve_nft_preview(body: dict) -> dict:
    """NFT preview for the dashboard add UI. Accepts a '<chain>:<slug>' in
    `input`/`query`, or a separate `chain` + `query` slug."""
    from services.radar.adapters import ADAPTERS_BY_KIND
    adapter = ADAPTERS_BY_KIND.get('nft')
    if adapter is None or getattr(adapter, 'disabled_reason', None):
        raise HTTPException(
            status_code=503,
            detail=getattr(adapter, 'disabled_reason', None)
                   or 'NFT adapter not configured.',
        )
    query = str(body.get('query') or body.get('input') or '').strip()
    chain = str(body.get('chain') or '').strip().lower()
    if not query:
        raise HTTPException(status_code=400, detail='query is required')
    ident = query if ':' in query else (f'{chain}:{query}' if chain else query)
    try:
        snap = await adapter.fetch_one(ident)
    except Exception as e:  # noqa: BLE001
        print(f'[radar/api] resolve nft failed: {type(e).__name__}: {e}')
        raise HTTPException(status_code=503,
            detail='NFT lookup unavailable. Try again shortly.')
    if snap is None:
        raise HTTPException(
            status_code=404,
            detail="Collection not found. Try the slug from the OpenSea URL "
                   "(e.g. 'boredapeyachtclub' from "
                   "opensea.io/collection/boredapeyachtclub), or paste the "
                   "contract address.",
        )
    raw = snap.get('raw') or {}
    return {
        'kind':           'nft',
        'identifier':     snap['identifier'],
        'chain':          raw.get('chain'),
        'slug':           raw.get('slug'),
        'symbol':         snap.get('symbol_display'),
        'name':           raw.get('name'),
        'price_usd':      snap.get('price_usd'),
        'price_symbol':   snap.get('price_display_symbol'),
        'change_24h_pct': snap.get('change_24h_pct'),
        'volume_24h_usd': snap.get('volume_24h_usd'),
        'image_url':      snap.get('image_url'),
        'page_url':       snap.get('page_url'),
    }


@app.post('/api/servers/{server_id}/radar/watchlist/resolve')
async def radar_watchlist_resolve(
    server_id: int,
    body: dict,
    user: dict = Depends(get_current_user),
):
    """Paste-and-resolve preview. Memecoin: a chain:address string or a
    dexscreener.com URL. NFT: a '<chain>:<slug>' (or a separate `chain` plus a
    `query` slug). Returns a snapshot preview so the admin can confirm before
    saving via POST /watchlist."""
    require_module_access(user, server_id, 'radar')
    rate_limit(f'radar_resolve:{server_id}', 30, 60.0)

    kind = (body.get('kind') or body.get('topic') or 'meme').strip().lower()
    if kind not in ('meme', 'nft'):
        raise HTTPException(
            status_code=400,
            detail='resolve supports kind=meme or kind=nft',
        )

    if kind == 'nft':
        return await _resolve_nft_preview(body)

    raw = str(body.get('input') or '').strip()
    if not raw:
        raise HTTPException(status_code=400, detail='input is required')

    from services.radar.adapters import ADAPTERS_BY_KIND
    from services.radar.adapters.dexscreener import (
        parse_meme_input, address_looks_valid, SUPPORTED_CHAINS,
    )
    parsed = parse_meme_input(raw)
    if not parsed:
        raise HTTPException(
            status_code=400,
            detail='Could not parse a chain:address or dexscreener.com URL.',
        )
    chain, address = parsed
    # chain may be None for a bare EVM address — the adapter scans all chains.
    if chain is not None and chain not in SUPPORTED_CHAINS:
        raise HTTPException(
            status_code=400,
            detail=f'Chain "{chain}" is not supported. Use one of: '
                   f'{", ".join(SUPPORTED_CHAINS)}.',
        )
    if not address_looks_valid(chain, address):
        raise HTTPException(
            status_code=400,
            detail=f'Address does not look valid for chain "{chain or "auto"}".',
        )
    adapter = ADAPTERS_BY_KIND.get('meme')
    try:
        snap = await adapter.fetch_one(f'{chain}:{address}' if chain else address)
    except Exception as e:  # noqa: BLE001
        print(f'[radar/api] resolve meme failed: {type(e).__name__}: {e}')
        raise HTTPException(status_code=503,
            detail='Memecoin lookup unavailable. Try again shortly.')
    if snap is None:
        raise HTTPException(status_code=404,
            detail='No active pair found for this token on DEXScreener. Check the address.')
    # Report the chain the adapter actually resolved (important for the
    # bare-address path where the request chain was unknown).
    resolved_chain = (snap.get('raw') or {}).get('chain') or chain
    return {
        'kind':              'meme',
        'identifier':        snap['identifier'],
        'chain':             resolved_chain,
        'address':           address,
        'symbol':            snap.get('symbol_display'),
        'name':              snap.get('raw', {}).get('name'),
        'price_usd':         snap.get('price_usd'),
        'change_24h_pct':    snap.get('change_24h_pct'),
        'volume_24h_usd':    snap.get('volume_24h_usd'),
        'image_url':         snap.get('image_url'),
        'page_url':          snap.get('page_url'),
    }


@app.get('/api/servers/{server_id}/radar/search-asset')
async def radar_search_asset(
    server_id: int,
    kind: str = 'crypto',
    q: str = '',
    user: dict = Depends(get_current_user),
):
    """Autocomplete for the dashboard add UI. Crypto via CoinGecko /search;
    NFT via OpenSea (resolves a 'chain:slug' directly — OpenSea has no fuzzy
    name search); Forex via Frankfurter /currencies (filtered). Memecoin uses
    paste-and-resolve (see /watchlist/resolve)."""
    require_module_access(user, server_id, 'radar')
    rate_limit(f'radar_search:{server_id}', 30, 60.0)
    kind = (kind or 'crypto').strip().lower()
    query = (q or '').strip()

    from services.radar.adapters import ADAPTERS_BY_KIND

    if kind == 'crypto':
        if not query:
            return {'kind': kind, 'q': '', 'suggestions': [], 'note': None}
        try:
            adapter = ADAPTERS_BY_KIND.get('crypto')
            results = await adapter.search(query, limit=10) if adapter else []
        except Exception as e:  # noqa: BLE001
            print(f'[radar/api] crypto search failed: {type(e).__name__}: {e}')
            results = []
        return {'kind': kind, 'q': query, 'suggestions': results, 'note': None}

    if kind == 'nft':
        adapter = ADAPTERS_BY_KIND.get('nft')
        if adapter is None or getattr(adapter, 'disabled_reason', None):
            return {'kind': kind, 'q': query, 'suggestions': [],
                    'note': getattr(adapter, 'disabled_reason', None)
                            or 'NFT adapter not configured.'}
        if not query:
            return {'kind': kind, 'q': '', 'suggestions': [], 'note': None}
        try:
            results = await adapter.search(query, limit=8)
        except Exception as e:  # noqa: BLE001
            print(f'[radar/api] nft search failed: {type(e).__name__}: {e}')
            results = []
        return {'kind': kind, 'q': query, 'suggestions': results, 'note': None}

    if kind == 'forex':
        adapter = ADAPTERS_BY_KIND.get('forex')
        if adapter is None:
            return {'kind': kind, 'q': query, 'suggestions': [],
                    'note': 'Forex adapter not configured.'}
        try:
            results = await adapter.search(query, limit=40)
        except Exception as e:  # noqa: BLE001
            print(f'[radar/api] forex search failed: {type(e).__name__}: {e}')
            results = []
        return {'kind': kind, 'q': query, 'suggestions': results, 'note': None}

    if kind == 'meme':
        return {
            'kind': kind, 'q': query, 'suggestions': [],
            'note': 'Paste a DEXScreener URL or chain:address and click Resolve.',
        }

    return {
        'kind':        kind,
        'q':           query,
        'suggestions': [],
        'note':        f'Search for {kind} ships in a later phase.',
    }


@app.get('/api/servers/{server_id}/radar/preview')
async def radar_preview(
    server_id: int,
    kind: str = 'crypto',
    identifier: str = '',
    user: dict = Depends(get_current_user),
):
    """Current cache snapshot for the dashboard live-preview card."""
    require_module_access(user, server_id, 'radar')
    kind = (kind or 'crypto').strip().lower()
    ident = (identifier or '').strip().lower()
    if not ident:
        raise HTTPException(status_code=400, detail='identifier is required')
    try:
        from services.radar.cache import CACHE as _RADAR_CACHE
        snap = _RADAR_CACHE.get_snapshot(kind, ident)
        is_fresh = _RADAR_CACHE.is_fresh(kind, ident)
    except Exception as e:  # noqa: BLE001
        print(f'[radar/api] preview read failed: {type(e).__name__}: {e}')
        snap, is_fresh = None, False

    # Cold cache + crypto → trigger a one-off fetch so the dashboard isn't
    # blank on first load. Other kinds just return null until their adapter
    # ships.
    if not snap and kind == 'crypto':
        try:
            from services.radar.adapters import ADAPTERS_BY_KIND
            from services.radar.cache    import CACHE as _RADAR_CACHE
            adapter = ADAPTERS_BY_KIND.get('crypto')
            if adapter:
                fresh = await adapter.fetch_one(ident)
                if fresh:
                    _RADAR_CACHE.put('crypto', fresh['identifier'], fresh)
                    snap, is_fresh = fresh, True
        except Exception as e:  # noqa: BLE001
            print(f'[radar/api] preview cold-fetch failed: {type(e).__name__}: {e}')

    return {
        'kind':       kind,
        'identifier': ident,
        'snapshot':   snap,
        'fresh':      is_fresh,
    }


@app.post('/api/servers/{server_id}/radar/preview-refresh')
async def radar_preview_refresh(
    server_id: int,
    body: Optional[dict] = None,
    user: dict = Depends(get_current_user),
):
    """One-shot batched fetch for this guild's watchlist. Optional body
    field `topic` scopes the refresh to a single kind; omitted/null
    refreshes every kind that has watchlist entries. Rate-limited
    per-user AND per-guild."""
    require_module_access(user, server_id, 'radar')
    uid = int(user.get('user_id') or user.get('id') or 0)
    if uid:
        rate_limit(f'radar_preview_refresh_u:{uid}', 4, 60.0)
    rate_limit(f'radar_preview_refresh_g:{server_id}', 12, 60.0)

    topic = ((body or {}).get('topic') or '').strip().lower() or None
    if topic and topic not in _RADAR_TOPIC_NAMES:
        raise HTTPException(status_code=400,
            detail=f'topic must be one of: {", ".join(_RADAR_TOPIC_NAMES)}')

    from services.radar.adapters import ADAPTERS_BY_KIND
    from services.radar.cache    import CACHE as _RADAR_CACHE

    kinds_to_refresh = (topic,) if topic else _RADAR_TOPIC_NAMES
    refreshed_total = 0
    requested_total = 0
    errors: list[str] = []

    for k in kinds_to_refresh:
        rows = db_list_radar_watchlist(server_id, asset_kind=k)
        # crypto/nft cache keys are lowercased; meme/forex preserve case.
        identifiers = sorted({
            ((r.get('asset_identifier') or '').strip().lower()
              if k in ('crypto', 'nft')
              else (r.get('asset_identifier') or '').strip())
            for r in rows
            if (r.get('asset_identifier') or '').strip()
        })
        requested_total += len(identifiers)
        if not identifiers:
            continue
        adapter = ADAPTERS_BY_KIND.get(k)
        if adapter is None or getattr(adapter, 'disabled_reason', None):
            errors.append(f'{k}: '
                          + (getattr(adapter, 'disabled_reason', '')
                             or 'adapter not registered'))
            continue
        try:
            fetched = await adapter.fetch_batch(identifiers)
        except Exception as e:  # noqa: BLE001
            print(f'[radar/api] preview-refresh {k} failed: '
                  f'{type(e).__name__}: {e}')
            errors.append(f'{k}: refresh unavailable')
            continue
        for snap in fetched:
            _RADAR_CACHE.put(k, snap['identifier'], snap)
        refreshed_total += len(fetched)

    if requested_total == 0:
        return {'ok': True, 'refreshed': 0, 'note': 'empty_watchlist',
                'topic': topic}
    if refreshed_total == 0 and errors:
        raise HTTPException(status_code=503,
            detail='Live price refresh is temporarily unavailable. ' + '; '.join(errors))

    return {
        'ok':         True,
        'topic':      topic,
        'refreshed':  refreshed_total,
        'requested':  requested_total,
        'errors':     errors,
    }


@app.post('/api/servers/{server_id}/radar/digest/send-now')
async def radar_digest_send_now(
    server_id: int,
    body: dict,
    user: dict = Depends(get_current_user),
):
    """Admin-triggered immediate per-topic digest. Body: {"topic":
    "crypto"|"nft"|"meme"|"forex"}. Per-(guild, topic) 5/UTC-day cap +
    5-min cooldown. Quota is consumed ONLY on successful send."""
    require_guild_admin(user, server_id)
    require_module_access(user, server_id, 'radar')

    topic = ((body or {}).get('topic') or '').strip().lower()
    if topic not in _RADAR_TOPIC_NAMES:
        raise HTTPException(status_code=400,
            detail=f'topic must be one of: {", ".join(_RADAR_TOPIC_NAMES)}')

    settings = db_get_radar_topic_settings(server_id, topic)
    cap = _RADAR_MANUAL_DIGEST_DAILY_CAP

    # UTC-day quota reset.
    today_utc = datetime.now(timezone.utc).date().isoformat()
    used = int(settings.get('manual_digests_used_today') or 0)
    if str(settings.get('manual_digests_reset_date') or '') != today_utc:
        used = 0
        db_update_radar_topic_settings(
            server_id, topic,
            manual_digests_used_today=0,
            manual_digests_reset_date=today_utc,
        )
        settings = db_get_radar_topic_settings(server_id, topic)

    if used >= cap:
        raise HTTPException(
            status_code=429,
            detail=(f'Daily cap reached for {topic} ({cap} manual sends per '
                    'UTC day). The scheduled daily digest still runs at the '
                    'configured time.'),
            headers={'Retry-After': '3600'},
        )

    # 5-minute cooldown — derived from radar_topic_settings.updated_at via
    # the last-sent date / time tuple; we use last_daily_sent_date plus a
    # transient per-process timestamp would be wrong across restarts, so
    # we read the cooldown from the most-recent radar_alerts_log entry of
    # type='digest_sent' if present — simpler: just compare manual_digests_*
    # update_at field.
    # Use a lightweight in-row marker: re-use manual_digests_reset_date AND
    # check the row's updated_at column.
    last_iso = settings.get('updated_at')
    if last_iso:
        try:
            last_dt = datetime.fromisoformat(str(last_iso).replace('Z', '+00:00'))
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds()
            # The 5-minute cooldown only applies when the row was last
            # touched specifically by a manual send (i.e. used_today > 0
            # AND reset_date == today). This avoids treating a settings
            # PATCH as a manual-send heartbeat.
            if used > 0 and elapsed < _RADAR_MANUAL_DIGEST_COOLDOWN_S:
                wait = int(_RADAR_MANUAL_DIGEST_COOLDOWN_S - elapsed) + 1
                raise HTTPException(
                    status_code=429,
                    detail=(f'Manual {topic} digests are limited to one every '
                            f'5 minutes. Try again in {wait}s.'),
                    headers={'Retry-After': str(wait)},
                )
        except (TypeError, ValueError):
            pass

    if not settings.get('daily_channel'):
        raise HTTPException(
            status_code=400,
            detail=f'Configure a {topic} daily channel first.',
        )

    if not bot.is_ready():
        raise HTTPException(status_code=503, detail='Bot not ready yet')

    try:
        from services.radar.digest import post_digest_now, DigestSendError
        result = await post_digest_now(bot, int(server_id), topic)
    except DigestSendError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:  # noqa: BLE001
        print(f'[radar/api] digest send-now crashed g={server_id} t={topic}: '
              f'{type(e).__name__}: {e}')
        raise HTTPException(status_code=502, detail='Unexpected error sending digest.')

    # Consume quota only after a confirmed post.
    db_update_radar_topic_settings(
        server_id, topic,
        manual_digests_used_today=used + 1,
        manual_digests_reset_date=today_utc,
    )

    log_event(
        server_id, 'admin_action', 'radar_digest_send_now',
        f'Manual Radar {topic} digest sent by {user.get("username")}',
        actor_user_id=user.get('user_id') or user.get('id'),
        actor_username=user.get('username'),
        module='radar', severity='info',
        details={
            'topic':              topic,
            'channel_id':         result.get('channel_id'),
            'message_id':         result.get('message_id'),
            'watchlist_count':    result.get('watchlist_count'),
            'remaining_today':    max(0, cap - (used + 1)),
        },
    )

    return {
        'ok':                True,
        'topic':             topic,
        'message_id':        str(result.get('message_id')),
        'channel_id':        str(result.get('channel_id')),
        'watchlist_count':   result.get('watchlist_count'),
        'used_today':        used + 1,
        'remaining_today':   max(0, cap - (used + 1)),
        'daily_cap':         cap,
    }


@app.get('/api/servers/{server_id}/radar/alerts/recent')
async def radar_alerts_recent(
    server_id: int,
    limit: int = 50,
    user: dict = Depends(get_current_user),
):
    require_module_access(user, server_id, 'radar')
    rows = db_list_recent_radar_alerts(server_id, limit=max(1, min(int(limit), 200)))
    out = []
    for r in rows:
        try:
            payload = json.loads(r.get('payload_json') or '{}')
        except (TypeError, ValueError):
            payload = {}
        out.append({
            'id':               int(r['id']),
            'asset_kind':       r.get('asset_kind'),
            'asset_identifier': r.get('asset_identifier'),
            'alert_type':       r.get('alert_type'),
            'payload':          payload,
            'sent_at':          r.get('sent_at'),
        })
    return {'alerts': out}


# ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ──
#  RAID ENDPOINTS
# ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ──

import re as _re
_TWEET_URL_RE = _re.compile(r'^https?://(x|twitter)\.com/.+/status/\d+', _re.IGNORECASE)

_MANUAL_CHECK_DAILY_LIMIT = 10


def _validate_tweet_url(url: str):
    if not _TWEET_URL_RE.match(url or ''):
        raise HTTPException(
            status_code=400,
            detail='Invalid tweet URL — must be an x.com or twitter.com /status/ URL',
        )


class _RaidSettingsUpdate(BaseModel):
    enabled: Optional[int] = None
    point_ratio_like: Optional[int] = None
    point_ratio_comment: Optional[int] = None
    point_ratio_retweet: Optional[int] = None
    raid_channel_id: Optional[str] = None
    raid_role_ids: Optional[str] = None
    raid_ping_role_id: Optional[str] = None
    embed_thumbnail_url: Optional[str] = None
    embed_footer_text: Optional[str] = None
    embed_color: Optional[str] = None
    # Guide section
    raid_guide_channel_id: Optional[str] = None
    raid_guide_title: Optional[str] = None
    raid_guide_description: Optional[str] = None
    raid_guide_thumbnail_url: Optional[str] = None
    raid_guide_image_url: Optional[str] = None
    raid_guide_color: Optional[str] = None
    raid_guide_footer_text: Optional[str] = None


class _RaidCreate(BaseModel):
    tweet_url: str
    total_points: int
    mode: str = 'partial'
    tasks: dict = {'like': True, 'comment': True, 'retweet': True}


class _RaidManualCheck(BaseModel):
    raid_id: int
    identifier: str


@app.get('/api/servers/{server_id}/raid/settings')
async def raid_get_settings(server_id: int, user: dict = Depends(get_current_user)):
    require_module_access(user, server_id, 'raid')
    from cogs.raidbot import LIVE_VERIFICATION_GUILD_IDS as _LIVE_VER_GUILDS
    s = db_get_raid_settings(server_id)
    return {
        **s,
        'unlimited_manual_check': server_id in _UNLIMITED_MC_GUILDS,
        'live_verification_mode': server_id in _LIVE_VER_GUILDS,
    }


@app.patch('/api/servers/{server_id}/raid/settings')
async def raid_update_settings(
    server_id: int, body: _RaidSettingsUpdate, user: dict = Depends(get_current_user),
):
    require_module_access(user, server_id, 'raid')
    updates = {k: v for k, v in body.model_dump(exclude_none=True).items()}

    ratio_keys = {'point_ratio_like', 'point_ratio_comment', 'point_ratio_retweet'}
    if any(k in updates for k in ratio_keys):
        current = db_get_raid_settings(server_id)
        total = (
            updates.get('point_ratio_like',    current.get('point_ratio_like', 12)) +
            updates.get('point_ratio_comment', current.get('point_ratio_comment', 40)) +
            updates.get('point_ratio_retweet', current.get('point_ratio_retweet', 48))
        )
        if total != 100:
            raise HTTPException(
                status_code=400,
                detail=f'Point ratios must sum to 100 (got {total})',
            )

    if updates:
        db_upsert_raid_settings(server_id, **updates)
    return db_get_raid_settings(server_id)


@app.get('/api/servers/{server_id}/raid/raids')
async def raid_list(
    server_id: int,
    status: str = 'active',
    limit: int = 50,
    user: dict = Depends(get_current_user),
):
    require_module_access(user, server_id, 'raid')
    raids = db_list_guild_raids(server_id, status=status, limit=min(limit, 200))
    for r in raids:
        with get_connection() as conn:
            cnt = conn.execute(
                "SELECT COUNT(*) FROM raid_participation WHERE raid_id=? AND guild_id=?",
                (r['raid_id'], server_id),
            ).fetchone()[0]
        r['participant_count'] = cnt
    return {'raids': raids}


@app.post('/api/servers/{server_id}/raid/raids')
async def raid_create(
    server_id: int, body: _RaidCreate, user: dict = Depends(get_current_user),
):
    require_module_access(user, server_id, 'raid')
    _validate_tweet_url(body.tweet_url)

    if body.total_points < 1:
        raise HTTPException(status_code=400, detail='total_points must be ≥ 1')
    if body.mode not in ('all', 'partial'):
        raise HTTPException(status_code=400, detail="mode must be 'all' or 'partial'")

    from cogs._twitter import extract_tweet_id
    tweet_id = extract_tweet_id(body.tweet_url)
    if not tweet_id:
        raise HTTPException(status_code=400, detail='Could not parse tweet ID from URL')

    tasks_json = json.dumps({t: bool(v) for t, v in body.tasks.items()
                              if t in ('like', 'comment', 'retweet')})

    if not bot.is_ready():
        raise HTTPException(status_code=503, detail='Bot not ready')
    guild = bot.get_guild(server_id)
    if guild is None:
        raise HTTPException(status_code=404, detail='Bot not in server')

    from cogs.raidbot import _fetch_tweet, _build_raid_embed, build_raid_panel_view
    from cogs._utils import resolve_channel, resolve_role

    settings   = db_get_raid_settings(server_id)
    tweet_data = _fetch_tweet(body.tweet_url)

    ch_val = (settings.get('raid_channel_id') or '').strip()
    if not ch_val:
        raise HTTPException(
            status_code=400,
            detail='Raid channel not configured. Set raid_channel_id in Raid Settings first.',
        )
    channel = resolve_channel(guild, ch_val)
    if channel is None:
        raise HTTPException(status_code=400, detail=f'Raid channel not found: {ch_val}')

    user_id_int = int(user['user_id'])
    raid_id = db_create_guild_raid(
        server_id, body.tweet_url, tweet_id,
        body.total_points, body.mode, tasks_json, user_id_int,
    )
    raid = db_get_guild_raid(raid_id, server_id)

    embed = _build_raid_embed(server_id, raid, tweet_data, settings)
    view  = build_raid_panel_view(raid_id)

    ping_raw  = (settings.get('raid_ping_role_id') or settings.get('ping_role_id') or '').strip()
    ping_role = resolve_role(guild, ping_raw) if ping_raw else None
    content   = f'{ping_role.mention} — new raid just dropped! ⚔️' if ping_role else '⚔️ New raid!'

    try:
        msg = await channel.send(content=content, embed=embed, view=view,
                                  allowed_mentions=discord.AllowedMentions(roles=True))
        bot.add_view(view, message_id=msg.id)
    except discord.Forbidden:
        raise HTTPException(status_code=400, detail='Bot lacks permission to send in that channel')
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    db_update_guild_raid(raid_id, server_id,
                         channel_id=str(channel.id), message_id=str(msg.id))

    return {'ok': True, 'raid_id': raid_id,
            'display_number': raid.get('display_number', raid_id),
            'message_id': str(msg.id), 'channel_id': str(channel.id)}


@app.post('/api/servers/{server_id}/raid/raids/{raid_id}/end')
async def raid_end(
    server_id: int, raid_id: int, user: dict = Depends(get_current_user),
):
    require_module_access(user, server_id, 'raid')
    raid = db_get_guild_raid(raid_id, server_id)
    if not raid:
        raise HTTPException(status_code=404, detail='Raid not found')
    db_end_raid(raid_id, server_id, ended_reason='admin')
    return {'ok': True, 'raid_id': raid_id}


@app.get('/api/servers/{server_id}/raid/leaderboard')
async def raid_leaderboard_api(
    server_id: int, limit: int = 10, user: dict = Depends(get_current_user),
):
    require_module_access(user, server_id, 'raid')
    return {'leaderboard': db_get_raid_leaderboard(server_id, min(limit, 100))}


@app.get('/api/servers/{server_id}/raid/verification-log')
async def raid_verification_log(
    server_id: int,
    status: str = 'flagged',
    limit: int = 50,
    user: dict = Depends(get_current_user),
):
    """Return aggregated verification flags — one row per (user, raid) with task details."""
    require_module_access(user, server_id, 'raid')
    guild = bot.get_guild(server_id) if bot.is_ready() else None

    having_clause = (
        '' if status == 'all'
        else 'HAVING SUM(CASE WHEN verified=0 THEN 1 ELSE 0 END) > 0'
    )
    with get_connection() as conn:
        agg_rows = conn.execute(f"""
            SELECT
                user_id, raid_id,
                COUNT(*) AS task_count,
                SUM(CASE WHEN verified=0 THEN 1 ELSE 0 END) AS failed_count,
                MAX(checked_at) AS last_checked,
                MAX(source) AS source
            FROM raid_verification_log
            WHERE guild_id=?
            GROUP BY user_id, raid_id
            {having_clause}
            ORDER BY last_checked DESC
            LIMIT ?
        """, (server_id, min(limit, 200))).fetchall()

        flags = []
        for row in agg_rows:
            d = dict(row)
            member = guild.get_member(d['user_id']) if guild else None
            d['discord_username'] = member.name if member else f'user_{d["user_id"]}'
            d['twitter_username'] = db_get_user_x_username(d['user_id']) or '(not linked)'
            raid = db_get_guild_raid(d['raid_id'], server_id)
            d['tweet_url'] = (raid or {}).get('tweet_url', '')
            task_rows = conn.execute(
                "SELECT task, claimed, verified, error_text FROM raid_verification_log "
                "WHERE guild_id=? AND user_id=? AND raid_id=? ORDER BY task",
                (server_id, d['user_id'], d['raid_id']),
            ).fetchall()
            tasks = []
            for t in task_rows:
                td = dict(t)
                v = td.get('verified')
                # Map DB int (1/0/-1) to JSON bool/null so frontend === comparisons work
                td['verified'] = True if v == 1 else (False if v == 0 else None)
                tasks.append(td)
            d['tasks'] = tasks
            flags.append(d)

    return {'flags': flags}


@app.post('/api/servers/{server_id}/raid/manual-check')
async def raid_manual_check(
    server_id: int, body: _RaidManualCheck, user: dict = Depends(get_current_user),
):
    require_module_access(user, server_id, 'raid')
    print(f'[raid] manual-check endpoint: server={server_id} raid_id={body.raid_id} identifier={body.identifier!r}')

    is_unlimited = server_id in _UNLIMITED_MC_GUILDS

    settings   = db_check_reset_manual_count(server_id)
    used_today = settings.get('manual_check_count_today', 0)
    if not is_unlimited and used_today >= _MANUAL_CHECK_DAILY_LIMIT:
        raise HTTPException(
            status_code=429,
            detail=f'Daily manual check limit ({_MANUAL_CHECK_DAILY_LIMIT}) reached. Resets at midnight UTC.',
        )

    if not bot.is_ready():
        raise HTTPException(status_code=503, detail='Bot not ready')

    cog = bot.get_cog('Raids')
    if cog is None:
        raise HTTPException(status_code=503, detail='Raids cog not loaded')

    result = await cog.manual_check(server_id, body.raid_id, body.identifier)
    if 'error' not in result and not is_unlimited:
        db_upsert_raid_settings(server_id, manual_check_count_today=used_today + 1)

    return {
        'used_today':       used_today + (0 if ('error' in result or is_unlimited) else 1),
        'limit':            None if is_unlimited else _MANUAL_CHECK_DAILY_LIMIT,
        'unlimited':        is_unlimited,
        **result,
    }


@app.get('/api/servers/{server_id}/raid/scraping-health')
async def raid_scraping_health(server_id: int, user: dict = Depends(get_current_user)):
    require_module_access(user, server_id, 'raid')
    try:
        from cogs._twitter import get_scraping_health
        return get_scraping_health()
    except Exception as e:
        return {'healthy': None, 'consecutive_failures': 0, 'error': str(e)}


@app.post('/api/servers/{server_id}/raid/send-guide')
async def raid_send_guide(server_id: int, user: dict = Depends(get_current_user)):
    require_module_access(user, server_id, 'raid')
    settings = db_get_raid_settings(server_id)

    if not bot.is_ready():
        raise HTTPException(status_code=503, detail='Bot not ready')
    guild = bot.get_guild(server_id)
    if guild is None:
        raise HTTPException(status_code=404, detail='Bot not in server')

    from cogs._utils import resolve_channel
    from cogs.raidbot import DEFAULT_GUIDE_TITLE, DEFAULT_GUIDE_DESCRIPTION

    ch_val = (settings.get('raid_guide_channel_id') or '').strip()
    if not ch_val:
        raise HTTPException(status_code=400, detail='Guide channel not configured (raid_guide_channel_id)')

    channel = resolve_channel(guild, ch_val)
    if channel is None:
        raise HTTPException(status_code=400, detail=f'Guide channel not found: {ch_val}')

    title       = (settings.get('raid_guide_title') or DEFAULT_GUIDE_TITLE).strip()
    description = (settings.get('raid_guide_description') or DEFAULT_GUIDE_DESCRIPTION).strip()
    color_str   = (settings.get('raid_guide_color') or '').strip()
    try:
        color = int(color_str.lstrip('#'), 16) if color_str else 0x94730D
    except ValueError:
        color = 0x94730D

    embed = discord.Embed(title=title, description=description, color=color)

    thumb = (settings.get('raid_guide_thumbnail_url') or '').strip()
    if thumb:
        embed.set_thumbnail(url=thumb)
    image = (settings.get('raid_guide_image_url') or '').strip()
    if image:
        embed.set_image(url=image)
    footer = (settings.get('raid_guide_footer_text') or '').strip()
    if footer:
        embed.set_footer(text=footer)

    try:
        msg = await channel.send(embed=embed)
    except discord.Forbidden:
        raise HTTPException(status_code=400, detail='Bot lacks permission to post in guide channel')
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {'ok': True, 'message_id': str(msg.id), 'channel_id': str(channel.id)}


@app.get('/api/raid/guide-defaults')
async def raid_guide_defaults():
    """Return the default guide title and description (no auth — public constants)."""
    from cogs.raidbot import DEFAULT_GUIDE_TITLE, DEFAULT_GUIDE_DESCRIPTION
    return {'title': DEFAULT_GUIDE_TITLE, 'description': DEFAULT_GUIDE_DESCRIPTION}


# ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ──
#  ASSETS LIBRARY ENDPOINTS
# ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ──

@app.get('/api/servers/{server_id}/assets')
async def list_assets(server_id: int, user: dict = Depends(get_current_user)):
    """List all non-deleted assets for this guild."""
    require_guild_admin(user, server_id)
    return {'assets': db_list_guild_assets(server_id)}


@app.post('/api/servers/{server_id}/assets/upload')
async def upload_asset(
    server_id: int,
    file: UploadFile = File(...),
    user: dict = Depends(get_current_user),
):
    """Upload a file to R2 and record it in the assets library."""
    require_guild_admin(user, server_id)

    contents = await file.read()
    if not contents:
        raise HTTPException(status_code=400, detail='Empty file')

    try:
        result = r2_upload(
            guild_id=server_id,
            filename=file.filename or 'upload',
            file_bytes=contents,
            content_type=file.content_type,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=f'R2 not configured: {e}')
    except Exception as e:
        print(f'[upload_asset] r2 error: {type(e).__name__}: {e}')
        raise HTTPException(status_code=500, detail='Upload to R2 failed')

    user_id = int(user['user_id']) if user.get('user_id') else 0
    asset_id = db_create_asset_record(
        guild_id=server_id,
        file_id=result['file_id'],
        key=result['key'],
        url=result['url'] or '',
        original_name=file.filename or '',
        size=result['size'],
        content_type=result['content_type'],
        extension=result['extension'],
        uploaded_by=user_id,
    )

    return {
        'asset_id':     asset_id,
        'url':          result['url'],
        'size':         result['size'],
        'extension':    result['extension'],
        'original_name': file.filename,
    }


@app.delete('/api/servers/{server_id}/assets/{asset_id}')
async def delete_asset(
    server_id: int,
    asset_id: int,
    user: dict = Depends(get_current_user),
):
    """Soft-delete an asset record and remove it from R2."""
    require_guild_admin(user, server_id)

    record = db_soft_delete_asset(server_id, asset_id)
    if record is None:
        raise HTTPException(status_code=404, detail='Asset not found in this guild')

    r2_delete(record['key'])

    return {'ok': True}


# ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ──
#  GLOBAL ADMIN — TWITTER ACCOUNT POOL MANAGEMENT
# ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ──

# Discord user ID(s) allowed to manage the global Twitter account pool.
# Only the bot owner should be here — this is a global (cross-guild) resource.
_GLOBAL_ADMIN_IDS = {461460143343927306}


def require_global_admin(user: dict):
    """Owner-only global gate.

    The id is taken ONLY from the verified JWT payload (`user` is the decoded,
    signature-checked token from get_current_user) — never from a client-supplied
    body/query/header that could be spoofed. This is intentionally DISTINCT from
    require_guild_admin / require_module_access: a guild owner or guild admin of
    ANY guild is NOT granted access unless their id is in _GLOBAL_ADMIN_IDS.
    """
    raw = user.get('user_id')
    try:
        uid = int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        uid = 0
    if uid not in _GLOBAL_ADMIN_IDS:
        raise HTTPException(status_code=403, detail='Global admin only')


class _TwAccountActivate(BaseModel):
    active: int  # 0 or 1


@app.get('/api/admin/twitter-accounts')
async def tw_list_accounts(user: dict = Depends(get_current_user)):
    """Return all configured Twitter account slots and their active status.
    Credentials are NEVER included in the response."""
    require_global_admin(user)
    from database import list_twitter_accounts
    rows = list_twitter_accounts()
    # Strip any field that could reveal credentials — only expose slot/username/active/last_used/notes
    safe = [
        {'slot': r['slot'], 'username': r['username'],
         'active': r['active'], 'last_used': r['last_used'], 'notes': r['notes']}
        for r in rows
    ]
    return {'accounts': safe}


@app.patch('/api/admin/twitter-accounts/{slot}')
async def tw_set_account_active(
    slot: int, body: _TwAccountActivate, user: dict = Depends(get_current_user),
):
    """Activate or deactivate a Twitter account slot (legacy endpoint — no-op pool reload with Apify)."""
    require_global_admin(user)
    from database import set_twitter_account_active
    if not set_twitter_account_active(slot, 1 if body.active else 0):
        raise HTTPException(status_code=404, detail=f'Slot {slot} not found in DB')
    from cogs._twitter import reload_api
    await reload_api()
    return {'ok': True, 'slot': slot, 'active': body.active}


@app.post('/api/admin/test-twitter')
async def admin_test_twitter(user: dict = Depends(get_current_user)):
    """Run a live TwitterAPI.io test lookup and return the result with health status."""
    require_global_admin(user)
    from cogs._twitter import lookup_twitter_user_by_login, get_scraping_health
    try:
        result = await lookup_twitter_user_by_login('twitter')
        health = get_scraping_health()
        if result:
            return {'status': 'ok', 'result': result, 'health': health}
        return {
            'status': 'failed',
            'message': 'TwitterAPI.io returned no data — check TWITTER_API_IO_KEY',
            'health': health,
            'hint': 'Verify key at https://twitterapi.io',
        }
    except Exception as e:
        return {'status': 'error', 'error': f'{type(e).__name__}: {e}'}


# ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ──
#  GLOBAL ADMIN — SECURE DATABASE BACKUPS (OWNER ONLY)
# ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ──
# All three operations are gated by require_global_admin (owner Discord id
# 461460143343927306 only — NOT guild admins). Backups always use sqlite3's
# online backup API for a consistent copy; the raw live file is never streamed.
# log_event attribution uses the owner's home guild id.
_BACKUP_LOG_GID = 1199707792706117642


@app.get('/api/admin/backup/download')
async def admin_backup_download(request: Request, user: dict = Depends(get_current_user)):
    """Owner-only: download a consistent point-in-time copy of the live DB.

    Makes a sqlite3 .backup() snapshot to a temp file, streams it as a dated
    .db attachment, and deletes the temp file after the response is sent.
    """
    require_global_admin(user)
    uid = int(user.get('user_id', 0))
    # Heavy endpoint (full DB copy + transfer): tight per-owner rate limit.
    rate_limit(f'backup:download:{uid}', max_calls=3, window_secs=300.0)

    from backup_service import make_consistent_copy, backup_filename
    try:
        tmp_path = await asyncio.to_thread(make_consistent_copy)
    except Exception as e:
        log_event(
            _BACKUP_LOG_GID, 'admin_action', 'db_backup_download_failed',
            f'On-demand DB backup copy failed: {type(e).__name__}: {e}',
            actor_user_id=uid, actor_username=user.get('username'),
            module='backup', severity='error',
        )
        raise HTTPException(status_code=500, detail='Backup copy failed')

    fname = backup_filename()
    log_event(
        _BACKUP_LOG_GID, 'admin_action', 'db_backup_downloaded',
        f'Owner downloaded a consistent DB backup ({fname})',
        actor_user_id=uid, actor_username=user.get('username'),
        module='backup', severity='warning',
        details={'filename': fname},
    )

    def _cleanup(path=tmp_path):
        try:
            os.remove(path)
        except OSError:
            pass

    return FileResponse(
        tmp_path,
        media_type='application/x-sqlite3',
        filename=fname,
        background=BackgroundTask(_cleanup),
    )


@app.post('/api/admin/backup/run-now')
async def admin_backup_run_now(request: Request, user: dict = Depends(get_current_user)):
    """Owner-only: immediately run the R2 backup (consistent copy + upload +
    retention prune) and return the resulting R2 key. Lets the owner force a
    backup before risky changes."""
    require_global_admin(user)
    uid = int(user.get('user_id', 0))
    rate_limit(f'backup:runnow:{uid}', max_calls=3, window_secs=300.0)

    from backup_service import upload_backup_to_r2
    try:
        result = await asyncio.to_thread(upload_backup_to_r2)
    except Exception as e:
        log_event(
            _BACKUP_LOG_GID, 'admin_action', 'db_backup_failed',
            f'Owner-triggered R2 backup failed: {type(e).__name__}: {e}',
            actor_user_id=uid, actor_username=user.get('username'),
            module='backup', severity='error',
            details={'trigger': 'run_now', 'error': f'{type(e).__name__}: {e}'},
        )
        raise HTTPException(status_code=500, detail=f'Backup failed: {type(e).__name__}')

    log_event(
        _BACKUP_LOG_GID, 'admin_action', 'db_backup_success',
        f'Owner-triggered DB backup uploaded to R2: {result["key"]}',
        actor_user_id=uid, actor_username=user.get('username'),
        module='backup', severity='info',
        details={'trigger': 'run_now', **result},
    )
    return {
        'ok':     True,
        'key':    result['key'],
        'size':   result['size'],
        'pruned': result['pruned'],
    }


# ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ──
#  GLOBAL ADMIN — CROSS-TENANT OVERVIEW (OWNER ONLY)
# ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ──
# Exposes cross-tenant business data (every guild, members, usage). Owner-only,
# same two-layer model as the backup endpoints: require_global_admin here
# (JWT-derived id, NOT guild admin) + frontend visibility gate. Aggregates come
# only from existing tables (database.get_global_overview) merged with live
# guild info from the bot instance.


@app.get('/api/admin/overview')
async def admin_global_overview(request: Request, user: dict = Depends(get_current_user)):
    """Owner-only: aggregate stats across ALL guilds the bot is in."""
    require_global_admin(user)
    uid = int(user.get('user_id', 0))
    rate_limit(f'admin:overview:{uid}', max_calls=30, window_secs=60.0)

    bot_instance = _get_bot_instance()
    from database import get_global_overview
    agg = await asyncio.to_thread(get_global_overview)

    guilds = []
    total_members = 0
    for g in bot_instance.guilds:
        gid = str(g.id)
        members = g.member_count or agg['snap_by_guild'].get(gid, 0) or 0
        total_members += members
        try:
            me = g.me
            added_at = me.joined_at.isoformat() if me and me.joined_at else None
        except Exception:
            added_at = None
        guilds.append({
            'id':          gid,
            'name':        g.name,
            'icon':        str(g.icon.url) if g.icon else None,
            'members':     members,
            'added_at':    added_at,
            'modules':     agg['modules_by_guild'].get(gid, []),
            'raids':       agg['raids_by_guild'].get(gid, 0),
            'points':      agg['points_by_guild'].get(gid, 0),
            'engage_subs': agg['engage_by_guild'].get(gid, 0),
            'last_active': agg['lastact_by_guild'].get(gid),
            'is_premium':  g.id in PREMIUM_GUILD_IDS,
        })

    totals = {
        'guilds':      len(guilds),
        'members':     total_members,
        'raids':       sum(x['raids'] for x in guilds),
        'points':      sum(x['points'] for x in guilds),
        'engage_subs': sum(x['engage_subs'] for x in guilds),
    }
    most_active = sorted(
        guilds, key=lambda x: (x['raids'] + x['engage_subs'], x['members']), reverse=True
    )[:5]

    log_event(
        _BACKUP_LOG_GID, 'admin_action', 'global_overview_viewed',
        f'Owner viewed global tenant overview ({totals["guilds"]} guilds)',
        actor_user_id=uid, actor_username=user.get('username'),
        module='admin', severity='info',
    )

    return {
        'totals':      totals,
        'guilds':      guilds,
        'most_active': [
            {'id': x['id'], 'name': x['name'], 'raids': x['raids'], 'engage_subs': x['engage_subs']}
            for x in most_active
        ],
    }


# ── Engage admin endpoints ────────────────────────────────────────────────────

_AMERETAVERSE_GID = 1199707792706117642


@app.get('/api/servers/{server_id}/engage/pools')
async def engage_pools_get(server_id: int, user: dict = Depends(get_current_user)):
    require_module_access(user, server_id, 'engage')
    import json as _json
    from database import list_engage_pools, ensure_default_pool
    pools = list_engage_pools(server_id)
    if not pools and server_id != _AMERETAVERSE_GID:
        ensure_default_pool(server_id)
        pools = list_engage_pools(server_id)
    for p in pools:
        try:
            p['allowed_role_ids'] = _json.loads(p.get('allowed_role_ids') or '[]')
        except Exception:
            p['allowed_role_ids'] = []
    return {
        'guild_id':      str(server_id),
        'is_multi_pool': server_id == _AMERETAVERSE_GID,
        'pools':         pools,
    }


@app.put('/api/servers/{server_id}/engage/pools/{pool_id}')
async def engage_pool_update(
    server_id: int, pool_id: int, body: dict,
    user: dict = Depends(get_current_user),
):
    require_module_access(user, server_id, 'engage')
    import json as _json
    from database import get_engage_pool_by_id, update_engage_pool
    pool = get_engage_pool_by_id(pool_id)
    if not pool or str(pool['guild_id']) != str(server_id):
        raise HTTPException(status_code=404, detail='Pool not found in this guild')

    allowed = {
        'enabled', 'channel_id', 'allowed_role_ids',
        'submit_cost', 'ttl_hours', 'auto_reset_daily',
        'min_followers', 'daily_submission_limit',
        'point_ratio_like', 'point_ratio_comment', 'point_ratio_retweet',
        'total_points_per_engage', 'allow_like', 'allow_comment', 'allow_retweet',
        'embed_color', 'embed_thumbnail_url', 'embed_footer_text', 'embed_footer_icon_url',
        'guide_title', 'guide_description', 'guide_image_url',
    }
    payload = {k: v for k, v in body.items() if k in allowed}

    if 'allowed_role_ids' in payload:
        ids = payload['allowed_role_ids']
        if not isinstance(ids, list):
            ids = []
        payload['allowed_role_ids'] = _json.dumps([str(x).strip() for x in ids if str(x).strip()])

    if 'ttl_hours' in payload:
        v = payload['ttl_hours']
        payload['ttl_hours'] = None if v in ('', None, 0, '0') else int(v)

    if 'channel_id' in payload:
        v = payload['channel_id']
        payload['channel_id'] = None if v in ('', None) else str(v).lstrip('#').strip()

    update_engage_pool(pool_id, **payload)
    updated = get_engage_pool_by_id(pool_id)
    try:
        updated['allowed_role_ids'] = _json.loads(updated.get('allowed_role_ids') or '[]')
    except Exception:
        updated['allowed_role_ids'] = []
    return updated


# ── Wallet Collection endpoints (inside the Engage module) ────────────────────
# Wallet Collections live under the Engage module's access gate, so admins who
# can see Engage can manage wallet collections. Paths mirror the codebase
# convention (/api/servers/{server_id}/...). Snowflakes stay strings throughout.

from cogs._wallet_validation import SUPPORTED_CHAINS as _WALLET_CHAINS


class _WalletCollectionCreate(BaseModel):
    name: str
    blockchain: Optional[str] = 'evm'
    channel_id: Optional[str] = ''
    required_role_id: Optional[str] = None
    ping_role_ids: Optional[object] = None
    embed_title: Optional[str] = None
    embed_description: Optional[str] = None
    embed_color: Optional[str] = None
    button_label: Optional[str] = None
    modal_title: Optional[str] = None
    modal_field_label: Optional[str] = None
    modal_placeholder: Optional[str] = None


class _WalletCollectionUpdate(BaseModel):
    name: Optional[str] = None
    blockchain: Optional[str] = None
    channel_id: Optional[str] = None
    required_role_id: Optional[str] = None
    ping_role_ids: Optional[object] = None
    embed_title: Optional[str] = None
    embed_description: Optional[str] = None
    embed_color: Optional[str] = None
    button_label: Optional[str] = None
    modal_title: Optional[str] = None
    modal_field_label: Optional[str] = None
    modal_placeholder: Optional[str] = None


def _wallet_collection_to_dict(c: dict) -> dict:
    """Hydrate a collection row for the dashboard: parse ping_role_ids JSON,
    expose color as '#RRGGBB', stringify snowflakes, surface submission_count."""
    try:
        ping = json.loads(c.get('ping_role_ids') or '[]')
        if not isinstance(ping, list):
            ping = []
    except (TypeError, ValueError):
        ping = []
    color_int = c.get('embed_color')
    color_hex = f'#{int(color_int):06x}' if color_int is not None else None
    return {
        'id':                int(c['id']),
        'guild_id':          str(c['guild_id']),
        'name':              c.get('name') or '',
        'channel_id':        c.get('channel_id') or '',
        'message_id':        c.get('message_id') or '',
        'ping_role_ids':     [str(r) for r in ping if str(r).strip()],
        'required_role_id':  str(c['required_role_id']) if c.get('required_role_id') else None,
        'blockchain':        c.get('blockchain') or 'evm',
        'embed_title':       c.get('embed_title') or '',
        'embed_description': c.get('embed_description') or '',
        'embed_color':       color_hex,
        'button_label':      c.get('button_label') or '',
        'modal_title':       c.get('modal_title') or '',
        'modal_field_label': c.get('modal_field_label') or '',
        'modal_placeholder': c.get('modal_placeholder') or '',
        'status':            c.get('status') or 'draft',
        'created_at':        c.get('created_at'),
        'updated_at':        c.get('updated_at'),
        'submission_count':  int(c.get('submission_count') or 0),
    }


def _wallet_normalize_payload(payload: dict) -> dict:
    """Validate + normalize the create/update field set into DB column values.
    Raises HTTPException(400) on bad input."""
    out: dict = {}
    if 'name' in payload and payload['name'] is not None:
        name = (payload['name'] or '').strip()
        if not name:
            raise HTTPException(status_code=400, detail='name cannot be empty')
        out['name'] = name[:100]
    if 'blockchain' in payload and payload['blockchain'] is not None:
        chain = (payload['blockchain'] or '').strip().lower()
        if chain not in _WALLET_CHAINS:
            raise HTTPException(status_code=400,
                detail=f'Unsupported blockchain. Choose one of: {", ".join(_WALLET_CHAINS)}')
        out['blockchain'] = chain
    if 'channel_id' in payload:
        v = payload['channel_id']
        out['channel_id'] = None if v in ('', None) else str(v).lstrip('#').strip()
    if 'required_role_id' in payload:
        v = payload['required_role_id']
        out['required_role_id'] = None if v in ('', None) else str(v).strip()
    if 'ping_role_ids' in payload and payload['ping_role_ids'] is not None:
        out['ping_role_ids'] = _normalize_role_id_list(
            payload['ping_role_ids'], field='ping_role_ids')
    if 'embed_color' in payload:
        col = _coerce_giveaway_color(payload['embed_color'])
        out['embed_color'] = col
    for key, limit in (
        ('embed_title', 256), ('embed_description', 4000),
        ('button_label', 80), ('modal_title', 45),
        ('modal_field_label', 45), ('modal_placeholder', 100),
    ):
        if key in payload and payload[key] is not None:
            out[key] = (payload[key] or '')[:limit]
    return out


@app.get('/api/servers/{server_id}/wallet-collections')
async def wallet_collections_list(server_id: int, user: dict = Depends(get_current_user)):
    require_module_access(user, server_id, 'engage')
    from database import list_wallet_collections
    rows = list_wallet_collections(server_id)
    return {'collections': [_wallet_collection_to_dict(r) for r in rows]}


@app.post('/api/servers/{server_id}/wallet-collections')
async def wallet_collections_create(
    server_id: int,
    body: _WalletCollectionCreate,
    user: dict = Depends(get_current_user),
):
    require_module_access(user, server_id, 'engage')
    rate_limit(f'wallet_coll_create:{server_id}', 30, 60.0)
    from database import (
        create_wallet_collection, get_wallet_collection, WalletCollectionNameExists,
    )

    payload = body.model_dump(exclude_unset=True)
    norm = _wallet_normalize_payload(payload)
    if not norm.get('name'):
        raise HTTPException(status_code=400, detail='name is required')
    norm.setdefault('blockchain', 'evm')

    try:
        cid = create_wallet_collection(server_id, **norm)
    except WalletCollectionNameExists:
        raise HTTPException(status_code=409, detail='A collection with that name already exists.')

    log_event(
        server_id, 'admin_action', 'wallet_collection_created',
        f'Wallet collection "{norm["name"]}" created by {user.get("username")}',
        actor_user_id=user.get('user_id') or user.get('id'),
        actor_username=user.get('username'),
        module='engage', severity='info',
        details={'collection_id': cid, 'blockchain': norm.get('blockchain')},
    )
    return _wallet_collection_to_dict(get_wallet_collection(cid, server_id))


@app.get('/api/servers/{server_id}/wallet-collections/{collection_id}')
async def wallet_collections_get(
    server_id: int, collection_id: int, user: dict = Depends(get_current_user),
):
    require_module_access(user, server_id, 'engage')
    from database import get_wallet_collection
    c = get_wallet_collection(collection_id, server_id)
    if c is None:
        raise HTTPException(status_code=404, detail='Wallet collection not found')
    return _wallet_collection_to_dict(c)


@app.patch('/api/servers/{server_id}/wallet-collections/{collection_id}')
async def wallet_collections_update(
    server_id: int, collection_id: int,
    body: _WalletCollectionUpdate,
    user: dict = Depends(get_current_user),
):
    require_module_access(user, server_id, 'engage')
    rate_limit(f'wallet_coll_update:{server_id}', 120, 60.0)
    from database import (
        get_wallet_collection, update_wallet_collection, WalletCollectionNameExists,
    )
    c = get_wallet_collection(collection_id, server_id)
    if c is None:
        raise HTTPException(status_code=404, detail='Wallet collection not found')

    payload = body.model_dump(exclude_unset=True)
    norm = _wallet_normalize_payload(payload)
    if norm:
        try:
            update_wallet_collection(collection_id, server_id, **norm)
        except WalletCollectionNameExists:
            raise HTTPException(status_code=409, detail='A collection with that name already exists.')

    fresh = get_wallet_collection(collection_id, server_id)

    # If already posted, refresh the live embed so dashboard edits flow through.
    live_edit = None
    if fresh and fresh.get('message_id') and fresh.get('channel_id') and bot.is_ready():
        guild = bot.get_guild(server_id)
        if guild is not None:
            try:
                from cogs._utils import resolve_channel
                from cogs.wallet_collection import (
                    build_wallet_collection_embed, build_wallet_collection_view,
                )
                channel = resolve_channel(guild, fresh.get('channel_id'))
                if channel is not None and fresh.get('status') != 'closed':
                    msg = await channel.fetch_message(int(fresh['message_id']))
                    await msg.edit(
                        embed=build_wallet_collection_embed(fresh),
                        view=build_wallet_collection_view(fresh),
                    )
                    live_edit = 'edited'
            except Exception as e:  # noqa: BLE001
                print(f'[wallet] live-edit failed cid={collection_id} guild={server_id}: '
                      f'{type(e).__name__}: {e}')
                live_edit = 'error'

    log_event(
        server_id, 'admin_action', 'wallet_collection_edited',
        f'Wallet collection #{collection_id} edited by {user.get("username")}',
        actor_user_id=user.get('user_id') or user.get('id'),
        actor_username=user.get('username'),
        module='engage', severity='info',
        details={'collection_id': collection_id, 'changed': sorted(norm.keys()),
                 'live_edit': live_edit},
    )
    out = _wallet_collection_to_dict(fresh)
    out['live_edit'] = live_edit
    return out


@app.delete('/api/servers/{server_id}/wallet-collections/{collection_id}')
async def wallet_collections_delete(
    server_id: int, collection_id: int, user: dict = Depends(get_current_user),
):
    require_module_access(user, server_id, 'engage')
    rate_limit(f'wallet_coll_delete:{server_id}', 30, 60.0)
    from database import get_wallet_collection, delete_wallet_collection
    c = get_wallet_collection(collection_id, server_id)
    if c is None:
        raise HTTPException(status_code=404, detail='Wallet collection not found')

    delete_wallet_collection(collection_id, server_id)
    log_event(
        server_id, 'admin_action', 'wallet_collection_deleted',
        f'Wallet collection "{c.get("name")}" deleted by {user.get("username")}',
        actor_user_id=user.get('user_id') or user.get('id'),
        actor_username=user.get('username'),
        module='engage', severity='info',
        details={'collection_id': collection_id},
    )
    return {'ok': True, 'deleted': collection_id}


@app.get('/api/servers/{server_id}/wallet-collections/{collection_id}/submissions')
async def wallet_collections_submissions(
    server_id: int, collection_id: int, user: dict = Depends(get_current_user),
):
    require_module_access(user, server_id, 'engage')
    from database import get_wallet_collection, list_wallet_submissions
    c = get_wallet_collection(collection_id, server_id)
    if c is None:
        raise HTTPException(status_code=404, detail='Wallet collection not found')

    rows = list_wallet_submissions(collection_id, server_id)

    # Best-effort Discord username resolution from the bot's guild cache. We do
    # not HTTP-fetch absent members here to keep the dashboard load fast; the
    # frontend falls back to the user id when the name is unknown.
    guild = bot.get_guild(server_id) if bot.is_ready() else None
    out = []
    for r in rows:
        uid = str(r.get('user_id'))
        username = None
        if guild is not None:
            try:
                m = guild.get_member(int(uid))
                if m is not None:
                    username = m.name
            except (TypeError, ValueError):
                pass
        out.append({
            'id':             int(r['id']),
            'user_id':        uid,
            'username':       username,
            'wallet_address': r.get('wallet_address') or '',
            'submitted_at':   r.get('submitted_at'),
            'updated_at':     r.get('updated_at'),
        })
    return {'submissions': out, 'total': len(out)}


@app.post('/api/servers/{server_id}/wallet-collections/{collection_id}/post')
async def wallet_collections_post(
    server_id: int, collection_id: int, user: dict = Depends(get_current_user),
):
    require_module_access(user, server_id, 'engage')
    rate_limit(f'wallet_coll_post:{server_id}', 30, 60.0)
    from database import get_wallet_collection
    c = get_wallet_collection(collection_id, server_id)
    if c is None:
        raise HTTPException(status_code=404, detail='Wallet collection not found')
    if not (c.get('channel_id') or '').strip():
        raise HTTPException(status_code=400, detail='Set a target channel before posting.')
    if not bot.is_ready():
        raise HTTPException(status_code=503, detail='Bot not ready yet')
    guild = bot.get_guild(server_id)
    if guild is None:
        raise HTTPException(status_code=404, detail='Bot is not in this server')

    from cogs.wallet_collection import post_collection_embed
    try:
        msg = await post_collection_embed(c, guild)
    except ValueError:
        raise HTTPException(status_code=400, detail=f'Channel not found: {c.get("channel_id")}')
    except discord.Forbidden:
        raise HTTPException(status_code=400, detail='Bot lacks permission to send in that channel')
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(e))

    log_event(
        server_id, 'admin_action', 'wallet_collection_posted',
        f'Wallet collection "{c.get("name")}" posted by {user.get("username")}',
        actor_user_id=user.get('user_id') or user.get('id'),
        actor_username=user.get('username'),
        module='engage', severity='info',
        details={'collection_id': collection_id, 'channel_id': c.get('channel_id'),
                 'message_id': str(msg.id)},
    )
    return _wallet_collection_to_dict(get_wallet_collection(collection_id, server_id))


@app.post('/api/servers/{server_id}/wallet-collections/{collection_id}/close')
async def wallet_collections_close(
    server_id: int, collection_id: int, user: dict = Depends(get_current_user),
):
    require_module_access(user, server_id, 'engage')
    rate_limit(f'wallet_coll_close:{server_id}', 30, 60.0)
    from database import get_wallet_collection, update_wallet_collection
    c = get_wallet_collection(collection_id, server_id)
    if c is None:
        raise HTTPException(status_code=404, detail='Wallet collection not found')

    update_wallet_collection(collection_id, server_id, status='closed')

    edited = False
    if (c.get('channel_id') or '').strip() and (c.get('message_id') or '').strip() and bot.is_ready():
        guild = bot.get_guild(server_id)
        if guild is not None:
            try:
                from cogs._utils import resolve_channel
                from cogs.wallet_collection import build_closed_view
                channel = resolve_channel(guild, c.get('channel_id'))
                if channel is not None:
                    msg = await channel.fetch_message(int(c['message_id']))
                    await msg.edit(view=build_closed_view(c))
                    edited = True
            except Exception as e:  # noqa: BLE001
                print(f'[wallet] close edit failed cid={collection_id}: {type(e).__name__}: {e}')

    log_event(
        server_id, 'admin_action', 'wallet_collection_closed',
        f'Wallet collection "{c.get("name")}" closed by {user.get("username")}',
        actor_user_id=user.get('user_id') or user.get('id'),
        actor_username=user.get('username'),
        module='engage', severity='info',
        details={'collection_id': collection_id, 'button_disabled': edited},
    )
    return _wallet_collection_to_dict(get_wallet_collection(collection_id, server_id))


# ── Settings module ──────────────────────────────────────────────────────────

@app.get('/api/servers/{server_id}/settings')
async def settings_get(server_id: int, user: dict = Depends(get_current_user)):
    require_module_access(user, server_id, 'settings')
    bot_instance = _get_bot_instance()
    guild = bot_instance.get_guild(server_id)
    brand  = get_guild_settings(server_id)
    access = list_module_access(server_id)
    by_role: dict = {}
    for a in access:
        if a['granted']:
            by_role.setdefault(a['role_id'], []).append(a['module'])
    roles_info = []
    if guild:
        for r in guild.roles:
            if r.is_default():
                continue
            roles_info.append({
                'id':       str(r.id),
                'name':     r.name,
                'color':    f'#{r.color.value:06x}' if r.color.value else None,
                'position': r.position,
                'modules':  by_role.get(str(r.id), []),
            })
        roles_info.sort(key=lambda x: x['position'], reverse=True)
    return {
        'guild_id': str(server_id),
        'brand':    brand,
        'modules':  list(MODULES),
        'roles':    roles_info,
    }


@app.put('/api/servers/{server_id}/settings/brand')
async def settings_brand_update(server_id: int, body: dict, user: dict = Depends(get_current_user)):
    require_module_access(user, server_id, 'settings')
    # Strip server-owned or read-only fields that must not be passed as kwargs
    for _strip in ('guild_id', 'updated_at'):
        body.pop(_strip, None)

    old = get_guild_settings(server_id) or {}
    update_guild_settings(server_id, **body)
    new = get_guild_settings(server_id) or {}

    log_event(
        server_id, 'settings', 'settings_updated',
        f'Brand settings updated by {user.get("username")}',
        actor_user_id=user.get('user_id') or user.get('id'),
        actor_username=user.get('username'),
        module='brand', severity='info',
        details={'changes': {k: body[k] for k in body if k in {
            'default_embed_color', 'default_thumbnail_url', 'default_footer_text',
            'default_footer_icon_url', 'bot_display_name', 'bot_avatar_url',
        }}},
    )

    # Best-effort apply to Discord — failures are logged but never break the save.
    AMERETAVERSE_GUILD_ID = 1199707792706117642
    is_amereta = (int(server_id) == AMERETAVERSE_GUILD_ID)

    try:
        bot_instance = _get_bot_instance()
    except HTTPException:
        bot_instance = None

    guild = bot_instance.get_guild(int(server_id)) if bot_instance else None

    # Per-guild bot nickname — supported on every guild.
    if guild is not None and 'bot_display_name' in body:
        new_name = (body.get('bot_display_name') or '').strip() or None
        try:
            me = guild.me
            if me is not None and me.nick != new_name:
                await me.edit(nick=new_name)
                print(f'[settings] applied nick={new_name!r} on guild {server_id}')
        except discord.Forbidden:
            print(f'[settings] forbidden: cannot change nick on guild {server_id}')
        except discord.HTTPException as e:
            print(f'[settings] HTTP error setting nick on {server_id}: {e}')
        except Exception as e:
            print(f'[settings] failed to apply nick on {server_id}: {type(e).__name__}: {e}')

    # Bot avatar — application-wide; only AmeretaVerse main can change it.
    if (
        is_amereta and bot_instance is not None
        and 'bot_avatar_url' in body
        and new.get('bot_avatar_url') != old.get('bot_avatar_url')
    ):
        avatar_url = (body.get('bot_avatar_url') or '').strip()
        if avatar_url:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(avatar_url) as resp:
                        if resp.status == 200:
                            avatar_bytes = await resp.read()
                            await bot_instance.user.edit(avatar=avatar_bytes)
                            print(f'[settings] applied avatar from {avatar_url}')
                        else:
                            print(f'[settings] avatar fetch HTTP {resp.status} for {avatar_url}')
            except discord.HTTPException as e:
                print(f'[settings] HTTP error setting avatar (possibly rate-limited): {e}')
            except Exception as e:
                print(f'[settings] failed to apply avatar: {type(e).__name__}: {e}')

    return new


@app.put('/api/servers/{server_id}/settings/levels')
async def settings_levels_update(server_id: int, body: dict, user: dict = Depends(get_current_user)):
    require_module_access(user, server_id, 'settings')
    allowed = {
        'level_enabled', 'xp_per_message', 'xp_cooldown_seconds',
        'level_up_message_enabled', 'level_up_channel_id',
    }
    payload: dict = {}
    for k, v in body.items():
        if k not in allowed:
            continue
        if k in ('level_enabled', 'level_up_message_enabled'):
            payload[k] = 1 if v else 0
        elif k in ('xp_per_message', 'xp_cooldown_seconds'):
            try:
                payload[k] = max(0, int(v))
            except (TypeError, ValueError):
                continue
        elif k == 'level_up_channel_id':
            s = str(v or '').lstrip('#').strip()
            payload[k] = s or None
    update_guild_settings(server_id, **payload)
    log_event(
        server_id, 'settings', 'settings_updated',
        f'Level settings updated by {user.get("username")}',
        actor_user_id=user.get('user_id') or user.get('id'),
        actor_username=user.get('username'),
        module='levels', severity='info',
        details={'changes': payload},
    )
    return get_guild_settings(server_id)


@app.put('/api/servers/{server_id}/settings/access')
async def settings_access_update(server_id: int, body: dict, user: dict = Depends(get_current_user)):
    require_module_access(user, server_id, 'settings')
    role_id = str(body.get('role_id', '')).strip()
    module  = str(body.get('module',  '')).strip()
    granted = bool(body.get('granted', False))
    if not role_id or module not in MODULES:
        raise HTTPException(status_code=400, detail=f'Invalid role_id or module')
    set_module_access(server_id, role_id, module, granted)
    log_event(
        server_id, 'settings', 'settings_updated',
        f'Access {"granted" if granted else "revoked"}: role {role_id} → {module}',
        actor_user_id=user.get('user_id') or user.get('id'),
        actor_username=user.get('username'),
        module='access_control', severity='info',
        details={'role_id': role_id, 'module': module, 'granted': granted},
    )
    return {'ok': True}


@app.get('/api/public/bot-info')
async def public_bot_info(request: Request):
    """Public endpoint: bot client ID + OAuth invite URL for Add to Server button."""
    rate_limit_public(request, 'bot-info', max_calls=60, window_secs=60.0)
    cid = CLIENT_ID or ''
    return {
        'client_id':  cid,
        'invite_url': f'https://discord.com/oauth2/authorize?client_id={cid}&permissions=8&scope=bot%20applications.commands',
    }


# ── Public overview endpoint ──────────────────────────────────────────────────

@app.get('/api/public/ameretaverse-overview')
async def public_ameretaverse_overview(request: Request):
    """Public read-only endpoint: 4 headline stats for AmeretaVerse main guild."""
    rate_limit_public(request, 'ameretaverse-overview', max_calls=60, window_secs=60.0)
    AMERETAVERSE_GID = 1199707792706117642
    try:
        bot_instance = _get_bot_instance()
        guild = bot_instance.get_guild(AMERETAVERSE_GID) if bot_instance and bot_instance.is_ready() else None
    except Exception:
        guild = None
    total_members = guild.member_count if guild else 0

    active = 0
    total_messages = 0
    growth = 0
    try:
        with get_connection() as conn:
            # Try analytics_snapshots table (may exist from analytics module)
            snap_latest = conn.execute(
                "SELECT member_count FROM analytics_snapshots "
                "WHERE guild_id=? ORDER BY snapshot_date DESC LIMIT 1",
                (str(AMERETAVERSE_GID),)
            ).fetchone()
            snap_old = conn.execute(
                "SELECT member_count FROM analytics_snapshots "
                "WHERE guild_id=? AND snapshot_date <= date('now','-30 days') "
                "ORDER BY snapshot_date DESC LIMIT 1",
                (str(AMERETAVERSE_GID),)
            ).fetchone()
            if snap_latest:
                m_latest = snap_latest['member_count'] or total_members
            else:
                m_latest = total_members
            m_old = snap_old['member_count'] if snap_old else m_latest
            growth = m_latest - m_old

            # message_counters table
            msgs = conn.execute(
                "SELECT SUM(message_count) as total FROM message_counters WHERE guild_id=?",
                (str(AMERETAVERSE_GID),)
            ).fetchone()
            total_messages = int(msgs['total'] or 0) if msgs else 0

            # Active: distinct days with messages in last 30 days
            active_row = conn.execute(
                "SELECT COUNT(*) as cnt FROM message_counters "
                "WHERE guild_id=? AND date >= date('now','-30 days')",
                (str(AMERETAVERSE_GID),)
            ).fetchone()
            active = int(active_row['cnt'] or 0) if active_row else 0
    except Exception as e:
        print(f'[public-overview] DB query error: {e}')

    return {
        'total_members':     total_members,
        'active_members':    active,
        'member_growth_30d': growth,
        'total_messages':    total_messages,
    }


# ── Health check ───────────────────────────────────────────────────────────────

@app.get('/api/servers/{server_id}/admin/points/user/{target_user_id}')
async def admin_get_user_points(server_id: int, target_user_id: int, user: dict = Depends(get_current_user)):
    require_guild_admin(user, server_id)
    from database import get_raid_user_points, get_engage_user_points, list_engage_pools
    community = get_raid_user_points(server_id, target_user_id) or {'total_points': 0}
    pools = list_engage_pools(server_id)
    engage_pools = [
        {
            'pool_id':      p['pool_id'],
            'name':         p['name'],
            'display_name': p.get('display_name') or p['name'],
            'points':       get_engage_user_points(p['pool_id'], target_user_id).get('points', 0),
        }
        for p in pools
    ]
    return {
        'user_id':          str(target_user_id),
        'community_points': community.get('total_points', 0),
        'engage_pools':     engage_pools,
    }


@app.post('/api/servers/{server_id}/admin/points/adjust')
async def admin_adjust_points(server_id: int, body: dict, user: dict = Depends(get_current_user)):
    require_guild_admin(user, server_id)
    from database import (
        upsert_raid_user_points, get_raid_user_points,
        upsert_engage_user_points, get_engage_user_points,
        reset_raid_user_points, reset_engage_user_points,
        reset_all_raid_points, reset_all_engage_points_in_pool,
        list_engage_pools, ensure_default_pool, get_engage_pool_by_id,
    )

    # Rate limit: bound how fast an admin can fire point operations (abuse /
    # runaway-script guard). Keyed per admin per guild.
    actor_id = int(user.get('user_id') or user.get('id') or 0)
    rate_limit(f'points-adjust:{server_id}:{actor_id}', max_calls=60, window_secs=60.0)

    action      = str(body.get('action', '')).strip()
    point_type  = str(body.get('type', '')).strip()
    target_uid  = body.get('user_id')
    pool_id_req = body.get('pool_id')

    if action not in ('add', 'remove', 'reset', 'reset-all'):
        raise HTTPException(status_code=400, detail='action must be add|remove|reset|reset-all')
    if point_type not in ('community', 'engage'):
        raise HTTPException(status_code=400, detail='type must be community|engage')
    # Bound the amount: positive, integer, and capped to prevent overflow /
    # crafted negative input flipping an add into a runaway value.
    amount = 0
    if action in ('add', 'remove'):
        amount = validate_point_amount(body.get('amount') or 0, 'amount')
        if amount < 1:
            raise HTTPException(status_code=400, detail='amount must be positive')
    if action == 'reset-all' and body.get('confirm') != 'CONFIRM':
        raise HTTPException(status_code=400, detail='reset-all requires confirm=CONFIRM')
    if action != 'reset-all':
        if not target_uid:
            raise HTTPException(status_code=400, detail='user_id required for this action')
        target_uid = validate_snowflake(target_uid, 'user_id')
    if pool_id_req is not None:
        try:
            pool_id_req = int(pool_id_req)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail='Invalid pool_id')

    # Security-relevant audit log for every point mutation.
    log_event(
        server_id, 'admin', 'points_adjusted',
        f'{action} {amount or ""} {point_type} points'
        + (f' for user {target_uid}' if action != 'reset-all' else ' (all users)'),
        actor_user_id=actor_id, actor_username=user.get('username'),
        module='points_admin', severity='warning' if action in ('reset', 'reset-all') else 'info',
        details={'action': action, 'type': point_type, 'amount': amount,
                 'target_user_id': str(target_uid) if target_uid else None,
                 'pool_id': pool_id_req},
    )

    if point_type == 'community':
        if action == 'add':
            upsert_raid_user_points(server_id, int(target_uid), +amount)
        elif action == 'remove':
            upsert_raid_user_points(server_id, int(target_uid), -amount)
        elif action == 'reset':
            reset_raid_user_points(server_id, int(target_uid))
        elif action == 'reset-all':
            reset_all_raid_points(server_id)
        new_pts = (get_raid_user_points(server_id, int(target_uid)) or {}).get('total_points', 0) if target_uid else None
        return {'ok': True, 'new_points': new_pts}

    AMERETAVERSE = 1199707792706117642
    pools = list_engage_pools(server_id)
    if not pools and server_id != AMERETAVERSE:
        pool_row = ensure_default_pool(server_id)
        pools = [pool_row]
    if pool_id_req:
        pool_obj = get_engage_pool_by_id(int(pool_id_req))
        if not pool_obj or pool_obj['guild_id'] != str(server_id):
            raise HTTPException(status_code=404, detail='Pool not found in this guild')
        pool = pool_obj
    elif len(pools) == 1:
        pool = pools[0]
    else:
        raise HTTPException(status_code=400, detail='Multiple pools — specify pool_id')

    pid = pool['pool_id']
    if action == 'add':
        upsert_engage_user_points(str(server_id), pid, str(int(target_uid)), delta_points=+amount)
    elif action == 'remove':
        upsert_engage_user_points(str(server_id), pid, str(int(target_uid)), delta_points=-amount)
    elif action == 'reset':
        reset_engage_user_points(pid, int(target_uid))
    elif action == 'reset-all':
        reset_all_engage_points_in_pool(pid)
    new_pts = get_engage_user_points(pid, str(int(target_uid))).get('points', 0) if target_uid else None
    return {'ok': True, 'pool_id': pid, 'new_points': new_pts}


@app.get('/health')
async def health():
    return {
        'status':      'ok',
        'bot_ready':   bot.is_ready(),
        'bot_user':    str(bot.user) if bot.user else None,
        'api_version': 'phase-4',
    }
