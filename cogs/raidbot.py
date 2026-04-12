import csv
import discord
from discord import app_commands
from discord.ext import commands, tasks
import io
import json
import random
import re
import requests
from datetime import datetime, timezone, timedelta, time as dtime
from database import get_connection

LOGO_URL = "https://i.imgur.com/FNE8Li0.png"
PROGRESS_BAR_URL = "https://i.imgur.com/5Mg2BIE.png"
RAID_CHANNEL_NAME = "Raid"
RAID_ROLE_NAME = "Raiders"

# Percentage of total_points each task is worth
TASK_WEIGHTS = {"like": 12.5, "comment": 40.0, "retweet": 47.5}

# In-memory per-user toggle state: {(raid_id, user_id): set[str]}
user_states: dict = {}


# ── helpers ───────────────────────────────────────────────────────────────────

def toggle_task(raid_id: int, user_id: int, task: str) -> set:
    key = (raid_id, user_id)
    s = user_states.setdefault(key, set())
    s.discard(task) if task in s else s.add(task)
    return s


def get_state(raid_id: int, user_id: int) -> set:
    return user_states.get((raid_id, user_id), set())


def calc_points(total: int, raid_tasks: list, done: set, mode: str) -> int:
    """
    Redistribute weights proportionally among the raid's tasks.
    mode=all:     user must complete every task → full points or 0
    mode=partial: points proportional to completed task weights
    """
    rd = {t.lower() for t in raid_tasks}
    cd = {t.lower() for t in done} & rd
    if mode == "all":
        return total if cd == rd else 0
    raid_w = sum(TASK_WEIGHTS.get(t, 0) for t in rd)
    if not raid_w:
        return 0
    done_w = sum(TASK_WEIGHTS.get(t, 0) for t in cd)
    return round(total * done_w / raid_w)


def upsert_user(user_id: int, username: str):
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO users (user_id, username) VALUES (?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username",
            (user_id, username),
        )


def get_raid(raid_id: int):
    with get_connection() as conn:
        return conn.execute("SELECT * FROM raids WHERE raid_id=?", (raid_id,)).fetchone()


def extract_tweet_id(url: str) -> str | None:
    """Extract the numeric tweet ID from an x.com or twitter.com URL."""
    match = re.search(r"/status/(\d+)", url)
    return match.group(1) if match else None


def fetch_tweet(url: str) -> dict:
    """
    Fetch tweet data via the fxtwitter API.
    Returns: {author, text, image, is_video} — all fields may be None/False on failure.
    """
    result = {"author": None, "text": None, "image": None, "is_video": False}
    tweet_id = extract_tweet_id(url)
    if not tweet_id:
        return result
    try:
        resp = requests.get(
            f"https://api.fxtwitter.com/status/{tweet_id}",
            timeout=10,
        )
        if resp.status_code != 200:
            return result
        data = resp.json()
        tweet = data.get("tweet", {})
        result["text"]   = tweet.get("text")
        result["author"] = tweet.get("author", {}).get("name")
        media = tweet.get("media", {})
        photos = media.get("photos", [])
        videos = media.get("videos", [])
        if videos:
            result["image"]    = videos[0].get("thumbnail_url")
            result["is_video"] = True
        elif photos:
            result["image"] = photos[0].get("url")
    except Exception:
        pass
    return result


def build_raid_embed(raid, tweet: dict = None) -> discord.Embed:
    tasks = json.loads(raid["tasks"]) if raid["tasks"] else []
    task_str = " • ".join(t.capitalize() for t in tasks) or "None"
    mode = raid["mode"]
    tweet = tweet or {}
    tweet_url = raid["tweet_link"]

    # Tweet blockquote section
    tweet_lines = [f"## **[📌 New Tweet]({tweet_url})**"]
    author = tweet.get("author")
    text   = tweet.get("text") or ""
    if len(text) > 280:
        text = text[:280] + "... show more"
    if tweet.get("is_video") and text:
        text = f"▶ Video\n{text}"
    if author or text:
        tweet_lines.append("")  # blank line before blockquote
        if author:
            tweet_lines.append(f"> **{author}**")
        for line in text.splitlines():
            tweet_lines.append(f"> {line}")

    mode_note = (
        "\n⚠️ **All tasks must be completed** to earn points."
        if mode == "all"
        else "\n💡 Earn points for each task completed."
    )

    description = (
        "\n".join(tweet_lines) + "\n\n"
        f"**Tasks:** {task_str}\n"
        f"**Points:** {raid['total_points']} pts  •  **Mode:** {mode}\n\n"
        f"Select the tasks you completed, then hit **Confirm** ✅{mode_note}"
    )

    embed = discord.Embed(description=description, color=0x94730D)
    embed.set_thumbnail(url=LOGO_URL)
    embed.set_image(url=tweet["image"] if tweet.get("image") else PROGRESS_BAR_URL)
    embed.set_footer(text=f"AmeretaVerse • Raids  |  Raid #{raid['raid_id']}")
    return embed


