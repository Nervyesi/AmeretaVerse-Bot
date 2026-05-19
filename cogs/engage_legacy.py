import discord
from discord import app_commands
from discord.ext import commands, tasks
import json
import re
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from database import (
    get_connection,
    get_config as _db_get_config,
    set_config as _db_set_config,
    get_all_config as _db_get_all_config,
)
from config import DEFAULT_BOT_THUMBNAIL_URL as LOGO_URL

ENGAGE_CHANNEL_NAME = "engage"
CREATOR_ENGAGE_CHANNEL_NAME = "creator-engage"


# ── pool context ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PoolCtx:
    is_creator: bool
    channel_name: str
    links_table: str
    part_table: str
    points_col: str
    cfg_prefix: str
    label: str


USER_POOL = PoolCtx(
    is_creator=False,
    channel_name=ENGAGE_CHANNEL_NAME,
    links_table="engage_links",
    part_table="engage_participation",
    points_col="engage_points",
    cfg_prefix="engage",
    label="Engage-for-Engage",
)

CREATOR_POOL = PoolCtx(
    is_creator=True,
    channel_name=CREATOR_ENGAGE_CHANNEL_NAME,
    links_table="creator_engage_links",
    part_table="creator_engage_participation",
    points_col="creator_engage_points",
    cfg_prefix="creator_engage",
    label="Creator Engage",
)


def get_pool(interaction: discord.Interaction) -> PoolCtx:
    creator_role = discord.utils.get(interaction.user.roles, name="Creator")
    return CREATOR_POOL if creator_role else USER_POOL


# ── per-guild config helpers ───────────────────────────────────────────────────

def get_cfg(guild_id: int, key: str, default: str = "0") -> str:
    val = _db_get_config(guild_id, key)
    return val if val is not None else default


def get_cfg_int(guild_id: int, key: str, default: int = 0) -> int:
    return int(float(get_cfg(guild_id, key, str(default))))


def get_cfg_float(guild_id: int, key: str, default: float = 0.0) -> float:
    return float(get_cfg(guild_id, key, str(default)))


# ── helpers ────────────────────────────────────────────────────────────────────

def extract_twitter_handle(url: str) -> str:
    match = re.search(r"https?://(?:x\.com|twitter\.com)/(\w+)/status/", url)
    return match.group(1) if match else "unknown"


def validate_tweet_link(url: str) -> bool:
    return bool(re.match(r"https?://(x\.com|twitter\.com)/\w+/status/\d+", url))


def calc_engage_points(done: set, cfg_prefix: str = "engage", guild_id: int = 0) -> int:
    weights = {
        "like":    get_cfg_float(guild_id, f"{cfg_prefix}_weight_like",    12.5),
        "comment": get_cfg_float(guild_id, f"{cfg_prefix}_weight_comment", 40.0),
        "retweet": get_cfg_float(guild_id, f"{cfg_prefix}_weight_retweet", 47.5),
    }
    total = get_cfg_int(guild_id, f"{cfg_prefix}_points_per_link", 10)
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


def get_available_links(user_id: int, limit: int, ctx: PoolCtx) -> list:
    now = datetime.now(timezone.utc).isoformat()
    lt = ctx.links_table
    pt = ctx.part_table

    with get_connection() as conn:
        submitted_links = conn.execute(
            f"""
            SELECT el.link_id, el.tweet_link, el.user_id AS owner_id, 'submit' AS source
            FROM {lt} el
            WHERE el.active = 1
              AND el.expires_at > ?
              AND el.user_id != ?
              AND el.link_id NOT IN (
                  SELECT ep.link_id FROM {pt} ep WHERE ep.user_id = ?
              )
            ORDER BY RANDOM()
            LIMIT ?
            """,
            (now, user_id, user_id, limit),
        ).fetchall()

        remaining = limit - len(submitted_links)
        raid_links = []
        if not ctx.is_creator and remaining > 0:
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


# ── session state ──────────────────────────────────────────────────────────────

