"""JPX 上場銘柄一覧の取得・キャッシュと、会社名照合・証券コード検証。

起動時は DB の ticker_master テーブルからメモリに読み込むだけにする。
キャッシュが 7 日より古いときだけ JPX から再ダウンロードし、失敗したら古いキャッシュを使い続ける。
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

import aiohttp
import pandas as pd

import db

log = logging.getLogger(__name__)

JPX_PAGE_URL = "https://www.jpx.co.jp/markets/statistics-equities/misc/01.html"
USER_AGENT = "Mozilla/5.0 (compatible; stock-signal-bot/1.0)"
# ETF・REIT・PRO Market などを除き、一般の株式だけを対象にする
TARGET_MARKET = re.compile(r"(プライム|スタンダード|グロース)（(内国|外国)株式）")
# 会社名の末尾から外して別名（略称）を作る語
NAME_SUFFIXES = ("ホールディングス", "グループ本社", "グループ", "HD", "株式会社")
MIN_ALIAS_LEN = 3
STALE_AFTER = timedelta(days=7)
RETRY_AFTER = timedelta(days=1)
MIN_KEEP_RATIO = 0.8
CODE_PATTERN = re.compile(r"[0-9][0-9A-Z]{2}[0-9A-Z]")  # 4 桁、または 130A のような英数字コード


@dataclass(frozen=True)
class StockInfo:
    code: str
    name: str
    market: str
    sector: str


def normalize(text: str) -> str:
    """全角英数を半角に揃え、空白を除く（JPX の銘柄名は全角表記のため）。"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


def normalize_code(code: str) -> str:
    code = normalize(code).upper()
    return code.removesuffix(".T")


class TickerMaster:
    def __init__(self) -> None:
        self._stocks: dict[str, StockInfo] = {}
        self._aliases: list[tuple[str, str]] = []  # (正規化した名前, code)、長い順
        self.fetched_at: datetime | None = None

    def __len__(self) -> int:
        return len(self._stocks)

    async def load(self) -> set[str]:
        """銘柄一覧を読み込み、前回の一覧から消えた（上場廃止になった）証券コードを返す。"""
        rows, fetched_at = await db.load_ticker_master()
        delisted: set[str] = set()
        if db.is_stale(fetched_at):
            try:
                new_rows = await self._download()
                # ファイル形式の変更などで件数が急減したときに、全銘柄を上場廃止扱いにしないための安全策
                if rows and len(new_rows) < len(rows) * MIN_KEEP_RATIO:
                    raise RuntimeError(f"銘柄数が急減しました ({len(rows)} → {len(new_rows)} 件)")
                if rows:
                    delisted = {r["code"] for r in rows} - {r["code"] for r in new_rows}
                rows = new_rows
                fetched_at = await db.replace_ticker_master(rows)
                log.info("JPX 銘柄一覧をダウンロードしてキャッシュしました (%d 件、上場廃止 %d 件)", len(rows), len(delisted))
            except Exception:
                if not rows:
                    raise
                log.exception("JPX 銘柄一覧の更新に失敗したため、古いキャッシュを使います（1日後に再試行）")
                fetched_at = datetime.now(timezone.utc) - STALE_AFTER + RETRY_AFTER
        else:
            log.info("JPX 銘柄一覧を DB キャッシュから読み込みました (%d 件)", len(rows))
        self._build(rows)
        self.fetched_at = fetched_at
        return delisted

    def is_stale(self) -> bool:
        return db.is_stale(self.fetched_at)

    def get(self, code: str) -> StockInfo | None:
        return self._stocks.get(normalize_code(code))

    def find_in_text(self, text: str) -> list[StockInfo]:
        """文章中に現れる会社名を探す。長い名前を優先し、重なった位置の短い名前は無視する。"""
        text = normalize(text)
        used: list[tuple[int, int]] = []
        found: dict[str, StockInfo] = {}
        for alias, code in self._aliases:
            start = text.find(alias)
            while start != -1:
                end = start + len(alias)
                if _on_word_boundary(text, alias, start, end) and not any(s < end and start < e for s, e in used):
                    used.append((start, end))
                    found.setdefault(code, self._stocks[code])
                start = text.find(alias, end)
        return list(found.values())

    def _build(self, rows: list[dict]) -> None:
        self._stocks = {
            r["code"]: StockInfo(r["code"], normalize(r["name"]), r["market"] or "", r["sector"] or "") for r in rows
        }
        aliases: dict[str, str] = {}
        for info in self._stocks.values():
            aliases.setdefault(info.name, info.code)
            for suffix in NAME_SUFFIXES:
                short = info.name.removesuffix(suffix)
                if short != info.name and len(short) >= MIN_ALIAS_LEN:
                    aliases.setdefault(short, info.code)
        self._aliases = sorted(aliases.items(), key=lambda kv: len(kv[0]), reverse=True)

    async def _download(self) -> list[dict]:
        headers = {"User-Agent": USER_AGENT}
        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
            async with session.get(JPX_PAGE_URL) as resp:
                resp.raise_for_status()
                page = await resp.text()
            # ファイル名や拡張子（xls→xlsx）が変わっても追従できるよう、ページからリンクを探す
            match = re.search(r'href="([^"]*data_j\.xlsx?)"', page)
            if not match:
                raise RuntimeError("JPX のページに銘柄一覧ファイルへのリンクが見つかりません")
            async with session.get(urljoin(JPX_PAGE_URL, match.group(1))) as resp:
                resp.raise_for_status()
                content = await resp.read()
        return await asyncio.to_thread(_parse_listing, content)


def _char_class(ch: str) -> str | None:
    if "ァ" <= ch <= "ヺ" or ch == "ー":
        return "katakana"
    if ch.isascii() and ch.isalnum():
        return "ascii"
    return None


def _on_word_boundary(text: str, alias: str, start: int, end: int) -> bool:
    """カタカナ・英字の社名が別の単語の一部に一致するのを防ぐ（例: 「エン」と「エンジン」、「パス」と「ブライトパス」）。"""
    head, tail = _char_class(alias[0]), _char_class(alias[-1])
    if head and start > 0 and _char_class(text[start - 1]) == head:
        return False
    if tail and end < len(text) and _char_class(text[end]) == tail:
        return False
    return True


def _parse_listing(content: bytes) -> list[dict]:
    df = pd.read_excel(io.BytesIO(content), dtype=str)
    df = df[df["市場・商品区分"].str.match(TARGET_MARKET, na=False)]
    return [
        {
            "code": normalize_code(row["コード"]),
            "name": normalize(row["銘柄名"]),
            "market": row["市場・商品区分"],
            "sector": row["33業種区分"],
        }
        for _, row in df.iterrows()
    ]