def build_status_embed(raid_id: int, user_id: int, raid_tasks: list) -> discord.Embed:
    state = get_state(raid_id, user_id)
    lines = []
    for t in ["like", "comment", "retweet"]:
        if t in {x.lower() for x in raid_tasks}:
            icon = "✅" if t in state else "⬜"
            lines.append(f"{icon} {t.capitalize()}")
    embed = discord.Embed(
        title="Your Task Selection",
        description="\n".join(lines) or "No tasks available.",
        color=0x94730D,
    )
    embed.set_footer(text="AmeretaVerse • Raids — toggle tasks, then Confirm")
    return embed


# ── ephemeral personal panel ──────────────────────────────────────────────────

class UserTaskView(discord.ui.View):
    """
    Sent ephemerally to a specific user.
    Task buttons toggle on/off (green/blue) via edit_message.
    """

    def __init__(self, raid_id: int, user_id: int, raid_tasks: list, mode: str, total_points: int):
        super().__init__(timeout=600)
        self.raid_id = raid_id
        self.user_id = user_id
        self.raid_tasks = [t.lower() for t in raid_tasks]
        self.mode = mode
        self.total_points = total_points
        self._rebuild()

    def _rebuild(self):
        self.clear_items()
        state = get_state(self.raid_id, self.user_id)
        TASK_META = [
            ("like",    "👍 Like"),
            ("comment", "💬 Comment"),
            ("retweet", "🔁 Retweet"),
        ]
        for task, label in TASK_META:
            active = task in state
            in_raid = task in self.raid_tasks
            btn = discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.success if active else discord.ButtonStyle.primary,
                disabled=not in_raid,
                row=0,
            )
            btn.callback = self._make_toggle(task)
            self.add_item(btn)

        confirm = discord.ui.Button(
            label="Confirm",
            style=discord.ButtonStyle.success,
            emoji="✅",
            row=0,
        )
        confirm.callback = self._confirm
        self.add_item(confirm)

    def _make_toggle(self, task: str):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.user_id:
                await interaction.response.send_message("❌ This is not your panel.", ephemeral=True)
                return
            toggle_task(self.raid_id, self.user_id, task)
            self._rebuild()
            await interaction.response.edit_message(
                embed=build_status_embed(self.raid_id, self.user_id, self.raid_tasks),
                view=self,
            )
        return callback

    async def _confirm(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ This is not your panel.", ephemeral=True)
            return
        await _process_confirm(interaction, self.raid_id, ephemeral_edit=True)


# ── persistent main embed view ────────────────────────────────────────────────

class RaidMainView(discord.ui.View):
    """
    Attached to the public raid embed. Buttons have stable custom_ids
    so interactions survive bot restarts.
    """

    def __init__(self, raid_id: int, raid_tasks: list, mode: str, total_points: int):
        super().__init__(timeout=None)
        self.raid_id = raid_id
        self.raid_tasks = [t.lower() for t in raid_tasks]
        self.mode = mode
        self.total_points = total_points

        TASK_META = [
            ("like",    "👍 Like"),
            ("comment", "💬 Comment"),
            ("retweet", "🔁 Retweet"),
        ]
        for task, label in TASK_META:
            btn = discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.primary,
                custom_id=f"raid_task_{task}_{raid_id}",
                disabled=task not in self.raid_tasks,
                row=0,
            )
            btn.callback = self._make_task_cb(task)
            self.add_item(btn)

        confirm = discord.ui.Button(
            label="Confirm",
            style=discord.ButtonStyle.success,
            emoji="✅",
            custom_id=f"raid_confirm_{raid_id}",
            row=0,
        )
        confirm.callback = self._confirm_cb
        self.add_item(confirm)

    def _make_task_cb(self, task: str):
        async def callback(interaction: discord.Interaction):
            toggle_task(self.raid_id, interaction.user.id, task)
            view = UserTaskView(
                self.raid_id, interaction.user.id,
                self.raid_tasks, self.mode, self.total_points,
            )
            await interaction.response.send_message(
                embed=build_status_embed(self.raid_id, interaction.user.id, self.raid_tasks),
                view=view,
                ephemeral=True,
            )
        return callback

    async def _confirm_cb(self, interaction: discord.Interaction):
        await _process_confirm(interaction, self.raid_id, ephemeral_edit=False)


