"""PostgreSQL 接続・テーブル自動作成・データアクセス。

DATABASE_URL の接続文字列で psycopg から接続し、テーブルは起動時に自動で作成する。
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any

from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

log = logging.getLogger(__name__)

DEFAULT_SETTINGS: dict[str, str] = {
    "proposal_freq": "twice",  # twice / once / off
    "signal_freq": "1h",  # 15m / 1h / close / off
    "jev_positive_threshold": "0.7",
    "jev_impact_threshold": "1.0",
    # 仮想売買
    "vp_cash": "2400000",  # 現金残高（円）。元手は新NISA 成長投資枠の年間上限と同じ 240 万円
    "vp_fee_rate": "0",  # 売買手数料（売買代金に対する割合。例: 0.0022 = 0.22%）
    "vp_default_amount": "200000",  # 金額を省略したときの購入額（円）
}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS pending_stocks (
    id           BIGSERIAL PRIMARY KEY,
    ticker       TEXT NOT NULL,
    company_name TEXT NOT NULL,
    news_title   TEXT NOT NULL,
    news_url     TEXT NOT NULL,
    score        DOUBLE PRECISION,
    impact       DOUBLE PRECISION,
    status       TEXT NOT NULL DEFAULT 'pending',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (ticker, news_url)
);
CREATE TABLE IF NOT EXISTS monitored_stocks (
    ticker           TEXT PRIMARY KEY,
    company_name     TEXT NOT NULL,
    source           TEXT NOT NULL DEFAULT 'manual',
    added_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_notified_at TIMESTAMPTZ,
    last_signal      TEXT,
    signal_state     JSONB
);
CREATE TABLE IF NOT EXISTS user_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ticker_master (
    code       TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    market     TEXT,
    sector     TEXT,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS company_relations (
    source        TEXT NOT NULL,
    target        TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    fetched_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (source, target, relation_type)
);
CREATE TABLE IF NOT EXISTS vp_positions (
    account      TEXT NOT NULL,  -- 'nisa' / 'tokutei'
    ticker       TEXT NOT NULL,
    company_name TEXT NOT NULL,
    shares       INTEGER NOT NULL,
    cost         DOUBLE PRECISION NOT NULL,  -- 取得費の合計（購入代金＋手数料）
    opened_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account, ticker)
);
CREATE TABLE IF NOT EXISTS vp_trades (
    id           BIGSERIAL PRIMARY KEY,
    account      TEXT NOT NULL,
    ticker       TEXT NOT NULL,
    company_name TEXT NOT NULL,
    side         TEXT NOT NULL,  -- 'buy' / 'sell'
    shares       INTEGER NOT NULL,
    price        DOUBLE PRECISION NOT NULL,
    amount       DOUBLE PRECISION NOT NULL,  -- 売買代金（株価×株数）
    fee          DOUBLE PRECISION NOT NULL DEFAULT 0,
    realized     DOUBLE PRECISION,  -- 売却時の損益（税引前、手数料込み）
    tax          DOUBLE PRECISION NOT NULL DEFAULT 0,  -- 源泉徴収額（マイナスは還付）
    traded_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS price_daily (
    ticker TEXT NOT NULL,
    date   DATE NOT NULL,
    open   DOUBLE PRECISION,
    high   DOUBLE PRECISION,
    low    DOUBLE PRECISION,
    close  DOUBLE PRECISION NOT NULL,
    volume BIGINT,
    PRIMARY KEY (ticker, date)
);
-- テーブルを REST API などで外部公開するサービスでも第三者に読み書きされないよう、RLS を有効化しておく。
-- (Bot はテーブル所有者のロールで接続するので RLS の影響を受けない)
ALTER TABLE pending_stocks   ENABLE ROW LEVEL SECURITY;
ALTER TABLE monitored_stocks ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_settings    ENABLE ROW LEVEL SECURITY;
ALTER TABLE ticker_master    ENABLE ROW LEVEL SECURITY;
ALTER TABLE company_relations ENABLE ROW LEVEL SECURITY;
ALTER TABLE price_daily      ENABLE ROW LEVEL SECURITY;
ALTER TABLE vp_positions     ENABLE ROW LEVEL SECURITY;
ALTER TABLE vp_trades        ENABLE ROW LEVEL SECURITY;
"""

