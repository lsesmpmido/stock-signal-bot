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
    # 定番レポートの ON/OFF（on / off）
    "report_morning": "on",
    "report_close": "on",
    "report_weekly": "on",
    "report_cleanup": "on",  # 週末の整理タイム
    "watch_limit": "10",  # 監視できる銘柄数の上限
    # 仮想売買
    "vp_cash": "2400000",  # 自分の現金残高（円）。元手は新NISA 成長投資枠の年間上限と同じ 240 万円
    "vp_cash_ai": "2400000",  # AI の現金残高（円）
    "vp_deposits_you": "2400000",  # 入金額の合計（円）。成績は入金額の合計に対する損益で計算する
    "vp_deposits_ai": "2400000",
    "vp_fee_rate": "0",  # 売買手数料（売買代金に対する割合。例: 0.0022 = 0.22%）
    "vp_default_amount": "200000",  # 金額を省略したときの購入額（円）
    "vp_next_deposit": "",  # 次回（1 月 1 日）の追加入金額（円）。空なら 240 万円
    "vp_nisa_preset": "",  # 勝負を始める前に使った NISA 枠（"年:円"）。その年の NISA 枠の残りから差し引く
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
-- 既存の DB にも列を追加する（'news' = ニュースの銘柄、'related' = 関連銘柄）
ALTER TABLE pending_stocks ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'news';
CREATE TABLE IF NOT EXISTS monitored_stocks (
    ticker           TEXT PRIMARY KEY,
    company_name     TEXT NOT NULL,
    source           TEXT NOT NULL DEFAULT 'manual',
    added_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_notified_at TIMESTAMPTZ,
    last_signal      TEXT,
    signal_state     JSONB
);
-- 監視銘柄の管理強化（メモ・お気に入り・タグ・整理タイムで「続ける」を選んだ日時）
ALTER TABLE monitored_stocks ADD COLUMN IF NOT EXISTS memo TEXT;
ALTER TABLE monitored_stocks ADD COLUMN IF NOT EXISTS starred BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE monitored_stocks ADD COLUMN IF NOT EXISTS star_user_id BIGINT;
ALTER TABLE monitored_stocks ADD COLUMN IF NOT EXISTS tag TEXT;
ALTER TABLE monitored_stocks ADD COLUMN IF NOT EXISTS kept_at TIMESTAMPTZ;
ALTER TABLE pending_stocks ADD COLUMN IF NOT EXISTS skip_reason TEXT;
CREATE TABLE IF NOT EXISTS price_alerts (
    id           BIGSERIAL PRIMARY KEY,
    ticker       TEXT NOT NULL,
    company_name TEXT NOT NULL,
    target       DOUBLE PRECISION NOT NULL,
    direction    TEXT NOT NULL,  -- 'above'（超えたら）/ 'below'（割ったら）
    created_by   BIGINT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    triggered_at TIMESTAMPTZ,
    active       BOOLEAN NOT NULL DEFAULT true
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
    owner        TEXT NOT NULL DEFAULT 'you',  -- 'you' / 'ai'
    account      TEXT NOT NULL,  -- 'nisa' / 'tokutei'
    ticker       TEXT NOT NULL,
    company_name TEXT NOT NULL,
    shares       INTEGER NOT NULL,
    cost         DOUBLE PRECISION NOT NULL,  -- 取得費の合計（購入代金＋手数料）
    opened_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (owner, account, ticker)
);
CREATE TABLE IF NOT EXISTS vp_trades (
    id           BIGSERIAL PRIMARY KEY,
    owner        TEXT NOT NULL DEFAULT 'you',
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
    reason       TEXT,  -- 売買の理由（AI の判断・損切り・入れ替えなど）
    confidence   DOUBLE PRECISION,  -- AI の判断の確信度
    traded_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- 持ち主の列がなかった頃の DB を移行する（既存の保有・履歴はすべて自分の分とする）。何度実行しても問題ない
ALTER TABLE vp_positions ADD COLUMN IF NOT EXISTS owner TEXT NOT NULL DEFAULT 'you';
ALTER TABLE vp_trades ADD COLUMN IF NOT EXISTS owner TEXT NOT NULL DEFAULT 'you';
ALTER TABLE vp_trades ADD COLUMN IF NOT EXISTS reason TEXT;
ALTER TABLE vp_trades ADD COLUMN IF NOT EXISTS confidence DOUBLE PRECISION;
DO $$
BEGIN
    IF (SELECT count(*) FROM information_schema.key_column_usage
        WHERE table_name = 'vp_positions' AND constraint_name = 'vp_positions_pkey') < 3 THEN
        ALTER TABLE vp_positions DROP CONSTRAINT vp_positions_pkey;
        ALTER TABLE vp_positions ADD PRIMARY KEY (owner, account, ticker);
    END IF;
END $$;
CREATE TABLE IF NOT EXISTS vp_orders (
    id           BIGSERIAL PRIMARY KEY,
    owner        TEXT NOT NULL,
    side         TEXT NOT NULL,  -- 'buy' / 'sell'
    ticker       TEXT NOT NULL,
    company_name TEXT NOT NULL,
    amount       DOUBLE PRECISION,  -- 買い: 購入金額（円）
    shares       INTEGER,  -- 売り: 株数（NULL なら全株）
    account      TEXT,  -- 売り: 口座（NULL なら特定口座から）
    reason       TEXT,
    confidence   DOUBLE PRECISION,
    source       TEXT,  -- AI の買い: 候補に入った理由（proposal / watch / your_holding）
    status       TEXT NOT NULL DEFAULT 'open',  -- open / filled / failed / cancelled / expired
    note         TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    filled_at    TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS vp_snapshots (
    owner       TEXT NOT NULL,
    date        DATE NOT NULL,
    total_value DOUBLE PRECISION NOT NULL,
    deposits    DOUBLE PRECISION NOT NULL,  -- その時点までの入金額の合計
    PRIMARY KEY (owner, date)
);
CREATE TABLE IF NOT EXISTS notification_log (
    id      BIGSERIAL PRIMARY KEY,
    kind    TEXT NOT NULL,  -- proposal / signal / delist / report
    ticker  TEXT,
    detail  TEXT,
    sent_at TIMESTAMPTZ NOT NULL DEFAULT now()
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
ALTER TABLE vp_orders        ENABLE ROW LEVEL SECURITY;
ALTER TABLE vp_snapshots     ENABLE ROW LEVEL SECURITY;
ALTER TABLE price_alerts     ENABLE ROW LEVEL SECURITY;
ALTER TABLE notification_log ENABLE ROW LEVEL SECURITY;
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
        initial = {
            **DEFAULT_SETTINGS,
            "vp_started_at": datetime.now(timezone.utc).isoformat(),
            # 始めた年はすでに元手を入れているので、次の追加入金は翌年 1 月から
            "_last_deposit_year": str(datetime.now(timezone(timedelta(hours=9))).year),
        }
        for key, value in initial.items():
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
    ticker: str,
    company_name: str,
    news_title: str,
    news_url: str,
    score: float,
    impact: float,
    kind: str = "news",
) -> int | None:
    """提案を保存して id を返す。同じ銘柄×同じ記事が既にあれば None。"""
    async with _pool_or_raise().connection() as conn:
        row = await (
            await conn.execute(
                "INSERT INTO pending_stocks (ticker, company_name, news_title, news_url, score, impact, kind) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (ticker, news_url) DO NOTHING RETURNING id",
                (ticker, company_name, news_title, news_url, score, impact, kind),
            )
        ).fetchone()
    return row["id"] if row else None


async def list_pending_since(since: datetime) -> list[dict[str, Any]]:
    """since 以降の提案を古い順に返す（答え合わせ用）。"""
    async with _pool_or_raise().connection() as conn:
        return await (
            await conn.execute("SELECT * FROM pending_stocks WHERE created_at >= %s ORDER BY created_at", (since,))
        ).fetchall()


async def get_pending(pending_id: int) -> dict[str, Any] | None:
    async with _pool_or_raise().connection() as conn:
        return await (await conn.execute("SELECT * FROM pending_stocks WHERE id = %s", (pending_id,))).fetchone()


async def set_skip_reason(pending_id: int, reason: str) -> None:
    async with _pool_or_raise().connection() as conn:
        await conn.execute("UPDATE pending_stocks SET skip_reason = %s WHERE id = %s", (reason, pending_id))


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


MONITORED_EDITABLE = {"memo", "starred", "star_user_id", "tag", "kept_at"}


async def update_monitored(ticker: str, **fields: Any) -> bool:
    """監視銘柄のメモ・お気に入り・タグなどを更新する。監視していなければ False。"""
    if not fields or not set(fields) <= MONITORED_EDITABLE:
        raise ValueError(f"更新できない項目です: {set(fields) - MONITORED_EDITABLE}")
    assignments = ", ".join(f"{k} = %({k})s" for k in fields)  # 項目名は上の許可リストのものだけ
    async with _pool_or_raise().connection() as conn:
        cur = await conn.execute(
            f"UPDATE monitored_stocks SET {assignments} WHERE ticker = %(ticker)s", {**fields, "ticker": ticker}
        )
        return cur.rowcount > 0


async def count_monitored() -> int:
    async with _pool_or_raise().connection() as conn:
        return (await (await conn.execute("SELECT count(*) AS n FROM monitored_stocks")).fetchone())["n"]


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


# ---------------------------------------------------------------- 仮想売買（vp_positions / vp_trades / vp_orders / vp_snapshots）

CASH_KEYS = {"you": "vp_cash", "ai": "vp_cash_ai"}
DEPOSIT_KEYS = {"you": "vp_deposits_you", "ai": "vp_deposits_ai"}


async def vp_positions(owner: str, ticker: str | None = None) -> list[dict[str, Any]]:
    async with _pool_or_raise().connection() as conn:
        if ticker is None:
            sql, params = "SELECT * FROM vp_positions WHERE owner = %s ORDER BY opened_at", (owner,)
        else:
            sql, params = (
                "SELECT * FROM vp_positions WHERE owner = %s AND ticker = %s ORDER BY account",
                (owner, ticker),
            )
        return await (await conn.execute(sql, params)).fetchall()


async def vp_year_totals(owner: str, year_start: datetime) -> dict[str, float]:
    """year_start 以降の、NISA の購入額合計・特定口座の実現損益合計・税金合計・手数料合計。"""
    async with _pool_or_raise().connection() as conn:
        row = await (
            await conn.execute(
                "SELECT "
                "COALESCE(SUM(amount) FILTER (WHERE account = 'nisa' AND side = 'buy'), 0) AS nisa_bought, "
                "COALESCE(SUM(realized) FILTER (WHERE account = 'tokutei' AND side = 'sell'), 0) AS tokutei_realized, "
                "COALESCE(SUM(tax), 0) AS tax, COALESCE(SUM(fee), 0) AS fee "
                "FROM vp_trades WHERE owner = %s AND traded_at >= %s",
                (owner, year_start),
            )
        ).fetchone()
    return {k: float(v) for k, v in row.items()}


async def vp_trades(owner: str, since: datetime | None = None) -> list[dict[str, Any]]:
    async with _pool_or_raise().connection() as conn:
        return await (
            await conn.execute(
                "SELECT * FROM vp_trades WHERE owner = %s AND traded_at >= %s ORDER BY traded_at",
                (owner, since or datetime(2000, 1, 1, tzinfo=timezone.utc)),
            )
        ).fetchall()


async def vp_record_trade(
    owner: str,
    trade: dict[str, Any],
    cash_delta: float,
    position: dict[str, Any] | None,
    delete_position: bool = False,
    traded_at: datetime | None = None,
) -> None:
    """売買 1 件を記録し、現金残高と保有を更新する（すべて 1 つのトランザクションで行う）。

    position: 更新後の保有（account, ticker, company_name, shares, cost）。delete_position=True なら保有を削除する。
    """
    async with _pool_or_raise().connection() as conn:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO vp_trades (owner, account, ticker, company_name, side, shares, price, amount, fee, "
                "realized, tax, reason, confidence, traded_at) "
                "VALUES (%(owner)s, %(account)s, %(ticker)s, %(company_name)s, %(side)s, %(shares)s, %(price)s, "
                "%(amount)s, %(fee)s, %(realized)s, %(tax)s, %(reason)s, %(confidence)s, %(traded_at)s)",
                {
                    "realized": None,
                    "tax": 0,
                    "reason": None,
                    "confidence": None,
                    **trade,
                    "owner": owner,
                    "traded_at": traded_at or datetime.now(timezone.utc),
                },
            )
            await conn.execute(
                "UPDATE user_settings SET value = (value::double precision + %s)::text WHERE key = %s",
                (cash_delta, CASH_KEYS[owner]),
            )
            if delete_position:
                await conn.execute(
                    "DELETE FROM vp_positions WHERE owner = %s AND account = %s AND ticker = %s",
                    (owner, trade["account"], trade["ticker"]),
                )
            elif position is not None:
                await conn.execute(
                    "INSERT INTO vp_positions (owner, account, ticker, company_name, shares, cost, opened_at) "
                    "VALUES (%(owner)s, %(account)s, %(ticker)s, %(company_name)s, %(shares)s, %(cost)s, %(opened_at)s) "
                    "ON CONFLICT (owner, account, ticker) DO UPDATE SET shares = EXCLUDED.shares, cost = EXCLUDED.cost",
                    {"opened_at": traded_at or datetime.now(timezone.utc), **position, "owner": owner},
                )


async def vp_deposit(owner: str, amount: float) -> None:
    """現金を入金し、入金額の合計も増やす。"""
    async with _pool_or_raise().connection() as conn:
        async with conn.transaction():
            for key in (CASH_KEYS[owner], DEPOSIT_KEYS[owner]):
                await conn.execute(
                    "UPDATE user_settings SET value = (value::double precision + %s)::text WHERE key = %s",
                    (amount, key),
                )


async def vp_add_order(order: dict[str, Any]) -> int:
    fields = {"amount": None, "shares": None, "account": None, "reason": None, "confidence": None, "source": None}
    async with _pool_or_raise().connection() as conn:
        row = await (
            await conn.execute(
                "INSERT INTO vp_orders (owner, side, ticker, company_name, amount, shares, account, reason, confidence, source) "
                "VALUES (%(owner)s, %(side)s, %(ticker)s, %(company_name)s, %(amount)s, %(shares)s, %(account)s, "
                "%(reason)s, %(confidence)s, %(source)s) RETURNING id",
                {**fields, **order},
            )
        ).fetchone()
    return row["id"]


async def vp_orders(status: str = "open", owner: str | None = None) -> list[dict[str, Any]]:
    async with _pool_or_raise().connection() as conn:
        if owner is None:
            sql, params = "SELECT * FROM vp_orders WHERE status = %s ORDER BY created_at", (status,)
        else:
            sql, params = (
                "SELECT * FROM vp_orders WHERE status = %s AND owner = %s ORDER BY created_at",
                (status, owner),
            )
        return await (await conn.execute(sql, params)).fetchall()


async def vp_update_order(order_id: int, status: str, note: str | None = None) -> bool:
    """注文の状態を変える。未約定（open）の注文だけを対象にし、変えられたら True。"""
    async with _pool_or_raise().connection() as conn:
        cur = await conn.execute(
            "UPDATE vp_orders SET status = %s, note = %s, filled_at = CASE WHEN %s = 'filled' THEN now() END "
            "WHERE id = %s AND status = 'open'",
            (status, note, status, order_id),
        )
        return cur.rowcount > 0


async def vp_save_snapshot(owner: str, day: date, total_value: float, deposits: float) -> None:
    async with _pool_or_raise().connection() as conn:
        await conn.execute(
            "INSERT INTO vp_snapshots (owner, date, total_value, deposits) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (owner, date) DO UPDATE SET total_value = EXCLUDED.total_value, deposits = EXCLUDED.deposits",
            (owner, day, total_value, deposits),
        )


async def vp_snapshots(owner: str) -> list[dict[str, Any]]:
    async with _pool_or_raise().connection() as conn:
        return await (
            await conn.execute("SELECT * FROM vp_snapshots WHERE owner = %s ORDER BY date", (owner,))
        ).fetchall()


async def vp_reset(
    cash: float,
    deposits: float,
    positions: list[dict[str, Any]],
    started_at: datetime,
    nisa_preset: str,
) -> None:
    """自分と AI の仮想口座を、同じ初期条件（現金・保有）で作り直す。売買履歴・注文・勝負の記録は消す。

    positions: 両チームに持たせる保有（account, ticker, company_name, shares, cost, opened_at）。
    """
    async with _pool_or_raise().connection() as conn:
        async with conn.transaction():
            for table in ("vp_positions", "vp_trades", "vp_orders", "vp_snapshots"):
                await conn.execute(f"DELETE FROM {table}")  # テーブル名は上の固定の一覧のものだけ
            for owner in CASH_KEYS:
                for p in positions:
                    await conn.execute(
                        "INSERT INTO vp_positions (owner, account, ticker, company_name, shares, cost, opened_at) "
                        "VALUES (%(owner)s, %(account)s, %(ticker)s, %(company_name)s, %(shares)s, %(cost)s, %(opened_at)s)",
                        {**p, "owner": owner},
                    )
            settings = {
                **{key: str(cash) for key in CASH_KEYS.values()},
                **{key: str(deposits) for key in DEPOSIT_KEYS.values()},
                "vp_started_at": started_at.isoformat(),
                "vp_nisa_preset": nisa_preset,
            }
            for key, value in settings.items():
                await conn.execute(
                    "INSERT INTO user_settings (key, value) VALUES (%s, %s) "
                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                    (key, value),
                )


# ---------------------------------------------------------------- notification_log


async def log_notification(kind: str, ticker: str | None = None, detail: str | None = None) -> None:
    """Bot が送った通知を記録する（週間レポートの通知件数・シグナル一覧に使う）。"""
    async with _pool_or_raise().connection() as conn:
        await conn.execute(
            "INSERT INTO notification_log (kind, ticker, detail) VALUES (%s, %s, %s)", (kind, ticker, detail)
        )


async def notifications_since(since: datetime, kind: str | None = None) -> list[dict[str, Any]]:
    async with _pool_or_raise().connection() as conn:
        if kind is None:
            sql, params = "SELECT * FROM notification_log WHERE sent_at >= %s ORDER BY sent_at", (since,)
        else:
            sql, params = (
                "SELECT * FROM notification_log WHERE sent_at >= %s AND kind = %s ORDER BY sent_at",
                (since, kind),
            )
        return await (await conn.execute(sql, params)).fetchall()


# ---------------------------------------------------------------- price_alerts


async def add_alert(ticker: str, company_name: str, target: float, direction: str, created_by: int | None) -> int:
    async with _pool_or_raise().connection() as conn:
        row = await (
            await conn.execute(
                "INSERT INTO price_alerts (ticker, company_name, target, direction, created_by) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (ticker, company_name, target, direction, created_by),
            )
        ).fetchone()
    return row["id"]


async def list_alerts(active: bool = True) -> list[dict[str, Any]]:
    async with _pool_or_raise().connection() as conn:
        return await (
            await conn.execute("SELECT * FROM price_alerts WHERE active = %s ORDER BY created_at", (active,))
        ).fetchall()


async def close_alert(alert_id: int, triggered: bool) -> bool:
    """アラートを終了する（triggered=True なら達して通知した、False なら取り消し）。"""
    async with _pool_or_raise().connection() as conn:
        cur = await conn.execute(
            "UPDATE price_alerts SET active = false, triggered_at = CASE WHEN %s THEN now() END "
            "WHERE id = %s AND active",
            (triggered, alert_id),
        )
        return cur.rowcount > 0
