"""AI との勝負。毎日記録している両チームの総資産から、月ごとの勝敗と AI のモードを決める。

- 月ごとに、総資産の増減率（税金・手数料込み、その月の入金分を除く）を比べる。差が 0.05% 以内なら引き分け
- AI のモード: 直近の連敗が 3 か月以上なら勝負師、1〜2 か月なら弟子、通算で AI が勝ち越しなら堅実、それ以外は通常
"""

from __future__ import annotations

from dataclasses import dataclass

import ai_trader
import db
import market

DRAW_MARGIN = 0.0005


@dataclass(frozen=True)
class MonthResult:
    month: str  # "2026-10"
    you: float
    ai: float
    finished: bool

    @property
    def winner(self) -> str:
        diff = self.you - self.ai
        if abs(diff) <= DRAW_MARGIN:
            return "draw"
        return "you" if diff > 0 else "ai"


@dataclass
class Standing:
    months: list[MonthResult]
    mode: ai_trader.Mode

    @property
    def finished(self) -> list[MonthResult]:
        return [m for m in self.months if m.finished]

    @property
    def current(self) -> MonthResult | None:
        return next((m for m in self.months if not m.finished), None)

    def record(self) -> tuple[int, int, int]:
        """自分から見た通算成績（勝ち・負け・引き分け）。"""
        results = [m.winner for m in self.finished]
        return results.count("you"), results.count("ai"), results.count("draw")


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
    streak = 0
    for m in reversed(finished):
        if m.winner != "you":
            break
        streak += 1
    if streak >= 3:
        return ai_trader.MODES["gambler"]
    if streak >= 1:
        return ai_trader.MODES["apprentice"]
    results = [m.winner for m in finished]
    if results.count("ai") > results.count("you"):
        return ai_trader.MODES["steady"]
    return ai_trader.MODES["normal"]


async def standing() -> Standing:
    you = _monthly_returns(await db.vp_snapshots("you"))
    ai = _monthly_returns(await db.vp_snapshots("ai"))
    this_month = market.now_jst().strftime("%Y-%m")
    months = [MonthResult(m, you[m], ai[m], m < this_month) for m in sorted(set(you) & set(ai))]
    return Standing(months, _mode([m for m in months if m.finished]))


async def current_mode() -> ai_trader.Mode:
    return (await standing()).mode
