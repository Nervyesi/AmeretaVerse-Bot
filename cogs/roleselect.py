import discord
from discord.ext import commands

from database import get_button, get_buttons, get_panel
from cogs._utils import resolve_role

BRAND = 0x94730D


# ── Ephemeral confirmation view (not persistent — lives for one interaction) ───

class ConfirmRoleView(discord.ui.View):
    def __init__(self, role: discord.Role, intent: str, dm_enabled: bool, dm_message: str):
        super().__init__(timeout=60)
        self.role = role
        self.intent = intent
        self.dm_enabled = dm_enabled
        self.dm_message = dm_message

    @discord.ui.button(label='Yes', style=discord.ButtonStyle.success, emoji='✅')
    async def yes_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        member = interaction.user
        try:
            if self.intent == 'give':
                await member.add_roles(self.role)
                action = 'given'
            else:
                await member.remove_roles(self.role)
                action = 'removed'
        except discord.Forbidden:
            await interaction.response.edit_message(
                content="❌ I don't have permission to manage that role.", embed=None, view=None
            )
            return
        except Exception as e:
            await interaction.response.edit_message(
                content=f'❌ Failed to update role: {e}', embed=None, view=None
            )
            return

        await interaction.response.edit_message(
            content=f'✅ Role **{self.role.name}** {action}.', embed=None, view=None
        )

        if self.dm_enabled:
            msg = self.dm_message.replace('{role}', self.role.name).replace(
                '{server}', interaction.guild.name
            )
            try:
                await member.send(msg)
            except (discord.Forbidden, discord.HTTPException):
                pass

    @discord.ui.button(label='No', style=discord.ButtonStyle.secondary, emoji='↩️')
    async def no_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content='Cancelled.', embed=None, view=None)


# ── Core button-click handler ─────────────────────────────────────────────────

async def handle_button_click(
    interaction: discord.Interaction, panel_id: int, button_id: int
):
    btn = get_button(button_id)
    if not btn:
        await interaction.response.send_message(
            '❌ This button is no longer configured. Contact an admin.', ephemeral=True
        )
        return

    role = resolve_role(interaction.guild, btn['role'])
    if role is None:
        await interaction.response.send_message(
            '❌ This role is not configured properly. Contact an admin.', ephemeral=True
        )
        return

    member = interaction.user
    has_role = role in member.roles
    mode = btn['mode']

    if mode == 'give':
        if has_role:
            await interaction.response.send_message(
                f'You already have the **{role.name}** role.', ephemeral=True
            )
            return
        intent = 'give'
    elif mode == 'take':
        if not has_role:
            await interaction.response.send_message(
                f"You don't have the **{role.name}** role.", ephemeral=True
            )
            return
        intent = 'take'
    else:  # toggle
        intent = 'take' if has_role else 'give'

    if intent == 'give':
        confirm_enabled = btn['confirm_give_enabled']
        confirm_message = btn['confirm_give_message']
        dm_enabled = bool(btn['dm_give_enabled'])
        dm_message = btn['dm_give_message']
    else:
        confirm_enabled = btn['confirm_take_enabled']
        confirm_message = btn['confirm_take_message']
        dm_enabled = bool(btn['dm_take_enabled'])
        dm_message = btn['dm_take_message']

    if confirm_enabled:
        await interaction.response.send_message(
            confirm_message or 'Are you sure?',
            view=ConfirmRoleView(role, intent, dm_enabled, dm_message),
            ephemeral=True,
        )
        return

    try:
        if intent == 'give':
            await member.add_roles(role)
            action = 'given'
        else:
            await member.remove_roles(role)
            action = 'removed'
    except discord.Forbidden:
        await interaction.response.send_message(
            "❌ I don't have permission to manage that role.", ephemeral=True
        )
        return
    except Exception as e:
        await interaction.response.send_message(
            f'❌ Failed to update role: {e}', ephemeral=True
        )
        return

    await interaction.response.send_message(
        f'✅ Role **{role.name}** {action}.', ephemeral=True
    )

    if dm_enabled:
        msg = dm_message.replace('{role}', role.name).replace(
            '{server}', interaction.guild.name
        )
        try:
            await member.send(msg)
        except (discord.Forbidden, discord.HTTPException):
            pass


