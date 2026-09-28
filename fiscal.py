"""決算日（EDINET コードリスト）から、配当・株主優待の権利付き最終日を計算する。

EDINET コードリスト（金融庁、API キー不要）の「決算日」を使う。権利確定日は期末（決算日）と中間（その 6 か月前）とし、
権利付き最終日は権利確定日の 2 営業日前（受け渡しが 2 営業日後のため）。
配当や株主優待を実際に出すか、中間配当があるかは会社ごとに違うので、お知らせでは「権利確定日の目安」として扱う。
"""

from __future__ import annotations

import asyncio
import calendar
import csv
import io
import logging
import re
import zipfile
from datetime import date, timedelta

import aiohttp

import db
import market
from ticker_master import normalize_code

log = logging.getLogger(__name__)

EDINET_CODELIST_URL = "https://disclosure2dl.edinet-fsa.go.jp/searchdocument/codelist/Edinetcode.zip"
FISCAL_END = re.compile(r"(\d{1,2})月(\d{1,2})日")


async def download() -> dict[str, str]:
    """上場企業の {証券コード: 決算日（例: "3月31日"）}。"""
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": "Mozilla/5.0"}) as session:
        async with session.get(EDINET_CODELIST_URL) as resp:
            resp.raise_for_status()
            content = await resp.read()
    return await asyncio.to_thread(_parse, content)


def _parse(content: bytes) -> dict[str, str]:
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        text = z.read(z.namelist()[0]).decode("cp932")
    lines = text.splitlines()[1:]  # 1 行目はダウンロード日と件数
    result = {}
    for row in csv.DictReader(lines):
        code, end = row.get("証券コード", "").strip(), row.get("決算日", "").strip()
        if row.get("上場区分") != "上場" or len(code) != 5 or not FISCAL_END.fullmatch(end):
            continue
        result[normalize_code(code[:4])] = end  # 証券コードは 5 桁（末尾に 0）で載っている
    return result


def _month_day(year: int, month: int, day: int, month_end: bool) -> date:
    last = calendar.monthrange(year, month)[1]
    return date(year, month, last if month_end else min(day, last))


def record_dates(fiscal_end: str, since: date, until: date) -> list[tuple[str, date]]:
    """since〜until の、期末・中間の権利確定日（(「期末」か「中間」, 日付) の古い順）。"""
    m = FISCAL_END.fullmatch(fiscal_end)
    if not m:
        return []
    month, day = int(m[1]), int(m[2])
    month_end = day == calendar.monthrange(2001, month)[1] or (month == 2 and day >= 28)
    half = (month + 5) % 12 + 1  # 6 か月前の月
    dates = []
    for year in range(since.year - 1, until.year + 2):
        for label, mon in (("期末", month), ("中間", half)):
            d = _month_day(year, mon, day, month_end)
            if since <= d <= until:
                dates.append((label, d))
    return sorted(dates, key=lambda x: x[1])


def last_cum_date(record: date) -> date:
    """権利付き最終日: 権利確定日（休みならその前の取引日）から、さらに 2 営業日前の取引日。"""
    d = record
    while not market.is_trading_day(d):
        d -= timedelta(days=1)
    for _ in range(2):
        d -= timedelta(days=1)
        while not market.is_trading_day(d):
            d -= timedelta(days=1)
    return d


async def upcoming(codes: list[str], today: date, within: int = 10) -> list[tuple[str, str, date, date, int]]:
    """codes のうち、権利付き最終日が今日から within 営業日以内のもの。

    (証券コード, 「期末」か「中間」, 権利確定日, 権利付き最終日, あと何営業日) の、権利付き最終日の近い順。
    """
    ends = await db.load_fiscal_ends(codes)
    result = []
    for code in codes:
        if code not in ends:
            continue
        for label, record in record_dates(ends[code], today, today + timedelta(days=within * 2 + 10)):
            cum = last_cum_date(record)
            left = market.trading_days_between(today, cum)
            if cum >= today and left <= within:
                result.append((code, label, record, cum, left))
    return sorted(result, key=lambda r: r[3])
