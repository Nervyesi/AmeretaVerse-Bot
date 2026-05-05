import traceback
import random
import string
import io

import discord
from discord.ext import commands
from captcha.image import ImageCaptcha

from database import get_config as db_get_config
from cogs._utils import resolve_role, resolve_channel
from cogs._branding import build_branded_embed

user_attempts = {}
user_captcha = {}


def _cfg(guild_id, key, default=''):
    val = db_get_config(guild_id, key)
    return val if val is not None else default


def _cfg_bool(guild_id, key, default='0'):
    return (_cfg(guild_id, key, default) or default).strip() == '1'


def _cfg_int(guild_id, key, default=3):
    try:
        return int(_cfg(guild_id, key, str(default)) or str(default))
    except (ValueError, TypeError):
        return default


def generate_captcha():
    characters = string.ascii_uppercase + string.digits
    characters = characters.replace('O', '').replace('0', '').replace('I', '').replace('1', '')
    code = ''.join(random.choices(characters, k=5))
    image = ImageCaptcha(width=280, height=90)
    data = image.generate(code)
    image_bytes = io.BytesIO(data.read())
    image_bytes.seek(0)
    return code, image_bytes


class CaptchaModal(discord.ui.Modal, title="Identity Check 🔍"):
    answer = discord.ui.TextInput(
        label="Enter the code from the image",
        placeholder="Type the 5 characters you see...",
        min_length=5,
        max_length=5,
        style=discord.TextStyle.short
    )

    def __init__(self, user_id):
        super().__init__()
        self.user_id = user_id

    async def on_submit(self, interaction: discord.Interaction):
        try:
            if interaction.user.id != self.user_id:
                await interaction.response.send_message(
                    "❌ This is not your verification!", ephemeral=True
                )
                return

            guild_id = interaction.guild.id
            max_attempts = _cfg_int(guild_id, 'verify_max_attempts', 3)
            correct_code = user_captcha.get(self.user_id, "")
            entered = self.answer.value.upper().strip()

            if entered == correct_code:
                user_attempts.pop(self.user_id, None)
                user_captcha.pop(self.user_id, None)

                role = resolve_role(interaction.guild, _cfg(guild_id, 'verify_success_role', 'Verified'))
                if role is None:
                    await interaction.response.edit_message(
                        content="✅ Correct! But the verification role is not configured. Contact an admin.",
                        view=None, attachments=[]
                    )
                    return

                try:
                    await interaction.user.add_roles(role)
                except discord.Forbidden:
                    await interaction.response.edit_message(
                        content="❌ I don't have permission to assign the verification role. Contact an admin.",
                        view=None, attachments=[]
                    )
                    return

                if _cfg_bool(guild_id, 'verify_dm_on_success_enabled'):
                    dm_msg = _cfg(guild_id, 'verify_dm_on_success_message',
                                  'Welcome! You have been verified in {server}.')
                    dm_embed = build_branded_embed(
                        guild_id,
                        description=dm_msg.replace('{server}', interaction.guild.name),
                        cog_prefix='verify',
                        use_thumbnail=True,
                        use_image=False,
                        use_footer=True,
                    )
                    try:
                        await interaction.user.send(embed=dm_embed)
                    except (discord.Forbidden, discord.HTTPException):
                        pass

                success_title = _cfg(guild_id, 'verify_success_message', '✅ Verified! Welcome to the server.')
                embed = build_branded_embed(
                    guild_id,
                    title=success_title,
                    description=f"You've been granted the **{role.name}** role.\n\nWelcome to **{interaction.guild.name}** 🦁",
                    cog_prefix='verify',
                    use_thumbnail=True,
                    use_image=True,
                    use_footer=True,
                )
                await interaction.response.edit_message(content=None, embed=embed, view=None, attachments=[])

            else:
                user_attempts[self.user_id] = user_attempts.get(self.user_id, 0) + 1
                attempts_left = max_attempts - user_attempts[self.user_id]

                if attempts_left > 1:
                    wrong_msg = _cfg(guild_id, 'verify_wrong_attempt_message',
                                     '❌ Wrong! You have {remaining} attempts left.')
                    embed = build_branded_embed(
                        guild_id,
                        title="Bruh... really? ☠️",
                        description=wrong_msg.replace('{remaining}', str(attempts_left)),
                        cog_prefix='verify',
                        use_thumbnail=True,
                        use_image=False,
                        use_footer=True,
                    )
                    view = TryAgainButton(self.user_id)
                    await interaction.response.edit_message(content=None, embed=embed, view=view, attachments=[])

                elif attempts_left == 1:
                    last_msg = _cfg(guild_id, 'verify_last_chance_message',
                                    "⚠️ Last chance! Get this one wrong and you'll be kicked.")
                    embed = build_branded_embed(
                        guild_id,
                        title="Again?! Seriously?! 😤",
                        description=last_msg,
                        cog_prefix='verify',
                        use_thumbnail=True,
                        use_image=False,
                        use_footer=True,
                    )
                    view = TryAgainButton(self.user_id)
                    await interaction.response.edit_message(content=None, embed=embed, view=view, attachments=[])

                else:
                    kicked_msg = _cfg(guild_id, 'verify_kicked_message',
                                      "You've been kicked for failing verification. You can rejoin and try again.")
                    embed = build_branded_embed(
                        guild_id,
                        title="Certified bot. 🤖",
                        description=kicked_msg,
                        cog_prefix='verify',
                        use_thumbnail=False,
                        use_image=False,
                        use_footer=True,
                    )
                    await interaction.response.edit_message(content=None, embed=embed, view=None, attachments=[])

                    if _cfg_bool(guild_id, 'verify_dm_on_kick_enabled'):
                        dm_msg = _cfg(guild_id, 'verify_dm_on_kick_message',
                                      'You were kicked from {server} for failing CAPTCHA. Feel free to try again.')
                        dm_embed = build_branded_embed(
                            guild_id,
                            description=dm_msg.replace('{server}', interaction.guild.name),
                            cog_prefix='verify',
                            use_thumbnail=True,
                            use_image=False,
                            use_footer=True,
                        )
                        try:
                            await interaction.user.send(embed=dm_embed)
                        except (discord.Forbidden, discord.HTTPException):
                            pass

                    try:
                        await interaction.user.kick(reason="Failed verification")
                    except (discord.Forbidden, discord.HTTPException):
                        pass

        except Exception as e:
            print(f'[verify] CaptchaModal.on_submit error: {type(e).__name__}: {e}')
            traceback.print_exc()
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        'An error occurred during verification.', ephemeral=True
                    )
            except Exception:
                pass


