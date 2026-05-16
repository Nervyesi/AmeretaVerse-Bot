"""
engage.py — Per-guild, per-pool engage-for-engage system.

Commands:
  /engage             Browse active submissions in this pool's channel and engage
  /submit <tweet_url> Submit your tweet to this pool (costs engage points)
  /engage-stats       Your balance and activity in this pool
  /engage-leaderboard Top earners in this pool

Architecture:
  - AmeretaVerse (1199707792706117642): two fixed pools — community + creator
  - All other guilds: one pool — default (auto-created on first use)
  - Pool resolved by channel_id — user must be in the correct channel
  - All DB queries scoped by pool_id for strict per-pool isolation
  - Live verification for LIVE_VERIFICATION_GUILD_IDS, adaptive daily for others
  - engage_user_points completely separate from raid community points
"""
import json
import asyncio
import traceback
from datetime import datetime, time, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from database import (
    get_connection,
    get_user_x_username,
    get_engage_pool_by_id,
    get_engage_pool_by_channel,
    list_engage_pools,
    update_engage_pool,
    ensure_default_pool,
    create_engage_submission,
    list_active_engage_submissions,
    expire_old_engage_submissions,
    reset_engage_pool_daily,
    get_user_daily_engage_submissions,
    get_engage_action,
    upsert_engage_action,
    add_engage_verification_log,
    upsert_engage_user_points,
    get_engage_user_points,
    get_engage_leaderboard,
    sample_pending_engage_actions,
)
from cogs._twitter import (
    check_comment,
    check_retweet,
    extract_tweet_id,
    normalize_username,
    lookup_twitter_user_by_login,
)
from cogs._branding import build_branded_embed
from cogs.raidbot import LIVE_VERIFICATION_GUILD_IDS, _normalize_point_ratio

AMERETAVERSE_GUILD_ID = 1199707792706117642

# ── In-memory slideshow sessions ───────────────────────────────────────────
# user_id → {pool_id, guild_id, queue: [sub_dict, ...], index: int,
#             selections: {submission_id: {like, comment, retweet}}, x_username: str}
_sessions: dict[int, dict] = {}


# ── Helpers ────────────────────────────────────────────────────────────────

async def _resolve_pool(interaction: discord.Interaction) -> dict | None:
    """Resolve pool from channel, enforce enabled + role gate. Returns pool or None (already replied)."""
    guild_id   = interaction.guild_id or 0
    channel_id = interaction.channel_id or 0

    pool = get_engage_pool_by_channel(str(guild_id), str(channel_id))

    # Auto-create default pool for tenant guilds so admin can configure it
    if pool is None and guild_id != AMERETAVERSE_GUILD_ID:
        try:
            ensure_default_pool(guild_id)
        except ValueError:
            pass
        pool = get_engage_pool_by_channel(str(guild_id), str(channel_id))

    if pool is None:
        await interaction.response.send_message(
            '❌ This channel is not linked to an engage pool. Ask an admin to configure the engage channel.',
            ephemeral=True,
        )
        return None

    if not pool.get('enabled'):
        await interaction.response.send_message(
            '❌ Engage is currently disabled for this pool.',
            ephemeral=True,
        )
        return None

    # Role gate — empty list = open to all
    try:
        allowed_roles = json.loads(pool.get('allowed_role_ids') or '[]')
    except Exception:
        allowed_roles = []

    if allowed_roles:
        member_role_ids = {str(r.id) for r in (interaction.user.roles if interaction.user else [])}
        if not member_role_ids.intersection(set(str(rid) for rid in allowed_roles)):
            await interaction.response.send_message(
                '❌ You don\'t have the required role to use engage in this channel.',
                ephemeral=True,
            )
            return None

    return pool


def _pool_allowed_tasks(pool: dict) -> dict:
    return {
        'like':    bool(pool.get('allow_like',    1)),
        'comment': bool(pool.get('allow_comment', 1)),
        'retweet': bool(pool.get('allow_retweet', 1)),
    }


