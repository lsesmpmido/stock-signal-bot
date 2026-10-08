"""AI の売買の基準値（Jev の確信度がこれ以上なら売買する）の保存・手動の変更・毎週の自動の見直し。

- 基準値は user_settings に保存する。保存がなければ、コード上の値（ai_trader.MODES・bold_trader の定数）を使う
- /threshold で手動で変えると、その値が自動の見直しの中心（base）になる
- 毎週土曜の振り返りで、買い・売りの基準を直近 4 週間の判断から見直す。base から ±MAX_SHIFT の範囲（1 ポイント刻み）の
  基準をそれぞれ試し、正しかった判断から間違っていた判断を引いた件数が最も多い基準へ、1 回に最大 MAX_STEP だけ近づける。
  判定が良くなる判断が MIN_GAIN 件に満たなければ据え置く
  - 買い: 買う判断の後に日経平均に勝てば当たり、負ければ外れ。見送った後に勝てば見逃し、負ければ見送りで正解
  - 売り: 売る判断の後に日経平均に負ければ売って正解、勝てば早すぎた。持ち続けた後に勝てば持ち続けて正解、負ければ売り遅れ
  - 利益確定（堅実モード）: 含み益がライン以上なら売ったとみなし、売りと同じように数える（Jev の確信度ではなく含み益で分ける）
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta

import pandas as pd

import db
import market

STEP = 0.01  # 試す基準の刻み（1 ポイント）
MAX_STEP = 0.05  # 1 回の見直しで動かす最大の幅
MAX_SHIFT = 0.15  # base からこれ以上は動かさない
WINDOW_DAYS = 28  # 見直しに使う判断の期間（直近 4 週間）
HORIZON = 5  # 判断した日の終値から、この営業日数後の終値までの値動きで成績を測る
MIN_COUNT = 5  # 値動きを測れた候補がこれ未満なら、判断せず据え置く
MIN_GAIN = 2  # 判定が良い方に変わる候補がこの件数に満たなければ動かさない（1 件程度の差は偶然のぶれとみなす）
INDEX = "^N225"
_FACTS_PATTERN = re.compile(r"短期 (\d+)% ・ 長期 (\d+)%")
_RETURN_PATTERN = re.compile(r"含み損益 ([+-]\d+(?:\.\d+)?)%")


@dataclass(frozen=True)
class Spec:
    owner: str
    key: str  # 例: normal_buy / short_buy
    label: str  # 例: 慎重AI・通常モード・買い
    default: float
    side: str  # buy / sell / take_profit（利益確定のライン。Jev の確信度ではなく含み益と比べる）


def specs() -> list[Spec]:
    """基準値の一覧。ai_trader・bold_trader はこのモジュールを使うので、循環しないよう呼ばれたときに読み込む。"""
    import ai_trader
    import bold_trader

    items = []
    for key, mode in ai_trader.MODES.items():
        name = mode.label.split(" ", 1)[1]
        items.append(Spec("ai", f"{key}_buy", f"慎重AI・{name}・買い", mode.buy_threshold, "buy"))
        items.append(Spec("ai", f"{key}_sell", f"慎重AI・{name}・売り", mode.sell_threshold, "sell"))
        if mode.take_profit is not None:
            items.append(Spec("ai", f"{key}_take_profit", f"慎重AI・{name}・利益確定（含み益）", mode.take_profit, "take_profit"))
    items.append(Spec("ai_bold", "short_buy", "大胆AI・短期（特定口座）・買い", bold_trader.SHORT_THRESHOLD, "buy"))
    items.append(Spec("ai_bold", "long_buy", "大胆AI・長期（NISA）・買い", bold_trader.LONG_THRESHOLD, "buy"))
    items.append(Spec("ai_bold", "long_sell", "大胆AI・長期（NISA）・売り", bold_trader.LONG_SELL_THRESHOLD, "sell"))
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


# 慎重 AI の性格の大胆さ（大きいほど大胆で、買いの基準が低い）。同じ大胆さどうしでは基準を引き継がない
BOLDNESS = {"steady": 0, "normal": 1, "apprentice": 1, "gambler": 2}
LAST_MODE_KEY = "threshold:ai:last_mode"


async def carry_over(mode) -> str | None:
    """慎重 AI の性格が変わったとき、新しい性格の買い・売りの基準が性格の向きと逆なら、前の性格の今の基準を引き継ぐ。

    大胆な性格ほど、買いの基準は低く（買いやすく）、売りの基準は高い（持ち続けやすい）。大胆な性格に変わったのに
    前の性格より買いの基準が高い・売りの基準が低いとき（慎重な性格に変わったときはその逆）に引き継ぐ。毎週の自動の
    見直しで性格ごとの基準が動き、性格の順番が逆になることがあるため。引き継いだら、その説明を返す。
    """
    import ai_trader

    settings = await db.get_all_settings()
    last = settings.get(LAST_MODE_KEY)
    if last == mode.key:
        return None
    await db.set_setting(LAST_MODE_KEY, mode.key)
    if last not in BOLDNESS or BOLDNESS[last] == BOLDNESS[mode.key]:
        return None  # 初めての判断、または同じ大胆さの性格への切り替え
    bolder = BOLDNESS[mode.key] > BOLDNESS[last]
    before, after = ai_trader.MODES[last].label, mode.label
    notes = []
    for side, name in (("buy", "買い"), ("sell", "売り")):
        previous = _read(settings, _spec("ai", f"{last}_{side}"))["value"]
        new_spec = _spec("ai", f"{mode.key}_{side}")
        saved = _read(settings, new_spec)
        lower = saved["value"] < previous  # 新しい性格の基準のほうが低い
        # 買いは基準が低いほど大胆、売りは基準が高いほど大胆
        too_cautious = (not lower and saved["value"] != previous) if side == "buy" else lower
        if saved["value"] == previous or too_cautious != bolder:
            continue
        await db.set_setting(_setting_key("ai", new_spec.key), json.dumps({"base": saved["base"], "value": previous}))
        direction = "慎重" if bolder else "大胆"
        notes.append(
            f"{after}の{name}の基準（{saved['value']:.0%}）が{before}の今の基準（{previous:.0%}）より{direction}だったため、"
            f"{previous:.0%} を引き継ぎました"
        )
    return f"性格が{before}から{after}に変わりました。" + "。".join(notes) if notes else None


async def mode_with_settings(mode):
    """慎重 AI の性格（モード）の買い・売りの基準と利益確定のラインを、保存されている値にする。"""
    settings = await db.get_all_settings()
    buy = _read(settings, _spec("ai", f"{mode.key}_buy"))["value"]
    sell = _read(settings, _spec("ai", f"{mode.key}_sell"))["value"]
    take_profit = mode.take_profit
    if take_profit is not None:
        take_profit = _read(settings, _spec("ai", f"{mode.key}_take_profit"))["value"]
    return replace(mode, buy_threshold=buy, sell_threshold=sell, take_profit=take_profit)


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


def _mode_key(data: dict) -> str | None:
    import ai_trader

    return next((k for k, m in ai_trader.MODES.items() if m.label == data.get("mode")), None)


def _buy_scores(owner: str, data: dict, entry: dict) -> dict[str, float]:
    """買うかどうかの判断の、基準値ごとの確信度（慎重 AI は性格の買い、大胆 AI は短期・長期の買い）。"""
    if owner == "ai":
        mode = _mode_key(data)
        return {f"{mode}_buy": entry["confidence"] + (entry.get("bonus") or 0.0)} if mode else {}
    if "short" in entry and "long" in entry:
        return {"short_buy": entry["short"], "long_buy": entry["long"]}
    found = _FACTS_PATTERN.search(entry.get("facts") or "")  # 短期・長期を項目で残す前の記録
    return {"short_buy": int(found[1]) / 100, "long_buy": int(found[2]) / 100} if found else {}


def _sell_scores(owner: str, data: dict, entry: dict) -> dict[str, float]:
    """売るかどうかの判断（Jev の「売る」の確信度）の、基準値ごとの確信度。

    慎重 AI の NISA の保有は「売りの基準 + NISA_SELL_MARGIN」で売るので、その分を引いて売りの基準と比べられるようにする。
    NISA かどうかを残す前の記録は、どちらの基準で判断したか分からないので使わない。大胆 AI は長期（NISA）の保有だけ Jev に聞く。
    """
    if entry.get("confidence") is None:
        return {}  # 損切りなどのルールで売ったもの・Jev に聞かなかったもの
    if owner == "ai_bold":
        return {"long_sell": entry["confidence"]}
    import ai_trader

    mode = _mode_key(data)
    if mode is None or "nisa" not in entry:
        return {}
    return {f"{mode}_sell": entry["confidence"] - (ai_trader.NISA_SELL_MARGIN if entry["nisa"] else 0.0)}


def _take_profit_scores(owner: str, data: dict, entry: dict) -> dict[str, float]:
    """利益確定のラインと比べる、その判断の時点の含み益（利益確定のルールがある性格だけ）。

    NISA の最低保有期間中の保有は利益確定の対象外なので使わない。含み益を項目で残す前の記録は、文章から読み取る。
    """
    import ai_trader

    mode = _mode_key(data)
    if owner != "ai" or mode is None or ai_trader.MODES[mode].take_profit is None:
        return {}
    if "最低保有期間" in (entry.get("note") or ""):
        return {}
    rate = entry.get("return_rate")
    if rate is None:
        found = _RETURN_PATTERN.search(entry.get("facts") or "")
        if not found:
            return {}
        rate = float(found[1]) / 100
    return {f"{mode}_take_profit": rate}


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


# 判定の呼び名: (基準以上で正しい, 基準未満で正しい, 基準以上で間違い, 基準未満で間違い)
WORDS = {
    "buy": ("当たり", "見送りで正解", "外れ", "見逃し"),
    "sell": ("売って正解", "持ち続けて正解", "早すぎた", "売り遅れ"),
    "take_profit": ("売って正解", "持ち続けて正解", "早すぎた", "売り遅れ"),
}


@dataclass(frozen=True)
class Tally:
    """ある基準で数え直したときの、基準以上（買う・売る）と基準未満（見送る・持ち続ける）の正しい・間違いの件数。

    買いの基準では「良い」は日経平均に勝ったこと、売りの基準では負けたこと（売った後に下がれば正解）。
    """

    hit: int  # 基準以上で、良い結果
    right_skip: int  # 基準未満で、良い結果ではなかった
    miss: int  # 基準以上で、良い結果ではなかった
    overlooked: int  # 基準未満で、良い結果

    @property
    def net(self) -> int:
        return self.hit + self.right_skip - self.miss - self.overlooked


def _tally(threshold: float, rows: list[tuple[float, float]]) -> Tally:
    """(確信度, 良さ) の並びを、その基準の以上・未満に分けて数える。良さが正なら良い結果。"""
    hit = sum(1 for score, excess in rows if score >= threshold and excess > 0)
    miss = sum(1 for score, excess in rows if score >= threshold and excess <= 0)
    overlooked = sum(1 for score, excess in rows if score < threshold and excess > 0)
    right_skip = sum(1 for score, excess in rows if score < threshold and excess <= 0)
    return Tally(hit, right_skip, miss, overlooked)


def choose(current: float, base: float, rows: list[tuple[float, float]], side: str = "buy") -> tuple[float, str]:
    """見直しの範囲（base ±MAX_SHIFT、1 ポイント刻み）で最も成績の良い基準を探し、そこへ最大 MAX_STEP だけ近づける。

    成績は、正しかった判断から間違っていた判断を引いた件数。今の基準より判定が良くなる判断が MIN_GAIN 件に満たなければ据え置く。
    rows は (確信度, 良さ)。良さは、買いなら日経平均との差、売りならその符号を逆にしたもの。
    """
    good, right, wrong, missed = WORDS[side]
    if len(rows) < MIN_COUNT:
        return current, f"{HORIZON} 営業日後の値動きを測れた判断が少ない（{len(rows)} 件）"
    lo, hi = max(round(base - MAX_SHIFT, 2), 0.01), min(round(base + MAX_SHIFT, 2), 0.99)
    candidates = [round(lo + i * STEP, 2) for i in range(round((hi - lo) / STEP) + 1)]
    now_tally = _tally(current, rows)
    # 成績が同じなら、今の基準に近いほうを選ぶ（必要以上に動かさない）
    best = max(candidates, key=lambda c: (_tally(c, rows).net, -abs(c - current)))
    gain = (_tally(best, rows).net - now_tally.net) // 2  # 判定が良い方に変わる候補の数（1 件変わると差し引きは 2 動く）
    counts = (
        f"{len(rows)} 件中、今の基準で {good} {now_tally.hit}・{right} {now_tally.right_skip}・"
        f"{wrong} {now_tally.miss}・{missed} {now_tally.overlooked}"
    )
    if best == current or gain < MIN_GAIN:
        better = f"判定が良くなる判断は {gain} 件だけ" if gain > 0 else "判定は良くならない"
        return current, f"{counts}。見直しの範囲（{lo:.0%}〜{hi:.0%}）のどの基準にしても、{better}"
    after = round(current + max(-MAX_STEP, min(MAX_STEP, best - current)), 2)
    after_tally = _tally(after, rows)
    change = f"{wrong} {now_tally.miss}→{after_tally.miss} 件・{missed} {now_tally.overlooked}→{after_tally.overlooked} 件"
    if after == best:
        return after, f"{counts}。{after:.0%} にすると {change} で、{gain} 件の判定が良くなる"
    return after, (
        f"{counts}。最も良いのは {best:.0%}（{gain} 件の判定が良くなる）だが、1 回に動かすのは"
        f" {MAX_STEP * 100:.0f} ポイントまでなので {after:.0%} に（{change}）"
    )


async def auto_adjust(now: datetime) -> list[Adjustment]:
    """直近 4 週間の判断から、各 AI の買い・売りの基準を見直して保存する。見直した結果（据え置きも含む）を返す。

    同じ銘柄を毎日判断していても、1 週間に 1 件として数える（売りは、その週に売っていれば売る判断のほうで数える）。
    売りの基準には、損切りなどのルールで売ったものは Jev の確信度がないので使わない（利益確定のラインには含み益で使う）。
    """
    since = now.date() - timedelta(days=WINDOW_DAYS)
    buy_seen: set[tuple] = set()
    # (持ち主, 銘柄, 週) → (sell/hold, 判断の記録, 項目, 判断した日)。売りの基準には Jev の確信度がある判断だけを使い、
    # 利益確定のラインにはルールで売ったものも含めて使うので、別々に選ぶ
    sell_first: dict[tuple, tuple] = {}
    position_first: dict[tuple, tuple] = {}
    samples: dict[tuple[str, str], list[tuple[float, str, date]]] = {}
    for d in await db.ai_decisions_since(since):
        week = d["decided_on"].isocalendar()[:2]
        for e in d["data"]["entries"]:
            action = e.get("action")
            key = (d["owner"], e["ticker"], week)
            if action in ("buy", "pass") and e.get("confidence") is not None and key not in buy_seen:
                buy_seen.add(key)
                for spec_key, score in _buy_scores(d["owner"], d["data"], e).items():
                    samples.setdefault((d["owner"], spec_key), []).append((score, e["ticker"], d["decided_on"]))
            elif action in ("sell", "hold"):
                targets = (position_first, sell_first) if e.get("confidence") is not None else (position_first,)
                for first in targets:
                    if key not in first or (action == "sell" and first[key][0] == "hold"):
                        first[key] = (action, d["data"], e, d["decided_on"])
    for first, scorer in ((sell_first, _sell_scores), (position_first, _take_profit_scores)):
        for (owner, ticker, _), (_, data, e, day) in first.items():
            for spec_key, score in scorer(owner, data, e).items():
                samples.setdefault((owner, spec_key), []).append((score, ticker, day))

    tickers = sorted({t for rows in samples.values() for _, t, _ in rows})
    daily = await market.get_daily([*tickers, INDEX], refresh=False) if tickers else {}
    settings = await db.get_all_settings()
    results = []
    for spec in specs():
        rows = samples.get((spec.owner, spec.key))
        if not rows:
            continue
        saved = _read(settings, spec)
        sign = 1 if spec.side == "buy" else -1  # 売り・利益確定は、売った後に日経平均に負けていれば良い結果
        measured = [
            (score, sign * excess)
            for score, ticker, day in rows
            if (excess := _excess(daily.get(ticker), daily.get(INDEX), day)) is not None
        ]
        after, reason = choose(saved["value"], saved["base"], measured, spec.side)
        if after != saved["value"]:
            await db.set_setting(_setting_key(spec.owner, spec.key), json.dumps({"base": saved["base"], "value": after}))
        results.append(Adjustment(spec, saved["value"], after, reason))
    return results
