"""
engage.py — Per-guild, per-pool engage-for-engage system.

Commands:
  /engage    — Browse active submissions and engage with tweets to earn engage points
  /submit    — Submit your tweet to the pool (costs engage points)
  /engage-stats — Your engage point balance and activity
  /engage-leaderboard — Top earners in this pool

Architecture:
  - Pool-scoped: all queries use pool_id, not just guild_id
  - Channel-based pool resolution: each pool has its own Discord channel
  - Live verification for LIVE_VERIFICATION_GUILD_IDS, adaptive daily check for others
  - engage_points are completely separate from raid community points
"""

import json
import asyncio
import traceback
import requests
from datetime import datetime, timezone, time as dtime

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
    create_engage_submission,
    list_active_submissions,
    expire_old_submissions,
    get_user_daily_submission_count,
    get_engage_action,
    upsert_engage_action,
    add_engage_verification_log,
    upsert_engage_user_points,
    get_engage_user_points,
    get_engage_leaderboard,
    sample_pending_engage_actions,
    count_pending_engage_actions,
)
from cogs._twitter import (
    check_comment,
    check_retweet,
    extract_tweet_id,
    normalize_username,
    lookup_twitter_user_by_login,
)
from cogs._branding import build_branded_embed
from cogs.raidbot import LIVE_VERIFICATION_GUILD_IDS

# ── Session state ───────────────────────────────────────────────────────────
# Stores per-user engage session while browsing the slideshow
# key: user_id → {pool_id, guild_id, queue: list[dict], index: int, selections: {sub_id: {like,comment,retweet}}}
_sessions: dict[int, dict] = {}


def _get_session(user_id: int) -> dict | None:
    return _sessions.get(user_id)


def _set_session(user_id: int, data: dict) -> None:
    _sessions[user_id] = data


def _clear_session(user_id: int) -> None:
    _sessions.pop(user_id, None)


# ── View builders ──────────────────────────────────────────────────────────

