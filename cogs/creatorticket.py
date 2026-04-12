import discord
from discord.ext import commands
import asyncio

LOGO_URL = "https://i.imgur.com/FNE8Li0.png"
PROGRESS_BAR_URL = "https://i.imgur.com/5Mg2BIE.png"

CREATOR_CATEGORY = "Creators"
TICKET_PREFIX = "apply"


class CreatorFormModal(discord.ui.Modal, title="Creator Application 🎯"):
    name = discord.ui.TextInput(
        label="Name",
        placeholder="Your name or alias",
        min_length=2,
        max_length=50,
        style=discord.TextStyle.short
    )
    x_link = discord.ui.TextInput(
        label="X Profile Link",
        placeholder="https://x.com/yourhandle",
        min_length=10,
        max_length=100,
        style=discord.TextStyle.short
    )
    followers = discord.ui.TextInput(
        label="Follower Count",
        placeholder="e.g. 10,000",
        min_length=1,
        max_length=20,
        style=discord.TextStyle.short
    )
    scores = discord.ui.TextInput(
        label="Sorsa X Score / Wallchain X Score",
        placeholder="Sorsa: 000 | Wallchain: 000",
        min_length=1,
        max_length=100,
        style=discord.TextStyle.short
    )
    niche_about = discord.ui.TextInput(
        label="Niche & About You",
        placeholder="Niche: NFTs, Web3 gaming...\nAbout: Why should we approve you?",
        min_length=20,
        max_length=500,
        style=discord.TextStyle.long
    )

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild

        # Find Creators category
        category = discord.utils.get(guild.categories, name=CREATOR_CATEGORY)
        if not category:
            await interaction.response.send_message(
                "❌ Creators category not found. Contact an admin.",
                ephemeral=True
            )
            return

        # Get next ticket number
        ticket_number = 1
        for channel in category.channels:
            if channel.name.startswith(TICKET_PREFIX):
                ticket_number += 1

        ticket_name = f"{TICKET_PREFIX}#{ticket_number}"

        # Channel permissions
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            interaction.user: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True)
        }

        # Give admins access
        admin_role = discord.utils.get(guild.roles, name="Admin")
        if admin_role:
            overwrites[admin_role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

        # Create ticket channel
        ticket_channel = await category.create_text_channel(
            name=ticket_name,
            overwrites=overwrites
        )

        # Send form data to ticket channel
        embed = discord.Embed(
            title=f"🎯 Creator Application — {interaction.user.display_name}",
            color=0x94730D
        )
        embed.add_field(name="Name", value=self.name.value, inline=True)
        embed.add_field(name="X Link", value=self.x_link.value, inline=True)
        embed.add_field(name="Follower Count", value=self.followers.value, inline=True)
        embed.add_field(name="Sorsa X Score / Wallchain X Score", value=self.scores.value, inline=False)
        embed.add_field(name="Niche & About", value=self.niche_about.value, inline=False)
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.set_footer(text=f"AmeretaVerse • Creator Applications | User ID: {interaction.user.id}")

        view = TicketManageView(interaction.user.id)
        await ticket_channel.send(
            content=f"📋 New application from {interaction.user.mention}",
            embed=embed,
            view=view
        )

        # Confirm to user
        confirm_embed = discord.Embed(
            title="✅ Application Submitted!",
            description=(
                "your application is in. 🎯\n\n"
                "our team will review it personally.\n"
                "real eyes, no bots.\n\n"
                f"ticket: {ticket_channel.mention}\n\n"
                "sit tight. we'll get back to you. 💰"
            ),
            color=0x94730D
        )
        confirm_embed.set_thumbnail(url=LOGO_URL)
        confirm_embed.set_image(url=PROGRESS_BAR_URL)
        confirm_embed.set_footer(text="AmeretaVerse • Creator Applications")
        await interaction.response.send_message(embed=confirm_embed, ephemeral=True)


class TicketManageView(discord.ui.View):
    def __init__(self, applicant_id):
        super().__init__(timeout=None)
        self.applicant_id = applicant_id

    @discord.ui.button(
        label="Approve",
        style=discord.ButtonStyle.success,
        emoji="✅",
        custom_id="ticket_approve"
    )
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "❌ Only admins can approve applications.", ephemeral=True
            )
            return

        member = interaction.guild.get_member(self.applicant_id)
        if member:
            # Remove Creator Apply role
            creator_apply_role = discord.utils.get(interaction.guild.roles, name="Creator-apply")
            if creator_apply_role and creator_apply_role in member.roles:
                await member.remove_roles(creator_apply_role)

            # Give Creator role
            creator_role = discord.utils.get(interaction.guild.roles, name="Creator")
            if creator_role:
                await member.add_roles(creator_role)

            # Notify the member
            try:
                notify_embed = discord.Embed(
                   title="you're in, creator. 🎯",
                    description=(
                        "application approved.\n\n"
                        "Creator hub is yours.\n"
                        "let's get you paid, habibi. 💰"
                    ),
                    color=0x94730D
                )
                notify_embed.set_footer(text="AmeretaVerse • Creator Applications")
                await member.send(embed=notify_embed)
            except discord.Forbidden:
                pass

        embed = discord.Embed(
            title="✅ Application Approved",
            description=f"Approved by {interaction.user.mention}\nCreator role granted.",
            color=0x00FF00
        )
        embed.set_footer(text="AmeretaVerse • Creator Applications")
        await interaction.response.send_message(embed=embed)

    @discord.ui.button(
        label="Reject",
        style=discord.ButtonStyle.danger,
        emoji="❌",
        custom_id="ticket_reject"
    )
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "❌ Only admins can reject applications.", ephemeral=True
            )
            return

        member = interaction.guild.get_member(self.applicant_id)
        if member:
            # Remove Creator Apply role
            creator_apply_role = discord.utils.get(interaction.guild.roles, name="Creator-apply")
            if creator_apply_role and creator_apply_role in member.roles:
                await member.remove_roles(creator_apply_role)

            # Notify the member
            try:
                notify_embed = discord.Embed(
                   title="not this time, habibi. 🫡",
                    description=(
                        "application didn't go through.\n\n"
                        "but the community is still yours,\n"
                        "head back and grab the User role."
                    ),
                    color=0xFF0000
                )
                notify_embed.set_footer(text="AmeretaVerse • Creator Applications")
                await member.send(embed=notify_embed)
            except discord.Forbidden:
                pass

        embed = discord.Embed(
            title="❌ Application Rejected",
            description=f"Rejected by {interaction.user.mention}\nCreator Apply role removed.",
            color=0xFF0000
        )
        embed.set_footer(text="AmeretaVerse • Creator Applications")
        await interaction.response.send_message(embed=embed)

    @discord.ui.button(
        label="Close Ticket",
        style=discord.ButtonStyle.secondary,
        emoji="🔒",
        custom_id="ticket_close"
    )
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "❌ Only admins can close tickets.", ephemeral=True
            )
            return

        await interaction.response.send_message("🔒 Closing ticket in 5 seconds...")
        await asyncio.sleep(5)
        await interaction.channel.delete()

    @discord.ui.button(
        label="Add Member",
        style=discord.ButtonStyle.primary,
        emoji="➕",
        custom_id="ticket_add"
    )
    async def add_member(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "❌ Only admins can add members.", ephemeral=True
            )
            return

        await interaction.response.send_message(
            "Mention the user you want to add:",
            ephemeral=True
        )

        def check(m):
            return m.author == interaction.user and m.channel == interaction.channel

        try:
            msg = await interaction.client.wait_for('message', check=check, timeout=30)
            if msg.mentions:
                member = msg.mentions[0]
                await interaction.channel.set_permissions(
                    member,
                    read_messages=True,
                    send_messages=True
                )
                await interaction.channel.send(f"✅ {member.mention} has been added to this ticket.")
                await msg.delete()
        except asyncio.TimeoutError:
            pass


