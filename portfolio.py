"""仮想売買（新NISA 成長投資枠＋特定口座）。

- 元手 240 万円。現金は 2 つの口座で共通
- 買うときは NISA を優先し、その年の NISA 枠（購入額の合計 240 万円）を超える分は特定口座で買う
- NISA 枠は売っても同じ年には戻らず、翌年 1 月に 240 万円に戻る
- 特定口座の売却益には 20.315% の税金がかかる。同じ年の損益は相殺し、損が出たら払い過ぎの税金を戻す
- 手数料は売買代金 × vp_fee_rate。特定口座では利益から差し引いてから税金を計算する
- 監視（通知）とは別の機能で、仮想で買っても監視対象には入らない
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field
from datetime import date, datetime

import db
import market
from market import JST

NISA_ANNUAL_LIMIT = 2_400_000
TAX_RATE = 0.20315
INITIAL_CASH = 2_400_000
ACCOUNT_LABELS = {"nisa": "NISA", "tokutei": "特定口座"}

_lock = asyncio.Lock()  # ボタンの連打などで売買が同時に走らないようにする


class TradeError(Exception):
    """利用者に見せるエラー（資金不足・保有なしなど）。"""


@dataclass
class Fill:
    account: str
    shares: int
    amount: float
    fee: float


@dataclass
class BuyResult:
    ticker: str
    company_name: str
    price: float
    fills: list[Fill]
    cash_after: float
    nisa_left: float


@dataclass
class SellResult:
    ticker: str
    company_name: str
    account: str
    shares: int
    price: float
    amount: float
    fee: float
    cost: float
    realized: float  # 税引前（手数料込み）
    tax: float  # マイナスは還付
    held_days: int
    topix_change: float | None  # 同じ期間の TOPIX 連動 ETF の騰落率
    cash_after: float

    @property
    def net(self) -> float:
        return self.realized - self.tax

    @property
    def return_rate(self) -> float:
        return self.net / self.cost if self.cost else 0.0


@dataclass
class Holding:
    account: str
    ticker: str
    company_name: str
    shares: int
    cost: float
    price: float | None

    @property
    def value(self) -> float | None:
        return None if self.price is None else self.price * self.shares

    @property
    def unrealized(self) -> float | None:
        return None if self.value is None else self.value - self.cost


@dataclass
class Summary:
    cash: float
    holdings: list[Holding]
    nisa_left: float
    year_tax: float
    year_fee: float
    started_at: datetime
    topix_change: float | None
    total_value: float = field(init=False)

    def __post_init__(self) -> None:
        self.total_value = self.cash + sum(h.value if h.value is not None else h.cost for h in self.holdings)

    @property
    def total_return(self) -> float:
        return self.total_value / INITIAL_CASH - 1


def _year_start(now: datetime) -> datetime:
    return datetime(now.year, 1, 1, tzinfo=JST)


async def _latest_price(ticker: str) -> float:
    daily = (await market.get_daily([ticker])).get(ticker)
    if daily is None or daily.empty:
        raise TradeError(f"{ticker} の株価を取得できませんでした。時間をおいて再度お試しください。")
    return float(daily["Close"].iloc[-1])


async def _settings() -> tuple[float, float]:
    s = await db.get_all_settings()
    return float(s["vp_cash"]), float(s["vp_fee_rate"])


async def default_amount() -> float:
    return float((await db.get_all_settings())["vp_default_amount"])


async def buy(ticker: str, company_name: str, amount: float) -> BuyResult:
    """amount 円以内で買える最大の株数を買う（1 株単位）。"""
    async with _lock:
        price = await _latest_price(ticker)
        cash, fee_rate = await _settings()
        budget = min(amount, cash)
        shares = math.floor(budget / (price * (1 + fee_rate)))
        if shares <= 0:
            raise TradeError(
                f"1 株も買えません（株価 {price:,.0f} 円、指定額 {amount:,.0f} 円、現金 {cash:,.0f} 円）。"
            )
        totals = await db.vp_year_totals(_year_start(market.now_jst()))
        nisa_left = max(0.0, NISA_ANNUAL_LIMIT - totals["nisa_bought"])
        nisa_shares = min(shares, math.floor(nisa_left / price))
        plan = [("nisa", nisa_shares), ("tokutei", shares - nisa_shares)]

        fills = []
        for account, n in plan:
            if n <= 0:
                continue
            trade_amount = round(price * n)
            fee = round(trade_amount * fee_rate)
            existing = next(iter(p for p in await db.vp_positions(ticker) if p["account"] == account), None)
            position = {
                "account": account,
                "ticker": ticker,
                "company_name": company_name,
                "shares": n + (existing["shares"] if existing else 0),
                "cost": trade_amount + fee + (existing["cost"] if existing else 0),
            }
            trade = {
                "account": account,
                "ticker": ticker,
                "company_name": company_name,
                "side": "buy",
                "shares": n,
                "price": price,
                "amount": trade_amount,
                "fee": fee,
            }
            await db.vp_record_trade(trade, -(trade_amount + fee), position)
            fills.append(Fill(account, n, trade_amount, fee))
            if account == "nisa":
                nisa_left -= trade_amount
        cash_after, _ = await _settings()
        return BuyResult(ticker, company_name, price, fills, cash_after, max(0.0, nisa_left))


async def sell(ticker: str, shares: int | None = None, account: str | None = None) -> SellResult:
    """保有を売る。shares を省略すると全株、account を省略すると特定口座から先に売る。"""
    async with _lock:
        positions = await db.vp_positions(ticker)
        if account:
            positions = [p for p in positions if p["account"] == account]
        if not positions:
            where = f"{ACCOUNT_LABELS[account]}で" if account else ""
            raise TradeError(f"{ticker} を{where}保有していません。")
        # 口座の指定がなければ、NISA は長く持つ前提として特定口座から売る
        position = sorted(positions, key=lambda p: p["account"] != "tokutei")[0]
        n = position["shares"] if shares is None else shares
        if n <= 0 or n > position["shares"]:
            raise TradeError(
                f"{ACCOUNT_LABELS[position['account']]}の保有は {position['shares']} 株です（指定: {n} 株）。"
            )

        price = await _latest_price(ticker)
        _, fee_rate = await _settings()
        amount = round(price * n)
        fee = round(amount * fee_rate)
        cost = position["cost"] * n / position["shares"]
        realized = amount - fee - cost

        tax = 0.0
        if position["account"] == "tokutei":
            # 同じ年の損益を相殺したうえでの源泉徴収額の増減（損が出たら還付でマイナスになる）
            before = (await db.vp_year_totals(_year_start(market.now_jst())))["tokutei_realized"]
            tax = round((max(0.0, before + realized) - max(0.0, before)) * TAX_RATE)

        remaining = position["shares"] - n
        trade = {
            "account": position["account"],
            "ticker": ticker,
            "company_name": position["company_name"],
            "side": "sell",
            "shares": n,
            "price": price,
            "amount": amount,
            "fee": fee,
            "realized": realized,
            "tax": tax,
        }
        updated = {**position, "shares": remaining, "cost": position["cost"] - cost} if remaining else None
        await db.vp_record_trade(trade, amount - fee - tax, updated, delete_position=remaining == 0)

        opened = position["opened_at"].astimezone(JST).date()
        cash_after, _ = await _settings()
        return SellResult(
            ticker=ticker,
            company_name=position["company_name"],
            account=position["account"],
            shares=n,
            price=price,
            amount=amount,
            fee=fee,
            cost=cost,
            realized=realized,
            tax=tax,
            held_days=(market.now_jst().date() - opened).days,
            topix_change=await _topix_change(opened),
            cash_after=cash_after,
        )


async def _topix_change(since: date) -> float | None:
    """since から直近までの TOPIX 連動 ETF（1306）の騰落率。"""
    topix = (await market.get_daily(["1306"], years=6)).get("1306")
    if topix is None or topix.empty:
        return None
    before = topix[topix.index.date <= since]
    base = before["Close"].iloc[-1] if not before.empty else topix["Close"].iloc[0]
    return float(topix["Close"].iloc[-1] / base - 1)


async def summary() -> Summary:
    positions = await db.vp_positions()
    tickers = sorted({p["ticker"] for p in positions})
    daily = await market.get_daily(tickers) if tickers else {}
    holdings = [
        Holding(
            p["account"],
            p["ticker"],
            p["company_name"],
            p["shares"],
            p["cost"],
            float(daily[p["ticker"]]["Close"].iloc[-1]) if p["ticker"] in daily else None,
        )
        for p in positions
    ]
    settings = await db.get_all_settings()
    totals = await db.vp_year_totals(_year_start(market.now_jst()))
    started_at = datetime.fromisoformat(settings["vp_started_at"])
    return Summary(
        cash=float(settings["vp_cash"]),
        holdings=holdings,
        nisa_left=max(0.0, NISA_ANNUAL_LIMIT - totals["nisa_bought"]),
        year_tax=totals["tax"],
        year_fee=totals["fee"],
        started_at=started_at,
        topix_change=await _topix_change(started_at.astimezone(JST).date()),
    )


def title_for(result: SellResult) -> str:
    rate = result.return_rate
    if rate >= 0.10:
        return "🥇 利確の達人"
    if rate >= 0:
        return "👍 堅実な利確"
    if rate > -0.05:
        return "✂️ 潔い損切り"
    return "💀 耐えきれず撤退"
