"""
raidbot.py — Per-guild Raid module.

Flow:
  1. Admin posts a raid via /raid post (modal) or Dashboard API.
     The raid always posts to raid_channel_id from settings.
  2. Bot sends raid embed with 4 inline buttons: Like / Comment / Retweet / Confirm.
  3. User toggles tasks (each click shows ephemeral status), then confirms.
     Confirm records participation and awards points.
  4. Daily midnight UTC: adaptive sample of pending participations verified via twscrape.
  5. Flagged users have points deducted; verification log updated.
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
    get_raid_settings,
    upsert_raid_settings,
    create_guild_raid,
    get_guild_raid,
    update_guild_raid,
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
from cogs._branding import build_branded_embed
from cogs._twitter import extract_tweet_id

# ── Constants ─────────────────────────────────────────────────────────────────
MANUAL_CHECK_DAILY_LIMIT = 10

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
    "- Four action buttons below the embed\n\n"
    "**━━━━━━━━━━━━━━━━━━━━━━━**\n\n"
    "**3️⃣ Complete the tasks on X**\n\n"
    "Open the tweet on X and do the tasks you want to claim. Be genuine — write thoughtful comments, don't just spam.\n\n"
    "**━━━━━━━━━━━━━━━━━━━━━━━**\n\n"
    "**4️⃣ Claim your tasks**\n\n"
    "Back on Discord, under the raid embed:\n\n"
    "❤️ **Like** — click to toggle if you liked the tweet\n"
    "💬 **Comment** — click to toggle if you commented\n"
    "🔁 **Retweet** — click to toggle if you retweeted\n\n"
    "Each click shows your current selection privately. Nothing is recorded until you confirm.\n\n"
    "**━━━━━━━━━━━━━━━━━━━━━━━**\n\n"
    "**5️⃣ Confirm your submission**\n\n"
    "When you're ready, click ✅ **Confirm**. The bot records what you claimed and shows the points you earned. "
    "Your submission is final — you can only confirm once per raid.\n\n"
    "**━━━━━━━━━━━━━━━━━━━━━━━**\n\n"
    "**🔍 How we verify**\n\n"
    "A random sample of submissions is automatically checked against X every day. "
    "Admins can also manually verify any submission. "
    "If a task was claimed but not done, it gets flagged and points are deducted.\n\n"
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


def _calc_points(settings: dict, total_points: int, tasks_enabled: set,
                  tasks_claimed: set, mode: str) -> int:
    done = tasks_claimed & tasks_enabled
    if mode == 'all':
        return total_points if done >= tasks_enabled else 0
    like_pct    = settings.get('point_ratio_like', 12) / 100
    comment_pct = settings.get('point_ratio_comment', 40) / 100
    retweet_pct = settings.get('point_ratio_retweet', 48) / 100
    points = 0
    if 'like'    in done: points += round(total_points * like_pct)
    if 'comment' in done: points += round(total_points * comment_pct)
    if 'retweet' in done: points += round(total_points * retweet_pct)
    return points


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
        f'Toggle tasks below then click ✅ Confirm.{mode_note}'
    )
    embed = discord.Embed(description=description, color=color)

    if settings.get('embed_thumbnail_url'):
        embed.set_thumbnail(url=settings['embed_thumbnail_url'])
    if tweet.get('image'):
        embed.set_image(url=tweet['image'])

    footer = (settings.get('embed_footer_text') or 'AmeretaVerse • Raids')
    embed.set_footer(text=f'{footer}  |  Raid #{display_num:04d}')
    return embed


# ── Inline DynamicItem buttons ────────────────────────────────────────────────

class RaidTaskButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r'raid:btn:(?P<task>like|comment|retweet):(?P<rid>\d+)',
):
    _EMOJI  = {'like': '❤️', 'comment': '💬', 'retweet': '🔁'}
    _LABEL  = {'like': 'Like', 'comment': 'Comment', 'retweet': 'Retweet'}

    def __init__(self, task: str, raid_id: int):
        super().__init__(
            discord.ui.Button(
                style=discord.ButtonStyle.secondary,
                label=self._LABEL[task],
                emoji=self._EMOJI[task],
                custom_id=f'raid:btn:{task}:{raid_id}',
            )
        )
        self.task    = task
        self.raid_id = raid_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(match['task'], int(match['rid']))

    async def callback(self, interaction: discord.Interaction):
        try:
            guild_id = interaction.guild_id or 0
            user_id  = interaction.user.id

            raid = get_guild_raid(self.raid_id, guild_id)
            if not raid or raid['status'] != 'active':
                await interaction.response.send_message('❌ This raid is no longer active.', ephemeral=True)
                return

            x_username = get_user_x_username(user_id)
            if not x_username:
                await interaction.response.send_message(
                    '⚠️ Link your X account first with `/setx <username>` before joining a raid.',
                    ephemeral=True,
                )
                return

            if get_raid_participation(guild_id, self.raid_id, user_id):
                await interaction.response.send_message(
                    '✅ You already submitted this raid. Each raid can only be confirmed once.',
                    ephemeral=True,
                )
                return

            state = _get_panel_state(user_id, self.raid_id)
            state[self.task] = not state[self.task]
            state['updated_at'] = datetime.utcnow()

            status_lines = [
                f"{'✅' if state['like'] else '⬜'} ❤️ Like",
                f"{'✅' if state['comment'] else '⬜'} 💬 Comment",
                f"{'✅' if state['retweet'] else '⬜'} 🔁 Retweet",
            ]
            embed = build_branded_embed(
                guild_id,
                title='Your task selection',
                description='\n'.join(status_lines) + '\n\nClick ✅ **Confirm** when ready to submit.',
                cog_prefix='raid',
                use_thumbnail=True, use_image=False, use_footer=True,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)

        except Exception as e:
            print(f'[raid] task button error: {type(e).__name__}: {e}')
            traceback.print_exc()
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message('An error occurred.', ephemeral=True)
            except Exception:
                pass


class RaidConfirmButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r'raid:btn:confirm:(?P<rid>\d+)',
):
    def __init__(self, raid_id: int):
        super().__init__(
            discord.ui.Button(
                style=discord.ButtonStyle.success,
                label='Confirm',
                emoji='✅',
                custom_id=f'raid:btn:confirm:{raid_id}',
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

            raid = get_guild_raid(self.raid_id, guild_id)
            if not raid or raid['status'] != 'active':
                await interaction.response.send_message('❌ This raid is no longer active.', ephemeral=True)
                return

            x_username = get_user_x_username(user_id)
            if not x_username:
                await interaction.response.send_message(
                    '⚠️ Link your X account first with `/setx <username>` before joining a raid.',
                    ephemeral=True,
                )
                return

            if get_raid_participation(guild_id, self.raid_id, user_id):
                await interaction.response.send_message(
                    '✅ You already submitted this raid.', ephemeral=True,
                )
                return

            state = _get_panel_state(user_id, self.raid_id)
            if not any([state['like'], state['comment'], state['retweet']]):
                await interaction.response.send_message(
                    '⚠️ Select at least one task before confirming.', ephemeral=True,
                )
                return

            # Intersect with tasks enabled for this raid
            allowed = {}
            try:
                allowed = json.loads(raid.get('tasks_json') or '{}')
            except Exception:
                pass
            claimed = {
                'like':    state['like']    and bool(allowed.get('like', True)),
                'comment': state['comment'] and bool(allowed.get('comment', True)),
                'retweet': state['retweet'] and bool(allowed.get('retweet', True)),
            }

            if raid.get('mode') == 'all':
                enabled_tasks = {t for t, v in allowed.items() if v}
                claimed_tasks = {t for t, v in claimed.items() if v}
                if claimed_tasks < enabled_tasks:
                    missing = enabled_tasks - claimed_tasks
                    await interaction.response.send_message(
                        f'❌ All tasks required. Also select: {", ".join(sorted(missing))}',
                        ephemeral=True,
                    )
                    return

            settings    = get_raid_settings(guild_id)
            r_like      = settings.get('point_ratio_like',    12)
            r_comment   = settings.get('point_ratio_comment', 40)
            r_retweet   = settings.get('point_ratio_retweet', 48)
            total       = int(raid['total_points'])

            earned  = 0
            lines   = []
            if claimed['like']:
                pts = total * r_like // 100
                earned += pts
                lines.append(f'❤️ Like — {pts} pts')
            if claimed['comment']:
                pts = total * r_comment // 100
                earned += pts
                lines.append(f'💬 Comment — {pts} pts')
            if claimed['retweet']:
                pts = total * r_retweet // 100
                earned += pts
                lines.append(f'🔁 Retweet — {pts} pts')

            # Persist
            with get_connection() as conn:
                conn.execute(
                    "INSERT INTO users (user_id, username) VALUES (?,?) "
                    "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username",
                    (user_id, str(interaction.user)),
                )
            tasks_json = json.dumps(claimed)
            create_raid_participation(guild_id, self.raid_id, user_id, tasks_json, earned)
            upsert_raid_user_points(guild_id, user_id, earned, delta_raids=1)
            with get_connection() as conn:
                conn.execute(
                    "UPDATE users SET total_points=total_points+? WHERE user_id=?",
                    (earned, user_id),
                )

            _clear_panel_state(user_id, self.raid_id)

            display_num = raid.get('display_number') or self.raid_id
            result_embed = build_branded_embed(
                guild_id,
                title=f'✅ Submitted — {earned} pts earned',
                description=(
                    '**Tasks claimed:**\n' + '\n'.join(lines) +
                    f'\n\nRaid #{display_num:04d} — '
                    '_A random sample of submissions is auto-verified daily._'
                ),
                cog_prefix='raid',
                use_thumbnail=True, use_image=False, use_footer=True,
            )
            await interaction.response.send_message(embed=result_embed, ephemeral=True)

        except Exception as e:
            print(f'[raid] confirm error: {type(e).__name__}: {e}')
            traceback.print_exc()
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message('An error occurred.', ephemeral=True)
            except Exception:
                pass


def build_raid_panel_view(raid_id: int) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    v.add_item(RaidTaskButton('like',    raid_id))
    v.add_item(RaidTaskButton('comment', raid_id))
    v.add_item(RaidTaskButton('retweet', raid_id))
    v.add_item(RaidConfirmButton(raid_id))
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
        rows     = get_raid_leaderboard(guild_id, limit=10)

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
        from cogs._twitter import lookup_twitter_user_by_login, lookup_twitter_user_by_id

        raw = (identifier or '').strip()
        if not raw:
            return None

        guild = self.bot.get_guild(guild_id)
        if not guild:
            return None

        def _make(discord_user_id, discord_username, twitter_username):
            return {
                'discord_user_id': discord_user_id,
                'discord_username': discord_username,
                'twitter_username': twitter_username,
            }

        def _enrich_twitter(tw_uname: str) -> dict:
            db_row = find_user_by_x_username(tw_uname)
            if db_row:
                m = guild.get_member(db_row['user_id'])
                uname = m.name if m else db_row.get('username', '(unknown)')
                return _make(db_row['user_id'], uname, tw_uname)
            return _make(None, '(not linked to Discord)', tw_uname)

        if raw.isdigit():
            member = guild.get_member(int(raw))
            if member:
                return _make(member.id, member.name, get_user_x_username(member.id))
            tw_user = await lookup_twitter_user_by_id(raw)
            if tw_user:
                return _enrich_twitter(tw_user['username'])
            return None

        cleaned = raw.lstrip('@')
        tw_user = await lookup_twitter_user_by_login(cleaned)
        if tw_user:
            return _enrich_twitter(tw_user['username'])

        lc = cleaned.lower()
        for member in guild.members:
            if member.name.lower() == lc or member.display_name.lower() == lc:
                return _make(member.id, member.name, get_user_x_username(member.id))

        return None

    # ── Manual check (called by API endpoint) ─────────────────────────────────

    async def manual_check(self, guild_id: int, raid_id: int, identifier: str) -> dict:
        from cogs._twitter import check_comment as tw_cc, check_retweet as tw_cr

        resolved = await self._resolve_manual_check_target(guild_id, identifier)
        if not resolved:
            return {
                'error': (
                    f'Could not resolve "{identifier}" — '
                    'try Discord username, Discord ID, @twitter_handle, or Twitter user ID'
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

        raid = get_guild_raid(raid_id, guild_id)
        if not raid:
            return {'error': 'Raid not found'}

        part = get_raid_participation(guild_id, raid_id, discord_user_id)
        if not part:
            return {'error': f'No participation found for {discord_username} in Raid #{raid_id}'}

        if not twitter_username:
            twitter_username = get_user_x_username(discord_user_id)
        if not twitter_username:
            return {'error': f'{discord_username} has no X/Twitter username linked'}

        tweet_id      = raid['tweet_id']
        tasks_claimed = set(json.loads(part.get('tasks_claimed') or '[]'))

        results        = {}
        comment_result = None
        retweet_result = None

        if 'comment' in tasks_claimed:
            comment_result = await tw_cc(tweet_id, twitter_username)
            results['comment'] = {'claimed': True, **comment_result}

        if 'retweet' in tasks_claimed:
            retweet_result = await tw_cr(tweet_id, twitter_username)
            results['retweet'] = {'claimed': True, **retweet_result}

        if 'like' in tasks_claimed:
            companions = tasks_claimed - {'like'}
            if not companions:
                results['like'] = {'claimed': True, 'verified': True, 'reason': 'like_always_pass'}
            else:
                companions_ok = all(
                    (comment_result['verified'] is not False if c == 'comment' and comment_result else True)
                    and
                    (retweet_result['verified'] is not False if c == 'retweet' and retweet_result else True)
                    for c in companions
                )
                results['like'] = {
                    'claimed': True,
                    'verified': companions_ok,
                    'reason': 'companions_passed' if companions_ok else 'companions_failed',
                }

        settings      = get_raid_settings(guild_id)
        flagged_tasks = {t for t, r in results.items() if r.get('verified') is False}

        if flagged_tasks:
            deduct = sum(
                round(raid['total_points'] * settings.get(f'point_ratio_{task}', 0) / 100)
                for task in flagged_tasks
            )
            with get_connection() as conn:
                conn.execute(
                    "UPDATE raid_participation SET verification_status='flagged', "
                    "flag_reason=?, points_earned=MAX(0, points_earned-?), "
                    "verified_at=datetime('now') WHERE participation_id=?",
                    (', '.join(sorted(flagged_tasks)), deduct, part['participation_id']),
                )
                if deduct > 0:
                    conn.execute(
                        "UPDATE raid_user_points SET total_points=MAX(0,total_points-?) "
                        "WHERE guild_id=? AND user_id=?", (deduct, guild_id, discord_user_id),
                    )
                    conn.execute(
                        "UPDATE users SET total_points=MAX(0,total_points-?) WHERE user_id=?",
                        (deduct, discord_user_id),
                    )
            for task in flagged_tasks:
                add_raid_verification_log(
                    guild_id, raid_id, discord_user_id, task, True, False, 'manual',
                    error_text='manual_check',
                )
        else:
            with get_connection() as conn:
                conn.execute(
                    "UPDATE raid_participation SET verification_status='verified', "
                    "verified_at=datetime('now') WHERE participation_id=?",
                    (part['participation_id'],),
                )
            for task in tasks_claimed:
                add_raid_verification_log(
                    guild_id, raid_id, discord_user_id, task, True, True, 'manual',
                )

        deducted = sum(
            round(raid['total_points'] * settings.get(f'point_ratio_{t}', 0) / 100)
            for t in flagged_tasks
        ) if flagged_tasks else 0

        return {
            'discord_username': discord_username,
            'twitter_username': twitter_username,
            'tasks': results,
            'flagged': sorted(flagged_tasks),
            'deducted': deducted,
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

        for row in rows:
            try:
                await self._verify_participation(row)
            except Exception as e:
                print(f'[raid] verify error: {type(e).__name__}: {e}')
            await asyncio.sleep(2)

    @daily_verification_check.before_loop
    async def before_daily_check(self):
        await self.bot.wait_until_ready()

    async def _verify_participation(self, row: dict):
        from cogs._twitter import check_comment as tw_cc, check_retweet as tw_cr

        guild_id = row['guild_id']
        raid     = get_guild_raid(row['raid_id'], guild_id)
        if not raid:
            return

        x_username = get_user_x_username(row['user_id'])
        if not x_username:
            return

        tweet_id      = raid['tweet_id']
        tasks_claimed = set(json.loads(row.get('tasks_claimed') or '[]'))

        comment_result = await tw_cc(tweet_id, x_username) if 'comment' in tasks_claimed else None
        retweet_result = await tw_cr(tweet_id, x_username) if 'retweet' in tasks_claimed else None

        flagged_tasks = set()

        if comment_result and comment_result['verified'] is False:
            flagged_tasks.add('comment')
        if retweet_result and retweet_result['verified'] is False:
            flagged_tasks.add('retweet')

        if 'like' in tasks_claimed:
            companions = tasks_claimed - {'like'}
            if companions:
                any_companion_failed = (
                    ('comment' in companions and comment_result and comment_result['verified'] is False) or
                    ('retweet' in companions and retweet_result and retweet_result['verified'] is False)
                )
                if any_companion_failed:
                    flagged_tasks.add('like')

        if flagged_tasks:
            settings    = get_raid_settings(guild_id)
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

            for task in flagged_tasks:
                add_raid_verification_log(
                    guild_id, row['raid_id'], row['user_id'],
                    task, True, False, 'auto', error_text=flag_reason,
                )
            print(f'[raid] flagged user {row["user_id"]} tasks={flag_reason} deducted={deduct}pts')
        else:
            with get_connection() as conn:
                conn.execute(
                    "UPDATE raid_participation SET verification_status='verified', "
                    "verified_at=datetime('now') WHERE participation_id=?",
                    (row['participation_id'],),
                )
            for task in tasks_claimed:
                add_raid_verification_log(
                    guild_id, row['raid_id'], row['user_id'], task, True, True, 'auto',
                )


async def setup(bot):
    await bot.add_cog(RaidsCog(bot))
    bot.add_dynamic_items(RaidTaskButton, RaidConfirmButton)
