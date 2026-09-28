"""Google News RSS からのニュース収集。"""

from __future__ import annotations

import asyncio
import calendar
import html
import logging
import re
import time
from dataclasses import dataclass
from urllib.parse import quote

import aiohttp
import feedparser

log = logging.getLogger(__name__)

RSS_URL = "https://news.google.com/rss/search?q={query}&hl=ja&gl=JP&ceid=JP:ja"
QUERIES = (
    "株価 上昇",
    "上方修正",
    "最高益",
    "増配",
    "自社株買い",
    "業務提携",
    "受注 獲得",
    "新製品 発表 株",
)
MAX_AGE_SECONDS = 24 * 3600


@dataclass(frozen=True)
class NewsItem:
    title: str
    url: str
    summary: str
    source: str


def _clean_html(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", text or "")).strip()


def _parse_feed(content: bytes, max_age: int = MAX_AGE_SECONDS) -> list[NewsItem]:
    now = time.time()
    items = []
    for entry in feedparser.parse(content).entries:
        published = entry.get("published_parsed")
        if published and now - calendar.timegm(published) > max_age:
            continue
        # Google News のタイトルは「見出し - 媒体名」形式
        title, _, source = entry.get("title", "").rpartition(" - ")
        if not title:
            title, source = entry.get("title", ""), ""
        items.append(NewsItem(title.strip(), entry.get("link", ""), _clean_html(entry.get("summary", "")), source))
    return items


async def _fetch_one(session: aiohttp.ClientSession, query: str) -> list[NewsItem]:
    url = RSS_URL.format(query=quote(f"{query} when:1d"))
    try:
        async with session.get(url) as resp:
            resp.raise_for_status()
            content = await resp.read()
    except Exception:
        log.warning("RSS 取得に失敗: %s", query, exc_info=True)
        return []
    return await asyncio.to_thread(_parse_feed, content)


async def fetch_news() -> list[NewsItem]:
    """全クエリの記事を集め、URL と見出しで重複を除いて返す。"""
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": "Mozilla/5.0"}) as session:
        results = await asyncio.gather(*(_fetch_one(session, q) for q in QUERIES))
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    unique = []
    for item in (i for batch in results for i in batch):
        if not item.url or item.url in seen_urls or item.title in seen_titles:
            continue
        seen_urls.add(item.url)
        seen_titles.add(item.title)
        unique.append(item)
    log.info("ニュース %d 件を取得", len(unique))
    return unique


async def search_company(name: str, days: int = 2, limit: int = 8) -> list[NewsItem]:
    """社名で直近 days 日のニュースを検索する（「なぜ動いた？」ボタン用）。取得に失敗したら例外を返す。"""
    url = RSS_URL.format(query=quote(f'"{name}" when:{days}d'))
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": "Mozilla/5.0"}) as session:
        async with session.get(url) as resp:
            resp.raise_for_status()
            content = await resp.read()
    items = await asyncio.to_thread(_parse_feed, content, days * 24 * 3600)
    seen, unique = set(), []
    for item in items:
        if item.title not in seen:
            seen.add(item.title)
            unique.append(item)
    return unique[:limit]
