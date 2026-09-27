"""テクニカル指標の計算と、売買シグナルの判定。

「RSI ≦ 30」のような条件は数日続けて成立するため、条件そのものではなく
状態が変わった瞬間（例: RSI が 30 以下の領域に入った、MA25 が MA75 を上抜いた）だけをシグナルとする。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import pandas as pd
from ta.momentum import RSIIndicator
from ta.trend import MACD

RSI_LOW = 30
RSI_HIGH = 70
COOLDOWN = timedelta(hours=24)


@dataclass(frozen=True)
class Signal:
    side: str  # "BUY" / "SELL"
    key: str  # クールダウン管理用の識別子
    label: str


SIGNALS = {
    ("rsi", "low"): Signal("BUY", "BUY:RSI", f"RSI 売られすぎ (≦{RSI_LOW})"),
    ("rsi", "high"): Signal("SELL", "SELL:RSI", f"RSI 買われすぎ (≧{RSI_HIGH})"),
    ("ma", "above"): Signal("BUY", "BUY:GC", "ゴールデンクロス (25日線 × 75日線)"),
    ("ma", "below"): Signal("SELL", "SELL:DC", "デッドクロス (25日線 × 75日線)"),
    ("macd", "above"): Signal("BUY", "BUY:MACD", "MACD ゴールデンクロス"),
    ("macd", "below"): Signal("SELL", "SELL:MACD", "MACD デッドクロス"),
}


def compute(daily: pd.DataFrame) -> pd.DataFrame:
    """日足に移動平均 25/75/200・RSI(14)・MACD(12,26,9) を追加した DataFrame を返す。"""
    df = daily.copy()
    close = df["Close"]
    for window in (25, 75, 200):
        df[f"MA{window}"] = close.rolling(window).mean()
    df["RSI"] = RSIIndicator(close, window=14).rsi()
    macd = MACD(close, window_slow=26, window_fast=12, window_sign=9)
    df["MACD"] = macd.macd()
    df["MACD_signal"] = macd.macd_signal()
    df["MACD_hist"] = macd.macd_diff()
    return df


def _side(a: float, b: float) -> str | None:
    if pd.isna(a) or pd.isna(b):
        return None
    return "above" if a > b else "below"


def state_of(ind: pd.DataFrame) -> dict[str, str | None]:
    """最新の足の状態。"""
    last = ind.iloc[-1]
    rsi = last["RSI"]
    if pd.isna(rsi):
        rsi_zone = None
    elif rsi <= RSI_LOW:
        rsi_zone = "low"
    elif rsi >= RSI_HIGH:
        rsi_zone = "high"
    else:
        rsi_zone = "mid"
    return {
        "rsi": rsi_zone,
        "ma": _side(last["MA25"], last["MA75"]),
        "macd": _side(last["MACD"], last["MACD_signal"]),
    }


def detect(prev: dict | None, cur: dict) -> list[Signal]:
    """前回の状態から変化した項目だけをシグナルにする。前回の状態がない（監視開始直後）ときは何も出さない。"""
    if not prev:
        return []
    events = []
    for name, value in cur.items():
        before = prev.get(name)
        if value is None or before is None or value == before:
            continue
        if signal := SIGNALS.get((name, value)):
            events.append(signal)
    return events


def apply_cooldown(events: list[Signal], sent: dict[str, str], now: datetime) -> list[Signal]:
    """同じシグナルは 24 時間に 1 回まで（RSI が 30 付近を行き来したときの連続通知を防ぐ）。"""
    return [e for e in events if e.key not in sent or now - datetime.fromisoformat(sent[e.key]) >= COOLDOWN]