# ── Persistent DynamicItem — button style ─────────────────────────────────────

class RoleSelectButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r'rs:b:(?P<panel_id>\d+):(?P<button_id>\d+)',
):
    def __init__(
        self,
        panel_id: int,
        button_id: int,
        label: str,
        emoji: str,
        style: discord.ButtonStyle,
    ):
        super().__init__(
            discord.ui.Button(
                label=label,
                emoji=emoji or None,
                style=style,
                custom_id=f'rs:b:{panel_id}:{button_id}',
            )
        )
        self.panel_id = panel_id
        self.button_id = button_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        panel_id = int(match['panel_id'])
        button_id = int(match['button_id'])
        btn = get_button(button_id)
        if not btn:
            return cls(panel_id, button_id, 'Removed', '', discord.ButtonStyle.secondary)
        return cls(
            panel_id,
            button_id,
            btn['label'],
            btn['emoji'] or '',
            discord.ButtonStyle.primary,
        )

    async def callback(self, interaction: discord.Interaction):
        await handle_button_click(interaction, self.panel_id, self.button_id)


# ── Persistent DynamicItem — dropdown style ───────────────────────────────────

class RoleSelectDropdown(
    discord.ui.DynamicItem[discord.ui.Select],
    template=r'rs:d:(?P<panel_id>\d+)',
):
    def __init__(self, panel_id: int, options: list, button_map: dict):
        super().__init__(
            discord.ui.Select(
                placeholder='Select a role...',
                options=options,
                custom_id=f'rs:d:{panel_id}',
                max_values=1,
            )
        )
        self.panel_id = panel_id
        self.button_map = button_map

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        panel_id = int(match['panel_id'])
        buttons = get_buttons(panel_id)
        options = []
        button_map = {}
        for btn in buttons:
            val = str(btn['button_id'])
            options.append(discord.SelectOption(
                label=btn['label'],
                value=val,
                emoji=btn['emoji'] or None,
            ))
            button_map[val] = btn['button_id']
        if not options:
            options = [discord.SelectOption(label='No roles configured', value='__none__')]
        return cls(panel_id, options, button_map)

    async def callback(self, interaction: discord.Interaction):
        selected = self.values[0] if self.values else None
        if not selected or selected == '__none__' or selected not in self.button_map:
            await interaction.response.send_message('❌ Invalid selection.', ephemeral=True)
            return
        button_id = self.button_map[selected]
        await handle_button_click(interaction, self.panel_id, button_id)


# ── View builder (used by API send/refresh endpoints) ─────────────────────────

def build_panel_view(panel_id: int) -> discord.ui.View:
    panel = get_panel(panel_id)
    view = discord.ui.View(timeout=None)
    if not panel:
        return view

    buttons = get_buttons(panel_id)

    if panel['style'] == 'dropdown':
        options = []
        button_map = {}
        for btn in buttons:
            val = str(btn['button_id'])
            options.append(discord.SelectOption(
                label=btn['label'],
                value=val,
                emoji=btn['emoji'] or None,
            ))
            button_map[val] = btn['button_id']
        if options:
            view.add_item(RoleSelectDropdown(panel_id, options, button_map))
    else:
        for btn in buttons:
            view.add_item(RoleSelectButton(
                panel_id=panel_id,
                button_id=btn['button_id'],
                label=btn['label'],
                emoji=btn['emoji'] or '',
                style=discord.ButtonStyle.primary,
            ))

    return view


# ── Cog ───────────────────────────────────────────────────────────────────────

class RoleSelectCog(commands.Cog, name='RoleSelect'):
    def __init__(self, bot):
        self.bot = bot


async def setup(bot):
    await bot.add_cog(RoleSelectCog(bot))
    bot.add_dynamic_items(RoleSelectButton)
    bot.add_dynamic_items(RoleSelectDropdown)
