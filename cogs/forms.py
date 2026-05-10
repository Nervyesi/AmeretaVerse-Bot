"""
forms.py — Generic form builder cog.

Admins define forms via the Dashboard.  Each form has:
  - An embed panel with a single "Apply" button
  - A list of fields (short_text, long_text only)
  - Submit behaviour: ticket category, staff roles, ping role
  - Approve action: optional role + optional DM
  - Reject action: optional DM
  - auto_close_on_decision: close channel on approve/reject (default on)

Flow:
  1. User clicks Apply → FormApplyButton.callback
  2. Fields are presented in Discord modals (5 per modal, chained for >5 fields)
  3. On completion → ticket channel created with answers embed + Approve/Reject/Close buttons
  4. Staff Approves → role granted, DM sent; channel deleted or left open per form config
     Staff Rejects → DM sent; channel deleted or left open per form config
     Staff Closes  → channel deleted, status = expired
"""

import re
import json
import asyncio
from datetime import datetime, timezone, timedelta

import discord
from discord.ext import commands, tasks

from database import (
    get_form,
    list_form_fields,
    has_pending_submission,
    get_submission,
    update_submission_status,
    get_connection,
)
from cogs._utils import resolve_category, resolve_role
from cogs._branding import build_branded_embed


# ── In-memory multi-step modal state ─────────────────────────────────────────
# key: (user_id, form_id)  value: {'answers': {str: str}, 'created_at': datetime}
_pending_state: dict = {}

VALID_FIELD_TYPES = {'short_text', 'long_text'}


def _cleanup_stale():
    now = datetime.now(timezone.utc)
    stale = [k for k, v in _pending_state.items()
             if now - v['created_at'] > timedelta(minutes=10)]
    for k in stale:
        _pending_state.pop(k, None)


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _safe_ephemeral(interaction: discord.Interaction, msg: str):
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        pass


def _build_panel_embed(guild_id: int, form: dict) -> discord.Embed:
    embed = build_branded_embed(
        guild_id,
        title=form.get('title') or form.get('name') or 'Application',
        description=form.get('description') or '',
        cog_prefix='forms',
        use_thumbnail=not bool(form.get('thumbnail_url')),
        use_image=not bool(form.get('image_url')),
        use_footer=not bool(form.get('footer_text')),
    )
    if form.get('thumbnail_url'):
        embed.set_thumbnail(url=form['thumbnail_url'])
    if form.get('image_url'):
        embed.set_image(url=form['image_url'])
    if form.get('footer_text'):
        embed.set_footer(text=form['footer_text'])
    if form.get('color'):
        try:
            embed.color = discord.Color(int(form['color'].lstrip('#'), 16))
        except (ValueError, AttributeError):
            pass
    return embed


def _safe_channel_name(user_name: str, display_num: int) -> str:
    slug = ''.join(
        c for c in user_name.lower().replace(' ', '-')
        if c.isascii() and (c.isalnum() or c == '-')
    )[:16] or 'user'
    return f'form-{slug}-{display_num:04d}'


# ── Finalize: create staff ticket channel ────────────────────────────────────

async def _finalize_submission(
    interaction: discord.Interaction,
    form: dict,
    answers: dict,
    fields: list,
):
    guild = interaction.guild
    guild_id = guild.id
    user = interaction.user

    # Resolve category
    cat_val = (form.get('ticket_category') or '').strip()
    category = resolve_category(guild, cat_val) if cat_val else None
    if category is None:
        await _safe_ephemeral(
            interaction,
            '❌ This form is misconfigured (ticket category not found). Contact an admin.'
        )
        return

    # Resolve staff roles
    staff_roles = []
    for r_str in (form.get('staff_roles') or '').split(','):
        r_str = r_str.strip()
        if r_str:
            role = resolve_role(guild, r_str)
            if role:
                staff_roles.append(role)

    # Persist stub submission with per-guild display_number (atomic in one connection)
    answers_json = json.dumps(answers)
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO form_submissions
               (form_id, guild_id, user_id, username, channel_id, answers, status)
               VALUES (?, ?, ?, ?, ?, ?, 'pending')""",
            (form['form_id'], guild_id, user.id, user.name, '0', answers_json),
        )
        submission_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        row = conn.execute(
            "SELECT COALESCE(MAX(display_number), 0) + 1 AS next_num "
            "FROM form_submissions WHERE guild_id=?",
            (guild_id,),
        ).fetchone()
        submission_display = row['next_num']
        conn.execute(
            "UPDATE form_submissions SET display_number=? WHERE submission_id=?",
            (submission_display, submission_id),
        )

    # Build channel
    channel_name = _safe_channel_name(user.name, submission_display)
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        user: discord.PermissionOverwrite(
            read_messages=True, send_messages=True, embed_links=True,
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

    try:
        channel = await category.create_text_channel(name=channel_name, overwrites=overwrites)
    except Exception as e:
        await _safe_ephemeral(interaction, f'❌ Failed to create ticket channel: {e}')
        return

    # Persist real channel_id
    with get_connection() as conn:
        conn.execute(
            'UPDATE form_submissions SET channel_id=? WHERE submission_id=?',
            (str(channel.id), submission_id),
        )

    # Submission embed with answers
    embed = discord.Embed(
        title=f'📋 {form.get("name", "Application")} — {user.display_name}',
        color=0x94730D,
    )
    for f in fields:
        answer = str(answers.get(str(f['field_id']), '')).strip() or '*(not answered)*'
        embed.add_field(name=f['label'][:256], value=answer[:1024], inline=False)
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.set_footer(text=f'Submission #{submission_display:04d} | User ID: {user.id}')

    view = SubmissionTicketView(submission_id, form['form_id'])

    ping_raw = (form.get('ping_role') or '').strip()
    ping_role = resolve_role(guild, ping_raw) if ping_raw else None
    content_parts = ([ping_role.mention] if ping_role else []) + [user.mention]

    await channel.send(
        content=' '.join(content_parts),
        embed=embed,
        view=view,
        allowed_mentions=discord.AllowedMentions(roles=True, users=True),
    )

    await _safe_ephemeral(
        interaction,
        f'✅ Application submitted! Your ticket: {channel.mention}'
    )


# ── FormModal — dynamically built from field definitions ──────────────────────

class FormModal(discord.ui.Modal):
    def __init__(
        self,
        form: dict,
        fields: list,
        step: int = 0,
        prior_answers: dict = None,
    ):
        self.form = form
        self.fields = fields
        self.step = step
        self.prior_answers = prior_answers or {}

        step_count = (len(fields) + 4) // 5
        title = (form.get('name') or 'Application')[:40]
        if step_count > 1:
            title = f'{title} ({step + 1}/{step_count})'

        super().__init__(title=title[:45])

        chunk = fields[step * 5: step * 5 + 5]
        for f in chunk:
            ftype = f.get('field_type', 'short_text')
            style = (discord.TextStyle.paragraph
                     if ftype == 'long_text' else discord.TextStyle.short)
            label = f['label'][:40]
            max_len = f.get('max_length') or (4000 if ftype == 'long_text' else 1024)
            placeholder = (f.get('placeholder') or '')[:100] or None
            self.add_item(discord.ui.TextInput(
                label=label,
                placeholder=placeholder,
                required=bool(f.get('required', 1)),
                style=style,
                max_length=min(int(max_len), 4000),
                custom_id=f'ff_{f["field_id"]}',
            ))

    async def on_submit(self, interaction: discord.Interaction):
        _cleanup_stale()
        answers = dict(self.prior_answers)
        for child in self.children:
            if isinstance(child, discord.ui.TextInput):
                fid = child.custom_id.replace('ff_', '')
                answers[fid] = child.value or ''

        step_count = (len(self.fields) + 4) // 5
        next_step = self.step + 1

        if next_step < step_count:
            await interaction.response.send_modal(
                FormModal(
                    self.form, self.fields,
                    step=next_step, prior_answers=answers,
                )
            )
        else:
            await interaction.response.defer(ephemeral=True)
            await _finalize_submission(interaction, self.form, answers, self.fields)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        print(f'[forms] FormModal error: {type(error).__name__}: {error}')
        await _safe_ephemeral(interaction, '❌ Something went wrong processing your submission.')


# ── FormApplyButton — persistent DynamicItem on the panel embed ───────────────

class FormApplyButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r'forms:apply:(?P<fid>\d+)',
):
    def __init__(self, form_id: int, button_label: str = 'Apply'):
        super().__init__(
            discord.ui.Button(
                label=button_label[:80],
                style=discord.ButtonStyle.primary,
                emoji='📋',
                custom_id=f'forms:apply:{form_id}',
            )
        )
        self.form_id = form_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match, /):
        form_id = int(match['fid'])
        guild_id = interaction.guild_id or 0
        form = get_form(form_id, guild_id) if guild_id else None
        label = (form.get('button_label') or 'Apply') if form else 'Apply'
        return cls(form_id, label)

    async def callback(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id or 0
        user = interaction.user
        form = get_form(self.form_id, guild_id)

        if not form or not form.get('enabled', 1):
            await interaction.response.send_message(
                '❌ This form is currently unavailable.', ephemeral=True
            )
            return

        if has_pending_submission(self.form_id, user.id):
            await interaction.response.send_message(
                '⚠️ You already have a pending submission for this form. '
                'Wait for a staff decision before reapplying.',
                ephemeral=True,
            )
            return

        fields = list_form_fields(self.form_id)
        if not fields:
            await interaction.response.send_message(
                '❌ This form has no fields configured. Contact an admin.', ephemeral=True
            )
            return

        await interaction.response.send_modal(FormModal(form, fields))


# ── SubmissionTicketView — Approve / Reject / Close on the staff channel ──────

class FormApproveButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r'forms:approve:(?P<sid>\d+):(?P<fid>\d+)',
):
    def __init__(self, submission_id: int, form_id: int):
        super().__init__(
            discord.ui.Button(
                label='Approve', style=discord.ButtonStyle.success, emoji='✅',
                custom_id=f'forms:approve:{submission_id}:{form_id}',
            )
        )
        self.submission_id = submission_id
        self.form_id = form_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match['sid']), int(match['fid']))

    async def callback(self, interaction: discord.Interaction):
        guild = interaction.guild
        sub = get_submission(self.submission_id, guild.id)
        if not sub:
            await interaction.response.send_message('❌ Submission not found.', ephemeral=True)
            return
        if sub['status'] != 'pending':
            await interaction.response.send_message(
                f'❌ Already {sub["status"]}.', ephemeral=True
            )
            return

        form = get_form(self.form_id, guild.id)
        if not form:
            await interaction.response.send_message('❌ Form not found.', ephemeral=True)
            return

        # Staff permission check
        is_staff = interaction.user.guild_permissions.administrator
        if not is_staff:
            for r_str in (form.get('staff_roles') or '').split(','):
                role = resolve_role(guild, r_str.strip())
                if role and role in interaction.user.roles:
                    is_staff = True
                    break
        if not is_staff:
            await interaction.response.send_message(
                '❌ You do not have permission to decide on this submission.', ephemeral=True
            )
            return

        print(f'[forms] approve: form={form["form_id"]} sub={self.submission_id} '
              f'user={sub["user_id"]} dm_enabled={form.get("approve_dm_enabled")} '
              f'auto_close={form.get("auto_close_on_decision", 1)}')

        await interaction.response.defer()
        update_submission_status(self.submission_id, guild.id, 'approved', interaction.user.id)

        member = guild.get_member(sub['user_id'])

        # 1. Grant role
        approve_val = (form.get('approve_role') or '').strip()
        if approve_val and member:
            role = resolve_role(guild, approve_val)
            if role:
                try:
                    await member.add_roles(role, reason='Form approved')
                    print(f'[forms] approve: granted role {role.name} to {member.id}')
                except discord.Forbidden:
                    print(f'[forms] approve: missing perms to grant role {role.name}')
                except Exception as e:
                    print(f'[forms] approve: role grant failed {type(e).__name__}: {e}')

        # 2. Send DM
        if form.get('approve_dm_enabled') and member:
            msg_template = form.get('approve_dm_message') or 'Your application has been approved.'
            msg_text = msg_template.replace('{user}', member.mention).replace('{server}', guild.name)
            dm_embed = build_branded_embed(
                guild.id,
                title='✅ Application Approved',
                description=msg_text,
                cog_prefix='forms',
                use_thumbnail=True, use_image=False, use_footer=True,
            )
            try:
                await member.send(embed=dm_embed)
                print(f'[forms] approve: DM sent to {member.id}')
            except discord.Forbidden:
                print(f'[forms] approve: user {member.id} has DMs disabled')
            except Exception as e:
                print(f'[forms] approve: DM failed {type(e).__name__}: {e}')
        elif not form.get('approve_dm_enabled'):
            print(f'[forms] approve: DM disabled for form {form["form_id"]}')
        elif not member:
            print(f'[forms] approve: user {sub["user_id"]} not in guild')

        # 3. Post result and close/leave open
        result_embed = discord.Embed(
            title='✅ Application Approved',
            description=f'Approved by {interaction.user.mention}',
            color=0x3ba55c,
        )

        if form.get('auto_close_on_decision', 1):
            await interaction.followup.send(embed=result_embed)
            await asyncio.sleep(5)
            try:
                await interaction.channel.delete()
            except Exception:
                pass
        else:
            await interaction.followup.send(embed=result_embed)
            close_only_view = discord.ui.View(timeout=None)
            close_only_view.add_item(FormCloseButton(self.submission_id))
            try:
                await interaction.message.edit(view=close_only_view)
            except Exception as e:
                print(f'[forms] approve: could not update view: {e}')


class FormRejectButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r'forms:reject:(?P<sid>\d+):(?P<fid>\d+)',
):
    def __init__(self, submission_id: int, form_id: int):
        super().__init__(
            discord.ui.Button(
                label='Reject', style=discord.ButtonStyle.danger, emoji='❌',
                custom_id=f'forms:reject:{submission_id}:{form_id}',
            )
        )
        self.submission_id = submission_id
        self.form_id = form_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match['sid']), int(match['fid']))

    async def callback(self, interaction: discord.Interaction):
        guild = interaction.guild
        sub = get_submission(self.submission_id, guild.id)
        if not sub:
            await interaction.response.send_message('❌ Submission not found.', ephemeral=True)
            return
        if sub['status'] != 'pending':
            await interaction.response.send_message(
                f'❌ Already {sub["status"]}.', ephemeral=True
            )
            return

        form = get_form(self.form_id, guild.id)
        if not form:
            await interaction.response.send_message('❌ Form not found.', ephemeral=True)
            return

        is_staff = interaction.user.guild_permissions.administrator
        if not is_staff:
            for r_str in (form.get('staff_roles') or '').split(','):
                role = resolve_role(guild, r_str.strip())
                if role and role in interaction.user.roles:
                    is_staff = True
                    break
        if not is_staff:
            await interaction.response.send_message(
                '❌ You do not have permission to decide on this submission.', ephemeral=True
            )
            return

        print(f'[forms] reject: form={form["form_id"]} sub={self.submission_id} '
              f'user={sub["user_id"]} dm_enabled={form.get("reject_dm_enabled")} '
              f'auto_close={form.get("auto_close_on_decision", 1)}')

        await interaction.response.defer()
        update_submission_status(self.submission_id, guild.id, 'rejected', interaction.user.id)

        member = guild.get_member(sub['user_id'])

        # Send DM
        if form.get('reject_dm_enabled') and member:
            msg_template = form.get('reject_dm_message') or 'Your application has not been approved.'
            msg_text = msg_template.replace('{user}', member.mention).replace('{server}', guild.name)
            dm_embed = build_branded_embed(
                guild.id,
                title='❌ Application Rejected',
                description=msg_text,
                cog_prefix='forms',
                use_thumbnail=True, use_image=False, use_footer=True,
            )
            try:
                await member.send(embed=dm_embed)
                print(f'[forms] reject: DM sent to {member.id}')
            except discord.Forbidden:
                print(f'[forms] reject: user {member.id} has DMs disabled')
            except Exception as e:
                print(f'[forms] reject: DM failed {type(e).__name__}: {e}')
        elif not form.get('reject_dm_enabled'):
            print(f'[forms] reject: DM disabled for form {form["form_id"]}')
        elif not member:
            print(f'[forms] reject: user {sub["user_id"]} not in guild')

        result_embed = discord.Embed(
            title='❌ Application Rejected',
            description=f'Rejected by {interaction.user.mention}',
            color=0xed4245,
        )

        if form.get('auto_close_on_decision', 1):
            await interaction.followup.send(embed=result_embed)
            await asyncio.sleep(5)
            try:
                await interaction.channel.delete()
            except Exception:
                pass
        else:
            await interaction.followup.send(embed=result_embed)
            close_only_view = discord.ui.View(timeout=None)
            close_only_view.add_item(FormCloseButton(self.submission_id))
            try:
                await interaction.message.edit(view=close_only_view)
            except Exception as e:
                print(f'[forms] reject: could not update view: {e}')


class FormCloseButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r'forms:close:(?P<sid>\d+)',
):
    def __init__(self, submission_id: int):
        super().__init__(
            discord.ui.Button(
                label='Close', style=discord.ButtonStyle.secondary, emoji='🔒',
                custom_id=f'forms:close:{submission_id}',
            )
        )
        self.submission_id = submission_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match['sid']))

    async def callback(self, interaction: discord.Interaction):
        is_staff = interaction.user.guild_permissions.administrator
        if not is_staff:
            await interaction.response.send_message(
                '❌ Only staff can close this ticket.', ephemeral=True
            )
            return

        guild = interaction.guild
        update_submission_status(
            self.submission_id, guild.id, 'expired', interaction.user.id
        )
        await interaction.response.send_message('🔒 Closing in 5 seconds…')
        await asyncio.sleep(5)
        try:
            await interaction.channel.delete()
        except Exception:
            pass


class SubmissionTicketView(discord.ui.View):
    def __init__(self, submission_id: int, form_id: int):
        super().__init__(timeout=None)
        self.add_item(FormApproveButton(submission_id, form_id))
        self.add_item(FormRejectButton(submission_id, form_id))
        self.add_item(FormCloseButton(submission_id))


# ── Cog ───────────────────────────────────────────────────────────────────────

class FormsCog(commands.Cog, name='Forms'):
    def __init__(self, bot):
        self.bot = bot
        self._stale_cleanup.start()

    def cog_unload(self):
        self._stale_cleanup.cancel()

    @tasks.loop(minutes=5)
    async def _stale_cleanup(self):
        _cleanup_stale()

    @_stale_cleanup.before_loop
    async def before_cleanup(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(FormsCog(bot))
    bot.add_dynamic_items(FormApplyButton)
    bot.add_dynamic_items(FormApproveButton)
    bot.add_dynamic_items(FormRejectButton)
    bot.add_dynamic_items(FormCloseButton)
