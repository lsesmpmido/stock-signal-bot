"""Discord の UI（ボタン・セレクトメニュー）。

承認・スキップ・監視解除ボタンは DynamicItem で custom_id に id や証券コードを埋め込み、
Bot の再起動後も過去のメッセージのボタンが押せるようにしている（main.py で add_dynamic_items する）。
"""

from __future__ import annotations

import logging
from typing import Any

import discord

import db
import ai_trader
import market
import orders
import portfolio
import review
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
REPORT_OPTIONS = [
    ("morning", "🌅 朝のブリーフィング (8:45)"),
    ("close", "🔔 大引けレポート (16:05)"),
    ("weekly", "📅 週間レポート (週の最後の取引日 16:10)"),
]


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
            placed = await orders.place_buy("you", self.code, name, await portfolio.default_amount())
        except portfolio.TradeError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return
        except Exception as exc:
            await interaction.followup.send(f"⚠️ 仮想購入でエラーが発生しました: `{exc}`", ephemeral=True)
            raise
        await interaction.followup.send(embed=placed_embed(placed, "buy", self.code, name), ephemeral=True)


# ---------------------------------------------------------------- 仮想売買の表示


async def company_name(client, code: str) -> str:
    if info := client.master.get(code):
        return info.name
    return await market.lookup_listed_name(code) or code


def _yen(v: float, sign: bool = False) -> str:
    return f"{v:+,.0f} 円" if sign else f"{v:,.0f} 円"


def placed_embed(placed: orders.Placed, side: str, code: str, name: str) -> discord.Embed:
    """すぐ約定したなら売買の結果、取引時間外なら注文の受付を表示する。"""
    if placed.result is not None:
        return buy_embed(placed.result) if side == "buy" else sell_embed(placed.result)
    action = "購入" if side == "buy" else "売却"
    return discord.Embed(
        title=f"📝 {name} ({code}) の仮想{action}注文を受け付けました",
        description=(
            "取引時間外のため、翌取引日の始値で約定します（終値を見てから、その終値で売買できないようにするため）。\n"
            f"注文番号 {placed.order_id} ・ 取り消しは `/orders`"
        ),
        color=discord.Color.light_grey(),
    )


