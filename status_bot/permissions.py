import discord


def can_manage_target(guild: discord.Guild, target: discord.Member) -> tuple[bool, str]:
    me = guild.me
    if me is None:
        return False, "Botのメンバー情報が取得できません。"

    if target.id == guild.owner_id:
        return False, "サーバーオーナーには変更できません。"

    if target == me:
        return False, "Bot自身には実行できません。"

    if target.top_role >= me.top_role:
        return False, "Botより上位または同位のロールを持つ相手には変更できません。"

    return True, ""


def has_manage_roles(interaction: discord.Interaction) -> bool:
    if interaction.guild is None:
        return False
    if not isinstance(interaction.user, discord.Member):
        return False
    return interaction.user.guild_permissions.manage_roles


def has_manage_guild(interaction: discord.Interaction) -> bool:
    if interaction.guild is None:
        return False
    if not isinstance(interaction.user, discord.Member):
        return False
    perms = interaction.user.guild_permissions
    return perms.manage_guild or perms.administrator
