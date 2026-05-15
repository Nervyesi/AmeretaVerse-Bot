"""
raidbot.py — Per-guild Raid module.

Flow:
  1. Admin posts a raid via /raid post or Dashboard. Embed has one "Join Raid" button.
  2. User clicks Join Raid → bot sends ephemeral personal panel with task toggles.
  3. User toggles tasks silently (edit_message, no new messages), then confirms.
     Confirm records participation and shows result in the same ephemeral.
  4. Daily midnight UTC: adaptive sample of pending participations verified via Apify.
  5. Flagged users have points deducted; verification log updated.
"""

import json
import asyncio
import traceback
import requests
from datetime import datetime, timezone, timedelta, time as dtime

import discord
from discord import app_commands
from discord.ext import commands, tasks

from database import (
    get_connection,
    get_raid_settings,
    upsert_raid_settings,
    create_guild_raid,
    get_guild_raid,
    update_guild_raid,
    end_raid,
    get_raid_participation,
    create_raid_participation,
    update_raid_participation,
    upsert_raid_user_points,
    get_raid_user_points,
    get_raid_leaderboard,
    add_raid_verification_log,
    count_pending_participations_24h,
    sample_pending_participations,
    get_user_x_username,
    find_user_by_x_username,
    list_guild_raids,
)
from cogs._utils import resolve_channel, resolve_role
from cogs._branding import build_branded_embed, PREMIUM_GUILD_IDS
from cogs._twitter import extract_tweet_id

# ── Constants ─────────────────────────────────────────────────────────────────
MANUAL_CHECK_DAILY_LIMIT = 10
# Operational override — ONLY the bot owner's dev/test server.
# Do NOT couple this to PREMIUM_GUILD_IDS: premium customers still get the daily
# limit to prevent API cost runaway.  Add IDs here manually and deliberately.
UNLIMITED_MANUAL_CHECK_GUILD_IDS: frozenset[int] = frozenset({1199707792706117642})

# Guilds that use live (on-submit) Twitter verification instead of daily sampling.
# Independent from both PREMIUM_GUILD_IDS and UNLIMITED_MANUAL_CHECK_GUILD_IDS.
# Daily verification check skips these guilds since tasks are checked at confirm time.
LIVE_VERIFICATION_GUILD_IDS: frozenset[int] = frozenset({1199707792706117642})

DEFAULT_GUIDE_TITLE = "Raid System - How It Works"

DEFAULT_GUIDE_DESCRIPTION = (
    "Welcome! This is your community's raid system. Here's everything you need to know to participate.\n\n"
    "**━━━━━━━━━━━━━━━━━━━━━━━**\n\n"
    "**1️⃣ Link your X (Twitter) account**\n\n"
    "Use the `/setx` command followed by your X username (without the @).\n\n"
    "Example: `/setx myusername`\n\n"
    "You only need to do this once. If you ever need to change it, you can update it again after a 7-day cooldown.\n\n"
    "**━━━━━━━━━━━━━━━━━━━━━━━**\n\n"
    "**2️⃣ Wait for a raid to drop**\n\n"
    "When a new raid is posted, you'll see an embed in the raid channel containing:\n"
    "- The tweet to raid\n"
    "- Which tasks count (Like, Comment, Retweet)\n"
    "- A 🎯 **Join Raid** button below the embed\n\n"
    "**━━━━━━━━━━━━━━━━━━━━━━━**\n\n"
    "**3️⃣ Complete the tasks on X**\n\n"
    "Open the tweet on X and do the tasks you want to claim. Be genuine — write thoughtful comments, don't just spam.\n\n"
    "**━━━━━━━━━━━━━━━━━━━━━━━**\n\n"
    "**4️⃣ Claim your tasks**\n\n"
    "Back on Discord, click 🎯 **Join Raid** under the raid embed. "
    "This opens a private panel just for you:\n\n"
    "❤️ **Like** — click to toggle if you liked the tweet\n"
    "💬 **Comment** — click to toggle if you commented\n"
    "🔁 **Retweet** — click to toggle if you retweeted\n\n"
    "Each click silently updates your selection — nothing is recorded until you confirm.\n\n"
    "**━━━━━━━━━━━━━━━━━━━━━━━**\n\n"
    "**5️⃣ Confirm your submission**\n\n"
    "When you're ready, click ✅ **Confirm**. The bot records what you claimed and shows the points you earned. "
    "Your submission is final — you can only confirm once per raid.\n\n"
    "**━━━━━━━━━━━━━━━━━━━━━━━**\n\n"
    "**🔍 How we verify**\n\n"
    "A random sample of submissions is automatically checked against X every day. "
    "Admins can also manually verify any submission. "
    "If a task was claimed but not done, it gets flagged and points are deducted. "
    "Admins can also take extra actions (ban, mute, etc.) at their discretion.\n\n"
    "**━━━━━━━━━━━━━━━━━━━━━━━**\n\n"
    "**📊 Track your progress**\n\n"
    "- `/raid leaderboard` — see the top raiders in this server\n"
    "- `/raid profile` — see your own stats\n\n"
    "Happy raiding! 🚀"
)

# ── In-memory per-user panel state ────────────────────────────────────────────
# key = (user_id, raid_id) → {'like': bool, 'comment': bool, 'retweet': bool, 'updated_at': datetime}
_panel_selections: dict = {}


def _get_panel_state(user_id: int, raid_id: int) -> dict:
    key = (user_id, raid_id)
    if key not in _panel_selections:
        _panel_selections[key] = {
            'like': False, 'comment': False, 'retweet': False,
            'updated_at': datetime.utcnow(),
        }
    return _panel_selections[key]


def _clear_panel_state(user_id: int, raid_id: int):
    _panel_selections.pop((user_id, raid_id), None)


# ── Adaptive check percentage ─────────────────────────────────────────────────

def _adaptive_check_pct(total_pending: int) -> int:
    if total_pending < 100:   return 50
    if total_pending < 500:   return 30
    if total_pending < 2000:  return 15
    if total_pending < 5000:  return 8
    return 5


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fetch_tweet(url: str) -> dict:
    result = {'author': None, 'text': None, 'image': None, 'is_video': False}
    tweet_id = extract_tweet_id(url)
    if not tweet_id:
        return result
    try:
        resp = requests.get(f'https://api.fxtwitter.com/status/{tweet_id}', timeout=10)
        if resp.status_code != 200:
            return result
        data = resp.json()
        tweet = data.get('tweet', {})
        result['text'] = tweet.get('text')
        result['author'] = tweet.get('author', {}).get('name')
        media = tweet.get('media', {})
        photos = media.get('photos', [])
        videos = media.get('videos', [])
        if videos:
            result['image'] = videos[0].get('thumbnail_url')
            result['is_video'] = True
        elif photos:
            result['image'] = photos[0].get('url')
    except Exception:
        pass
    return result


