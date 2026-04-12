import discord
from discord import app_commands
from discord.ext import commands, tasks
import json
import random
import re
from datetime import datetime, timezone, timedelta, time as dtime
from database import get_connection

LOGO_URL = "https://i.imgur.com/FNE8Li0.png"
PROGRESS_BAR_URL = "https://i.imgur.com/5Mg2BIE.png"
ENGAGE_CHANNEL_NAME = "engage"


# ── config helper ─────────────────────────────────────────────────────────────

def get_config(key: str, default: str = "0") -> str:
    with get_connection() as conn:
        row = conn.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def get_config_int(key: str, default: int = 0) -> int:
    return int(float(get_config(key, str(default))))


def get_config_float(key: str, default: float = 0.0) -> float:
    return float(get_config(key, str(default)))


# ── helpers ───────────────────────────────────────────────────────────────────

def extract_tweet_id(url: str) -> str | None:
    match = re.search(r"/status/(\d+)", url)
    return match.group(1) if match else None


def validate_tweet_link(url: str) -> bool:
    return bool(re.match(r"https?://(x\.com|twitter\.com)/\w+/status/\d+", url))


# In-memory toggle state: {(link_id, user_id): set[str]}
engage_states: dict = {}


def toggle_engage_task(link_id: int, user_id: int, task: str) -> set:
    key = (link_id, user_id)
    s = engage_states.setdefault(key, set())
    s.discard(task) if task in s else s.add(task)
    return s


def get_engage_state(link_id: int, user_id: int) -> set:
    return engage_states.get((link_id, user_id), set())


def calc_engage_points(done: set) -> int:
    """Calculate engage points based on config weights."""
    weights = {
        "like": get_config_float("engage_weight_like", 12.5),
        "comment": get_config_float("engage_weight_comment", 40.0),
        "retweet": get_config_float("engage_weight_retweet", 47.5),
    }
    total = get_config_int("engage_points_per_link", 10)
    total_w = sum(weights.values())
    if not total_w or not done:
        return 0
    done_w = sum(weights.get(t, 0) for t in done)
    return round(total * done_w / total_w)


def upsert_user(user_id: int, username: str):
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO users (user_id, username) VALUES (?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username",
            (user_id, username),
        )


def get_available_links(user_id: int, limit: int = 10) -> list:
    """
    Get links for a user to engage with:
    1. Links submitted by OTHER users (not self)
    2. Unfinished raid links (user hasn't participated in those raids)
    3. Exclude links the user already engaged with
    4. Only active, non-expired links
    """
    now = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        # Get engage links from other users, not yet engaged by this user
        submitted_links = conn.execute(
            """
            SELECT el.link_id, el.tweet_link, el.user_id AS owner_id, 'engage' AS source
            FROM engage_links el
            WHERE el.active = 1
              AND el.expires_at > ?
              AND el.user_id != ?
              AND el.link_id NOT IN (
                  SELECT ep.link_id FROM engage_participation ep WHERE ep.user_id = ?
              )
            ORDER BY RANDOM()
            LIMIT ?
            """,
            (now, user_id, user_id, limit),
        ).fetchall()

        remaining = limit - len(submitted_links)

        # Fill remaining slots with unfinished raid links
        raid_links = []
        if remaining > 0:
            raid_links = conn.execute(
                """
                SELECT r.raid_id AS link_id, r.tweet_link, r.created_by AS owner_id, 'raid' AS source
                FROM raids r
                WHERE r.active = 1
                  AND r.raid_id NOT IN (
                      SELECT rp.raid_id FROM raid_participation rp WHERE rp.user_id = ?
                  )
                  AND r.tweet_link NOT IN (
                      SELECT el.tweet_link FROM engage_links el
                      WHERE el.link_id IN (
                          SELECT ep.link_id FROM engage_participation ep WHERE ep.user_id = ?
                      )
                  )
                ORDER BY RANDOM()
                LIMIT ?
                """,
                (user_id, user_id, remaining),
            ).fetchall()

    return list(submitted_links) + list(raid_links)


# ── per-link task panel ──────────────────────────────────────────────────────

