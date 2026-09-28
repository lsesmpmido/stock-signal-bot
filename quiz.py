"""銘柄当てクイズ: 銘柄名と価格を隠したチャートを出題し、次の出題のときに答えを発表する。"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta

import pandas as pd

import db
import market

CHART_BARS = 125  # 出題するチャートの期間（約 6 か月）
PROPOSAL_DAYS = 30  # 直近この日数に提案した銘柄も出題の候補にする
NO_REPEAT = 20  # 直近この回数のクイズの正解は出さない
# 選択肢を埋めるための、よく知られた銘柄
FAMOUS = ["7203", "6758", "9984", "8306", "9432", "6861", "8035", "4063", "9983", "7974", "6501", "8058", "4502", "6098"]


@dataclass(frozen=True)
class Question:
    code: str
    name: str
    sector: str
    choices: list[tuple[str, str]]  # [(証券コード, 銘柄名), ...]
    chart: pd.DataFrame  # 出題する期間の日足
    change: float  # その期間の騰落率


async def make_question(master, now: datetime) -> Question | None:
    """監視銘柄・保有銘柄・直近の提案銘柄から 1 社を選び、ほかの 3 社を選択肢に混ぜる（日付ごとに同じ結果）。"""
    rng = random.Random(now.date().toordinal())
    pool = [s["ticker"] for s in await db.list_monitored()]
    pool += [p["ticker"] for p in await db.vp_positions("you")]
    pool += [p["ticker"] for p in await db.list_pending_since(now - timedelta(days=PROPOSAL_DAYS))]
    pool = [c for c in dict.fromkeys(pool) if master.get(c)]
    used = set(await db.recent_quiz_answers(NO_REPEAT))
    candidates = [c for c in pool if c not in used] or pool
    if not candidates:
        return None
    daily = await market.get_daily(candidates, refresh=False)
    candidates = [c for c in candidates if c in daily and len(daily[c]) >= CHART_BARS]
    if not candidates:
        return None
    code = rng.choice(candidates)
    others = [c for c in dict.fromkeys([*pool, *FAMOUS]) if c != code and master.get(c)]
    wrong = rng.sample(others, min(3, len(others)))
    choices = [(c, master.get(c).name) for c in [code, *wrong]]
    rng.shuffle(choices)
    chart = daily[code].tail(CHART_BARS)
    change = float(chart["Close"].iloc[-1] / chart["Close"].iloc[0] - 1)
    info = master.get(code)
    return Question(code, info.name, info.sector or "不明", choices, chart, change)
