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
import fiscal
import glossary
import market
import portfolio
import review
import signals
from jev_client import CATEGORIES
from market import JST

INDEX_LABELS = {"^N225": "日経平均", "1306": "TOPIX（連動ETF）"}
MAX_LIST = 10
RSI_NEAR = 5  # RSI がしきい値（30 / 70）まであとこの幅以内なら「接近」
MA_NEAR = 0.01  # 25 日線と 75 日線の差がこの割合以内なら「接近」
MACD_NEAR = 0.1  # MACD とシグナル線の差が、直近 60 日の平均的な差のこの割合以内なら「接近」
KIND_LABELS = {"proposal": "提案", "signal": "売買シグナル", "delist": "自動解除", "report": "レポート", "fill": "約定", "ai_trade": "AIの売買", "deposit": "追加入金", "alert": "価格アラート", "cleanup": "整理タイム", "reset": "勝負のやり直し", "ai_decisions": "AIの判断", "pick": "今日の1銘柄", "hot": "話題銘柄の急騰・急落", "quiz": "銘柄当てクイズ", "thread": "振り返りスレッド"}


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


def _today_change(df: pd.DataFrame | None) -> float | None:
    """今日の足の騰落率。今日の足がまだ保存されていなければ None（前日の騰落を今日の値として見せないため）。"""
    if df is None or df.empty or df.index[-1].date() != market.now_jst().date():
        return None
    return _last_change(df)


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
    daily = await market.get_daily([*watch, *market.INDEX_CODES], refresh=False)
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

    if years_ago := await _years_ago(watch, names, now):
        embed.add_field(name="📅 〇年前の今日と比べると", value=years_ago, inline=False)
    if rights := await _rights_lines(watch, names, now):
        embed.add_field(name="🗓️ 権利付き最終日が近い銘柄（配当・優待の権利確定日の目安）", value=rights, inline=False)
    term, meaning = glossary.term_of_day(now.date())
    embed.add_field(name=f"📖 今日の用語: {term}", value=meaning, inline=False)

    proposals = await db.list_pending_since(_day_start(now))
    embed.set_footer(text=f"今朝の提案 {len(proposals)} 件 ・ 株価は前営業日の終値")
    return embed


async def _rights_lines(watch: list[str], names: dict[str, str], now: datetime) -> str | None:
    """監視・保有銘柄のうち、権利付き最終日が 10 営業日以内の銘柄。"""
    _, held, _ = await _targets()
    rows = await fiscal.upcoming(list(dict.fromkeys([*watch, *held])), now.date())
    lines = [
        f"・{names.get(code, code)} ({code}): {label}（{record:%m/%d}）→ 権利付き最終日 **{cum:%m/%d}**"
        + ("（今日）" if left == 0 else f"（あと {left} 営業日）")
        for code, label, record, cum, left in rows[:MAX_LIST]
    ]
    if not lines:
        return None
    return "\n".join(lines) + "\n※ 決算日から計算した目安。配当・優待を出すか、中間配当があるかは会社の発表を確かめてください"


YEARS_AGO = (1, 5)


def _years_back(d, n: int):
    """n 年前の同じ日。うるう日（2/29）は 2/28 にする。"""
    try:
        return d.replace(year=d.year - n)
    except ValueError:
        return d.replace(year=d.year - n, day=28)


async def _years_ago(watch: list[str], names: dict[str, str], now: datetime) -> str | None:
    """監視銘柄の今の株価を、1 年前・5 年前の同じ日（休みならその前の取引日）の終値と比べる。"""
    if not watch:
        return None
    daily = await market.get_daily(watch, years=max(YEARS_AGO) + 1, refresh=False)
    lines = []
    for t in watch:
        df = daily.get(t)
        if df is None or df.empty:
            continue
        parts = []
        for n in YEARS_AGO:
            target = _years_back(now.date(), n)
            # 上場から n 年たっていない銘柄は比べない。5 年分のダウンロードは数日ずれて始まることがあるので 1 週間は許す
            if df.index[0].date() > target + timedelta(days=7):
                continue
            past = df[df.index.date <= target]
            base = float(past["Close"].iloc[-1] if not past.empty else df["Close"].iloc[0])
            parts.append(f"{n}年前 {base:,.0f} 円（{df['Close'].iloc[-1] / base - 1:+.0%}）")
        if parts:
            lines.append(f"・{names.get(t, t)} ({t}): " + " ・ ".join(parts))
    return "\n".join(lines[:MAX_LIST]) or None