class TryAgainButton(discord.ui.View):
    def __init__(self, user_id):
        super().__init__(timeout=120)
        self.user_id = user_id

    @discord.ui.button(label="Try Again", style=discord.ButtonStyle.primary, emoji="🔄")
    async def try_again(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if interaction.user.id != self.user_id:
                await interaction.response.send_message(
                    "❌ This is not your verification!", ephemeral=True
                )
                return

            guild_id = interaction.guild.id
            max_attempts = _cfg_int(guild_id, 'verify_max_attempts', 3)
            code, image_bytes = generate_captcha()
            user_captcha[self.user_id] = code

            file = discord.File(image_bytes, filename="captcha.png")
            attempts_left = max_attempts - user_attempts.get(self.user_id, 0)

            embed = build_branded_embed(
                guild_id,
                title="Identity Check 🔍",
                description=(
                    f"Match the code. You get {max_attempts} shots.\n"
                    f"**Attempts left: {attempts_left}** — tick tock.\n\n"
                    "Type the code you see in the image below."
                ),
                cog_prefix='verify',
                use_thumbnail=True,
                use_image=False,
                use_footer=True,
            )
            embed.set_image(url="attachment://captcha.png")

            view = CaptchaInputView(self.user_id)
            await interaction.response.edit_message(content=None, embed=embed, view=view, attachments=[file])
        except Exception as e:
            print(f'[verify] TryAgainButton error: {type(e).__name__}: {e}')
            traceback.print_exc()


class CaptchaInputView(discord.ui.View):
    def __init__(self, user_id):
        super().__init__(timeout=120)
        self.user_id = user_id

    @discord.ui.button(label="Enter Code", style=discord.ButtonStyle.success, emoji="⌨️")
    async def enter_code(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "❌ This is not your verification!", ephemeral=True
            )
            return
        modal = CaptchaModal(self.user_id)
        await interaction.response.send_modal(modal)


class VerifyView(discord.ui.View):
    """Persistent view for the Verify button. Accepts button_label so the API
    can build it with the guild's configured label while still registering the
    default ('Verify') at startup for existing Discord messages."""

    def __init__(self, button_label: str = 'Verify'):
        super().__init__(timeout=None)
        btn = discord.ui.Button(
            label=button_label,
            style=discord.ButtonStyle.success,
            custom_id='verify_button',
            emoji='✅'
        )
        btn.callback = self._on_verify
        self.add_item(btn)

    async def _on_verify(self, interaction: discord.Interaction):
        print(f'[verify] verify button clicked by {interaction.user.id} in guild {interaction.guild_id}')
        try:
            guild_id = interaction.guild.id

            if not _cfg_bool(guild_id, 'verify_enabled', '1'):
                await interaction.response.send_message(
                    "❌ Verification is currently disabled.", ephemeral=True
                )
                return

            user_id = interaction.user.id
            max_attempts = _cfg_int(guild_id, 'verify_max_attempts', 3)

            role = resolve_role(interaction.guild, _cfg(guild_id, 'verify_success_role', 'Verified'))
            if role and role in interaction.user.roles:
                await interaction.response.send_message(
                    "✅ You are already verified!", ephemeral=True
                )
                return

            if user_attempts.get(user_id, 0) >= max_attempts:
                await interaction.response.send_message(
                    "❌ You have exceeded the maximum attempts. You will be kicked.", ephemeral=True
                )
                try:
                    await interaction.user.kick(reason="Failed verification — max attempts exceeded")
                except (discord.Forbidden, discord.HTTPException):
                    pass
                return

            if user_id not in user_attempts:
                user_attempts[user_id] = 0

            code, image_bytes = generate_captcha()
            user_captcha[user_id] = code

            file = discord.File(image_bytes, filename="captcha.png")
            attempts_left = max_attempts - user_attempts[user_id]

            embed = build_branded_embed(
                guild_id,
                title="Identity Check 🔍",
                description=(
                    f"Match the code. You get {max_attempts} shots.\n"
                    f"**Attempts left: {attempts_left}** — tick tock.\n\n"
                    "Type the code you see in the image below."
                ),
                cog_prefix='verify',
                use_thumbnail=True,
                use_image=False,
                use_footer=True,
            )
            embed.set_image(url="attachment://captcha.png")

            view = CaptchaInputView(user_id)
            await interaction.response.send_message(embed=embed, file=file, view=view, ephemeral=True)

        except Exception as e:
            print(f'[verify] _on_verify error: {type(e).__name__}: {e}')
            traceback.print_exc()
            try:
                if interaction.response.is_done():
                    await interaction.followup.send('An unexpected error occurred.', ephemeral=True)
                else:
                    await interaction.response.send_message('An unexpected error occurred.', ephemeral=True)
            except Exception:
                pass


class VerifyCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    @commands.has_permissions(administrator=True)
    async def sendverify(self, ctx):
        guild_id = ctx.guild.id

        ch_val = _cfg(guild_id, 'verify_channel', '').strip()
        if ch_val:
            channel = resolve_channel(ctx.guild, ch_val)
            if channel is None:
                await ctx.send(f"❌ Channel not found: `{ch_val}`. Update verify_channel in the dashboard.")
                return
        else:
            channel = ctx.channel

        title = _cfg(guild_id, 'verify_embed_title', '🔒 Verify to Enter')
        description = _cfg(guild_id, 'verify_embed_description',
                           'Click the button below and solve the CAPTCHA to access the server.')
        button_label = _cfg(guild_id, 'verify_embed_button_label', 'Verify')

        embed = build_branded_embed(
            guild_id,
            title=title,
            description=description,
            cog_prefix='verify',
            use_thumbnail=True,
            use_image=True,
            use_footer=True,
        )

        view = VerifyView(button_label=button_label)
        msg = await channel.send(embed=embed, view=view)
        self.bot.add_view(view, message_id=msg.id)
        print(f'[verify] registered persistent view for new panel msg {msg.id}')


async def setup(bot):
    await bot.add_cog(VerifyCog(bot))
    bot.add_view(VerifyView())