def _build_engage_panel_view(user_id: int, session: dict) -> discord.ui.View:
    idx      = session['index']
    queue    = session['queue']
    sub      = queue[idx]
    sub_id   = sub['submission_id']
    pool     = session['pool']
    sels     = session['selections'].get(sub_id, {'like': False, 'comment': False, 'retweet': False})
    total    = len(queue)
    view     = discord.ui.View(timeout=600)

    def _toggle_cb(task: str):
        async def cb(inter: discord.Interaction):
            if inter.user.id != user_id:
                await inter.response.send_message('❌ Not your session.', ephemeral=True)
                return
            s = _sessions.get(user_id)
            if not s:
                await inter.response.send_message('❌ Session expired.', ephemeral=True)
                return
            curr_sub_id = s['queue'][s['index']]['submission_id']
            sels_now = s['selections'].setdefault(curr_sub_id, {'like': False, 'comment': False, 'retweet': False})
            sels_now[task] = not sels_now[task]
            new_view = _build_engage_panel_view(user_id, s)
            embed    = _build_engage_embed(s)
            await inter.response.edit_message(embed=embed, view=new_view)
        return cb

    # Task toggle buttons (row 0)
    task_cfg = [
        ('like',    '❤️', 'Like',    pool.get('allow_like', 1)),
        ('comment', '💬', 'Comment', pool.get('allow_comment', 1)),
        ('retweet', '🔁', 'Retweet', pool.get('allow_retweet', 1)),
    ]
    for task, emoji, label, allowed in task_cfg:
        if not allowed:
            continue
        is_on = sels.get(task, False)
        btn = discord.ui.Button(
            style=discord.ButtonStyle.success if is_on else discord.ButtonStyle.secondary,
            label=label, emoji=emoji, row=0,
            custom_id=f'engage:toggle:{task}:{user_id}:{sub_id}',
        )
        btn.callback = _toggle_cb(task)
        view.add_item(btn)

    # Navigation + submit (row 1)
    async def prev_cb(inter: discord.Interaction):
        if inter.user.id != user_id:
            await inter.response.send_message('❌ Not your session.', ephemeral=True)
            return
        s = _sessions.get(user_id)
        if not s or s['index'] <= 0:
            await inter.response.defer()
            return
        s['index'] -= 1
        new_view = _build_engage_panel_view(user_id, s)
        embed    = _build_engage_embed(s)
        await inter.response.edit_message(embed=embed, view=new_view)

    async def next_cb(inter: discord.Interaction):
        if inter.user.id != user_id:
            await inter.response.send_message('❌ Not your session.', ephemeral=True)
            return
        s = _sessions.get(user_id)
        if not s:
            await inter.response.send_message('❌ Session expired.', ephemeral=True)
            return
        if s['index'] >= len(s['queue']) - 1:
            await _finalize_session(inter, user_id)
            return
        s['index'] += 1
        new_view = _build_engage_panel_view(user_id, s)
        embed    = _build_engage_embed(s)
        await inter.response.edit_message(embed=embed, view=new_view)

    async def submit_cb(inter: discord.Interaction):
        if inter.user.id != user_id:
            await inter.response.send_message('❌ Not your session.', ephemeral=True)
            return
        s = _sessions.get(user_id)
        if not s:
            await inter.response.send_message('❌ Session expired.', ephemeral=True)
            return
        curr_sub_id = s['queue'][s['index']]['submission_id']
        sels_curr   = s['selections'].get(curr_sub_id, {})
        if not any(sels_curr.values()):
            await inter.response.send_message('⚠️ Select at least one task before submitting.', ephemeral=True)
            return
        if s['index'] >= len(s['queue']) - 1:
            await _finalize_session(inter, user_id)
            return
        s['index'] += 1
        new_view = _build_engage_panel_view(user_id, s)
        embed    = _build_engage_embed(s)
        await inter.response.edit_message(embed=embed, view=new_view)

    prev_btn = discord.ui.Button(
        style=discord.ButtonStyle.secondary, label='◀️ Prev',
        disabled=(idx == 0), row=1,
        custom_id=f'engage:prev:{user_id}',
    )
    prev_btn.callback = prev_cb

    next_btn = discord.ui.Button(
        style=discord.ButtonStyle.secondary, label='Next ▶️',
        row=1, custom_id=f'engage:next:{user_id}',
    )
    next_btn.callback = next_cb

    submit_btn = discord.ui.Button(
        style=discord.ButtonStyle.primary,
        label='✅ Done & Next' if idx < total - 1 else '✅ Finish',
        row=1, custom_id=f'engage:submit:{user_id}',
    )
    submit_btn.callback = submit_cb

    finish_btn = discord.ui.Button(
        style=discord.ButtonStyle.danger, label='Exit',
        row=1, custom_id=f'engage:exit:{user_id}',
    )
    async def exit_cb(inter: discord.Interaction):
        if inter.user.id != user_id:
            await inter.response.send_message('❌ Not your session.', ephemeral=True)
            return
        await _finalize_session(inter, user_id)
    finish_btn.callback = exit_cb

    view.add_item(prev_btn)
    view.add_item(next_btn)
    view.add_item(submit_btn)
    view.add_item(finish_btn)
    return view


def _build_engage_embed(session: dict) -> discord.Embed:
    idx   = session['index']
    queue = session['queue']
    sub   = queue[idx]
    pool  = session['pool']
    total = len(queue)
    sels  = session['selections'].get(sub['submission_id'], {})

    color_str = (pool.get('embed_color') or '').strip()
    try:
        color = int(color_str.lstrip('#'), 16) if color_str else 0x94730D
    except ValueError:
        color = 0x94730D

    tasks_toggled = [t for t in ('like', 'comment', 'retweet') if sels.get(t)]
    task_str = ', '.join(t.capitalize() for t in tasks_toggled) if tasks_toggled else 'None selected'

    embed = discord.Embed(
        title=f'Engage — Tweet {idx + 1} / {total}',
        description=(
            f'**[Open Tweet]({sub["tweet_url"]})**\n\n'
            f'Submitted by: @{sub.get("submitter_x_username") or "unknown"}\n\n'
            f'**Selected:** {task_str}\n\n'
            f'Use the buttons below to toggle tasks, then click ✅ Done & Next.'
        ),
        color=color,
    )
    footer = pool.get('embed_footer_text') or 'AmeretaVerse • Engage'
    embed.set_footer(text=footer)
    return embed


