"""
giveaway.py — Giveaway module Discord side.

Two persistent dynamic buttons attached to a posted giveaway embed:
  - Enter Giveaway  (custom_id `giveaway:enter:<gid>`)
  - Show Status     (custom_id `giveaway:status:<gid>`)

A 30-second scheduler tick finalizes giveaways whose `ends_at` has passed and
recovers any that were left in the `drawing` state by a crashed process. The
draw uses a seeded `random.Random(random_seed)` so the result is reproducible
and auditable months later if ever questioned.

Community-points integration goes through the existing `raid_user_points`
table via the database helpers `enter_giveaway_atomic` (deduct + insert in one
transaction) and `refund_giveaway_entries` (cancel path). Engage points are
not touched.
"""
import json
import random
import asyncio
import re as _re
from collections import deque
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from database import (
    get_connection,
    get_giveaway,
    update_giveaway,
    list_giveaway_entries,
    count_giveaway_entries,
    get_giveaway_entry,
    enter_giveaway_atomic,
    claim_giveaway_for_draw,
    list_due_giveaways,
    log_event,
    GiveawayInsufficientPoints,
    GiveawayAlreadyEntered,
)
from cogs._branding import build_branded_embed


# ── Helpers ──────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        # Accept both naive and tz-aware ISO strings.
        s2 = s.replace('Z', '+00:00')
        d = datetime.fromisoformat(s2)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d
    except (TypeError, ValueError):
        return None


def _ends_unix(g: dict) -> int | None:
    d = _parse_iso(g.get('ends_at'))
    return int(d.timestamp()) if d else None


def _allowed_role_ids(g: dict) -> list[str]:
    raw = g.get('allowed_role_ids') or '[]'
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    out: list[str] = []
    for v in data:
        s = str(v).strip()
        if s:
            out.append(s)
    return out


def _mention_role_ids(g: dict) -> list[str]:
    """Pinged-on-post role ids. Prefer the new mention_role_ids JSON list;
    fall back to wrapping the legacy single mention_role_id for older draft
    rows so back-compat holds."""
    raw = g.get('mention_role_ids') or '[]'
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        data = []
    if not isinstance(data, list):
        data = []
    out: list[str] = []
    for v in data:
        s = str(v).strip()
        if s:
            out.append(s)
    if not out and g.get('mention_role_id'):
        legacy = str(g['mention_role_id']).strip()
        if legacy:
            out.append(legacy)
    return out


def _winner_ids(g: dict) -> list[str]:
    raw = g.get('winners_json') or '[]'
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [str(v).strip() for v in data if str(v).strip()]


# ── Embed builders ───────────────────────────────────────────────────────────