_pool: AsyncConnectionPool | None = None


def _pool_or_raise() -> AsyncConnectionPool:
    if _pool is None:
        raise RuntimeError("db.init() が呼ばれていません")
    return _pool


async def init() -> None:
    """接続プールを開き、テーブルを自動作成して設定の既定値を投入する。"""
    global _pool
    params = conninfo_to_dict(os.environ["DATABASE_URL"])
    params.setdefault("sslmode", "prefer")  # SSL が使えれば使う（ローカルの SSL なし PostgreSQL にもつながる）
    _pool = AsyncConnectionPool(
        make_conninfo(**params),
        min_size=1,
        max_size=4,
        # トランザクションモードのコネクションプーラー経由でも動くよう、プリペアドステートメントを使わない
        kwargs={"autocommit": True, "prepare_threshold": None, "row_factory": dict_row},
        check=AsyncConnectionPool.check_connection,
        open=False,
    )
    await _pool.open(wait=True, timeout=30)
    async with _pool.connection() as conn:
        await conn.execute(SCHEMA_SQL)
        for key, value in {**DEFAULT_SETTINGS, "vp_started_at": datetime.now(timezone.utc).isoformat()}.items():
            await conn.execute(
                "INSERT INTO user_settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING",
                (key, value),
            )
    log.info("DB 初期化完了")


async def close() -> None:
    if _pool is not None:
        await _pool.close()


# ---------------------------------------------------------------- user_settings


async def get_all_settings() -> dict[str, str]:
    async with _pool_or_raise().connection() as conn:
        rows = await (await conn.execute("SELECT key, value FROM user_settings")).fetchall()
    return {**DEFAULT_SETTINGS, **{r["key"]: r["value"] for r in rows}}


async def set_setting(key: str, value: str) -> None:
    async with _pool_or_raise().connection() as conn:
        await conn.execute(
            "INSERT INTO user_settings (key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (key, value),
        )


# ---------------------------------------------------------------- pending_stocks


async def recently_proposed_tickers(days: int = 3) -> set[str]:
    """直近 days 日以内に提案済み（承認・スキップ含む）の銘柄。重複提案の抑止に使う。"""
    async with _pool_or_raise().connection() as conn:
        rows = await (
            await conn.execute(
                "SELECT DISTINCT ticker FROM pending_stocks WHERE created_at > now() - make_interval(days => %s)",
                (days,),
            )
        ).fetchall()
    return {r["ticker"] for r in rows}


async def add_pending(
    ticker: str, company_name: str, news_title: str, news_url: str, score: float, impact: float
) -> int | None:
    """提案を保存して id を返す。同じ銘柄×同じ記事が既にあれば None。"""
    async with _pool_or_raise().connection() as conn:
        row = await (
            await conn.execute(
                "INSERT INTO pending_stocks (ticker, company_name, news_title, news_url, score, impact) "
                "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (ticker, news_url) DO NOTHING RETURNING id",
                (ticker, company_name, news_title, news_url, score, impact),
            )
        ).fetchone()
    return row["id"] if row else None


async def get_pending(pending_id: int) -> dict[str, Any] | None:
    async with _pool_or_raise().connection() as conn:
        return await (await conn.execute("SELECT * FROM pending_stocks WHERE id = %s", (pending_id,))).fetchone()


async def set_pending_status(pending_id: int, status: str) -> None:
    async with _pool_or_raise().connection() as conn:
        await conn.execute("UPDATE pending_stocks SET status = %s WHERE id = %s", (status, pending_id))


# ---------------------------------------------------------------- monitored_stocks


async def add_monitored(ticker: str, company_name: str, source: str) -> bool:
    """監視対象に追加。既に登録済みなら False。"""
    async with _pool_or_raise().connection() as conn:
        row = await (
            await conn.execute(
                "INSERT INTO monitored_stocks (ticker, company_name, source) VALUES (%s, %s, %s) "
                "ON CONFLICT (ticker) DO NOTHING RETURNING ticker",
                (ticker, company_name, source),
            )
        ).fetchone()
    return row is not None


async def remove_monitored(ticker: str) -> dict[str, Any] | None:
    """監視解除。解除した行を返す（未登録なら None）。"""
    async with _pool_or_raise().connection() as conn:
        return await (
            await conn.execute("DELETE FROM monitored_stocks WHERE ticker = %s RETURNING *", (ticker,))
        ).fetchone()


