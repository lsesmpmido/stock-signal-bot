"""/ask: 指定した銘柄が今買い時か売り時かを、集めた記事と値動き（RSI・MACD など）から Jev に判定させる。

- 記事: 直近 NEWS_DAYS 日の記事を社名で検索し、まだ判定していない記事を Jev で判定して news_judgements に残す（1 回 MAX_NEW_ARTICLES 件まで）
- 既定では、自分が持っていない銘柄は「買うべきか」、持っている銘柄は「売るべきか」を聞く（慎重 AI と同じ質問）。
  どちらを聞くかは指定もできる（持っていない銘柄の売り時は「持っているとしたら」の目安）
- 今は買い時・売り時ではない（様子見・見送り・持ち続け）ときは、判断し直すまでに待つ期間の目安（1週間以内〜半年・不明）も Jev に聞く
- Jev は理由の文章を返さないので、判断に使った材料（指標の値・記事ごとの評価）を一緒に返して表示する
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import pandas as pd

import ai_trader
import db
import market
import news
import signals
from jev_client import CATEGORIES, JevJudge
from market import JST

log = logging.getLogger(__name__)

NEWS_DAYS = 7
MAX_NEW_ARTICLES = 5
MAX_NEWS_FOR_JEV = 5  # 判定に渡す記事の数（新しい順）
CROSS_LOOKBACK = 5  # MACD とシグナル線のクロスを、直近この本数までさかのぼって探す
HIGH = 0.65  # 確信度がこれ以上なら「買い時」「売り時」
LOW = 0.35  # これ以下なら「見送り」「持ち続け」
JEV_CONCURRENCY = 3

# (表示, 色) 。色は discord.Color の値
VERDICTS = {
    ("buy", "high"): ("🟢 買い時", 0x2ECC71),
    ("buy", "mid"): ("🟡 様子見", 0xF1C40F),
    ("buy", "low"): ("⚪ 見送り", 0x95A5A6),
    ("sell", "high"): ("🔴 売り時", 0xE74C3C),
    ("sell", "mid"): ("🟡 様子見", 0xF1C40F),
    ("sell", "low"): ("🔵 持ち続け", 0x3498DB),  # 買い時（緑）と見分けられるよう青にする
}


class AdviceError(Exception):
    """利用者に見せるエラー（株価データが足りない・Jev の判定に失敗したなど）。"""


@dataclass
class Advice:
    code: str
    name: str
    question: str  # buy（持っていない）/ sell（持っている）
    confidence: float
    ind: pd.DataFrame  # チャート用（指標付きの日足）
    features: dict
    macd: dict
    holding: dict | None
    articles: list[dict] = field(default_factory=list)  # 新しい順
    new_articles: int = 0  # 今回 Jev で判定した記事の数
    wait: str | None = None  # 買い時・売り時でないときの、待つ期間の目安（WAIT_HORIZONS のキー。聞けなかったら None）

    @property
    def level(self) -> str:
        return "high" if self.confidence >= HIGH else "low" if self.confidence <= LOW else "mid"

    @property
    def verdict(self) -> str:
        return VERDICTS[(self.question, self.level)][0]

    @property
    def color(self) -> int:
        return VERDICTS[(self.question, self.level)][1]


def macd_state(ind: pd.DataFrame) -> dict:
    """MACD とシグナル線の値、どちらが上か、直近のクロス（何本前か）。"""
    last = ind.iloc[-1]
    hist = ind["MACD_hist"].dropna()
    cross = None
    for back in range(1, min(CROSS_LOOKBACK, len(hist) - 1) + 1):
        now, before = hist.iloc[-back], hist.iloc[-back - 1]
        if (now > 0) != (before > 0):
            cross = {"type": "golden" if now > 0 else "dead", "bars_ago": back - 1}
            break
    return {
        "macd": None if pd.isna(last["MACD"]) else round(float(last["MACD"]), 2),
        "signal": None if pd.isna(last["MACD_signal"]) else round(float(last["MACD_signal"]), 2),
        "diff": None if pd.isna(last["MACD_hist"]) else round(float(last["MACD_hist"]), 2),
        "recent_cross": cross,
    }


async def _judge_new_articles(jev: JevJudge, code: str, name: str) -> int:
    """直近の記事のうち、まだ判定していないものを Jev で判定して残し、判定した数を返す。"""
    try:
        items = await news.search_company(name, days=NEWS_DAYS, limit=10)
    except Exception:
        log.warning("ニュースを検索できませんでした: %s", name, exc_info=True)
        return 0
    done = await db.judged_urls(code)
    targets = [i for i in items if i.url not in done][:MAX_NEW_ARTICLES]
    semaphore = asyncio.Semaphore(JEV_CONCURRENCY)

    async def judge(item) -> bool:
        async with semaphore:
            try:
                j = await jev.judge(item.title, item.summary, name)
            except Exception:
                log.warning("Jev 判定に失敗: %s / %s", name, item.title, exc_info=True)
                return False
        await db.save_judgement(code, item.url, item.title, "ask", j.is_positive, j.impact, j.category)
        return True

    return sum(await asyncio.gather(*(judge(i) for i in targets)))


async def _holding(code: str, df: pd.DataFrame, today) -> dict | None:
    positions = await db.vp_positions("you", code)
    if not positions:
        return None
    shares = sum(p["shares"] for p in positions)
    cost = sum(p["cost"] for p in positions)
    value = float(df["Close"].iloc[-1]) * shares
    opened = min(p["opened_at"] for p in positions).astimezone(JST).date()
    return {
        "shares": shares,
        "cost": cost,
        "value": value,
        "return_rate": value / cost - 1 if cost else 0.0,
        "held_trading_days": market.trading_days_between(opened, today),
        "accounts": sorted({p["account"] for p in positions}),
    }


async def advise(jev: JevJudge, code: str, name: str, now: datetime, question: str | None = None) -> Advice:
    """question は "buy"（買うべきか）/ "sell"（売るべきか）。None なら、持っていれば sell、なければ buy。"""
    df = (await market.get_daily([code])).get(code)
    if df is None or len(df) < 80:
        raise AdviceError(f"{name} ({code}) の株価データを十分に取得できませんでした。")
    ind = signals.compute(df)
    features = ai_trader.features(df)
    macd = macd_state(ind)
    new_articles = await _judge_new_articles(jev, code, name)
    articles = list(reversed(await db.judgements(code, now - timedelta(days=NEWS_DAYS))))
    holding = await _holding(code, df, now.date())

    recent_news = [
        {
            "headline": a["news_title"],
            "positive_probability": round(a["is_positive"], 2),
            "impact_0_to_2": round(a["impact"], 2),
            "category": CATEGORIES[a["category"]][0].split(" ", 1)[1] if a["category"] in CATEGORIES else None,
        }
        for a in articles[:MAX_NEWS_FOR_JEV]
    ]
    state = {"company": name, **features, "macd": macd, "recent_news": recent_news or "直近 7 日の記事なし"}
    question = question or ("sell" if holding else "buy")
    if holding:
        state["holding"] = {"return_rate": round(holding["return_rate"], 4), "held_trading_days": holding["held_trading_days"]}
    elif question == "sell":
        state["holding"] = "保有していない。持っているとしたら、値動きと材料から今売るべきかを判断する"
    try:
        if question == "sell":
            confidence = await jev.should_sell(state)
        else:
            confidence = await jev.should_buy({**state, "sources": ["あなたからの質問"]})
    except Exception as exc:
        log.warning("Jev の判定（/ask）に失敗: %s", code, exc_info=True)
        raise AdviceError("Jev の判定に失敗しました。時間をおいて再度お試しください。") from exc
    advice = Advice(code, name, question, confidence, ind, features, macd, holding, articles, new_articles)
    if advice.level != "high":
        try:
            advice.wait = await jev.wait_horizon(state, question)
        except Exception:  # 目安はおまけなので、聞けなくても判定は返す
            log.warning("Jev の待つ期間の判定（/ask）に失敗: %s", code, exc_info=True)
    return advice
