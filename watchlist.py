"""監視銘柄の管理: 価格アラートの判定と、週末の整理タイムの候補選び。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import pandas as pd

import db
import market
from market import JST

SLEEP_DAYS = 30  # この日数シグナルが出ていなければ「眠っている銘柄」
DRAWDOWN = -0.10  # 監視を始めてからこれ以上下がっていれば候補
KEEP_DAYS = 28  # 整理タイムで「続ける」を選んだ銘柄は、この日数は候補に出さない
TAG_LABELS = {"long": "長期", "short": "短期", "watch": "様子見"}


@dataclass(frozen=True)
class Hit:
    alert: dict
    price: float  # 条件に達したときの株価（アラートを作ってからの高値または安値）


def _reached_before(a: dict) -> bool:
    """作った日の、作る前の値動きで、すでに指定価格に達していたか（その日の日足を判定に使えない）。

    作った時点のその日の高値・安値がない（古いアラート）ときも、作った日の日足は使わない。
    """
    if a["direction"] == "above":
        return a.get("base_high") is None or a["base_high"] >= a["target"]
    return a.get("base_low") is None or a["base_low"] <= a["target"]


def check_alerts(alerts: list[dict], daily: dict[str, pd.DataFrame]) -> list[Hit]:
    """アラートを作った日以降の日足の高値・安値で、指定価格に達したかを調べる。

    作った日の日足には作る前の値動きも含まれるので、作る前に指定価格に達していた日は判定に使わない。
    """
    hits = []
    for a in alerts:
        df = daily.get(a["ticker"])
        if df is None or df.empty:
            continue
        created_on = a["created_at"].astimezone(JST).date()
        since = df[df.index.date >= created_on]
        if _reached_before(a):
            since = since[since.index.date > created_on]
        if since.empty:
            continue
        if a["direction"] == "above" and since["High"].max() >= a["target"]:
            hits.append(Hit(a, float(since["High"].max())))
        elif a["direction"] == "below" and since["Low"].min() <= a["target"]:
            hits.append(Hit(a, float(since["Low"].min())))
    return hits


@dataclass(frozen=True)
class CleanupCandidate:
    ticker: str
    name: str
    kind: str  # sleeping / drawdown / oldest
    detail: str

    @property
    def label(self) -> str:
        return {"sleeping": "😴 眠っている", "drawdown": "📉 下がっている", "oldest": "🕰️ いちばん古い"}[self.kind]


async def cleanup_candidates(now: datetime) -> list[CleanupCandidate]:
    """外す候補を最大 3 つ（眠っている・下がっている・いちばん古い、を 1 つずつ）選ぶ。"""
    stocks = [
        s for s in await db.list_monitored() if not (s["kept_at"] and now - s["kept_at"] < timedelta(days=KEEP_DAYS))
    ]
    if not stocks:
        return []
    daily = await market.get_daily([s["ticker"] for s in stocks], refresh=False)
    picks: list[CleanupCandidate] = []
    used: set[str] = set()

    def add(stock, kind, detail):
        if stock["ticker"] not in used:
            used.add(stock["ticker"])
            picks.append(CleanupCandidate(stock["ticker"], stock["company_name"], kind, detail))

    def last_activity(s):
        return s["last_notified_at"] or s["added_at"]

    sleeping = [s for s in stocks if now - last_activity(s) >= timedelta(days=SLEEP_DAYS)]
    if sleeping:
        s = min(sleeping, key=last_activity)
        days = (now - last_activity(s)).days
        add(s, "sleeping", f"{days} 日間シグナルなし")

    drops = []
    for s in stocks:
        df = daily.get(s["ticker"])
        if df is None or df.empty:
            continue
        before = df[df.index.date <= s["added_at"].astimezone(JST).date()]
        base = before["Close"].iloc[-1] if not before.empty else df["Close"].iloc[0]
        change = float(df["Close"].iloc[-1] / base - 1)
        if change <= DRAWDOWN:
            drops.append((change, s))
    if drops:
        change, s = min(drops, key=lambda x: x[0])
        add(s, "drawdown", f"監視開始から {change:+.1%}")

    oldest = min(stocks, key=lambda s: s["added_at"])
    add(oldest, "oldest", f"{(now - oldest['added_at']).days} 日前から監視")
    return picks
