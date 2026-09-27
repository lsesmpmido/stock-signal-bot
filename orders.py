"""仮想売買の注文。取引時間中は最新の株価ですぐ約定し、取引時間外は注文として受け付けて翌取引日の始値で約定する。

AI の注文は大引け後の判断で出すので、常に翌取引日の始値で約定する。
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


async def fill_open_orders(now: datetime) -> list[Executed]:
    """今日の寄り付きより前に出された注文を、今日の始値で約定させる。今日の株価がまだない銘柄は次回に回す。"""
    today = now.date()
    if not market.is_trading_day(today):
        return []
    open_at = datetime.combine(today, portfolio.MARKET_OPEN, JST)
    pending = [o for o in await db.vp_orders("open") if o["created_at"] < open_at]
    if not pending:
        return []
    daily = await market.get_daily(sorted({o["ticker"] for o in pending}))

    executed = []
    # 売りを先に約定させ、売った代金で買えるようにする
    for order in sorted(pending, key=lambda o: (o["side"] != "sell", o["created_at"])):
        df = daily.get(order["ticker"])
        if df is None or df.empty or df.index[-1].date() != today or df["Open"].isna().iloc[-1]:
            if _trading_days_between(order["created_at"].astimezone(JST).date(), today) >= EXPIRE_TRADING_DAYS:
                await db.vp_update_order(order["id"], "expired", "株価を取得できないまま期限切れ")
                executed.append(Executed(order, None, "株価を取得できないまま期限切れになりました"))
            continue
        price = float(df["Open"].iloc[-1])
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
                    traded_at=open_at,
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
                    traded_at=open_at,
                )
        except portfolio.TradeError as exc:
            await db.vp_update_order(order["id"], "failed", str(exc))
            executed.append(Executed(order, None, str(exc)))
            continue
        await db.vp_update_order(order["id"], "filled")
        executed.append(Executed(order, result))
    return executed


async def cancel(order_id: int, owner: str = "you") -> bool:
    orders = [o for o in await db.vp_orders("open", owner) if o["id"] == order_id]
    return bool(orders) and await db.vp_update_order(order_id, "cancelled")