# ── shared confirm logic ──────────────────────────────────────────────────────

async def _process_confirm(interaction: discord.Interaction, raid_id: int, ephemeral_edit: bool):
    """
    Shared by both RaidMainView and UserTaskView confirm buttons.
    ephemeral_edit=True  → edit the existing ephemeral message
    ephemeral_edit=False → send a new ephemeral reply
    """
    raid = get_raid(raid_id)
    if not raid or not raid["active"]:
        msg = "❌ This raid is no longer active."
        if ephemeral_edit:
            await interaction.response.edit_message(content=msg, embed=None, view=None)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
        return

    with get_connection() as conn:
        existing = conn.execute(
            "SELECT id FROM raid_participation WHERE raid_id=? AND user_id=?",
            (raid_id, interaction.user.id),
        ).fetchone()

    if existing:
        msg = "⚠️ You already submitted for this raid."
        if ephemeral_edit:
            await interaction.response.edit_message(content=msg, embed=None, view=None)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
        return

    state = get_state(raid_id, interaction.user.id)
    if not state:
        msg = "⚠️ Select at least one task first."
        if ephemeral_edit:
            await interaction.response.edit_message(content=msg, embed=None, view=None)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
        return

    raid_tasks = json.loads(raid["tasks"]) if raid["tasks"] else []
    raid_task_set = {t.lower() for t in raid_tasks}

    if raid["mode"] == "all":
        missing = raid_task_set - {t.lower() for t in state}
        if missing:
            msg = (
                "❌ You must complete **all** tasks to earn points in this raid.\n"
                f"Missing: {', '.join(t.capitalize() for t in sorted(missing))}"
            )
            if ephemeral_edit:
                await interaction.response.edit_message(content=msg, embed=None, view=None)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
            return

    points = calc_points(raid["total_points"], raid_tasks, state, raid["mode"])

    upsert_user(interaction.user.id, str(interaction.user))
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO raid_participation (raid_id, user_id, tasks_completed, points_earned, confirmed_at) "
            "VALUES (?,?,?,?,?)",
            (
                raid_id, interaction.user.id,
                json.dumps(sorted(state)), points,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.execute(
            "UPDATE users SET total_points = total_points + ? WHERE user_id=?",
            (points, interaction.user.id),
        )

    user_states.pop((raid_id, interaction.user.id), None)

    embed = discord.Embed(
        title="raid confirmed. 💰",
        description=(
            f"Tasks: {', '.join(t.capitalize() for t in sorted(state))}\n"
            f"**+{points} points** credited. let's go. 🔥"
        ),
        color=0x94730D,
    )
    embed.set_footer(text="AmeretaVerse • Raids")

    if ephemeral_edit:
        await interaction.response.edit_message(content=None, embed=embed, view=None)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ── tweet existence check ─────────────────────────────────────────────────────

def scrape_tweet_check(tweet_link: str) -> bool:
    """
    Check tweet existence via fxtwitter API.
    Returns True = tweet exists (keep), False = deleted/not found (flag).
    """
    tweet_id = extract_tweet_id(tweet_link)
    if not tweet_id:
        return True  # can't parse URL — don't flag
    try:
        resp = requests.get(
            f"https://api.fxtwitter.com/status/{tweet_id}",
            timeout=10,
        )
        if resp.status_code in (404, 410):
            return False  # tweet deleted
        if resp.status_code != 200:
            return True   # API error — don't flag
        data = resp.json()
        # fxtwitter returns code 200 with an error message for suspended/not found
        return data.get("code", 200) == 200
    except Exception:
        return True  # network error — don't flag


# ── cog ───────────────────────────────────────────────────────────────────────

class RaidsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.daily_check.start()

    def cog_unload(self):
        self.daily_check.cancel()

    @commands.Cog.listener()
    async def on_ready(self):
        """Re-register persistent views for all active raids after a restart."""
        with get_connection() as conn:
            active = conn.execute(
                "SELECT raid_id, tasks, mode, total_points FROM raids WHERE active=1"
            ).fetchall()
        for row in active:
            raid_tasks = json.loads(row["tasks"]) if row["tasks"] else []
            self.bot.add_view(
                RaidMainView(row["raid_id"], raid_tasks, row["mode"], row["total_points"])
            )

    # ── /post ──────────────────────────────────────────────────────────────────

    @app_commands.command(name="post", description="Post a new raid (admin only)")
    @app_commands.describe(
        tweet_link="Link to the tweet",
        like="Include Like as a task",
        retweet="Include Retweet as a task",
        comment="Include Comment as a task",
        total_points="Total points awarded for this raid",
        mode="all = must complete every task; partial = earn points per task",
    )
    @app_commands.choices(
        like=[
            app_commands.Choice(name="Yes", value=1),
            app_commands.Choice(name="No",  value=0),
        ],
        retweet=[
            app_commands.Choice(name="Yes", value=1),
            app_commands.Choice(name="No",  value=0),
        ],
        comment=[
            app_commands.Choice(name="Yes", value=1),
            app_commands.Choice(name="No",  value=0),
        ],
        mode=[
            app_commands.Choice(name="all",     value="all"),
            app_commands.Choice(name="partial", value="partial"),
        ],
    )
    @app_commands.default_permissions(administrator=True)
    async def post(
        self,
        interaction: discord.Interaction,
        tweet_link: str,
        total_points: int,
        like: app_commands.Choice[int] = None,
        retweet: app_commands.Choice[int] = None,
        comment: app_commands.Choice[int] = None,
        mode: app_commands.Choice[str] = None,
    ):
        task_list = [
            t for t, choice in [("like", like), ("retweet", retweet), ("comment", comment)]
            if choice is not None and choice.value == 1
        ]
        if not task_list:
            await interaction.response.send_message(
                "❌ Select at least one task (like, retweet, or comment).", ephemeral=True
            )
            return

        mode_value = mode.value if mode else "all"

        raid_channel = next(
            (c for c in interaction.guild.text_channels if c.name.lower() == RAID_CHANNEL_NAME.lower()),
            None,
        )
        if not raid_channel:
            await interaction.response.send_message(
                f"❌ No channel named **{RAID_CHANNEL_NAME}** found.", ephemeral=True
            )
            return

        # Defer so we have time to fetch OG data
        await interaction.response.defer(ephemeral=True)

        tweet_data = fetch_tweet(tweet_link)

        with get_connection() as conn:
            cursor = conn.execute(
                "INSERT INTO raids (tweet_link, tasks, total_points, mode, created_by) VALUES (?,?,?,?,?)",
                (tweet_link, json.dumps(task_list), total_points, mode_value, interaction.user.id),
            )
            raid_id = cursor.lastrowid

        raid = get_raid(raid_id)
        raid_role = discord.utils.get(interaction.guild.roles, name=RAID_ROLE_NAME)
        mention = raid_role.mention if raid_role else f"@{RAID_ROLE_NAME}"

        raid_embed = build_raid_embed(raid, tweet_data)
        view = RaidMainView(raid_id, task_list, mode_value, total_points)
        self.bot.add_view(view)

        await raid_channel.send(
            content=f"{mention} — new raid just dropped! ⚔️",
            embed=raid_embed,
            view=view,
        )

        await interaction.followup.send(
            f"✅ Raid `#{raid_id}` posted in {raid_channel.mention}.", ephemeral=True
        )

    # ── /leaderboard ───────────────────────────────────────────────────────────

    @app_commands.command(name="leaderboard", description="Top 10 users by total raid points")
    async def leaderboard(self, interaction: discord.Interaction):
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT username, total_points FROM users ORDER BY total_points DESC LIMIT 10"
            ).fetchall()

        if not rows:
            await interaction.response.send_message("No raid participants yet. Be the first. ⚔️")
            return

        medals = ["🥇", "🥈", "🥉"]
        lines = [
            f"{medals[i] if i < 3 else f'`{i+1}.`'} **{r['username']}** — `{r['total_points']} pts`"
            for i, r in enumerate(rows)
        ]
        embed = discord.Embed(
            title="⚔️ Raid Leaderboard",
            description="\n".join(lines),
            color=0x94730D,
        )
        embed.set_thumbnail(url=LOGO_URL)
        embed.set_footer(text="AmeretaVerse • Raids")
        await interaction.response.send_message(embed=embed)

    # ── /profile ───────────────────────────────────────────────────────────────

    @app_commands.command(name="profile", description="View raid points and participation count")
    @app_commands.describe(member="Member to look up (defaults to yourself)")
    async def profile(self, interaction: discord.Interaction, member: discord.Member = None):
        target = member or interaction.user
        with get_connection() as conn:
            user = conn.execute(
                "SELECT total_points, x_username FROM users WHERE user_id=?", (target.id,)
            ).fetchone()
            count = conn.execute(
                "SELECT COUNT(*) as c FROM raid_participation WHERE user_id=?", (target.id,)
            ).fetchone()

        points      = user["total_points"] if user else 0
        x_username  = user["x_username"]   if user else None
        raids_count = count["c"]           if count else 0

        name = "your" if target == interaction.user else f"{target.display_name}'s"
        embed = discord.Embed(title=f"{name} profile 🦁", color=0x94730D)
        embed.add_field(name="Total Points", value=f"`{points} pts`", inline=True)
        embed.add_field(name="Raids Joined",  value=f"`{raids_count}`",  inline=True)
        embed.add_field(
            name="X Username",
            value=f"[@{x_username}](https://x.com/{x_username})" if x_username else "`not set` — use /setx`",
            inline=False,
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.set_footer(text="AmeretaVerse • Raids")
        await interaction.response.send_message(embed=embed)

    # ── /setx ──────────────────────────────────────────────────────────────────

    @app_commands.command(name="setx", description="Link your X (Twitter) username to your profile")
    @app_commands.describe(username="Your X username without the @ symbol")
    async def setx(self, interaction: discord.Interaction, username: str):
        username = username.lstrip("@").strip()
        now = datetime.now(timezone.utc)

        with get_connection() as conn:
            user = conn.execute(
                "SELECT x_username, x_username_set_at FROM users WHERE user_id=?",
                (interaction.user.id,),
            ).fetchone()

        if user and user["x_username"] and user["x_username_set_at"]:
            set_at = datetime.fromisoformat(user["x_username_set_at"])
            if set_at.tzinfo is None:
                set_at = set_at.replace(tzinfo=timezone.utc)
            days_since = (now - set_at).days
            if days_since < 7:
                days_left = 7 - days_since
                await interaction.response.send_message(
                    f"⏳ You can change your X username in **{days_left} day{'s' if days_left != 1 else ''}**.",
                    ephemeral=True,
                )
                return

        upsert_user(interaction.user.id, str(interaction.user))
        with get_connection() as conn:
            conn.execute(
                "UPDATE users SET x_username=?, x_username_set_at=? WHERE user_id=?",
                (username, now.isoformat(), interaction.user.id),
            )

        await interaction.response.send_message(
            f"✅ X username set to **@{username}**.", ephemeral=True
        )

    # ── /engagers ──────────────────────────────────────────────────────────────

    @app_commands.command(name="engagers", description="Export raid participation as CSV (admin only)")
    @app_commands.describe(tweet_link="Tweet link of the raid to export")
    @app_commands.default_permissions(administrator=True)
    async def engagers(self, interaction: discord.Interaction, tweet_link: str):
        with get_connection() as conn:
            raid = conn.execute(
                "SELECT raid_id FROM raids WHERE tweet_link=? ORDER BY raid_id DESC LIMIT 1",
                (tweet_link,),
            ).fetchone()

        if not raid:
            await interaction.response.send_message(
                "❌ No raid found for that tweet link.", ephemeral=True
            )
            return

        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT u.username, u.user_id, u.x_username,
                       rp.tasks_completed, rp.points_earned, rp.flagged
                FROM raid_participation rp
                JOIN users u ON rp.user_id = u.user_id
                WHERE rp.raid_id = ?
                ORDER BY rp.points_earned DESC
                """,
                (raid["raid_id"],),
            ).fetchall()

        if not rows:
            await interaction.response.send_message(
                "No participation data for this raid yet.", ephemeral=True
            )
            return

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["discord_username", "discord_id", "x_username",
                         "tasks_completed", "points_earned", "flagged"])
        for r in rows:
            tasks_done = json.loads(r["tasks_completed"]) if r["tasks_completed"] else []
            writer.writerow([
                r["username"],
                r["user_id"],
                r["x_username"] or "",
                ", ".join(tasks_done),
                r["points_earned"],
                bool(r["flagged"]),
            ])

        buf.seek(0)
        file = discord.File(
            io.BytesIO(buf.getvalue().encode()),
            filename=f"raid_{raid['raid_id']}_engagers.csv",
        )
        await interaction.response.send_message(
            f"📊 **Raid #{raid['raid_id']}** — {len(rows)} participant{'s' if len(rows) != 1 else ''}:",
            file=file,
        )

    # ── daily verification check ──────────────────────────────────────────────

    @tasks.loop(time=dtime(hour=0, minute=0, tzinfo=timezone.utc))
    async def daily_check(self):
        """
        Midnight: sample 20% of participations from the last 7 days.
        - Flag if tweet is deleted.
        - If user has x_username set, verify retweet/comment task counts
          via fxtwitter (note: fxtwitter exposes aggregate counts only, not
          per-user lists — zero engagement on a claimed task triggers a flag).
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        with get_connection() as conn:
            recent = conn.execute(
                """
                SELECT rp.id, rp.user_id, rp.tasks_completed, r.tweet_link
                FROM raid_participation rp
                JOIN raids r ON rp.raid_id = r.raid_id
                WHERE rp.confirmed_at >= ? AND rp.flagged = 0
                """,
                (cutoff,),
            ).fetchall()

        if not recent:
            return

        sample_size = max(1, round(len(recent) * 0.20))
        sample = random.sample(recent, min(sample_size, len(recent)))

        flag_ids = []
        for entry in sample:
            tweet_link = entry["tweet_link"]

            # 1. Flag if tweet is deleted/gone
            if not scrape_tweet_check(tweet_link):
                flag_ids.append(entry["id"])
                continue

            # 2. Skip deeper check if user has no x_username
            with get_connection() as conn:
                user = conn.execute(
                    "SELECT x_username FROM users WHERE user_id=?", (entry["user_id"],)
                ).fetchone()
            if not user or not user["x_username"]:
                continue

            # 3. Verify engagement counts via fxtwitter
            tweet_id = extract_tweet_id(tweet_link)
            if not tweet_id:
                continue

            tasks_done = set(json.loads(entry["tasks_completed"])) if entry["tasks_completed"] else set()
            try:
                resp = requests.get(
                    f"https://api.fxtwitter.com/status/{tweet_id}", timeout=10
                )
                if resp.status_code != 200:
                    continue
                tweet = resp.json().get("tweet", {})

                # fxtwitter can't verify per-user actions; flag if zero engagement
                # on a task the user claimed to have completed
                if "retweet" in tasks_done and tweet.get("retweet_count", 1) == 0:
                    flag_ids.append(entry["id"])
                elif "comment" in tasks_done and tweet.get("replies", 1) == 0:
                    flag_ids.append(entry["id"])
            except Exception:
                continue

        if flag_ids:
            with get_connection() as conn:
                conn.executemany(
                    "UPDATE raid_participation SET flagged=1 WHERE id=?",
                    [(fid,) for fid in flag_ids],
                )

    @daily_check.before_loop
    async def before_daily_check(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(RaidsCog(bot))