async def close(now: datetime) -> discord.Embed:
    watch, held, names = await _targets()
    tickers = list(dict.fromkeys([*watch, *held]))
    daily = await market.get_daily([*tickers, *market.INDEX_CODES], refresh=False)
    embed = discord.Embed(title=f"🔔 大引けレポート（{now:%m/%d}）", color=discord.Color.blue())
    embed.add_field(name="今日の市場", value=_index_lines(daily, _today_change), inline=False)
    embed.add_field(
        name="監視・保有銘柄の騰落ランキング", value=_ranking(_moves(tickers, names, daily, _today_change)), inline=False
    )

    signals_today = await db.notifications_since(_day_start(now), "signal")
    embed.add_field(name="今日のシグナル", value=f"{len(signals_today)} 件", inline=True)

    s = await portfolio.summary("you", refresh=False)
    day_change = 0.0
    for h in s.holdings:
        df = daily.get(h.ticker)
        if df is not None and len(df) >= 2 and _today_change(df) is not None:
            day_change += (df["Close"].iloc[-1] - df["Close"].iloc[-2]) * h.shares
    embed.add_field(
        name="仮想ポートフォリオ",
        value=f"総資産 {s.total_value:,.0f} 円（前日比 {day_change:+,.0f} 円 / 通算 {s.total_return:+.2%}）",
        inline=True,
    )
    if seeds := await _related_seeds(now):
        embed.add_field(name="🔗 連想買いの芽（関連銘柄として提案した銘柄の、その後）", value=seeds, inline=False)
    embed.set_footer(text="株価は今日の終値")
    return embed


async def weekly_look_back(now: datetime) -> discord.Embed:
    """週末の振り返りスレッドの最初のメッセージ: 今週の自分と AI の売買、監視・保有銘柄の週間騰落。"""
    week_start = _week_start(now)
    embed = discord.Embed(
        title=f"📝 今週の振り返り（{week_start:%m/%d}〜{now - timedelta(days=1):%m/%d}）", color=discord.Color.blurple()
    )
    for owner in battle.TEAMS:
        label = f"{portfolio.OWNER_ICONS[owner]} {portfolio.OWNER_LABELS[owner]}の売買"
        trades = await db.vp_trades(owner, week_start)
        lines = [
            f"・{'買い' if t['side'] == 'buy' else '売り'}: {t['company_name']} ({t['ticker']}) {t['shares']:,} 株 × {t['price']:,.1f} 円"
            for t in trades[:MAX_LIST]
        ]
        embed.add_field(name=label, value="\n".join(lines) or "今週はありません", inline=False)
    watch, held, names = await _targets()
    tickers = list(dict.fromkeys([*watch, *held]))
    daily = await market.get_daily(tickers, refresh=False) if tickers else {}
    moves = _moves(tickers, names, daily, lambda df: _change_since(df, week_start))
    embed.add_field(name="監視・保有銘柄の今週の騰落", value=_ranking(moves), inline=False)
    embed.set_footer(text="このメッセージのスレッドに、今週のメモを書き込めます")
    return embed


BIG_MOVE = 0.03  # 大引けレポートで「なぜ動いた？」ボタンを付ける値動き


