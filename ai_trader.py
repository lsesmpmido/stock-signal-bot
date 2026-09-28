"""AI トレーダー。自分と同じルールの別口座で、毎取引日の大引け後に売買を判断して注文を出す。

- 候補: 直近 10 営業日の提案銘柄、自分の監視銘柄、自分が仮想で買った銘柄（保有中と直近 20 営業日の購入）
- 高値づかみを防ぐ安全ルールで候補を絞り、Jev に「買うべきか」を判断させる
- 保有銘柄は、安全ルール（損切り・最長保有）と Jev の「売るべきか」の判断で売る
- 現金が足りなければ、保有で最も見劣りする銘柄より明らかに良い候補だけ入れ替える
- 注文はすべて翌取引日の始値で約定する（orders.fill_open_orders）
- 例外として、損切りだけは取引時間中にも調べ、含み損が基準に達したらその場の株価ですぐ売る（intraday_stop_loss）
- 性格（モード）によって、1 回の金額・判断の基準・安全ルールの緩さが変わる
- 銘柄ごとの判断（売買・見送りと確信度・理由）を DB に残し、翌取引日の朝にまとめて知らせる
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import pandas as pd

import db
import market
import orders
import portfolio
import review
import signals
from jev_client import JevJudge
from market import JST

log = logging.getLogger(__name__)

OWNER = "ai"
PROPOSAL_WINDOW = 10  # 提案から何営業日まで候補にするか
YOUR_BUY_WINDOW = 20  # 自分が買ってから何営業日まで候補にするか
STOP_LOSS = -0.15
MAX_HOLD_DAYS = 120  # 営業日
REPLACE_MARGIN = 0.15  # 入れ替えは、候補の確信度が保有の「持ち続ける確信度」をこれ以上上回るときだけ
MAX_BUYS_PER_DAY = 3
MAX_CANDIDATES = 30  # Jev に問い合わせる候補の上限（1 日の呼び出し回数を抑える）
SOURCE_LABELS = {"proposal": "提案銘柄", "watch": "あなたの監視銘柄", "your_holding": "あなたが買った銘柄"}


@dataclass(frozen=True)
class Mode:
    key: str
    label: str
    amount: float  # 1 回の購入額
    buy_threshold: float  # Jev の「買う」の確信度がこれ以上なら買う
    sell_threshold: float  # Jev の「売る」の確信度がこれ以上なら売る
    max_rise: float  # 提案からこれ以上上がっていたら買わない（高値づかみ防止）
    max_rsi: float  # RSI がこれ以上なら買わない
    max_ma_gap: float  # 25 日線からこれ以上上に離れていたら買わない
    take_profit: float | None = None  # これ以上の含み益で利益確定する（堅実モード）
    follow_you: bool = False  # 自分の買い方のクセに近い候補を優先する（弟子モード）


MODES = {
    "steady": Mode("steady", "🛡️ 堅実モード", 150_000, 0.75, 0.55, 0.10, 70, 0.15, take_profit=0.10),
    "normal": Mode("normal", "😐 通常モード", 200_000, 0.65, 0.65, 0.10, 70, 0.15),
    "apprentice": Mode("apprentice", "🥋 弟子モード", 200_000, 0.65, 0.65, 0.10, 70, 0.15, follow_you=True),
    "gambler": Mode("gambler", "🎲 勝負師モード", 400_000, 0.55, 0.75, 0.25, 80, 0.30),
}


@dataclass
class Candidate:
    ticker: str
    name: str
    sources: list[str]
    features: dict
    news: dict | None = None  # 提案銘柄なら、元のニュースと Jev の評価
    confidence: float | None = None  # Jev の「買う」の確信度（問い合わせていなければ None）
    bonus: float = 0.0  # 弟子モードの、自分の買い方との近さ
    note: str | None = None  # 買わなかった理由（翌朝の「AI の判断」に出す）
    blocked: list[str] = field(default_factory=list)  # 当たった安全ルール

    @property
    def score(self) -> float:
        return (self.confidence or 0.0) + self.bonus


@dataclass
class HoldingView:
    ticker: str
    name: str
    positions: list[dict]
    value: float
    cost: float
    held_days: int
    features: dict
    keep: float = 1.0  # 持ち続ける確信度（1 - 売る確信度）
    sell_confidence: float | None = None  # Jev の「売る」の確信度（問い合わせていなければ None）
    note: str | None = None  # 売らなかった理由の補足（Jev の判断に失敗したなど）

    @property
    def return_rate(self) -> float:
        return self.value / self.cost - 1 if self.cost else 0.0


@dataclass
class Decisions:
    mode: Mode
    sells: list[tuple[HoldingView, str, float | None]] = field(default_factory=list)  # (保有, 理由, 確信度)
    buys: list[Candidate] = field(default_factory=list)
    skipped_by_rules: int = 0
    judged: int = 0
    log: list[dict] = field(default_factory=list)  # 銘柄ごとの判断（DB に保存して翌朝に知らせる）


def _trading_days_since(start: datetime, today) -> int:
    days, d = 0, start.astimezone(JST).date()
    while d < today:
        d += timedelta(days=1)
        if market.is_trading_day(d):
            days += 1
    return days


def features(df: pd.DataFrame) -> dict:
    """判断に使う値動きの状態。"""
    ind = signals.compute(df)
    last = ind.iloc[-1]

    def num(v, digits=4):
        return None if pd.isna(v) else round(float(v), digits)

    close = float(last["Close"])
    return {
        "close": close,
        "rsi": num(last["RSI"], 1),
        "ma25_gap": num(close / last["MA25"] - 1) if pd.notna(last["MA25"]) else None,
        "ma75_gap": num(close / last["MA75"] - 1) if pd.notna(last["MA75"]) else None,
        "macd_above_signal": None if pd.isna(last["MACD_hist"]) else bool(last["MACD_hist"] > 0),
        "change_5d": num(close / ind["Close"].iloc[-6] - 1) if len(ind) > 5 else None,
        "change_20d": num(close / ind["Close"].iloc[-21] - 1) if len(ind) > 20 else None,
    }


def _block_reasons(c: Candidate, mode: Mode) -> list[str]:
    """高値づかみを防ぐ安全ルールのうち、当たったもの。空なら買ってよい。"""
    f = c.features
    rise = f.get("change_since_proposal")
    reasons = []
    if rise is not None and rise >= mode.max_rise:
        reasons.append(f"提案から {rise:+.1%}（{mode.max_rise:.0%} 以上）")
    if f["rsi"] is not None and f["rsi"] >= mode.max_rsi:
        reasons.append(f"RSI {f['rsi']:.0f}（{mode.max_rsi:.0f} 以上）")
    if f["ma25_gap"] is not None and f["ma25_gap"] >= mode.max_ma_gap:
        reasons.append(f"25 日線から {f['ma25_gap']:+.1%}（{mode.max_ma_gap:.0%} 以上）")
    return reasons


def _facts(f: dict) -> str:
    """判断に使った値動きの状態を 1 行にまとめる（Jev は理由の文章を返さないので、材料を見せる）。"""
    parts = []
    if f.get("rsi") is not None:
        parts.append(f"RSI {f['rsi']:.0f}")
    if f.get("ma25_gap") is not None:
        parts.append(f"25日線 {f['ma25_gap']:+.1%}")
    if f.get("macd_above_signal") is not None:
        parts.append("MACD↑" if f["macd_above_signal"] else "MACD↓")
    if f.get("change_5d") is not None:
        parts.append(f"5日 {f['change_5d']:+.1%}")
    if f.get("change_since_proposal") is not None:
        parts.append(f"提案から {f['change_since_proposal']:+.1%}")
    return " ・ ".join(parts)


async def _collect_candidates(now: datetime) -> dict[str, Candidate]:
    today = now.date()
    held = {p["ticker"] for p in await db.vp_positions(OWNER)}
    ordered = {o["ticker"] for o in await db.vp_orders("open", OWNER)}
    found: dict[str, Candidate] = {}

    def add(ticker, name, source, news=None):
        if ticker in held or ticker in ordered:
            return
        c = found.setdefault(ticker, Candidate(ticker, name, [], {}))
        if source not in c.sources:
            c.sources.append(source)
        if news and c.news is None:
            c.news = news

    for p in reversed(await db.list_pending_since(now - timedelta(days=PROPOSAL_WINDOW * 2))):
        if _trading_days_since(p["created_at"], today) <= PROPOSAL_WINDOW:
            add(p["ticker"], p["company_name"], "proposal", p)
    for s in await db.list_monitored():
        add(s["ticker"], s["company_name"], "watch")
    for p in await db.vp_positions("you"):
        add(p["ticker"], p["company_name"], "your_holding")
    for t in await db.vp_trades("you", now - timedelta(days=YOUR_BUY_WINDOW * 2)):
        if t["side"] == "buy" and _trading_days_since(t["traded_at"], today) <= YOUR_BUY_WINDOW:
            add(t["ticker"], t["company_name"], "your_holding")
    return found


async def _your_style(daily: dict[str, pd.DataFrame]) -> dict | None:
    """自分が買ったときの RSI と 25 日線からの乖離の平均（弟子モードで使う）。"""
    rsis, gaps = [], []
    for t in await db.vp_trades("you"):
        df = daily.get(t["ticker"])
        if t["side"] != "buy" or df is None:
            continue
        upto = df[df.index.date <= t["traded_at"].astimezone(JST).date()]
        if len(upto) < 80:
            continue
        f = features(upto)
        if f["rsi"] is not None and f["ma25_gap"] is not None:
            rsis.append(f["rsi"])
            gaps.append(f["ma25_gap"])
    if not rsis:
        return None
    return {"rsi": sum(rsis) / len(rsis), "ma25_gap": sum(gaps) / len(gaps), "samples": len(rsis)}


def _similarity(f: dict, style: dict) -> float:
    """自分の買い方との近さ（0〜1）。"""
    if f["rsi"] is None or f["ma25_gap"] is None:
        return 0.0
    distance = abs(f["rsi"] - style["rsi"]) / 20 + abs(f["ma25_gap"] - style["ma25_gap"]) / 0.10
    return max(0.0, 1 - distance / 2)


def _news_state(news: dict | None) -> dict | None:
    if not news:
        return None
    return {
        "headline": news["news_title"],
        "positive_probability": round(news["score"] or 0, 2),
        "impact_0_to_2": round(news["impact"] or 0, 2),
        "kind": "関連銘柄として提案" if news.get("kind") == "related" else "ニュースの当事者",
    }


async def decide(jev: JevJudge, mode: Mode, now: datetime) -> Decisions:
    """今日の大引け後の判断をして、翌取引日の始値で約定する注文を出す。"""
    decisions = Decisions(mode)
    today = now.date()
    # 前日の売り注文がまだ約定していない銘柄は、見直しの対象から外す（売り注文の重複を防ぐ）
    pending_sells = {o["ticker"] for o in await db.vp_orders("open", OWNER) if o["side"] == "sell"}
    positions = [p for p in await db.vp_positions(OWNER) if p["ticker"] not in pending_sells]
    candidates = await _collect_candidates(now)
    tickers = sorted({p["ticker"] for p in positions} | set(candidates))
    your_buys = [t["ticker"] for t in await db.vp_trades("you") if t["side"] == "buy"]
    daily = await market.get_daily(sorted(set(tickers) | set(your_buys)), refresh=False) if tickers else {}

    # ---- 保有の見直し
    holdings: dict[str, HoldingView] = {}
    for p in positions:
        df = daily.get(p["ticker"])
        if df is None or len(df) < 80:
            continue
        h = holdings.get(p["ticker"])
        if h is None:
            h = holdings[p["ticker"]] = HoldingView(
                p["ticker"], p["company_name"], [], 0.0, 0.0, _trading_days_since(p["opened_at"], today), features(df)
            )
        h.positions.append(p)
        h.value += float(df["Close"].iloc[-1]) * p["shares"]
        h.cost += p["cost"]
        h.held_days = max(h.held_days, _trading_days_since(p["opened_at"], today))

    selling: set[str] = set()
    for h in holdings.values():
        reason, confidence = None, None
        if h.return_rate <= STOP_LOSS:
            reason = f"損切り（{h.return_rate:+.1%}）"
        elif h.held_days >= MAX_HOLD_DAYS:
            reason = f"最長保有（{h.held_days} 営業日）"
        elif mode.take_profit is not None and h.return_rate >= mode.take_profit:
            reason = f"利益確定（{h.return_rate:+.1%}）"
        else:
            holding = {"return_rate": round(h.return_rate, 4), "held_trading_days": h.held_days}
            state = {"company": h.name, "holding": holding, **h.features}
            try:
                confidence = await jev.should_sell(state)
            except Exception:
                log.warning("Jev の売り判断に失敗: %s", h.ticker, exc_info=True)
                h.note = "Jev の判断に失敗したため持ち続ける"
                continue
            decisions.judged += 1
            h.sell_confidence = confidence
            h.keep = 1 - confidence
            if confidence >= mode.sell_threshold:
                reason = "Jev の判断"
        if reason:
            decisions.sells.append((h, reason, confidence))
            selling.add(h.ticker)

    # ---- 候補の評価
    for c in candidates.values():
        df = daily.get(c.ticker)
        if df is None or len(df) < 80:
            c.note = "株価データが足りない"
            continue
        c.features = features(df)
        if c.news:
            # 提案時点で確定していた終値（答え合わせと同じ基準）からの値動き
            before = df[df.index.date <= review.base_cutoff(c.news["created_at"])]
            if not before.empty:
                rise = df["Close"].iloc[-1] / before["Close"].iloc[-1] - 1
                c.features["change_since_proposal"] = round(float(rise), 4)
    usable = [c for c in candidates.values() if c.features]
    for c in usable:
        c.blocked = _block_reasons(c, mode)
    decisions.skipped_by_rules = sum(bool(c.blocked) for c in usable)
    # 提案が新しいもの・自分が気にしているものを優先して、問い合わせる数を抑える
    usable = [c for c in usable if not c.blocked]
    for c in usable[MAX_CANDIDATES:]:
        c.note = f"問い合わせの上限（{MAX_CANDIDATES} 件）を超えた"
    usable = usable[:MAX_CANDIDATES]

    style = await _your_style(daily) if mode.follow_you else None
    for c in usable:
        state = {
            "company": c.name,
            "sources": [SOURCE_LABELS[s] for s in c.sources],
            "news": _news_state(c.news),
            **c.features,
        }
        try:
            c.confidence = await jev.should_buy(state)
        except Exception:
            log.warning("Jev の買い判断に失敗: %s", c.ticker, exc_info=True)
            c.note = "Jev の判断に失敗した"
            continue
        decisions.judged += 1
        if style:
            c.bonus = round(0.1 * _similarity(c.features, style), 3)
        if c.score < mode.buy_threshold:
            c.note = f"買いの基準（{mode.buy_threshold:.0%}）に届かない"

    # ---- 注文（売り → 買い。現金が足りなければ入れ替え）
    cash = float((await db.get_all_settings())[db.CASH_KEYS[OWNER]])
    for h, reason, confidence in decisions.sells:
        for p in h.positions:
            await orders.place_sell(OWNER, h.ticker, None, p["account"], reason, confidence)
        cash += h.value

    eligible = sorted((c for c in usable if c.score >= mode.buy_threshold), key=lambda c: c.score, reverse=True)
    for i, c in enumerate(eligible):
        stop = None
        if len(decisions.buys) >= MAX_BUYS_PER_DAY:
            stop = f"1 日に買う上限（{MAX_BUYS_PER_DAY} 件）に達した"
        elif cash < mode.amount * 0.5:
            keepers = [h for h in holdings.values() if h.ticker not in selling]
            if not keepers:
                stop = "現金が足りない"
            else:
                weakest = min(keepers, key=lambda h: h.keep)
                if c.score - weakest.keep < REPLACE_MARGIN:
                    stop = (
                        f"現金が足りず、入れ替えの条件（{weakest.name} の持ち続ける確信度 {weakest.keep:.0%} を"
                        f" {REPLACE_MARGIN * 100:.0f} ポイント以上上回る）に届かない"
                    )
        if stop:
            for rest in eligible[i:]:
                rest.note = stop
            break
        if cash < mode.amount * 0.5:
            reason = f"入れ替え（{c.name} を買うため）"
            for p in weakest.positions:
                await orders.place_sell(OWNER, weakest.ticker, None, p["account"], reason, 1 - weakest.keep)
            decisions.sells.append((weakest, reason, 1 - weakest.keep))
            selling.add(weakest.ticker)
            cash += weakest.value
        amount = min(mode.amount, cash)
        await orders.place_buy(OWNER, c.ticker, c.name, amount, f"Jev の判断（{mode.label}）", c.score, c.sources[0])
        decisions.buys.append(c)
        cash -= amount

    decisions.log = _decision_log(decisions, holdings, candidates)
    await db.ai_save_decisions(
        today,
        {
            "mode": mode.label,
            "buy_threshold": mode.buy_threshold,
            "sell_threshold": mode.sell_threshold,
            "judged": decisions.judged,
            "entries": decisions.log,
        },
    )
    log.info(
        "AI の判断: %s、売り %d 件、買い %d 件（Jev 判定 %d 件、安全ルールで除外 %d 件）",
        mode.label,
        len(decisions.sells),
        len(decisions.buys),
        decisions.judged,
        decisions.skipped_by_rules,
    )
    return decisions


def _decision_log(decisions: Decisions, holdings: dict[str, HoldingView], candidates: dict[str, Candidate]) -> list[dict]:
    """銘柄ごとの判断。action は sell / hold / buy / pass（見送り）/ blocked（安全ルール）/ skipped（判定せず）。"""
    entries = []
    sold = {h.ticker: (reason, confidence) for h, reason, confidence in decisions.sells}
    for h in holdings.values():
        position = f"含み損益 {h.return_rate:+.1%} ・ 保有 {h.held_days} 営業日"
        entry = {"ticker": h.ticker, "name": h.name, "facts": f"{position} ・ {_facts(h.features)}"}
        if h.ticker in sold:
            reason, confidence = sold[h.ticker]
            entries.append({**entry, "action": "sell", "confidence": confidence, "note": reason})
        else:
            entries.append({**entry, "action": "hold", "confidence": h.sell_confidence, "note": h.note})
    bought = {c.ticker for c in decisions.buys}
    for c in candidates.values():
        entry = {
            "ticker": c.ticker,
            "name": c.name,
            "sources": [SOURCE_LABELS[s] for s in c.sources],
            "confidence": c.confidence,
            "bonus": c.bonus,
            "facts": _facts(c.features),
        }
        if c.ticker in bought:
            entries.append({**entry, "action": "buy", "note": None})
        elif c.blocked:
            entries.append({**entry, "action": "blocked", "note": "、".join(c.blocked)})
        elif c.confidence is None:
            entries.append({**entry, "action": "skipped", "note": c.note})
        else:
            entries.append({**entry, "action": "pass", "note": c.note})
    return entries


async def intraday_stop_loss(now: datetime) -> list[orders.Executed]:
    """取引時間中の損切り。含み損が STOP_LOSS に達した保有を、その場の株価（約 20 分遅れ）ですぐ売る。

    自分の取引時間中の売買と同じ条件で約定させる。売った後に知らせるので、先回りされる心配はない。
    """
    if not portfolio.in_live_session(now):
        return []
    # 前日の判断で出した売り注文がまだ約定していない銘柄は、その注文に任せる
    pending_sells = {o["ticker"] for o in await db.vp_orders("open", OWNER) if o["side"] == "sell"}
    by_ticker: dict[str, list[dict]] = {}
    for p in await db.vp_positions(OWNER):
        if p["ticker"] not in pending_sells:
            by_ticker.setdefault(p["ticker"], []).append(p)
    if not by_ticker:
        return []
    daily = await market.get_daily(sorted(by_ticker))
    executed = []
    for ticker, positions in by_ticker.items():
        df = daily.get(ticker)
        if df is None or df.empty or df.index[-1].date() != now.date():
            continue  # 今日の株価がまだない
        price = float(df["Close"].iloc[-1])
        cost = sum(p["cost"] for p in positions)
        rate = price * sum(p["shares"] for p in positions) / cost - 1 if cost else 0.0
        if rate > STOP_LOSS:
            continue
        reason = f"損切り（取引時間中 {rate:+.1%}）"
        for p in positions:
            order = {
                "ticker": ticker,
                "company_name": p["company_name"],
                "side": "sell",
                "reason": reason,
                "confidence": None,
                "intraday": True,  # 注文を通さない売却（通知の文面を変える）
            }
            try:
                result = await portfolio.sell(OWNER, ticker, price, None, p["account"], reason)
            except portfolio.TradeError as exc:
                executed.append(orders.Executed(order, None, f"売れませんでした（{exc}）"))
                continue
            except Exception:
                # 1 件の失敗で、それまでに売れた分の通知まで止めない
                log.exception("AI の取引時間中の損切りでエラー: %s（%s）", ticker, p["account"])
                note = "エラーで、売れたかどうかを確かめられませんでした。`/portfolio` で AI の保有を確かめてください"
                executed.append(orders.Executed(order, None, note))
                continue
            executed.append(orders.Executed(order, result))
    if executed:
        log.info("AI が取引時間中に損切り: %s", sorted({e.order["ticker"] for e in executed}))
    return executed