def _pool_ratios(pool: dict) -> dict:
    allowed = _pool_allowed_tasks(pool)
    fake_settings = {
        'point_ratio_like':    pool.get('point_ratio_like',    12),
        'point_ratio_comment': pool.get('point_ratio_comment', 40),
        'point_ratio_retweet': pool.get('point_ratio_retweet', 48),
    }
    return _normalize_point_ratio(fake_settings, allowed)


async def _verify_tasks(tweet_id: str, x_username: str, claims: dict) -> dict:
    """Run Twitter checks for claimed tasks. Returns {task: {verified, reason}}."""
    results = {}

    if claims.get('comment'):
        results['comment'] = await check_comment(tweet_id, x_username)
    if claims.get('retweet'):
        results['retweet'] = await check_retweet(tweet_id, x_username)
    if claims.get('like'):
        companions = [results[t] for t in ('comment', 'retweet') if t in results]
        if not companions:
            results['like'] = {'verified': True, 'reason': 'like_only_trusted'}
        elif any(r.get('verified') is False for r in companions):
            results['like'] = {'verified': False, 'reason': 'like_companion_failed'}
        elif any(r.get('verified') is None for r in companions):
            results['like'] = {'verified': None, 'reason': 'like_companion_inconclusive'}
        else:
            results['like'] = {'verified': True, 'reason': 'like_companions_passed'}

    return results


def _compute_earned(results: dict, claims: dict, ratios: dict, total: int) -> int:
    earned = 0
    for task in ('like', 'comment', 'retweet'):
        if claims.get(task) and results.get(task, {}).get('verified') is True:
            earned += total * ratios[task] // 100
    return earned


def _session_embed(session: dict, pool: dict) -> discord.Embed:
    idx   = session['index']
    queue = session['queue']
    sub   = queue[idx]
    sels  = session['selections'].get(sub['submission_id'], {})

    color_str = (pool.get('embed_color') or '').strip()
    try:
        color = int(color_str.lstrip('#'), 16) if color_str else 0x94730D
    except ValueError:
        color = 0x94730D

    selected = [t.capitalize() for t in ('like', 'comment', 'retweet') if sels.get(t)]
    sel_str  = ', '.join(selected) if selected else 'None'

    embed = discord.Embed(
        title  = f'Engage — {idx + 1} / {len(queue)}',
        description = (
            f'**[Open Tweet]({sub["tweet_url"]})**\n'
            f'By: @{sub.get("submitter_x_username") or "unknown"}\n\n'
            f'**Selected tasks:** {sel_str}\n\n'
            'Toggle tasks below. Click ✅ Done & Next (or ✅ Finish on the last tweet).'
        ),
        color = color,
    )
    footer = pool.get('embed_footer_text') or 'AmeretaVerse • Engage'
    embed.set_footer(text=footer)
    return embed


class _EngageView(discord.ui.View):
    """Ephemeral slideshow view for one tweet."""

    def __init__(self, user_id: int, session: dict, pool: dict):
        super().__init__(timeout=600)
        self.user_id = user_id

        idx     = session['index']
        queue   = session['queue']
        sub     = queue[idx]
        sub_id  = sub['submission_id']
        sels    = session['selections'].get(sub_id, {})
        is_last = idx == len(queue) - 1

        # Task toggle buttons (row 0)
        for task, emoji, label in (
            ('like',    '❤️', 'Like'),
            ('comment', '💬', 'Comment'),
            ('retweet', '🔁', 'Retweet'),
        ):
            if not pool.get(f'allow_{task}', 1):
                continue
            is_on = bool(sels.get(task))
            btn = discord.ui.Button(
                style     = discord.ButtonStyle.success if is_on else discord.ButtonStyle.secondary,
                label     = label,
                emoji     = emoji,
                row       = 0,
                custom_id = f'eng:toggle:{task}:{user_id}:{sub_id}',
            )
            btn.callback = self._make_toggle(task)
            self.add_item(btn)

        # Previous (row 1)
        prev = discord.ui.Button(
            style     = discord.ButtonStyle.secondary,
            label     = '◀️ Prev',
            disabled  = (idx == 0),
            row       = 1,
            custom_id = f'eng:prev:{user_id}',
        )
        prev.callback = self._prev_cb
        self.add_item(prev)

        # Skip (row 1)
        skip = discord.ui.Button(
            style     = discord.ButtonStyle.secondary,
            label     = 'Skip ▶️',
            row       = 1,
            custom_id = f'eng:skip:{user_id}',
        )
        skip.callback = self._skip_cb
        self.add_item(skip)

        # Done/Finish (row 1)
        done = discord.ui.Button(
            style     = discord.ButtonStyle.primary,
            label     = '✅ Finish' if is_last else '✅ Done & Next',
            row       = 1,
            custom_id = f'eng:done:{user_id}',
        )
        done.callback = self._done_cb
        self.add_item(done)

        # Exit (row 1)
        ex = discord.ui.Button(
            style     = discord.ButtonStyle.danger,
            label     = 'Exit',
            row       = 1,
            custom_id = f'eng:exit:{user_id}',
        )
        ex.callback = self._exit_cb
        self.add_item(ex)

    def _make_toggle(self, task: str):
        async def cb(inter: discord.Interaction):
            if inter.user.id != self.user_id:
                await inter.response.send_message('❌ Not your session.', ephemeral=True)
                return
            s = _sessions.get(self.user_id)
            if not s:
                await inter.response.edit_message(content='Session expired — run /engage again.', embed=None, view=None)
                return
            sub_id = s['queue'][s['index']]['submission_id']
            sels   = s['selections'].setdefault(sub_id, {'like': False, 'comment': False, 'retweet': False})
            sels[task] = not sels[task]
            pool  = get_engage_pool_by_id(s['pool_id'])
            embed = _session_embed(s, pool)
            view  = _EngageView(self.user_id, s, pool)
            await inter.response.edit_message(embed=embed, view=view)
        return cb

    async def _prev_cb(self, inter: discord.Interaction):
        if inter.user.id != self.user_id:
            await inter.response.send_message('❌ Not your session.', ephemeral=True)
            return
        s = _sessions.get(self.user_id)
        if not s or s['index'] <= 0:
            await inter.response.defer()
            return
        s['index'] -= 1
        pool  = get_engage_pool_by_id(s['pool_id'])
        embed = _session_embed(s, pool)
        view  = _EngageView(self.user_id, s, pool)
        await inter.response.edit_message(embed=embed, view=view)

    async def _skip_cb(self, inter: discord.Interaction):
        if inter.user.id != self.user_id:
            await inter.response.send_message('❌ Not your session.', ephemeral=True)
            return
        s = _sessions.get(self.user_id)
        if not s:
            await inter.response.edit_message(content='Session expired — run /engage again.', embed=None, view=None)
            return
        s['index'] += 1
        if s['index'] >= len(s['queue']):
            await _finalize(inter, self.user_id)
            return
        pool  = get_engage_pool_by_id(s['pool_id'])
        embed = _session_embed(s, pool)
        view  = _EngageView(self.user_id, s, pool)
        await inter.response.edit_message(embed=embed, view=view)

    async def _done_cb(self, inter: discord.Interaction):
        if inter.user.id != self.user_id:
            await inter.response.send_message('❌ Not your session.', ephemeral=True)
            return
        s = _sessions.get(self.user_id)
        if not s:
            await inter.response.edit_message(content='Session expired — run /engage again.', embed=None, view=None)
            return
        s['index'] += 1
        if s['index'] >= len(s['queue']):
            await _finalize(inter, self.user_id)
            return
        pool  = get_engage_pool_by_id(s['pool_id'])
        embed = _session_embed(s, pool)
        view  = _EngageView(self.user_id, s, pool)
        await inter.response.edit_message(embed=embed, view=view)

    async def _exit_cb(self, inter: discord.Interaction):
        if inter.user.id != self.user_id:
            await inter.response.send_message('❌ Not your session.', ephemeral=True)
            return
        await _finalize(inter, self.user_id)


