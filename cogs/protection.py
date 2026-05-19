"""
protection.py — AmeretaVerse Auto-Moderation Module
Handles: link detection, spam, suspicious users, phishing, anti-raid, banned words.
All settings stored in the per-guild config table with protection_ prefix.
"""

import re
import time
import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from database import (
    get_connection,
    get_config as _db_get_config,
    set_config as _db_set_config,
    log_event,
)
from config import DEFAULT_BOT_THUMBNAIL_URL

# Severity by protection action type — anything missing falls back to 'warning'.
_PROTECTION_SEVERITY = {
    'spam_mute':         'warning',
    'phishing_delete':   'error',
    'link_delete':       'warning',
    'banned_word':       'warning',
    'suspicious_flag':   'warning',
    'suspicious_kick':   'error',
    'suspicious_ban':    'critical',
    'raid_lockdown':     'critical',
    'raid_kick_new':     'critical',
    'raid_ban_new':      'critical',
}

URL_RE = re.compile(
    r"(https?://|www\.)\S+|"
    r"\b(?:[a-zA-Z0-9\-]+\.)+(?:com|io|xyz|net|org|gg|app|co|ru|site|club|info|biz|me)\b",
    re.IGNORECASE,
)


# ── Per-guild config helpers ──────────────────────────────────────────────────

def cfg_get(guild_id: int, key: str, default: str = "") -> str:
    val = _db_get_config(guild_id, key)
    return val if val is not None else default


def cfg_set(guild_id: int, key: str, value: str):
    _db_set_config(guild_id, key, value)


def cfg_bool(guild_id: int, key: str, default: bool = True) -> bool:
    val = cfg_get(guild_id, key, "1" if default else "0")
    return val.strip().lower() in ("1", "true", "yes", "on")


def cfg_int(guild_id: int, key: str, default: int) -> int:
    try:
        return int(cfg_get(guild_id, key, str(default)))
    except ValueError:
        return default


def cfg_list(guild_id: int, key: str, default: str = "") -> list[str]:
    raw = cfg_get(guild_id, key, default)
    return [x.strip().lower() for x in raw.split(",") if x.strip()]


# ── Channel / role resolution by name OR ID ───────────────────────────────────

def resolve_channel(guild: discord.Guild, value: str) -> discord.TextChannel | None:
    if not value or not value.strip():
        return None
    v = value.strip()
    try:
        return guild.get_channel(int(v))
    except (ValueError, TypeError):
        pass
    return discord.utils.get(guild.text_channels, name=v)


def resolve_role(guild: discord.Guild, value: str) -> discord.Role | None:
    if not value or not value.strip():
        return None
    v = value.strip()
    try:
        return guild.get_role(int(v))
    except (ValueError, TypeError):
        pass
    return discord.utils.get(guild.roles, name=v)


# ── DB: log protection actions ────────────────────────────────────────────────