async def _finalize_session(interaction: discord.Interaction, user_id: int) -> None:
    """Process all selections in the session, run verification, award points."""
    session = _sessions.get(user_id)
    if not session:
        await interaction.response.edit_message(content='❌ Session not found.', embed=None, view=None)
        return

    guild_id = session['guild_id']
    pool     = session['pool']
    pool_id  = pool['pool_id']
    queue    = session['queue']
    sels     = session['selections']
    x_username = session.get('x_username', '')

    # Collect submissions where at least one task was selected
    claimed_subs = [
        (sub, sels[sub['submission_id']])
        for sub in queue
        if sub['submission_id'] in sels and any(sels[sub['submission_id']].values())
    ]

    if not claimed_subs:
        _clear_session(user_id)
        await interaction.response.edit_message(
            content='You didn\'t select any tasks. No points earned.',
            embed=None, view=None,
        )
        return

    # Signal processing
    await interaction.response.edit_message(
        content=f'⏳ Processing {len(claimed_subs)} tweet(s)…',
        embed=None, view=None,
    )

    use_live = guild_id in LIVE_VERIFICATION_GUILD_IDS
    total_earned = 0
    result_lines = []

    allowed_tasks = {
        'like':    bool(pool.get('allow_like',    1)),
        'comment': bool(pool.get('allow_comment', 1)),
        'retweet': bool(pool.get('allow_retweet', 1)),
    }
    ratios = {
        'like':    int(pool.get('point_ratio_like',    12)),
        'comment': int(pool.get('point_ratio_comment', 40)),
        'retweet': int(pool.get('point_ratio_retweet', 48)),
    }
    total_pts = int(pool.get('total_points_per_engage', 10))

    for sub, sel in claimed_subs:
        sub_id   = sub['submission_id']
        tweet_id = sub.get('tweet_id', '')

        # Effective claimed per task (only if pool allows it)
        effective = {t: bool(sel.get(t)) and allowed_tasks[t] for t in ('like', 'comment', 'retweet')}

        if use_live:
            # Live verification
            ver_results = {}
            if effective.get('comment') and tweet_id:
                ver_results['comment'] = await check_comment(tweet_id, x_username)
            if effective.get('retweet') and tweet_id:
                ver_results['retweet'] = await check_retweet(tweet_id, x_username)
            if effective.get('like'):
                companion_results = [ver_results[t] for t in ('comment', 'retweet') if t in ver_results]
                if not companion_results:
                    ver_results['like'] = {'verified': True, 'reason': 'like_only_trusted'}
                elif any(r.get('verified') is False for r in companion_results):
                    ver_results['like'] = {'verified': False, 'reason': 'like_companion_failed'}
                elif any(r.get('verified') is None for r in companion_results):
                    ver_results['like'] = {'verified': None, 'reason': 'like_companion_inconclusive'}
                else:
                    ver_results['like'] = {'verified': True, 'reason': 'like_companions_passed'}

            # Award per-task for partial passes
            earned = 0
            verified_tasks = {}
            source = 'live'
            for task in ('like', 'comment', 'retweet'):
                if not effective.get(task):
                    verified_tasks[task] = None
                    continue
                v = ver_results.get(task, {}).get('verified')
                verified_tasks[task] = v
                if v is True:
                    task_pts = total_pts * ratios[task] // 100
                    earned  += task_pts
                    add_engage_verification_log(str(guild_id), pool_id, sub_id, str(user_id), task, 1, 1, 'live')
                elif v is False:
                    add_engage_verification_log(str(guild_id), pool_id, sub_id, str(user_id), task, 1, 0, 'live',
                                               error_text=ver_results[task].get('reason'))
                else:
                    source = 'live_provisional'
                    add_engage_verification_log(str(guild_id), pool_id, sub_id, str(user_id), task, 1, -1, 'live_provisional',
                                               error_text=ver_results.get(task, {}).get('reason', 'inconclusive'))

            upsert_engage_action(
                str(guild_id), pool_id, sub_id, str(user_id), x_username,
                int(effective.get('like', False)), int(effective.get('comment', False)), int(effective.get('retweet', False)),
                verified_tasks.get('like'), verified_tasks.get('comment'), verified_tasks.get('retweet'),
                earned, source,
            )
        else:
            # Instant award (adaptive daily check verifies later)
            earned = sum(
                total_pts * ratios[task] // 100
                for task in ('like', 'comment', 'retweet')
                if effective.get(task)
            )
            upsert_engage_action(
                str(guild_id), pool_id, sub_id, str(user_id), x_username,
                int(effective.get('like', False)), int(effective.get('comment', False)), int(effective.get('retweet', False)),
                None, None, None,
                earned, None,  # verification_source=None means pending daily check
            )

        total_earned += earned
        task_emojis = {'like': '❤️', 'comment': '💬', 'retweet': '🔁'}
        claimed_str = ' '.join(task_emojis[t] for t in ('like', 'comment', 'retweet') if effective.get(t))
        result_lines.append(f'Tweet #{sub.get("display_number", sub_id)} — {claimed_str} — {earned} pts')

    # Bulk award points
    if total_earned > 0:
        upsert_engage_user_points(str(guild_id), pool_id, str(user_id), total_earned,
                                  delta_engaged=len(claimed_subs))
        with get_connection() as conn:
            conn.execute("UPDATE users SET engage_points=engage_points+? WHERE user_id=?",
                         (total_earned, user_id))

    _clear_session(user_id)

    pts_row = get_engage_user_points(pool_id, str(user_id))
    embed = discord.Embed(
        title=f'✅ Done — {total_earned} engage pts earned',
        description=(
            '**Results:**\n' + '\n'.join(result_lines) +
            f'\n\n**Your balance:** {pts_row["points"]} engage pts'
        ),
        color=0x94730D,
    )
    pool_display = pool.get('display_name') or pool.get('name', 'Engage')
    embed.set_footer(text=f'{pool_display} Pool')
    await interaction.edit_original_response(content=None, embed=embed)


