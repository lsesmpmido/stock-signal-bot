"""Jev (TypeSafe AI) によるニュース判定。

Jev は分類・判定専用のモデルで、文章から銘柄を抜き出すことはできない。
銘柄の特定は ticker_master で済ませ、ここでは「その銘柄にとってプラス材料か」だけを判定する。

単体で動作確認するとき:
    python jev_client.py "トヨタ、通期業績予想を上方修正" "トヨタ自動車"
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass

from typesafe_sdk import AsyncTypeSafeClient, Noul, RetryPolicy, Score

IMPACT_LEVELS = [
    "軽微: 株価への影響はほとんどない",
    "中程度: 株価がある程度動く可能性がある",
    "大きい: 業績や企業価値を大きく変え、株価が大きく動く可能性が高い",
]


@dataclass(frozen=True)
class Judgement:
    is_positive: float  # 0〜1（1 に近いほどプラス材料）
    impact: float  # 0〜2（IMPACT_LEVELS の番号に対応する期待値）


class JevJudge:
    def __init__(self, api_key: str | None = None) -> None:
        # 429 / 5xx は SDK が指数バックオフでリトライする
        self._client = AsyncTypeSafeClient(
            api_key=api_key or os.environ["JEV_API_KEY"],
            retry=RetryPolicy(max_retries=4),
            timeout=15.0,
        )

    async def judge(self, title: str, summary: str, company: str, relation: str | None = None) -> Judgement:
        """relation を渡すと、ニュースの当事者ではない関連企業として判定する（例: 「○○は△△の主要販売先」）。"""
        state = f"対象企業: {company}\nニュース見出し: {title}"
        if summary and summary != title:
            state += f"\n概要: {summary}"
        if relation:
            state += f"\n対象企業とニュースの関係: {relation}"
        result = await self._client.system_one(
            state=state,
            questions={
                "is_positive": Noul(
                    instructions=f"このニュースは「{company}」の株価にとってプラス材料ですか？",
                    criteria={
                        "true": "業績向上・増配・提携・受注など、株価の上昇要因になる内容",
                        "false": "悪材料、または株価と関係のない内容",
                    },
                ),
                "impact": Score(
                    instructions=f"このニュースが「{company}」の株価に与える影響の大きさは？",
                    criteria=IMPACT_LEVELS,
                ),
            },
        )
        return Judgement(result.nouls["is_positive"].noul, result.scores["impact"].score)

    async def should_buy(self, state: dict) -> float:
        """AI トレーダー用: この銘柄を翌取引日の寄り付きで買うべきかの確信度（0〜1）。"""
        result = await self._client.system_one(
            state=state,
            questions={
                "buy": Noul(
                    instructions=(
                        "あなたは日本株を数週間〜数か月の期間で運用する個人投資家です。"
                        "この銘柄を翌取引日の寄り付きで買うべきですか？ ニュースで急騰した直後の高値づかみは避け、"
                        "材料の強さと、RSI・移動平均からの乖離・MACD などの値動きの状態のバランスで判断してください。"
                    ),
                    criteria={
                        "true": "材料と値動きの両面から、今が買いのタイミング",
                        "false": "割高・材料が弱い・トレンドが悪いなどの理由で、見送るか待つべき",
                    },
                )
            },
        )
        return result.nouls["buy"].noul

    async def should_sell(self, state: dict) -> float:
        """AI トレーダー用: 保有中のこの銘柄を翌取引日の寄り付きで売るべきかの確信度（0〜1）。"""
        result = await self._client.system_one(
            state=state,
            questions={
                "sell": Noul(
                    instructions=(
                        "あなたは日本株を数週間〜数か月の期間で運用する個人投資家です。"
                        "保有中のこの銘柄を翌取引日の寄り付きで売るべきですか？ 損益・保有期間・値動きの状態から判断してください。"
                    ),
                    criteria={
                        "true": "利益確定・トレンドの悪化・買われすぎなどの理由で、売るべき",
                        "false": "上昇の余地があり、持ち続けるべき",
                    },
                )
            },
        )
        return result.nouls["sell"].noul

    async def aclose(self) -> None:
        await self._client.aclose()


async def _main() -> None:
    from dotenv import load_dotenv

    load_dotenv()
    if len(sys.argv) < 3:
        sys.exit('使い方: python jev_client.py "ニュース見出し" "企業名"')
    judge = JevJudge()
    try:
        j = await judge.judge(sys.argv[1], "", sys.argv[2])
        print(f"is_positive={j.is_positive:.2f}  impact={j.impact:.2f}")
    finally:
        await judge.aclose()


if __name__ == "__main__":
    asyncio.run(_main())
