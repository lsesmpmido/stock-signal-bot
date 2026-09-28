"""Discord の UI（ボタン・セレクトメニュー）。

承認・スキップ・監視解除ボタンは DynamicItem で custom_id に id や証券コードを埋め込み、
Bot の再起動後も過去のメッセージのボタンが押せるようにしている（main.py で add_dynamic_items する）。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import pandas as pd

import discord

import db
import ai_trader
import battle
import market
import news
import orders
import portfolio
import review
import watchlist
from jev_client import CATEGORIES
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
    ("cleanup", "🧹 週末の整理タイム (土曜 10:00)"),
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


def add_indicator_fields(embed: discord.Embed, ind: pd.DataFrame) -> None:
    """終値・RSI・MACD・移動平均の欄を追加する（シグナル通知と /chart で共通）。"""
    last, prev = ind.iloc[-1], ind.iloc[-2]
    change = (last["Close"] / prev["Close"] - 1) * 100
    embed.add_field(name="終値", value=f"{last['Close']:,.1f} 円 ({change:+.2f}%)")
    embed.add_field(name="RSI(14)", value=f"{last['RSI']:.1f}")
    embed.add_field(name="MACD / シグナル", value=f"{last['MACD']:.2f} / {last['MACD_signal']:.2f}")
    ma = " / ".join(f"{last[c]:,.0f}" if pd.notna(last[c]) else "—" for c in ("MA25", "MA75", "MA200"))
    embed.add_field(name="移動平均 25 / 75 / 200", value=ma, inline=False)
    embed.set_footer(text=f"日足ベース・データは約20分遅れ・{ind.index[-1]:%Y/%m/%d}")


def link_view(code: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for label, url in external_links(code):
        view.add_item(discord.ui.Button(label=label, url=url, row=0))
    return view


def decided_view(code: str) -> discord.ui.View:
    """提案を承認・スキップした後のボタン（リンクと「仮想で買う」は残す）。"""
    view = link_view(code)
    view.add_item(VirtualBuyButton(code))
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
    view.add_item(WhyButton(code))
    return view


def movers_view(moves: list) -> discord.ui.View | None:
    """大きく動いた銘柄の「なぜ動いた？」ボタン（大引けレポート用）。moves は (証券コード, 銘柄名, 騰落率)。"""
    if not moves:
        return None
    view = discord.ui.View(timeout=None)
    for code, name, change in moves[:5]:
        view.add_item(WhyButton(code, f"🔍 {name[:20]} {change:+.0%}"))
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
        if await watch_is_full() and code not in {s["ticker"] for s in await db.list_monitored()}:
            view = await ReplaceWatchView.create(code, name, "proposal", self.pending_id, interaction.message)
            await interaction.response.send_message(
                content=await replace_prompt(code, name), view=view, ephemeral=True
            )
            return
        added = await db.add_monitored(code, name, "proposal")
        await db.set_pending_status(self.pending_id, "added")
        note = f"✅ {interaction.user.display_name} が監視対象に追加しました"
        if not added:
            note = "ℹ️ すでに監視対象に登録されています"
        await interaction.response.edit_message(embed=_with_footer(interaction.message, note), view=decided_view(code))


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
        view = decided_view(pending["ticker"]) if pending else None
        await interaction.response.edit_message(embed=embed, view=view)
        await interaction.followup.send(
            "よければスキップした理由を選んでください（選ばなくてもスキップは完了しています）。",
            view=SkipReasonView(self.pending_id),
            ephemeral=True,
        )


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


class WhyButton(discord.ui.DynamicItem[discord.ui.Button], template=r"why:(?P<code>[0-9A-Z]+)"):
    """「なぜ動いた？」: その銘柄の直近 2 日のニュースを社名で検索して、押した人にだけ見せる。"""

    def __init__(self, code: str, label: str = "🔍 なぜ動いた？") -> None:
        super().__init__(
            discord.ui.Button(label=label[:80], style=discord.ButtonStyle.secondary, custom_id=f"why:{code}", row=2)
        )
        self.code = code

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match) -> Any:
        return cls(match["code"], item.label or "🔍 なぜ動いた？")

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        name = await company_name(interaction.client, self.code)
        try:
            items = await news.search_company(name)
        except Exception as exc:
            await interaction.followup.send(f"⚠️ ニュースを検索できませんでした: `{exc}`", ephemeral=True)
            raise
        df = (await market.get_daily([self.code], refresh=False)).get(self.code)
        move = ""
        if df is not None and len(df) >= 2:
            change = df["Close"].iloc[-1] / df["Close"].iloc[-2] - 1
            move = f"（{df.index[-1]:%m/%d} {change:+.1%}）"
        embed = discord.Embed(title=f"🔍 {name} ({self.code}) はなぜ動いた？{move}", color=discord.Color.blurple())
        if items:
            embed.description = "\n".join(
                f"・[{i.title}]({i.url})" + (f" — {i.source}" if i.source else "") for i in items
            )[:4000]
        else:
            embed.description = "直近 2 日に、この社名のニュースは見つかりませんでした（市場全体の動きかもしれません）。"
        embed.set_footer(text="Google News で社名を検索した直近 2 日の記事。値動きとの関係は記事を見て確かめてください")
        await interaction.followup.send(embed=embed, ephemeral=True)


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
    when, why = orders.queued_fill(market.now_jst())
    return discord.Embed(
        title=f"📝 {name} ({code}) の仮想{action}注文を受け付けました",
        description=(
            f"{why}、{when}で約定します（見えている株価を見てから、その株価で売買できないようにするため）。\n"
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
    embed = discord.Embed(title="📝 注文が約定しました（寄り付きの始値）", color=discord.Color.blue())
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
    embed.description = "\n".join(lines)[:4000]  # 埋め込みの説明は 4096 文字まで
    return embed


def ai_fills_embed(
    executed: list[orders.Executed], mode: ai_trader.Mode, now, title: str | None = None
) -> discord.Embed:
    """AI の今日の売買（寄り付きで約定したもの、または取引時間中の損切り）をまとめる。"""
    title = title or f"🤖 AIの売買（{now:%m/%d} 寄り付き）{mode.label}"
    embed = discord.Embed(title=title, color=discord.Color.dark_teal())
    lines = []
    for e in executed:
        o = e.order
        conf = f"確信度 {o['confidence']:.0%}" if o["confidence"] is not None else None
        if e.result is None and o.get("intraday"):
            lines.append(f"⚠️ {o['company_name']} ({o['ticker']}): {e.error}")
        elif e.result is None:
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


AI_LOG_LIMIT = 8  # 見送り・除外は、それぞれこの件数まで表示する


def _pct(v: float | None) -> str:
    return "—" if v is None else f"{v:.0%}"


def _field_lines(lines: list[str], rest: int) -> str:
    text = "\n".join(lines + ([f"ほか {rest} 件"] if rest > 0 else []))
    return text if len(text) <= 1024 else text[:1020] + "…"


def ai_decisions_embed(record: dict) -> discord.Embed:
    """前の取引日の大引け後に AI が判断した内容（売買・持ち続け・見送りと、それぞれの確信度・理由）。"""
    data, day = record["data"], record["decided_on"]
    entries = data["entries"]
    by_action: dict[str, list[dict]] = {}
    for e in entries:
        by_action.setdefault(e["action"], []).append(e)
    sells, buys = by_action.get("sell", []), by_action.get("buy", [])
    holds, passes = by_action.get("hold", []), by_action.get("pass", [])
    blocked, skipped = by_action.get("blocked", []), by_action.get("skipped", [])

    embed = discord.Embed(title=f"🧠 AIの判断（{day:%m/%d} 大引け後）{data['mode']}", color=discord.Color.dark_teal())
    headline = (
        f"売り {len(sells)} ・ 買い {len(buys)}" if sells or buys else "**売買はしませんでした**（すべて見送り・持ち続け）"
    )
    embed.description = (
        f"{headline}\n"
        f"持ち続け {len(holds)} ・ 見送り {len(passes)} ・ 安全ルールで除外 {len(blocked)} ・ Jev の判定 {data['judged']} 件\n"
        f"基準: 買いの確信度 {data['buy_threshold']:.0%} 以上 ・ 売りの確信度 {data['sell_threshold']:.0%} 以上"
    )
    if not entries:
        embed.description += "\n\n保有も候補もありませんでした（候補は提案銘柄・監視銘柄・あなたが買った銘柄）。"

    def score(e: dict) -> str:
        bonus = f"（弟子ボーナス +{e['bonus']:.0%} 込み）" if e.get("bonus") else ""
        return f"{_pct(None if e['confidence'] is None else e['confidence'] + (e.get('bonus') or 0))}{bonus}"

    if sells:
        lines = [
            f"**{e['name']}** ({e['ticker']}) — {e['note']}"
            + (f" ・ 売りの確信度 {_pct(e['confidence'])}" if e["confidence"] is not None else "")
            + f"\n　{e['facts']}"
            for e in sells
        ]
        embed.add_field(name="🔴 売り", value=_field_lines(lines, 0), inline=False)
    if buys:
        lines = [
            f"**{e['name']}** ({e['ticker']}) — 買いの確信度 {score(e)} ・ 候補: {'・'.join(e['sources'])}\n　{e['facts']}"
            for e in buys
        ]
        embed.add_field(name="🟢 買い", value=_field_lines(lines, 0), inline=False)
    if holds:
        lines = [
            f"{e['name']} ({e['ticker']}) — "
            + (e["note"] if e["note"] else f"売りの確信度 {_pct(e['confidence'])}")
            + f"\n　{e['facts']}"
            for e in holds[:AI_LOG_LIMIT]
        ]
        embed.add_field(name="⏸️ 持ち続け", value=_field_lines(lines, len(holds) - AI_LOG_LIMIT), inline=False)
    if passes:
        passes = sorted(passes, key=lambda e: e["confidence"] + (e.get("bonus") or 0), reverse=True)
        lines = [f"{e['name']} ({e['ticker']}) — 確信度 {score(e)} ・ {e['note']}" for e in passes[:AI_LOG_LIMIT]]
        embed.add_field(name="👀 見送り（確信度の高い順）", value=_field_lines(lines, len(passes) - AI_LOG_LIMIT), inline=False)
    if blocked:
        lines = [f"{e['name']} ({e['ticker']}) — {e['note']}" for e in blocked[:AI_LOG_LIMIT]]
        embed.add_field(
            name="🛡️ 安全ルールで除外（Jev には聞かない）",
            value=_field_lines(lines, len(blocked) - AI_LOG_LIMIT),
            inline=False,
        )
    if skipped:
        reasons: dict[str, int] = {}
        for e in skipped:
            reasons[e["note"] or "不明"] = reasons.get(e["note"] or "不明", 0) + 1
        embed.add_field(
            name="⚠️ 判定しなかった候補", value="、".join(f"{k} {v} 件" for k, v in reasons.items())[:1024], inline=False
        )
    embed.set_footer(
        text="確信度は Jev の判定（0〜100%）。Jev は理由の文章を返さないため、判断に使った材料（RSI など）とルールを表示しています"
    )
    # 1 つの埋め込みも合計 6000 文字まで。超えたら重要度の低い欄（後ろ）から外す
    while len(embed) > 6000 and embed.fields:
        embed.remove_field(len(embed.fields) - 1)
    return embed


# ---------------------------------------------------------------- 初期条件の設定（/reset）


class ResetModal(discord.ui.Modal, title="AI との勝負をやり直す"):
    cash = discord.ui.TextInput(label="現金（万円）", placeholder="例: 200", max_length=10)
    holdings = discord.ui.TextInput(
        label="すでに持っている銘柄（1 行に 1 銘柄）",
        style=discord.TextStyle.paragraph,
        placeholder="証券コード 株数 平均取得単価 [取得日] [NISA/特定]\n例: 7832 100 3852 2026/05/19 NISA",
        required=False,
        max_length=2000,
    )
    nisa_used = discord.ui.TextInput(
        label="今年すでに使った NISA 枠（万円・省略可）",
        placeholder="省略すると、今年 NISA で買った保有の合計",
        required=False,
        max_length=10,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            cash = _man_yen(self.cash.value, "現金")
            nisa = _man_yen(self.nisa_used.value, "今年使った NISA 枠") if self.nisa_used.value.strip() else None
            plan = await battle.plan_start(
                cash, self.holdings.value, nisa, lambda code: company_name(interaction.client, code), market.now_jst()
            )
        except battle.StartError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return
        except Exception as exc:  # 株価・銘柄名の取得の失敗など。「考え中」のまま止めない
            await interaction.followup.send(f"⚠️ 初期条件の確認でエラーが発生しました: `{exc}`", ephemeral=True)
            raise
        await interaction.followup.send(
            embed=reset_plan_embed(plan, market.now_jst()), view=ResetConfirmView(plan, interaction.user.id), ephemeral=True
        )


def _man_yen(text: str, label: str) -> float:
    try:
        value = float(text.replace(",", "").replace("万", "").replace("円", "").strip())
    except ValueError:
        raise battle.StartError(f"{label}は数字（万円）で書いてください。") from None
    if value < 0:
        raise battle.StartError(f"{label}は 0 以上にしてください。")
    return round(value * 10_000)


def reset_plan_embed(plan: battle.StartPlan, now: datetime, done: bool = False) -> discord.Embed:
    title = "🔄 AI との勝負をやり直しました" if done else "🔄 この初期条件で、AI との勝負をやり直しますか？"
    embed = discord.Embed(title=title, color=discord.Color.orange())
    lines = [
        "あなたと AI に同じ現金・保有を持たせて、ここから勝負します。",
        f"開始時の総資産 **{_yen(plan.total)}**（現金 {_yen(plan.cash)} ＋ 保有の時価）",
        f"今年使った NISA 枠 {_yen(plan.nisa_used)}（残り {_yen(portfolio.NISA_ANNUAL_LIMIT - plan.nisa_used)}）",
    ]
    if not done:
        lines.append(
            "\n⚠️ **あなたと AI の仮想口座・売買履歴・未約定の注文・勝負の記録（月ごとの勝敗）はすべて消えます。**"
        )
    embed.description = "\n".join(lines)
    if plan.holdings:
        rows = []
        for h in plan.holdings:
            gain = h.value - h.cost
            rows.append(
                f"{h.ticker} {h.company_name}（{portfolio.ACCOUNT_LABELS[h.account]}）{h.shares:,} 株 ・ "
                f"取得 {h.opened_on:%Y/%m/%d} {_yen(h.cost)} → 時価 {_yen(h.value)}（{_yen(gain, sign=True)} / {gain / h.cost if h.cost else 0:+.1%}）"
            )
        # 登録する保有は確認のため全部見せる（1 欄 1024 文字に収まるよう、複数の欄に分ける）
        chunks: list[list[str]] = [[]]
        for row in rows:
            if chunks[-1] and len("\n".join(chunks[-1] + [row])) > 1024:
                chunks.append([])
            chunks[-1].append(row[:1024])
        for i, chunk in enumerate(chunks):
            name = f"保有（両チーム共通・{len(rows)} 件）" if i == 0 else "保有（続き）"
            embed.add_field(name=name, value="\n".join(chunk), inline=False)
        if old := battle.long_held(plan, now.date()):
            names = "、".join(h.company_name for h in old)
            embed.add_field(
                name="ℹ️ AI の売買ルール",
                value=f"{names} は AI の最長保有（{ai_trader.MAX_HOLD_DAYS} 営業日）を過ぎているため、AI は次の判断で売ります。",
                inline=False,
            )
    embed.set_footer(
        text=f"時価は {plan.valued_on:%m/%d} の終値 ・ 成績（通算・月ごと）はこの総資産からの増減で測ります ・ 保有はすぐに AI の売買判断（取引時間中の損切り・今日の大引け後の判断）の対象になります"
    )
    return embed


class ResetConfirmView(discord.ui.View):
    def __init__(self, plan: battle.StartPlan, user_id: int) -> None:
        super().__init__(timeout=600)
        self.plan = plan
        self.user_id = user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

    @discord.ui.button(label="やり直す", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="⏳ 口座を作り直しています…", embed=None, view=None)
        now = market.now_jst()
        try:
            done = await interaction.client.run_reset(self.plan, now)
        except Exception as exc:
            self.stop()
            await interaction.edit_original_response(content=f"⚠️ やり直しに失敗しました: `{exc}`")
            raise
        if not done:
            # ボタンを戻して、処理が終わってから押し直せるようにする
            await interaction.edit_original_response(
                content="⏳ いま AI の判断か注文の約定の途中です。数分たってから、もう一度「やり直す」を押してください。",
                embed=reset_plan_embed(self.plan, now),
                view=self,
            )
            return
        self.stop()
        await interaction.edit_original_response(content="✅ やり直しました。", embed=None)
        await interaction.followup.send(embed=reset_plan_embed(self.plan, now, done=True))
        await db.log_notification("reset", detail=f"{self.plan.total:.0f}")

    @discord.ui.button(label="やめる", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(content="やり直しをやめました。", embed=None, view=None)


# ---------------------------------------------------------------- 便利コマンド（/chart /ranking /related）


def chart_embed(code: str, name: str, ind: pd.DataFrame) -> discord.Embed:
    embed = discord.Embed(title=f"📈 {name} ({code})", color=discord.Color.blurple())
    add_indicator_fields(embed, ind)
    return embed


def ranking_embed(title: str, moves: list, note: str) -> discord.Embed:
    """moves は reports.Move のリスト（騰落率の高い順）。"""
    embed = discord.Embed(title=title, color=discord.Color.blurple())
    if not moves:
        embed.description = "対象の銘柄がありません。`/watch add` や `/buy` で追加できます。"
        return embed
    medals = {0: "🥇", 1: "🥈", 2: "🥉"}
    lines = [
        f"{medals.get(i, f'{i + 1}.')} {m.name} ({m.ticker}) {m.close:,.1f} 円　**{m.change:+.2%}**"
        for i, m in enumerate(moves[:20])
    ]
    if len(moves) > 20:
        lines.append(f"ほか {len(moves) - 20} 銘柄")
    embed.description = "\n".join(lines)
    embed.set_footer(text=note)
    return embed


RELATION_GROUPS = {1: "主要取引先・親会社・子会社", 2: "資本関係・資本業務提携", 3: "業務提携・技術・共同研究", 4: "取引先・製品導入"}


def related_embed(code: str, name: str, related: list, names: dict[str, str], watched: set, held: set) -> discord.Embed:
    """related は relations.Related のリスト（影響の大きい順）。"""
    embed = discord.Embed(title=f"🔗 {name} ({code}) の関連企業", color=discord.Color.teal())
    if not related:
        embed.description = "関係データに、この銘柄と上場企業との関係は見つかりませんでした。"
        return embed
    shown = related[:15]
    for priority, group in RELATION_GROUPS.items():
        rows = [r for r in shown if r.priority == priority]
        if not rows:
            continue
        lines = []
        for r in rows:
            marks = ("👀" if r.code in watched else "") + ("💰" if r.code in held else "")
            lines.append(f"・{names.get(r.code, r.code)} ({r.code}){marks}: {name}の{r.label}")
        embed.add_field(name=group, value="\n".join(lines)[:1024], inline=False)
    rest = len(related) - len(shown)
    embed.description = f"{len(related)} 社（影響の大きい順に表示）" + (f"・ほか {rest} 社" if rest else "") + "\n👀 監視中　💰 仮想で保有中"
    embed.set_footer(text="関係データ: JP Market Vis（EDINET 等から自動抽出。誤りを含む場合があります）")
    return embed


TREE_FIRST = 8  # 連想ツリーで、2 段階目をたどる 1 段階目の会社の数
TREE_SECOND = 3  # 1 社から 2 段階目に出す会社の数


def related_tree_embed(
    code: str, name: str, first: list, second: dict[str, list], names: dict[str, str], watched: set, held: set
) -> discord.Embed:
    """関連企業と、その関連企業（2 段階）を、木の形で表示する。first・second は relations.Related のリスト。"""
    embed = discord.Embed(title=f"🌳 {name} ({code}) の連想ツリー（2 段階）", color=discord.Color.teal())
    if not first:
        embed.description = "関係データに、この銘柄と上場企業との関係は見つかりませんでした。"
        return embed

    def mark(c: str) -> str:
        return ("👀" if c in watched else "") + ("💰" if c in held else "")

    for r in first[:TREE_FIRST]:
        rows = [
            f"└ {names[x.code]} ({x.code}){mark(x.code)}: {names[r.code]}の{x.label}" for x in second.get(r.code, [])
        ]
        embed.add_field(
            name=f"{names[r.code]} ({r.code}){mark(r.code)}: {name}の{r.label}"[:256],
            value="\n".join(rows)[:1024] or "（この先の関係は見つかりませんでした）",
            inline=False,
        )
    rest = len(first) - TREE_FIRST
    embed.description = (
        f"1 段階目 {len(first)} 社のうち、業績への影響が大きい順に {min(len(first), TREE_FIRST)} 社をたどりました"
        + (f"（ほか {rest} 社）" if rest > 0 else "")
        + "\n👀 監視中　💰 仮想で保有中"
    )
    embed.set_footer(text="関係データ: JP Market Vis（EDINET 等から自動抽出。誤りを含む場合があります）")
    return embed


# ---------------------------------------------------------------- 追加入金・AI との勝負

DEPOSIT_CHOICES = [(2_400_000, "240万円"), (1_200_000, "120万円"), (0, "入金しない")]


class DepositButton(discord.ui.DynamicItem[discord.ui.Button], template=r"deposit:(?P<amount>[0-9]+)"):
    def __init__(self, amount: int) -> None:
        label = dict(DEPOSIT_CHOICES).get(amount, f"{amount:,} 円")
        super().__init__(
            discord.ui.Button(label=label, style=discord.ButtonStyle.primary, custom_id=f"deposit:{amount}")
        )
        self.amount = amount

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match) -> Any:
        return cls(int(match["amount"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await db.set_setting("vp_next_deposit", str(self.amount))
        await interaction.response.send_message(f"✅ 次回の追加入金を **{self.amount:,} 円** にしました。", ephemeral=True)


def deposit_view() -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for amount, _ in DEPOSIT_CHOICES:
        view.add_item(DepositButton(amount))
    return view


def battle_embed(st: battle.Standing, you: portfolio.Summary, ai: portfolio.Summary) -> discord.Embed:
    wins, losses, draws = st.record()
    embed = discord.Embed(title="🏆 AI vs あなた", color=discord.Color.orange())
    lines = [f"通算成績（月ごと）: あなた **{wins}勝 {losses}敗 {draws}分**", f"AI の今の性格: {st.mode.label}"]
    if (m := st.current) is not None:
        lead = {"you": "あなたがリード", "ai": "AI がリード", "draw": "互角"}[m.winner]
        lines.append(f"\n**今月（{m.month}）の途中経過**: 🧑 {m.you:+.2%} / 🤖 {m.ai:+.2%} → {lead}")
    else:
        lines.append("\n今月の記録はまだありません（大引け後に毎日記録します）。")
    embed.description = "\n".join(lines)
    for label, s in (("🧑 あなた", you), ("🤖 AI", ai)):
        embed.add_field(
            name=label,
            value=f"総資産 {_yen(s.total_value)}（通算 {s.total_return:+.2%}）\n保有 {len(s.holdings)} 銘柄 ・ 現金 {_yen(s.cash)}",
        )
    if st.finished:
        history = [
            f"{m.month}: 🧑 {m.you:+.2%} / 🤖 {m.ai:+.2%} → "
            + {"you": "あなたの勝ち", "ai": "AI の勝ち", "draw": "引き分け"}[m.winner]
            for m in st.finished[-6:]
        ]
        embed.add_field(name="これまでの月", value="\n".join(history), inline=False)
    embed.set_footer(text="総資産の増減率（税金・手数料込み、入金分を除く）で比べる。差が 0.05% 以内は引き分け")
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
    embed.set_footer(text="次の寄り付き（翌取引日の始値。昼休みに出した注文は今日の後場の始値）で約定します")
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
    by_category = [
        text
        for key, (name, _) in CATEGORIES.items()
        if (text := row(name, r.group(lambda o, k=key: o.category == k)))
    ]
    if by_category:
        embed.add_field(name="材料の種類ごと（承認・スキップ・未回答すべて）", value="\n".join(by_category), inline=False)
    embed.set_footer(text="基準は提案時点で確定していた直近の終値。未回答は比較から外して参考表示")
    return embed


# ---------------------------------------------------------------- 監視枠・スキップの理由・整理タイム・アラート


async def watch_limit() -> int:
    return int((await db.get_all_settings())["watch_limit"])


async def watch_is_full() -> bool:
    return await db.count_monitored() >= await watch_limit()


async def replace_prompt(code: str, name: str) -> str:
    return (
        f"監視枠（{await watch_limit()} 銘柄）がいっぱいです。{name} ({code}) を追加するなら、"
        "入れ替える銘柄を選んでください。"
    )


class _ReplaceSelect(discord.ui.Select):
    def __init__(self, stocks: list[dict]) -> None:
        super().__init__(
            placeholder="外して入れ替える銘柄を選択",
            options=[
                discord.SelectOption(label=f"{s['ticker']} {s['company_name']}"[:100], value=s["ticker"])
                for s in sort_watch(stocks)[:25]
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: ReplaceWatchView = self.view
        # メニューを開いている間に、ほかの操作で監視が変わっていることがある。
        # 先に追加し、追加できたときだけ外す（すでに監視中なら、選んだ銘柄を外さない）
        if not await db.add_monitored(view.code, view.name, view.source):
            await interaction.response.edit_message(
                content=f"ℹ️ {view.name} ({view.code}) はすでに監視中です。入れ替えはしていません。", view=None
            )
            return
        removed = await db.remove_monitored(self.values[0])
        if view.pending_id is not None:
            await db.set_pending_status(view.pending_id, "added")
        # 選んだ銘柄がすでに外れていたら、空いた枠に追加しただけになる
        swapped = f"（{removed['company_name']} と入れ替え）" if removed else ""
        if view.proposal_message is not None:
            note = f"✅ {interaction.user.display_name} が監視対象に追加しました{swapped}"
            await view.proposal_message.edit(
                embed=_with_footer(view.proposal_message, note), view=decided_view(view.code)
            )
        if removed:
            text = f"🔁 {removed['company_name']} ({removed['ticker']}) を外して、{view.name} ({view.code}) を追加しました。"
        else:
            text = f"👀 {view.name} ({view.code}) を追加しました（選んだ銘柄は、すでに監視から外れていました）。"
        await interaction.response.edit_message(content=text, view=None)


class ReplaceWatchView(discord.ui.View):
    """監視枠が満杯のときに、入れ替える銘柄を選ぶ（入れ替えないことも選べる）。"""

    def __init__(self, code: str, name: str, source: str, pending_id: int | None = None, proposal_message=None) -> None:
        super().__init__(timeout=600)
        self.code, self.name, self.source = code, name, source
        self.pending_id, self.proposal_message = pending_id, proposal_message

    @classmethod
    async def create(cls, *args, **kwargs) -> ReplaceWatchView:
        view = cls(*args, **kwargs)
        view.add_item(_ReplaceSelect(await db.list_monitored()))
        return view

    @discord.ui.button(label="入れ替えない", style=discord.ButtonStyle.secondary, row=1)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content=f"{self.name} ({self.code}) の追加をやめました。", view=None)


SKIP_REASONS = [
    ("expensive", "💸 高すぎる"),
    ("sector", "🙅 業種が苦手"),
    ("weak", "📰 材料が弱い"),
    ("too_late", "🚀 もう上がりすぎ"),
    ("other", "🤷 その他"),
]


class _SkipReasonSelect(discord.ui.Select):
    def __init__(self, pending_id: int) -> None:
        super().__init__(
            placeholder="スキップした理由",
            options=[discord.SelectOption(label=label, value=key) for key, label in SKIP_REASONS],
        )
        self.pending_id = pending_id

    async def callback(self, interaction: discord.Interaction) -> None:
        await db.set_skip_reason(self.pending_id, self.values[0])
        label = dict(SKIP_REASONS)[self.values[0]]
        await interaction.response.edit_message(content=f"📝 スキップの理由（{label}）を記録しました。", view=None)


class SkipReasonView(discord.ui.View):
    def __init__(self, pending_id: int) -> None:
        super().__init__(timeout=600)
        self.add_item(_SkipReasonSelect(pending_id))


class PruneButton(discord.ui.DynamicItem[discord.ui.Button], template=r"prune:(?P<action>keep|remove):(?P<code>[0-9A-Z]+)"):
    """週末の整理タイムの「続ける」「外す」ボタン。"""

    def __init__(self, action: str, code: str, name: str = "") -> None:
        label = f"{'続ける' if action == 'keep' else '外す'}: {name or code}"[:80]
        style = discord.ButtonStyle.secondary if action == "keep" else discord.ButtonStyle.danger
        super().__init__(discord.ui.Button(label=label, style=style, custom_id=f"prune:{action}:{code}"))
        self.action, self.code = action, code

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match) -> Any:
        return cls(match["action"], match["code"])

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.action == "keep":
            ok = await db.update_monitored(self.code, kept_at=datetime.now(timezone.utc))
            text = f"👍 {self.code} は監視を続けます（{watchlist.KEEP_DAYS} 日間は候補に出しません）。"
        else:
            ok = (await db.remove_monitored(self.code)) is not None
            text = f"🧹 {self.code} を監視対象から外しました。"
        if not ok:
            text = f"{self.code} はすでに監視対象ではありません。"
        await interaction.response.send_message(text, ephemeral=True)


def cleanup_message(candidates: list[watchlist.CleanupCandidate], total: int, limit: int):
    embed = discord.Embed(
        title="🧹 週末の整理タイム",
        description=(
            f"監視中 {total} / {limit} 銘柄。外すか続けるか、ボタンで選んでください。\n"
            "監視を絞ると、通知が見やすくなります。"
        ),
        color=discord.Color.dark_green(),
    )
    view = discord.ui.View(timeout=None)
    for i, c in enumerate(candidates):
        embed.add_field(name=f"{c.label}: {c.name} ({c.ticker})", value=c.detail, inline=False)
        keep, remove = PruneButton("keep", c.ticker, c.name), PruneButton("remove", c.ticker, c.name)
        keep.item.row = remove.item.row = i
        view.add_item(keep)
        view.add_item(remove)
    return embed, view


def alerts_embed(alerts: list[dict]) -> discord.Embed:
    embed = discord.Embed(title=f"⏰ 価格アラート ({len(alerts)})", color=discord.Color.orange())
    if not alerts:
        embed.description = "設定中のアラートはありません。`/alert add` で設定できます。"
        return embed
    embed.description = "\n".join(
        f"#{a['id']} {a['company_name']} ({a['ticker']}) が {a['target']:,.1f} 円を"
        f"{'超えたら' if a['direction'] == 'above' else '割ったら'}"
        for a in alerts
    )
    embed.set_footer(text="取引時間中に 15 分おきにチェック ・ 株価は約 20 分遅れ")
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


def sort_watch(stocks: list[dict]) -> list[dict]:
    """お気に入り（⭐）を先頭に、あとは追加した順。"""
    return sorted(stocks, key=lambda s: (not s.get("starred"), s["added_at"]))


def watch_list_embed(stocks: list[dict], limit: int | None = None) -> discord.Embed:
    count = f"{len(stocks)} / {limit}" if limit else str(len(stocks))
    embed = discord.Embed(title=f"👀 監視中の銘柄 ({count})", color=discord.Color.blurple())
    if not stocks:
        embed.description = "監視中の銘柄はありません。`/watch add` で追加できます。"
        return embed
    for s in stocks[:25]:
        added = s["added_at"].astimezone(JST).strftime("%Y/%m/%d")
        tag = f"［{watchlist.TAG_LABELS[s['tag']]}］" if s.get("tag") in watchlist.TAG_LABELS else ""
        value = f"{SOURCE_LABELS.get(s['source'], s['source'])}・{added}追加\n最後のシグナル: {s['last_signal'] or 'なし'}"
        if s.get("memo"):
            value += f"\n📝 {s['memo']}"
        embed.add_field(
            name=f"{'⭐ ' if s.get('starred') else ''}{s['ticker']} {s['company_name']}{tag}", value=value[:1024], inline=True
        )
    if len(stocks) > 25:
        embed.set_footer(text=f"ほか {len(stocks) - 25} 件（表示は25件まで）")
    return embed
