"""AI との勝負。毎日記録している 3 チーム（あなた・慎重 AI・大胆 AI）の総資産から、月ごとの勝敗・順位と慎重 AI のモードを決める。

- 月ごとに、総資産の増減率（税金・手数料込み、その月の入金分を除く）を比べる。差が 0.05% 以内なら引き分け
- 勝敗は「あなた対慎重 AI」「あなた対大胆 AI」をそれぞれ数え、月ごとの順位も出す
- 慎重 AI のモード: あなたに対する直近の連敗が 3 か月以上なら勝負師、1〜2 か月なら弟子、通算で勝ち越しなら堅実、それ以外は通常
  （大胆 AI の性格は固定）
- 初期条件（現金・すでに持っている銘柄）を指定して、3 チーム同じ状態から勝負をやり直せる
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Awaitable, Callable

import ai_trader
import db
import market
import portfolio
import review
from market import JST
from ticker_master import CODE_PATTERN, normalize_code

DRAW_MARGIN = 0.0005
MAX_START_HOLDINGS = 20
ACCOUNT_WORDS = {"nisa": "nisa", "ニーサ": "nisa", "特定": "tokutei", "特定口座": "tokutei", "tokutei": "tokutei"}
DATE_PATTERN = re.compile(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})")


TEAMS = ("you", "ai", "ai_bold")


@dataclass(frozen=True)
class MonthResult:
    month: str  # "2026-10"
    returns: dict[str, float]  # チームごとの増減率（その月に記録のあるチームだけ）
    finished: bool

    def versus(self, ai: str) -> str | None:
        """あなたと ai の勝敗（"you" / ai / "draw"）。どちらかの記録がなければ None。"""
        if "you" not in self.returns or ai not in self.returns:
            return None
        diff = self.returns["you"] - self.returns[ai]
        if abs(diff) <= DRAW_MARGIN:
            return "draw"
        return "you" if diff > 0 else ai

    def ranking(self) -> list[str]:
        """増減率の高い順のチーム。"""
        return sorted(self.returns, key=lambda t: self.returns[t], reverse=True)

    def returns_text(self) -> str:
        """各チームの増減率（例: 🧑 +1.20% / 🤖 +0.50% / ⚡ +2.10%）。"""
        return " / ".join(f"{portfolio.OWNER_ICONS[t]} {self.returns[t]:+.2%}" for t in TEAMS if t in self.returns)

    def leader_text(self) -> str:
        """トップのチーム（差が引き分けの幅以内なら並んでいるとする）。終わった月は「1 位」、途中の月は「リード」。"""
        ranking = self.ranking()
        top = [t for t in ranking if self.returns[ranking[0]] - self.returns[t] <= DRAW_MARGIN]
        names = "・".join(portfolio.OWNER_LABELS[t] for t in top)
        if len(top) > 1:
            return f"{names}が並んで{' 1 位' if self.finished else 'トップ'}"
        return f"{names}が{' 1 位' if self.finished else 'リード'}"


@dataclass
class Standing:
    months: list[MonthResult]
    mode: ai_trader.Mode  # 慎重 AI のモード

    @property
    def finished(self) -> list[MonthResult]:
        return [m for m in self.months if m.finished]

    @property
    def current(self) -> MonthResult | None:
        return next((m for m in self.months if not m.finished), None)

    def record(self, ai: str = "ai") -> tuple[int, int, int]:
        """あなたから見た、ai との通算成績（勝ち・負け・引き分け）。"""
        results = [r for m in self.finished if (r := m.versus(ai)) is not None]
        return results.count("you"), results.count(ai), results.count("draw")


def _monthly_returns(snapshots: list[dict]) -> dict[str, float]:
    """月ごとの増減率。前月末の総資産（最初の月は入金額）を基準に、その月の入金分を除いて計算する。"""
    by_month: dict[str, list[dict]] = {}
    for s in snapshots:
        by_month.setdefault(s["date"].strftime("%Y-%m"), []).append(s)
    returns, prev = {}, None
    for month in sorted(by_month):
        end = by_month[month][-1]
        if prev is None:
            start_value, deposited = by_month[month][0]["deposits"], end["deposits"] - by_month[month][0]["deposits"]
        else:
            start_value, deposited = prev["total_value"], end["deposits"] - prev["deposits"]
        base = start_value + deposited
        returns[month] = (end["total_value"] - start_value - deposited) / base if base else 0.0
        prev = end
    return returns


def _mode(finished: list[MonthResult]) -> ai_trader.Mode:
    """慎重 AI のモード（あなたとの勝敗で決める）。"""
    results = [r for m in finished if (r := m.versus("ai")) is not None]
    streak = 0
    for r in reversed(results):
        if r != "you":
            break
        streak += 1
    if streak >= 3:
        return ai_trader.MODES["gambler"]
    if streak >= 1:
        return ai_trader.MODES["apprentice"]
    if results.count("ai") > results.count("you"):
        return ai_trader.MODES["steady"]
    return ai_trader.MODES["normal"]


async def standing() -> Standing:
    returns = {team: _monthly_returns(await db.vp_snapshots(team)) for team in TEAMS}
    this_month = market.now_jst().strftime("%Y-%m")
    months = [
        MonthResult(m, {team: r[m] for team, r in returns.items() if m in r}, m < this_month)
        for m in sorted(set().union(*returns.values()))
    ]
    return Standing(months, _mode([m for m in months if m.finished]))


async def current_mode() -> ai_trader.Mode:
    return (await standing()).mode


# ---------------------------------------------------------------- 初期条件の設定


class StartError(Exception):
    """利用者に見せるエラー（入力の書き方の誤り・株価が取れないなど）。"""


@dataclass(frozen=True)
class StartHolding:
    ticker: str
    company_name: str
    account: str
    shares: int
    cost: float  # 取得費の合計（平均取得単価 × 株数）
    opened_on: date
    close: float  # 直近の確定した終値

    @property
    def value(self) -> float:
        return self.close * self.shares


@dataclass(frozen=True)
class StartPlan:
    cash: float
    holdings: list[StartHolding]
    nisa_used: float  # 今年すでに使った NISA 枠
    year: int
    valued_on: date  # 保有の評価に使った終値の日付（最も新しいもの）

    @property
    def total(self) -> float:
        """開始時点の総資産。入金額の合計とし、勝負の成績はここからの増減で測る。"""
        return self.cash + sum(h.value for h in self.holdings)


def parse_holdings(text: str, today: date) -> list[tuple[str, int, float, date, str]]:
    """1 行に 1 銘柄「証券コード 株数 平均取得単価 [取得日] [NISA/特定]」を読む。取得日の既定は今日、口座の既定は特定口座。"""
    rows = []
    for no, line in enumerate(text.splitlines(), start=1):
        tokens = [t for t in re.split(r"[\s,、]+", line.strip()) if t]
        if not tokens:
            continue
        where = f"{no} 行目「{line.strip()}」"
        if len(tokens) < 3:
            raise StartError(f"{where}: 証券コード・株数・平均取得単価の 3 つが必要です。")
        code = normalize_code(tokens[0])
        if not CODE_PATTERN.fullmatch(code):
            raise StartError(f"{where}: `{tokens[0]}` は証券コードの形式ではありません。")
        try:
            shares = int(tokens[1].replace("株", ""))
            price = float(tokens[2].replace("円", ""))
        except ValueError:
            raise StartError(f"{where}: 株数と平均取得単価は数字で書いてください。") from None
        if shares <= 0 or price <= 0:
            raise StartError(f"{where}: 株数と平均取得単価は 0 より大きくしてください。")
        opened_on, account = today, "tokutei"
        for token in tokens[3:]:
            if m := DATE_PATTERN.fullmatch(token):
                try:
                    opened_on = date(int(m[1]), int(m[2]), int(m[3]))
                except ValueError:
                    raise StartError(f"{where}: `{token}` は存在しない日付です。") from None
            elif token.lower() in ACCOUNT_WORDS:
                account = ACCOUNT_WORDS[token.lower()]
            else:
                raise StartError(f"{where}: `{token}` が読み取れません（取得日は 2026/05/19、口座は NISA か 特定）。")
        if opened_on > today:
            raise StartError(f"{where}: 取得日が未来の日付です。")
        rows.append((code, shares, price, opened_on, account))
    return rows


async def plan_start(
    cash: float,
    holdings_text: str,
    nisa_used: float | None,
    name_of: Callable[[str], Awaitable[str]],
    now: datetime,
) -> StartPlan:
    """入力を確かめて、初期条件を組み立てる（まだ DB は変えない）。"""
    if cash < 0:
        raise StartError("現金は 0 以上にしてください。")
    rows = parse_holdings(holdings_text, now.date())
    # 今年 NISA で買った額は、行をまとめる前に行ごとに数える（まとめると取得日が最も古い日になるため）
    bought_this_year = sum(
        round(price * shares)
        for _, shares, price, opened_on, account in rows
        if account == "nisa" and opened_on.year == now.year
    )
    merged: dict[tuple[str, str], list] = {}  # 同じ銘柄・同じ口座の行は 1 つの保有にまとめる
    for code, shares, price, opened_on, account in rows:
        m = merged.setdefault((code, account), [0, 0.0, opened_on])
        m[0] += shares
        m[1] += round(price * shares)
        m[2] = min(m[2], opened_on)
    # 確認画面に全部を表示できる数に抑える（同じ銘柄でも NISA と特定口座は別に数える）
    if len(merged) > MAX_START_HOLDINGS:
        raise StartError(f"保有は {MAX_START_HOLDINGS} 件（銘柄 × 口座）までにしてください。")

    daily = await market.get_daily(sorted({code for code, _ in merged})) if merged else {}
    cutoff = review.base_cutoff(now)  # 取引時間中なら、まだ確定していない今日の足は使わない
    holdings, valued = [], []
    for (code, account), (shares, cost, opened_on) in merged.items():
        df = daily.get(code)
        confirmed = df[df.index.date <= cutoff] if df is not None else None
        if confirmed is None or confirmed.empty:
            raise StartError(f"`{code}` の株価を取得できませんでした。証券コードを確かめてください。")
        valued.append(confirmed.index[-1].date())
        holdings.append(
            StartHolding(code, await name_of(code), account, shares, cost, opened_on, float(confirmed["Close"].iloc[-1]))
        )

    if bought_this_year > portfolio.NISA_ANNUAL_LIMIT:
        raise StartError(f"今年 NISA で買った保有の合計（{bought_this_year:,.0f} 円）が、年間の枠 240 万円を超えています。")
    if nisa_used is None:
        nisa_used = bought_this_year
    elif not bought_this_year <= nisa_used <= portfolio.NISA_ANNUAL_LIMIT:
        raise StartError(
            f"今年使った NISA 枠は、今年 NISA で買った保有の合計（{bought_this_year:,.0f} 円）以上、240 万円以下にしてください。"
        )
    return StartPlan(cash, holdings, nisa_used, now.year, max(valued, default=cutoff))


def long_held(plan: StartPlan, today: date) -> list[StartHolding]:
    """AI の最長保有（営業日）をすでに過ぎていて、翌取引日に AI が売る保有。"""
    return [
        h
        for h in plan.holdings
        if market.trading_days_between(h.opened_on, today) >= ai_trader.MAX_HOLD_DAYS
    ]


async def apply_start(plan: StartPlan, now: datetime) -> None:
    """3 チームを同じ初期条件にして、勝負をやり直す。これまでの売買履歴・注文・勝負の記録は消える。"""
    positions = [
        {
            "account": h.account,
            "ticker": h.ticker,
            "company_name": h.company_name,
            "shares": h.shares,
            "cost": h.cost,
            "opened_at": datetime.combine(h.opened_on, portfolio.MARKET_OPEN, JST),
        }
        for h in plan.holdings
    ]
    preset = f"{plan.year}:{plan.nisa_used:.0f}" if plan.nisa_used else ""
    async with portfolio.trade_lock():
        await db.vp_reset(plan.cash, plan.total, positions, now, preset)
