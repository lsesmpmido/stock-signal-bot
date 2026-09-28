"""仮想売買の注文。取引時間中は最新の株価ですぐ約定し、取引時間外は注文として受け付けて次の寄り付きの始値で約定する。

AI の注文は大引け後の判断で出すので、常に翌取引日の始値で約定する（取引時間中の損切りだけは注文を通さず、
ai_trader.intraday_stop_loss がその場の株価で売る）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import db
import market
import portfolio
from market import JST

log = logging.getLogger(__name__)

EXPIRE_TRADING_DAYS = 5  # この営業日数たっても約定しない注文（売買停止など）は取り消す


@dataclass
class Placed:
    """注文の結果。result が入っていればすぐ約定、order_id が入っていれば翌取引日の始値で約定する注文。"""

    result: portfolio.BuyResult | portfolio.SellResult | None = None
    order_id: int | None = None


@dataclass
class Executed:
    order: dict
    result: portfolio.BuyResult | portfolio.SellResult | None
    error: str | None = None


async def place_buy(
    owner: str,
    ticker: str,
    company_name: str,
    amount: float,
    reason: str | None = None,
    confidence: float | None = None,
    source: str | None = None,
) -> Placed:
    price = await portfolio.live_price(ticker) if owner == "you" else None
    if price is not None:
        return Placed(result=await portfolio.buy(owner, ticker, company_name, amount, price))
    order_id = await db.vp_add_order(
        {
            "owner": owner,
            "side": "buy",
            "ticker": ticker,
            "company_name": company_name,
            "amount": amount,
            "reason": reason,
            "confidence": confidence,
            "source": source,
        }
    )
    return Placed(order_id=order_id)


async def place_sell(
    owner: str,
    ticker: str,
    shares: int | None = None,
    account: str | None = None,
    reason: str | None = None,
    confidence: float | None = None,
) -> Placed:
    # 注文を受け付ける時点で、保有しているかと株数を確かめておく
    position = portfolio.pick_position(await db.vp_positions(owner, ticker), account)
    if shares is not None and shares > position["shares"]:
        raise portfolio.TradeError(
            f"{portfolio.ACCOUNT_LABELS[position['account']]}の保有は {position['shares']} 株です（指定: {shares} 株）。"
        )
    price = await portfolio.live_price(ticker) if owner == "you" else None
    if price is not None:
        return Placed(result=await portfolio.sell(owner, ticker, price, shares, account))
    order_id = await db.vp_add_order(
        {
            "owner": owner,
            "side": "sell",
            "ticker": ticker,
            "company_name": position["company_name"],
            "shares": shares,
            "account": account,
            "reason": reason,
            "confidence": confidence,
        }
    )
    return Placed(order_id=order_id)


def _trading_days_between(start: date, end: date) -> int:
    """start の翌日から end までの取引日の数。"""
    days, d = 0, start
    while d < end:
        d += timedelta(days=1)
        if market.is_trading_day(d):
            days += 1
    return days


def queued_fill(now: datetime) -> tuple[str, str]:
    """今出した注文が、いつの株価で約定するか（いつ, なぜ）。注文を受け付けたときの表示に使う。"""
    t = now.time()
    if market.is_trading_day(now.date()) and t < portfolio.MARKET_CLOSE:
        if t < portfolio.MARKET_OPEN:
            return "今日の始値", "寄り付き前のため"
        if t < portfolio.LUNCH_START:
            return "今日の始値", "今日の株価がまだ取れないため"
        if t < portfolio.AFTERNOON_LIVE:
            return "今日の後場の始値（12:30）", "昼休み（後場の株価が届く前）のため"
    return "翌取引日の始値", "取引時間外のため"


def _afternoon_open(intraday) -> float | None:
    """今日の 5 分足から、後場の寄り付き（12:30 以降の最初の足）の始値。"""
    if intraday is None or intraday.empty:
        return None
    bars = intraday[intraday.index.time >= portfolio.AFTERNOON_OPEN]
    return None if bars.empty else float(bars["Open"].iloc[0])


async def fill_open_orders(now: datetime) -> list[Executed]:
    """取引時間外・昼休みなどに出た注文を、次の寄り付きの始値で約定させる。株価がまだない銘柄は次回に回す。

    - 前場の終わり（11:30）より前に出た注文: 今日の始値（前日以前の注文と、寄り付き直後で株価がまだ取れなかった注文）
    - 昼休み（後場の株価が届く 12:50 まで）に出た注文: 今日の後場の始値（12:30）。後場の株価が届いてから約定させる
    - それより後に出た注文: 翌取引日に回す
    約定の処理を始める前に注文を「約定中」にして、取り消しと同時に約定したり、二重に約定したりしないようにする。
    """
    today = now.date()
    if not market.is_trading_day(today):
        return []
    open_at = datetime.combine(today, portfolio.MARKET_OPEN, JST)
    lunch_at = datetime.combine(today, portfolio.LUNCH_START, JST)
    afternoon_at = datetime.combine(today, portfolio.AFTERNOON_OPEN, JST)
    afternoon_live = datetime.combine(today, portfolio.AFTERNOON_LIVE, JST)
    pending = []
    for o in await db.vp_orders("open"):
        if o["created_at"] < lunch_at:
            pending.append((o, "open"))
        elif o["created_at"] < afternoon_live and now >= afternoon_live:
            pending.append((o, "afternoon"))
    if not pending:
        return []
    daily = await market.get_daily(sorted({o["ticker"] for o, _ in pending}))
    intraday = {}
    for ticker in sorted({o["ticker"] for o, when in pending if when == "afternoon"}):
        intraday[ticker] = await market.fetch_intraday(ticker)

    executed = []
    # 売りを先に約定させ、売った代金で買えるようにする
    for order, when in sorted(pending, key=lambda p: (p[0]["side"] != "sell", p[0]["created_at"])):
        df = daily.get(order["ticker"])
        today_bar = df is not None and not df.empty and df.index[-1].date() == today and not df["Open"].isna().iloc[-1]
        if when == "open":
            price, traded_at = (float(df["Open"].iloc[-1]) if today_bar else None), open_at
        else:
            bars = intraday.get(order["ticker"])
            on_today = bars is not None and not bars.empty and bars.index[-1].date() == today
            price, traded_at = (_afternoon_open(bars) if on_today else None), afternoon_at
        if price is None:
            if _trading_days_between(order["created_at"].astimezone(JST).date(), today) >= EXPIRE_TRADING_DAYS:
                await db.vp_update_order(order["id"], "expired", "株価を取得できないまま期限切れ")
                executed.append(Executed(order, None, "株価を取得できないまま期限切れになりました"))
            continue
        if not await db.vp_update_order(order["id"], "filling"):
            continue  # 同時に取り消された
        try:
            if order["side"] == "buy":
                result = await portfolio.buy(
                    order["owner"],
                    order["ticker"],
                    order["company_name"],
                    order["amount"],
                    price,
                    order["reason"],
                    order["confidence"],
                    traded_at=traded_at,
                )
            else:
                result = await portfolio.sell(
                    order["owner"],
                    order["ticker"],
                    price,
                    order["shares"],
                    order["account"],
                    order["reason"],
                    order["confidence"],
                    traded_at=traded_at,
                )
        except portfolio.TradeError as exc:
            await db.vp_update_order(order["id"], "failed", str(exc), current="filling")
            executed.append(Executed(order, None, str(exc)))
            continue
        except Exception:
            # 売買の途中で失敗した注文は、二重に約定しないよう「約定中」のまま残し、ほかの注文の約定は続ける
            log.exception("注文 #%s の約定でエラーが発生しました", order["id"])
            executed.append(Executed(order, None, "エラーが発生しました。`/portfolio` で保有を確かめてください"))
            continue
        await db.vp_update_order(order["id"], "filled", current="filling")
        executed.append(Executed(order, result))
    return executed


async def cancel(order_id: int, owner: str = "you") -> bool:
    orders = [o for o in await db.vp_orders("open", owner) if o["id"] == order_id]
    return bool(orders) and await db.vp_update_order(order_id, "cancelled")
