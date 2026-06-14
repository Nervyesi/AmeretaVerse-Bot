"""
_engagers_view.py — Shared paginated engagers list.

Used by the Engage module (/my-engagers-list, /engagers-list) and the Raid
module (/raiders) so all three render the same clean, readable list directly in
Discord instead of an external dump. One ephemeral embed, 15 engagers per page,
global numbering that never restarts, Previous / Next buttons, locked to the
original caller, five minute inactivity timeout.

Each caller passes a list of plain entry dicts:
    {
        'user_id':      str,          # Discord snowflake, string end to end
        'x_handle':     str,          # X handle without a leading @, may be ''
        'badges':       list[str],    # already formatted task badges, e.g. '✅ like'
        'completeness': int,          # number of completed enabled tasks
        'ts':           str,          # engagement timestamp, 'YYYY-MM-DD HH:MM:SS'
    }

Discord names are resolved only for the entries on the page being shown, so a
list with many engagers stays responsive while paging.
"""
import discord

# Bot standard gold. No BRAND_GOLD constant exists in cogs/_branding.py, so the
# spec fallback value is used directly.
GOLD      = 0xC8A84E
PAGE_SIZE = 15
_NO_PING  = discord.AllowedMentions(roles=False, users=False, everyone=False)


def sort_engagers(entries: list) -> list:
    """Completeness DESC, then latest engagement timestamp DESC.

    The timestamp is an ISO style string ('YYYY-MM-DD HH:MM:SS'); lexicographic
    order matches chronological order, so a plain string sort is correct for the
    tie break."""
    return sorted(
        entries,
        key=lambda e: (int(e.get('completeness') or 0), e.get('ts') or ''),
        reverse=True,
    )


async def _resolve_discord_name(guild, client, user_id: str) -> str:
    """@username via the guild cache, then an HTTP user fetch, then (left server)."""
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return '(left server)'
    member = guild.get_member(uid) if guild else None
    if member is not None:
        return f'@{member.name}'
    try:
        user = await client.fetch_user(uid)
        if user is not None:
            return f'@{user.name}'
    except Exception:
        pass
    return '(left server)'


def comment_badge(x_handle: str, reply_tweet_id) -> str:
    """The verified comment badge. When the reply tweet id was captured at verify
    time and the engager's handle is known, the word reply links straight to the
    reply on X. Older rows from before reply ids were captured have no id, so the
    plain badge is shown. NULL or empty inputs never raise."""
    if reply_tweet_id and x_handle:
        return f'✅ comment ([reply](https://x.com/{x_handle}/status/{reply_tweet_id}))'
    return '✅ comment'


def _format_line(global_index: int, discord_name: str, x_handle: str, badges: list) -> str:
    handle = f'[@{x_handle}](https://x.com/{x_handle})' if x_handle else '(no X handle)'
    tasks  = ' · '.join(badges) if badges else '(no tasks recorded)'
    # Middle dot separators only. The constraint forbids em dashes, en dashes and
    # hyphens as prose separators, so the handle and the task list are joined with
    # the same middle dot used between the name and the handle.
    return f'{global_index}. {discord_name} · {handle} · {tasks}'


def page_count(total: int) -> int:
    return max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)


async def build_page_embed(
    guild, client, entries: list, page: int, title: str, color: int = GOLD,
) -> discord.Embed:
    total       = len(entries)
    total_pages = page_count(total)
    page        = max(0, min(page, total_pages - 1))
    start       = page * PAGE_SIZE
    end         = min(start + PAGE_SIZE, total)

    lines = []
    for offset, e in enumerate(entries[start:end]):
        gi   = start + offset + 1
        name = await _resolve_discord_name(guild, client, e.get('user_id'))
        lines.append(_format_line(gi, name, e.get('x_handle') or '', e.get('badges') or []))

    embed = discord.Embed(
        title       = title,
        description = '\n'.join(lines) if lines else 'No engagers yet.',
        color       = color,
    )
    # Numeric range below (start-end) is range notation, not a prose separator.
    shown_lo = start + 1 if total else 0
    embed.set_footer(
        text=f'Page {page + 1} of {total_pages} · Showing {shown_lo}-{end} of {total}'
    )
    return embed


class EngagersView(discord.ui.View):
    """Previous / Next pager, locked to the original caller, five minute timeout."""

    def __init__(self, *, entries: list, title: str, original_user_id: int, color: int = GOLD):
        super().__init__(timeout=300)
        self.entries          = entries
        self.title            = title
        self.color            = color
        self.original_user_id = original_user_id
        self.page             = 0
        self.total_pages      = page_count(len(entries))
        self.message          = None  # set by the caller for the timeout edit

        self._prev = discord.ui.Button(
            label='◀ Previous', style=discord.ButtonStyle.secondary, row=0,
        )
        self._prev.callback = self._prev_cb
        self.add_item(self._prev)

        self._next = discord.ui.Button(
            label='Next ▶', style=discord.ButtonStyle.secondary, row=0,
        )
        self._next.callback = self._next_cb
        self.add_item(self._next)

        self._sync_buttons()

    def _sync_buttons(self):
        self._prev.disabled = self.page <= 0
        self._next.disabled = self.page >= self.total_pages - 1

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.original_user_id:
            await interaction.response.send_message(
                'This list is not yours to page.', ephemeral=True,
            )
            return False
        return True

    async def _render(self, interaction: discord.Interaction):
        self._sync_buttons()
        embed = await build_page_embed(
            interaction.guild, interaction.client, self.entries,
            self.page, self.title, self.color,
        )
        await interaction.response.edit_message(
            embed=embed, view=self, allowed_mentions=_NO_PING,
        )

    async def _prev_cb(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        self.page = max(0, self.page - 1)
        await self._render(interaction)

    async def _next_cb(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        self.page = min(self.total_pages - 1, self.page + 1)
        await self._render(interaction)

    async def on_timeout(self):
        self._prev.disabled = True
        self._next.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass
