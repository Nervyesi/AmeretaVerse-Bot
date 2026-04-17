import discord
from discord.ext import commands

LOGO_URL = "https://i.imgur.com/FNE8Li0.png"

SECTION_ROLE_NAMES = [
    "Section-NFT",
    "Section-Raid",
    "Section-Trade",
    "Section-Degen",
    "Section-AI",
]

SECTIONS = [
    {
        "label":          "NFTs | Culture | Collab",
        "role":           "Section-NFT",
        "custom_id":      "section_nft",
        "emoji":          "🎨",
        "get_confirm":    "joining the culture room, habibi?\n\nmints drop here. art talks here. collabs cook here.\nyour pfp game's about to level up.\n\ntap confirm — you're on the guest list.\ntap cancel — stay in the shadows.",
        "get_success":    "you're in, degen. welcome to the culture.\n\nfloor checks, mint alerts, collab plays — all yours now.\nshow up with taste or get ratio'd. 🎨",
        "remove_confirm": "leaving the culture room?\n\nno more mint pings. no more drop alerts.\nthe art keeps moving without you, habibi.\n\nsure you wanna bounce?",
        "remove_success": "role dropped. culture room locked behind you.\n\nthe gallery closes for now.\ncome back when your taste returns. (☞ﾟヮﾟ)☞",
    },
    {
        "label":          "Raid | Engage | Support",
        "role":           "Section-Raid",
        "custom_id":      "section_raid",
        "emoji":          "⚔️",
        "get_confirm":    "suiting up for the frontline, habibi?\n\nraids. pushes. community warfare.\nwhen the squad moves, you move. no lurkers here.\n\ntap confirm — you're on the roster.\ntap cancel — stay on the bench.",
        "get_success":    "you're in the army now, degen. ⚔️\n\nraid pings incoming. engagement calls loaded.\nwhen we say move, we move together.\ndon't make us drag you into battle.",
        "remove_confirm": "deserting the frontline, habibi?\n\nthe squad moves without you now.\nno raid pings. no battle calls.\nyou good with that?",
        "remove_success": "role dropped. you're off the roster.\n\nthe war continues without you.\nhelmet's on the shelf — grab it when you're ready to fight again.",
    },
    {
        "label":          "Trade | Markets | News",
        "role":           "Section-Trade",
        "custom_id":      "section_trade",
        "emoji":          "📈",
        "get_confirm":    "stepping into the trading floor, habibi?\n\ncharts. flows. macro moves. news that matters.\nno noise. just signal.\n\ntap confirm — you're watching the tape now.\ntap cancel — stay off the desk.",
        "get_success":    "desk seat secured, degen. 📈\n\nmarket pings live. news drops incoming.\ntrade ideas flowing through.\neyes on the charts — don't blink.",
        "remove_confirm": "closing the terminal, habibi?\n\nno more market pings. no more news flow.\nthe tape keeps running without you.\n\nsure you wanna log off?",
        "remove_success": "role dropped. desk's closed for you.\n\ncharts keep moving. markets don't wait.\ncome back when you're ready to trade again. 📉📈",
    },
    {
        "label":          "Degen | Alpha | Memes",
        "role":           "Section-Degen",
        "custom_id":      "section_degen",
        "emoji":          "🃏",
        "get_confirm":    "entering the degen pit, habibi?\n\nalpha leaks. low caps. meme warfare.\nthis room is NOT for the weak hands.\npure chaos energy only.\n\ntap confirm — welcome to the jungle.\ntap cancel — go back to safety.",
        "get_success":    "you're in the pit now, degen. 🃏\n\nalpha drops. gem calls. meme battles.\nngmi or ngmi — there's no in between.\nhold on tight. we ride till zero or Valhalla. 🔥",
        "remove_confirm": "tapping out of the degen pit?\n\nthe chaos continues without you.\nno more alpha. no more 100x whispers.\nyou sure your hands aren't shaking, habibi?",
        "remove_success": "role dropped. pit's behind you now.\n\nthe degens move on. the alpha keeps flowing.\nyou'll be back. they always come back. (☞ﾟヮﾟ)☞",
    },
    {
        "label":          "AI | Vibecoding",
        "role":           "Section-AI",
        "custom_id":      "section_ai",
        "emoji":          "🤖",
        "get_confirm":    "joining the build room, habibi?\n\nAI tools. vibe coding. ship-or-die energy.\nprompts fly. stacks stack. builders only.\n\ntap confirm — terminal's open.\ntap cancel — stay a user.",
        "get_success":    "you're in, builder. 🤖\n\nprompt drops. tool leaks. launch pings.\nno tutorials, no hand-holding — just ship.\nlet's cook something the timeline can't ignore.",
        "remove_confirm": "closing the IDE, habibi?\n\nno more build pings. no more prompt drops.\nthe builders keep shipping without you.\n\nsure you wanna log out?",
        "remove_success": "role dropped. terminal's closed.\n\nthe code keeps compiling. the ships keep launching.\ncome back when inspiration hits. 🤖",
    },
]

