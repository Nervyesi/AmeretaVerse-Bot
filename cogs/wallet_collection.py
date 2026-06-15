"""
wallet_collection.py — Wallet Collection feature (inside the Engage module).

NFT projects in the community gate a mint whitelist behind AVbot. An admin
creates a Wallet Collection (from the dashboard or by API), the bot posts a
branded embed with a button into a chosen channel, members with a required role
click the button and submit a wallet, the bot validates the address against the
configured chain, and the project owner reads the collected wallets from the
dashboard or the admin-only /wallet-list command.

Persistence: the submit button is a discord.ui.DynamicItem whose custom_id
carries the collection id (wallet_collection_submit:{id}). bot.add_dynamic_items
at setup rebinds the handler after a restart, so the button keeps working on
old messages without storing per-message views.

Constraints honored here:
  * Snowflakes are strings end to end.
  * Every query is guild scoped (guild_id passed through to the DB helpers).
  * AllowedMentions disables every mention EXCEPT the initial post, which
    intentionally pings the configured roles.
  * log_event fires on every state change (post, close, submit, update).
"""
import io
import json
import traceback

import discord
from discord import app_commands
from discord.ext import commands

from database import (
    get_wallet_collection,
    get_wallet_collection_by_name,
    update_wallet_collection,
    list_wallet_submissions,
    upsert_wallet_submission,
    log_event,
)
from cogs._utils import resolve_channel
from cogs._wallet_validation import validate_wallet, CHAIN_LABELS
from cogs._engagers_view import _resolve_discord_name

GOLD       = 0xC8A84E
PAGE_SIZE  = 15
_NO_PING   = discord.AllowedMentions(roles=False, users=False, everyone=False)
_PING_ROLES = discord.AllowedMentions(roles=True, users=False, everyone=False)

# Discord input limits.
_MODAL_TITLE_MAX = 45
_LABEL_MAX       = 45
_PLACEHOLDER_MAX = 100
_WALLET_MAX      = 256


# ── Embed + view builders (shared by the cog and the API) ───────────────────

def build_wallet_collection_embed(collection: dict) -> discord.Embed:
    color = collection.get('embed_color')
    try:
        color_int = int(color) if color is not None else GOLD
    except (TypeError, ValueError):
        color_int = GOLD
    embed = discord.Embed(
        title       = (collection.get('embed_title') or 'Submit Your Wallet')[:256],
        description = (collection.get('embed_description') or '')[:4096],
        color       = color_int,
    )
    thumb = (collection.get('embed_thumbnail_url') or '').strip()
    if thumb:
        embed.set_thumbnail(url=thumb)
    image = (collection.get('embed_image_url') or '').strip()
    if image:
        embed.set_image(url=image)
    return embed


def build_wallet_collection_view(collection: dict) -> discord.ui.View:
    """A persistent (timeout=None) view carrying the dynamic submit button."""
    view = discord.ui.View(timeout=None)
    view.add_item(WalletSubmitButton(
        int(collection['id']),
        collection.get('button_label') or 'Submit Wallet',
    ))
    return view


def build_closed_view(collection: dict) -> discord.ui.View:
    """A non-interactive view with a single disabled button, used when a
    collection is closed so the original message reads clearly as shut."""
    view = discord.ui.View(timeout=None)
    btn = discord.ui.Button(
        label=(collection.get('button_label') or 'Submit Wallet'),
        style=discord.ButtonStyle.secondary,
        disabled=True,
        custom_id=f'wallet_collection_closed:{collection["id"]}',
    )
    view.add_item(btn)
    return view


async def post_collection_embed(collection: dict, guild: discord.Guild):
    """Send (or repost) the collection embed into its configured channel and
    persist channel_id/message_id/status. Returns the sent message. Raises
    ValueError('channel_not_found') / discord.Forbidden on failure so callers
    can roll back. Pings the configured roles (the one allowed mention send)."""
    channel = resolve_channel(guild, collection.get('channel_id'))
    if channel is None:
        raise ValueError('channel_not_found')

    embed = build_wallet_collection_embed(collection)
    view  = build_wallet_collection_view(collection)

    try:
        ping = json.loads(collection.get('ping_role_ids') or '[]')
        if not isinstance(ping, list):
            ping = []
    except (TypeError, ValueError):
        ping = []
    ping = [str(r).strip() for r in ping if str(r).strip()]
    content = ' '.join(f'<@&{r}>' for r in ping) if ping else None

    msg = await channel.send(
        content=content, embed=embed, view=view,
        allowed_mentions=_PING_ROLES,
    )
    update_wallet_collection(
        int(collection['id']), guild.id,
        channel_id=str(channel.id),
        message_id=str(msg.id),
        status='posted',
    )
    return msg