async def _finalize(interaction: discord.Interaction, user_id: int) -> None:
    """Process all session selections, verify, award points, show summary."""
    session = _sessions.pop(user_id, None)
    if not session:
        if not interaction.response.is_done():
            await interaction.response.edit_message(content='Session not found — run /engage again.', embed=None, view=None)
        return

    guild_id   = session['guild_id']
    pool_id    = session['pool_id']
    pool       = get_engage_pool_by_id(pool_id)
    x_username = session.get('x_username', '')

    to_process = [
        (sub, session['selections'][sub['submission_id']])
        for sub in session['queue']
        if sub['submission_id'] in session['selections']
        and any(session['selections'][sub['submission_id']].values())
    ]

    if not to_process:
        embed = discord.Embed(
            title       = 'Engage session ended',
            description = 'You didn\'t select any tasks. No points awarded.',
            color       = 0x94730D,
        )
        if interaction.response.is_done():
            await interaction.edit_original_response(content=None, embed=embed, view=None)
        else:
            await interaction.response.edit_message(content=None, embed=embed, view=None)
        return

    ratios = _pool_ratios(pool)
    total  = int(pool.get('total_points_per_engage', 10))
    use_live = guild_id in LIVE_VERIFICATION_GUILD_IDS

    if use_live:
        # Signal we are verifying
        est_lo = len(to_process) * 2
        est_hi = len(to_process) * 4
        if interaction.response.is_done():
            await interaction.edit_original_response(
                content=f'⏳ Verifying {len(to_process)} engagement(s) with X… ({est_lo}–{est_hi}s)',
                embed=None, view=None,
            )
        else:
            await interaction.response.edit_message(
                content=f'⏳ Verifying {len(to_process)} engagement(s) with X… ({est_lo}–{est_hi}s)',
                embed=None, view=None,
            )

    total_earned = 0
    result_lines = []

    for sub, claims in to_process:
        sub_id   = sub['submission_id']
        tweet_id = sub.get('tweet_id', '')
        dn       = sub.get('display_number', sub_id)

        if use_live:
            results = await _verify_tasks(tweet_id, x_username, claims)
            earned  = _compute_earned(results, claims, ratios, total)
            source  = 'live'

            for task in ('like', 'comment', 'retweet'):
                if not claims.get(task):
                    continue
                v    = results.get(task, {}).get('verified')
                db_v = 1 if v is True else (0 if v is False else -1)
                add_engage_verification_log(str(guild_id), pool_id, sub_id, user_id,
                                            task, 1, db_v, source,
                                            error_text=results.get(task, {}).get('reason'))

            like_v    = results.get('like',    {}).get('verified')
            comment_v = results.get('comment', {}).get('verified')
            retweet_v = results.get('retweet', {}).get('verified')

            def _flag(v):
                return 1 if v is True else (0 if v is False else -1)

            upsert_engage_action(
                str(guild_id), pool_id, sub_id, str(user_id), x_username,
                int(bool(claims.get('like'))),
                int(bool(claims.get('comment'))),
                int(bool(claims.get('retweet'))),
                _flag(like_v)    if claims.get('like')    else None,
                _flag(comment_v) if claims.get('comment') else None,
                _flag(retweet_v) if claims.get('retweet') else None,
                earned, source,
            )
        else:
            earned = 0
            for task in ('like', 'comment', 'retweet'):
                if claims.get(task):
                    earned += total * ratios[task] // 100
            upsert_engage_action(
                str(guild_id), pool_id, sub_id, str(user_id), x_username,
                int(bool(claims.get('like'))),
                int(bool(claims.get('comment'))),
                int(bool(claims.get('retweet'))),
                None, None, None,
                earned, None,  # verification_source=None → pending daily check
            )

        total_earned += earned
        result_lines.append(f'Tweet #{dn:04d} — {earned} pts earned')

    if total_earned > 0:
        upsert_engage_user_points(str(guild_id), pool_id, str(user_id),
                                  delta_points=total_earned, delta_engaged=len(to_process))

    pts = get_engage_user_points(pool_id, str(user_id))
    pool_name = pool.get('display_name') or pool.get('name', 'Engage')
    embed = discord.Embed(
        title       = f'✅ Engage complete — {total_earned} pts earned',
        description = f'Engaged with **{len(to_process)}** tweet(s).\n\n' +
                      '\n'.join(result_lines) +
                      f'\n\n**Balance:** {pts["points"]} engage pts',
        color       = 0x94730D,
    )
    embed.set_footer(text=f'{pool_name} Pool')

    if use_live:
        await interaction.edit_original_response(content=None, embed=embed, view=None)
    else:
        if interaction.response.is_done():
            await interaction.edit_original_response(content=None, embed=embed, view=None)
        else:
            await interaction.response.edit_message(content=None, embed=embed, view=None)