def log_action(guild_id: int, action_type: str, user_id: int, detail: str, *, username: str = None):
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO protection_actions
               (guild_id, action_type, user_id, detail)
               VALUES (?,?,?,?)""",
            (guild_id, action_type, user_id, detail),
        )

    log_event(
        guild_id, 'protection', action_type,
        f'Protection: {action_type} — {detail}',
        target_user_id=user_id, target_username=username,
        module='protection',
        severity=_PROTECTION_SEVERITY.get(action_type, 'warning'),
        details={'action': action_type, 'detail': detail},
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def extract_domains(text: str) -> list[str]:
    urls = URL_RE.findall(text)
    domains = []
    for u in urls:
        u = re.sub(r"^https?://", "", u, flags=re.I)
        u = re.sub(r"^www\.", "", u, flags=re.I)
        domain = u.split("/")[0].lower()
        domains.append(domain)
    return domains


async def get_mod_log(guild: discord.Guild) -> discord.TextChannel | None:
    value = cfg_get(guild.id, "protection_log_channel", "mod-log")
    return resolve_channel(guild, value)


async def send_mod_log(guild: discord.Guild, embed: discord.Embed):
    ch = await get_mod_log(guild)
    if ch:
        try:
            await ch.send(embed=embed)
        except discord.Forbidden:
            pass


def protection_embed(title: str, description: str, color: int = 0x94730D) -> discord.Embed:
    e = discord.Embed(title=title, description=description, color=color,
                      timestamp=datetime.now(timezone.utc))
    e.set_thumbnail(url=DEFAULT_BOT_THUMBNAIL_URL)
    e.set_footer(text="AmeretaVerse • Protection System")
    return e


# ── Protection Cog ────────────────────────────────────────────────────────────

class ProtectionCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._spam_buckets: dict[int, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
        self._join_buckets: dict[int, list[float]] = defaultdict(list)
        self._raid_locked: set[int] = set()

    # ── DM helper ─────────────────────────────────────────────────────────────

    async def _send_dm(self, member: discord.Member, template_key: str, **fmt_kwargs):
        guild_id = member.guild.id
        if not cfg_bool(guild_id, "protection_dm_on_action", default=False):
            return
        template = cfg_get(guild_id, template_key, "")
        if not template.strip():
            return
        try:
            await member.send(template.format(**fmt_kwargs))
        except (discord.Forbidden, discord.HTTPException):
            pass

    # ── Action helper ─────────────────────────────────────────────────────────

    async def _execute_member_action(self, member: discord.Member, action: str, reason: str):
        if action == "kick":
            try:
                await member.kick(reason=reason)
            except discord.Forbidden:
                pass
        elif action == "ban":
            try:
                await member.ban(reason=reason, delete_message_days=1)
            except discord.Forbidden:
                pass

    # ── Event: message ────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or message.author.bot:
            return
        if message.author.guild_permissions.administrator:
            return

        await self._check_spam(message)
        await self._check_links(message)
        await self._check_banned_words(message)

    # ── Spam detection ────────────────────────────────────────────────────────

    async def _check_spam(self, message: discord.Message):
        guild_id = message.guild.id
        if not cfg_bool(guild_id, "protection_spam_detection"):
            return

        threshold = cfg_int(guild_id, "protection_spam_threshold", 5)
        window    = cfg_int(guild_id, "protection_spam_window",    10)
        user_id   = message.author.id
        now       = time.monotonic()

        bucket = self._spam_buckets[guild_id][user_id]
        bucket.append(now)
        self._spam_buckets[guild_id][user_id] = [t for t in bucket if now - t <= window]

        if len(self._spam_buckets[guild_id][user_id]) >= threshold:
            self._spam_buckets[guild_id][user_id].clear()
            action   = cfg_get(guild_id, "protection_spam_action", "mute")
            duration = cfg_int(guild_id, "protection_spam_mute_duration", 600)
            reason   = f"Spam detection: {threshold}+ msgs in {window}s"

            if action == "mute":
                await self._mute_user(message.guild, message.author, reason, duration)
            else:
                await self._execute_member_action(message.author, action, reason)

            log_action(guild_id, "spam_mute", user_id, f"Sent {threshold}+ msgs in {window}s")
            await self._send_dm(message.author, "protection_dm_spam_message", duration=duration)
            embed = protection_embed(
                "🚫 Spam Detected",
                f"**User:** {message.author.mention} (`{message.author}`)\n"
                f"**Reason:** {threshold}+ messages in {window} seconds\n"
                f"**Action:** `{action}`\n"
                f"**Channel:** {message.channel.mention}",
                color=0xFF6600,
            )
            await send_mod_log(message.guild, embed)

    async def _mute_user(self, guild: discord.Guild, member: discord.Member,
                         reason: str, duration_seconds: int = 600):
        try:
            until = datetime.now(timezone.utc) + timedelta(seconds=duration_seconds)
            await member.timeout(until, reason=reason)
            return
        except (discord.Forbidden, AttributeError):
            pass
        mute_role_value = cfg_get(guild.id, "protection_mute_role", "Muted")
        mute_role = resolve_role(guild, mute_role_value)
        if not mute_role:
            try:
                mute_role = await guild.create_role(
                    name=mute_role_value, reason="Auto-created by AVbot protection"
                )
                for ch in guild.channels:
                    try:
                        await ch.set_permissions(mute_role, send_messages=False, speak=False)
                    except discord.Forbidden:
                        pass
            except discord.Forbidden:
                return
        try:
            await member.add_roles(mute_role, reason=reason)
        except discord.Forbidden:
            pass

    # ── Link / phishing detection ─────────────────────────────────────────────

    async def _check_links(self, message: discord.Message):
        text = message.content
        if not URL_RE.search(text):
            return

        domains = extract_domains(text)
        if not domains:
            return

        guild_id = message.guild.id

        # 1. Phishing check
        if cfg_bool(guild_id, "protection_phishing_detection"):
            phishing_domains = set(cfg_list(
                guild_id, "protection_phishing_list",
                "discorcl.com,discord-nitro.com"
            ))
            for d in domains:
                if d in phishing_domains:
                    phishing_action = cfg_get(guild_id, "protection_phishing_action", "delete")
                    try:
                        await message.delete()
                    except discord.NotFound:
                        pass
                    log_action(guild_id, "phishing_delete", message.author.id, d)
                    await self._send_dm(message.author, "protection_dm_phishing_message")
                    if phishing_action not in ("delete", "warn"):
                        await self._execute_member_action(
                            message.author, phishing_action,
                            f"Phishing link detected: {d}"
                        )
                    embed = protection_embed(
                        "⚠️ Phishing Link Deleted",
                        f"**User:** {message.author.mention} (`{message.author}`)\n"
                        f"**Domain:** `{d}`\n"
                        f"**Action:** `{phishing_action}`\n"
                        f"**Channel:** {message.channel.mention}",
                        color=0xFF0000,
                    )
                    await send_mod_log(message.guild, embed)
                    return

        # 2. General link detection
        if not cfg_bool(guild_id, "protection_link_detection"):
            return

        whitelist_raw = cfg_get(guild_id, "protection_link_whitelist",
                                "twitter.com,x.com,discord.gg,youtube.com")
        whitelist = {d.strip().lower() for d in whitelist_raw.split(",") if d.strip()}

        for d in domains:
            if any(d == w or d.endswith("." + w) for w in whitelist):
                continue
            link_action = cfg_get(guild_id, "protection_link_action", "delete")
            try:
                await message.delete()
            except discord.NotFound:
                pass
            log_action(guild_id, "link_delete", message.author.id, d)
            await self._send_dm(message.author, "protection_dm_link_message")
            if link_action not in ("delete", "warn"):
                await self._execute_member_action(
                    message.author, link_action, f"Non-whitelisted link: {d}"
                )
            embed = protection_embed(
                "🔗 Link Removed",
                f"**User:** {message.author.mention} (`{message.author}`)\n"
                f"**Domain:** `{d}`\n"
                f"**Action:** `{link_action}`\n"
                f"**Channel:** {message.channel.mention}",
                color=0xFFAA00,
            )
            await send_mod_log(message.guild, embed)
            return

    # ── Banned words filter ────────────────────────────────────────────────────

    async def _check_banned_words(self, message: discord.Message):
        guild_id = message.guild.id
        if not cfg_bool(guild_id, "protection_banned_words"):
            return

        words_raw = cfg_get(guild_id, "protection_banned_words_list", "")
        if not words_raw.strip():
            return

        banned = [w.strip().lower() for w in words_raw.split(",") if w.strip()]
        content_lower = message.content.lower()

        for word in banned:
            if word in content_lower:
                banned_action = cfg_get(guild_id, "protection_banned_words_action", "delete")
                try:
                    await message.delete()
                except discord.NotFound:
                    pass
                log_action(guild_id, "banned_word", message.author.id, word)
                await self._send_dm(message.author, "protection_dm_banned_word_message")
                if banned_action not in ("delete", "warn"):
                    await self._execute_member_action(
                        message.author, banned_action, f"Banned word: {word}"
                    )
                embed = protection_embed(
                    "🚫 Banned Word Removed",
                    f"**User:** {message.author.mention} (`{message.author}`)\n"
                    f"**Matched:** `{word}`\n"
                    f"**Action:** `{banned_action}`\n"
                    f"**Channel:** {message.channel.mention}",
                    color=0xFF6600,
                )
                await send_mod_log(message.guild, embed)
                return

    # ── Event: member join ────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        await self._check_suspicious(member)
        await self._check_anti_raid(member)

    # ── Suspicious user detection ─────────────────────────────────────────────

    async def _check_suspicious(self, member: discord.Member):
        guild_id = member.guild.id
        if not cfg_bool(guild_id, "protection_suspicious_users"):
            return

        flags: list[str] = []

        if cfg_bool(guild_id, "protection_suspicious_no_avatar", True) and member.avatar is None:
            flags.append("No profile picture (default avatar)")

        min_age_days = cfg_int(guild_id, "protection_suspicious_account_age", 7)
        if min_age_days > 0:
            age = datetime.now(timezone.utc) - member.created_at
            if age < timedelta(days=min_age_days):
                flags.append(f"Account created {age.days}d ago (minimum: {min_age_days}d)")

        if cfg_bool(guild_id, "protection_suspicious_username_keywords", True):
            scam_words = cfg_list(
                guild_id, "protection_suspicious_keywords_list",
                "admin,mod,moderator,support,giveaway,airdrop,staff,team"
            )
            name_lower = (member.name + " " + (member.display_name or "")).lower()
            matched = [w for w in scam_words if w in name_lower]
            if matched:
                flags.append(f"Suspicious name keywords: {', '.join(matched)}")

        if not flags:
            return

        action = cfg_get(guild_id, "protection_suspicious_action", "flag")
        guild  = member.guild

        log_action(guild.id, f"suspicious_{action}", member.id, "; ".join(flags))

        embed = protection_embed(
            "🚨 Suspicious User Detected",
            f"**User:** {member.mention} (`{member}`)\n"
            f"**User ID:** `{member.id}`\n"
            f"**Flags:**\n" + "\n".join(f"• {f}" for f in flags) +
            f"\n\n**Action taken:** `{action}`",
            color=0xFF3300,
        )
        await send_mod_log(guild, embed)
        await self._send_dm(member, "protection_dm_suspicious_message")

        if action in ("kick", "ban"):
            await self._execute_member_action(member, action, "Suspicious user — auto-moderation")

    # ── Anti-raid ─────────────────────────────────────────────────────────────

    async def _check_anti_raid(self, member: discord.Member):
        guild_id = member.guild.id
        if not cfg_bool(guild_id, "protection_anti_raid"):
            return

        threshold = cfg_int(guild_id, "protection_anti_raid_threshold", 10)
        window    = cfg_int(guild_id, "protection_anti_raid_window",    60)
        now       = time.monotonic()

        bucket = self._join_buckets[guild_id]
        bucket.append(now)
        self._join_buckets[guild_id] = [t for t in bucket if now - t <= window]

        if len(self._join_buckets[guild_id]) >= threshold and guild_id not in self._raid_locked:
            self._raid_locked.add(guild_id)
            self._join_buckets[guild_id].clear()
            await self._lockdown(member.guild, threshold, window)

    async def _lockdown(self, guild: discord.Guild, threshold: int, window: int):
        log_action(guild.id, "anti_raid_lockdown", 0, f"{threshold} joins in {window}s")

        try:
            invites = await guild.invites()
            for inv in invites:
                try:
                    await inv.delete(reason="Anti-raid lockdown")
                except discord.Forbidden:
                    pass
        except discord.Forbidden:
            pass

        embed = protection_embed(
            "🚨 RAID DETECTED — Server Locked",
            f"**Trigger:** {threshold}+ users joined within {window} seconds.\n\n"
            "**Actions taken:**\n"
            "• All invite links have been paused\n"
            "• Admins have been notified\n\n"
            "Use `/protection-unlock` to restore normal access after the threat has been assessed.",
            color=0xFF0000,
        )
        await send_mod_log(guild, embed)

        ch = await get_mod_log(guild)
        if ch:
            admin_mentions = " ".join(
                m.mention for m in guild.members
                if not m.bot and m.guild_permissions.administrator
            )
            if admin_mentions:
                try:
                    await ch.send(f"🚨 **RAID ALERT** — admins: {admin_mentions}")
                except discord.Forbidden:
                    pass

    # ── Admin commands ────────────────────────────────────────────────────────

    @app_commands.command(
        name="protection-config",
        description="[Admin] View or toggle protection module settings.",
    )
    @app_commands.describe(
        feature="Which feature to configure",
        enabled="Enable or disable the feature",
        value="String value to set (for non-boolean settings)",
    )
    @app_commands.choices(feature=[
        app_commands.Choice(name="link-detection",     value="protection_link_detection"),
        app_commands.Choice(name="spam-detection",     value="protection_spam_detection"),
        app_commands.Choice(name="suspicious-users",   value="protection_suspicious_users"),
        app_commands.Choice(name="phishing-detection", value="protection_phishing_detection"),
        app_commands.Choice(name="anti-raid",          value="protection_anti_raid"),
        app_commands.Choice(name="banned-words",       value="protection_banned_words"),
        app_commands.Choice(name="link-whitelist",     value="protection_link_whitelist"),
        app_commands.Choice(name="banned-words-list",  value="protection_banned_words_list"),
        app_commands.Choice(name="suspicious-action",  value="protection_suspicious_action"),
        app_commands.Choice(name="spam-threshold",     value="protection_spam_threshold"),
        app_commands.Choice(name="anti-raid-threshold",value="protection_anti_raid_threshold"),
        app_commands.Choice(name="show-all",           value="show_all"),
    ])
    @app_commands.checks.has_permissions(administrator=True)
    async def protection_config(
        self,
        interaction: discord.Interaction,
        feature: str,
        enabled: bool | None = None,
        value: str | None = None,
    ):
        guild_id = interaction.guild_id

        if feature == "show_all":
            await self._show_config(interaction)
            return

        if enabled is not None:
            cfg_set(guild_id, feature, "1" if enabled else "0")
            state = "✅ Enabled" if enabled else "❌ Disabled"
            embed = protection_embed(
                "⚙️ Protection Config Updated",
                f"**Feature:** `{feature}`\n**Status:** {state}",
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if value is not None:
            cfg_set(guild_id, feature, value)
            embed = protection_embed(
                "⚙️ Protection Config Updated",
                f"**Feature:** `{feature}`\n**Value:** `{value}`",
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        current = cfg_get(guild_id, feature, "(not set)")
        embed = protection_embed(
            "⚙️ Protection Config",
            f"**Feature:** `{feature}`\n**Current value:** `{current}`",
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _show_config(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id

        def row(label: str, key: str, is_bool: bool = True) -> str:
            if is_bool:
                v = "✅ On" if cfg_bool(guild_id, key) else "❌ Off"
            else:
                v = f"`{cfg_get(guild_id, key, '—')}`"
            return f"**{label}:** {v}"

        lines = [
            row("Link Detection",        "protection_link_detection"),
            row("Spam Detection",        "protection_spam_detection"),
            row("Suspicious Users",      "protection_suspicious_users"),
            row("Phishing Detection",    "protection_phishing_detection"),
            row("Anti-Raid",             "protection_anti_raid"),
            row("Banned Words",          "protection_banned_words"),
            "",
            row("Link Action",           "protection_link_action",         is_bool=False),
            row("Spam Action",           "protection_spam_action",         is_bool=False),
            row("Phishing Action",       "protection_phishing_action",     is_bool=False),
            row("Suspicious Action",     "protection_suspicious_action",   is_bool=False),
            row("Anti-Raid Action",      "protection_anti_raid_action",    is_bool=False),
            row("Spam Threshold",        "protection_spam_threshold",      is_bool=False),
            row("Anti-Raid Threshold",   "protection_anti_raid_threshold", is_bool=False),
            row("Link Whitelist",        "protection_link_whitelist",      is_bool=False),
            row("DM on Action",          "protection_dm_on_action"),
        ]
        embed = protection_embed("⚙️ Protection Module — Current Config", "\n".join(lines))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(
        name="protection-unlock",
        description="[Admin] Lift anti-raid lockdown and re-enable server access.",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def protection_unlock(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id
        if guild_id in self._raid_locked:
            self._raid_locked.discard(guild_id)
            embed = protection_embed(
                "🔓 Lockdown Lifted",
                "The anti-raid lockdown has been removed. You can now re-create invite links.",
                color=0x3ba55c,
            )
        else:
            embed = protection_embed(
                "ℹ️ No Active Lockdown",
                "The server is not currently in raid-lockdown mode.",
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(
        name="protection-stats",
        description="[Admin] Show protection action counts.",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def protection_stats(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id
        with get_connection() as conn:
            rows = conn.execute(
                """SELECT action_type, COUNT(*) as cnt
                   FROM protection_actions
                   WHERE guild_id=?
                   GROUP BY action_type
                   ORDER BY cnt DESC""",
                (guild_id,),
            ).fetchall()

        if not rows:
            desc = "No protection actions recorded yet."
        else:
            desc = "\n".join(f"**{r['action_type']}:** {r['cnt']}" for r in rows)

        embed = protection_embed("🛡️ Protection Stats", desc)
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(ProtectionCog(bot))