# ── Submit modal ────────────────────────────────────────────────────────────

class WalletSubmitModal(discord.ui.Modal):
    def __init__(self, collection: dict):
        super().__init__(title=(collection.get('modal_title') or 'Submit Your Wallet')[:_MODAL_TITLE_MAX])
        self.collection = collection
        self.wallet_input = discord.ui.TextInput(
            label=(collection.get('modal_field_label') or 'Your wallet address')[:_LABEL_MAX],
            placeholder=(collection.get('modal_placeholder') or '')[:_PLACEHOLDER_MAX] or None,
            max_length=_WALLET_MAX,
            style=discord.TextStyle.short,
            required=True,
        )
        self.add_item(self.wallet_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            col = self.collection
            chain = col.get('blockchain') or 'other'
            ok, result = validate_wallet(self.wallet_input.value, chain)
            if not ok:
                await interaction.response.send_message(f'❌ {result}', ephemeral=True)
                return

            action = upsert_wallet_submission(
                int(col['id']), interaction.guild_id, interaction.user.id, result,
            )

            log_event(
                interaction.guild_id, 'bot_activity',
                'wallet_submit' if action == 'insert' else 'wallet_update',
                f'Wallet {"submitted" if action == "insert" else "updated"} '
                f'for collection {col.get("name")}',
                actor_user_id=interaction.user.id,
                actor_username=str(interaction.user),
                module='engage', severity='info',
                details={
                    'collection_id': int(col['id']),
                    'user_id': str(interaction.user.id),
                    'action': 'submit_wallet' if action == 'insert' else 'update_wallet',
                    'blockchain': chain,
                },
            )

            note = ('Wallet saved. You can resubmit anytime to update it.'
                    if action == 'insert' else
                    'Wallet updated. You can resubmit anytime to change it again.')
            await interaction.response.send_message(f'✅ {note}', ephemeral=True)
        except Exception as e:
            print(f'[wallet_collection] modal on_submit error: {type(e).__name__}: {e}')
            traceback.print_exc()
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        'An error occurred saving your wallet. Try again.', ephemeral=True,
                    )
            except Exception:
                pass


# ── Persistent submit button (DynamicItem) ──────────────────────────────────

async def _handle_submit_click(interaction: discord.Interaction, collection_id: int):
    col = get_wallet_collection(collection_id, interaction.guild_id)
    if col is None:
        await interaction.response.send_message(
            'This wallet collection is no longer available.', ephemeral=True,
        )
        return
    if col.get('status') == 'closed':
        await interaction.response.send_message(
            'This wallet collection is closed and no longer accepting submissions.',
            ephemeral=True,
        )
        return

    required = col.get('required_role_id')
    if required:
        member_role_ids = {str(r.id) for r in getattr(interaction.user, 'roles', [])}
        if str(required) not in member_role_ids:
            await interaction.response.send_message(
                "You don't have the required role to submit a wallet for this collection.",
                ephemeral=True,
            )
            return

    await interaction.response.send_modal(WalletSubmitModal(col))


class WalletSubmitButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r'wallet_collection_submit:(?P<collection_id>\d+)',
):
    def __init__(self, collection_id: int, label: str = 'Submit Wallet'):
        super().__init__(
            discord.ui.Button(
                label=(label or 'Submit Wallet')[:80],
                style=discord.ButtonStyle.success,
                custom_id=f'wallet_collection_submit:{collection_id}',
                emoji='💼',
            )
        )
        self.collection_id = collection_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        cid = int(match['collection_id'])
        col = get_wallet_collection(cid, interaction.guild_id)
        label = (col.get('button_label') if col else None) or 'Submit Wallet'
        return cls(cid, label)

    async def callback(self, interaction: discord.Interaction):
        await _handle_submit_click(interaction, self.collection_id)


# ── /wallet-list pagination ──────────────────────────────────────────────────

def _page_count(total: int) -> int:
    return max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)