async def big_movers() -> list[tuple[str, str, float]]:
    """監視銘柄・保有銘柄のうち、今日 ±3% 以上動いた銘柄（動きの大きい順）。"""
    watch, held, names = await _targets()
    tickers = list(dict.fromkeys([*watch, *held]))
    daily = await market.get_daily(tickers, refresh=False) if tickers else {}
    moves = [m for m in _moves(tickers, names, daily, _today_change) if abs(m.change) >= BIG_MOVE]
    return [(m.ticker, m.name, m.change) for m in sorted(moves, key=lambda m: abs(m.change), reverse=True)]


SEED_DAYS = 5  # 関連銘柄の提案から、この営業日数まで値動きを追う


async def _related_seeds(now: datetime) -> str | None:
    """直近に関連銘柄として提案した銘柄の、提案してからの値動きを、ニュースの当事者の値動きと並べる。"""
    rows = await db.list_pending_since(now - timedelta(days=SEED_DAYS * 2 + 4))
    origins = {r["news_url"]: r for r in rows if r["kind"] == "news"}
    related = [
        r
        for r in rows
        if r["kind"] == "related" and 1 <= market.trading_days_between(r["created_at"].astimezone(JST).date(), now.date()) <= SEED_DAYS
    ]
    if not related:
        return None
    codes = {r["ticker"] for r in related} | {origins[r["news_url"]]["ticker"] for r in related if r["news_url"] in origins}
    daily = await market.get_daily(sorted(codes), refresh=False)  # 直近の提案銘柄は 16:00 に日足を保存済み
    seeds = []
    for r in related:
        change = review.change_since(daily.get(r["ticker"]), review.base_cutoff(r["created_at"]))
        if change is None:
            continue
        origin = origins.get(r["news_url"])
        tail = ""
        if origin:
            o_change = review.change_since(daily.get(origin["ticker"]), review.base_cutoff(origin["created_at"]))
            tail = f" ← {origin['company_name']}のニュース（{origin['created_at'].astimezone(JST):%m/%d}" + (
                f"・当事者 {o_change:+.1%}）" if o_change is not None else "）"
            )
        seeds.append((change, f"{_arrow(change)} {r['company_name']} ({r['ticker']}) {change:+.1%}{tail}"))
    seeds.sort(key=lambda s: s[0], reverse=True)
    return "\n".join(text for _, text in seeds[:5])[:1024] or None


async def weekly(now: datetime) -> discord.Embed:
    week_start = _week_start(now)
    watch, held, names = await _targets()
    tickers = list(dict.fromkeys([*watch, *held]))
    daily = await market.get_daily([*tickers, *market.INDEX_CODES], refresh=False)

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

    s = await portfolio.summary("you", refresh=False)
    pf = [f"総資産 {s.total_value:,.0f} 円（通算 {s.total_return:+.2%}）"]
    if s.topix_change is not None:
        diff = s.total_return - s.topix_change
        pf.append(f"TOPIX 比 {diff:+.2%}（{'勝ち' if diff >= 0 else '負け'}）")
    pf.append(f"今年の NISA 枠の残り {s.nisa_left:,.0f} 円")
    embed.add_field(name="仮想ポートフォリオ", value="\n".join(pf), inline=False)

    st = await battle.standing()
    lines = [
        "通算 あなた "
        + " ・ ".join(
            f"vs {portfolio.OWNER_LABELS[ai]} {w}勝 {l}敗 {d}分" for ai in portfolio.AI_OWNERS for w, l, d in [st.record(ai)]
        )
        + f" ・ 慎重AIの性格: {st.mode.label}"
    ]
    if (m := st.current) is not None:
        lines.append(f"今月の途中経過: {m.returns_text()} → {m.leader_text()}")
    for ai in portfolio.AI_OWNERS:
        ai_trades = await db.vp_trades(ai, week_start)
        icon = portfolio.OWNER_ICONS[ai]
        for t in ai_trades[:MAX_LIST]:
            action = "買い" if t["side"] == "buy" else "売り"
            lines.append(f"・{icon} {action}: {t['company_name']} ({t['ticker']}) {t['shares']:,} 株 × {t['price']:,.1f} 円")
        if len(ai_trades) > MAX_LIST:
            lines.append(f"・{icon} ほか {len(ai_trades) - MAX_LIST} 件")
        if not ai_trades:
            lines.append(f"今週の{portfolio.OWNER_LABELS[ai]}の売買はありません")
    embed.add_field(name="🏆 AIと勝負", value="\n".join(lines)[:1024], inline=False)

    counts: dict[str, int] = {}
    for n in await db.notifications_since(week_start):
        counts[n["kind"]] = counts.get(n["kind"], 0) + 1
    total = sum(counts.values())
    breakdown = "、".join(f"{KIND_LABELS.get(k, k)} {v}" for k, v in counts.items())
    embed.set_footer(text=f"今週の通知 {total} 件" + (f"（{breakdown}）" if breakdown else ""))
    return embed


