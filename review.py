"""提案の答え合わせ。

提案した時点で確定していた直近の終値を基準に、N 営業日後の騰落率を計算し、
承認・スキップ・未回答や、ニュースの銘柄・関連銘柄ごとに成績を比べる。
株価は日足の DB キャッシュから取るので、過去の提案もさかのぼって評価できる。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

import pandas as pd

import db
import market
from market import JST

# 大引け（15:30）以降の提案は、その日の終値を基準にする。それより前なら前営業日の終値
CLOSE_TIME = time(15, 30)
WINDOW_DAYS = 60  # 直近この日数の提案を対象にする
HIGH_IMPACT = 1.5  # Jev のインパクトがこれ以上を「高評価」とする
STATUS_LABELS = {"added": "✅ 承認", "skipped": "⏭️ スキップ", "pending": "❔ 未回答"}


@dataclass(frozen=True)
class Outcome:
    ticker: str
    company_name: str
    status: str  # added / skipped / pending
    kind: str  # news / related
    impact: float
    category: str | None  # 材料の種類（記録を始める前の提案は None）
    proposed_at: datetime
    change: float  # 騰落率
    topix: float | None  # 同じ期間の TOPIX 連動 ETF の騰落率

    @property
    def excess(self) -> float | None:
        return None if self.topix is None else self.change - self.topix


@dataclass(frozen=True)
class Group:
    count: int
    change: float
    excess: float | None


@dataclass
class Review:
    days: int
    since: date
    outcomes: list[Outcome]
    waiting: int  # 期間がまだたっていない提案の件数

    def group(self, pred) -> Group | None:
        rows = [o for o in self.outcomes if pred(o)]
        if not rows:
            return None
        excess = [o.excess for o in rows if o.excess is not None]
        return Group(
            count=len(rows),
            change=sum(o.change for o in rows) / len(rows),
            excess=sum(excess) / len(excess) if excess else None,
        )

    def extreme(self, status: str, highest: bool) -> Outcome | None:
        """status の提案のうち、騰落率が最も高い（highest=False なら最も低い）もの。"""
        rows = [o for o in self.outcomes if o.status == status]
        if not rows:
            return None
        return max(rows, key=lambda o: o.change) if highest else min(rows, key=lambda o: o.change)


def base_cutoff(proposed_at: datetime) -> date:
    """基準にする終値の日付の上限。"""
    local = proposed_at.astimezone(JST)
    return local.date() if local.time() >= CLOSE_TIME else local.date() - timedelta(days=1)


def _change(df: pd.DataFrame, cutoff: date, days: int) -> tuple[float, date, date] | None:
    """cutoff 以前で最新の終値から、days 本（営業日）後の終値までの騰落率と、基準日・評価日。"""
    dates = df.index.date
    base_idx = int((dates <= cutoff).sum()) - 1
    if base_idx < 0 or base_idx + days >= len(df):
        return None
    base, target = df["Close"].iloc[base_idx], df["Close"].iloc[base_idx + days]
    return float(target / base - 1), dates[base_idx], dates[base_idx + days]


def change_since(df: pd.DataFrame | None, cutoff: date) -> float | None:
    """cutoff 以前で最新の終値から、直近の終値までの騰落率。"""
    if df is None:
        return None
    before = df[df.index.date <= cutoff]
    if before.empty or before.index[-1] == df.index[-1]:
        return None
    return float(df["Close"].iloc[-1] / before["Close"].iloc[-1] - 1)


def _period_change(df: pd.DataFrame | None, start: date, end: date) -> float | None:
    if df is None:
        return None
    dates = df.index.date
    a, b = df[dates <= start], df[dates <= end]
    if a.empty or b.empty:
        return None
    return float(b["Close"].iloc[-1] / a["Close"].iloc[-1] - 1)


async def build(days: int = 5) -> Review:
    since = market.now_jst() - timedelta(days=WINDOW_DAYS)
    proposals = await db.list_pending_since(since)
    tickers = sorted({p["ticker"] for p in proposals})
    # 大引け後の保存ジョブで直近の提案の日足は DB にあるので、足りない銘柄だけダウンロードする
    daily = await market.get_daily([*tickers, "1306"], years=1, refresh=False) if proposals else {}
    topix = daily.get("1306")

    outcomes, waiting = [], 0
    for p in proposals:
        df = daily.get(p["ticker"])
        result = _change(df, base_cutoff(p["created_at"]), days) if df is not None else None
        if result is None:
            waiting += 1
            continue
        change, base_date, target_date = result
        outcomes.append(
            Outcome(
                ticker=p["ticker"],
                company_name=p["company_name"],
                status=p["status"],
                kind=p["kind"],
                impact=p["impact"] or 0.0,
                category=p.get("category"),
                proposed_at=p["created_at"],
                change=change,
                topix=_period_change(topix, base_date, target_date),
            )
        )
    return Review(days=days, since=since.date(), outcomes=outcomes, waiting=waiting)