def _build_raid_embed(guild_id: int, raid: dict, tweet: dict, settings: dict) -> discord.Embed:
    color_str = (settings.get('embed_color') or '').strip()
    try:
        color = int(color_str.lstrip('#'), 16) if color_str else 0x94730D
    except ValueError:
        color = 0x94730D

    tasks_obj = {}
    try:
        tasks_obj = json.loads(raid.get('tasks_json') or '{}')
    except Exception:
        pass
    active_tasks = [t for t, v in tasks_obj.items() if v]
    task_str = ' • '.join(t.capitalize() for t in active_tasks) or 'None'

    tweet_lines = [f"## **[📌 New Tweet]({raid['tweet_url']})**"]
    author = tweet.get('author') or ''
    text   = (tweet.get('text') or '')
    if len(text) > 280:
        text = text[:280] + '...'
    if tweet.get('is_video') and text:
        text = f'▶ Video\n{text}'
    if author or text:
        tweet_lines.append('')
        if author:
            tweet_lines.append(f'> **{author}**')
        for line in text.splitlines():
            tweet_lines.append(f'> {line}')

    mode_note = (
        '\n⚠️ **All tasks must be completed** to earn points.'
        if raid.get('mode') == 'all'
        else '\n💡 Earn points for each task you complete.'
    )
    display_num = raid.get('display_number') or raid.get('raid_id', 0)
    description = (
        '\n'.join(tweet_lines) + '\n\n'
        f'**Tasks:** {task_str}\n'
        f'**Points:** {raid["total_points"]} pts  •  **Mode:** {raid.get("mode", "partial")}\n\n'
        f'Click 🎯 **Join Raid** to participate.{mode_note}'
    )
    embed = discord.Embed(description=description, color=color)

    if settings.get('embed_thumbnail_url'):
        embed.set_thumbnail(url=settings['embed_thumbnail_url'])
    if tweet.get('image'):
        embed.set_image(url=tweet['image'])

    footer = (settings.get('embed_footer_text') or 'AmeretaVerse • Raids')
    embed.set_footer(text=f'{footer}  |  Raid #{display_num:04d}')
    return embed


def _check_auto_end(raid: dict) -> tuple[dict, bool]:
    """Return (updated_raid_dict, was_ended). Mutates raid dict if ended."""
    if raid.get('status') != 'active':
        return raid, False
    posted_str = raid.get('posted_at') or ''
    if not posted_str:
        return raid, False
    try:
        posted = datetime.fromisoformat(posted_str)
        if posted.tzinfo is None:
            posted = posted.replace(tzinfo=timezone.utc)
        if (datetime.now(timezone.utc) - posted) > timedelta(hours=48):
            end_raid(int(raid['raid_id']), int(raid['guild_id']), ended_reason='auto_48h')
            raid = dict(raid)
            raid['status'] = 'ended'
            raid['ended_reason'] = 'auto_48h'
            return raid, True
    except Exception:
        pass
    return raid, False


# ── Personal ephemeral panel (non-persistent, 10-min timeout) ─────────────────

def _build_personal_panel_view(raid_id: int, state: dict, user_id: int, allowed_tasks: dict) -> discord.ui.View:
    view = discord.ui.View(timeout=600)

    for task, emoji, label in [
        ('like',    '❤️', 'Like'),
        ('comment', '💬', 'Comment'),
        ('retweet', '🔁', 'Retweet'),
    ]:
        if not allowed_tasks.get(task, False):
            continue
        is_on = state.get(task, False)
        btn = discord.ui.Button(
            style=discord.ButtonStyle.success if is_on else discord.ButtonStyle.secondary,
            label=label,
            emoji=emoji,
            custom_id=f'rp:{task}:{raid_id}:{user_id}',
        )

        def _make_toggle_cb(t):
            async def cb(interaction: discord.Interaction):
                if interaction.user.id != user_id:
                    await interaction.response.send_message('❌ This panel is not yours.', ephemeral=True)
                    return
                s = _get_panel_state(user_id, raid_id)
                s[t] = not s[t]
                s['updated_at'] = datetime.utcnow()
                new_view = _build_personal_panel_view(raid_id, s, user_id, allowed_tasks)
                await interaction.response.edit_message(view=new_view)
            return cb

        btn.callback = _make_toggle_cb(task)
        view.add_item(btn)

    confirm_btn = discord.ui.Button(
        style=discord.ButtonStyle.primary,
        label='Confirm',
        emoji='✅',
        custom_id=f'rp:confirm:{raid_id}:{user_id}',
    )

    async def confirm_cb(interaction: discord.Interaction):
        if interaction.user.id != user_id:
            await interaction.response.send_message('❌ This panel is not yours.', ephemeral=True)
            return
        await _handle_personal_confirm(interaction, raid_id, user_id)

    confirm_btn.callback = confirm_cb
    view.add_item(confirm_btn)
    return view


def _normalize_point_ratio(settings: dict, allowed_tasks: dict) -> dict:
    """Rescale point ratios so enabled tasks always sum to exactly 100%.

    When some tasks are disabled (e.g. retweet off), the remaining tasks'
    percentages are rescaled so their sum is still 100, preserving the
    relative proportions of the enabled tasks.
    """
    raw = {
        'like':    int(settings.get('point_ratio_like',    12)),
        'comment': int(settings.get('point_ratio_comment', 40)),
        'retweet': int(settings.get('point_ratio_retweet', 48)),
    }
    enabled = [t for t in ('like', 'comment', 'retweet') if allowed_tasks.get(t, False)]
    enabled_total = sum(raw[t] for t in enabled)

    if not enabled:
        return {'like': 0, 'comment': 0, 'retweet': 0}

    if enabled_total == 0:
        equal = 100 // len(enabled)
        return {t: (equal if t in enabled else 0) for t in ('like', 'comment', 'retweet')}

    normalized: dict = {}
    running = 0
    for i, t in enumerate(enabled):
        if i == len(enabled) - 1:
            normalized[t] = 100 - running  # last task gets the remainder
        else:
            normalized[t] = raw[t] * 100 // enabled_total
            running += normalized[t]
    for t in ('like', 'comment', 'retweet'):
        if t not in normalized:
            normalized[t] = 0
    return normalized