class ApplyButton(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Apply as Creator 🎯",
        style=discord.ButtonStyle.success,
        custom_id="creator_apply_button"
    )
    async def apply(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Must have Creator-apply role to apply
        creator_apply_role = discord.utils.get(interaction.guild.roles, name="Creator-apply")
        if not creator_apply_role or creator_apply_role not in interaction.user.roles:
            await interaction.response.send_message(
                "⚠️ You need the Creator-apply role to apply. Head back to role selection.",
                ephemeral=True
            )
            return

        modal = CreatorFormModal()
        await interaction.response.send_modal(modal)
 
 
class CreatorTicketCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    @commands.has_permissions(administrator=True)
    async def sendcreatorapply(self, ctx):
        embed = discord.Embed(
            description=(
                "**so you're a creator, habibi.** 🎯\n\n"
                "respect. but we don't just hand out the role. "
                "fill the form below, our team reviews it personally.\n\n"
                "get approved → get the role → get the deals. 💰\n\n"
                "hit the button and let's see what you got. 👇"
            ),
            color=0x94730D
        )
        embed.set_thumbnail(url=LOGO_URL)
        embed.set_image(url=PROGRESS_BAR_URL)
        embed.set_footer(text="AmeretaVerse • Creator Applications")
        await ctx.send(embed=embed, view=ApplyButton())


async def setup(bot):
    await bot.add_cog(CreatorTicketCog(bot))
    bot.add_view(ApplyButton())
    bot.add_view(TicketManageView(0))