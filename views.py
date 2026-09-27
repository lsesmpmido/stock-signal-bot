"""Discord の UI（ボタン・セレクトメニュー）。

承認・スキップ・監視解除ボタンは DynamicItem で custom_id に id や証券コードを埋め込み、
Bot の再起動後も過去のメッセージのボタンが押せるようにしている（main.py で add_dynamic_items する）。
"""

from __future__ import annotations

import logging
from typing import Any

import discord

import db
from market import JST

log = logging.getLogger(__name__)

PROPOSAL_OPTIONS = [
    ("twice", "1日2回 (8:30 / 16:00)"),
    ("once", "1日1回 (8:30)"),
    ("off", "OFF"),
]
SIGNAL_OPTIONS = [
    ("15m", "15分間隔 (場中)"),
    ("1h", "1時間間隔 (場中)"),
    ("close", "1日1回 (大引け後 15:45)"),
    ("off", "OFF"),
]
SOURCE_LABELS = {"proposal": "提案から", "manual": "手動"}


# 上場企業同士の関係図。銘柄ページを直接開く URL はないので、トップページを開く
JP_MARKET_VIS_URL = "https://mattyamonaca.github.io/JP_Market_Vis/"


def external_links(code: str) -> list[tuple[str, str]]:
    return [
        ("JP Market Vis", JP_MARKET_VIS_URL),
        ("株探", f"https://kabutan.jp/stock/?code={code}"),
        ("Yahoo!ファイナンス", f"https://finance.yahoo.co.jp/quote/{code}.T"),
        ("四季報", f"https://shikiho.toyokeizai.net/stocks/{code}"),
    ]