async def _instant_award(
    interaction: discord.Interaction,
    guild_id: int, user_id: int,
    raid: dict, claimed: dict, nr: dict,
):
    """Tenant-guild flow: award points immediately. Daily check verifies later."""
    raid_id = raid['raid_id']
    total   = int(raid['total_points'])

    earned, lines = 0, []
    if claimed['like']:
        pts = total * nr['like'] // 100; earned += pts
        lines.append(f'❤️ Like — {pts} pts')
    if claimed['comment']:
        pts = total * nr['comment'] // 100; earned += pts
        lines.append(f'💬 Comment — {pts} pts')
    if claimed['retweet']:
        pts = total * nr['retweet'] // 100; earned += pts
        lines.append(f'🔁 Retweet — {pts} pts')

    create_raid_participation(guild_id, raid_id, user_id, json.dumps(claimed), earned)
    upsert_raid_user_points(guild_id, user_id, earned, delta_raids=1)
    with get_connection() as conn:
        conn.execute(
            "UPDATE users SET total_points=total_points+? WHERE user_id=?",
            (earned, user_id),
        )
    _clear_panel_state(user_id, raid_id)

    display_num = raid.get('display_number') or raid_id
    embed = build_branded_embed(
        guild_id,
        title=f'✅ Submitted — {earned} pts earned',
        description=(
            '**Tasks claimed:**\n' + '\n'.join(lines) +
            f'\n\nRaid #{display_num:04d} — '
            '_A random sample of submissions is auto-verified daily._'
        ),
        cog_prefix='raid', use_thumbnail=True, use_image=False, use_footer=True,
    )
    await interaction.response.edit_message(content=None, embed=embed, view=None)


async def _live_verify_and_award(
    interaction: discord.Interaction,
    guild_id: int, user_id: int,
    raid: dict, claimed: dict, nr: dict,
    x_username: str,
):
    """Live verification flow: verify on X before awarding points.

    Three outcomes:
    - all_passed → award full points, mark verified in log
    - any_failed → no points, no participation record (user may retry after fixing)
    - any_inconclusive → provisional points, flagged for re-check
    """
    from cogs._twitter import check_comment, check_retweet

    raid_id     = raid['raid_id']
    total       = int(raid['total_points'])
    display_num = raid.get('display_number') or raid_id
    tweet_id    = raid.get('tweet_id') or ''

    _task_emoji = {'like': '❤️', 'comment': '💬', 'retweet': '🔁'}
    _task_label = {'like': 'Like', 'comment': 'Comment', 'retweet': 'Retweet'}

    # Signal that verification is in progress (must respond to interaction immediately)
    await interaction.response.edit_message(
        content='⏳ Verifying your tasks on X… (a few seconds)',
        embed=None, view=None,
    )

    if not tweet_id:
        await interaction.edit_original_response(
            content='⚠️ This raid has an invalid tweet URL — contact an admin.',
        )
        return

    # Run Twitter checks
    results: dict = {}

    if claimed.get('comment'):
        print(f'[raid] live_verify: check_comment tweet={tweet_id} user={x_username}')
        results['comment'] = await check_comment(tweet_id, x_username)

    if claimed.get('retweet'):
        print(f'[raid] live_verify: check_retweet tweet={tweet_id} user={x_username}')
        results['retweet'] = await check_retweet(tweet_id, x_username)

    # Like conditional logic: tie to companions' outcomes
    if claimed.get('like'):
        companion_results = [results[t] for t in ('comment', 'retweet') if t in results]
        if not companion_results:
            results['like'] = {'verified': True, 'reason': 'like_only_trusted'}
        elif any(r.get('verified') is False for r in companion_results):
            results['like'] = {'verified': False, 'reason': 'like_companion_failed'}
        elif any(r.get('verified') is None for r in companion_results):
            results['like'] = {'verified': None, 'reason': 'like_companion_inconclusive'}
        else:
            results['like'] = {'verified': True, 'reason': 'like_companions_passed'}

    any_failed      = any(r.get('verified') is False for r in results.values())
    any_inconclusive = any(r.get('verified') is None  for r in results.values())
    all_passed      = bool(results) and not any_failed and not any_inconclusive

    if all_passed:
        earned, lines = 0, []
        for task in ('like', 'comment', 'retweet'):
            if not claimed.get(task): continue
            pts = total * nr[task] // 100; earned += pts
            lines.append(f'{_task_emoji[task]} {_task_label[task]} — {pts} pts ✅')

        create_raid_participation(guild_id, raid_id, user_id, json.dumps(claimed), earned)
        upsert_raid_user_points(guild_id, user_id, earned, delta_raids=1)
        with get_connection() as conn:
            conn.execute("UPDATE users SET total_points=total_points+? WHERE user_id=?",
                         (earned, user_id))
        for task, r in results.items():
            add_raid_verification_log(guild_id, raid_id, user_id, task, True, 1, 'live')
        _clear_panel_state(user_id, raid_id)
        print(f'[raid] live_verify PASSED: user={user_id} raid={raid_id} earned={earned}')

        embed = build_branded_embed(guild_id,
            title=f'✅ Verified — {earned} pts earned',
            description='**Tasks verified:**\n' + '\n'.join(lines) + f'\n\nRaid #{display_num:04d}',
            cog_prefix='raid', use_thumbnail=True, use_image=False, use_footer=True,
        )
        await interaction.edit_original_response(content=None, embed=embed)
        return

    if any_failed:
        lines = []
        for task in ('like', 'comment', 'retweet'):
            if not claimed.get(task): continue
            v = results.get(task, {}).get('verified')
            icon, label = _task_emoji[task], _task_label[task]
            if v is True:    lines.append(f'{icon} {label} — ✅ verified')
            elif v is False: lines.append(f'{icon} {label} — ❌ not found on X')
            else:            lines.append(f'{icon} {label} — ⚠️ inconclusive')

        for task, r in results.items():
            v = r.get('verified')
            db_v = 1 if v is True else (0 if v is False else -1)
            add_raid_verification_log(guild_id, raid_id, user_id, task,
                                      True, db_v, 'live', error_text=r.get('reason'))
        _clear_panel_state(user_id, raid_id)
        print(f'[raid] live_verify FAILED: user={user_id} raid={raid_id}')

        embed = build_branded_embed(guild_id,
            title='❌ Verification failed — no points awarded',
            description=(
                'Your tasks could not be verified on X. No points were credited.\n\n'
                '**Results:**\n' + '\n'.join(lines) +
                '\n\nCompleted the tasks but seeing this? X can take a few minutes to sync — '
                'try again shortly.\n\n'
                f'Raid #{display_num:04d}'
            ),
            cog_prefix='raid', use_thumbnail=True, use_image=False, use_footer=True,
        )
        await interaction.edit_original_response(content=None, embed=embed)
        return

    # any_inconclusive — API error: award provisionally, leave as pending for re-check
    earned, lines = 0, []
    for task in ('like', 'comment', 'retweet'):
        if not claimed.get(task): continue
        pts = total * nr[task] // 100; earned += pts
        lines.append(f'{_task_emoji[task]} {_task_label[task]} — {pts} pts')

    create_raid_participation(guild_id, raid_id, user_id, json.dumps(claimed), earned)
    upsert_raid_user_points(guild_id, user_id, earned, delta_raids=1)
    with get_connection() as conn:
        conn.execute("UPDATE users SET total_points=total_points+? WHERE user_id=?",
                     (earned, user_id))
    for task, r in results.items():
        v = r.get('verified')
        db_v = 1 if v is True else (0 if v is False else -1)
        add_raid_verification_log(guild_id, raid_id, user_id, task,
                                  True, db_v, 'live_provisional', error_text=r.get('reason'))
    _clear_panel_state(user_id, raid_id)
    print(f'[raid] live_verify PROVISIONAL: user={user_id} raid={raid_id} earned={earned}')

    embed = build_branded_embed(guild_id,
        title=f'⚠️ Submitted — {earned} pts (provisional)',
        description=(
            'Verification hit a temporary API issue. Points have been credited provisionally '
            'and will be re-checked by an admin.\n\n'
            f'Raid #{display_num:04d}'
        ),
        cog_prefix='raid', use_thumbnail=True, use_image=False, use_footer=True,
    )
    await interaction.edit_original_response(content=None, embed=embed)