async def surprising_link(master, relations, now: datetime) -> str | None:
    """業種の違う会社同士のつながりを 1 つ選ぶ。監視銘柄・保有銘柄の関係を優先し、なければ全体から選ぶ（日替わり）。"""
    def sector(code: str) -> str | None:
        info = master.get(code)
        return info.sector if info and info.sector and info.sector != "-" else None

    watch, held, _ = await _targets()
    mine = [
        (c, r.code)
        for c in dict.fromkeys([*watch, *held])
        if (info := master.get(c))
        for r in relations.neighbors(c, info.name)
        if sector(c) and sector(r.code) and sector(c) != sector(r.code)
    ]
    pairs = mine or [(a, b) for a, b in relations.pairs() if sector(a) and sector(b) and sector(a) != sector(b)]
    if not pairs:
        return None
    a, b = pairs[now.date().toordinal() % len(pairs)]
    a_info, b_info = master.get(a), master.get(b)
    link = next((r for r in relations.neighbors(a, a_info.name) if r.code == b), None)
    if link is None:
        return None
    return (
        f"{b_info.name} ({b})〔{b_info.sector}〕は、{a_info.name} ({a})〔{a_info.sector}〕の{link.label}"
        + ("\n（あなたの監視・保有銘柄のつながりから）" if mine else "")
    )


PICK_PROPOSAL_DAYS = 7


async def stock_of_day(master, relations, now: datetime):
    """今日の 1 銘柄: 監視も保有もしていない銘柄から 1 社を選ぶ（直近の提案でインパクトの高いものを優先し、
    なければ監視・保有銘柄の関連企業から日替わりで）。(証券コード, 銘柄名, 紹介の理由) か None。"""
    watch, held, _ = await _targets()
    mine = set(watch) | set(held)
    proposals = [
        p for p in await db.list_pending_since(now - timedelta(days=PICK_PROPOSAL_DAYS)) if p["ticker"] not in mine
    ]
    if proposals:
        p = max(proposals, key=lambda p: (p["impact"] or 0, p["score"] or 0))
        category = _category_label(p.get("category"))
        return p["ticker"], p["company_name"], f"{p['created_at'].astimezone(JST):%m/%d} の提案{category}: {p['news_title']}"
    related = []
    for c in dict.fromkeys([*watch, *held]):
        if info := master.get(c):
            related += [(r, info) for r in relations.neighbors(c, info.name) if r.code not in mine and master.get(r.code)]
    if not related:
        return None
    r, origin = related[now.date().toordinal() % len(related)]
    return r.code, master.get(r.code).name, f"あなたの監視・保有銘柄 {origin.name} の{r.label}"


def _category_label(category: str | None) -> str:
    return f"（{CATEGORIES[category][0]}）" if category in CATEGORIES else ""


BUILDERS = {"morning": morning, "close": close, "weekly": weekly}
REPORT_LABELS = {"morning": "朝のブリーフィング", "close": "大引けレポート", "weekly": "週間レポート"}
