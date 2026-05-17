"""
Brand & visual customization helper.

This is the ONE place where bot embed styling decisions live. All cogs that
build embeds should call build_branded_embed() here, not construct embeds
with set_thumbnail/set_image/etc. themselves.

DESIGN: per-guild config keys (with sensible defaults) control:
- Embed color (hex)
- Default thumbnail URL (small image, top-right)
- Default image URL (large image at bottom — can be GIF)
- Footer text + icon URL
- Author name + icon URL

PLAN-AWARE FUTURE: each get_X helper checks a future server_plan config key.
Free plan: branding values come from AVbot defaults (BRAND_DEFAULTS).
Premium+ plan: branding values come from per-guild config (overriding defaults).

Today, server_plan is always 'free' for everyone EXCEPT the AmeretaVerse guild
(hardcoded ID below) which is treated as 'premium' so its admin can customize
the bot's appearance for testing.

When monetization launches, we just change the plan-detection logic and
existing cogs need NO code changes.
"""
import discord
from database import get_config, get_guild_settings as _gs

# AmeretaVerse main server — always treated as premium for branding purposes.
# This lets us test customization features on the main server while everyone
# else sees the AVbot default brand.
PREMIUM_GUILD_IDS = {1199707792706117642}

# AVbot default brand. These are the values everyone on free plan sees.
# When you give us official assets later, just update these constants.
BRAND_DEFAULTS = {
    'color':            0x94730D,
    'thumbnail_url':    '',
    'image_url':        '',
    'footer_text':      'Powered by AVbot',
    'footer_icon_url':  '',
    'author_name':      '',
    'author_icon_url':  '',
}


def get_plan(guild_id: int) -> str:
    """
    Return 'free' or 'premium' for this guild.
    Today: AmeretaVerse main is premium, everyone else is free.
    Future: read from a 'server_plan' DB row managed by billing system.
    """
    if guild_id in PREMIUM_GUILD_IDS:
        return 'premium'
    plan = get_config(guild_id, 'server_plan', 'free') or 'free'
    return plan if plan in ('free', 'premium', 'premium_plus') else 'free'


def can_customize(guild_id: int) -> bool:
    """Can this guild override AVbot brand defaults?"""
    return get_plan(guild_id) in ('premium', 'premium_plus')


def get_brand_value(guild_id: int, key: str, cog_override_key: str = None):
    """
    Return the current brand value for a given key.
    Resolution order:
      1. If guild can customize AND has a value for cog_override_key in config: use it
      2. If guild can customize AND has a value for brand_{key} in config: use it
      3. Otherwise: use BRAND_DEFAULTS[key]
    """
    customizable = can_customize(guild_id)

    if customizable and cog_override_key:
        cog_val = get_config(guild_id, cog_override_key, '') or ''
        if cog_val:
            if key == 'color' and cog_val.startswith('#'):
                try:
                    return int(cog_val.lstrip('#'), 16)
                except ValueError:
                    pass
            elif key != 'color':
                return cog_val

    if customizable:
        global_key = f'brand_{key}'
        global_val = get_config(guild_id, global_key, '') or ''
        if global_val:
            if key == 'color' and global_val.startswith('#'):
                try:
                    return int(global_val.lstrip('#'), 16)
                except ValueError:
                    pass
            elif key != 'color':
                return global_val

    # Guild-level settings fallback (guild_settings table)
    brand_defs = _gs(guild_id)
    if key == 'color':
        gs_color = brand_defs.get('default_embed_color') or ''
        if gs_color.startswith('#'):
            try:
                return int(gs_color.lstrip('#'), 16)
            except ValueError:
                pass
    elif key == 'thumbnail_url':
        gs_val = brand_defs.get('default_thumbnail_url') or ''
        if gs_val:
            return gs_val
    elif key == 'footer_text':
        gs_val = brand_defs.get('default_footer_text') or ''
        if gs_val:
            return gs_val
    elif key == 'footer_icon_url':
        gs_val = brand_defs.get('default_footer_icon_url') or ''
        if gs_val:
            return gs_val

    return BRAND_DEFAULTS.get(key, '')


def build_branded_embed(
    guild_id: int,
    *,
    title: str = '',
    description: str = '',
    cog_prefix: str = '',
    use_thumbnail: bool = True,
    use_image: bool = False,
    use_footer: bool = True,
    use_author: bool = False,
) -> discord.Embed:
    """
    Build an embed with brand-aware visual styling.

    cog_prefix: if set (e.g. 'verify'), the helper looks for {prefix}_thumbnail_url etc
                as cog-specific overrides BEFORE falling back to global brand_* then BRAND_DEFAULTS.
    use_thumbnail/use_image/use_footer/use_author: which visual elements to apply.
    """
    color = get_brand_value(
        guild_id, 'color',
        f'{cog_prefix}_color' if cog_prefix else None,
    )
    embed = discord.Embed(title=title, description=description, color=color)

    if use_thumbnail:
        url = get_brand_value(
            guild_id, 'thumbnail_url',
            f'{cog_prefix}_thumbnail_url' if cog_prefix else None,
        )
        if url:
            embed.set_thumbnail(url=url)

    if use_image:
        url = get_brand_value(
            guild_id, 'image_url',
            f'{cog_prefix}_image_url' if cog_prefix else None,
        )
        if url:
            embed.set_image(url=url)

    if use_footer:
        text = get_brand_value(
            guild_id, 'footer_text',
            f'{cog_prefix}_footer_text' if cog_prefix else None,
        )
        icon = get_brand_value(
            guild_id, 'footer_icon_url',
            f'{cog_prefix}_footer_icon_url' if cog_prefix else None,
        )
        if text or icon:
            embed.set_footer(text=text or '', icon_url=icon or None)

    if use_author:
        name = get_brand_value(
            guild_id, 'author_name',
            f'{cog_prefix}_author_name' if cog_prefix else None,
        )
        icon = get_brand_value(
            guild_id, 'author_icon_url',
            f'{cog_prefix}_author_icon_url' if cog_prefix else None,
        )
        if name:
            embed.set_author(name=name, icon_url=icon or None)

    return embed