async def _handle_personal_confirm(interaction: discord.Interaction, raid_id: int, user_id: int):
    try:
        guild_id = interaction.guild_id or 0

        raid = get_guild_raid(raid_id, guild_id)
        if not raid:
            await interaction.response.edit_message(content='❌ Raid not found.', embed=None, view=None)
            return

        raid, _ = _check_auto_end(dict(raid))

        if raid['status'] != 'active':
            reason = raid.get('ended_reason') or ''
            msg = '❌ This raid has been ended by admin.' if reason == 'admin' else '❌ This raid has ended.'
            await interaction.response.edit_message(content=msg, embed=None, view=None)
            return

        if get_raid_participation(guild_id, raid_id, user_id):
            await interaction.response.edit_message(content='✅ You already submitted this raid.', embed=None, view=None)
            return

        state = _get_panel_state(user_id, raid_id)
        if not any([state.get('like'), state.get('comment'), state.get('retweet')]):
            await interaction.response.edit_message(content='⚠️ Select at least one task first.', embed=None, view=None)
            return

        allowed = {}
        try:
            allowed = json.loads(raid.get('tasks_json') or '{}')
        except Exception:
            pass
        claimed = {
            'like':    state.get('like', False)    and bool(allowed.get('like', True)),
            'comment': state.get('comment', False) and bool(allowed.get('comment', True)),
            'retweet': state.get('retweet', False) and bool(allowed.get('retweet', True)),
        }

        if raid.get('mode') == 'all':
            enabled_tasks = {t for t, v in allowed.items() if v}
            claimed_tasks = {t for t, v in claimed.items() if v}
            if claimed_tasks < enabled_tasks:
                _friendly = {'like': 'Like', 'comment': 'Comment', 'retweet': 'Retweet'}
                missing_str = ', '.join(_friendly.get(t, t.capitalize()) for t in sorted(enabled_tasks - claimed_tasks))
                await interaction.response.edit_message(
                    content=f"❌ This raid requires ALL tasks to be completed. You haven't selected: **{missing_str}**.",
                    embed=None, view=None,
                )
                return

        settings = get_raid_settings(guild_id)
        nr       = _normalize_point_ratio(settings, allowed)

        # Upsert username record (common to both award paths)
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO users (user_id, username) VALUES (?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username",
                (user_id, str(interaction.user)),
            )

        # Branch: live verification (on-submit) vs instant award (daily check)
        if guild_id in LIVE_VERIFICATION_GUILD_IDS:
            x_username = get_user_x_username(user_id) or ''
            await _live_verify_and_award(
                interaction, guild_id, user_id, raid, claimed, nr, x_username
            )
        else:
            await _instant_award(interaction, guild_id, user_id, raid, claimed, nr)

    except Exception as e:
        print(f'[raid] confirm error: {type(e).__name__}: {e}')
        traceback.print_exc()
        try:
            if not interaction.response.is_done():
                await interaction.response.edit_message(content='An error occurred.', embed=None, view=None)
        except Exception:
            pass


# ── RaidJoinButton — single persistent entry-point button ─────────────────────

class RaidJoinButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r'raid:join:(?P<rid>\d+)',
):
    def __init__(self, raid_id: int):
        super().__init__(
            discord.ui.Button(
                style=discord.ButtonStyle.success,
                label='Join Raid',
                emoji='🎯',
                custom_id=f'raid:join:{raid_id}',
            )
        )
        self.raid_id = raid_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match['rid']))

    async def callback(self, interaction: discord.Interaction):
        try:
            guild_id = interaction.guild_id or 0
            user_id  = interaction.user.id

            # FIX 7: check enabled first
            settings = get_raid_settings(guild_id)
            if not settings or not settings.get('enabled'):
                await interaction.response.send_message(
                    '❌ The Raid System is currently disabled.', ephemeral=True
                )
                return

            raid = get_guild_raid(self.raid_id, guild_id)
            if not raid:
                await interaction.response.send_message('❌ Raid not found.', ephemeral=True)
                return

            # FIX 5: 48h auto-end
            raid = dict(raid)
            raid, _ = _check_auto_end(raid)

            if raid['status'] != 'active':
                reason = raid.get('ended_reason') or ''
                msg = '❌ This raid has been ended by admin.' if reason == 'admin' else '❌ This raid has ended.'
                await interaction.response.send_message(msg, ephemeral=True)
                return

            x_username = get_user_x_username(user_id)
            if not x_username:
                await interaction.response.send_message(
                    '⚠️ Link your X account first: `/setx your_username`', ephemeral=True
                )
                return

            if get_raid_participation(guild_id, self.raid_id, user_id):
                await interaction.response.send_message(
                    '✅ You already submitted this raid.', ephemeral=True
                )
                return

            allowed_tasks = {'like': True, 'comment': True, 'retweet': True}
            try:
                parsed = json.loads(raid.get('tasks_json') or '{}')
                if any(parsed.values()):
                    allowed_tasks = parsed
            except Exception:
                pass
            state = _get_panel_state(user_id, self.raid_id)
            view  = _build_personal_panel_view(self.raid_id, state, user_id, allowed_tasks)
            await interaction.response.send_message(
                content='**Select the tasks you completed, then click ✅ Confirm.**',
                view=view,
                ephemeral=True,
            )

        except Exception as e:
            print(f'[raid] join button error: {type(e).__name__}: {e}')
            traceback.print_exc()
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message('An error occurred.', ephemeral=True)
            except Exception:
                pass