class EngageLinkView(discord.ui.View):
    """Ephemeral panel for a single link — toggle tasks and confirm."""

    def __init__(self, link_id: int, user_id: int, tweet_link: str, source: str):
        super().__init__(timeout=600)
        self.link_id = link_id
        self.user_id = user_id
        self.tweet_link = tweet_link
        self.source = source  # 'engage' or 'raid'
        self._rebuild()

    def _rebuild(self):
        self.clear_items()
        state = get_engage_state(self.link_id, self.user_id)
        TASK_META = [
            ("like",    "👍 Like"),
            ("comment", "💬 Comment"),
            ("retweet", "🔁 Retweet"),
        ]
        for task, label in TASK_META:
            active = task in state
            btn = discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.success if active else discord.ButtonStyle.primary,
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
            toggle_engage_task(self.link_id, self.user_id, task)
            self._rebuild()
            await interaction.response.edit_message(
                embed=self._status_embed(),
                view=self,
            )
        return callback

    def _status_embed(self) -> discord.Embed:
        state = get_engage_state(self.link_id, self.user_id)
        lines = []
        for t in ["like", "comment", "retweet"]:
            icon = "✅" if t in state else "⬜"
            lines.append(f"{icon} {t.capitalize()}")
        pts = calc_engage_points(state)
        embed = discord.Embed(
            title="engage this tweet 🔗",
            description=(
                f"**[Open Tweet]({self.tweet_link})**\n\n"
                + "\n".join(lines)
                + f"\n\n**Points:** `+{pts}` engage pts"
            ),
            color=0x94730D,
        )
        embed.set_footer(text="AmeretaVerse • Engage-for-Engage")
        return embed

    async def _confirm(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ This is not your panel.", ephemeral=True)
            return

        state = get_engage_state(self.link_id, self.user_id)
        if not state:
            await interaction.response.edit_message(
                content="⚠️ Select at least one task first.", embed=None, view=None
            )
            return

        # Check if already participated
        with get_connection() as conn:
            existing = conn.execute(
                "SELECT id FROM engage_participation WHERE link_id=? AND user_id=?",
                (self.link_id, self.user_id),
            ).fetchone()

        if existing:
            await interaction.response.edit_message(
                content="⚠️ You already engaged with this link.", embed=None, view=None
            )
            return

        points = calc_engage_points(state)

        upsert_user(interaction.user.id, str(interaction.user))
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO engage_participation (link_id, user_id, tasks_completed, points_earned) "
                "VALUES (?,?,?,?)",
                (self.link_id, self.user_id, json.dumps(sorted(state)), points),
            )
            conn.execute(
                "UPDATE users SET engage_points = engage_points + ? WHERE user_id=?",
                (points, self.user_id),
            )

        engage_states.pop((self.link_id, self.user_id), None)

        embed = discord.Embed(
            title="engaged. 🔥",
            description=(
                f"Tasks: {', '.join(t.capitalize() for t in sorted(state))}\n"
                f"**+{points} engage points** credited.\n\n"
                f"keep grinding, habibi. 💰"
            ),
            color=0x94730D,
        )
        embed.set_footer(text="AmeretaVerse • Engage-for-Engage")
        await interaction.response.edit_message(content=None, embed=embed, view=None)


# ── link selector (dropdown) ────────────────────────────────────────────────

class LinkSelectView(discord.ui.View):
    """Shows available links as a select dropdown."""

    def __init__(self, user_id: int, links: list):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.links = links

        options = []
        for i, link in enumerate(links):
            tweet_id = extract_tweet_id(link["tweet_link"]) or "unknown"
            short_id = tweet_id[-8:]  # last 8 digits
            source_tag = "📌 Raid" if link["source"] == "raid" else "🔗 User"
            options.append(
                discord.SelectOption(
                    label=f"{source_tag} — ...{short_id}",
                    description=link["tweet_link"][:80],
                    value=str(i),
                )
            )

        select = discord.ui.Select(
            placeholder="pick a tweet to engage with 👇",
            options=options,
            min_values=1,
            max_values=1,
        )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ This is not your panel.", ephemeral=True)
            return

        idx = int(interaction.data["values"][0])
        link = self.links[idx]

        view = EngageLinkView(
            link_id=link["link_id"],
            user_id=self.user_id,
            tweet_link=link["tweet_link"],
            source=link["source"],
        )
        await interaction.response.edit_message(
            embed=view._status_embed(),
            view=view,
        )


# ── submit link modal ────────────────────────────────────────────────────────