def link_view(code: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for label, url in external_links(code):
        view.add_item(discord.ui.Button(label=label, url=url, row=0))
    return view


def proposal_view(pending_id: int, code: str) -> discord.ui.View:
    view = link_view(code)
    view.add_item(AddPendingButton(pending_id))
    view.add_item(SkipPendingButton(pending_id))
    return view


def signal_view(code: str) -> discord.ui.View:
    view = link_view(code)
    view.add_item(UnwatchButton(code))
    return view


def _with_footer(message: discord.Message, text: str) -> discord.Embed | None:
    if not message.embeds:
        return None
    embed = message.embeds[0]
    embed.set_footer(text=text)
    return embed


class AddPendingButton(discord.ui.DynamicItem[discord.ui.Button], template=r"pending:add:(?P<id>[0-9]+)"):
    def __init__(self, pending_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="➕ 監視対象に追加",
                style=discord.ButtonStyle.success,
                custom_id=f"pending:add:{pending_id}",
                row=1,
            )
        )
        self.pending_id = pending_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match) -> Any:
        return cls(int(match["id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        pending = await db.get_pending(self.pending_id)
        if pending is None:
            await interaction.response.send_message("この提案は見つかりませんでした。", ephemeral=True)
            return
        code, name = pending["ticker"], pending["company_name"]
        added = await db.add_monitored(code, name, "proposal")
        await db.set_pending_status(self.pending_id, "added")
        note = f"✅ {interaction.user.display_name} が監視対象に追加しました"
        if not added:
            note = "ℹ️ すでに監視対象に登録されています"
        await interaction.response.edit_message(embed=_with_footer(interaction.message, note), view=link_view(code))


class SkipPendingButton(discord.ui.DynamicItem[discord.ui.Button], template=r"pending:skip:(?P<id>[0-9]+)"):
    def __init__(self, pending_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="❌ スキップ",
                style=discord.ButtonStyle.secondary,
                custom_id=f"pending:skip:{pending_id}",
                row=1,
            )
        )
        self.pending_id = pending_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match) -> Any:
        return cls(int(match["id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        pending = await db.get_pending(self.pending_id)
        await db.set_pending_status(self.pending_id, "skipped")
        embed = _with_footer(interaction.message, "⏭️ スキップしました")
        view = link_view(pending["ticker"]) if pending else None
        await interaction.response.edit_message(embed=embed, view=view)


class UnwatchButton(discord.ui.DynamicItem[discord.ui.Button], template=r"watch:remove:(?P<code>[0-9A-Z]+)"):
    def __init__(self, code: str) -> None:
        super().__init__(
            discord.ui.Button(
                label="🗑️ 監視解除",
                style=discord.ButtonStyle.danger,
                custom_id=f"watch:remove:{code}",
                row=1,
            )
        )
        self.code = code

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match) -> Any:
        return cls(match["code"])

    async def callback(self, interaction: discord.Interaction) -> None:
        removed = await db.remove_monitored(self.code)
        await interaction.response.edit_message(view=link_view(self.code))
        if removed:
            msg = f"🗑️ {removed['company_name']} ({self.code}) を監視対象から外しました。"
        else:
            msg = f"{self.code} はすでに監視対象ではありません。"
        await interaction.followup.send(msg, ephemeral=True)


# ---------------------------------------------------------------- /settings


class _SettingSelect(discord.ui.Select):
    def __init__(self, key: str, placeholder: str, options: list[tuple[str, str]], current: str, row: int) -> None:
        self.key = key
        self.labels = dict(options)
        super().__init__(
            placeholder=placeholder,
            options=[discord.SelectOption(label=label, value=value, default=value == current) for value, label in options],
            row=row,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        value = self.values[0]
        await db.set_setting(self.key, value)
        for option in self.options:
            option.default = option.value == value
        await interaction.response.edit_message(
            content=f"✅ {self.placeholder} を「{self.labels[value]}」に変更しました。", view=self.view
        )


class SettingsView(discord.ui.View):
    def __init__(self, settings: dict[str, str]) -> None:
        super().__init__(timeout=600)
        self.add_item(_SettingSelect("proposal_freq", "新銘柄提案", PROPOSAL_OPTIONS, settings["proposal_freq"], 0))
        self.add_item(_SettingSelect("signal_freq", "売買シグナル", SIGNAL_OPTIONS, settings["signal_freq"], 1))


def settings_text(settings: dict[str, str]) -> str:
    proposal = dict(PROPOSAL_OPTIONS).get(settings["proposal_freq"], settings["proposal_freq"])
    signal = dict(SIGNAL_OPTIONS).get(settings["signal_freq"], settings["signal_freq"])
    return f"⚙️ **通知設定**\n新銘柄提案: **{proposal}**\n売買シグナル: **{signal}**"


# ---------------------------------------------------------------- /watch list


class _UnwatchSelect(discord.ui.Select):
    def __init__(self, stocks: list[dict]) -> None:
        super().__init__(
            placeholder="監視を解除する銘柄を選択",
            options=[
                discord.SelectOption(label=f"{s['ticker']} {s['company_name']}"[:100], value=s["ticker"])
                for s in stocks[:25]
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        code = self.values[0]
        removed = await db.remove_monitored(code)
        stocks = await db.list_monitored()
        text = f"🗑️ {removed['company_name']} ({code}) を監視対象から外しました。" if removed else f"{code} はすでに解除済みです。"
        await interaction.response.edit_message(content=text, embed=watch_list_embed(stocks), view=WatchListView(stocks))


class WatchListView(discord.ui.View):
    def __init__(self, stocks: list[dict]) -> None:
        super().__init__(timeout=600)
        if stocks:
            self.add_item(_UnwatchSelect(stocks))


def watch_list_embed(stocks: list[dict]) -> discord.Embed:
    embed = discord.Embed(title=f"👀 監視中の銘柄 ({len(stocks)})", color=discord.Color.blurple())
    if not stocks:
        embed.description = "監視中の銘柄はありません。`/watch add` で追加できます。"
        return embed
    for s in stocks[:25]:
        added = s["added_at"].astimezone(JST).strftime("%Y/%m/%d")
        embed.add_field(
            name=f"{s['ticker']} {s['company_name']}",
            value=f"{SOURCE_LABELS.get(s['source'], s['source'])}・{added}追加\n最後のシグナル: {s['last_signal'] or 'なし'}",
            inline=True,
        )
    if len(stocks) > 25:
        embed.set_footer(text=f"ほか {len(stocks) - 25} 件（表示は25件まで）")
    return embed