@dataclass
class EngageSession:
    links: list
    ctx: PoolCtx
    guild_id: int = 0
    index: int = 0
    task_state: dict = field(default_factory=dict)
    submitted: dict = field(default_factory=dict)
    skipped: set = field(default_factory=set)

    def current_link(self):
        return self.links[self.index] if self.index < len(self.links) else None

    def is_done(self) -> bool:
        return self.index >= len(self.links)

    def total_points(self) -> int:
        return sum(self.submitted.values())

    def toggle_task(self, task: str):
        s = self.task_state.setdefault(self.index, set())
        s.discard(task) if task in s else s.add(task)

    def current_tasks(self) -> set:
        return self.task_state.get(self.index, set())


active_sessions: dict[int, EngageSession] = {}


# ── step 2: list view ─────────────────────────────────────────────────────────

class EngageListView(discord.ui.View):
    def __init__(self, user_id: int, session: EngageSession):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.session = session

    @discord.ui.button(label="Start Engage ⚡", style=discord.ButtonStyle.success, row=0)
    async def start_engage(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ This is not your panel.", ephemeral=True)
            return
        self.session.index = 0
        view = EngageTweetView(self.user_id, self.session)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)

    async def on_timeout(self):
        active_sessions.pop(self.user_id, None)


# ── steps 3-5: per-tweet view ─────────────────────────────────────────────────