class EngageCog(commands.Cog, name='Engage'):
    def __init__(self, bot):
        self.bot = bot
        self.expire_task.start()

    def cog_unload(self):
        self.expire_task.cancel()

    # ── Helper ──────────────────────────────────────────────────────────────

    def _resolve_pool(self, interaction: discord.Interaction) -> dict | None:
        """Find pool by interaction channel. Returns pool dict or None."""
        guild_id   = str(interaction.guild_id or 0)
        channel_id = str(interaction.channel_id or 0)
        return get_engage_pool_by_channel(guild_id, channel_id)

    # ── /engage ─────────────────────────────────────────────────────────────

    @app_commands.command(name='engage', description='Browse active tweets and engage to earn engage points.')
    async def engage_cmd(self, interaction: discord.Interaction):
        pool = self._resolve_pool(interaction)
        if not pool:
            await interaction.response.send_message(
                '❌ No engage pool is configured for this channel. Use the correct engage channel.',
                ephemeral=True,
            )
            return
        if not pool.get('enabled'):
            await interaction.response.send_message('❌ This engage pool is currently disabled.', ephemeral=True)
            return

        user_id    = interaction.user.id
        guild_id   = interaction.guild_id or 0
        x_username = get_user_x_username(user_id)
        if not x_username:
            await interaction.response.send_message(
                '⚠️ Link your X account first: `/setx your_username`', ephemeral=True
            )
            return

        pool_id = pool['pool_id']
        queue   = list_active_submissions(pool_id, limit=10, exclude_user_id=str(user_id))
        if not queue:
            await interaction.response.send_message(
                '📭 No active tweets in this pool right now. Check back later or submit your own!',
                ephemeral=True,
            )
            return

        # Start session
        _set_session(user_id, {
            'pool_id':     pool_id,
            'guild_id':    guild_id,
            'pool':        pool,
            'queue':       queue,
            'index':       0,
            'selections':  {},
            'x_username':  x_username,
        })

        view  = _build_engage_panel_view(user_id, _sessions[user_id])
        embed = _build_engage_embed(_sessions[user_id])
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    # ── /submit ──────────────────────────────────────────────────────────────

    @app_commands.command(name='submit', description='Submit your tweet to the engage pool (costs engage points).')
    @app_commands.describe(tweet_url='Full tweet URL: https://x.com/.../status/...')
    async def submit_cmd(self, interaction: discord.Interaction, tweet_url: str):
        pool = self._resolve_pool(interaction)
        if not pool:
            await interaction.response.send_message(
                '❌ No engage pool is configured for this channel.', ephemeral=True
            )
            return
        if not pool.get('enabled'):
            await interaction.response.send_message('❌ This engage pool is currently disabled.', ephemeral=True)
            return

        user_id  = interaction.user.id
        guild_id = interaction.guild_id or 0

        x_username = get_user_x_username(user_id)
        if not x_username:
            await interaction.response.send_message(
                '⚠️ Link your X account first: `/setx your_username`', ephemeral=True
            )
            return

        tweet_id = extract_tweet_id(tweet_url)
        if not tweet_id:
            await interaction.response.send_message(
                '❌ Invalid tweet URL. Must be an x.com or twitter.com status link.', ephemeral=True
            )
            return

        pool_id = pool['pool_id']
        await interaction.response.defer(ephemeral=True)

        # Verify tweet author
        tweet_info = await lookup_twitter_user_by_login(x_username)
        if not tweet_info:
            await interaction.followup.send(
                '❌ Could not look up your X account. Make sure your /setx username is correct.',
                ephemeral=True,
            )
            return

        # Check daily limit
        daily_count = get_user_daily_submission_count(pool_id, str(user_id))
        daily_limit = int(pool.get('daily_submission_limit', 3))
        if daily_count >= daily_limit:
            await interaction.followup.send(
                f'❌ Daily submission limit reached ({daily_limit}/day). Try again tomorrow.',
                ephemeral=True,
            )
            return

        # Check points balance
        cost        = int(pool.get('submit_cost', 50))
        pts_row     = get_engage_user_points(pool_id, str(user_id))
        balance     = pts_row.get('points', 0)
        if balance < cost:
            await interaction.followup.send(
                f'❌ Not enough engage points. Need {cost}, have {balance}. Engage with more tweets to earn points.',
                ephemeral=True,
            )
            return

        # Deduct cost and create submission
        upsert_engage_user_points(str(guild_id), pool_id, str(user_id),
                                  delta_points=-cost, delta_submitted=1)
        try:
            sub = create_engage_submission(
                str(guild_id), pool_id, str(user_id),
                tweet_url.strip(), tweet_id, x_username,
                cost, int(pool.get('ttl_hours', 24)),
            )
        except Exception as e:
            # Refund on failure
            upsert_engage_user_points(str(guild_id), pool_id, str(user_id), delta_points=cost, delta_submitted=-1)
            print(f'[engage] submit error: {type(e).__name__}: {e}')
            await interaction.followup.send('❌ Failed to submit. Please try again.', ephemeral=True)
            return

        display_num = sub.get('display_number', sub['submission_id'])
        new_balance = balance - cost
        await interaction.followup.send(
            f'✅ Tweet **#{display_num:04d}** submitted to the pool.\n'
            f'Cost: **{cost}** engage pts | New balance: **{new_balance}** engage pts',
            ephemeral=True,
        )

    # ── /engage-stats ────────────────────────────────────────────────────────

    @app_commands.command(name='engage-stats', description='Your engage points and activity in this pool.')
    async def engage_stats_cmd(self, interaction: discord.Interaction):
        pool = self._resolve_pool(interaction)
        if not pool:
            await interaction.response.send_message(
                '❌ No engage pool is configured for this channel.', ephemeral=True
            )
            return

        pts_row = get_engage_user_points(pool['pool_id'], str(interaction.user.id))
        pool_display = pool.get('display_name') or pool.get('name', 'Engage')
        embed = discord.Embed(
            title=f'Your {pool_display} Stats',
            color=0x94730D,
        )
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.add_field(name='Engage Points', value=f'`{pts_row.get("points", 0)}`', inline=True)
        embed.add_field(name='Tweets Engaged', value=f'`{pts_row.get("total_engaged", 0)}`', inline=True)
        embed.add_field(name='Tweets Submitted', value=f'`{pts_row.get("total_submitted", 0)}`', inline=True)
        embed.set_footer(text=f'{pool_display} Pool')
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /engage-leaderboard ──────────────────────────────────────────────────

    @app_commands.command(name='engage-leaderboard', description='Top engage point earners in this pool.')
    async def engage_leaderboard_cmd(self, interaction: discord.Interaction):
        pool = self._resolve_pool(interaction)
        if not pool:
            await interaction.response.send_message(
                '❌ No engage pool is configured for this channel.', ephemeral=True
            )
            return

        rows = get_engage_leaderboard(pool['pool_id'], limit=10)
        if not rows:
            await interaction.response.send_message('No data yet. Be the first to engage!', ephemeral=True)
            return

        medals = ['🥇', '🥈', '🥉']
        lines  = []
        for i, r in enumerate(rows):
            rank = medals[i] if i < 3 else f'`{i+1}.`'
            name = r.get('username') or f'<@{r["user_id"]}>'
            lines.append(f'{rank} **{name}** — `{r["points"]} pts`')

        pool_display = pool.get('display_name') or pool.get('name', 'Engage')
        embed = discord.Embed(
            title=f'⚡ {pool_display} Leaderboard',
            description='\n'.join(lines),
            color=0x94730D,
        )
        embed.set_footer(text=f'{pool_display} Pool')
        await interaction.response.send_message(embed=embed)

    # ── Background tasks ──────────────────────────────────────────────────────

    @tasks.loop(hours=1)
    async def expire_task(self):
        count = expire_old_submissions()
        if count:
            print(f'[engage] expired {count} old submissions')

    @expire_task.before_loop
    async def before_expire_task(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(EngageCog(bot))
