"""操作できる人の確認。

コマンドと、状態を変えるボタンは、Bot を使うサーバーで「サーバーの管理」権限を持つメンバーだけに許す。
コマンドの default_permissions は表示の既定値にすぎず（サーバー側で変えられ、DM では効かない）、
ボタンにはそもそも効かないので、ここでも確かめる。
"""

from __future__ import annotations

import os

import discord
from discord import app_commands

DENIED = "🔒 この操作は、サーバーの「サーバーの管理」権限を持つメンバーだけが使えます。"


def allowed(interaction: discord.Interaction) -> bool:
    """設定したサーバーの中で、「サーバーの管理」権限を持つメンバーの操作なら True（DM は不可）。"""
    if interaction.guild_id is None:
        return False
    if (guild_id := os.getenv("DISCORD_GUILD_ID")) and interaction.guild_id != int(guild_id):
        return False
    return interaction.permissions.manage_guild


async def check(interaction: discord.Interaction) -> bool:
    """許されていなければ、操作した人にだけ断りのメッセージを送って False を返す。"""
    if allowed(interaction):
        return True
    await interaction.response.send_message(DENIED, ephemeral=True)
    return False


class Tree(app_commands.CommandTree):
    """すべてのスラッシュコマンドで、操作できる人かを確かめる。"""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.type is discord.InteractionType.autocomplete:
            return allowed(interaction)  # 入力補完には返事のメッセージを送れないので、候補を出さないだけにする
        return await check(interaction)
