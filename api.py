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
import json
import time
from datetime import datetime, timezone, timedelta, date
from typing import Optional

import aiohttp
import jwt
from fastapi import FastAPI, Depends, HTTPException, Request, status, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, JSONResponse
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

    jwt_payload = {
        'user_id':  user_id,
        'username': user_data.get('global_name') or user_data['username'],
        'avatar':   avatar_url(user_data['id'], user_data.get('avatar'), user_data.get('discriminator', '0')),
        'guilds':   admin_guilds,
    }
    token = create_jwt(jwt_payload)
    return RedirectResponse(f'{FRONTEND_URL}/dashboard?token={token}')


@app.get('/auth/me')
async def auth_me(user: dict = Depends(get_current_user)):
    """Return current user info decoded from JWT."""
    return {
        'user_id':  user['user_id'],
        'username': user['username'],
        'avatar':   user['avatar'],
        'guilds':   user.get('guilds', []),
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
    require_guild_admin(user, server_id)

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

        e4e_stats = conn.execute("""
            SELECT COUNT(*) as total_links,
                   SUM(CASE WHEN active=1 THEN 1 ELSE 0 END) as active_links
            FROM engage_links
        """).fetchone()

        e4e_part = conn.execute("""
            SELECT COUNT(*) as total, SUM(points_earned) as points
            FROM engage_participation
        """).fetchone()

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
        by_ym = {r['ym']: r for r in snaps_12mo}
        mg, jl, msgs = [], [], []
        for i in range(11, -1, -1):
            y, m = today.year, today.month - i
            while m <= 0:
                m += 12; y -= 1
            d = date(y, m, 1)
            snap = by_ym.get(d.strftime('%Y-%m'))
            mg.append({'label': month_label(d),
                       'value': int(snap['avg_members'] or 0) if snap else 0})
            jl.append({'label': month_label(d),
                       'joins':  int(snap['total_joins']  or 0) if snap else 0,
                       'leaves': int(snap['total_leaves'] or 0) if snap else 0})
            msgs.append({'label': month_label(d),
                         'value': int(snap['total_msgs'] or 0) if snap else 0})
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

    return {
        'member_growth':             member_growth,
        'joins_leaves':              joins_leaves,
        'messages':                  messages,
        'leaves_tracking_started':   leaves_tracking_started or None,
        'first_message_tracked_date': first_msg_row['first_date'] if first_msg_row else None,
        'raids': {
            'total':          raid_stats['total_raids'],
            'active':         raid_stats['active_raids'],
            'points_offered': raid_stats['total_points_offered'] or 0,
        },
        'engage': {
            'total_links':       e4e_stats['total_links'],
            'active_links':      e4e_stats['active_links'],
            'total_engagements': e4e_part['total'],
            'total_points':      e4e_part['points'] or 0,
        },
        'leaderboard':         [dict(r) for r in leaderboard],
        'data_started':        first_snap['first_date'] if first_snap else None,
        'first_snapshot_date': first_snap['first_date'] if first_snap else None,
        'has_any_data':        has_data,
        'voice':               None,
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
    return db_get_all_config(server_id)


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
    require_guild_admin(user, server_id)
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
    require_guild_admin(user, server_id)

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
    require_guild_admin(user, server_id)
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT id, action_type, user_id, detail, created_at
            FROM protection_actions
            WHERE guild_id=?
            ORDER BY created_at DESC
            LIMIT ?
        """, (server_id, min(limit, 200))).fetchall()
    return [dict(r) for r in rows]

@app.get('/api/servers/{server_id}/flagged')
async def flagged_users(server_id: int, user: dict = Depends(get_current_user)):
    """Flagged raid participants for this guild."""
    require_guild_admin(user, server_id)
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
    require_guild_admin(user, server_id)
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
    require_guild_admin(user, server_id)

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
    require_guild_admin(user, server_id)
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
    require_guild_admin(user, server_id)
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
    require_guild_admin(user, server_id)

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
    require_guild_admin(user, server_id)
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
    require_guild_admin(user, server_id)
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
    require_guild_admin(user, server_id)
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
    require_guild_admin(user, server_id)
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
    require_guild_admin(user, server_id)
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
    require_guild_admin(user, server_id)
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
    require_guild_admin(user, server_id)
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
    require_guild_admin(user, server_id)
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
    require_guild_admin(user, server_id)
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
    require_guild_admin(user, server_id)
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
    require_guild_admin(user, server_id)
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
    require_guild_admin(user, server_id)
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
    require_guild_admin(user, server_id)
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
    require_guild_admin(user, server_id)
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
    require_guild_admin(user, server_id)
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
    require_guild_admin(user, server_id)
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
    require_guild_admin(user, server_id)
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
    require_guild_admin(user, server_id)
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
    require_guild_admin(user, server_id)
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
    require_guild_admin(user, server_id)
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
    require_guild_admin(user, server_id)
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
    require_guild_admin(user, server_id)
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
    require_guild_admin(user, server_id)
    raid = db_get_guild_raid(raid_id, server_id)
    if not raid:
        raise HTTPException(status_code=404, detail='Raid not found')
    db_end_raid(raid_id, server_id, ended_reason='admin')
    return {'ok': True, 'raid_id': raid_id}


@app.get('/api/servers/{server_id}/raid/leaderboard')
async def raid_leaderboard_api(
    server_id: int, limit: int = 10, user: dict = Depends(get_current_user),
):
    require_guild_admin(user, server_id)
    return {'leaderboard': db_get_raid_leaderboard(server_id, min(limit, 100))}


@app.get('/api/servers/{server_id}/raid/verification-log')
async def raid_verification_log(
    server_id: int,
    status: str = 'flagged',
    limit: int = 50,
    user: dict = Depends(get_current_user),
):
    """Return aggregated verification flags — one row per (user, raid) with task details."""
    require_guild_admin(user, server_id)
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
    require_guild_admin(user, server_id)
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
    require_guild_admin(user, server_id)
    try:
        from cogs._twitter import get_scraping_health
        return get_scraping_health()
    except Exception as e:
        return {'healthy': None, 'consecutive_failures': 0, 'error': str(e)}


@app.post('/api/servers/{server_id}/raid/send-guide')
async def raid_send_guide(server_id: int, user: dict = Depends(get_current_user)):
    require_guild_admin(user, server_id)
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
    uid = int(user.get('user_id', 0))
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


# ── Engage admin endpoints ────────────────────────────────────────────────────

_AMERETAVERSE_GID = 1199707792706117642


@app.get('/api/servers/{server_id}/engage/pools')
async def engage_pools_get(server_id: int, user: dict = Depends(get_current_user)):
    require_guild_admin(user, server_id)
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
    require_guild_admin(user, server_id)
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


# ── Health check ───────────────────────────────────────────────────────────────

@app.get('/health')
async def health():
    return {
        'status':      'ok',
        'bot_ready':   bot.is_ready(),
        'bot_user':    str(bot.user) if bot.user else None,
        'api_version': 'phase-4',
    }
