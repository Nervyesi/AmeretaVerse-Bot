"""
embed_message.py — Embed Message module Discord-side helper.

Builds a discord.Embed from an embed_messages DB row using the existing
build_branded_embed system so brand consistency is automatic (color, footer,
thumbnail all flow from the guild's brand settings unless explicitly
overridden on the row).

This file deliberately does NOT register a Cog: the entire surface is driven
from the dashboard (api.py endpoints) which import build_embed_from_row()
when posting / editing / resending. Keeping the module Cog-free avoids
adding slash commands and keeps the Discord side a thin renderer.
"""
import json
import discord

from cogs._branding import build_branded_embed


# Discord platform caps (kept here so callers can validate the same way).
EMBED_TITLE_MAX        = 256
EMBED_DESCRIPTION_MAX  = 4000
EMBED_FIELD_NAME_MAX   = 256
EMBED_FIELD_VALUE_MAX  = 1024
EMBED_FIELDS_MAX       = 10
EMBED_TOTAL_MAX        = 6000


def _safe_color(value) -> int | None:
    """Accept an int or '#RRGGBB' string; return an int or None."""
    if value is None or value == '':
        return None
    if isinstance(value, int):
        return value & 0xFFFFFF
    if isinstance(value, str):
        v = value.strip().lstrip('#')
        if not v:
            return None
        try:
            return int(v, 16) & 0xFFFFFF
        except ValueError:
            return None
    return None


def _parse_fields(fields_json) -> list[dict]:
    """Parse the fields_json column into a clean list of {name,value,inline}
    dicts. Bad JSON, wrong shape, or empty entries silently produce []."""
    if not fields_json:
        return []
    try:
        data = fields_json if isinstance(fields_json, list) else json.loads(fields_json)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    out: list[dict] = []
    for entry in data[:EMBED_FIELDS_MAX]:
        if not isinstance(entry, dict):
            continue
        name  = str(entry.get('name')  or '').strip()
        value = str(entry.get('value') or '').strip()
        if not name and not value:
            continue
        inline = bool(entry.get('inline'))
        out.append({
            'name':   name[:EMBED_FIELD_NAME_MAX]  or '​',
            'value':  value[:EMBED_FIELD_VALUE_MAX] or '​',
            'inline': inline,
        })
    return out


def build_embed_from_row(guild_id: int, row: dict) -> discord.Embed:
    """Render an embed_messages row through the guild's branding pipeline.

    Color/thumbnail on the row override the guild brand defaults; everything
    else (footer text/icon) flows through build_branded_embed as usual so the
    posted embed matches the rest of the bot's look-and-feel.
    """
    title       = (row.get('title')       or '')[:EMBED_TITLE_MAX]
    description = (row.get('description') or '')[:EMBED_DESCRIPTION_MAX]

    # build_branded_embed honors brand color/footer/thumbnail by default.
    # We pass cog_prefix='' so it only consults the guild-level brand, not a
    # per-cog override key. Then we apply the row's overrides on top.
    embed = build_branded_embed(
        guild_id,
        title=title,
        description=description,
        cog_prefix='',
        use_thumbnail=True,
        use_image=False,
        use_footer=True,
        use_author=False,
    )

    color_override = _safe_color(row.get('color'))
    if color_override is not None:
        embed.color = discord.Color(color_override)

    thumb = (row.get('thumbnail_url') or '').strip()
    if thumb:
        embed.set_thumbnail(url=thumb)

    image_url = (row.get('image_url') or '').strip()
    if image_url:
        embed.set_image(url=image_url)

    for f in _parse_fields(row.get('fields_json')):
        embed.add_field(name=f['name'], value=f['value'], inline=f['inline'])

    return embed
