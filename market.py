"""yfinance からの株価取得（キャッシュ・リトライ付き）と、東証の取引日判定。"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

import jpholiday
import pandas as pd
import yfinance as yf

log = logging.getLogger(__name__)

JST = ZoneInfo("Asia/Tokyo")
OHLCV = ["Open", "High", "Low", "Close", "Volume"]
CHUNK_SIZE = 20
CACHE_TTL_SECONDS = 600
MAX_ATTEMPTS = 4

_daily_cache: dict[str, tuple[float, pd.DataFrame]] = {}


def symbol(code: str) -> str:
    return f"{code}.T"


def is_trading_day(d: date) -> bool:
    """土日・祝日・年末年始（12/31〜1/3）を除く。"""
    if d.weekday() >= 5 or jpholiday.is_holiday(d):
        return False
    return not ((d.month == 12 and d.day == 31) or (d.month == 1 and d.day <= 3))


def now_jst() -> datetime:
    return datetime.now(JST)


def _with_retry(func, *args):
    """yfinance はレート制限（YFRateLimitError）や空データを返しやすいので、待ち時間を延ばしながら再試行する。"""
    for attempt in range(MAX_ATTEMPTS):
        try:
            result = func(*args)
            if result is not None and not result.empty:
                return result
            raise ValueError("空のデータ")
        except Exception as exc:
            if attempt == MAX_ATTEMPTS - 1:
                raise
            wait = 5 * 2**attempt
            log.warning("yfinance 取得失敗 (%s)。%d 秒後に再試行します", exc, wait)
            time.sleep(wait)
    return None


def _download_daily(symbols: list[str]) -> pd.DataFrame:
    return yf.download(
        symbols,
        period="2y",
        interval="1d",
        group_by="ticker",
        auto_adjust=True,
        progress=False,
        threads=False,
    )


def _split_by_symbol(raw: pd.DataFrame, symbols: list[str]) -> dict[str, pd.DataFrame]:
    frames = {}
    for sym in symbols:
        if isinstance(raw.columns, pd.MultiIndex):
            if sym not in raw.columns.get_level_values(0):
                continue
            df = raw[sym]
        else:
            df = raw
        df = df[[c for c in OHLCV if c in df.columns]].dropna(subset=["Close"])
        if not df.empty:
            frames[sym] = df
    return frames


def _fetch_daily_sync(codes: list[str]) -> dict[str, pd.DataFrame]:
    now = time.time()
    result: dict[str, pd.DataFrame] = {}
    missing = []
    for code in codes:
        hit = _daily_cache.get(code)
        if hit and now - hit[0] < CACHE_TTL_SECONDS:
            result[code] = hit[1]
        else:
            missing.append(code)
    for i in range(0, len(missing), CHUNK_SIZE):
        chunk = missing[i : i + CHUNK_SIZE]
        symbols = [symbol(c) for c in chunk]
        try:
            raw = _with_retry(_download_daily, symbols)
        except Exception:
            log.exception("日足の取得に失敗: %s", chunk)
            continue
        frames = _split_by_symbol(raw, symbols)
        for code in chunk:
            if (df := frames.get(symbol(code))) is not None:
                _daily_cache[code] = (now, df)
                result[code] = df
        if i + CHUNK_SIZE < len(missing):
            time.sleep(2)
    return result


async def fetch_daily(codes: list[str]) -> dict[str, pd.DataFrame]:
    """複数銘柄の日足（2 年分）をまとめて取得する。取得できなかった銘柄は結果に含まれない。"""
    if not codes:
        return {}
    return await asyncio.to_thread(_fetch_daily_sync, codes)


def _fetch_intraday_sync(code: str) -> pd.DataFrame | None:
    try:
        df = _with_retry(lambda: yf.Ticker(symbol(code)).history(period="5d", interval="5m"))
    except Exception:
        log.warning("5分足の取得に失敗: %s", code, exc_info=True)
        return None
    df = df[OHLCV].dropna(subset=["Close"])
    if df.empty:
        return None
    df.index = df.index.tz_convert(JST).tz_localize(None)
    # 夜間・休日でも直前の取引日を表示できるよう、最後の取引日の分だけを残す
    last_day = df.index[-1].date()
    return df[df.index.date == last_day]


async def fetch_intraday(code: str) -> pd.DataFrame | None:
    """直近の取引日の 5 分足。取得できなければ None。"""
    return await asyncio.to_thread(_fetch_intraday_sync, code)


def _lookup_listed_name_sync(code: str) -> str | None:
    ticker = yf.Ticker(symbol(code))
    if ticker.history(period="5d").empty:
        return None
    try:
        info = ticker.info
        return info.get("longName") or info.get("shortName") or code
    except Exception:
        return code


async def lookup_listed_name(code: str) -> str | None:
    """JPX の一覧に載る前の新規上場銘柄向け。直近 5 日の株価が取れれば上場中とみなして銘柄名を返す。
    取れなければ None。通信エラーやレート制限は例外のまま呼び出し元に返す。"""
    return await asyncio.to_thread(_lookup_listed_name_sync, code)


def resample(daily: pd.DataFrame, rule: str) -> pd.DataFrame:
    """日足を週足（'W-FRI'）・月足（'ME'）にまとめ直す。"""
    agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
    return daily.resample(rule).agg(agg).dropna(subset=["Close"])