SECTIONS_TEXT = (
    "pick your poison, habibi.\n"
    "five rooms. five vibes.\n"
    "grab the ones you want — skip the ones you don't.\n"
    "your pings, your rules. no noise you didn't sign up for.\n"
    "changed your mind later? hit the button again, role's gone.\n"
    "no commitment issues here. (☞ﾟヮﾟ)☞\n"
    "━━━━━━━━━━━━━━━━━━━\n"
    "🎨  **NFTs | Culture | Collab**\n"
    "mints. drops. art that hits.\n"
    "culture talk and collab plays.\n"
    "if you live for the pfp game — you belong here.\n\n"
    "⚔️  **Raid | Engage | Support**\n"
    "the frontline, degen.\n"
    "raids, engagement pushes, community power moves.\n"
    "tag in when the squad calls. we move together.\n\n"
    "📈  **Trade | Markets | News**\n"
    "charts. flows. macro noise that matters.\n"
    "market updates, trade ideas, what's moving and why.\n"
    "for the ones watching the tape.\n\n"
    "🃏  **Degen | Alpha | Memes**\n"
    "the wild room.\n"
    "alpha leaks, degen plays, meme warfare.\n"
    "low cap gems, high cap dreams, zero chill.\n"
    "enter at your own risk, habibi.\n\n"
    "🤖  **AI | Vibecoding**\n"
    "build season.\n"
    "AI tools, vibe coding, ship-or-die energy.\n"
    "prompts, stacks, launches, builders only.\n"
    "━━━━━━━━━━━━━━━━━━━\n"
    "tap what fits. skip what doesn't.\n"
    "we'll ping you only where you asked to be."
)


def _build_footer(member: discord.Member) -> str:
    held = [
        r.name for r in member.roles
        if r.name in SECTION_ROLE_NAMES
    ]
    if held:
        roles_str = ", ".join(held)
        return (
            f"your current roles: {roles_str}\n"
            "tap any button to toggle — add or drop, your call."
        )
    return (
        "no roles yet, habibi.\n"
        "the server's quiet without you in the rooms.\n"
        "pick at least one — we'll make it worth the pings."
    )


class ConfirmView(discord.ui.View):
    def __init__(self, section: dict, removing: bool):
        super().__init__(timeout=60)
        self.section = section
        self.removing = removing

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        role = discord.utils.get(interaction.guild.roles, name=self.section["role"])
        if not role:
            await interaction.response.edit_message(
                content=f"❌ Role **{self.section['role']}** not found. Contact an admin.",
                embed=None, view=None
            )
            return

        if self.removing:
            await interaction.user.remove_roles(role)
            msg = self.section["remove_success"]
        else:
            await interaction.user.add_roles(role)
            msg = self.section["get_success"]

        footer = _build_footer(interaction.user)
        embed = discord.Embed(description=msg, color=0x94730D)
        embed.set_thumbnail(url=LOGO_URL)
        embed.set_footer(text=footer)
        await interaction.response.edit_message(embed=embed, view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="↩️")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content="cancelled. buttons are still live on the main panel.", embed=None, view=None
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

    removing = role in interaction.user.roles
    confirm_text = section["remove_confirm"] if removing else section["get_confirm"]

    embed = discord.Embed(description=confirm_text, color=0x94730D)
    embed.set_thumbnail(url=LOGO_URL)
    embed.set_footer(text="AmeretaVerse • Community Sections")
    await interaction.response.send_message(
        embed=embed, view=ConfirmView(section, removing), ephemeral=True
    )


class SectionsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="NFTs | Culture | Collab",
        style=discord.ButtonStyle.primary,
        custom_id="section_nft",
        emoji="🎨",
    )
    async def section_nft(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _handle_section_toggle(interaction, SECTIONS[0])

    @discord.ui.button(
        label="Raid | Engage | Support",
        style=discord.ButtonStyle.primary,
        custom_id="section_raid",
        emoji="⚔️",
    )
    async def section_raid(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _handle_section_toggle(interaction, SECTIONS[1])

    @discord.ui.button(
        label="Trade | Markets | News",
        style=discord.ButtonStyle.primary,
        custom_id="section_trade",
        emoji="📈",
    )
    async def section_trade(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _handle_section_toggle(interaction, SECTIONS[2])

    @discord.ui.button(
        label="Degen | Alpha | Memes",
        style=discord.ButtonStyle.primary,
        custom_id="section_degen",
        emoji="🃏",
    )
    async def section_degen(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _handle_section_toggle(interaction, SECTIONS[3])

    @discord.ui.button(
        label="AI | Vibecoding",
        style=discord.ButtonStyle.primary,
        custom_id="section_ai",
        emoji="🤖",
    )
    async def section_ai(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _handle_section_toggle(interaction, SECTIONS[4])


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
