"""定番レポート（朝のブリーフィング・大引けレポート・週間レポート）の中身を作る。

どのレポートも 1 通のまとめメッセージにし、個別の通知を増やさない。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import discord
import pandas as pd

import battle
import db
import market
import portfolio
import review
import signals
from market import JST

INDEX_LABELS = {"^N225": "日経平均", "1306": "TOPIX（連動ETF）"}
MAX_LIST = 10
RSI_NEAR = 5  # RSI がしきい値（30 / 70）まであとこの幅以内なら「接近」
MA_NEAR = 0.01  # 25 日線と 75 日線の差がこの割合以内なら「接近」
MACD_NEAR = 0.1  # MACD とシグナル線の差が、直近 60 日の平均的な差のこの割合以内なら「接近」
KIND_LABELS = {"proposal": "提案", "signal": "売買シグナル", "delist": "自動解除", "report": "レポート", "fill": "約定", "ai_trade": "AIの売買", "deposit": "追加入金", "alert": "価格アラート", "cleanup": "整理タイム", "reset": "勝負のやり直し", "ai_decisions": "AIの判断"}


@dataclass(frozen=True)
class Move:
    ticker: str
    name: str
    close: float
    change: float


def _day_start(now: datetime) -> datetime:
    return datetime(now.year, now.month, now.day, tzinfo=JST)


def _week_start(now: datetime) -> datetime:
    return _day_start(now) - timedelta(days=now.weekday())


def _last_change(df: pd.DataFrame | None) -> float | None:
    """直近の足と、その 1 本前の終値との騰落率。"""
    if df is None or len(df) < 2:
        return None
    return float(df["Close"].iloc[-1] / df["Close"].iloc[-2] - 1)


def _change_since(df: pd.DataFrame | None, start: datetime) -> float | None:
    """start より前の最後の終値から、直近の終値までの騰落率（週間の騰落に使う）。"""
    if df is None or df.empty:
        return None
    before = df[df.index.date < start.date()]
    if before.empty:
        return None
    return float(df["Close"].iloc[-1] / before["Close"].iloc[-1] - 1)


def _pct(v: float | None) -> str:
    return "—" if v is None else f"{v:+.2%}"


def _arrow(v: float | None) -> str:
    if v is None:
        return "・"
    if v >= 0.03:
        return "🚀"
    if v > 0:
        return "📈"
    if v <= -0.03:
        return "💥"
    if v < 0:
        return "📉"
    return "・"


def _index_lines(daily: dict[str, pd.DataFrame], changer) -> str:
    lines = []
    for code, label in INDEX_LABELS.items():
        df = daily.get(code)
        value = changer(df)
        close = f"{df['Close'].iloc[-1]:,.0f}" if df is not None and not df.empty else "—"
        lines.append(f"{_arrow(value)} {label} {close}（{_pct(value)}）")
    return "\n".join(lines)


def _near_signals(ind: pd.DataFrame) -> list[str]:
    """シグナルが出そうな状態（しきい値やクロスの手前）を短い文にする。"""
    last = ind.iloc[-1]
    notes = []
    rsi = last["RSI"]
    if pd.notna(rsi):
        if signals.RSI_LOW < rsi <= signals.RSI_LOW + RSI_NEAR:
            notes.append(f"RSI {rsi:.0f}（売られすぎ目前）")
        elif signals.RSI_HIGH - RSI_NEAR <= rsi < signals.RSI_HIGH:
            notes.append(f"RSI {rsi:.0f}（買われすぎ目前）")
    if pd.notna(last["MA25"]) and pd.notna(last["MA75"]) and abs(last["MA25"] / last["MA75"] - 1) <= MA_NEAR:
        notes.append("25日線と75日線が接近")
    typical = ind["MACD_hist"].abs().tail(60).mean()
    if pd.notna(last["MACD_hist"]) and typical and abs(last["MACD_hist"]) <= typical * MACD_NEAR:
        notes.append("MACD がシグナル線に接近")
    return notes


def _moves(tickers: list[str], names: dict[str, str], daily: dict[str, pd.DataFrame], changer) -> list[Move]:
    moves = []
    for t in tickers:
        df = daily.get(t)
        change = changer(df)
        if change is not None:
            moves.append(Move(t, names.get(t, t), float(df["Close"].iloc[-1]), change))
    return sorted(moves, key=lambda m: m.change, reverse=True)


def _ranking(moves: list[Move], n: int = 3) -> str:
    if not moves:
        return "なし"
    top = [f"{_arrow(m.change)} {m.name} ({m.ticker}) {_pct(m.change)}" for m in moves[:n]]
    if len(moves) <= n:
        return "\n".join(top)
    bottom = [f"{_arrow(m.change)} {m.name} ({m.ticker}) {_pct(m.change)}" for m in moves[-n:][::-1] if m not in moves[:n]]
    return "\n".join(["**上位**", *top, "**下位**", *bottom])


async def _targets() -> tuple[list[str], list[str], dict[str, str]]:
    """監視銘柄、仮想保有銘柄、銘柄名の対応。"""
    monitored = await db.list_monitored()
    positions = await db.vp_positions("you")
    names = {s["ticker"]: s["company_name"] for s in monitored}
    names.update({p["ticker"]: p["company_name"] for p in positions})
    held = list(dict.fromkeys(p["ticker"] for p in positions))
    return [s["ticker"] for s in monitored], held, names


async def morning(now: datetime) -> discord.Embed:
    watch, _, names = await _targets()
    daily = await market.get_daily([*watch, *market.INDEX_CODES])
    embed = discord.Embed(title=f"🌅 朝のブリーフィング（{now:%m/%d}）", color=discord.Color.gold())
    embed.add_field(name="前日の市場", value=_index_lines(daily, _last_change), inline=False)

    moves = _moves(watch, names, daily, _last_change)
    if moves:
        lines = [f"{_arrow(m.change)} {m.name} ({m.ticker}) {m.close:,.1f} 円（{_pct(m.change)}）" for m in moves[:MAX_LIST]]
        if len(moves) > MAX_LIST:
            lines.append(f"ほか {len(moves) - MAX_LIST} 銘柄")
        embed.add_field(name="監視銘柄の前日終値", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="監視銘柄", value="監視中の銘柄はありません。", inline=False)

    near = []
    for t in watch:
        df = daily.get(t)
        if df is not None and len(df) >= 80:
            if notes := _near_signals(signals.compute(df)):
                near.append(f"・{names.get(t, t)} ({t}): {' / '.join(notes)}")
    embed.add_field(name="シグナルが近い銘柄", value="\n".join(near[:MAX_LIST]) or "なし", inline=False)

    proposals = await db.list_pending_since(_day_start(now))
    embed.set_footer(text=f"今朝の提案 {len(proposals)} 件 ・ 株価は前営業日の終値")
    return embed


async def close(now: datetime) -> discord.Embed:
    watch, held, names = await _targets()
    tickers = list(dict.fromkeys([*watch, *held]))
    daily = await market.get_daily([*tickers, *market.INDEX_CODES])
    embed = discord.Embed(title=f"🔔 大引けレポート（{now:%m/%d}）", color=discord.Color.blue())
    embed.add_field(name="今日の市場", value=_index_lines(daily, _last_change), inline=False)
    embed.add_field(
        name="監視・保有銘柄の騰落ランキング", value=_ranking(_moves(tickers, names, daily, _last_change)), inline=False
    )

    signals_today = await db.notifications_since(_day_start(now), "signal")
    embed.add_field(name="今日のシグナル", value=f"{len(signals_today)} 件", inline=True)

    s = await portfolio.summary("you")
    day_change = 0.0
    for h in s.holdings:
        df = daily.get(h.ticker)
        if df is not None and len(df) >= 2:
            day_change += (df["Close"].iloc[-1] - df["Close"].iloc[-2]) * h.shares
    embed.add_field(
        name="仮想ポートフォリオ",
        value=f"総資産 {s.total_value:,.0f} 円（前日比 {day_change:+,.0f} 円 / 通算 {s.total_return:+.2%}）",
        inline=True,
    )
    embed.set_footer(text="株価は今日の終値")
    return embed


async def weekly(now: datetime) -> discord.Embed:
    week_start = _week_start(now)
    watch, held, names = await _targets()
    tickers = list(dict.fromkeys([*watch, *held]))
    daily = await market.get_daily([*tickers, *market.INDEX_CODES])

    def weekly_change(df):
        return _change_since(df, week_start)

    embed = discord.Embed(title=f"📅 週間レポート（{week_start:%m/%d}〜{now:%m/%d}）", color=discord.Color.purple())
    embed.add_field(name="今週の市場", value=_index_lines(daily, weekly_change), inline=False)
    embed.add_field(
        name="監視・保有銘柄の週間騰落", value=_ranking(_moves(tickers, names, daily, weekly_change)), inline=False
    )

    week_signals = await db.notifications_since(week_start, "signal")
    lines = [f"・{names.get(n['ticker'], n['ticker'])} ({n['ticker']}): {n['detail']}" for n in week_signals[:MAX_LIST]]
    if len(week_signals) > MAX_LIST:
        lines.append(f"ほか {len(week_signals) - MAX_LIST} 件")
    embed.add_field(name=f"今週のシグナル（{len(week_signals)} 件）", value="\n".join(lines) or "なし", inline=False)

    r = await review.build(5)
    added = r.group(lambda o: o.status == "added" and o.kind == "news")
    skipped = r.group(lambda o: o.status == "skipped" and o.kind == "news")
    review_lines = [f"評価済み {len(r.outcomes)} 件（評価待ち {r.waiting} 件）"]
    if added:
        review_lines.append(f"✅ 承認 {added.count} 件 平均 {added.change:+.1%}")
    if skipped:
        review_lines.append(f"⏭️ スキップ {skipped.count} 件 平均 {skipped.change:+.1%}")
    embed.add_field(name="提案の答え合わせ（5営業日後）", value="\n".join(review_lines) + "\n詳しくは `/review`", inline=False)

    s = await portfolio.summary("you")
    pf = [f"総資産 {s.total_value:,.0f} 円（通算 {s.total_return:+.2%}）"]
    if s.topix_change is not None:
        diff = s.total_return - s.topix_change
        pf.append(f"TOPIX 比 {diff:+.2%}（{'勝ち' if diff >= 0 else '負け'}）")
    pf.append(f"今年の NISA 枠の残り {s.nisa_left:,.0f} 円")
    embed.add_field(name="仮想ポートフォリオ", value="\n".join(pf), inline=False)

    st = await battle.standing()
    wins, losses, draws = st.record()
    lines = [f"通算 あなた {wins}勝 {losses}敗 {draws}分 ・ AI の性格: {st.mode.label}"]
    if (m := st.current) is not None:
        lead = {"you": "あなたがリード", "ai": "AI がリード", "draw": "互角"}[m.winner]
        lines.append(f"今月の途中経過: 🧑 {m.you:+.2%} / 🤖 {m.ai:+.2%} → {lead}")
    ai_trades = await db.vp_trades("ai", week_start)
    for t in ai_trades[:MAX_LIST]:
        action = "買い" if t["side"] == "buy" else "売り"
        lines.append(f"・🤖 {action}: {t['company_name']} ({t['ticker']}) {t['shares']:,} 株 × {t['price']:,.1f} 円")
    if not ai_trades:
        lines.append("今週の AI の売買はありません")
    embed.add_field(name="🏆 AIと勝負", value="\n".join(lines)[:1024], inline=False)

    counts: dict[str, int] = {}
    for n in await db.notifications_since(week_start):
        counts[n["kind"]] = counts.get(n["kind"], 0) + 1
    total = sum(counts.values())
    breakdown = "、".join(f"{KIND_LABELS.get(k, k)} {v}" for k, v in counts.items())
    embed.set_footer(text=f"今週の通知 {total} 件" + (f"（{breakdown}）" if breakdown else ""))
    return embed


BUILDERS = {"morning": morning, "close": close, "weekly": weekly}
REPORT_LABELS = {"morning": "朝のブリーフィング", "close": "大引けレポート", "weekly": "週間レポート"}
