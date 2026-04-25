import discord


def resolve_channel(guild: discord.Guild, value: str):
    """Resolve a text channel by name or ID. Returns None if not found."""
    if not value or not value.strip():
        return None
    v = value.strip()
    try:
        return guild.get_channel(int(v))
    except (ValueError, TypeError):
        pass
    return discord.utils.get(guild.text_channels, name=v)


def resolve_role(guild: discord.Guild, value: str):
    """Resolve a role by name or ID. Returns None if not found."""
    if not value or not value.strip():
        return None
    v = value.strip()
    try:
        return guild.get_role(int(v))
    except (ValueError, TypeError):
        pass
    return discord.utils.get(guild.roles, name=v)


def resolve_category(guild: discord.Guild, value: str):
    """Resolve a category channel by name or ID. Returns None if not found."""
    if not value or not value.strip():
        return None
    v = value.strip()
    try:
        return guild.get_channel(int(v))
    except (ValueError, TypeError):
        pass
    return discord.utils.get(guild.categories, name=v)