def buy_embed(r: portfolio.BuyResult, price_note: str = "約 20 分遅れの最新株価") -> discord.Embed:
    embed = discord.Embed(
        title=f"💰 {r.company_name} ({r.ticker}) を仮想購入しました",
        description=f"{r.shares:,} 株 × {r.price:,.1f} 円（{price_note}）",
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
    who = "🤖 AI の" if s.owner == "ai" else ""
    embed = discord.Embed(title=f"📊 {who}仮想ポートフォリオ（新NISA＋特定口座）", color=color)
    lines = [f"総資産 **{_yen(s.total_value)}**（入金額の合計 {_yen(s.deposits)} から {s.total_return:+.2%}）"]
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
        empty = "まだ保有していません。" + ("" if s.owner == "ai" else "`/buy` や「💰 仮想で買う」ボタンで買えます。")
        embed.add_field(name="保有", value=empty, inline=False)
    embed.set_footer(
        text=f"今年の税金 {_yen(s.year_tax)} ・ 手数料 {_yen(s.year_fee)} ・ {s.started_at.astimezone(JST):%Y/%m/%d} 開始 ・ 株価は約 20 分遅れ"
    )
    return embed


def fills_embed(executed: list[orders.Executed], owner: str) -> discord.Embed:
    """取引時間外に出した注文の約定結果をまとめる。"""
    embed = discord.Embed(title="📝 注文が約定しました（今日の始値）", color=discord.Color.blue())
    lines = []
    for e in executed:
        o = e.order
        action = "買い" if o["side"] == "buy" else "売り"
        if e.result is None:
            lines.append(f"⚠️ {action}: {o['company_name']} ({o['ticker']}) — 約定できませんでした（{e.error}）")
        elif isinstance(e.result, portfolio.BuyResult):
            lines.append(f"🟢 買い: {o['company_name']} ({o['ticker']}) {e.result.shares:,} 株 × {e.result.price:,.1f} 円")
        else:
            r = e.result
            lines.append(
                f"🔴 売り: {o['company_name']} ({o['ticker']}) {r.shares:,} 株 × {r.price:,.1f} 円"
                f"（損益 {_yen(r.net, sign=True)} / {r.return_rate:+.1%}）"
            )
    embed.description = "\n".join(lines)
    return embed


def ai_fills_embed(executed: list[orders.Executed], mode: ai_trader.Mode, now) -> discord.Embed:
    """AI の今日の売買（寄り付きで約定したもの）をまとめる。"""
    embed = discord.Embed(title=f"🤖 AIの売買（{now:%m/%d} 寄り付き）{mode.label}", color=discord.Color.dark_teal())
    lines = []
    for e in executed:
        o = e.order
        conf = f"確信度 {o['confidence']:.0%}" if o["confidence"] is not None else None
        if e.result is None:
            lines.append(f"⚠️ {o['company_name']} ({o['ticker']}) の注文は約定しませんでした（{e.error}）")
        elif isinstance(e.result, portfolio.BuyResult):
            r = e.result
            accounts = "・".join(portfolio.ACCOUNT_LABELS[f.account] for f in r.fills)
            source = ai_trader.SOURCE_LABELS.get(o["source"] or "", o["source"] or "")
            detail = " ・ ".join(x for x in (conf, f"候補: {source}" if source else None) if x)
            lines.append(f"🟢 買い: {r.company_name} ({r.ticker}) {r.shares:,} 株 × {r.price:,.1f} 円（{accounts}）\n　{detail}")
        else:
            r = e.result
            tax = f" ・ 税金 {_yen(r.tax)}" if r.account == "tokutei" and r.tax else ""
            detail = " ・ ".join(x for x in (f"理由: {o['reason']}" if o["reason"] else None, conf) if x)
            lines.append(
                f"🔴 売り: {r.company_name} ({r.ticker}) {r.shares:,} 株 × {r.price:,.1f} 円"
                f"（{portfolio.ACCOUNT_LABELS[r.account]}）\n　損益 {_yen(r.net, sign=True)}（{r.return_rate:+.1%}）"
                f" ・ 保有 {r.held_days} 日{tax}\n　{detail}"
            )
    embed.description = "\n".join(lines)[:4000]
    return embed


class _CancelOrderSelect(discord.ui.Select):
    def __init__(self, open_orders: list[dict]) -> None:
        super().__init__(
            placeholder="取り消す注文を選択",
            options=[
                discord.SelectOption(label=order_label(o)[:100], value=str(o["id"])) for o in open_orders[:25]
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        ok = await orders.cancel(int(self.values[0]))
        remaining = await db.vp_orders("open", "you")
        text = "🗑️ 注文を取り消しました。" if ok else "その注文はすでに約定済みか、取り消し済みです。"
        await interaction.response.edit_message(content=text, embed=orders_embed(remaining), view=OrdersView(remaining))


class OrdersView(discord.ui.View):
    def __init__(self, open_orders: list[dict]) -> None:
        super().__init__(timeout=600)
        if open_orders:
            self.add_item(_CancelOrderSelect(open_orders))


def order_label(o: dict) -> str:
    if o["side"] == "buy":
        what = f"買い {o['amount']:,.0f} 円分"
    else:
        what = "売り " + (f"{o['shares']:,} 株" if o["shares"] else "全株")
    return f"#{o['id']} {o['ticker']} {o['company_name']} {what}"


def orders_embed(open_orders: list[dict]) -> discord.Embed:
    embed = discord.Embed(title=f"📝 未約定の注文 ({len(open_orders)})", color=discord.Color.light_grey())
    if not open_orders:
        embed.description = "未約定の注文はありません。"
        return embed
    embed.description = "\n".join(
        f"{order_label(o)}（{o['created_at'].astimezone(JST):%m/%d %H:%M} 受付）" for o in open_orders[:25]
    )
    embed.set_footer(text="翌取引日の始値で約定します")
    return embed


# ---------------------------------------------------------------- 答え合わせ


def review_embed(r: review.Review) -> discord.Embed:
    label = "1週間" if r.days == 5 else "1か月" if r.days == 20 else f"{r.days}営業日"
    embed = discord.Embed(title=f"📝 提案の答え合わせ（{r.days}営業日後 ≒ {label}）", color=discord.Color.purple())
    lines = [f"対象: {r.since:%m/%d} 以降の提案 {len(r.outcomes)} 件（評価待ち {r.waiting} 件）"]
    if not r.outcomes:
        embed.description = "\n".join(lines + ["", "評価できる提案はまだありません。提案から数営業日たつと表示されます。"])
        return embed

    def row(name: str, g: review.Group | None) -> str | None:
        if g is None:
            return None
        excess = f" / TOPIX比 {g.excess:+.1%}" if g.excess is not None else ""
        return f"{name}: {g.count} 件　平均 {g.change:+.1%}{excess}"

    for status, name in review.STATUS_LABELS.items():
        if text := row(name, r.group(lambda o, s=status: o.status == s and o.kind == "news")):
            lines.append(text)
    added = r.group(lambda o: o.status == "added" and o.kind == "news")
    skipped = r.group(lambda o: o.status == "skipped" and o.kind == "news")
    if added and skipped:
        diff = added.change - skipped.change
        lines.append(f"→ 承認した銘柄は、スキップした銘柄より {abs(diff):.1%} {'良かった' if diff >= 0 else '悪かった'}")
    if text := row("🔗 関連銘柄", r.group(lambda o: o.kind == "related")):
        lines.append(text)
    high = r.group(lambda o: o.impact >= review.HIGH_IMPACT)
    low = r.group(lambda o: o.impact < review.HIGH_IMPACT)
    if high and low:
        lines.append(f"🎯 Jev 高評価（インパクト {review.HIGH_IMPACT} 以上）平均 {high.change:+.1%} / それ以外 {low.change:+.1%}")
    embed.description = "\n".join(lines)

    picks = []
    if (o := r.extreme("added", highest=True)) and o.change > 0:
        picks.append(f"👍 ナイス承認: {o.company_name} ({o.ticker}) {o.change:+.1%}")
    if (o := r.extreme("skipped", highest=False)) and o.change < 0:
        picks.append(f"✨ ナイススキップ: {o.company_name} ({o.ticker}) {o.change:+.1%}")
    if (o := r.extreme("skipped", highest=True)) and o.change > 0:
        picks.append(f"😢 惜しいスキップ: {o.company_name} ({o.ticker}) {o.change:+.1%}")
    if picks:
        embed.add_field(name="注目の提案", value="\n".join(picks), inline=False)
    embed.set_footer(text="基準は提案時点で確定していた直近の終値。未回答は比較から外して参考表示")
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


class _ReportSelect(discord.ui.Select):
    """定番レポートの ON/OFF（複数選択。選んだものが ON）。"""

    def __init__(self, settings: dict[str, str]) -> None:
        super().__init__(
            placeholder="定番レポート（届けるものを選択）",
            min_values=0,
            max_values=len(REPORT_OPTIONS),
            options=[
                discord.SelectOption(label=label, value=kind, default=settings.get(f"report_{kind}", "on") == "on")
                for kind, label in REPORT_OPTIONS
            ],
            row=2,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        for kind, _ in REPORT_OPTIONS:
            await db.set_setting(f"report_{kind}", "on" if kind in self.values else "off")
        for option in self.options:
            option.default = option.value in self.values
        await interaction.response.edit_message(content=settings_text(await db.get_all_settings()), view=self.view)


class SettingsView(discord.ui.View):
    def __init__(self, settings: dict[str, str]) -> None:
        super().__init__(timeout=600)
        self.add_item(_SettingSelect("proposal_freq", "新銘柄提案", PROPOSAL_OPTIONS, settings["proposal_freq"], 0))
        self.add_item(_SettingSelect("signal_freq", "売買シグナル", SIGNAL_OPTIONS, settings["signal_freq"], 1))
        self.add_item(_ReportSelect(settings))


def settings_text(settings: dict[str, str]) -> str:
    proposal = dict(PROPOSAL_OPTIONS).get(settings["proposal_freq"], settings["proposal_freq"])
    signal = dict(SIGNAL_OPTIONS).get(settings["signal_freq"], settings["signal_freq"])
    on = [label for kind, label in REPORT_OPTIONS if settings.get(f"report_{kind}", "on") == "on"]
    return (
        f"⚙️ **通知設定**\n新銘柄提案: **{proposal}**\n売買シグナル: **{signal}**\n"
        f"定番レポート: **{'、'.join(on) if on else 'すべて OFF'}**"
    )


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