def build_giveaway_embed(guild_id: int, g: dict, entry_count: int) -> discord.Embed:
    """Render a giveaway row through build_branded_embed and layer on the
    giveaway-specific fields (prize, ends-at relative timestamp, entries,
    winners list, cancellation note)."""
    status = g.get('status') or 'draft'
    title  = (g.get('title') or 'Giveaway')[:256]
    desc   = (g.get('description') or '')[:4000]

    embed = build_branded_embed(
        int(guild_id),
        title=title,
        description=desc,
        cog_prefix='',
        use_thumbnail=True,
        use_image=False,
        use_footer=True,
    )

    # Per-giveaway overrides on top of the brand defaults.
    if g.get('color') is not None:
        try:
            embed.color = discord.Color(int(g['color']) & 0xFFFFFF)
        except (TypeError, ValueError):
            pass
    thumb = (g.get('thumbnail_url') or '').strip()
    if thumb:
        embed.set_thumbnail(url=thumb)
    image = (g.get('image_url') or '').strip()
    if image:
        embed.set_image(url=image)

    prize = (g.get('prize') or '').strip()
    if prize:
        embed.add_field(name='🎁 Prize', value=prize[:1024], inline=False)

    winners_n = max(1, int(g.get('winner_count') or 1))
    cost      = max(0, int(g.get('entry_cost_points') or 0))
    role_ids  = _allowed_role_ids(g)

    meta_lines: list[str] = []
    meta_lines.append(f'**Winners:** {winners_n}')
    meta_lines.append('**Entry cost:** ' + (
        f'{cost:,} community points' if cost > 0 else 'Free'
    ))
    if role_ids:
        mentions = ' '.join(f'<@&{rid}>' for rid in role_ids[:8])
        meta_lines.append(f'**Required roles:** {mentions}')
    else:
        meta_lines.append('**Open to everyone**')

    ends_unix = _ends_unix(g)
    if status == 'active' and ends_unix:
        meta_lines.append(f'**Ends:** <t:{ends_unix}:R> (<t:{ends_unix}:F>)')
    elif status in ('ended', 'drawing') and g.get('ended_at'):
        ended = _parse_iso(g.get('ended_at'))
        if ended:
            meta_lines.append(f'**Ended:** <t:{int(ended.timestamp())}:R>')
    elif status == 'cancelled':
        meta_lines.append('**Cancelled.** All paid entries were refunded.')
    elif status == 'draft':
        meta_lines.append('**Draft** — not yet posted.')

    embed.add_field(name='Details', value='\n'.join(meta_lines), inline=False)
    embed.add_field(
        name='Entries',
        value=f'**{int(entry_count or 0):,}** entered',
        inline=True,
    )

    winners = _winner_ids(g)
    if status == 'ended':
        if winners:
            mentions = ', '.join(f'<@{w}>' for w in winners[:25])
            embed.add_field(name='🏆 Winners', value=mentions, inline=False)
        else:
            embed.add_field(
                name='🏆 Winners',
                value='No entrants — no winner could be drawn.',
                inline=False,
            )

    return embed


def build_giveaway_view(g: dict) -> discord.ui.View:
    """A persistent view with Enter + Status buttons (active) or a single
    disabled "Ended" button (ended/cancelled). The button labels and styles
    are stable so persistent dispatch works after restart."""
    gid    = int(g['id'])
    status = g.get('status') or 'draft'
    view   = discord.ui.View(timeout=None)

    if status == 'active':
        view.add_item(GiveawayEnterButton(gid))
        view.add_item(GiveawayStatusButton(gid))
    else:
        ended_label = {
            'ended':     '🏁 Giveaway Ended',
            'drawing':   '🎲 Drawing…',
            'cancelled': '🚫 Cancelled',
            'draft':     'Draft',
        }.get(status, 'Closed')
        btn = discord.ui.Button(
            style=discord.ButtonStyle.secondary,
            label=ended_label,
            custom_id=f'giveaway:closed:{gid}',
            disabled=True,
        )
        view.add_item(btn)

    return view


# ── Persistent dynamic buttons ───────────────────────────────────────────────

class GiveawayEnterButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r'giveaway:enter:(?P<gid>\d+)',
):
    def __init__(self, giveaway_id: int):
        super().__init__(
            discord.ui.Button(
                style=discord.ButtonStyle.success,
                label='Enter Giveaway',
                emoji='🎉',
                custom_id=f'giveaway:enter:{giveaway_id}',
            )
        )
        self.giveaway_id = int(giveaway_id)

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match['gid']))

    async def callback(self, interaction: discord.Interaction):
        # ALWAYS defer first so Discord's 3s window never bites us.
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.HTTPException:
            pass

        cog = interaction.client.get_cog('Giveaway')
        if cog is None:
            try:
                await interaction.followup.send(
                    'Giveaway module is unavailable. Try again in a moment.',
                    ephemeral=True,
                )
            except discord.HTTPException:
                pass
            return
        await cog.handle_enter(interaction, self.giveaway_id)


class GiveawayStatusButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r'giveaway:status:(?P<gid>\d+)',
):
    def __init__(self, giveaway_id: int):
        super().__init__(
            discord.ui.Button(
                style=discord.ButtonStyle.secondary,
                label='Show Status',
                emoji='ℹ️',
                custom_id=f'giveaway:status:{giveaway_id}',
            )
        )
        self.giveaway_id = int(giveaway_id)

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match['gid']))

    async def callback(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.HTTPException:
            pass

        cog = interaction.client.get_cog('Giveaway')
        if cog is None:
            return
        await cog.handle_status(interaction, self.giveaway_id)


# ── Cog ──────────────────────────────────────────────────────────────────────

class Giveaway(commands.Cog):
    """The Discord side of the Giveaway module. The dashboard (api.py) owns
    create/edit/start/end/cancel; this cog owns the runtime: button handlers,
    the periodic tick that finalizes giveaways whose ends_at has passed, and
    the embed-refresh debouncer."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Per-user click rate limit on Enter (3 clicks per 10 seconds). Catches
        # accidental double-clicks and stops accidental hammering. Per-user is
        # enough — the uniqueness index in the schema is the real defence.
        self._enter_buckets: dict[int, deque] = {}
        self._enter_lock = asyncio.Lock()
        # Debounced embed refresh: each click schedules an edit ~3s in the
        # future, coalescing bursts. A second click within the window resets
        # the timer; the latest entry count is what gets posted.
        self._refresh_tasks: dict[int, asyncio.Task] = {}
        self._refresh_lock  = asyncio.Lock()

        self.scheduler_tick.start()

    def cog_unload(self):
        self.scheduler_tick.cancel()
        for t in self._refresh_tasks.values():
            t.cancel()

    # ── Enter button handler ─────────────────────────────────────────────

    async def handle_enter(self, interaction: discord.Interaction, giveaway_id: int):
        guild_id = interaction.guild_id or 0
        user     = interaction.user

        if user.bot or not guild_id:
            await self._ephemeral(interaction, 'This giveaway is for server members only.')
            return

        # Soft per-user click rate cap.
        if not await self._enter_rate_ok(user.id):
            await self._ephemeral(interaction, 'Slow down a moment, then try again.')
            return

        g = get_giveaway(giveaway_id, guild_id)
        if g is None or int(g['guild_id']) != int(guild_id):
            await self._ephemeral(interaction, 'This giveaway is not available in this server.')
            log_event(guild_id, 'admin_action', 'giveaway_entry_skipped',
                      f'Giveaway #{giveaway_id} entry skipped: not_found',
                      module='giveaway', severity='info',
                      details={'giveaway_id': giveaway_id, 'user_id': user.id,
                               'reason': 'not_found'})
            return

        status = g.get('status') or 'draft'
        if status != 'active':
            msg = {
                'ended':     'This giveaway has already ended.',
                'drawing':   'Winners are being drawn right now.',
                'cancelled': 'This giveaway has been cancelled.',
                'draft':     'This giveaway has not started yet.',
            }.get(status, 'This giveaway is not accepting entries.')
            await self._ephemeral(interaction, msg)
            log_event(guild_id, 'admin_action', 'giveaway_entry_skipped',
                      f'Giveaway #{giveaway_id} entry skipped: status={status}',
                      module='giveaway', severity='info',
                      details={'giveaway_id': giveaway_id, 'user_id': user.id,
                               'reason': f'status={status}'})
            return

        # ends_at sanity check — if the scheduler hasn't ticked yet but time is
        # up, refuse the entry rather than letting it slip in after end.
        ends = _parse_iso(g.get('ends_at'))
        if ends and datetime.now(timezone.utc) >= ends:
            await self._ephemeral(interaction, 'This giveaway just ended. Winners are being drawn.')
            return

        # Role restriction.
        role_ids = _allowed_role_ids(g)
        if role_ids:
            user_role_ids = {str(r.id) for r in getattr(user, 'roles', [])}
            if not (user_role_ids & set(role_ids)):
                mentions = ' '.join(f'<@&{rid}>' for rid in role_ids[:8])
                await self._ephemeral(
                    interaction,
                    f'You need one of these roles to enter: {mentions}',
                )
                log_event(guild_id, 'admin_action', 'giveaway_entry_skipped',
                          f'Giveaway #{giveaway_id} entry skipped: role_gated',
                          module='giveaway', severity='info',
                          details={'giveaway_id': giveaway_id, 'user_id': user.id,
                                   'reason': 'role_gated'})
                return

        # Already entered?
        if get_giveaway_entry(giveaway_id, guild_id, user.id):
            total = count_giveaway_entries(giveaway_id, guild_id)
            odds  = f'1 in {total:,}' if total > 0 else '—'
            await self._ephemeral(
                interaction,
                f'You are already entered. Good luck!\n'
                f'**Entries:** {total:,}\n**Your odds:** {odds}',
            )
            return

        cost = max(0, int(g.get('entry_cost_points') or 0))

        try:
            res = enter_giveaway_atomic(giveaway_id, guild_id, user.id, cost)
        except GiveawayInsufficientPoints as e:
            need = max(0, e.required - e.balance)
            await self._ephemeral(
                interaction,
                f'You need **{need:,}** more community points to enter '
                f'(balance: {e.balance:,}, cost: {e.required:,}).',
            )
            log_event(guild_id, 'admin_action', 'giveaway_entry_skipped',
                      f'Giveaway #{giveaway_id} entry skipped: insufficient_points',
                      module='giveaway', severity='info',
                      details={'giveaway_id': giveaway_id, 'user_id': user.id,
                               'reason': 'insufficient_points',
                               'balance': e.balance, 'required': e.required})
            return
        except GiveawayAlreadyEntered:
            total = count_giveaway_entries(giveaway_id, guild_id)
            await self._ephemeral(
                interaction,
                f'You are already entered. Good luck!\n**Entries:** {total:,}',
            )
            return
        except Exception as exc:  # noqa: BLE001
            print(f'[giveaway] enter failure gid={giveaway_id} user={user.id}: '
                  f'{type(exc).__name__}: {exc}')
            await self._ephemeral(interaction, 'Could not record your entry. Try again shortly.')
            return

        total = count_giveaway_entries(giveaway_id, guild_id)
        odds  = f'1 in {total:,}' if total > 0 else '—'
        balance_line = (
            f'\n**Balance:** {res["new_balance"]:,} community points'
            if cost > 0 else ''
        )
        await self._ephemeral(
            interaction,
            f'You are in! 🎉\n**Entries:** {total:,}\n**Your odds:** {odds}{balance_line}',
        )

        log_event(guild_id, 'admin_action', 'giveaway_entered',
                  f'Giveaway #{giveaway_id} entered by {user}',
                  actor_user_id=user.id, actor_username=str(user),
                  module='giveaway', severity='info',
                  details={'giveaway_id': giveaway_id, 'user_id': user.id,
                           'cost': cost, 'entries_after': total})

        # Schedule a debounced embed refresh so the entries count stays live
        # without rate-limiting Discord on a flood.
        await self._schedule_refresh(int(g['id']), guild_id)

    # ── Status button handler ───────────────────────────────────────────

    async def handle_status(self, interaction: discord.Interaction, giveaway_id: int):
        guild_id = interaction.guild_id or 0
        g = get_giveaway(giveaway_id, guild_id)
        if g is None:
            await self._ephemeral(interaction, 'This giveaway is not available in this server.')
            return

        total = count_giveaway_entries(giveaway_id, guild_id)
        already = get_giveaway_entry(giveaway_id, guild_id, interaction.user.id)

        ends_unix = _ends_unix(g)
        lines = [
            f'**Title:** {g.get("title") or "Giveaway"}',
            f'**Prize:** {g.get("prize") or "—"}',
            f'**Winners:** {int(g.get("winner_count") or 1)}',
            f'**Entries:** {total:,}',
        ]
        if g.get('status') == 'active' and ends_unix:
            lines.append(f'**Ends:** <t:{ends_unix}:R>')
        cost = max(0, int(g.get('entry_cost_points') or 0))
        lines.append('**Entry cost:** ' + (f'{cost:,} community points' if cost > 0 else 'Free'))
        if already:
            odds = f'1 in {total:,}' if total > 0 else '—'
            lines.append(f'\n✅ You are entered. Your odds: **{odds}**')
        else:
            lines.append('\n🟡 You are NOT entered yet. Click **Enter Giveaway** to join.')

        await self._ephemeral(interaction, '\n'.join(lines))

    # ── Live embed refresh (debounced) ──────────────────────────────────

    async def _schedule_refresh(self, giveaway_id: int, guild_id: int):
        """Coalesce a burst of entries into a single embed edit ~3s later."""
        async with self._refresh_lock:
            existing = self._refresh_tasks.get(giveaway_id)
            if existing and not existing.done():
                existing.cancel()
            self._refresh_tasks[giveaway_id] = asyncio.create_task(
                self._do_refresh_later(giveaway_id, guild_id, delay=3.0)
            )

    async def _do_refresh_later(self, giveaway_id: int, guild_id: int, delay: float):
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        try:
            await self.refresh_embed(giveaway_id, guild_id)
        except Exception as e:  # noqa: BLE001
            print(f'[giveaway] refresh failed gid={giveaway_id}: '
                  f'{type(e).__name__}: {e}')

    async def refresh_embed(self, giveaway_id: int, guild_id: int):
        """Edit the live Discord message in place with the latest state. No-op
        if the giveaway has no message_id (draft) or the channel/message is
        gone. Never raises out."""
        g = get_giveaway(giveaway_id, guild_id)
        if not g:
            return
        ch_id  = g.get('channel_id')
        msg_id = g.get('message_id')
        if not ch_id or not msg_id:
            return
        guild = self.bot.get_guild(int(guild_id))
        if guild is None:
            return
        try:
            channel = guild.get_channel(int(ch_id))
        except (TypeError, ValueError):
            channel = None
        if channel is None:
            return
        try:
            msg = await channel.fetch_message(int(msg_id))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return
        entry_count = count_giveaway_entries(giveaway_id, guild_id)
        embed = build_giveaway_embed(guild_id, g, entry_count)
        view  = build_giveaway_view(g)
        try:
            await msg.edit(embed=embed, view=view)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
            print(f'[giveaway] embed edit failed gid={giveaway_id}: '
                  f'{type(e).__name__}: {e}')

    # ── Scheduler / draw ────────────────────────────────────────────────

    @tasks.loop(seconds=30)
    async def scheduler_tick(self):
        try:
            due = list_due_giveaways(_now_iso())
        except Exception as e:  # noqa: BLE001
            print(f'[giveaway] scheduler list_due failed: {type(e).__name__}: {e}')
            return

        for row in due:
            try:
                await self._finalize_one(row)
            except Exception as e:  # noqa: BLE001
                print(f'[giveaway] finalize failed gid={row.get("id")}: '
                      f'{type(e).__name__}: {e}')

    @scheduler_tick.before_loop
    async def _before_scheduler(self):
        await self.bot.wait_until_ready()

    async def _finalize_one(self, g: dict):
        """Atomically claim the giveaway for drawing, run the draw, edit the
        embed, and announce winners in the channel. Idempotent: a giveaway in
        'drawing' state that gets seen again (e.g. crash mid-draw) will be
        claimed by claim_giveaway_for_draw exactly once (the second caller
        sees rowcount=0)."""
        gid      = int(g['id'])
        guild_id = int(g['guild_id'])
        status   = g.get('status') or 'active'

        # Two paths into here: status=='active' (normal end) and status=='drawing'
        # (recovery from a crash). For the active path we need to flip to
        # 'drawing' atomically so a parallel tick doesn't double-draw.
        if status == 'active':
            if not claim_giveaway_for_draw(gid, guild_id):
                return  # Someone else got it first.
        elif status == 'drawing':
            print(f'[giveaway] recovering stuck draw gid={gid}')

        # Reload AFTER the claim so we see the 'drawing' status the rest of
        # the system observes.
        g = get_giveaway(gid, guild_id) or g

        entries = list_giveaway_entries(gid, guild_id)
        user_ids = [int(e['user_id']) for e in entries
                    if e.get('user_id') is not None]

        winner_count = max(1, int(g.get('winner_count') or 1))
        seed = g.get('random_seed') or ''
        if not seed:
            # Edge case: a 'drawing' row from before random_seed was set. Use
            # a deterministic-ish fallback so the draw is still reproducible.
            seed = f'gid={gid}|started={g.get("started_at") or ""}'

        rng = random.Random(seed)
        if user_ids:
            n = min(winner_count, len(user_ids))
            winner_ids = rng.sample(user_ids, n)
        else:
            winner_ids = []

        ended_at = _now_iso()
        update_giveaway(
            gid, guild_id,
            status='ended',
            ended_at=ended_at,
            winners_json=json.dumps([str(w) for w in winner_ids]),
        )

        log_event(
            guild_id, 'admin_action', 'giveaway_ended',
            f'Giveaway #{gid} ended ({len(winner_ids)} winner(s) from {len(user_ids)} entrants)',
            module='giveaway', severity='info',
            details={'giveaway_id': gid, 'winners': [str(w) for w in winner_ids],
                     'entrants': len(user_ids)},
        )

        # Refresh embed in place + announce winners.
        fresh = get_giveaway(gid, guild_id) or g
        await self.refresh_embed(gid, guild_id)
        await self._announce_winners(fresh, winner_ids, len(user_ids))

    async def _announce_winners(self, g: dict, winner_ids: list[int], entrants: int):
        """Post a separate, unmissable announcement message in the same channel
        as the giveaway embed. Soft-fail if the channel was deleted."""
        ch_id = g.get('channel_id')
        if not ch_id:
            return
        guild = self.bot.get_guild(int(g['guild_id']))
        if guild is None:
            return
        try:
            ch = guild.get_channel(int(ch_id))
        except (TypeError, ValueError):
            ch = None
        if ch is None:
            print(f'[giveaway] announce skipped: channel gone gid={g["id"]}')
            return

        prize = (g.get('prize') or g.get('title') or 'the giveaway').strip()
        if winner_ids:
            mentions = ' '.join(f'<@{w}>' for w in winner_ids)
            content  = (f'🎉 Congrats {mentions}! You won **{prize}**.\n'
                        f'({entrants:,} entrants)')
        else:
            content  = (f'🎲 The giveaway **{prize}** ended with no entrants, '
                        f'so no winner could be drawn.')
        try:
            await ch.send(
                content=content,
                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
            )
        except (discord.Forbidden, discord.HTTPException) as e:
            print(f'[giveaway] announce send failed gid={g["id"]}: '
                  f'{type(e).__name__}: {e}')

    # ── Restart recovery ─────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_ready(self):
        """Catch giveaways whose ends_at passed while the bot was down, plus
        any 'drawing' state left by a crash."""
        try:
            due = list_due_giveaways(_now_iso())
        except Exception as e:  # noqa: BLE001
            print(f'[giveaway] on_ready list_due failed: {type(e).__name__}: {e}')
            return
        if due:
            print(f'[giveaway] on_ready: finalizing {len(due)} due/stuck giveaway(s)')
        for row in due:
            try:
                await self._finalize_one(row)
            except Exception as e:  # noqa: BLE001
                print(f'[giveaway] on_ready finalize failed gid={row.get("id")}: '
                      f'{type(e).__name__}: {e}')

    # ── Rate-limit + ephemeral helpers ──────────────────────────────────

    async def _enter_rate_ok(self, user_id: int, max_calls: int = 3, window: float = 10.0) -> bool:
        from time import monotonic
        async with self._enter_lock:
            dq = self._enter_buckets.get(user_id)
            if dq is None:
                dq = deque()
                self._enter_buckets[user_id] = dq
            now = monotonic()
            cutoff = now - window
            while dq and dq[0] < cutoff:
                dq.popleft()
            if len(dq) >= max_calls:
                return False
            dq.append(now)
            return True

    async def _ephemeral(self, interaction: discord.Interaction, text: str):
        try:
            await interaction.followup.send(text, ephemeral=True)
        except discord.HTTPException as e:
            print(f'[giveaway] followup failed: {type(e).__name__}: {e}')


async def setup(bot: commands.Bot):
    await bot.add_cog(Giveaway(bot))
    # Persistent dispatch for the dynamic buttons survives bot restarts; the
    # buttons stored in posted messages keep working without rebuilding views.
    bot.add_dynamic_items(GiveawayEnterButton, GiveawayStatusButton)
