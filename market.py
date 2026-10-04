"""株価の取得と東証の取引日判定。

日足は DB（price_daily）にキャッシュする。初めて扱う銘柄だけ長期間をダウンロードし、
以後は直近数日分だけを取りに行って DB を更新する（yfinance の呼び出しとレート制限を抑えるため）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime, timedelta
from fractions import Fraction
from zoneinfo import ZoneInfo

import jpholiday
import pandas as pd
import yfinance as yf

import db

log = logging.getLogger(__name__)

JST = ZoneInfo("Asia/Tokyo")
OHLCV = ["Open", "High", "Low", "Close", "Volume"]
CHUNK_SIZE = 20
MAX_ATTEMPTS = 4
FULL_HISTORY = "5y"  # 初めて扱う銘柄のダウンロード期間
RECENT_OVERLAP_DAYS = 7  # 差分取得で、保存済みの最新日からさかのぼる日数
SPLIT_TOLERANCE = 0.005  # 保存済みの終値と 0.5% 以上ずれたら、株式分割などで過去が修正されたとみなす
SPLIT_RATIO_TOLERANCE = 0.03  # ずれの比が 1:2 などの分割比から 3% 以内なら、株式分割とみなす
INDEX_CODES = ("^N225", "1306")  # 日経平均と TOPIX 連動 ETF（TOPIX 指数は yfinance で取れないことが多いため）


def symbol(code: str) -> str:
    return code if code.startswith("^") else f"{code}.T"


def is_trading_day(d: date) -> bool:
    """土日・祝日・年末年始（12/31〜1/3）を除く。"""
    if d.weekday() >= 5 or jpholiday.is_holiday(d):
        return False
    return not ((d.month == 12 and d.day == 31) or (d.month == 1 and d.day <= 3))


def is_last_trading_day_of_week(d: date) -> bool:
    """その週（月〜金）で最後の取引日なら True。金曜が祝日の週は木曜などになる。"""
    if not is_trading_day(d) or d.weekday() > 4:
        return False
    return not any(is_trading_day(d + timedelta(days=k)) for k in range(1, 5 - d.weekday()))


def trading_days_between(start: date, end: date) -> int:
    """start の翌日から end までの取引日の数（保有日数・注文の期限・候補の期間を数えるのに使う）。"""
    days, d = 0, start
    while d < end:
        d += timedelta(days=1)
        if is_trading_day(d):
            days += 1
    return days


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


def _download_sync(codes: list[str], **period) -> dict[str, pd.DataFrame]:
    """codes の日足をまとめてダウンロードする。period は period="5y" か start=date(...)。取れなかった銘柄は含まれない。"""
    result: dict[str, pd.DataFrame] = {}
    for i in range(0, len(codes), CHUNK_SIZE):
        chunk = codes[i : i + CHUNK_SIZE]
        symbols = [symbol(c) for c in chunk]
        try:
            raw = _with_retry(
                lambda: yf.download(
                    symbols,
                    interval="1d",
                    group_by="ticker",
                    # 配当で過去の株価が毎回少しずつ変わると差分更新と合わないので、配当補正はしない（分割補正のみ）
                    auto_adjust=False,
                    progress=False,
                    threads=False,
                    **period,
                )
            )
        except Exception:
            log.exception("日足の取得に失敗: %s", chunk)
            continue
        frames = _split_by_symbol(raw, symbols)
        for code in chunk:
            if (df := frames.get(symbol(code))) is not None:
                result[code] = df
        if i + CHUNK_SIZE < len(codes):
            time.sleep(2)
    return result


def _to_rows(code: str, df: pd.DataFrame) -> list[db.PriceRow]:
    def num(v):
        return None if pd.isna(v) else float(v)

    return [
        (
            code,
            ts.date(),
            num(r["Open"]),
            num(r["High"]),
            num(r["Low"]),
            float(r["Close"]),
            None if pd.isna(r["Volume"]) else int(r["Volume"]),
        )
        for ts, r in df.iterrows()
    ]


def _was_restated(stored: dict[date, float], fetched: pd.DataFrame, last_stored: date) -> bool:
    """差分で取った終値が、保存済みの確定値と食い違っていれば True（株式分割で過去の株価が修正された場合など）。
    保存済みの最新日は場中の途中経過のことがあるので比べない。"""
    for ts, close in fetched["Close"].items():
        d = ts.date()
        if d < last_stored and d in stored and abs(close / stored[d] - 1) > SPLIT_TOLERANCE:
            return True
    return False


def _detect_split(stored: dict[date, float], fetched: pd.DataFrame) -> tuple[date, float] | None:
    """取り直す前と後の終値の比から、分割日（分割後の株価になった最初の日）と分割比を求める。

    分割比は「1 株が何株になるか」（取り直す前の終値 ÷ 取り直した後の終値）。
    1:2 や 2:3、2:1 の併合のような比にならない修正（データの訂正など）なら None。
    """
    closes = {ts.date(): float(c) for ts, c in fetched["Close"].items()}
    changed = [
        (d, stored[d] / closes[d])
        for d in sorted(stored)
        if closes.get(d) and abs(stored[d] / closes[d] - 1) > SPLIT_TOLERANCE
    ]
    if not changed:
        return None
    rates = sorted(r for _, r in changed)
    rate = rates[len(rates) // 2]
    ratio = Fraction(rate).limit_denominator(10)
    if ratio == 1 or abs(rate / float(ratio) - 1) > SPLIT_RATIO_TOLERANCE:
        return None
    later = [d for d in sorted(closes) if d > changed[-1][0]]
    if not later:
        return None
    return later[0], float(ratio)


def _download_groups(coverage: dict[str, date]) -> dict[date, list[str]]:
    """差分取得の開始日ごとに、まとめてダウンロードする銘柄を分ける。

    保存済みの最新日が新しい銘柄（最も新しい銘柄から RECENT_OVERLAP_DAYS 以内）は、まとめて同じ開始日から取る。
    それより古い銘柄（売買停止・上場廃止など）は、自分の最新日から別に取る。1 銘柄が古いだけで、
    全銘柄を何か月分も取り直すことにならないようにするため。
    """
    latest = max(coverage.values())
    overlap = timedelta(days=RECENT_OVERLAP_DAYS)
    fresh = [c for c, d in coverage.items() if d >= latest - overlap]
    groups: dict[date, list[str]] = {min(coverage[c] for c in fresh) - overlap: fresh}
    for c, d in coverage.items():
        if d < latest - overlap:
            groups.setdefault(d - overlap, []).append(c)
    return groups


async def get_daily(codes: list[str], years: int = 2, refresh: bool = True) -> dict[str, pd.DataFrame]:
    """codes の日足を、直近 years 年分返す。

    - DB にまだない銘柄は、5 年分をダウンロードして保存する
    - refresh=True なら、保存済みの銘柄も直近分をダウンロードして上書きする（場中なら「今日の足」も最新になる）
    - ダウンロードに失敗した銘柄は、DB にある分だけを返す（DB にもなければ結果に含まれない）
    """
    codes = list(dict.fromkeys(codes))
    if not codes:
        return {}
    coverage = await db.price_coverage(codes)
    missing = [c for c in codes if c not in coverage]
    stored_codes = [c for c in codes if c in coverage] if refresh else []

    if missing:
        full = await asyncio.to_thread(_download_sync, missing, period=FULL_HISTORY)
        for code, df in full.items():
            await db.upsert_prices(_to_rows(code, df))

    if stored_codes:
        recent: dict[str, pd.DataFrame] = {}
        groups = _download_groups({c: coverage[c] for c in stored_codes})
        for start, group in sorted(groups.items()):
            recent.update(await asyncio.to_thread(_download_sync, group, start=start))
        since = min(groups)
        stored: dict[str, dict[date, float]] = {}
        for r in await db.load_prices(list(recent), since):
            stored.setdefault(r["ticker"], {})[r["date"]] = r["close"]
        restated = [c for c, df in recent.items() if _was_restated(stored.get(c, {}), df, coverage[c])]
        for code, df in recent.items():
            if code not in restated:
                await db.upsert_prices(_to_rows(code, df))
        if restated:
            log.info("過去の株価が修正されていたため取り直します（株式分割など）: %s", restated)
            full = await asyncio.to_thread(_download_sync, restated, period=FULL_HISTORY)
            for code, df in full.items():
                # 先に保有などを直す。日足の入れ替えが失敗しても次回また検出され、分割は二重には反映されない
                if split := _detect_split(stored.get(code, {}), df):
                    ex_date, ratio = split
                    if (detail := await db.apply_split(code, ex_date, ratio)) is not None:
                        log.info("株式分割を反映しました: %s %s 1:%g %s", code, ex_date, ratio, detail)
                else:
                    log.warning("過去の株価が修正されましたが、分割比を判定できませんでした（保有は直しません）: %s", code)
                await db.replace_prices(code, _to_rows(code, df))

    since = now_jst().date() - timedelta(days=366 * years)
    frames: dict[str, list[dict]] = {}
    for r in await db.load_prices(codes, since):
        frames.setdefault(r["ticker"], []).append(r)
    return {code: _rows_to_frame(rows) for code, rows in frames.items()}


def _rows_to_frame(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    df.index = pd.DatetimeIndex(pd.to_datetime(df["date"]), name="Date")
    df = df.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"})
    return df[OHLCV].astype(float)


async def prune_old_prices(years: int = 6) -> int:
    """years 年より古い日足を DB から削除する。"""
    return await db.prune_prices(now_jst().date() - timedelta(days=366 * years))


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