async def list_monitored() -> list[dict[str, Any]]:
    async with _pool_or_raise().connection() as conn:
        return await (await conn.execute("SELECT * FROM monitored_stocks ORDER BY added_at")).fetchall()


async def save_signal_state(ticker: str, signal_state: dict[str, Any], last_signal: str | None = None) -> None:
    """シグナル判定の状態を保存。last_signal を渡したときは通知したものとして通知日時も更新する。"""
    async with _pool_or_raise().connection() as conn:
        if last_signal is None:
            await conn.execute(
                "UPDATE monitored_stocks SET signal_state = %s WHERE ticker = %s",
                (Jsonb(signal_state), ticker),
            )
        else:
            await conn.execute(
                "UPDATE monitored_stocks SET signal_state = %s, last_signal = %s, last_notified_at = now() "
                "WHERE ticker = %s",
                (Jsonb(signal_state), last_signal, ticker),
            )


# ---------------------------------------------------------------- ticker_master


async def load_ticker_master() -> tuple[list[dict[str, Any]], datetime | None]:
    """キャッシュ済みの銘柄一覧と、その取得日時を返す。"""
    async with _pool_or_raise().connection() as conn:
        rows = await (await conn.execute("SELECT code, name, market, sector, fetched_at FROM ticker_master")).fetchall()
    fetched_at = min((r["fetched_at"] for r in rows), default=None)
    return rows, fetched_at


async def replace_ticker_master(rows: list[dict[str, Any]]) -> datetime:
    """銘柄一覧をまるごと入れ替え、取得日時を返す。"""
    now = datetime.now(timezone.utc)
    async with _pool_or_raise().connection() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM ticker_master")
            async with conn.cursor() as cur:
                await cur.executemany(
                    "INSERT INTO ticker_master (code, name, market, sector, fetched_at) VALUES (%s, %s, %s, %s, %s)",
                    [(r["code"], r["name"], r["market"], r["sector"], now) for r in rows],
                )
    return now


def is_stale(fetched_at: datetime | None, days: int = 7) -> bool:
    return fetched_at is None or datetime.now(timezone.utc) - fetched_at > timedelta(days=days)


# ---------------------------------------------------------------- company_relations


async def load_relations() -> tuple[list[dict[str, Any]], datetime | None]:
    """キャッシュ済みの関係データと、その取得日時を返す。"""
    async with _pool_or_raise().connection() as conn:
        rows = await (
            await conn.execute("SELECT source, target, relation_type, fetched_at FROM company_relations")
        ).fetchall()
    fetched_at = min((r["fetched_at"] for r in rows), default=None)
    return rows, fetched_at


async def replace_relations(rows: list[dict[str, Any]]) -> datetime:
    """関係データをまるごと入れ替え、取得日時を返す。"""
    now = datetime.now(timezone.utc)
    async with _pool_or_raise().connection() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM company_relations")
            async with conn.cursor() as cur:
                await cur.executemany(
                    "INSERT INTO company_relations (source, target, relation_type, fetched_at) VALUES (%s, %s, %s, %s)",
                    [(r["source"], r["target"], r["relation_type"], now) for r in rows],
                )
    return now


# ---------------------------------------------------------------- price_daily

PriceRow = tuple[str, date, float | None, float | None, float | None, float, int | None]


async def price_coverage(tickers: list[str]) -> dict[str, date]:
    """銘柄ごとの、保存済みの最新日付。1 件も保存していない銘柄は含まれない。"""
    async with _pool_or_raise().connection() as conn:
        rows = await (
            await conn.execute(
                "SELECT ticker, max(date) AS last FROM price_daily WHERE ticker = ANY(%s) GROUP BY ticker",
                (tickers,),
            )
        ).fetchall()
    return {r["ticker"]: r["last"] for r in rows}


async def load_prices(tickers: list[str], since: date) -> list[dict[str, Any]]:
    async with _pool_or_raise().connection() as conn:
        return await (
            await conn.execute(
                "SELECT ticker, date, open, high, low, close, volume FROM price_daily "
                "WHERE ticker = ANY(%s) AND date >= %s ORDER BY ticker, date",
                (tickers, since),
            )
        ).fetchall()


