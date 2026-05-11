"""
raidbot.py — Per-guild Raid module.

Flow:
  1. Admin posts a raid via /raid post (modal) or Dashboard API.
  2. Bot sends raid embed with 'My Panel' button to the configured channel.
  3. User clicks My Panel → ephemeral toggle panel → Confirm → participation recorded.
  4. Daily midnight UTC: adaptive sample of pending participations verified via twscrape.
  5. Flagged users have points deducted; verification log updated.
"""

import json
import asyncio
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
    list_guild_raids,
)
from cogs._utils import resolve_channel, resolve_role
from cogs._branding import build_branded_embed
from cogs._twitter import extract_tweet_id

DEFAULT_GUIDE_MESSAGE = (
    "🎯 **AmeretaVerse Raid System Guide**\n\n"
    "Welcome! This bot organizes community raids on social media. Here's how it works:\n\n"
    "**1. Link your X account**\n"
    "Use `/setx <your_username>` to link your X (Twitter) account. You only need to do this once.\n\n"
    "**2. Wait for a raid**\n"
    "When a raid drops, you'll see an embed with the tweet to raid and the tasks (Like, Comment, Retweet).\n\n"
    "**3. Open your panel**\n"
    "Click \"My Panel\" on the raid embed.\n\n"
    "**4. Claim tasks you completed**\n"
    "Toggle the tasks you actually did, then click Confirm. "
    "You'll earn points based on what you completed.\n\n"
    "**5. Stay honest!**\n"
    "A random sample of raids is auto-verified daily. Admins can also manually check any user. "
    "Cheating leads to flags and may result in bans.\n\n"
    "That's it! Happy raiding 🚀"
)

# In-memory panel state: (raid_id, user_id) -> set[str]
_panel_states: dict = {}


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
        f'Click **My Panel** to claim tasks.{mode_note}'
    )
    embed = discord.Embed(description=description, color=color)

    if settings.get('embed_thumbnail_url'):
        embed.set_thumbnail(url=settings['embed_thumbnail_url'])

    if tweet.get('image'):
        embed.set_image(url=tweet['image'])

    footer = (settings.get('embed_footer_text') or 'AmeretaVerse • Raids')
    embed.set_footer(text=f'{footer}  |  Raid #{display_num:04d}')
    return embed


def _build_status_embed(raid_id: int, user_id: int, tasks_enabled: set) -> discord.Embed:
    state = _panel_states.get((raid_id, user_id), set())
    lines = []
    for t in ['like', 'comment', 'retweet']:
        if t in tasks_enabled:
            icon = '✅' if t in state else '⬜'
            lines.append(f'{icon} {t.capitalize()}')
    embed = discord.Embed(
        title='Your Task Selection',
        description='\n'.join(lines) or 'No tasks available.',
        color=0x94730D,
    )
    embed.set_footer(text='Toggle tasks then click Confirm ✅')
    return embed


# ── RaidPersonalPanel ─────────────────────────────────────────────────────────