class EngageTweetView(discord.ui.View):
    def __init__(self, user_id: int, session: EngageSession):
        super().__init__(timeout=600)
        self.user_id = user_id
        self.session = session
        self._rebuild()

    def _rebuild(self):
        self.clear_items()
        tasks = self.session.current_tasks()
        index = self.session.index

        for task, label, emoji in [
            ("like",    "Like",    "👍"),
            ("comment", "Comment", "💬"),
            ("retweet", "Retweet", "🔁"),
        ]:
            active = task in tasks
            btn = discord.ui.Button(
                label=f"{emoji} {label}",
                style=discord.ButtonStyle.success if active else discord.ButtonStyle.secondary,
                row=0,
            )
            btn.callback = self._make_toggle(task)
            self.add_item(btn)

        prev_btn = discord.ui.Button(
            label="◀ Previous",
            style=discord.ButtonStyle.primary,
            disabled=(index == 0),
            row=1,
        )
        prev_btn.callback = self._on_prev
        self.add_item(prev_btn)

        next_btn = discord.ui.Button(label="Next ▶", style=discord.ButtonStyle.primary, row=1)
        next_btn.callback = self._on_next
        self.add_item(next_btn)

        submit_btn = discord.ui.Button(label="Submit ✅", style=discord.ButtonStyle.success, row=1)
        submit_btn.callback = self._on_submit
        self.add_item(submit_btn)

    def build_embed(self) -> discord.Embed:
        session = self.session
        if session.is_done():
            return self._summary_embed()

        index = session.index
        link = session.current_link()
        total = len(session.links)
        handle = extract_twitter_handle(link["tweet_link"])
        tasks = session.current_tasks()

        check = {t: "✅" if t in tasks else "⬜" for t in ("like", "comment", "retweet")}
        pts = calc_engage_points(tasks, session.ctx.cfg_prefix, session.guild_id)

        already = index in session.submitted
        status_note = f"\n✔ Submitted (+{session.submitted[index]} pts)" if already else ""

        embed = discord.Embed(title=f"Tweet {index + 1} of {total} ⚡", color=0x94730D)
        embed.add_field(
            name=f"@{handle}",
            value=f"[Open Tweet]({link['tweet_link']})\n`{link['tweet_link']}`",
            inline=False,
        )
        embed.add_field(
            name="Tasks",
            value=f"{check['like']} Like\n{check['comment']} Comment\n{check['retweet']} Retweet",
            inline=True,
        )
        embed.add_field(name="Points Preview", value=f"`+{pts} pts`{status_note}", inline=True)
        embed.set_footer(
            text=f"AmeretaVerse • {session.ctx.label} | Toggle tasks → Submit  |  Next to skip"
        )
        return embed

    def _summary_embed(self) -> discord.Embed:
        session = self.session
        total_pts = session.total_points()
        lines = []
        for i, link in enumerate(session.links):
            handle = extract_twitter_handle(link["tweet_link"])
            if i in session.submitted:
                lines.append(f"✅ @{handle} — +{session.submitted[i]} pts")
            elif i in session.skipped:
                lines.append(f"⏭ @{handle} — skipped")
            else:
                lines.append(f"⬜ @{handle} — not reached")

        embed = discord.Embed(
            title="engage session complete. 🔥",
            description="\n".join(lines),
            color=0x94730D,
        )
        embed.add_field(name="Engaged",      value=f"`{len(session.submitted)}`", inline=True)
        embed.add_field(name="Skipped",      value=f"`{len(session.skipped)}`",   inline=True)
        embed.add_field(name="Total Points", value=f"`+{total_pts} pts`",         inline=True)
        embed.set_thumbnail(url=LOGO_URL)
        embed.set_footer(text=f"AmeretaVerse • {session.ctx.label} | keep grinding, habibi 💰")
        return embed

    def _make_toggle(self, task: str):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.user_id:
                await interaction.response.send_message("❌ Not your panel.", ephemeral=True)
                return
            self.session.toggle_task(task)
            self._rebuild()
            await interaction.response.edit_message(embed=self.build_embed(), view=self)
        return callback

    async def _on_prev(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Not your panel.", ephemeral=True)
            return
        if self.session.index > 0:
            self.session.index -= 1
        self._rebuild()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _on_next(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Not your panel.", ephemeral=True)
            return
        self.session.skipped.add(self.session.index)
        self.session.index += 1
        if self.session.is_done():
            active_sessions.pop(self.user_id, None)
            self.stop()
            await interaction.response.edit_message(embed=self.build_embed(), view=None)
        else:
            self._rebuild()
            await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Not your panel.", ephemeral=True)
            return

        tasks = self.session.current_tasks()
        if not tasks:
            await interaction.response.send_message(
                "⚠️ Toggle at least one task before submitting, habibi.", ephemeral=True
            )
            return

        ctx = self.session.ctx
        index = self.session.index
        link = self.session.current_link()
        points = calc_engage_points(tasks, ctx.cfg_prefix, interaction.guild_id)
        user_id = interaction.user.id
        pt = ctx.part_table
        pc = ctx.points_col

        upsert_user(user_id, str(interaction.user))
        with get_connection() as conn:
            existing = conn.execute(
                f"SELECT id, points_earned FROM {pt} WHERE link_id=? AND user_id=?",
                (link["link_id"], user_id),
            ).fetchone()

            if existing:
                old_pts = existing["points_earned"]
                conn.execute(
                    f"UPDATE {pt} SET tasks_completed=?, points_earned=? "
                    f"WHERE link_id=? AND user_id=?",
                    (json.dumps(sorted(tasks)), points, link["link_id"], user_id),
                )
                conn.execute(
                    f"UPDATE users SET {pc} = {pc} + ? WHERE user_id=?",
                    (points - old_pts, user_id),
                )
            else:
                conn.execute(
                    f"INSERT INTO {pt} (link_id, user_id, tasks_completed, points_earned) "
                    f"VALUES (?,?,?,?)",
                    (link["link_id"], user_id, json.dumps(sorted(tasks)), points),
                )
                conn.execute(
                    f"UPDATE users SET {pc} = {pc} + ? WHERE user_id=?",
                    (points, user_id),
                )

        self.session.submitted[index] = points
        self.session.skipped.discard(index)
        self.session.index += 1

        if self.session.is_done():
            active_sessions.pop(self.user_id, None)
            self.stop()
            await interaction.response.edit_message(embed=self.build_embed(), view=None)
        else:
            self._rebuild()
            await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def on_timeout(self):
        active_sessions.pop(self.user_id, None)


# ── submit modal ───────────────────────────────────────────────────────────────

class SubmitLinkModal(discord.ui.Modal, title="Submit Your Tweet 🔗"):
    tweet_link = discord.ui.TextInput(
        label="Tweet Link",
        placeholder="https://x.com/yourhandle/status/123456789",
        min_length=20,
        max_length=300,
        style=discord.TextStyle.short,
    )

    def __init__(self, ctx: PoolCtx):
        super().__init__(title="Submit Your Tweet 🔗")
        self.ctx = ctx

    async def on_submit(self, interaction: discord.Interaction):
        ctx = self.ctx
        url = self.tweet_link.value.strip()
        guild_id = interaction.guild_id

        if not validate_tweet_link(url):
            await interaction.response.send_message(
                "❌ Invalid tweet link. Use format: `https://x.com/user/status/123456`",
                ephemeral=True,
            )
            return

        cost = get_cfg_int(guild_id, f"{ctx.cfg_prefix}_submit_cost", 0)
        if cost > 0:
            with get_connection() as conn:
                user = conn.execute(
                    f"SELECT {ctx.points_col} FROM users WHERE user_id=?",
                    (interaction.user.id,)
                ).fetchone()
            current_pts = user[ctx.points_col] if user else 0
            if current_pts < cost:
                await interaction.response.send_message(
                    f"❌ Need **{cost} engage points** to submit. You have **{current_pts}**.\n"
                    f"Use `/engage` to earn more first.",
                    ephemeral=True,
                )
                return

        now = datetime.now(timezone.utc).isoformat()
        with get_connection() as conn:
            dup = conn.execute(
                f"SELECT link_id FROM {ctx.links_table} WHERE tweet_link=? AND active=1 AND expires_at>?",
                (url, now),
            ).fetchone()

        if dup:
            await interaction.response.send_message(
                "⚠️ This tweet is already in the engage pool.", ephemeral=True
            )
            return

        lifetime = get_cfg_int(guild_id, f"{ctx.cfg_prefix}_link_lifetime_hours", 24)
        expires = datetime.now(timezone.utc) + timedelta(hours=lifetime)

        upsert_user(interaction.user.id, str(interaction.user))
        with get_connection() as conn:
            conn.execute(
                f"INSERT INTO {ctx.links_table} (user_id, tweet_link, source, expires_at) VALUES (?,?,?,?)",
                (interaction.user.id, url, "submit", expires.isoformat()),
            )
            if cost > 0:
                conn.execute(
                    f"UPDATE users SET {ctx.points_col} = {ctx.points_col} - ? WHERE user_id=?",
                    (cost, interaction.user.id),
                )

        embed = discord.Embed(
            title="tweet submitted. 🔗",
            description=(
                f"Your tweet is now in the engage pool.\n\n"
                f"**Link:** {url}\n"
                f"**Expires in:** {lifetime}h\n"
                + (f"**Cost:** -{cost} engage pts\n" if cost > 0 else "")
                + "\nothers will engage with it. let's grow together. 🔥"
            ),
            color=0x94730D,
        )
        embed.set_thumbnail(url=LOGO_URL)
        embed.set_footer(text=f"AmeretaVerse • {ctx.label}")
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ── cog ────────────────────────────────────────────────────────────────────────

class EngageCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.cleanup_expired.start()

    def cog_unload(self):
        self.cleanup_expired.cancel()

    @app_commands.command(name="engage", description="Get tweets to engage with and earn points")
    async def engage(self, interaction: discord.Interaction):
        pool = get_pool(interaction)
        guild_id = interaction.guild_id

        if (
            not isinstance(interaction.channel, discord.TextChannel)
            or interaction.channel.name != pool.channel_name
        ):
            await interaction.response.send_message(
                f"❌ Use `/engage` in the **#{pool.channel_name}** channel only.",
                ephemeral=True,
            )
            return

        with get_connection() as conn:
            user_row = conn.execute(
                "SELECT x_username FROM users WHERE user_id=?", (interaction.user.id,)
            ).fetchone()
        if not user_row or not user_row["x_username"]:
            await interaction.response.send_message(
                "⚠️ you need to link your X account first, habibi. use /setx to set your username.",
                ephemeral=True,
            )
            return

        daily_limit = get_cfg_int(guild_id, f"{pool.cfg_prefix}_daily_limit", 0)
        if daily_limit > 0:
            today_start = datetime.now(timezone.utc).replace(
                hour=0, minute=0, second=0, microsecond=0
            ).isoformat()
            with get_connection() as conn:
                today_count = conn.execute(
                    f"SELECT COUNT(*) as c FROM {pool.part_table} "
                    f"WHERE user_id=? AND confirmed_at>=?",
                    (interaction.user.id, today_start),
                ).fetchone()
            if today_count and today_count["c"] >= daily_limit:
                await interaction.response.send_message(
                    f"⏳ You've hit your daily limit (**{daily_limit}** today). "
                    f"Come back tomorrow, habibi.",
                    ephemeral=True,
                )
                return

        limit = get_cfg_int(guild_id, f"{pool.cfg_prefix}_links_per_request", 10)
        links = get_available_links(interaction.user.id, limit, pool)

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
            embed.set_footer(text=f"AmeretaVerse • {pool.label}")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        session = EngageSession(
            links=[dict(lnk) for lnk in links],
            ctx=pool,
            guild_id=guild_id,
        )
        active_sessions[interaction.user.id] = session

        lines = []
        for i, lnk in enumerate(links, 1):
            handle = extract_twitter_handle(lnk["tweet_link"])
            source_tag = "📌" if lnk["source"] == "raid" else "🔗"
            lines.append(f"**{i}.** {source_tag} `@{handle}`\n{lnk['tweet_link']}")

        footer_legend = "📌 raid  •  🔗 user submitted" if not pool.is_creator else "🔗 creator submitted"
        embed = discord.Embed(
            title=f"engage time. ⚡ — {len(links)} tweets ready",
            description="\n\n".join(lines),
            color=0x94730D,
        )
        embed.set_thumbnail(url=LOGO_URL)
        embed.set_footer(text=f"AmeretaVerse • {pool.label} | {footer_legend}")

        view = EngageListView(interaction.user.id, session)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @app_commands.command(name="submit", description="Submit your tweet for others to engage with")
    async def submit(self, interaction: discord.Interaction):
        pool = get_pool(interaction)

        if (
            not isinstance(interaction.channel, discord.TextChannel)
            or interaction.channel.name != pool.channel_name
        ):
            await interaction.response.send_message(
                f"❌ Use `/submit` in the **#{pool.channel_name}** channel only.",
                ephemeral=True,
            )
            return

        modal = SubmitLinkModal(pool)
        await interaction.response.send_modal(modal)

    @app_commands.command(name="engage-stats", description="View your engage points and stats")
    @app_commands.describe(member="Member to look up (defaults to yourself)")
    async def engage_stats(
        self, interaction: discord.Interaction, member: discord.Member = None
    ):
        pool = get_pool(interaction)
        target = member or interaction.user

        with get_connection() as conn:
            user = conn.execute(
                f"SELECT {pool.points_col} FROM users WHERE user_id=?", (target.id,)
            ).fetchone()
            done_count = conn.execute(
                f"SELECT COUNT(*) as c FROM {pool.part_table} WHERE user_id=?", (target.id,)
            ).fetchone()
            sub_count = conn.execute(
                f"SELECT COUNT(*) as c FROM {pool.links_table} WHERE user_id=?", (target.id,)
            ).fetchone()

        points = user[pool.points_col] if user else 0
        name = "your" if target == interaction.user else f"{target.display_name}'s"
        embed = discord.Embed(title=f"{name} engage stats ⚡", color=0x94730D)
        embed.add_field(name="Engage Points",   value=f"`{points} pts`",                          inline=True)
        embed.add_field(name="Tweets Engaged",  value=f"`{done_count['c'] if done_count else 0}`", inline=True)
        embed.add_field(name="Links Submitted", value=f"`{sub_count['c'] if sub_count else 0}`",   inline=True)
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.set_footer(text=f"AmeretaVerse • {pool.label}")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="engage-leaderboard", description="Top 10 users by engage points")
    async def engage_leaderboard(self, interaction: discord.Interaction):
        pool = get_pool(interaction)

        with get_connection() as conn:
            rows = conn.execute(
                f"SELECT username, {pool.points_col} AS pts FROM users "
                f"WHERE {pool.points_col} > 0 ORDER BY {pool.points_col} DESC LIMIT 10"
            ).fetchall()

        if not rows:
            await interaction.response.send_message(
                "No engage activity yet. Be the first — use `/engage`. ⚡"
            )
            return

        medals = ["🥇", "🥈", "🥉"]
        lines = [
            f"{medals[i] if i < 3 else f'`{i+1}.`'} **{r['username']}** — `{r['pts']} pts`"
            for i, r in enumerate(rows)
        ]
        embed = discord.Embed(
            title=f"⚡ {pool.label} Leaderboard",
            description="\n".join(lines),
            color=0x94730D,
        )
        embed.set_thumbnail(url=LOGO_URL)
        embed.set_footer(text=f"AmeretaVerse • {pool.label}")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="engage-config", description="View or change engage settings (admin)")
    @app_commands.describe(setting="Which setting to change", value="New value")
    @app_commands.choices(setting=[
        app_commands.Choice(name="Link lifetime (hours)",          value="engage_link_lifetime_hours"),
        app_commands.Choice(name="Links per /engage",              value="engage_links_per_request"),
        app_commands.Choice(name="Daily limit (0=unlimited)",      value="engage_daily_limit"),
        app_commands.Choice(name="Submit cost (points)",           value="engage_submit_cost"),
        app_commands.Choice(name="Points per link",                value="engage_points_per_link"),
        app_commands.Choice(name="Weight: Like %",                 value="engage_weight_like"),
        app_commands.Choice(name="Weight: Comment %",              value="engage_weight_comment"),
        app_commands.Choice(name="Weight: Retweet %",              value="engage_weight_retweet"),
        app_commands.Choice(name="[Creator] Link lifetime (hours)",value="creator_engage_link_lifetime_hours"),
        app_commands.Choice(name="[Creator] Links per /engage",    value="creator_engage_links_per_request"),
        app_commands.Choice(name="[Creator] Points per link",      value="creator_engage_points_per_link"),
        app_commands.Choice(name="[Creator] Weight: Like %",       value="creator_engage_weight_like"),
        app_commands.Choice(name="[Creator] Weight: Comment %",    value="creator_engage_weight_comment"),
        app_commands.Choice(name="[Creator] Weight: Retweet %",    value="creator_engage_weight_retweet"),
    ])
    @app_commands.default_permissions(administrator=True)
    async def engage_config(
        self,
        interaction: discord.Interaction,
        setting: app_commands.Choice[str] = None,
        value: str = None,
    ):
        guild_id = interaction.guild_id

        if setting and value:
            _db_set_config(guild_id, setting.value, value)
            await interaction.response.send_message(
                f"✅ **{setting.name}** set to `{value}`.", ephemeral=True
            )
        else:
            cfg = _db_get_all_config(guild_id)
            lines = [
                f"`{k}` = **{v}**"
                for k, v in sorted(cfg.items())
                if k.startswith("engage_") or k.startswith("creator_engage_")
            ]
            embed = discord.Embed(
                title="⚙️ Engage Config",
                description="\n".join(lines) or "No settings found.",
                color=0x94730D,
            )
            embed.set_footer(text="AmeretaVerse • Engage Config | Use /engage-config to change")
            await interaction.response.send_message(embed=embed, ephemeral=True)

    @tasks.loop(hours=1)
    async def cleanup_expired(self):
        now = datetime.now(timezone.utc).isoformat()
        with get_connection() as conn:
            conn.execute(
                "UPDATE engage_links SET active=0 WHERE active=1 AND expires_at<=?", (now,)
            )
            conn.execute(
                "UPDATE creator_engage_links SET active=0 WHERE active=1 AND expires_at<=?", (now,)
            )

    @cleanup_expired.before_loop
    async def before_cleanup(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(EngageCog(bot))
