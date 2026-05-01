import io
import re
import traceback
import discord
from discord import app_commands
from discord.ext import commands, tasks
from datetime import datetime, timezone, timedelta

from database import get_connection, get_config as db_get_config
from cogs._utils import resolve_channel, resolve_role, resolve_category

BRAND = 0x94730D


# ── Config helpers ─────────────────────────────────────────────────────────────

def _cfg(guild_id: int, key: str, default: str = '') -> str:
    val = db_get_config(guild_id, key)
    return val if val is not None else default


def _cfg_bool(guild_id: int, key: str, default: str = '1') -> bool:
    return _cfg(guild_id, key, default).strip() == '1'


def _cfg_int(guild_id: int, key: str, default: int) -> int:
    try:
        return int(_cfg(guild_id, key, str(default)) or str(default))
    except (ValueError, TypeError):
        return default


# ── Channel name sanitiser ────────────────────────────────────────────────────

def _safe_name(username: str) -> str:
    """Return a Discord-channel-safe segment from a username."""
    name = username.lower().replace(' ', '-')
    safe = ''.join(c for c in name if c.isascii() and (c.isalnum() or c == '-'))
    return safe[:20] or 'user'


# ── Close Button (DynamicItem — ticket_id embedded in custom_id) ───────────────

class CloseTicketDynamicButton(discord.ui.DynamicItem[discord.ui.Button],
                                template=r'tickets:close:(?P<tid>[0-9]+)'):
    def __init__(self, ticket_id: int):
        super().__init__(
            discord.ui.Button(
                label='Close Ticket',
                style=discord.ButtonStyle.danger,
                emoji='🔒',
                custom_id=f'tickets:close:{ticket_id}',
            )
        )
        self.ticket_id = ticket_id

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match,
    ):
        return cls(int(match.group('tid')))

    async def callback(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog('Tickets')
        if cog is None:
            await interaction.response.send_message(
                'Ticket system is currently unavailable.', ephemeral=True
            )
            return
        await cog._close_ticket(interaction, self.ticket_id, 'manual')


# ── Open Button View (persistent, fixed custom_id) ────────────────────────────

class OpenTicketView(discord.ui.View):
    def __init__(self, button_label: str = 'Open Ticket'):
        super().__init__(timeout=None)
        btn = discord.ui.Button(
            label=button_label,
            style=discord.ButtonStyle.primary,
            custom_id='tickets:open',
            emoji='🎟️',
        )
        btn.callback = self._on_open
        self.add_item(btn)

    async def _on_open(self, interaction: discord.Interaction):
        try:
            guild    = interaction.guild
            user     = interaction.user
            guild_id = guild.id

            if not _cfg_bool(guild_id, 'tickets_enabled', '0'):
                await interaction.response.send_message(
                    'Tickets are currently disabled.', ephemeral=True
                )
                return

            # One open ticket per user
            with get_connection() as conn:
                existing = conn.execute(
                    "SELECT ticket_id, channel_id FROM tickets "
                    "WHERE guild_id=? AND user_id=? AND status='open'",
                    (guild_id, user.id),
                ).fetchone()

            if existing:
                await interaction.response.send_message(
                    f"You already have an open ticket: <#{existing['channel_id']}>",
                    ephemeral=True,
                )
                return

            # Resolve category
            cat_val  = (_cfg(guild_id, 'tickets_category') or '').strip()
            category = resolve_category(guild, cat_val)
            if category is None:
                await interaction.response.send_message(
                    'Ticket system is misconfigured (category not found). '
                    'Please contact an admin.',
                    ephemeral=True,
                )
                return

            # Resolve staff roles
            staff_raw  = _cfg(guild_id, 'tickets_staff_roles') or ''
            staff_roles = []
            for r_str in staff_raw.split(','):
                r_str = r_str.strip()
                if r_str:
                    role = resolve_role(guild, r_str)
                    if role:
                        staff_roles.append(role)

            # Defer so we have time for channel creation
            await interaction.response.defer(ephemeral=True)

            # INSERT stub row to get ticket_id before naming the channel
            with get_connection() as conn:
                conn.execute(
                    "INSERT INTO tickets (guild_id, channel_id, user_id, username, status) "
                    "VALUES (?, 0, ?, ?, 'open')",
                    (guild_id, user.id, user.name),
                )
                ticket_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]

            # Permission overwrites
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(read_messages=False),
                user: discord.PermissionOverwrite(
                    read_messages=True, send_messages=True,
                    embed_links=True, attach_files=True,
                ),
                guild.me: discord.PermissionOverwrite(
                    read_messages=True, send_messages=True,
                    manage_channels=True, manage_messages=True,
                ),
            }
            for role in staff_roles:
                overwrites[role] = discord.PermissionOverwrite(
                    read_messages=True, send_messages=True, manage_messages=True,
                )

            channel_name = f'ticket-{_safe_name(user.name)}-{ticket_id:04d}'

            try:
                channel = await category.create_text_channel(
                    name=channel_name, overwrites=overwrites
                )
            except Exception as e:
                with get_connection() as conn:
                    conn.execute('DELETE FROM tickets WHERE ticket_id=?', (ticket_id,))
                await interaction.followup.send(
                    f'Failed to create ticket channel: {e}', ephemeral=True
                )
                return

            # Persist real channel_id
            with get_connection() as conn:
                conn.execute(
                    'UPDATE tickets SET channel_id=? WHERE ticket_id=?',
                    (channel.id, ticket_id),
                )

            # Welcome embed
            welcome_tmpl = _cfg(
                guild_id, 'tickets_welcome_message',
                'Hi {user}, thanks for opening a ticket. A staff member will be with you shortly.',
            )
            embed = discord.Embed(
                title=f'Ticket #{ticket_id:04d}',
                description=welcome_tmpl.replace('{user}', user.mention),
                color=BRAND,
            )
            embed.set_footer(text=f'AmeretaVerse • Support Tickets | ID: {ticket_id}')

            # Ping content
            ping_raw  = (_cfg(guild_id, 'tickets_ping_role') or '').strip()
            ping_role = resolve_role(guild, ping_raw) if ping_raw else None
            parts     = ([ping_role.mention] if ping_role else []) + [user.mention]

            close_view = discord.ui.View(timeout=None)
            close_view.add_item(CloseTicketDynamicButton(ticket_id))

            await channel.send(
                content=' '.join(parts),
                embed=embed,
                view=close_view,
                allowed_mentions=discord.AllowedMentions(roles=True, users=True),
            )

            # DM on open
            if _cfg_bool(guild_id, 'tickets_dm_on_open_enabled'):
                dm_tmpl = _cfg(
                    guild_id, 'tickets_dm_on_open_message',
                    "Your support ticket has been opened in {server}. We'll be in touch soon.",
                )
                try:
                    await user.send(dm_tmpl.replace('{server}', guild.name))
                except (discord.Forbidden, discord.HTTPException):
                    pass

            await interaction.followup.send(
                f'✅ Ticket created: {channel.mention}', ephemeral=True
            )

        except Exception as e:
            print(f'[tickets] _on_open error: {type(e).__name__}: {e}')
            traceback.print_exc()
            try:
                if interaction.response.is_done():
                    await interaction.followup.send('An unexpected error occurred opening your ticket.', ephemeral=True)
                else:
                    await interaction.response.send_message('An unexpected error occurred opening your ticket.', ephemeral=True)
            except Exception:
                pass


