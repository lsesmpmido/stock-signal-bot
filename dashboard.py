"""Web のポートフォリオ画面（スマホ向け）。ヘルスチェック用の aiohttp サーバーに載せる。

- ログイン: Discord の /dashboard で、許可したユーザー（DASHBOARD_USER_ID、未設定なら Bot の所有者）にだけ
  1 回限りのログイン用リンク（5 分有効）を出す。リンクを開くとセッションの Cookie（30 日有効）を発行する。
  リンクのトークンとセッションは、DB にハッシュだけを保存する
- 画面は見るだけ（売買などの操作はしない）。データは DB に保存済みの日足で計算する（ダウンロードし直さない）
- 推移は、売買履歴と日足から日ごとの保有を計算し直す（今の保有から売買を逆にたどる）。新しく記録を始めなくても全期間を出せる。
  勝負を始める前も、開始時点の保有（vp_start_holdings）を取得日から持っていたとして、最も古い取得日から描く
  （その間の現金は、開始時点の現金に、まだ買っていない保有の取得額を足したもの）
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import time as _time
from collections import deque
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

import pandas as pd
from aiohttp import web

import battle
import db
import market
import portfolio
from market import JST

log = logging.getLogger(__name__)

TOKEN_TTL = timedelta(minutes=5)
SESSION_TTL = timedelta(days=30)
COOKIE = "session"
FAIL_WINDOW = 600  # 秒。この間にログインの失敗が FAIL_LIMIT 回を超えたら、しばらく受け付けない（総当たりの防止）
FAIL_LIMIT = 20
MAX_TICKERS = 8  # 推移のグラフで銘柄ごとに分ける数。残りは「その他」にまとめる
PAGE = Path(__file__).with_name("static") / "dashboard.html"
CHART_JS = (
    "https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js",
    "sha384-bs/nf9FbdNouRbMiFcrcZfLXYPKiPaGVGplVbv7dLGECccEXDW+S3zjqSKR5ZEaD",
)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def base_url() -> str | None:
    """画面の URL（DASHBOARD_URL）。未設定なら None（/dashboard でリンクを出せない）。"""
    return os.getenv("DASHBOARD_URL", "").strip().rstrip("/") or None


async def create_login_link(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    await db.dashboard_add_token(_hash(token), user_id, datetime.now(timezone.utc) + TOKEN_TTL)
    return f"{base_url()}/login?token={token}"


# ---------------------------------------------------------------- 画面に出すデータ


def _rate(change: float | None, base: float) -> float | None:
    return None if change is None or not base else change / base


def _changes(h: portfolio.Holding, df: pd.DataFrame | None) -> tuple[float | None, float | None]:
    """保有の前日比と前月比（円）。前日・1 か月前より後に買った保有は、取得額からの増減にする。"""
    if df is None or len(df) < 2 or h.value is None:
        return None, None
    last = df.index[-1].date()
    opened = h.opened_at.astimezone(JST).date()
    price = h.price
    day = (price - float(df["Close"].iloc[-2])) * h.shares if opened < last else h.value - h.cost
    month_ago = (pd.Timestamp(last) - pd.DateOffset(months=1)).date()
    before = df[df.index.date <= month_ago]
    if opened > month_ago:
        month = h.value - h.cost
    elif before.empty:
        month = None
    else:
        month = (price - float(before["Close"].iloc[-1])) * h.shares
    return day, month


async def summary_data(team: str) -> dict[str, Any]:
    s = await portfolio.summary(team, refresh=False)
    tickers = sorted({h.ticker for h in s.holdings})
    daily = await market.get_daily(tickers, refresh=False) if tickers else {}
    rows = []
    for h in s.holdings:
        day, month = _changes(h, daily.get(h.ticker))
        value = h.value if h.value is not None else h.cost
        rows.append(
            {
                "ticker": h.ticker,
                "name": h.company_name,
                "account": h.account,
                "shares": h.shares,
                "cost": h.cost,
                "price": h.price,
                "value": value,
                "unrealized": h.unrealized,
                "unrealized_rate": h.return_rate,
                "day_change": day,
                "day_rate": _rate(day, value - (day or 0)),
                "month_change": month,
                "month_rate": _rate(month, value - (month or 0)),
            }
        )
    rows.sort(key=lambda r: r["value"], reverse=True)
    stock = sum(r["value"] for r in rows)
    # ゲーム外の保有（米国株など）は、取得額のまま一覧の最後に出す（国内株式の合計額には含めない）
    for o in s.outside:
        rows.append(
            {
                "ticker": "",
                "name": f"{o['name']}（ゲーム外）",
                "account": o["account"],
                "outside": True,
                "shares": None,
                "cost": o["cost"],
                "price": None,
                "value": o["cost"],
                "unrealized": None,
                "unrealized_rate": None,
                "day_change": None,
                "day_rate": None,
                "month_change": None,
                "month_rate": None,
            }
        )

    def total(key: str) -> float | None:
        values = [r[key] for r in rows if r[key] is not None and not r.get("outside")]
        return sum(values) if values else None

    day, month, unrealized = total("day_change"), total("month_change"), total("unrealized")
    dates = [df.index[-1].date() for df in daily.values() if not df.empty]
    return {
        "team": team,
        "label": portfolio.OWNER_LABELS[team],
        "price_date": max(dates).isoformat() if dates else None,
        "total_value": s.total_value,
        "deposits": s.deposits,
        "total_return": s.total_return,
        "cash": s.cash,
        "stock_value": stock,
        "unrealized": unrealized,
        "unrealized_rate": _rate(unrealized, stock - (unrealized or 0)),
        "day_change": day,
        "day_rate": _rate(day, stock - (day or 0)),
        "month_change": month,
        "month_rate": _rate(month, stock - (month or 0)),
        "nisa_limit": portfolio.NISA_ANNUAL_LIMIT,
        "nisa_used": portfolio.NISA_ANNUAL_LIMIT - s.nisa_left,
        "year": market.now_jst().year,
        "holdings": rows,
    }


def _split_factor(splits: list[dict], ticker: str, traded_on: date) -> float:
    """traded_on より後に反映した株式分割の比の積（その日の株数を、今の株数の単位に直す）。"""
    factor = 1.0
    for s in splits:
        if s["ticker"] == ticker and s["ex_date"] > traded_on:
            factor *= s["ratio"]
    return factor


async def history_data(team: str, max_tickers: int | None = MAX_TICKERS) -> dict[str, Any]:
    """日ごとの保有（銘柄ごと・口座ごとの時価）と現金・入金額の合計。今の保有と現金から、売買と入金を逆にたどって求める。

    max_tickers を超える銘柄は「その他」にまとめる（None ならまとめない）。
    """
    settings = await db.get_all_settings()
    started_on = datetime.fromisoformat(settings["vp_started_at"]).astimezone(JST).date()
    start_holdings = portfolio.start_holdings(settings)
    outside = portfolio.outside_holdings(settings, market.now_jst().date())
    start = portfolio.invested_since(settings)
    positions = await db.vp_positions(team)
    trades = await db.vp_trades(team)
    snapshots = await db.vp_snapshots(team)
    splits = await db.all_splits()
    names = (
        {h["ticker"]: h["name"] for h in start_holdings}
        | {t["ticker"]: t["company_name"] for t in trades}
        | {p["ticker"]: p["company_name"] for p in positions}
    )
    daily = await market.get_daily(sorted(set(names) | {"1306"}), years=6, refresh=False)
    calendar = sorted({d for df in daily.values() for d in df.index.date if d >= start})
    if not calendar:
        calendar = [market.now_jst().date()]

    deposits_now = float(settings[db.DEPOSIT_KEYS[team]])
    deposit_on = {s["date"]: float(s["deposits"]) for s in snapshots}
    first_deposit = float(snapshots[0]["deposits"]) if snapshots else deposits_now

    shares = {(p["ticker"], p["account"]): float(p["shares"]) for p in positions}
    cash = float(settings[db.CASH_KEYS[team]])
    pending = sorted(trades, key=lambda t: t["traded_at"], reverse=True)
    states = []
    i = 0
    for d in reversed(calendar):
        # d より後の売買を逆にたどり、d の大引け時点の保有と現金にする
        while i < len(pending) and pending[i]["traded_at"].astimezone(JST).date() > d:
            t = pending[i]
            traded_on = t["traded_at"].astimezone(JST).date()
            n = t["shares"] * _split_factor(splits, t["ticker"], traded_on)
            key = (t["ticker"], t["account"])
            if t["side"] == "buy":
                shares[key] = shares.get(key, 0.0) - n
                cash += t["amount"] + t["fee"]
            else:
                shares[key] = shares.get(key, 0.0) + n
                cash -= t["amount"] - t["fee"] - t["tax"]
            i += 1
        if d < started_on:
            # 勝負を始める前: 開始時点の保有のうち、その日までに買ったものだけを持ち、まだ買っていない分は現金にある
            held = {(h["ticker"], h["account"]): 0.0 for h in start_holdings}
            cash_then = cash
            for h in start_holdings:
                if h["opened_on"] <= d:
                    held[(h["ticker"], h["account"])] += h["shares"] * _split_factor(splits, h["ticker"], started_on)
                else:
                    cash_then += h["cost"]
            states.append((d, held, cash_then))
        else:
            states.append((d, dict(shares), cash))
    states.reverse()

    closes = {code: df["Close"] for code, df in daily.items() if not df.empty}
    deposits_series, cash_series, total_series = [], [], []
    by_ticker: dict[str, list[float]] = {}
    by_account = {"nisa": [], "tokutei": []}
    deposits = first_deposit
    for n, (d, held, cash_then) in enumerate(states):
        deposits = deposit_on.get(d, deposits)
        # 入金は記録（大引け後の総資産の記録）の入金額の合計から、その日より後に入れた分を除く
        cash_on_day = cash_then - (deposits_now - deposits)
        values = {"nisa": 0.0, "tokutei": 0.0}
        day_by_ticker: dict[str, float] = {}
        for o in outside:  # ゲーム外の保有は、取得日から取得額のまま持つ。それまでは現金にある
            if o["opened_on"] <= d:
                values[o["account"]] += o["cost"]
                day_by_ticker[f"outside:{o['name']}"] = day_by_ticker.get(f"outside:{o['name']}", 0.0) + o["cost"]
            else:
                cash_on_day += o["cost"]
        for (ticker, account), count in held.items():
            if count <= 0 or ticker not in closes:
                continue
            price = closes[ticker].asof(pd.Timestamp(d))
            if pd.isna(price):
                continue
            value = float(price) * count
            values[account] += value
            day_by_ticker[ticker] = day_by_ticker.get(ticker, 0.0) + value
        for ticker in set(by_ticker) | set(day_by_ticker):
            by_ticker.setdefault(ticker, [0.0] * n).append(day_by_ticker.get(ticker, 0.0))
        for account in by_account:
            by_account[account].append(values[account])
        deposits_series.append(deposits)
        cash_series.append(cash_on_day)
        total_series.append(cash_on_day + values["nisa"] + values["tokutei"])

    for o in outside:
        names[f"outside:{o['name']}"] = f"{o['name']}（ゲーム外）"
    ranked = sorted(by_ticker, key=lambda t: max(by_ticker[t]), reverse=True)
    limit = len(ranked) if max_tickers is None else max_tickers
    tickers = [{"ticker": t, "name": names.get(t, t), "values": by_ticker[t]} for t in ranked[:limit]]
    if len(ranked) > limit:
        rest = [sum(by_ticker[t][k] for t in ranked[limit:]) for k in range(len(states))]
        tickers.append({"ticker": "", "name": "その他", "values": rest})
    return {
        "team": team,
        "dates": [d.isoformat() for d, _, _ in states],
        "deposits": deposits_series,
        "cash": cash_series,
        "total": total_series,
        "tickers": tickers,
        "accounts": by_account,
    }


# ---------------------------------------------------------------- Web サーバー


class Dashboard:
    def __init__(self, allowed_user: Callable[[], Awaitable[int | None]]) -> None:
        self._allowed_user = allowed_user
        self._failures: deque[float] = deque()

    def setup(self, app: web.Application) -> None:
        app.middlewares.append(self._headers)
        app.router.add_get("/", self.page)
        app.router.add_get("/login", self.login)
        app.router.add_get("/api/summary", self.api_summary)
        app.router.add_get("/api/history", self.api_history)

    @web.middleware
    async def _headers(self, request: web.Request, handler) -> web.StreamResponse:
        try:
            response = await handler(request)
        except RuntimeError:  # 起動の途中で、まだ DB につながっていない
            response = web.json_response({"error": "starting"}, status=503)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")  # ログイン用リンクのトークンを外に漏らさない
        if request.path != "/health":
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    async def page(self, _: web.Request) -> web.Response:
        nonce = secrets.token_urlsafe(16)
        html = PAGE.read_text(encoding="utf-8")
        html = html.replace("{{NONCE}}", nonce).replace("{{CHART_JS}}", CHART_JS[0]).replace("{{CHART_JS_SRI}}", CHART_JS[1])
        csp = (
            "default-src 'none'; "
            f"script-src 'nonce-{nonce}' https://cdnjs.cloudflare.com; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
        )
        return web.Response(text=html, content_type="text/html", headers={"Content-Security-Policy": csp})

    def _blocked(self) -> bool:
        now = _time.monotonic()
        while self._failures and now - self._failures[0] > FAIL_WINDOW:
            self._failures.popleft()
        return len(self._failures) >= FAIL_LIMIT

    async def login(self, request: web.Request) -> web.Response:
        if self._blocked():
            return web.Response(status=429, text="ログインの失敗が続いたため、しばらく受け付けません。10 分ほどたってから試してください。")
        token = request.query.get("token", "")
        user_id = await db.dashboard_use_token(_hash(token)) if token else None
        if user_id is None or user_id != await self._allowed_user():
            self._failures.append(_time.monotonic())
            return web.Response(
                status=400,
                text="このリンクは使えません（5 分の期限切れ・使用済み・無効）。Discord で /dashboard を実行して、新しいリンクを開いてください。",
            )
        session = secrets.token_urlsafe(32)
        await db.dashboard_add_session(_hash(session), user_id, datetime.now(timezone.utc) + SESSION_TTL)
        response = web.Response(status=303, headers={"Location": "/"})  # URL からトークンを消す
        response.set_cookie(
            COOKIE,
            session,
            max_age=int(SESSION_TTL.total_seconds()),
            path="/",
            httponly=True,
            secure=(base_url() or "").startswith("https://"),
            samesite="Lax",
        )
        log.info("Web 画面にログインしました (user %s)", user_id)
        return response

    async def _authorized(self, request: web.Request) -> bool:
        session = request.cookies.get(COOKIE)
        if not session:
            return False
        user_id = await db.dashboard_session_user(_hash(session))
        return user_id is not None and user_id == await self._allowed_user()

    def _team(self, request: web.Request) -> str:
        team = request.query.get("team", "you")
        if team not in battle.TEAMS:
            raise web.HTTPBadRequest(text="team が不正です")
        return team

    async def api_summary(self, request: web.Request) -> web.Response:
        if not await self._authorized(request):
            return web.json_response({"error": "login"}, status=401)
        return web.json_response(await summary_data(self._team(request)))

    async def api_history(self, request: web.Request) -> web.Response:
        if not await self._authorized(request):
            return web.json_response({"error": "login"}, status=401)
        return web.json_response(await history_data(self._team(request)))
