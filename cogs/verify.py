import discord
from discord.ext import commands
import random
import string
from captcha.image import ImageCaptcha
import io

VERIFY_THUMB_URL = "https://i.imgur.com/FNE8Li0.png"
LOGO_URL = "https://i.imgur.com/KAkfd9v.png"
PROGRESS_BAR_URL = "https://i.imgur.com/5Mg2BIE.png"

user_attempts = {}
user_captcha = {}

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
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "❌ This is not your verification!", ephemeral=True
            )
            return

        correct_code = user_captcha.get(self.user_id, "")
        entered = self.answer.value.upper().strip()

        if entered == correct_code:
            user_attempts.pop(self.user_id, None)
            user_captcha.pop(self.user_id, None)

            amereta_role = discord.utils.get(interaction.guild.roles, name="Verified")
            if amereta_role:
                await interaction.user.add_roles(amereta_role)
                embed = discord.Embed(
                    title="✅ Verified. You're one of us now. 🔥",
                    description=(
                        "You've been granted the **Verified** role.\n\n"
                        "Welcome to **AmeretaVerse** 🦁"
                    ),
                    color=0x94730D
                )
                embed.set_thumbnail(url=LOGO_URL)
                embed.set_image(url=PROGRESS_BAR_URL)
                embed.set_footer(text="AmeretaVerse • Verification System")
                await interaction.response.edit_message(
                    content=None, embed=embed, view=None, attachments=[]
                )
            else:
                await interaction.response.edit_message(
                    content="✅ Correct! But Amereta role not found. Contact an admin.",
                    view=None, attachments=[]
                )
        else:
            user_attempts[self.user_id] = user_attempts.get(self.user_id, 0) + 1
            attempts_left = 3 - user_attempts[self.user_id]

            if attempts_left == 2:
                embed = discord.Embed(
                    title="Bruh... really? ☠️",
                    description=(
                        "That was incorrect.\n"
                        "**2 shots left.**\n"
                        "We're watching you."
                    ),
                    color=0xFF6600
                )
                embed.set_thumbnail(url=VERIFY_THUMB_URL)
                embed.set_footer(text="AmeretaVerse • Verification System")
                view = TryAgainButton(self.user_id)
                await interaction.response.edit_message(
                    content=None, embed=embed, view=view, attachments=[]
                )

            elif attempts_left == 1:
                embed = discord.Embed(
                    title="Again?! Seriously?! 😤",
                    description=(
                        "**Last chance, Degen.**\n"
                        "One more wrong answer and you're kebab. 🍢"
                    ),
                    color=0xFF3300
                )
                embed.set_thumbnail(url=VERIFY_THUMB_URL)
                embed.set_footer(text="AmeretaVerse • Verification System")
                view = TryAgainButton(self.user_id)
                await interaction.response.edit_message(
                    content=None, embed=embed, view=view, attachments=[]
                )

            else:
                embed = discord.Embed(
                    title="Certified bot. 🤖",
                    description="Kicked. See ya never. 🚪",
                    color=0xFF0000
                )
                embed.set_footer(text="AmeretaVerse • Verification System")
                await interaction.response.edit_message(
                    content=None, embed=embed, view=None, attachments=[]
                )
                await interaction.user.kick(reason="Failed verification 3 times")


class TryAgainButton(discord.ui.View):
    def __init__(self, user_id):
        super().__init__(timeout=120)
        self.user_id = user_id

    @discord.ui.button(
        label="Try Again",
        style=discord.ButtonStyle.primary,
        emoji="🔄"
    )
    async def try_again(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "❌ This is not your verification!", ephemeral=True
            )
            return

        code, image_bytes = generate_captcha()
        user_captcha[self.user_id] = code

        file = discord.File(image_bytes, filename="captcha.png")
        attempts_left = 3 - user_attempts.get(self.user_id, 0)

        embed = discord.Embed(
            title="Identity Check 🔍",
            description=(
                f"Match the code. You get 3 shots.\n"
                f"**Attempts left: {attempts_left}** — tick tock.\n\n"
                "Type the code you see in the image below."
            ),
            color=0x94730D
        )
        embed.set_thumbnail(url=VERIFY_THUMB_URL)
        embed.set_image(url="attachment://captcha.png")
        embed.set_footer(text="AmeretaVerse • Verification System")

        view = CaptchaInputView(self.user_id)
        await interaction.response.edit_message(
            content=None, embed=embed, view=view, attachments=[file]
        )


class CaptchaInputView(discord.ui.View):
    def __init__(self, user_id):
        super().__init__(timeout=120)
        self.user_id = user_id

    @discord.ui.button(
        label="Enter Code",
        style=discord.ButtonStyle.success,
        emoji="⌨️"
    )
    async def enter_code(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "❌ This is not your verification!", ephemeral=True
            )
            return
        modal = CaptchaModal(self.user_id)
        await interaction.response.send_modal(modal)


class VerifyButton(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Verify",
        style=discord.ButtonStyle.success,
        custom_id="verify_button",
        emoji="✅"
    )
    async def verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = interaction.user.id

        amereta_role = discord.utils.get(interaction.guild.roles, name="Verified")
        if amereta_role and amereta_role in interaction.user.roles:
            await interaction.response.send_message(
                "✅ You are already verified!", ephemeral=True
            )
            return

        if user_id not in user_attempts:
            user_attempts[user_id] = 0

        if user_attempts[user_id] >= 3:
            await interaction.response.send_message(
                "❌ You have exceeded the maximum attempts. You will be kicked.",
                ephemeral=True
            )
            await interaction.user.kick(reason="Failed verification 3 times")
            return

        code, image_bytes = generate_captcha()
        user_captcha[user_id] = code

        file = discord.File(image_bytes, filename="captcha.png")
        attempts_left = 3 - user_attempts[user_id]

        embed = discord.Embed(
            title="Identity Check 🔍",
            description=(
                f"Match the code. You get 3 shots.\n"
                f"**Attempts left: {attempts_left}** — tick tock.\n\n"
                "Type the code you see in the image below."
            ),
            color=0x94730D
        )
        embed.set_thumbnail(url=VERIFY_THUMB_URL)
        embed.set_image(url="attachment://captcha.png")
        embed.set_footer(text="AmeretaVerse • Verification System")

        view = CaptchaInputView(user_id)
        await interaction.response.send_message(
            embed=embed, file=file, view=view, ephemeral=True
        )


class VerifyCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    @commands.has_permissions(administrator=True)
    async def sendverify(self, ctx):
        with open("intro.gif", "rb") as f:
            gif_file = discord.File(f, filename="intro.gif")

        main_embed = discord.Embed(
            description=(
                "**Welcome to AmeretaVerse, Degen.** 👋\n\n"
                "We roast bots here. 🔥\n"
                "Scammers get grilled. Farm freaks get kicked.\n\n"
                "Human? Hit verify.\n"
                "Bot? Run. (☞ﾟヮﾟ)☞"
            ),
            color=0x94730D
        )
        main_embed.set_thumbnail(url=LOGO_URL)
        main_embed.set_image(url="attachment://intro.gif")
        main_embed.set_footer(text="AmeretaVerse • Verification System")

        await ctx.send(file=gif_file, embed=main_embed, view=VerifyButton())


async def setup(bot):
    await bot.add_cog(VerifyCog(bot))
    bot.add_view(VerifyButton())