# ── Tickets Cog ────────────────────────────────────────────────────────────────

class Tickets(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.inactivity_check.start()

    def cog_unload(self):
        self.inactivity_check.cancel()

    # ── Core close logic (shared by button, command, and auto-close) ─────────

    async def _close_ticket(
        self,
        interaction: discord.Interaction | None,
        ticket_id: int,
        close_reason: str = 'manual',
    ):
        with get_connection() as conn:
            ticket = conn.execute(
                "SELECT * FROM tickets WHERE ticket_id=? AND status='open'",
                (ticket_id,),
            ).fetchone()

        if ticket is None:
            if interaction:
                msg = 'Ticket not found or already closed.'
                if interaction.response.is_done():
                    await interaction.followup.send(msg, ephemeral=True)
                else:
                    await interaction.response.send_message(msg, ephemeral=True)
            return

        guild_id   = ticket['guild_id']
        channel_id = ticket['channel_id']
        user_id    = ticket['user_id']

        # Manual close — permission check
        if close_reason == 'manual' and interaction:
            guild     = interaction.guild
            user      = interaction.user
            is_opener = (user.id == user_id)
            is_staff  = user.guild_permissions.administrator

            if not is_opener and not is_staff:
                staff_raw = _cfg(guild_id, 'tickets_staff_roles') or ''
                for r_str in staff_raw.split(','):
                    role = resolve_role(guild, r_str.strip())
                    if role and role in user.roles:
                        is_staff = True
                        break

            if not is_opener and not is_staff:
                await interaction.response.send_message(
                    'Only the ticket opener or staff can close this ticket.',
                    ephemeral=True,
                )
                return

            await interaction.response.send_message('🔒 Closing ticket...')

        # Mark closed in DB
        closed_by = (
            (interaction.user.id if interaction else None)
            or (self.bot.user.id if self.bot.user else 0)
        )
        now_utc = datetime.now(timezone.utc).isoformat()
        with get_connection() as conn:
            conn.execute(
                "UPDATE tickets SET status='closed', closed_at=?, closed_by=?, "
                "close_reason=? WHERE ticket_id=?",
                (now_utc, closed_by, close_reason, ticket_id),
            )

        guild   = self.bot.get_guild(guild_id)
        channel = self.bot.get_channel(channel_id)

        # Archive transcript (best-effort)
        archive_val = (_cfg(guild_id, 'tickets_archive_channel') or '').strip()
        if archive_val and guild and channel:
            archive_ch = resolve_channel(guild, archive_val)
            if archive_ch:
                try:
                    msgs  = [m async for m in channel.history(limit=100, oldest_first=True)]
                    lines = []
                    for m in msgs:
                        ts   = m.created_at.strftime('%Y-%m-%d %H:%M UTC')
                        body = m.content or ('[embed]' if m.embeds else '[attachment]')
                        lines.append(f'[{ts}] {m.author.display_name}: {body}')
                    transcript = '\n'.join(lines) or 'No messages.'

                    arch_embed = discord.Embed(
                        title=f'Ticket #{ticket_id:04d} Closed',
                        description=(
                            f'**User:** <@{user_id}>\n'
                            f'**Reason:** {close_reason}\n'
                            f'**Closed:** {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}'
                        ),
                        color=BRAND,
                    )
                    buf = io.BytesIO(transcript.encode('utf-8'))
                    buf.seek(0)
                    await archive_ch.send(
                        embed=arch_embed,
                        file=discord.File(buf, filename=f'ticket-{ticket_id:04d}.txt'),
                    )
                except Exception:
                    pass

        # DM on close (best-effort)
        if guild and _cfg_bool(guild_id, 'tickets_dm_on_close_enabled'):
            dm_tmpl = _cfg(
                guild_id, 'tickets_dm_on_close_message',
                'Your support ticket in {server} has been closed.',
            )
            member = guild.get_member(user_id)
            if member:
                try:
                    await member.send(dm_tmpl.replace('{server}', guild.name))
                except (discord.Forbidden, discord.HTTPException):
                    pass

        # Delete channel
        if channel:
            try:
                closer = (
                    interaction.user.display_name
                    if (interaction and close_reason == 'manual')
                    else 'auto-close'
                )
                await channel.delete(reason=f'Ticket closed by {closer} ({close_reason})')
            except (discord.NotFound, discord.Forbidden, Exception):
                pass

    # ── Activity reset on message ────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        with get_connection() as conn:
            row = conn.execute(
                "SELECT ticket_id FROM tickets WHERE channel_id=? AND status='open'",
                (message.channel.id,),
            ).fetchone()
        if row:
            with get_connection() as conn:
                conn.execute(
                    "UPDATE tickets SET last_activity_at=CURRENT_TIMESTAMP, warned_at=NULL "
                    "WHERE ticket_id=?",
                    (row['ticket_id'],),
                )

    # ── Inactivity check (every 15 minutes) ─────────────────────────────────

    @tasks.loop(minutes=15)
    async def inactivity_check(self):
        now = datetime.now(timezone.utc)

        with get_connection() as conn:
            open_tickets = conn.execute(
                "SELECT * FROM tickets WHERE status='open'"
            ).fetchall()

        for ticket in open_tickets:
            guild_id  = ticket['guild_id']
            ticket_id = ticket['ticket_id']

            if not _cfg_bool(guild_id, 'tickets_enabled', '0'):
                continue
            if not _cfg_bool(guild_id, 'tickets_auto_close_enabled'):
                continue

            warning_hours = _cfg_int(guild_id, 'tickets_auto_close_warning_hours', 48)
            final_hours   = _cfg_int(guild_id, 'tickets_auto_close_final_hours',   72)

            try:
                last_activity = datetime.fromisoformat(
                    ticket['last_activity_at']
                ).replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                continue

            inactive_for = now - last_activity

            if ticket['warned_at'] is None:
                if inactive_for >= timedelta(hours=warning_hours):
                    channel = self.bot.get_channel(ticket['channel_id'])
                    if channel:
                        warn_msg = _cfg(
                            guild_id,
                            'tickets_auto_close_warning_message',
                            'This ticket has been inactive and will be auto-closed soon.',
                        )
                        try:
                            await channel.send(warn_msg)
                        except Exception:
                            pass
                    with get_connection() as conn:
                        conn.execute(
                            'UPDATE tickets SET warned_at=? WHERE ticket_id=?',
                            (now.isoformat(), ticket_id),
                        )
            else:
                if inactive_for >= timedelta(hours=final_hours):
                    await self._close_ticket(None, ticket_id, 'auto_inactivity')

    @inactivity_check.before_loop
    async def before_inactivity_check(self):
        await self.bot.wait_until_ready()

    # ── /tickets-panel ───────────────────────────────────────────────────────

    @app_commands.command(
        name='tickets-panel',
        description='Send the support ticket panel to the configured channel.',
    )
    @app_commands.default_permissions(administrator=True)
    async def tickets_panel_cmd(self, interaction: discord.Interaction):
        guild    = interaction.guild
        guild_id = guild.id

        panel_ch_val = (_cfg(guild_id, 'tickets_panel_channel') or '').strip()
        if not panel_ch_val:
            await interaction.response.send_message(
                'tickets_panel_channel is not configured.', ephemeral=True
            )
            return

        channel = resolve_channel(guild, panel_ch_val)
        if channel is None:
            await interaction.response.send_message(
                f'Channel not found: {panel_ch_val}', ephemeral=True
            )
            return

        title   = _cfg(guild_id, 'tickets_panel_title',       'Support Tickets')
        desc    = _cfg(guild_id, 'tickets_panel_description',  'Click below to open a ticket.')
        btn_lbl = _cfg(guild_id, 'tickets_panel_button_label', 'Open Ticket')

        embed = discord.Embed(title=title, description=desc, color=BRAND)
        embed.set_footer(text='AmeretaVerse • Support Tickets')

        view = OpenTicketView(button_label=btn_lbl)
        await channel.send(embed=embed, view=view)
        await interaction.response.send_message(
            f'✅ Panel sent to {channel.mention}', ephemeral=True
        )

    # ── /tickets-stats ───────────────────────────────────────────────────────

    @app_commands.command(
        name='tickets-stats',
        description='Show ticket statistics for this server.',
    )
    @app_commands.default_permissions(administrator=True)
    async def tickets_stats_cmd(self, interaction: discord.Interaction):
        guild_id = interaction.guild.id
        with get_connection() as conn:
            open_count = conn.execute(
                "SELECT COUNT(*) FROM tickets WHERE guild_id=? AND status='open'",
                (guild_id,),
            ).fetchone()[0]
            total_count = conn.execute(
                'SELECT COUNT(*) FROM tickets WHERE guild_id=?',
                (guild_id,),
            ).fetchone()[0]

        embed = discord.Embed(title='Ticket Statistics', color=BRAND)
        embed.add_field(name='Open',   value=str(open_count),              inline=True)
        embed.add_field(name='Closed', value=str(total_count - open_count), inline=True)
        embed.add_field(name='Total',  value=str(total_count),             inline=True)
        embed.set_footer(text=f'AmeretaVerse • {interaction.guild.name}')
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    cog = Tickets(bot)
    await bot.add_cog(cog)
    bot.add_view(OpenTicketView())
    bot.add_dynamic_items(CloseTicketDynamicButton)