def build_raid_panel_view(raid_id: int) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    v.add_item(RaidJoinButton(raid_id))
    return v


# ── RaidPostModal ─────────────────────────────────────────────────────────────

class RaidPostModal(discord.ui.Modal, title='Post New Raid'):
    tweet_url    = discord.ui.TextInput(label='Tweet URL', placeholder='https://x.com/user/status/123456789', max_length=200)
    total_points = discord.ui.TextInput(label='Total Points', placeholder='100', max_length=10)
    mode         = discord.ui.TextInput(label='Mode', placeholder='partial  (or: all)', default='partial', max_length=10)
    tasks        = discord.ui.TextInput(label='Tasks (comma-separated)', placeholder='like,comment,retweet', default='like,comment,retweet', max_length=50)

    def __init__(self, bot):
        super().__init__()
        self._bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id or 0
        settings = get_raid_settings(guild_id)

        if not settings or not settings.get('enabled'):
            await interaction.response.send_message(
                '❌ The Raid System is disabled on this server.', ephemeral=True
            )
            return

        tweet_url_val = self.tweet_url.value.strip()
        tweet_id_val  = extract_tweet_id(tweet_url_val)
        if not tweet_id_val:
            await interaction.response.send_message(
                '❌ Invalid tweet URL — must be an x.com or twitter.com status link.', ephemeral=True
            )
            return

        try:
            pts = int(self.total_points.value.strip())
            if pts < 1:
                raise ValueError
        except ValueError:
            await interaction.response.send_message('❌ Total points must be a positive integer.', ephemeral=True)
            return

        ch_id = (settings.get('raid_channel_id') or '').strip()
        if not ch_id:
            await interaction.response.send_message(
                '❌ No raid channel configured. Set it in Dashboard → Raid → Settings first.', ephemeral=True
            )
            return
        channel = resolve_channel(interaction.guild, ch_id)
        if not channel:
            await interaction.response.send_message(
                f'❌ Raid channel not found: `{ch_id}`. Update it in Dashboard → Raid → Settings.', ephemeral=True
            )
            return

        mode_val   = self.mode.value.strip().lower()
        if mode_val not in ('all', 'partial'):
            mode_val = 'partial'
        raw_tasks  = [t.strip().lower() for t in self.tasks.value.split(',') if t.strip()]
        valid_set  = {'like', 'comment', 'retweet'}
        task_list  = [t for t in raw_tasks if t in valid_set] or ['like', 'comment', 'retweet']
        tasks_obj  = {t: (t in task_list) for t in ['like', 'comment', 'retweet']}
        tasks_json = json.dumps(tasks_obj)

        await interaction.response.defer(ephemeral=True)

        tweet_data = _fetch_tweet(tweet_url_val)
        raid_id    = create_guild_raid(
            guild_id, tweet_url_val, tweet_id_val, pts, mode_val, tasks_json,
            interaction.user.id,
        )
        raid = get_guild_raid(raid_id, guild_id)

        embed = _build_raid_embed(guild_id, raid, tweet_data, settings)
        view  = build_raid_panel_view(raid_id)

        ping_raw  = (settings.get('raid_ping_role_id') or settings.get('ping_role_id') or '').strip()
        ping_role = resolve_role(interaction.guild, ping_raw) if ping_raw else None
        content   = f'{ping_role.mention} — new raid just dropped! ⚔️' if ping_role else '⚔️ New raid just dropped!'

        msg = await channel.send(content=content, embed=embed, view=view,
                                  allowed_mentions=discord.AllowedMentions(roles=True))
        self._bot.add_view(view, message_id=msg.id)
        update_guild_raid(raid_id, guild_id, channel_id=str(channel.id), message_id=str(msg.id))

        display_num = raid.get('display_number') or raid_id
        await interaction.followup.send(
            f'✅ Raid **#{display_num:04d}** posted in {channel.mention}.', ephemeral=True
        )


# ── Cog ───────────────────────────────────────────────────────────────────────