async def _build_list_embed(guild, client, collection, rows, page, name) -> discord.Embed:
    total       = len(rows)
    total_pages = _page_count(total)
    page        = max(0, min(page, total_pages - 1))
    start       = page * PAGE_SIZE
    end         = min(start + PAGE_SIZE, total)

    lines = []
    for offset, r in enumerate(rows[start:end]):
        gi   = start + offset + 1
        name_str = await _resolve_discord_name(guild, client, r.get('user_id'))
        lines.append(f'{gi}. {name_str} · `{r.get("wallet_address")}`')

    chain = collection.get('blockchain') or 'other'
    embed = discord.Embed(
        title       = f'Wallets · {name}',
        description = '\n'.join(lines) if lines else 'No wallets submitted yet.',
        color       = GOLD,
    )
    shown_lo = start + 1 if total else 0
    embed.set_footer(
        text=f'{CHAIN_LABELS.get(chain, chain)} · Page {page + 1} of {total_pages} '
             f'· Showing {shown_lo}-{end} of {total}'
    )
    return embed


class WalletListView(discord.ui.View):
    """Previous / Next pager for /wallet-list, locked to the original caller."""

    def __init__(self, *, collection, rows, name, original_user_id):
        super().__init__(timeout=300)
        self.collection       = collection
        self.rows             = rows
        self.name             = name
        self.original_user_id = original_user_id
        self.page             = 0
        self.total_pages      = _page_count(len(rows))
        self.message          = None

        self._prev = discord.ui.Button(label='◀ Previous', style=discord.ButtonStyle.secondary)
        self._prev.callback = self._prev_cb
        self.add_item(self._prev)
        self._next = discord.ui.Button(label='Next ▶', style=discord.ButtonStyle.secondary)
        self._next.callback = self._next_cb
        self.add_item(self._next)
        self._sync()

    def _sync(self):
        self._prev.disabled = self.page <= 0
        self._next.disabled = self.page >= self.total_pages - 1

    async def _guard(self, interaction):
        if interaction.user.id != self.original_user_id:
            await interaction.response.send_message('This list is not yours to page.', ephemeral=True)
            return False
        return True

    async def _render(self, interaction):
        self._sync()
        embed = await _build_list_embed(
            interaction.guild, interaction.client, self.collection, self.rows, self.page, self.name,
        )
        await interaction.response.edit_message(embed=embed, view=self, allowed_mentions=_NO_PING)

    async def _prev_cb(self, interaction):
        if not await self._guard(interaction):
            return
        self.page = max(0, self.page - 1)
        await self._render(interaction)

    async def _next_cb(self, interaction):
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


# ── Cog ───────────────────────────────────────────────────────────────────

def _is_admin(interaction: discord.Interaction) -> bool:
    perms = getattr(interaction.user, 'guild_permissions', None)
    return bool(perms and (perms.administrator or perms.manage_guild))


