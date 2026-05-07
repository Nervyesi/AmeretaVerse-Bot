"""
forms.py — Generic form builder cog.

Admins define forms via the Dashboard.  Each form has:
  - An embed panel with a single "Apply" button
  - A list of fields (short_text, long_text, number, dropdown)
  - Submit behaviour: ticket category, staff roles, ping role
  - Approve action: optional role + optional DM
  - Reject action: optional DM

Flow:
  1. User clicks Apply → FormApplyButton.callback
  2. Text/number fields are presented in Discord modals (5 per modal, chained)
  3. Dropdown fields are presented in a follow-up ephemeral SelectMenu view
  4. On completion → ticket channel created with answers embed + Approve/Reject/Close buttons
  5. Staff Approves → role granted, DM sent, channel deleted
     Staff Rejects → DM sent, channel deleted
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
    create_form_submission,
    get_submission,
    update_submission_status,
    get_connection,
)
from cogs._utils import resolve_category, resolve_role
from cogs._branding import build_branded_embed


# ── In-memory multi-step modal state ─────────────────────────────────────────
# key: (user_id, form_id)  value: {'answers': {str: str}, 'created_at': datetime}
_pending_state: dict = {}

VALID_FIELD_TYPES = {'short_text', 'long_text', 'number', 'dropdown'}


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


def _safe_channel_name(user_name: str, submission_id: int) -> str:
    slug = ''.join(
        c for c in user_name.lower().replace(' ', '-')
        if c.isascii() and (c.isalnum() or c == '-')
    )[:16] or 'user'
    return f'form-{slug}-{submission_id:04d}'


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

    # Number validation
    for f in fields:
        if f['field_type'] == 'number':
            val = str(answers.get(str(f['field_id']), '')).strip()
            if val:
                try:
                    int(val)
                except ValueError:
                    await _safe_ephemeral(
                        interaction,
                        f'❌ **{f["label"]}** must be a whole number. Submission cancelled.'
                    )
                    return

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

    # Persist stub submission
    answers_json = json.dumps(answers)
    submission_id = create_form_submission(
        form_id=form['form_id'],
        guild_id=guild_id,
        user_id=user.id,
        username=user.name,
        channel_id='0',
        answers=answers_json,
    )

    # Build channel
    channel_name = _safe_channel_name(user.name, submission_id)
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
    embed.set_footer(text=f'Submission #{submission_id:04d} | User ID: {user.id}')

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


# ── DropdownStepView — ephemeral select menus for dropdown fields ─────────────

class DropdownStepView(discord.ui.View):
    def __init__(self, form: dict, all_fields: list, dropdown_fields: list, prior_answers: dict):
        super().__init__(timeout=600)
        self.form = form
        self.all_fields = all_fields
        self.dropdown_fields = dropdown_fields
        self.prior_answers = dict(prior_answers)
        self.dropdown_answers: dict = {}

        for f in dropdown_fields:
            try:
                opts = json.loads(f.get('options') or '[]')
            except Exception:
                opts = []
            options = [
                discord.SelectOption(label=str(o)[:100], value=str(o)[:100])
                for o in opts[:25]
            ] or [discord.SelectOption(label='(No options configured)', value='__none__')]

            sel = discord.ui.Select(
                placeholder=f.get('label', 'Select…')[:100],
                options=options,
                custom_id=f"dd_{f['field_id']}",
                min_values=0 if not f.get('required') else 1,
                max_values=1,
            )
            sel.callback = self._make_select_cb(str(f['field_id']))
            self.add_item(sel)

        submit_btn = discord.ui.Button(
            label='Submit', style=discord.ButtonStyle.success, emoji='✅',
        )
        submit_btn.callback = self._on_submit
        self.add_item(submit_btn)

    def _make_select_cb(self, field_id: str):
        async def _cb(interaction: discord.Interaction):
            selected = interaction.data.get('values', [])
            if selected and selected[0] != '__none__':
                self.dropdown_answers[field_id] = selected[0]
            await interaction.response.defer()
        return _cb

    async def _on_submit(self, interaction: discord.Interaction):
        # Required dropdown check
        for f in self.dropdown_fields:
            fid = str(f['field_id'])
            if f.get('required') and fid not in self.dropdown_answers:
                await interaction.response.send_message(
                    f'❌ Please select a value for **{f["label"]}**.', ephemeral=True
                )
                return

        final_answers = dict(self.prior_answers)
        final_answers.update(self.dropdown_answers)
        self.stop()
        await interaction.response.defer(ephemeral=True)
        await _finalize_submission(interaction, self.form, final_answers, self.all_fields)


# ── FormModal — dynamically built from field definitions ──────────────────────

class FormModal(discord.ui.Modal):
    def __init__(
        self,
        form: dict,
        all_fields: list,
        text_fields: list,
        dropdown_fields: list,
        step: int = 0,
        prior_answers: dict = None,
    ):
        self.form = form
        self.all_fields = all_fields
        self.text_fields = text_fields
        self.dropdown_fields = dropdown_fields
        self.step = step
        self.prior_answers = prior_answers or {}

        step_count = (len(text_fields) + 4) // 5
        title = (form.get('name') or 'Application')[:40]
        if step_count > 1:
            title = f'{title} ({step + 1}/{step_count})'

        super().__init__(title=title[:45])

        chunk = text_fields[step * 5: step * 5 + 5]
        for f in chunk:
            ftype = f.get('field_type', 'short_text')
            style = (discord.TextStyle.paragraph
                     if ftype == 'long_text' else discord.TextStyle.short)
            label = f['label'][:40]
            if ftype == 'number':
                label = f'{label} (number)'[:45]
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

        step_count = (len(self.text_fields) + 4) // 5
        next_step = self.step + 1

        if next_step < step_count:
            # More text modals
            await interaction.response.send_modal(
                FormModal(
                    self.form, self.all_fields,
                    self.text_fields, self.dropdown_fields,
                    step=next_step, prior_answers=answers,
                )
            )
        elif self.dropdown_fields:
            # Dropdown step
            view = DropdownStepView(
                self.form, self.all_fields, self.dropdown_fields, answers
            )
            await interaction.response.send_message(
                'Almost done! Make your selections below, then click **Submit**.',
                view=view,
                ephemeral=True,
            )
        else:
            # Finalize
            await interaction.response.defer(ephemeral=True)
            await _finalize_submission(interaction, self.form, answers, self.all_fields)

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

        text_fields = [f for f in fields if f['field_type'] != 'dropdown']
        dropdown_fields = [f for f in fields if f['field_type'] == 'dropdown']

        if text_fields:
            await interaction.response.send_modal(
                FormModal(form, fields, text_fields, dropdown_fields)
            )
        elif dropdown_fields:
            view = DropdownStepView(form, fields, dropdown_fields, {})
            await interaction.response.send_message(
                'Make your selections below, then click **Submit**.',
                view=view,
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                '❌ This form has no fields configured.', ephemeral=True
            )


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

        await interaction.response.defer()
        update_submission_status(self.submission_id, guild.id, 'approved', interaction.user.id)

        # Give approve role
        member = guild.get_member(sub['user_id'])
        approve_val = (form.get('approve_role') or '').strip()
        if member and approve_val:
            role = resolve_role(guild, approve_val)
            if role:
                try:
                    await member.add_roles(role)
                except (discord.Forbidden, discord.HTTPException):
                    pass

        # DM on approve
        if member and form.get('approve_dm_enabled') and form.get('approve_dm_message'):
            dm_embed = build_branded_embed(
                guild.id,
                description=form['approve_dm_message'].replace('{server}', guild.name),
                cog_prefix='forms',
                use_thumbnail=True, use_image=False, use_footer=True,
            )
            try:
                await member.send(embed=dm_embed)
            except (discord.Forbidden, discord.HTTPException):
                pass

        result_embed = discord.Embed(
            title='✅ Application Approved',
            description=f'Approved by {interaction.user.mention}',
            color=0x3ba55c,
        )
        await interaction.followup.send(embed=result_embed)
        await asyncio.sleep(5)
        try:
            await interaction.channel.delete()
        except Exception:
            pass


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

        await interaction.response.defer()
        update_submission_status(self.submission_id, guild.id, 'rejected', interaction.user.id)

        # DM on reject
        member = guild.get_member(sub['user_id'])
        if member and form.get('reject_dm_enabled') and form.get('reject_dm_message'):
            dm_embed = build_branded_embed(
                guild.id,
                description=form['reject_dm_message'].replace('{server}', guild.name),
                cog_prefix='forms',
                use_thumbnail=True, use_image=False, use_footer=True,
            )
            try:
                await member.send(embed=dm_embed)
            except (discord.Forbidden, discord.HTTPException):
                pass

        result_embed = discord.Embed(
            title='❌ Application Rejected',
            description=f'Rejected by {interaction.user.mention}',
            color=0xed4245,
        )
        await interaction.followup.send(embed=result_embed)
        await asyncio.sleep(5)
        try:
            await interaction.channel.delete()
        except Exception:
            pass


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