class RaidsCog(commands.Cog, name='Raids'):
    def __init__(self, bot):
        self.bot = bot
        self.daily_verification_check.start()

    def cog_unload(self):
        self.daily_verification_check.cancel()

    # ── /raid command group ───────────────────────────────────────────────────

    raid_group = app_commands.Group(name='raid', description='Raid system commands')

    @raid_group.command(name='post', description='Create and post a new raid (admin/staff only)')
    async def raid_post(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id or 0
        settings = get_raid_settings(guild_id)

        if not settings or not settings.get('enabled'):
            await interaction.response.send_message(
                '❌ The Raid System is disabled on this server.', ephemeral=True
            )
            return

        is_authorized = interaction.user.guild_permissions.administrator
        if not is_authorized:
            raid_role_ids = (settings.get('raid_role_ids') or '').strip()
            for r_str in raid_role_ids.split(','):
                role = resolve_role(interaction.guild, r_str.strip())
                if role and role in interaction.user.roles:
                    is_authorized = True
                    break

        if not is_authorized:
            await interaction.response.send_message(
                '❌ You need admin or a configured raid role to post raids.', ephemeral=True
            )
            return

        await interaction.response.send_modal(RaidPostModal(self.bot))

    @raid_group.command(name='leaderboard', description='Top raiders in this server')
    async def raid_leaderboard(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id or 0
        settings = get_raid_settings(guild_id)
        if not settings or not settings.get('enabled'):
            await interaction.response.send_message('❌ The Raid System is disabled on this server.', ephemeral=True)
            return

        rows = get_raid_leaderboard(guild_id, limit=10)
        if not rows:
            await interaction.response.send_message('No raid participants yet. Be the first. ⚔️')
            return

        medals = ['🥇', '🥈', '🥉']
        lines  = []
        for i, r in enumerate(rows):
            rank = medals[i] if i < 3 else f'`{i+1}.`'
            name = r.get('username') or f'<@{r["user_id"]}>'
            lines.append(f'{rank} **{name}** — `{r["total_points"]} pts`')
        embed = build_branded_embed(
            guild_id, title='⚔️ Raid Leaderboard',
            description='\n'.join(lines),
            cog_prefix='raid', use_thumbnail=True, use_image=False, use_footer=True,
        )
        await interaction.response.send_message(embed=embed)

    @raid_group.command(name='profile', description='View raid stats for yourself or a member')
    @app_commands.describe(member='Member to look up (defaults to yourself)')
    async def raid_profile(self, interaction: discord.Interaction, member: discord.Member = None):
        guild_id = interaction.guild_id or 0
        settings = get_raid_settings(guild_id)
        if not settings or not settings.get('enabled'):
            await interaction.response.send_message('❌ The Raid System is disabled on this server.', ephemeral=True)
            return

        target   = member or interaction.user
        pts_row  = get_raid_user_points(guild_id, target.id)
        x_uname  = get_user_x_username(target.id)
        points   = pts_row['total_points']    if pts_row else 0
        raids_c  = pts_row['raids_completed'] if pts_row else 0

        name = 'Your' if target == interaction.user else f"{target.display_name}'s"
        embed = build_branded_embed(
            guild_id, title=f'{name} Raid Profile',
            cog_prefix='raid', use_thumbnail=False, use_image=False, use_footer=True,
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(name='Points',       value=f'`{points} pts`', inline=True)
        embed.add_field(name='Raids Joined', value=f'`{raids_c}`',    inline=True)
        embed.add_field(
            name='X Account',
            value=f'[@{x_uname}](https://x.com/{x_uname})' if x_uname else '`not set` — use `/setx`',
            inline=False,
        )
        await interaction.response.send_message(embed=embed)

    # ── /setx ─────────────────────────────────────────────────────────────────

    @app_commands.command(name='setx', description='Link your X (Twitter) username to your profile')
    @app_commands.describe(username='Your X username without the @ symbol')
    async def setx(self, interaction: discord.Interaction, username: str):
        username = username.lstrip('@').strip()
        now = datetime.now(timezone.utc)

        with get_connection() as conn:
            user = conn.execute(
                "SELECT x_username, x_username_set_at FROM users WHERE user_id=?",
                (interaction.user.id,),
            ).fetchone()

        if user and user['x_username'] and user['x_username_set_at']:
            set_at = datetime.fromisoformat(user['x_username_set_at'])
            if set_at.tzinfo is None:
                set_at = set_at.replace(tzinfo=timezone.utc)
            days_since = (now - set_at).days
            if days_since < 7:
                days_left = 7 - days_since
                await interaction.response.send_message(
                    f'⏳ You can change your X username in **{days_left} day{"s" if days_left != 1 else ""}**.',
                    ephemeral=True,
                )
                return

        with get_connection() as conn:
            conn.execute(
                "INSERT INTO users (user_id, username) VALUES (?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username",
                (interaction.user.id, str(interaction.user)),
            )
            conn.execute(
                "UPDATE users SET x_username=?, x_username_set_at=? WHERE user_id=?",
                (username, now.isoformat(), interaction.user.id),
            )

        await interaction.response.send_message(
            f'✅ X username set to **@{username}**.', ephemeral=True
        )

    # ── Target resolution for manual check ───────────────────────────────────

    async def _resolve_manual_check_target(self, guild_id: int, identifier: str) -> dict | None:
        raw = (identifier or '').strip()
        print(f'[raid] resolve_manual_check: guild={guild_id} identifier="{raw}"')
        if not raw:
            print('[raid] resolve_manual_check: empty identifier')
            return None

        guild = self.bot.get_guild(guild_id)
        if not guild:
            print(f'[raid] resolve_manual_check: guild {guild_id} not found in bot cache')
            return None

        # 1. All-numeric → Discord ID only
        if raw.isdigit():
            print(f'[raid] resolve_manual_check: numeric input — trying Discord member ID')
            member = guild.get_member(int(raw))
            if member:
                tw = get_user_x_username(member.id)
                print(f'[raid] resolve_manual_check: found member {member.name}, x_username={tw!r}')
                return {
                    'discord_user_id':  member.id,
                    'discord_username': member.name,
                    'twitter_username': tw,
                }
            print(f'[raid] resolve_manual_check: {raw} is not a member ID in this guild')
            return None

        cleaned = raw.lstrip('@').strip()

        # 2. DB lookup by x_username FIRST — works even when Twitter scraping is offline
        print(f'[raid] resolve_manual_check: trying DB x_username lookup for "{cleaned}"')
        db_row = find_user_by_x_username(cleaned)
        if db_row:
            member = guild.get_member(int(db_row['user_id']))
            uname  = member.name if member else db_row.get('username', '(not in guild)')
            print(f'[raid] resolve_manual_check: DB found — discord={uname} twitter={cleaned}')
            return {
                'discord_user_id':  int(db_row['user_id']),
                'discord_username': uname,
                'twitter_username': cleaned,
            }

        # 3. Twitter API lookup (only if DB had no match — confirms handle exists)
        print(f'[raid] resolve_manual_check: no DB linkage, trying Twitter API for "{cleaned}"')
        from cogs._twitter import lookup_twitter_user_by_login
        tw_user = await lookup_twitter_user_by_login(cleaned)
        if tw_user:
            # Twitter user exists but no /setx linkage — do NOT fall through to Discord search
            print(f'[raid] resolve_manual_check: @{tw_user["username"]} on Twitter but not linked via /setx')
            return None

        # 4. Discord username / display_name search — skip bots
        print(f'[raid] resolve_manual_check: trying Discord username search for "{cleaned}"')
        target_lower = cleaned.lower()
        for member in guild.members:
            if member.bot:
                continue
            if member.name.lower() == target_lower or (member.display_name or '').lower() == target_lower:
                tw = get_user_x_username(member.id)
                print(f'[raid] resolve_manual_check: found by Discord name: {member.name}, x_username={tw!r}')
                return {
                    'discord_user_id':  member.id,
                    'discord_username': member.name,
                    'twitter_username': tw,
                }

        print(f'[raid] resolve_manual_check: no match found for "{cleaned}"')
        return None

    # ── Manual check (called by API endpoint) ─────────────────────────────────

    async def manual_check(self, guild_id: int, raid_id: int, identifier: str) -> dict:
        from cogs._twitter import check_comment as tw_cc, check_retweet as tw_cr

        print(f'[raid] manual_check: guild={guild_id} raid_id={raid_id} identifier="{identifier}"')

        resolved = await self._resolve_manual_check_target(guild_id, identifier)
        if not resolved:
            return {
                'error': (
                    f'Could not resolve "{identifier}". Tried Discord username, Discord ID, '
                    'and Twitter handle. If using a Twitter handle, the user must have '
                    'linked their Discord account with /setx <twitter_handle> first.'
                )
            }

        discord_user_id  = resolved.get('discord_user_id')
        twitter_username = resolved.get('twitter_username')
        discord_username = resolved.get('discord_username', '(unknown)')

        if not discord_user_id:
            return {
                'error': (
                    f'Found Twitter user @{twitter_username} but they are not '
                    "linked to a Discord account in this server's database"
                )
            }

        # Resolve raid: display_number FIRST (what admin sees), PK as fallback
        raid = None
        with get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM raids WHERE guild_id=? AND display_number=? LIMIT 1",
                (guild_id, raid_id),
            ).fetchone()
            if row:
                raid = dict(row)
                print(f'[raid] manual_check: resolved by display_number={raid_id} → raid_id={raid["raid_id"]}')
        if not raid:
            raid = get_guild_raid(raid_id, guild_id)
            if raid:
                print(f'[raid] manual_check: resolved by PK={raid_id}')
        if not raid:
            with get_connection() as conn:
                all_ids = [r[0] for r in conn.execute(
                    "SELECT display_number FROM raids WHERE guild_id=? ORDER BY posted_at DESC LIMIT 10",
                    (guild_id,),
                ).fetchall()]
            print(f'[raid] manual_check: raid {raid_id} not found. Guild display_numbers: {all_ids}')
            return {'error': f'Raid #{raid_id} not found in this guild'}

        actual_raid_id = raid['raid_id']  # may differ from param if display_number fallback was used

        part = get_raid_participation(guild_id, actual_raid_id, discord_user_id)
        if not part:
            display = raid.get('display_number', actual_raid_id)
            return {'error': f'No participation found for {discord_username} in Raid #{display}'}

        if not twitter_username:
            twitter_username = get_user_x_username(discord_user_id)
        if not twitter_username:
            return {'error': f'{discord_username} has no X/Twitter username linked'}

        tweet_id = raid['tweet_id']

        # Parse which tasks this raid allows
        try:
            allowed_tasks = json.loads(raid.get('tasks_json') or '{}')
        except Exception:
            allowed_tasks = {'like': True, 'comment': True, 'retweet': True}

        # Parse what the user actually claimed — stored as {"like": true, "comment": false, ...}
        try:
            tc_raw = json.loads(part.get('tasks_claimed') or '{}')
            if isinstance(tc_raw, dict):
                tasks_claimed = {t for t, v in tc_raw.items() if v}
            else:
                tasks_claimed = set(tc_raw)
        except Exception:
            tasks_claimed = set()

        comment_result = None
        retweet_result = None

        if 'comment' in tasks_claimed and allowed_tasks.get('comment'):
            comment_result = await tw_cc(tweet_id, twitter_username)

        if 'retweet' in tasks_claimed and allowed_tasks.get('retweet'):
            retweet_result = await tw_cr(tweet_id, twitter_username)

        # Build like result — propagate inconclusive companions as None (not failure)
        like_result = None
        if 'like' in tasks_claimed and allowed_tasks.get('like'):
            companions = (tasks_claimed & {t for t, v in allowed_tasks.items() if v}) - {'like'}
            if not companions:
                like_result = {'verified': True, 'reason': 'like_always_pass'}
            else:
                any_companion_failed = (
                    ('comment' in companions and comment_result and comment_result.get('verified') is False) or
                    ('retweet' in companions and retweet_result and retweet_result.get('verified') is False)
                )
                any_companion_inconclusive = (
                    ('comment' in companions and (not comment_result or comment_result.get('verified') is None)) or
                    ('retweet' in companions and (not retweet_result or retweet_result.get('verified') is None))
                )
                if any_companion_failed:
                    like_result = {'verified': False, 'reason': 'like_companion_failed'}
                elif any_companion_inconclusive:
                    like_result = {'verified': None, 'reason': 'like_companion_inconclusive'}
                else:
                    like_result = {'verified': True, 'reason': 'like_companions_passed'}

        # Build unified results dict (claimed=True for all, so frontend knows what was attempted)
        results: dict = {}
        if comment_result is not None: results['comment'] = {'claimed': True, **comment_result}
        if retweet_result is not None: results['retweet'] = {'claimed': True, **retweet_result}
        if like_result    is not None: results['like']    = {'claimed': True, **like_result}

        flagged_tasks      = {t for t, r in results.items() if r.get('verified') is False}
        inconclusive_tasks = {t for t, r in results.items() if r.get('verified') is None}
        any_real_failure   = bool(flagged_tasks)
        any_inconclusive   = bool(inconclusive_tasks)

        # Write log for all checked tasks — 1=passed, 0=failed, -1=inconclusive
        for task, r in results.items():
            v    = r.get('verified')
            db_v = 1 if v is True else (0 if v is False else -1)
            add_raid_verification_log(
                guild_id, actual_raid_id, discord_user_id, task,
                True, db_v, 'manual',
                error_text=r.get('reason') if v is not True else None,
            )

        settings = get_raid_settings(guild_id)
        deducted = 0

        if any_real_failure:
            deducted = sum(
                round(raid['total_points'] * settings.get(f'point_ratio_{t}', 0) / 100)
                for t in flagged_tasks
            )
            with get_connection() as conn:
                conn.execute(
                    "UPDATE raid_participation SET verification_status='flagged', "
                    "flag_reason=?, points_earned=MAX(0, points_earned-?), "
                    "verified_at=datetime('now') WHERE participation_id=?",
                    (', '.join(sorted(flagged_tasks)), deducted, part['participation_id']),
                )
                if deducted > 0:
                    conn.execute(
                        "UPDATE raid_user_points SET total_points=MAX(0,total_points-?) "
                        "WHERE guild_id=? AND user_id=?", (deducted, guild_id, discord_user_id),
                    )
                    conn.execute(
                        "UPDATE users SET total_points=MAX(0,total_points-?) WHERE user_id=?",
                        (deducted, discord_user_id),
                    )
            print(f'[raid] manual_check FLAGGED {discord_username} tasks={sorted(flagged_tasks)} deducted={deducted}')
        elif not any_inconclusive:
            # All checks conclusive and passed — mark verified
            with get_connection() as conn:
                conn.execute(
                    "UPDATE raid_participation SET verification_status='verified', "
                    "verified_at=datetime('now') WHERE participation_id=?",
                    (part['participation_id'],),
                )
        else:
            print(f'[raid] manual_check inconclusive for {discord_username} — NOT flagged')

        from cogs._twitter import get_scraping_health as _tw_health
        health = _tw_health()

        return {
            'discord_username': discord_username,
            'twitter_username': twitter_username,
            'tasks':            results,
            'flagged':          sorted(flagged_tasks),
            'deducted':         deducted,
            'inconclusive':     any_inconclusive,
            'scraping_healthy': health['healthy'],
        }

    # ── Daily verification ─────────────────────────────────────────────────────

    @tasks.loop(time=dtime(hour=0, minute=0, tzinfo=timezone.utc))
    async def daily_verification_check(self):
        total = count_pending_participations_24h()
        if total == 0:
            print('[raid] daily check: no pending participations')
            return

        pct         = _adaptive_check_pct(total)
        sample_size = max(1, total * pct // 100)
        rows        = sample_pending_participations(sample_size)
        print(f'[raid] daily check: total={total} sampling={sample_size} ({pct}%)')

        flagged_count = inconclusive_count = verified_count = 0
        for row in rows:
            if row.get('guild_id') in LIVE_VERIFICATION_GUILD_IDS:
                print(f'[raid] daily check: skipping guild {row["guild_id"]} (live verification mode)')
                continue
            try:
                outcome = await self._verify_participation(row)
                if   outcome == 'flagged':      flagged_count     += 1
                elif outcome == 'inconclusive': inconclusive_count += 1
                elif outcome == 'verified':     verified_count    += 1
            except Exception as e:
                print(f'[raid] verify error: {type(e).__name__}: {e}')
                inconclusive_count += 1
            await asyncio.sleep(2)
        print(f'[raid] daily check done: flagged={flagged_count} verified={verified_count} inconclusive={inconclusive_count}')
        if inconclusive_count > 0 and flagged_count == 0 and verified_count == 0:
            print('[raid] daily check: all results inconclusive — Twitter verification may be offline, no flags issued')

    @daily_verification_check.before_loop
    async def before_daily_check(self):
        await self.bot.wait_until_ready()

    async def _verify_participation(self, row: dict) -> str:
        """Verify one participation row. Returns 'flagged', 'verified', 'inconclusive', or 'skipped'."""
        from cogs._twitter import check_comment as tw_cc, check_retweet as tw_cr

        guild_id = row['guild_id']

        settings = get_raid_settings(guild_id)
        if not settings or not settings.get('enabled'):
            return 'skipped'

        raid = get_guild_raid(row['raid_id'], guild_id)
        if not raid:
            return 'skipped'

        x_username = get_user_x_username(row['user_id'])
        if not x_username:
            return 'skipped'

        tweet_id = raid['tweet_id']

        try:
            allowed_tasks = json.loads(raid.get('tasks_json') or '{}')
        except Exception:
            allowed_tasks = {'like': True, 'comment': True, 'retweet': True}

        try:
            tc_raw = json.loads(row.get('tasks_claimed') or '{}')
            if isinstance(tc_raw, dict):
                tasks_claimed = {t for t, v in tc_raw.items() if v}
            else:
                tasks_claimed = set(tc_raw)
        except Exception:
            tasks_claimed = set()

        effective_tasks = tasks_claimed & {t for t, v in allowed_tasks.items() if v}
        if not effective_tasks:
            return 'skipped'

        comment_result = await tw_cc(tweet_id, x_username) if 'comment' in effective_tasks else None
        retweet_result = await tw_cr(tweet_id, x_username) if 'retweet' in effective_tasks else None

        # Build like result based on companion outcomes (None companions → inconclusive, not failure)
        like_result = None
        if 'like' in effective_tasks:
            companions = effective_tasks - {'like'}
            if not companions:
                like_result = {'verified': True, 'reason': 'like_only_trusted'}
            else:
                any_companion_failed = (
                    ('comment' in companions and comment_result and comment_result.get('verified') is False) or
                    ('retweet' in companions and retweet_result and retweet_result.get('verified') is False)
                )
                any_companion_inconclusive = (
                    ('comment' in companions and (not comment_result or comment_result.get('verified') is None)) or
                    ('retweet' in companions and (not retweet_result or retweet_result.get('verified') is None))
                )
                if any_companion_failed:
                    like_result = {'verified': False, 'reason': 'like_companion_failed'}
                elif any_companion_inconclusive:
                    like_result = {'verified': None, 'reason': 'like_companion_inconclusive'}
                else:
                    like_result = {'verified': True, 'reason': 'like_companions_passed'}

        task_results: dict = {}
        if comment_result is not None: task_results['comment'] = comment_result
        if retweet_result is not None: task_results['retweet'] = retweet_result
        if like_result    is not None: task_results['like']    = like_result

        flagged_tasks      = {t for t, r in task_results.items() if r.get('verified') is False}
        inconclusive_tasks = {t for t, r in task_results.items() if r.get('verified') is None}
        any_real_failure   = bool(flagged_tasks)
        any_inconclusive   = bool(inconclusive_tasks)

        # Write log for every checked task — 1=passed, 0=failed, -1=inconclusive
        for task, r in task_results.items():
            v    = r.get('verified')
            db_v = 1 if v is True else (0 if v is False else -1)
            add_raid_verification_log(
                guild_id, row['raid_id'], row['user_id'], task,
                True, db_v, 'auto',
                error_text=r.get('reason') if v is not True else None,
            )

        if any_real_failure:
            deduct      = sum(
                round(raid['total_points'] * settings.get(f'point_ratio_{t}', 0) / 100)
                for t in flagged_tasks
            )
            flag_reason = ', '.join(sorted(flagged_tasks))
            with get_connection() as conn:
                conn.execute(
                    "UPDATE raid_participation SET verification_status='flagged', "
                    "flag_reason=?, points_earned=MAX(0, points_earned-?), "
                    "verified_at=datetime('now') WHERE participation_id=?",
                    (flag_reason, deduct, row['participation_id']),
                )
                if deduct > 0:
                    conn.execute(
                        "UPDATE raid_user_points SET total_points=MAX(0,total_points-?) "
                        "WHERE guild_id=? AND user_id=?",
                        (deduct, guild_id, row['user_id']),
                    )
                    conn.execute(
                        "UPDATE users SET total_points=MAX(0,total_points-?) WHERE user_id=?",
                        (deduct, row['user_id']),
                    )
            print(f'[raid] FLAGGED user {row["user_id"]} tasks={flag_reason} deducted={deduct}pts')
            return 'flagged'

        if any_inconclusive:
            # Leave participation as 'pending' for future retry — do NOT mark verified
            print(f'[raid] inconclusive: user {row["user_id"]} in raid {row["raid_id"]} — NOT flagged, left pending')
            return 'inconclusive'

        with get_connection() as conn:
            conn.execute(
                "UPDATE raid_participation SET verification_status='verified', "
                "verified_at=datetime('now') WHERE participation_id=?",
                (row['participation_id'],),
            )
        print(f'[raid] verified: user {row["user_id"]} in raid {row["raid_id"]}')
        return 'verified'


async def setup(bot):
    await bot.add_cog(RaidsCog(bot))
    bot.add_dynamic_items(RaidJoinButton)