class RaidPersonalPanel(discord.ui.View):
    def __init__(self, raid_id: int, user_id: int, guild_id: int,
                 raid: dict, settings: dict):
        super().__init__(timeout=600)
        self.raid_id       = raid_id
        self.user_id       = user_id
        self.guild_id      = guild_id
        self.raid          = raid
        self.settings      = settings
        try:
            tasks_obj = json.loads(raid.get('tasks_json') or '{}')
            self.tasks_enabled = {t for t, v in tasks_obj.items() if v}
        except Exception:
            self.tasks_enabled = {'like', 'comment', 'retweet'}
        self._rebuild()

    def _rebuild(self):
        self.clear_items()
        state = _panel_states.get((self.raid_id, self.user_id), set())
        for task, label in [('like', '👍 Like'), ('comment', '💬 Comment'), ('retweet', '🔁 Retweet')]:
            active  = task in state
            in_raid = task in self.tasks_enabled
            btn = discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.success if active else discord.ButtonStyle.secondary,
                disabled=not in_raid,
                row=0,
            )
            btn.callback = self._make_toggle(task)
            self.add_item(btn)

        confirm_btn = discord.ui.Button(
            label='Confirm', style=discord.ButtonStyle.success, emoji='✅', row=0,
        )
        confirm_btn.callback = self._on_confirm
        self.add_item(confirm_btn)

    def _make_toggle(self, task: str):
        async def _cb(interaction: discord.Interaction):
            if interaction.user.id != self.user_id:
                await interaction.response.send_message('❌ This is not your panel.', ephemeral=True)
                return
            state = _panel_states.setdefault((self.raid_id, self.user_id), set())
            if task in state:
                state.discard(task)
            else:
                state.add(task)
            self._rebuild()
            await interaction.response.edit_message(
                embed=_build_status_embed(self.raid_id, self.user_id, self.tasks_enabled),
                view=self,
            )
        return _cb

    async def _on_confirm(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message('❌ This is not your panel.', ephemeral=True)
            return

        guild_id = interaction.guild_id or self.guild_id

        # Re-check raid active
        raid = get_guild_raid(self.raid_id, guild_id)
        if not raid or raid['status'] != 'active':
            await interaction.response.edit_message(content='❌ This raid is no longer active.', embed=None, view=None)
            return

        # Re-check no existing participation
        if get_raid_participation(guild_id, self.raid_id, self.user_id):
            await interaction.response.edit_message(content='⚠️ You already submitted for this raid.', embed=None, view=None)
            return

        state = _panel_states.get((self.raid_id, self.user_id), set())
        valid_state = state & self.tasks_enabled
        if not valid_state:
            await interaction.response.edit_message(content='⚠️ Select at least one task first.', embed=None, view=None)
            return

        if raid['mode'] == 'all':
            missing = self.tasks_enabled - valid_state
            if missing:
                await interaction.response.edit_message(
                    content=f'❌ All tasks required. Missing: {", ".join(sorted(missing))}',
                    embed=None, view=None,
                )
                return

        settings = get_raid_settings(guild_id)
        points = _calc_points(settings, raid['total_points'], self.tasks_enabled, valid_state, raid['mode'])

        # Upsert global user record
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO users (user_id, username) VALUES (?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username",
                (self.user_id, str(interaction.user)),
            )

        tasks_json = json.dumps(sorted(valid_state))
        create_raid_participation(guild_id, self.raid_id, self.user_id, tasks_json, points)
        upsert_raid_user_points(guild_id, self.user_id, points, delta_raids=1)

        # Keep global users.total_points for backwards compat
        with get_connection() as conn:
            conn.execute(
                "UPDATE users SET total_points=total_points+? WHERE user_id=?",
                (points, self.user_id),
            )

        _panel_states.pop((self.raid_id, self.user_id), None)

        display_num = raid.get('display_number') or self.raid_id
        embed = discord.Embed(
            title='✅ Raid Confirmed!',
            description=(
                f'**Tasks:** {", ".join(t.capitalize() for t in sorted(valid_state))}\n'
                f'**Points earned:** +{points} pts 🔥\n'
                f'Raid #{display_num:04d}'
            ),
            color=0x3ba55c,
        )
        embed.set_footer(text='AmeretaVerse • Raids')
        await interaction.response.edit_message(content=None, embed=embed, view=None)


# ── RaidPanelButton — persistent DynamicItem ──────────────────────────────────

class RaidPanelButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r'raid:panel:(?P<rid>\d+)',
):
    def __init__(self, raid_id: int):
        super().__init__(
            discord.ui.Button(
                label='My Panel',
                style=discord.ButtonStyle.primary,
                emoji='⚔️',
                custom_id=f'raid:panel:{raid_id}',
            )
        )
        self.raid_id = raid_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match['rid']))

    async def callback(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id or 0
        user     = interaction.user

        raid = get_guild_raid(self.raid_id, guild_id)
        if not raid or raid['status'] != 'active':
            await interaction.response.send_message('❌ This raid is no longer active.', ephemeral=True)
            return

        x_username = get_user_x_username(user.id)
        if not x_username:
            await interaction.response.send_message(
                '⚠️ Link your X account first: `/setx your_username`', ephemeral=True,
            )
            return

        if get_raid_participation(guild_id, self.raid_id, user.id):
            await interaction.response.send_message('⚠️ You already submitted for this raid.', ephemeral=True)
            return

        settings = get_raid_settings(guild_id)
        view = RaidPersonalPanel(self.raid_id, user.id, guild_id, raid, settings)
        await interaction.response.send_message(
            embed=_build_status_embed(self.raid_id, user.id, view.tasks_enabled),
            view=view,
            ephemeral=True,
        )


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

        # Validate
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

        mode_val = self.mode.value.strip().lower()
        if mode_val not in ('all', 'partial'):
            mode_val = 'partial'

        raw_tasks = [t.strip().lower() for t in self.tasks.value.split(',') if t.strip()]
        valid_tasks = {'like', 'comment', 'retweet'}
        task_list = [t for t in raw_tasks if t in valid_tasks] or ['like', 'comment', 'retweet']
        tasks_obj = {t: (t in task_list) for t in ['like', 'comment', 'retweet']}
        tasks_json = json.dumps(tasks_obj)

        await interaction.response.defer(ephemeral=True)

        tweet_data = _fetch_tweet(tweet_url_val)

        raid_id = create_guild_raid(
            guild_id, tweet_url_val, tweet_id_val, pts, mode_val, tasks_json,
            interaction.user.id,
        )
        raid = get_guild_raid(raid_id, guild_id)

        # Determine channel
        channel = None
        ch_id = (settings.get('guide_channel_id') or '').strip()
        # Actually post to configured raid channel (ping_role_id field is for ping, not channel)
        # Use guide_channel_id as fallback — in practice admin sets a channel via Dashboard
        # For /raid post, we post to current channel if not configured
        guild = interaction.guild
        if not channel:
            channel = interaction.channel

        embed = _build_raid_embed(guild_id, raid, tweet_data, settings)
        view  = discord.ui.View(timeout=None)
        view.add_item(RaidPanelButton(raid_id))

        # Ping role
        ping_raw  = (settings.get('ping_role_id') or '').strip()
        ping_role = resolve_role(guild, ping_raw) if ping_raw else None
        content   = f'{ping_role.mention} — new raid just dropped! ⚔️' if ping_role else '⚔️ New raid just dropped!'

        msg = await channel.send(content=content, embed=embed, view=view,
                                  allowed_mentions=discord.AllowedMentions(roles=True))
        self._bot.add_dynamic_items(RaidPanelButton)

        update_guild_raid(raid_id, guild_id,
                          channel_id=str(channel.id), message_id=str(msg.id))

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

        # Permission check
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

    # ── Manual check (called by API endpoint) ─────────────────────────────────

    async def manual_check(self, guild_id: int, raid_id: int, user_id: int) -> dict:
        """Verify a single participation. Returns per-task results dict."""
        from cogs._twitter import check_comment as tw_check_comment
        from cogs._twitter import check_retweet as tw_check_retweet

        raid = get_guild_raid(raid_id, guild_id)
        if not raid:
            return {'error': 'Raid not found'}

        part = get_raid_participation(guild_id, raid_id, user_id)
        if not part:
            return {'error': 'No participation found for this user'}

        x_username = get_user_x_username(user_id)
        if not x_username:
            return {'error': 'User has no X username set'}

        tweet_id     = raid['tweet_id']
        tasks_claimed = set(json.loads(part.get('tasks_claimed') or '[]'))

        results = {}
        comment_result  = None
        retweet_result  = None

        if 'comment' in tasks_claimed:
            comment_result = await tw_check_comment(tweet_id, x_username)
            results['comment'] = {'claimed': True, **comment_result}

        if 'retweet' in tasks_claimed:
            retweet_result = await tw_check_retweet(tweet_id, x_username)
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

        # Log and apply deductions
        settings = get_raid_settings(guild_id)
        flagged_tasks = {t for t, r in results.items() if r.get('verified') is False}

        if flagged_tasks:
            deduct = 0
            for task in flagged_tasks:
                pct = settings.get(f'point_ratio_{task}', 0) / 100
                deduct += round(raid['total_points'] * pct)

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
                        "WHERE guild_id=? AND user_id=?", (deduct, guild_id, user_id),
                    )
                    conn.execute(
                        "UPDATE users SET total_points=MAX(0,total_points-?) WHERE user_id=?",
                        (deduct, user_id),
                    )

            for task in flagged_tasks:
                add_raid_verification_log(
                    guild_id, raid_id, user_id, task, True, False, 'manual',
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
                    guild_id, raid_id, user_id, task, True, True, 'manual',
                )

        return {'x_username': x_username, 'tasks': results,
                'flagged': sorted(flagged_tasks), 'deducted': 0 if not flagged_tasks else
                sum(round(raid['total_points'] * settings.get(f'point_ratio_{t}', 0) / 100) for t in flagged_tasks)}

    # ── Daily verification ─────────────────────────────────────────────────────

    @tasks.loop(time=dtime(hour=0, minute=0, tzinfo=timezone.utc))
    async def daily_verification_check(self):
        total = count_pending_participations_24h()
        if total == 0:
            print('[raid] daily check: no pending participations')
            return

        if total < 500:      pct = 25
        elif total < 2000:   pct = 15
        elif total < 5000:   pct = 8
        else:                pct = 5

        sample_size = max(1, total * pct // 100)
        rows = sample_pending_participations(sample_size)
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
        from cogs._twitter import check_comment as tw_cc
        from cogs._twitter import check_retweet as tw_cr

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
            settings = get_raid_settings(guild_id)
            deduct   = sum(
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
    bot.add_dynamic_items(RaidPanelButton)