async def upsert_prices(rows: list[PriceRow]) -> None:
    """日足を保存する。同じ銘柄・日付があれば上書きする（場中の途中経過の足を、最新値や確定値で置き換える）。"""
    if not rows:
        return
    async with _pool_or_raise().connection() as conn:
        async with conn.cursor() as cur:
            await cur.executemany(
                "INSERT INTO price_daily (ticker, date, open, high, low, close, volume) VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (ticker, date) DO UPDATE SET open = EXCLUDED.open, high = EXCLUDED.high, "
                "low = EXCLUDED.low, close = EXCLUDED.close, volume = EXCLUDED.volume",
                rows,
            )


async def replace_prices(ticker: str, rows: list[PriceRow]) -> None:
    """銘柄の日足をまるごと入れ替える（株式分割で過去の株価が修正されたとき用）。"""
    async with _pool_or_raise().connection() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM price_daily WHERE ticker = %s", (ticker,))
            async with conn.cursor() as cur:
                await cur.executemany(
                    "INSERT INTO price_daily (ticker, date, open, high, low, close, volume) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    rows,
                )


async def prune_prices(before: date) -> int:
    """指定日より古い日足を削除し、削除した行数を返す。"""
    async with _pool_or_raise().connection() as conn:
        cur = await conn.execute("DELETE FROM price_daily WHERE date < %s", (before,))
        return cur.rowcount


# ---------------------------------------------------------------- 仮想売買（vp_positions / vp_trades）


async def vp_positions(ticker: str | None = None) -> list[dict[str, Any]]:
    async with _pool_or_raise().connection() as conn:
        if ticker is None:
            sql, params = "SELECT * FROM vp_positions ORDER BY opened_at", ()
        else:
            sql, params = "SELECT * FROM vp_positions WHERE ticker = %s ORDER BY account", (ticker,)
        return await (await conn.execute(sql, params)).fetchall()


async def vp_year_totals(year_start: datetime) -> dict[str, float]:
    """year_start 以降の、NISA の購入額合計・特定口座の実現損益合計・税金合計・手数料合計。"""
    async with _pool_or_raise().connection() as conn:
        row = await (
            await conn.execute(
                "SELECT "
                "COALESCE(SUM(amount) FILTER (WHERE account = 'nisa' AND side = 'buy'), 0) AS nisa_bought, "
                "COALESCE(SUM(realized) FILTER (WHERE account = 'tokutei' AND side = 'sell'), 0) AS tokutei_realized, "
                "COALESCE(SUM(tax), 0) AS tax, COALESCE(SUM(fee), 0) AS fee "
                "FROM vp_trades WHERE traded_at >= %s",
                (year_start,),
            )
        ).fetchone()
    return {k: float(v) for k, v in row.items()}


async def vp_record_trade(
    trade: dict[str, Any], cash_delta: float, position: dict[str, Any] | None, delete_position: bool = False
) -> None:
    """売買 1 件を記録し、現金残高と保有を更新する（すべて 1 つのトランザクションで行う）。

    position: 更新後の保有（account, ticker, company_name, shares, cost）。delete_position=True なら保有を削除する。
    """
    async with _pool_or_raise().connection() as conn:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO vp_trades (account, ticker, company_name, side, shares, price, amount, fee, realized, tax) "
                "VALUES (%(account)s, %(ticker)s, %(company_name)s, %(side)s, %(shares)s, %(price)s, %(amount)s, "
                "%(fee)s, %(realized)s, %(tax)s)",
                {"realized": None, "tax": 0, **trade},
            )
            await conn.execute(
                "UPDATE user_settings SET value = (value::double precision + %s)::text WHERE key = 'vp_cash'",
                (cash_delta,),
            )
            if delete_position:
                await conn.execute(
                    "DELETE FROM vp_positions WHERE account = %s AND ticker = %s", (trade["account"], trade["ticker"])
                )
            elif position is not None:
                await conn.execute(
                    "INSERT INTO vp_positions (account, ticker, company_name, shares, cost) "
                    "VALUES (%(account)s, %(ticker)s, %(company_name)s, %(shares)s, %(cost)s) "
                    "ON CONFLICT (account, ticker) DO UPDATE SET shares = EXCLUDED.shares, cost = EXCLUDED.cost",
                    position,
                )