# ── Cog ────────────────────────────────────────────────────────────────────

class EngageCog(commands.Cog, name='Engage'):
    def __init__(self, bot):
        self.bot = bot
        self._expire_task.start()
        self._daily_reset.start()

    def cog_unload(self):
        self._expire_task.cancel()
        self._daily_reset.cancel()

    # ── /engage ───────────────────────────────────────────────────────────

    @app_commands.command(name='engage', description='Engage with submitted tweets and earn engage points.')
    async def engage_cmd(self, interaction: discord.Interaction):
        pool = await _resolve_pool(interaction)
        if not pool:
            return

        user_id    = interaction.user.id
        guild_id   = interaction.guild_id or 0
        x_username = get_user_x_username(user_id)
        if not x_username:
            await interaction.response.send_message(
                '⚠️ Link your X account first with `/setx <username>`.',
                ephemeral=True,
            )
            return

        queue = list_active_engage_submissions(pool['pool_id'], limit=10, exclude_user_id=str(user_id))
        if not queue:
            await interaction.response.send_message(
                '📭 No active tweets in this pool right now. Come back later or submit yours!',
                ephemeral=True,
            )
            return

        _sessions[user_id] = {
            'pool_id':    pool['pool_id'],
            'guild_id':   guild_id,
            'queue':      queue,
            'index':      0,
            'selections': {},
            'x_username': x_username,
        }

        embed = _session_embed(_sessions[user_id], pool)
        view  = _EngageView(user_id, _sessions[user_id], pool)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    # ── /submit ────────────────────────────────────────────────────────────

    @app_commands.command(name='submit', description='Submit your tweet to the engage pool.')
    @app_commands.describe(tweet_url='Full tweet URL: https://x.com/.../status/...')
    async def submit_cmd(self, interaction: discord.Interaction, tweet_url: str):
        pool = await _resolve_pool(interaction)
        if not pool:
            return

        user_id    = interaction.user.id
        guild_id   = interaction.guild_id or 0
        x_username = get_user_x_username(user_id)
        if not x_username:
            await interaction.response.send_message(
                '⚠️ Link your X account first with `/setx <username>`.',
                ephemeral=True,
            )
            return

        tweet_id = extract_tweet_id(tweet_url)
        if not tweet_id:
            await interaction.response.send_message(
                '❌ Invalid tweet URL. Please use the full https://x.com/.../status/... link.',
                ephemeral=True,
            )
            return

        # Daily limit check
        daily = get_user_daily_engage_submissions(pool['pool_id'], user_id)
        if daily >= int(pool.get('daily_submission_limit', 3)):
            await interaction.response.send_message(
                f'⚠️ Daily submission limit reached ({pool["daily_submission_limit"]}). Try again tomorrow.',
                ephemeral=True,
            )
            return

        # Points balance check
        pts_row = get_engage_user_points(pool['pool_id'], str(user_id))
        cost    = int(pool.get('submit_cost', 50))
        if pts_row['points'] < cost:
            await interaction.response.send_message(
                f'⚠️ Not enough engage points. Need **{cost}**, you have **{pts_row["points"]}**. Engage with more tweets to earn.',
                ephemeral=True,
            )
            return

        # Defer for API call
        await interaction.response.defer(ephemeral=True)

        info      = await lookup_twitter_user_by_login(x_username)
        followers = int((info or {}).get('followers_count') or (info or {}).get('followers') or 0)
        min_fol   = int(pool.get('min_followers', 100))
        if followers < min_fol:
            await interaction.followup.send(
                f'⚠️ You need at least **{min_fol}** followers to submit to this pool. You have **{followers}**.',
                ephemeral=True,
            )
            return

        # Deduct cost BEFORE creating submission (atomic)
        upsert_engage_user_points(str(guild_id), pool['pool_id'], str(user_id),
                                  delta_points=-cost, delta_submitted=1)
        try:
            sub = create_engage_submission(
                str(guild_id), pool['pool_id'], str(user_id),
                tweet_url.strip(), tweet_id, x_username,
                cost, pool.get('ttl_hours'),
            )
        except Exception as e:
            # Refund on failure
            upsert_engage_user_points(str(guild_id), pool['pool_id'], str(user_id),
                                      delta_points=cost, delta_submitted=-1)
            print(f'[engage] submit error: {type(e).__name__}: {e}')
            traceback.print_exc()
            await interaction.followup.send('❌ Failed to submit. Try again.', ephemeral=True)
            return

        dn = sub.get('display_number', sub['submission_id'])
        pool_name = pool.get('display_name') or pool.get('name', 'Engage')
        ttl_val = pool.get('ttl_hours')
        ttl_note = f'Expires in {ttl_val}h.' if ttl_val else 'No expiry (stays until removed).'
        new_balance = pts_row['points'] - cost
        await interaction.followup.send(
            f'✅ Tweet **#{dn:04d}** submitted to **{pool_name}**.\n'
            f'Cost: **{cost}** pts | New balance: **{new_balance}** pts | {ttl_note}',
            ephemeral=True,
        )

    # ── /engage-stats ──────────────────────────────────────────────────────

    @app_commands.command(name='engage-stats', description='Your engage points and activity in this pool.')
    async def engage_stats_cmd(self, interaction: discord.Interaction):
        pool = await _resolve_pool(interaction)
        if not pool:
            return

        stats = get_engage_user_points(pool['pool_id'], str(interaction.user.id))
        pool_name = pool.get('display_name') or pool.get('name', 'Engage')
        embed = discord.Embed(
            title       = f'Your Engage Stats — {pool_name}',
            color       = 0x94730D,
        )
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.add_field(name='Engage Points',    value=f'`{stats["points"]}`',          inline=True)
        embed.add_field(name='Tweets Engaged',   value=f'`{stats["total_engaged"]}`',   inline=True)
        embed.add_field(name='Tweets Submitted', value=f'`{stats["total_submitted"]}`', inline=True)
        embed.set_footer(text=f'{pool_name} Pool')
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /engage-leaderboard ────────────────────────────────────────────────

    @app_commands.command(name='engage-leaderboard', description='Top engage point earners in this pool.')
    async def engage_leaderboard_cmd(self, interaction: discord.Interaction):
        pool = await _resolve_pool(interaction)
        if not pool:
            return

        rows = get_engage_leaderboard(pool['pool_id'], limit=10)
        if not rows:
            await interaction.response.send_message('No leaderboard data yet.', ephemeral=True)
            return

        medals = ['🥇', '🥈', '🥉']
        lines  = []
        for i, r in enumerate(rows):
            rank = medals[i] if i < 3 else f'`{i + 1}.`'
            name = r.get('username') or f'<@{r["user_id"]}>'
            lines.append(f'{rank} **{name}** — `{r["points"]} pts` ({r["total_engaged"]} engages)')

        pool_name = pool.get('display_name') or pool.get('name', 'Engage')
        embed = discord.Embed(
            title       = f'⚡ {pool_name} Leaderboard',
            description = '\n'.join(lines),
            color       = 0x94730D,
        )
        embed.set_footer(text=f'{pool_name} Pool')
        await interaction.response.send_message(embed=embed)

    # ── Background tasks ───────────────────────────────────────────────────

    @tasks.loop(hours=1)
    async def _expire_task(self):
        count = expire_old_engage_submissions()
        if count:
            print(f'[engage] expired {count} old submissions')

    @tasks.loop(time=time(0, 0, tzinfo=timezone.utc))
    async def _daily_reset(self):
        """Reset pools with auto_reset_daily=1 at midnight UTC."""
        with get_connection() as conn:
            pools = conn.execute(
                "SELECT pool_id, guild_id, name FROM engage_pools WHERE auto_reset_daily=1"
            ).fetchall()
        for p in pools:
            count = reset_engage_pool_daily(p['pool_id'])
            print(f'[engage] daily reset pool {p["name"]} (guild {p["guild_id"]}) — {count} submissions expired')

    @_expire_task.before_loop
    async def _before_expire(self):
        await self.bot.wait_until_ready()

    @_daily_reset.before_loop
    async def _before_reset(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(EngageCog(bot))
