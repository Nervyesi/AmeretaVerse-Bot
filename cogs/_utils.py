import discord


def resolve_channel(guild: discord.Guild, value):
    """Accept channel name, channel ID (str or int), or mention (<#id> / #name). Return Channel or None."""
    if value is None:
        return None
    # Strip mention syntax and leading # before deciding name-vs-id
    s = str(value).strip().lstrip('<#').rstrip('>')
    s = s.lstrip('#').strip()
    if not s:
        return None
    if s.isdigit():
        ch = guild.get_channel(int(s))
        if ch:
            return ch
    return (
        discord.utils.get(guild.text_channels, name=s)
        or discord.utils.get(guild.channels, name=s)
    )


def resolve_role(guild: discord.Guild, value):
    """Accept role name, role ID (str or int), or mention (<@&id> / @name). Return Role or None."""
    if value is None:
        return None
    # Strip mention syntax and leading @ before deciding name-vs-id
    s = str(value).strip()
    s = s.lstrip('<@&').rstrip('>')
    s = s.lstrip('@').strip()
    if not s:
        return None
    if s.isdigit():
        r = guild.get_role(int(s))
        if r:
            return r
    return discord.utils.get(guild.roles, name=s)


def resolve_category(guild: discord.Guild, value):
    """Accept category name or ID (str or int). Return CategoryChannel or None."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if s.isdigit():
        ch = guild.get_channel(int(s))
        if ch:
            return ch
    return discord.utils.get(guild.categories, name=s)
