import discord
from discord.ext import commands

LOGO_URL = "https://i.imgur.com/FNE8Li0.png"

SECTIONS = [
    {
        "label": "NFTs | Culture | Collab",
        "role":  "Section-NFT",
        "custom_id": "section_nft",
        "emoji": "🖼️",
    },
    {
        "label": "Trade | Markets | News",
        "role":  "Section-Trade",
        "custom_id": "section_trade",
        "emoji": "📈",
    },
    {
        "label": "Degen | Alpha | Memes",
        "role":  "Section-Degen",
        "custom_id": "section_degen",
        "emoji": "🔥",
    },
    {
        "label": "AI | Vibecoding",
        "role":  "Section-AI",
        "custom_id": "section_ai",
        "emoji": "🤖",
    },
]

SECTIONS_TEXT = (
    "**pick your lane, habibi.** 🎯\n\n"
    "🖼️ **NFTs | Culture | Collab** — drops, collabs, and all things culture.\n\n"
    "📈 **Trade | Markets | News** — charts, calls, and alpha before it hits.\n\n"
    "🔥 **Degen | Alpha | Memes** — unfiltered. loud. degeneracy at its finest.\n\n"
    "🤖 **AI | Vibecoding** — builders, prompters, and vibecoders only.\n\n"
    "toggle any section you want. stack them. mix them. go wild. 🃏"
)


async def _handle_section_toggle(interaction: discord.Interaction, section: dict):
    amereta_role = discord.utils.get(interaction.guild.roles, name="Amereta")
    if not amereta_role or amereta_role not in interaction.user.roles:
        await interaction.response.send_message(
            "⚠️ You need to be verified first.", ephemeral=True
        )
        return

    role = discord.utils.get(interaction.guild.roles, name=section["role"])
    if not role:
        await interaction.response.send_message(
            f"❌ Role **{section['role']}** not found. Contact an admin.", ephemeral=True
        )
        return

    if role in interaction.user.roles:
        await interaction.user.remove_roles(role)
        await interaction.response.send_message(
            f"👋 You left **{section['label']}**.", ephemeral=True
        )
    else:
        await interaction.user.add_roles(role)
        await interaction.response.send_message(
            f"✅ You joined **{section['label']}**. welcome, habibi. 🔥", ephemeral=True
        )


class SectionsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="NFTs | Culture | Collab",
        style=discord.ButtonStyle.primary,
        custom_id="section_nft",
        emoji="🖼️",
    )
    async def section_nft(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _handle_section_toggle(interaction, SECTIONS[0])

    @discord.ui.button(
        label="Trade | Markets | News",
        style=discord.ButtonStyle.primary,
        custom_id="section_trade",
        emoji="📈",
    )
    async def section_trade(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _handle_section_toggle(interaction, SECTIONS[1])

    @discord.ui.button(
        label="Degen | Alpha | Memes",
        style=discord.ButtonStyle.primary,
        custom_id="section_degen",
        emoji="🔥",
    )
    async def section_degen(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _handle_section_toggle(interaction, SECTIONS[2])

    @discord.ui.button(
        label="AI | Vibecoding",
        style=discord.ButtonStyle.primary,
        custom_id="section_ai",
        emoji="🤖",
    )
    async def section_ai(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _handle_section_toggle(interaction, SECTIONS[3])


class SectionsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    @commands.has_permissions(administrator=True)
    async def sendsections(self, ctx):
        embed = discord.Embed(description=SECTIONS_TEXT, color=0x94730D)
        embed.set_thumbnail(url=LOGO_URL)
        embed.set_footer(text="AmeretaVerse • Community Sections")
        await ctx.send(embed=embed, view=SectionsView())


async def setup(bot):
    await bot.add_cog(SectionsCog(bot))
    bot.add_view(SectionsView())