class SubmitLinkModal(discord.ui.Modal, title="Submit Your Tweet 🔗"):
    tweet_link = discord.ui.TextInput(
        label="Tweet Link",
        placeholder="https://x.com/yourhandle/status/123456789",
        min_length=20,
        max_length=300,
        style=discord.TextStyle.short,
    )

    async def on_submit(self, interaction: discord.Interaction):
        url = self.tweet_link.value.strip()

        if not validate_tweet_link(url):
            await interaction.response.send_message(
                "❌ Invalid tweet link. Use format: `https://x.com/user/status/123456`",
                ephemeral=True,
            )
            return

        # Check submit cost
        cost = get_config_int("engage_submit_cost", 0)
        if cost > 0:
            with get_connection() as conn:
                user = conn.execute(
                    "SELECT engage_points FROM users WHERE user_id=?",
                    (interaction.user.id,),
                ).fetchone()
            current_pts = user["engage_points"] if user else 0
            if current_pts < cost:
                await interaction.response.send_message(
                    f"❌ You need **{cost} engage points** to submit a link. "
                    f"You have **{current_pts}**.\n"
                    f"Use `/engage` to earn more points first.",
                    ephemeral=True,
                )
                return

        # Check duplicate active link
        with get_connection() as conn:
            now = datetime.now(timezone.utc).isoformat()
            dup = conn.execute(
                "SELECT link_id FROM engage_links WHERE tweet_link=? AND active=1 AND expires_at>?",
                (url, now),
            ).fetchone()

        if dup:
            await interaction.response.send_message(
                "⚠️ This tweet is already in the engage pool.",
                ephemeral=True,
            )
            return

        # Submit
        lifetime = get_config_int("engage_link_lifetime_hours", 24)
        expires = datetime.now(timezone.utc) + timedelta(hours=lifetime)

        upsert_user(interaction.user.id, str(interaction.user))
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO engage_links (user_id, tweet_link, source, expires_at) VALUES (?,?,?,?)",
                (interaction.user.id, url, "submit", expires.isoformat()),
            )
            # Deduct cost if set
            if cost > 0:
                conn.execute(
                    "UPDATE users SET engage_points = engage_points - ? WHERE user_id=?",
                    (cost, interaction.user.id),
                )

        embed = discord.Embed(
            title="tweet submitted. 🔗",
            description=(
                f"Your tweet is now in the engage pool.\n\n"
                f"**Link:** {url}\n"
                f"**Expires in:** {lifetime}h\n"
                + (f"**Cost:** -{cost} engage pts\n" if cost > 0 else "")
                + "\nother users will engage with it. let's grow together. 🔥"
            ),
            color=0x94730D,
        )
        embed.set_thumbnail(url=LOGO_URL)
        embed.set_footer(text="AmeretaVerse • Engage-for-Engage")
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ── cog ──────────────────────────────────────────────────────────────────────

class EngageCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.cleanup_expired.start()

    def cog_unload(self):
        self.cleanup_expired.cancel()

    # ── /engage ────────────────────────────────────────────────────────────────

    @app_commands.command(name="engage", description="Get tweets to engage with and earn points")
    async def engage(self, interaction: discord.Interaction):
        # Check daily limit
        daily_limit = get_config_int("engage_daily_limit", 0)
        if daily_limit > 0:
            today_start = datetime.now(timezone.utc).replace(
                hour=0, minute=0, second=0, microsecond=0
            ).isoformat()
            with get_connection() as conn:
                today_count = conn.execute(
                    "SELECT COUNT(*) as c FROM engage_participation "
                    "WHERE user_id=? AND confirmed_at>=?",
                    (interaction.user.id, today_start),
                ).fetchone()
            if today_count and today_count["c"] >= daily_limit:
                await interaction.response.send_message(
                    f"⏳ You've hit your daily engage limit (**{daily_limit}** today). "
                    f"Come back tomorrow, habibi.",
                    ephemeral=True,
                )
                return

        limit = get_config_int("engage_links_per_request", 10)
        links = get_available_links(interaction.user.id, limit)

        if not links:
            embed = discord.Embed(
                title="no tweets available right now. 😴",
                description=(
                    "The engage pool is empty.\n\n"
                    "Submit your own tweet with `/submit` to get the ball rolling. 🔗"
                ),
                color=0x94730D,
            )
            embed.set_thumbnail(url=LOGO_URL)
            embed.set_footer(text="AmeretaVerse • Engage-for-Engage")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        embed = discord.Embed(
            title=f"engage time. ⚡ ({len(links)} tweets)",
            description=(
                "Pick a tweet from the dropdown below.\n"
                "Engage with it, select what you did, hit confirm.\n"
                "Each tweet earns you **engage points**. 💰\n\n"
                "📌 = raid tweet  •  🔗 = user submitted"
            ),
            color=0x94730D,
        )
        embed.set_thumbnail(url=LOGO_URL)
        embed.set_footer(text="AmeretaVerse • Engage-for-Engage")

        view = LinkSelectView(interaction.user.id, links)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    # ── /submit ────────────────────────────────────────────────────────────────

    @app_commands.command(name="submit", description="Submit your tweet for others to engage with")
    async def submit(self, interaction: discord.Interaction):
        modal = SubmitLinkModal()
        await interaction.response.send_modal(modal)

    # ── /engage-stats ──────────────────────────────────────────────────────────

    @app_commands.command(name="engage-stats", description="View your engage points and stats")
    @app_commands.describe(member="Member to look up (defaults to yourself)")
    async def engage_stats(self, interaction: discord.Interaction, member: discord.Member = None):
        target = member or interaction.user
        with get_connection() as conn:
            user = conn.execute(
                "SELECT engage_points FROM users WHERE user_id=?", (target.id,)
            ).fetchone()
            engages_done = conn.execute(
                "SELECT COUNT(*) as c FROM engage_participation WHERE user_id=?", (target.id,)
            ).fetchone()
            links_submitted = conn.execute(
                "SELECT COUNT(*) as c FROM engage_links WHERE user_id=?", (target.id,)
            ).fetchone()

        points = user["engage_points"] if user else 0
        done_count = engages_done["c"] if engages_done else 0
        submitted_count = links_submitted["c"] if links_submitted else 0

        name = "your" if target == interaction.user else f"{target.display_name}'s"
        embed = discord.Embed(title=f"{name} engage stats ⚡", color=0x94730D)
        embed.add_field(name="Engage Points", value=f"`{points} pts`", inline=True)
        embed.add_field(name="Tweets Engaged", value=f"`{done_count}`", inline=True)
        embed.add_field(name="Links Submitted", value=f"`{submitted_count}`", inline=True)
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.set_footer(text="AmeretaVerse • Engage-for-Engage")
        await interaction.response.send_message(embed=embed)

    # ── /engage-leaderboard ────────────────────────────────────────────────────

    @app_commands.command(name="engage-leaderboard", description="Top 10 users by engage points")
    async def engage_leaderboard(self, interaction: discord.Interaction):
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT username, engage_points FROM users "
                "WHERE engage_points > 0 ORDER BY engage_points DESC LIMIT 10"
            ).fetchall()

        if not rows:
            await interaction.response.send_message(
                "No engage activity yet. Be the first — use `/engage`. ⚡"
            )
            return

        medals = ["🥇", "🥈", "🥉"]
        lines = [
            f"{medals[i] if i < 3 else f'`{i+1}.`'} **{r['username']}** — `{r['engage_points']} pts`"
            for i, r in enumerate(rows)
        ]
        embed = discord.Embed(
            title="⚡ Engage Leaderboard",
            description="\n".join(lines),
            color=0x94730D,
        )
        embed.set_thumbnail(url=LOGO_URL)
        embed.set_footer(text="AmeretaVerse • Engage-for-Engage")
        await interaction.response.send_message(embed=embed)

    # ── /engage-config (admin) ────────────────────────────────────────────────

    @app_commands.command(name="engage-config", description="View or change engage settings (admin)")
    @app_commands.describe(
        setting="Which setting to change",
        value="New value",
    )
    @app_commands.choices(setting=[
        app_commands.Choice(name="Link lifetime (hours)", value="engage_link_lifetime_hours"),
        app_commands.Choice(name="Links per /engage", value="engage_links_per_request"),
        app_commands.Choice(name="Daily limit (0=unlimited)", value="engage_daily_limit"),
        app_commands.Choice(name="Submit cost (points)", value="engage_submit_cost"),
        app_commands.Choice(name="Points per link", value="engage_points_per_link"),
        app_commands.Choice(name="Weight: Like %", value="engage_weight_like"),
        app_commands.Choice(name="Weight: Comment %", value="engage_weight_comment"),
        app_commands.Choice(name="Weight: Retweet %", value="engage_weight_retweet"),
    ])
    @app_commands.default_permissions(administrator=True)
    async def engage_config(
        self,
        interaction: discord.Interaction,
        setting: app_commands.Choice[str] = None,
        value: str = None,
    ):
        if setting and value:
            with get_connection() as conn:
                conn.execute(
                    "INSERT INTO config (key, value) VALUES (?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (setting.value, value),
                )
            await interaction.response.send_message(
                f"✅ **{setting.name}** set to `{value}`.", ephemeral=True
            )
        else:
            # Show all config
            with get_connection() as conn:
                rows = conn.execute(
                    "SELECT key, value FROM config WHERE key LIKE 'engage_%' ORDER BY key"
                ).fetchall()
            lines = [f"`{r['key']}` = **{r['value']}**" for r in rows]
            embed = discord.Embed(
                title="⚙️ Engage Config",
                description="\n".join(lines) or "No settings found.",
                color=0x94730D,
            )
            embed.set_footer(text="AmeretaVerse • Engage-for-Engage | Use /engage-config to change")
            await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── cleanup expired links ────────────────────────────────────────────────

    @tasks.loop(hours=1)
    async def cleanup_expired(self):
        """Deactivate expired engage links every hour."""
        now = datetime.now(timezone.utc).isoformat()
        with get_connection() as conn:
            conn.execute(
                "UPDATE engage_links SET active=0 WHERE active=1 AND expires_at<=?",
                (now,),
            )

    @cleanup_expired.before_loop
    async def before_cleanup(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(EngageCog(bot))