class WalletCollectionCog(commands.Cog, name='WalletCollection'):
    def __init__(self, bot):
        self.bot = bot

    # ── /wallet-collection-post ──────────────────────────────────────────
    @app_commands.command(
        name='wallet-collection-post',
        description='Admin: post a wallet collection embed to its configured channel.',
    )
    @app_commands.describe(name='The collection name set in the dashboard.')
    async def post_cmd(self, interaction: discord.Interaction, name: str):
        if not _is_admin(interaction):
            await interaction.response.send_message(
                '⚠️ Admin only. You need the Manage Server permission.', ephemeral=True,
            )
            return

        col = get_wallet_collection_by_name(interaction.guild_id, name)
        if col is None:
            await interaction.response.send_message(
                f'No wallet collection named **{name}** in this server.', ephemeral=True,
            )
            return
        if not (col.get('channel_id') or '').strip():
            await interaction.response.send_message(
                'Set a target channel for this collection in the dashboard before posting.',
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        try:
            msg = await post_collection_embed(col, interaction.guild)
        except ValueError:
            await interaction.followup.send(
                f'Channel not found: `{col.get("channel_id")}`. Update it in the dashboard.',
                ephemeral=True,
            )
            return
        except discord.Forbidden:
            await interaction.followup.send(
                'I lack permission to send in that channel.', ephemeral=True,
            )
            return

        log_event(
            interaction.guild_id, 'admin_action', 'wallet_collection_posted',
            f'Wallet collection {col.get("name")} posted',
            actor_user_id=interaction.user.id,
            actor_username=str(interaction.user),
            module='engage', severity='info',
            details={'collection_id': int(col['id']), 'channel_id': col.get('channel_id'),
                     'message_id': str(msg.id)},
        )
        await interaction.followup.send(
            f'✅ Posted **{col.get("name")}** to <#{col.get("channel_id")}>.', ephemeral=True,
        )

    # ── /wallet-collection-close ─────────────────────────────────────────
    @app_commands.command(
        name='wallet-collection-close',
        description='Admin: close a wallet collection and disable its button.',
    )
    @app_commands.describe(name='The collection name set in the dashboard.')
    async def close_cmd(self, interaction: discord.Interaction, name: str):
        if not _is_admin(interaction):
            await interaction.response.send_message(
                '⚠️ Admin only. You need the Manage Server permission.', ephemeral=True,
            )
            return

        col = get_wallet_collection_by_name(interaction.guild_id, name)
        if col is None:
            await interaction.response.send_message(
                f'No wallet collection named **{name}** in this server.', ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        update_wallet_collection(int(col['id']), interaction.guild_id, status='closed')

        # Best effort: disable the live button on the posted message.
        edited = False
        if (col.get('channel_id') or '').strip() and (col.get('message_id') or '').strip():
            channel = resolve_channel(interaction.guild, col.get('channel_id'))
            if channel is not None:
                try:
                    msg = await channel.fetch_message(int(col['message_id']))
                    await msg.edit(view=build_closed_view(col))
                    edited = True
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass

        log_event(
            interaction.guild_id, 'admin_action', 'wallet_collection_closed',
            f'Wallet collection {col.get("name")} closed',
            actor_user_id=interaction.user.id,
            actor_username=str(interaction.user),
            module='engage', severity='info',
            details={'collection_id': int(col['id']), 'button_disabled': edited},
        )
        await interaction.followup.send(
            f'✅ Closed **{col.get("name")}**. '
            + ('The button has been disabled.' if edited else 'No live message to update.'),
            ephemeral=True,
        )

    # ── /wallet-list ─────────────────────────────────────────────────────
    @app_commands.command(
        name='wallet-list',
        description='Admin: list the wallets collected for a collection.',
    )
    @app_commands.describe(name='The collection name set in the dashboard.')
    async def wallet_list_cmd(self, interaction: discord.Interaction, name: str):
        if not _is_admin(interaction):
            await interaction.response.send_message(
                '⚠️ Admin only. You need the Manage Server permission.', ephemeral=True,
            )
            return

        col = get_wallet_collection_by_name(interaction.guild_id, name)
        if col is None:
            await interaction.response.send_message(
                f'No wallet collection named **{name}** in this server.', ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        rows = list_wallet_submissions(int(col['id']), interaction.guild_id)

        log_event(
            interaction.guild_id, 'bot_activity', 'wallet_list_viewed',
            f'Admin viewed wallets for collection {col.get("name")}',
            actor_user_id=interaction.user.id,
            actor_username=str(interaction.user),
            module='engage', severity='info',
            details={'collection_id': int(col['id']), 'count': len(rows)},
        )

        embed = await _build_list_embed(
            interaction.guild, interaction.client, col, rows, 0, col.get('name'),
        )
        view = None
        if len(rows) > PAGE_SIZE:
            view = WalletListView(
                collection=col, rows=rows, name=col.get('name'),
                original_user_id=interaction.user.id,
            )

        # Copy-all: a code block members can drag-select, or a .txt attachment
        # when the list is large enough to risk Discord's message length limit.
        wallets = [r.get('wallet_address') or '' for r in rows]
        joined  = '\n'.join(wallets)
        copy_block = f'```\n{joined}\n```' if joined else None
        use_file = len(rows) > 50 or len(joined) > 1800

        msg = await interaction.followup.send(
            embed=embed, view=view, ephemeral=True,
            allowed_mentions=_NO_PING, wait=True,
        )
        if view is not None:
            view.message = msg

        if not rows:
            return

        if use_file:
            buf = io.BytesIO(joined.encode('utf-8'))
            fname = f'wallets_{col.get("name")}.txt'.replace(' ', '_')
            await interaction.followup.send(
                content='Copy all wallets (open the file and select all):',
                file=discord.File(buf, filename=fname),
                ephemeral=True, allowed_mentions=_NO_PING,
            )
        else:
            await interaction.followup.send(
                content=f'Copy all wallets:\n{copy_block}',
                ephemeral=True, allowed_mentions=_NO_PING,
            )


async def setup(bot):
    await bot.add_cog(WalletCollectionCog(bot))
    bot.add_dynamic_items(WalletSubmitButton)
