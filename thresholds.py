"""AI の売買の基準値（Jev の確信度がこれ以上なら売買する）の保存・手動の変更・毎週の自動の見直し。

- 基準値は user_settings に保存する。保存がなければ、コード上の値（ai_trader.MODES・bold_trader の定数）を使う
- /threshold で手動で変えると、その値が自動の見直しの中心（base）になる
- 毎週土曜の振り返りで、買いの基準を直近 4 週間の判断から見直す。基準のすぐ下の候補が日経平均に勝っていたら下げ（見逃し）、
  すぐ上の候補が負けていたら上げる（外れ）。1 回 STEP ずつ、base から ±MAX_SHIFT の範囲で動かす
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta

import pandas as pd

import db
import market

STEP = 0.05  # 1 回の見直しで動かす幅
MAX_SHIFT = 0.15  # base からこれ以上は動かさない
WINDOW_DAYS = 28  # 見直しに使う判断の期間（直近 4 週間）
HORIZON = 5  # 判断した日の終値から、この営業日数後の終値までの値動きで成績を測る
MIN_COUNT = 5  # 基準のすぐ上・すぐ下の候補がこれ未満なら、判断せず据え置く
INDEX = "^N225"
_FACTS_PATTERN = re.compile(r"短期 (\d+)% ・ 長期 (\d+)%")


@dataclass(frozen=True)
class Spec:
    owner: str
    key: str  # 例: normal_buy / short_buy
    label: str  # 例: 慎重AI・通常モード・買い
    default: float
    auto: bool  # 毎週の自動の見直しの対象か（買いの基準だけ）


def specs() -> list[Spec]:
    """基準値の一覧。ai_trader・bold_trader はこのモジュールを使うので、循環しないよう呼ばれたときに読み込む。"""
    import ai_trader
    import bold_trader

    items = []
    for key, mode in ai_trader.MODES.items():
        name = mode.label.split(" ", 1)[1]
        items.append(Spec("ai", f"{key}_buy", f"慎重AI・{name}・買い", mode.buy_threshold, True))
        items.append(Spec("ai", f"{key}_sell", f"慎重AI・{name}・売り", mode.sell_threshold, False))
    items.append(Spec("ai_bold", "short_buy", "大胆AI・短期（特定口座）・買い", bold_trader.SHORT_THRESHOLD, True))
    items.append(Spec("ai_bold", "long_buy", "大胆AI・長期（NISA）・買い", bold_trader.LONG_THRESHOLD, True))
    items.append(Spec("ai_bold", "long_sell", "大胆AI・長期（NISA）・売り", bold_trader.LONG_SELL_THRESHOLD, False))
    return items


def _spec(owner: str, key: str) -> Spec:
    return next(s for s in specs() if s.owner == owner and s.key == key)


def _setting_key(owner: str, key: str) -> str:
    return f"threshold:{owner}:{key}"


def _read(settings: dict[str, str], spec: Spec) -> dict[str, float]:
    """{"base": 自動の見直しの中心, "value": 今の基準}。"""
    raw = settings.get(_setting_key(spec.owner, spec.key))
    if not raw:
        return {"base": spec.default, "value": spec.default}
    return json.loads(raw)


async def current(owner: str, key: str) -> float:
    return _read(await db.get_all_settings(), _spec(owner, key))["value"]


async def all_values() -> list[tuple[Spec, float, float]]:
    """(基準値の定義, 自動の見直しの中心, 今の基準) の一覧。"""
    settings = await db.get_all_settings()
    return [(s, (v := _read(settings, s))["base"], v["value"]) for s in specs()]


async def set_manual(owner: str, key: str, value: float) -> None:
    """手動で変える。その値を、これからの自動の見直しの中心にする。"""
    await db.set_setting(_setting_key(owner, key), json.dumps({"base": round(value, 2), "value": round(value, 2)}))


async def mode_with_settings(mode):
    """慎重 AI の性格（モード）の買い・売りの基準を、保存されている値にする。"""
    settings = await db.get_all_settings()
    buy = _read(settings, _spec("ai", f"{mode.key}_buy"))["value"]
    sell = _read(settings, _spec("ai", f"{mode.key}_sell"))["value"]
    return replace(mode, buy_threshold=buy, sell_threshold=sell)


# ---------------------------------------------------------------- 毎週の自動の見直し


@dataclass(frozen=True)
class Adjustment:
    spec: Spec
    before: float
    after: float
    reason: str

    def text(self) -> str:
        if self.after == self.before:
            return f"{self.spec.label}: 据え置き {self.before:.0%}（{self.reason}）"
        verb = "下げました" if self.after < self.before else "上げました"
        return f"{self.spec.label}: **{self.before:.0%} → {self.after:.0%}** に{verb}（{self.reason}）"


def _scores(owner: str, data: dict, entry: dict) -> dict[str, float]:
    """その判断の、基準値ごとの確信度（慎重 AI はモードの買い、大胆 AI は短期・長期の買い）。"""
    if owner == "ai":
        import ai_trader

        mode = next((k for k, m in ai_trader.MODES.items() if m.label == data.get("mode")), None)
        return {f"{mode}_buy": entry["confidence"] + (entry.get("bonus") or 0.0)} if mode else {}
    if "short" in entry and "long" in entry:
        return {"short_buy": entry["short"], "long_buy": entry["long"]}
    found = _FACTS_PATTERN.search(entry.get("facts") or "")  # 短期・長期を項目で残す前の記録
    return {"short_buy": int(found[1]) / 100, "long_buy": int(found[2]) / 100} if found else {}


def _excess(df: pd.DataFrame | None, index: pd.DataFrame | None, day: date) -> float | None:
    """判断した日の終値から HORIZON 営業日後の終値までの値動きの、日経平均との差。まだ測れなければ None。"""
    if df is None or index is None:
        return None

    def change(frame: pd.DataFrame) -> float | None:
        after = frame[frame.index.date > day]["Close"]
        base = frame[frame.index.date <= day]["Close"]
        if len(after) < HORIZON or base.empty:
            return None
        return float(after.iloc[HORIZON - 1] / base.iloc[-1] - 1)

    stock, market_change = change(df), change(index)
    return None if stock is None or market_change is None else stock - market_change


def _band_text(lo: float, hi: float, values: list[float]) -> str:
    """例: 「40%〜45% の 6 件が、日経平均を平均 4.0% 上回っていた」"""
    mean = sum(values) / len(values)
    return f"{lo:.0%}〜{hi:.0%} の {len(values)} 件が、日経平均を平均 {abs(mean):.1%} {'上回' if mean > 0 else '下回'}っていた"


async def auto_adjust(now: datetime) -> list[Adjustment]:
    """直近 4 週間の判断から、各 AI の買いの基準を見直して保存する。見直した結果（据え置きも含む）を返す。"""
    since = now.date() - timedelta(days=WINDOW_DAYS)
    seen: set[tuple] = set()
    samples: dict[tuple[str, str], list[tuple[float, str, date]]] = {}
    for d in await db.ai_decisions_since(since):
        week = d["decided_on"].isocalendar()[:2]
        for e in d["data"]["entries"]:
            if e.get("action") not in ("buy", "pass") or e.get("confidence") is None:
                continue
            if (d["owner"], e["ticker"], week) in seen:
                continue  # 同じ銘柄を毎日聞いていても、1 週間に 1 件として数える
            seen.add((d["owner"], e["ticker"], week))
            for key, score in _scores(d["owner"], d["data"], e).items():
                samples.setdefault((d["owner"], key), []).append((score, e["ticker"], d["decided_on"]))

    tickers = sorted({t for rows in samples.values() for _, t, _ in rows})
    daily = await market.get_daily([*tickers, INDEX], refresh=False) if tickers else {}
    settings = await db.get_all_settings()
    results = []
    for spec in specs():
        rows = samples.get((spec.owner, spec.key))
        if not spec.auto or not rows:
            continue
        saved = _read(settings, spec)
        t = saved["value"]
        below, above = [], []
        for score, ticker, day in rows:
            excess = _excess(daily.get(ticker), daily.get(INDEX), day)
            if excess is None:
                continue
            if t - STEP <= score < t:
                below.append(excess)
            elif t <= score < t + STEP:
                above.append(excess)
        missed = len(below) >= MIN_COUNT and sum(below) / len(below) > 0
        wrong = len(above) >= MIN_COUNT and sum(above) / len(above) < 0
        after = t
        if missed and wrong:
            reason = "すぐ下の候補は日経平均を上回り、すぐ上の候補は下回っていて、確信度が当たっていないため"
        elif missed:
            after, reason = t - STEP, f"見送ったすぐ下の {_band_text(t - STEP, t, below)}。見逃し"
        elif wrong:
            after, reason = t + STEP, f"買う判断のすぐ上の {_band_text(t, t + STEP, above)}。外れ"
        elif len(below) < MIN_COUNT and len(above) < MIN_COUNT:
            reason = f"{HORIZON} 営業日後の値動きを測れた、基準の前後の候補が少ない。すぐ下 {len(below)} 件・すぐ上 {len(above)} 件"
        else:
            reason = f"すぐ下 {len(below)} 件・すぐ上 {len(above)} 件で、見逃しも外れも目立たない"
        lo, hi = round(saved["base"] - MAX_SHIFT, 2), round(saved["base"] + MAX_SHIFT, 2)
        if after != t and not lo <= round(after, 2) <= hi:
            reason += f"。ただし見直しの範囲（{lo:.0%}〜{hi:.0%}）の端なので据え置き"
            after = t
        after = round(after, 2)
        if after != t:
            await db.set_setting(_setting_key(spec.owner, spec.key), json.dumps({"base": saved["base"], "value": after}))
        results.append(Adjustment(spec, t, after, reason))
    return results
