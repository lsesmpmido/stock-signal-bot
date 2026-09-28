"""上場企業同士の関係データ（JP Market Vis）の取得・キャッシュと、関連企業の検索。

データ: https://github.com/mattyamonaca/JP_Market_Vis の public/M5_company_relations.json
（有価証券報告書〔EDINET〕や企業の公式サイトから抽出された関係。自動抽出のため誤りを含むことがある）

ファイルは 40MB 超あり、まとめて読み込むとメモリを 300MB 以上使うため、
ストリーミングで読みながら「上場企業同士」「確定済み」「業績に直結する種類」の関係だけを取り出し、DB にキャッシュする。
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import aiohttp
import ijson

import db

log = logging.getLogger(__name__)

RELATIONS_URL = "https://raw.githubusercontent.com/mattyamonaca/JP_Market_Vis/HEAD/public/M5_company_relations.json"
STALE_AFTER = timedelta(days=7)
RETRY_AFTER = timedelta(days=1)
MIN_KEEP_RATIO = 0.8

# 関係の種類ごとの優先度（小さいほど業績への影響が大きい）と、
# 「関連企業 B は、ニュースの企業 A の◯◯」という説明。
# (A が source のときの説明, A が target のときの説明)。向きのない関係は同じ文言にする。
# 役員兼任・株式保有・取引銀行は業績とのつながりが弱いため使わない。
RELATION_TYPES: dict[str, tuple[int, str, str]] = {
    "major_customer": (1, "主要販売先", "主要な納入元（{a}が主要顧客）"),
    "major_supplier": (1, "主要仕入先", "主要な納入先（{a}から仕入れている）"),
    "parent_subsidiary": (1, "子会社", "親会社"),
    "capital_alliance": (2, "資本業務提携先", "資本業務提携先"),
    "affiliate": (2, "関連会社", "出資元（{a}を関連会社としている）"),
    "joint_venture": (2, "合弁会社", "合弁の出資者"),
    "business_alliance": (3, "業務提携先", "業務提携先"),
    "technology_license": (3, "技術供与先", "技術供与元"),
    "joint_research": (3, "共同研究先", "共同研究先"),
    "transaction_partner": (4, "取引先", "取引先"),
    "product_adoption": (4, "製品の導入企業", "導入している製品の提供企業"),
}


@dataclass(frozen=True)
class Related:
    code: str
    relation_type: str
    priority: int
    label: str  # 「B は A の {label}」


class RelationGraph:
    def __init__(self) -> None:
        self._edges: dict[str, list[tuple[str, str, bool]]] = {}  # code -> [(相手, 種類, code が source か)]
        self.fetched_at: datetime | None = None

    def __len__(self) -> int:
        return sum(len(v) for v in self._edges.values()) // 2

    async def load(self) -> None:
        rows, fetched_at = await db.load_relations()
        if db.is_stale(fetched_at):
            try:
                new_rows = await self._download()
                # 形式の変更などで件数が急減したら、更新せず古いキャッシュを使う
                if rows and len(new_rows) < len(rows) * MIN_KEEP_RATIO:
                    raise RuntimeError(f"関係データの件数が急減しました ({len(rows)} → {len(new_rows)} 件)")
                rows = new_rows
                fetched_at = await db.replace_relations(rows)
                log.info("関係データをダウンロードしてキャッシュしました (%d 件)", len(rows))
            except Exception:
                log.exception("関係データの更新に失敗しました（1日後に再試行）")
                fetched_at = datetime.now(timezone.utc) - STALE_AFTER + RETRY_AFTER
        else:
            log.info("関係データを DB キャッシュから読み込みました (%d 件)", len(rows))
        self._build(rows)
        self.fetched_at = fetched_at

    def is_stale(self) -> bool:
        return db.is_stale(self.fetched_at)

    def neighbors(self, code: str, company_name: str) -> list[Related]:
        """code の関連企業を、業績への影響が大きい関係から順に返す。"""
        best: dict[str, Related] = {}
        for other, rtype, is_source in self._edges.get(code, []):
            priority, as_source, as_target = RELATION_TYPES[rtype]
            label = (as_source if is_source else as_target).format(a=company_name)
            current = best.get(other)
            if current is None or priority < current.priority:
                best[other] = Related(other, rtype, priority, label)
        return sorted(best.values(), key=lambda r: (r.priority, r.code))

    def pairs(self) -> list[tuple[str, str]]:
        """関係のある企業の組（source, target）を、重複なく返す。"""
        return sorted({(a, b) if a < b else (b, a) for a, others in self._edges.items() for b, _, _ in others})

    def _build(self, rows: list[dict]) -> None:
        edges: dict[str, list[tuple[str, str, bool]]] = {}
        for r in rows:
            edges.setdefault(r["source"], []).append((r["target"], r["relation_type"], True))
            edges.setdefault(r["target"], []).append((r["source"], r["relation_type"], False))
        self._edges = edges

    async def _download(self) -> list[dict]:
        timeout = aiohttp.ClientTimeout(total=300)
        with tempfile.TemporaryFile() as f:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(RELATIONS_URL) as resp:
                    resp.raise_for_status()
                    async for chunk in resp.content.iter_chunked(1 << 16):
                        f.write(chunk)
            f.seek(0)
            return await asyncio.to_thread(_parse_relations, f)


def _parse_relations(f) -> list[dict]:
    seen: set[tuple[str, str, str]] = set()
    rows = []
    for rel in ijson.items(f, "relations.item"):
        src, tgt = rel["source"], rel["target"]
        rtype = rel["relation_type"]
        if src["type"] != "listed" or tgt["type"] != "listed" or src["key"] == tgt["key"]:
            continue
        if rel["status"] != "confirmed" or rtype not in RELATION_TYPES:
            continue
        key = (src["key"], tgt["key"], rtype)
        if key not in seen:
            seen.add(key)
            rows.append({"source": key[0], "target": key[1], "relation_type": rtype})
    return rows
