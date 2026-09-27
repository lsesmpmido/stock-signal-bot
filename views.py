"""Discord の UI（ボタン・セレクトメニュー）。

承認・スキップ・監視解除ボタンは DynamicItem で custom_id に id や証券コードを埋め込み、
Bot の再起動後も過去のメッセージのボタンが押せるようにしている（main.py で add_dynamic_items する）。
"""

from __future__ import annotations

import logging
from typing import Any

import discord

import db
import market
import portfolio
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
    view.add_item(VirtualBuyButton(code))
    return view


def signal_view(code: str) -> discord.ui.View:
    view = link_view(code)
    view.add_item(UnwatchButton(code))
    view.add_item(VirtualBuyButton(code))
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


class VirtualBuyButton(discord.ui.DynamicItem[discord.ui.Button], template=r"vp:buy:(?P<code>[0-9A-Z]+)"):
    def __init__(self, code: str) -> None:
        super().__init__(
            discord.ui.Button(
                label="💰 仮想で買う",
                style=discord.ButtonStyle.primary,
                custom_id=f"vp:buy:{code}",
                row=1,
            )
        )
        self.code = code

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match) -> Any:
        return cls(match["code"])

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            name = await company_name(interaction.client, self.code)
            result = await portfolio.buy(self.code, name, await portfolio.default_amount())
        except portfolio.TradeError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return
        except Exception as exc:
            await interaction.followup.send(f"⚠️ 仮想購入でエラーが発生しました: `{exc}`", ephemeral=True)
            raise
        await interaction.followup.send(embed=buy_embed(result), ephemeral=True)


# ---------------------------------------------------------------- 仮想売買の表示


async def company_name(client, code: str) -> str:
    if info := client.master.get(code):
        return info.name
    return await market.lookup_listed_name(code) or code


def _yen(v: float, sign: bool = False) -> str:
    return f"{v:+,.0f} 円" if sign else f"{v:,.0f} 円"


def buy_embed(r: portfolio.BuyResult) -> discord.Embed:
    shares = sum(f.shares for f in r.fills)
    embed = discord.Embed(
        title=f"💰 {r.company_name} ({r.ticker}) を仮想購入しました",
        description=f"{shares:,} 株 × {r.price:,.1f} 円（約 20 分遅れの最新株価）",
        color=discord.Color.blue(),
    )
    for f in r.fills:
        fee = f"（手数料 {_yen(f.fee)}）" if f.fee else ""
        embed.add_field(name=portfolio.ACCOUNT_LABELS[f.account], value=f"{f.shares:,} 株 / {_yen(f.amount)}{fee}")
    embed.set_footer(text=f"現金残高 {_yen(r.cash_after)} ・ 今年の NISA 枠の残り {_yen(r.nisa_left)}")
    return embed


def sell_embed(r: portfolio.SellResult) -> discord.Embed:
    color = discord.Color.green() if r.net >= 0 else discord.Color.red()
    embed = discord.Embed(
        title=f"🎓 {r.company_name} ({r.ticker}) を仮想売却しました",
        description=f"{portfolio.ACCOUNT_LABELS[r.account]} ・ {r.shares:,} 株 × {r.price:,.1f} 円",
        color=color,
    )
    embed.add_field(name="保有期間", value=f"{r.held_days} 日")
    embed.add_field(name="損益", value=f"{_yen(r.net, sign=True)}（{r.return_rate:+.1%}）")
    if r.topix_change is not None:
        diff = r.return_rate - r.topix_change
        verdict = "勝ち" if diff >= 0 else "負け"
        embed.add_field(name="同じ期間の TOPIX", value=f"{r.topix_change:+.1%} → 市場平均に {diff:+.1%} の{verdict}")
    details = [f"売却代金 {_yen(r.amount)}", f"取得費 {_yen(r.cost)}"]
    if r.fee:
        details.append(f"手数料 {_yen(r.fee)}")
    if r.account == "tokutei":
        details.append(f"税金 {_yen(r.tax)}" if r.tax >= 0 else f"税金の還付 {_yen(-r.tax)}")
    else:
        details.append("NISA のため非課税")
    embed.add_field(name="内訳", value=" / ".join(details), inline=False)
    embed.add_field(name="称号", value=portfolio.title_for(r), inline=False)
    embed.set_footer(text=f"現金残高 {_yen(r.cash_after)}")
    return embed


def portfolio_embed(s: portfolio.Summary) -> discord.Embed:
    color = discord.Color.green() if s.total_return >= 0 else discord.Color.red()
    embed = discord.Embed(title="📊 仮想ポートフォリオ（新NISA＋特定口座）", color=color)
    lines = [f"総資産 **{_yen(s.total_value)}**（元手 {_yen(portfolio.INITIAL_CASH)} から {s.total_return:+.2%}）"]
    if s.topix_change is not None:
        diff = s.total_return - s.topix_change
        lines.append(f"同じ期間の TOPIX {s.topix_change:+.2%} → 市場平均に {diff:+.2%} の{'勝ち' if diff >= 0 else '負け'}")
    lines.append(f"現金 {_yen(s.cash)} ・ 今年の NISA 枠の残り {_yen(s.nisa_left)}")
    embed.description = "\n".join(lines)
    for account, label in portfolio.ACCOUNT_LABELS.items():
        rows = [h for h in s.holdings if h.account == account]
        if not rows:
            continue
        text = []
        for h in rows[:15]:
            if h.value is None:
                text.append(f"{h.ticker} {h.company_name} {h.shares:,} 株（株価を取得できません）")
            else:
                rate = h.unrealized / h.cost if h.cost else 0
                text.append(f"{h.ticker} {h.company_name} {h.shares:,} 株 {_yen(h.value)}（{_yen(h.unrealized, sign=True)} / {rate:+.1%}）")
        if len(rows) > 15:
            text.append(f"ほか {len(rows) - 15} 銘柄")
        embed.add_field(name=label, value="\n".join(text)[:1024], inline=False)
    if not s.holdings:
        embed.add_field(name="保有", value="まだ保有していません。`/buy` や「💰 仮想で買う」ボタンで買えます。", inline=False)
    embed.set_footer(
        text=f"今年の税金 {_yen(s.year_tax)} ・ 手数料 {_yen(s.year_fee)} ・ {s.started_at.astimezone(JST):%Y/%m/%d} 開始 ・ 株価は約 20 分遅れ"
    )
    return embed


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
