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
from fastapi import FastAPI, Depends, HTTPException, Request, status
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
)
from shared_bot import bot

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

def require_guild_admin(user: dict, guild_id: int):
    """Raise 403 if user is not admin of the requested guild."""
    guilds = user.get('guilds', [])
    for g in guilds:
        if int(g['id']) == guild_id:
            perms = int(g.get('permissions', 0))
            if perms & 0x8:  # ADMINISTRATOR flag
                return
    raise HTTPException(status_code=403, detail='You are not an administrator of this server')

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
    """
    Return servers where:
    - user has Administrator permission AND
    - the bot is present (bot.get_guild returns non-None)
    """
    result = []
    for g in user.get('guilds', []):
        guild_id = int(g['id'])
        guild = bot.get_guild(guild_id)
        if guild is None:
            continue
        icon_url = (
            f'{DISCORD_CDN}/icons/{guild_id}/{guild.icon.key}.png'
            if guild.icon else None
        )
        result.append({
            'id':      str(guild_id),
            'name':    guild.name,
            'icon':    icon_url,
            'members': guild.member_count,
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
                   SUM(CASE WHEN active=1 THEN 1 ELSE 0 END) as active_raids,
                   SUM(total_points) as total_points_offered
            FROM raids
        """).fetchone()

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

    embed = discord.Embed(title=title, description=desc, color=0x94730D)
    embed.set_footer(text='AmeretaVerse • Support Tickets')

    # Create a minimal persistent-compatible view (handler registered via bot.add_view at startup)
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(
        label=btn_lbl,
        style=discord.ButtonStyle.primary,
        custom_id='tickets:open',
        emoji='\U0001f3df️',
    ))

    try:
        msg = await channel.send(embed=embed, view=view)
    except discord.Forbidden:
        raise HTTPException(status_code=400, detail='Bot lacks permission to send in that channel')
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

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
#  ROLE SELECT ENDPOINTS
# ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ──

# ── Pydantic request models ───────────────────────────────────────────────────

class _PanelCreate(BaseModel):
    title: str = '🎯 Role Selection'
    description: str = ''
    style: str = 'buttons'


class _PanelUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    style: Optional[str] = None
    channel_id: Optional[str] = None


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

    channel = resolve_channel(guild, body.channel_id)
    if channel is None:
        raise HTTPException(status_code=400, detail=f'Channel not found: {body.channel_id}')

    embed = discord.Embed(
        title=panel['title'],
        description=panel['description'] or '',
        color=0x94730D,
    )
    embed.set_footer(text='AmeretaVerse • Role Selection')
    view = build_panel_view(panel_id)

    # Edit the existing Discord message if one was already sent
    if panel.get('message_id') and panel.get('channel_id'):
        existing_ch = guild.get_channel(int(panel['channel_id']))
        if existing_ch:
            try:
                msg = await existing_ch.fetch_message(int(panel['message_id']))
                await msg.edit(embed=embed, view=view)
                db_update_panel(panel_id, channel_id=channel.id, message_id=msg.id)
                return {'ok': True, 'message_id': str(msg.id), 'channel_id': str(channel.id)}
            except Exception:
                pass

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

    embed = discord.Embed(
        title=panel['title'],
        description=panel['description'] or '',
        color=0x94730D,
    )
    embed.set_footer(text='AmeretaVerse • Role Selection')
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


# ── Health check ───────────────────────────────────────────────────────────────

@app.get('/health')
async def health():
    return {
        'status':      'ok',
        'bot_ready':   bot.is_ready(),
        'bot_user':    str(bot.user) if bot.user else None,
        'api_version': 'phase-4',
    }
