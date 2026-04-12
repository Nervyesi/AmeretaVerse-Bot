import discord
from discord.ext import commands

LOGO_URL = "https://i.imgur.com/FNE8Li0.png"
PROGRESS_BAR_URL = "https://i.imgur.com/5Mg2BIE.png"

MAIN_TEXT = (
    "**Degen, who are you?** 👀\n\n"
    "🎯 **Creator** — lacking deals? lacking brand money? come here habibi. 💰\n\n"
    "👤 **User** — here for the alpha, the calls, the degen life? you're home. 🔥\n\n"
    "⚠️ pick wrong and you'll know real fast. just saying."
)


class CreatorWarningView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.button(
        label="Yes, I'm a Creator",
        style=discord.ButtonStyle.success,
        emoji="🎯"
    )
    async def confirm_creator(self, interaction: discord.Interaction, button: discord.ui.Button):
        role = discord.utils.get(interaction.guild.roles, name="Creator-apply")
        if role:
            await interaction.user.add_roles(role)
            embed = discord.Embed(
                title="🎯 Creator Role Granted.",
                description=(
                    "You're in. Welcome to the creator side of AmeretaVerse.\n\n"
                    "Next step: open a ticket so our team can verify your profile.\n"
                    "Once verified, the full Creator hub unlocks. 🔓"
                ),
                color=0x94730D
            )
            embed.set_thumbnail(url=LOGO_URL)
            embed.set_image(url=PROGRESS_BAR_URL)
            embed.set_footer(text="AmeretaVerse • Role System")
            await interaction.response.edit_message(embed=embed, view=None)
        else:
            await interaction.response.edit_message(
                content="❌ Creator role not found. Contact an admin.",
                view=None
            )

    @discord.ui.button(
        label="Go Back",
        style=discord.ButtonStyle.secondary,
        emoji="↩️"
    )
    async def go_back_creator(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(description=MAIN_TEXT, color=0x94730D)
        embed.set_thumbnail(url=LOGO_URL)
        embed.set_footer(text="AmeretaVerse • Role System")
        await interaction.response.edit_message(embed=embed, view=RoleSelectView())


class UserWarningView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.button(
        label="Yes, I'm a User",
        style=discord.ButtonStyle.success,
        emoji="👤"
    )
    async def confirm_user(self, interaction: discord.Interaction, button: discord.ui.Button):
        role = discord.utils.get(interaction.guild.roles, name="Amereta")
        if role:
            await interaction.user.add_roles(role)
            embed = discord.Embed(
                title="👤 Amereta Role Granted.",
                description=(
                    "You're in. Welcome to the AmeretaVerse community.\n\n"
                    "The alpha starts now. Stay sharp, Degen. 🦁"
                ),
                color=0x94730D
            )
            embed.set_thumbnail(url=LOGO_URL)
            embed.set_image(url=PROGRESS_BAR_URL)
            embed.set_footer(text="AmeretaVerse • Role System")
            await interaction.response.edit_message(embed=embed, view=None)
        else:
            await interaction.response.edit_message(
                content="❌ User role not found. Contact an admin.",
                view=None
            )

    @discord.ui.button(
        label="Go Back",
        style=discord.ButtonStyle.secondary,
        emoji="↩️"
    )
    async def go_back_user(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(description=MAIN_TEXT, color=0x94730D)
        embed.set_thumbnail(url=LOGO_URL)
        embed.set_footer(text="AmeretaVerse • Role System")
        await interaction.response.edit_message(embed=embed, view=RoleSelectView())


class RoleSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Creator",
        style=discord.ButtonStyle.primary,
        custom_id="role_creator",
        emoji="🎯"
    )
    async def select_creator(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            description=(
                "you sure you a creator habibi? 🎯\n\n"
                "no content, no collabs — wrong door.\n\n"
                "we good? let's get you paid. 👇"
            ),
            color=0x94730D
        )
        embed.set_thumbnail(url=LOGO_URL)
        embed.set_footer(text="AmeretaVerse • Role System")
        await interaction.response.send_message(embed=embed, view=CreatorWarningView(), ephemeral=True)

    @discord.ui.button(
        label="User",
        style=discord.ButtonStyle.primary,
        custom_id="role_user",
        emoji="👤"
    )
    async def select_user(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            description=(
                "user life incoming. 👀\n\n"
                "pings. alpha. noise. the good stuff.\n\n"
                "ready for it? come in. 🔥👇"
            ),
            color=0x94730D
        )
        embed.set_thumbnail(url=LOGO_URL)
        embed.set_footer(text="AmeretaVerse • Role System")
        await interaction.response.send_message(embed=embed, view=UserWarningView(), ephemeral=True)


class MaineRolesCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    @commands.has_permissions(administrator=True)
    async def sendroles(self, ctx):
        embed = discord.Embed(description=MAIN_TEXT, color=0x94730D)
        embed.set_thumbnail(url=LOGO_URL)
        embed.set_image(url=PROGRESS_BAR_URL)
        embed.set_footer(text="AmeretaVerse • Role System")
        await ctx.send(embed=embed, view=RoleSelectView())


async def setup(bot):
    await bot.add_cog(MaineRolesCog(bot))
    bot.add_view(RoleSelectView())