"""大胆 AI。慎重 AI（ai_trader）と同じ候補から、急騰・出来高の急増・新しい材料をきっかけに、場中に素早く売買する。

- 判断の時刻: 取引日の 10:00・11:00・13:00・14:00・15:00。約 20 分遅れの株価で、その場で約定する（自分の場中の売買と同じ条件）
- きっかけ（どれか）に当たった銘柄だけ Jev に聞く: 前日比 +3% 以上、出来高が 20 日平均の 2 倍以上、プラス材料の新しい提案
- Jev に短期（数日）と長期（数か月以上）の確信度を聞き、長期向きなら NISA、短期向きなら特定口座で買う（NISA の枠を短期売買で使わない）
- 短期（特定口座）の保有: 利益確定 +6%、損切り -4%、含み益が出た後に最高値から -3%、最長 5 営業日で売る
- 長期（NISA）の保有: 慎重 AI と同じく -15% で損切りし、大引け後に Jev の「売るべきか」で見直す（翌取引日の始値で売る）。
  買ってから portfolio.NISA_MIN_HOLD_DAYS 営業日は損切り以外で売らない（Jev にも聞かない）
- 売買しすぎを防ぐ: 買いは 1 日 2 件まで、保有は 6 銘柄まで、売った銘柄は 3 営業日買い直さない、
  税金・手数料を引いても見込みがプラスのときだけ買う、同じ銘柄を Jev に聞くのは 1 日 1 回まで、Jev の呼び出しは 1 日の上限まで
- その日の判断（売買・見送り）は 1 日分を ai_decisions にまとめ、翌取引日の朝に慎重 AI の判断と並べて知らせる
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta

import pandas as pd

import ai_trader
import db
import market
import orders
import portfolio
from jev_client import JevJudge
from market import JST

log = logging.getLogger(__name__)

OWNER = "ai_bold"
LABEL = "⚡ 大胆AI"
SLOT_TIMES = [time(10), time(11), time(13), time(14), time(15)]
AMOUNT = 300_000  # 1 回の購入額
MAX_POSITIONS = 6  # 保有する銘柄数の上限
MAX_BUYS_PER_DAY = 2
COOLDOWN_DAYS = 3  # 売ってから、この営業日数は同じ銘柄を買い直さない
MAX_WATCH = 30  # 場中に株価を見る候補の上限（yfinance の呼び出しを抑える）
RISE_TRIGGER = 0.03  # 前日比がこれ以上ならきっかけ
VOLUME_TRIGGER = 2.0  # 今日の出来高が 20 日平均のこの倍以上ならきっかけ
NEWS_TRIGGER_DAYS = 1  # 提案からこの営業日数以内なら「新しい材料」
SHORT_THRESHOLD = 0.60  # 短期の確信度がこれ以上なら特定口座で買う
LONG_THRESHOLD = 0.60  # 長期の確信度がこれ以上（かつ短期以上）なら NISA で買う
TAKE_PROFIT = 0.06
STOP_LOSS = -0.04
TRAILING_STOP = 0.03  # 含み益が出た後、保有中の最高値からこれだけ下がったら売る
MAX_SHORT_DAYS = 5  # 短期の保有の最長（営業日）
LONG_SELL_THRESHOLD = 0.80  # 長期（NISA）の保有を、大引け後の Jev の「売る」の確信度がこれ以上なら売る（NISA の枠は売っても戻らないので高め）
STYLE_LABELS = {"tokutei": "短期（特定口座）", "nisa": "長期（NISA）"}
CRITERIA = (
    f"買い: 短期の確信度 {SHORT_THRESHOLD:.0%} 以上（特定口座）・ 長期 {LONG_THRESHOLD:.0%} 以上（NISA） ・ "
    f"長期の保有の売り: 確信度 {LONG_SELL_THRESHOLD:.0%} 以上"
)


def _empty_record() -> dict:
    return {"mode": LABEL, "criteria": CRITERIA, "judged": 0, "asked": [], "entries": []}


def _has_today(df: pd.DataFrame | None, today) -> bool:
    return df is not None and not df.empty and df.index[-1].date() == today


def worth_it(confidence: float, fee_rate: float) -> bool:
    """短期の買いの見込みが、税金・手数料を引いてもプラスか。

    確信度の確率で利益確定（+6%、税引後）、残りで損切り（-4%）になるとみなし、往復の手数料を引く。
    """
    expected = confidence * TAKE_PROFIT * (1 - portfolio.TAX_RATE) + (1 - confidence) * STOP_LOSS - 2 * fee_rate
    return expected > 0


def triggers(df: pd.DataFrame, candidate: ai_trader.Candidate, today) -> list[str]:
    """買いを考えるきっかけ。どれにも当たらなければ Jev に聞かない。"""
    found = []
    change = float(df["Close"].iloc[-1] / df["Close"].iloc[-2] - 1)
    if change >= RISE_TRIGGER:
        found.append(f"前日比 {change:+.1%}")
    volumes = df["Volume"].iloc[-21:-1].dropna()
    today_volume = df["Volume"].iloc[-1]
    if len(volumes) >= 10 and volumes.mean() > 0 and pd.notna(today_volume):
        ratio = float(today_volume / volumes.mean())
        if ratio >= VOLUME_TRIGGER:
            found.append(f"出来高 20 日平均の {ratio:.1f} 倍")
    news = candidate.news
    if news and market.trading_days_between(news["created_at"].astimezone(JST).date(), today) <= NEWS_TRIGGER_DAYS:
        found.append("新しいプラス材料")
    return found


def sell_reason(position: dict, df: pd.DataFrame, today) -> str | None:
    """場中に売る理由。売らなければ None。"""
    price = float(df["Close"].iloc[-1])
    per_share = position["cost"] / position["shares"]
    rate = price / per_share - 1
    if position["account"] == "nisa":
        return f"損切り（{rate:+.1%}）" if rate <= ai_trader.STOP_LOSS else None
    if rate >= TAKE_PROFIT:
        return f"利益確定（{rate:+.1%}）"
    if rate <= STOP_LOSS:
        return f"損切り（{rate:+.1%}）"
    opened = position["opened_at"].astimezone(JST).date()
    peak = float(df[df.index.date >= opened]["Close"].max())
    if peak > per_share and price <= peak * (1 - TRAILING_STOP):
        return f"最高値から {price / peak - 1:+.1%}（含み益 {peak / per_share - 1:+.1%} の後の下落）"
    held = market.trading_days_between(opened, today)
    if held >= MAX_SHORT_DAYS:
        return f"最長保有（{held} 営業日）"
    return None


async def _use_jev(today) -> bool:
    """Jev を 1 回使ってよければ、使った回数を数えて True。1 日の上限に達していたら False。"""
    settings = await db.get_all_settings()
    limit = int(settings.get("bold_jev_daily_limit") or 60)
    day, _, count = settings.get("_bold_jev_calls", "").partition(":")
    used = int(count) if day == today.isoformat() and count else 0
    if used >= limit:
        return False
    await db.set_setting("_bold_jev_calls", f"{today.isoformat()}:{used + 1}")
    return True


async def _recently_sold(now: datetime) -> set[str]:
    since = now - timedelta(days=COOLDOWN_DAYS * 2 + 4)
    return {
        t["ticker"]
        for t in await db.vp_trades(OWNER, since)
        if t["side"] == "sell"
        and market.trading_days_between(t["traded_at"].astimezone(JST).date(), now.date()) <= COOLDOWN_DAYS
    }


async def run_slot(jev: JevJudge, now: datetime) -> list[orders.Executed]:
    """場中の判断。売る条件に当たった保有を売り、きっかけのあった候補を Jev に聞いて買う（その場の株価で約定する）。"""
    if not portfolio.in_live_session(now):
        return []
    today = now.date()
    stamp = f"{now:%H:%M}"
    record = await db.ai_get_decisions(OWNER, today) or _empty_record()
    pending_sells = {o["ticker"] for o in await db.vp_orders("open", OWNER) if o["side"] == "sell"}
    positions = [p for p in await db.vp_positions(OWNER) if p["ticker"] not in pending_sells]
    candidates = list((await ai_trader.collect_candidates(now, OWNER)).values())[:MAX_WATCH]
    tickers = sorted({p["ticker"] for p in positions} | {c.ticker for c in candidates})
    if not tickers:
        return []
    daily = await market.get_daily(tickers)
    executed: list[orders.Executed] = []

    # ---- 売り（短期のルール、長期は損切りだけ）
    for p in positions:
        df = daily.get(p["ticker"])
        if not _has_today(df, today):
            continue  # 今日の株価がまだない
        reason = sell_reason(p, df, today)
        if reason is None:
            continue
        order = {
            "ticker": p["ticker"],
            "company_name": p["company_name"],
            "side": "sell",
            "reason": reason,
            "confidence": None,
            "intraday": True,
        }
        try:
            result = await portfolio.sell(OWNER, p["ticker"], float(df["Close"].iloc[-1]), None, p["account"], reason)
        except portfolio.TradeError as exc:
            executed.append(orders.Executed(order, None, f"売れませんでした（{exc}）"))
            continue
        except Exception:
            log.exception("大胆 AI の売りでエラー: %s（%s）", p["ticker"], p["account"])
            note = "エラーで、売れたかどうかを確かめられませんでした。`/portfolio` で大胆 AI の保有を確かめてください"
            executed.append(orders.Executed(order, None, note))
            continue
        executed.append(orders.Executed(order, result))
        record["entries"].append(
            {
                "ticker": p["ticker"],
                "name": p["company_name"],
                "action": "sell",
                "confidence": None,
                "note": f"{stamp} {reason}",
                "facts": f"{STYLE_LABELS[p['account']]} ・ 損益 {result.return_rate:+.1%}",
            }
        )

    # ---- 買い
    held = {p["ticker"] for p in await db.vp_positions(OWNER)}
    day_start = datetime.combine(today, time(0), JST)
    bought_today = len({t["ticker"] for t in await db.vp_trades(OWNER, day_start) if t["side"] == "buy"})
    buys_left = min(MAX_BUYS_PER_DAY - bought_today, MAX_POSITIONS - len(held))
    if buys_left > 0:
        executed += await _buy(jev, now, stamp, record, candidates, daily, held, buys_left)

    await db.ai_save_decisions(OWNER, today, record)
    if executed:
        log.info("大胆 AI の売買（%s）: %s", stamp, [(e.order["side"], e.order["ticker"]) for e in executed])
    return executed


async def _buy(
    jev: JevJudge,
    now: datetime,
    stamp: str,
    record: dict,
    candidates: list[ai_trader.Candidate],
    daily: dict[str, pd.DataFrame],
    held: set[str],
    buys_left: int,
) -> list[orders.Executed]:
    today = now.date()
    asked = set(record["asked"])
    cooling = await _recently_sold(now)
    settings = await db.get_all_settings()
    fee_rate = float(settings["vp_fee_rate"])
    nisa_left = await portfolio.nisa_room(OWNER, now)

    judged = []  # (候補, 口座 or None, 確信度, 短期, 長期, きっかけ)
    for c in candidates:
        df = daily.get(c.ticker)
        if c.ticker in held or c.ticker in asked or c.ticker in cooling or not _has_today(df, today) or len(df) < 80:
            continue
        found = triggers(df, c, today)
        if not found:
            continue
        if not await _use_jev(today):
            log.info("大胆 AI: Jev の 1 日の上限に達したため、残りの候補は聞きません")
            break
        asked.add(c.ticker)
        c.features = ai_trader.features(df)
        state = {
            "company": c.name,
            "sources": [ai_trader.SOURCE_LABELS[s] for s in c.sources],
            "triggers": found,
            "news": ai_trader.news_state(c.news),
            **c.features,
        }
        try:
            short, long_ = await jev.should_buy_bold(state)
        except Exception:
            log.warning("Jev の買い判断（大胆 AI）に失敗: %s", c.ticker, exc_info=True)
            continue
        record["judged"] += 1
        if long_ >= LONG_THRESHOLD and long_ >= short and nisa_left >= AMOUNT * 0.5:
            judged.append((c, "nisa", long_, short, long_, found))
        elif short >= SHORT_THRESHOLD and worth_it(short, fee_rate):
            judged.append((c, "tokutei", short, short, long_, found))
        else:
            judged.append((c, None, max(short, long_), short, long_, found))
    record["asked"] = sorted(asked)

    executed = []
    cash = float(settings[db.CASH_KEYS[OWNER]])
    for c, account, confidence, short, long_, found in sorted(judged, key=lambda j: j[2], reverse=True):
        entry = {
            "ticker": c.ticker,
            "name": c.name,
            "sources": [ai_trader.SOURCE_LABELS[s] for s in c.sources],
            "confidence": confidence,
            "facts": f"短期 {short:.0%} ・ 長期 {long_:.0%} ・ きっかけ: {'、'.join(found)} ・ {ai_trader.facts(c.features)}",
        }
        if account is None:
            record["entries"].append({**entry, "action": "pass", "note": f"{stamp} 買いの基準に届かない"})
            continue
        if buys_left <= 0:
            record["entries"].append({**entry, "action": "pass", "note": f"{stamp} 1 日に買う上限・保有の上限に達した"})
            continue
        if cash < AMOUNT * 0.5:
            record["entries"].append({**entry, "action": "pass", "note": f"{stamp} 現金が足りない"})
            continue
        amount = min(AMOUNT, cash, nisa_left) if account == "nisa" else min(AMOUNT, cash)
        reason = f"{STYLE_LABELS[account]} ・ きっかけ: {'、'.join(found)}"
        order = {
            "ticker": c.ticker,
            "company_name": c.name,
            "side": "buy",
            "reason": reason,
            "confidence": confidence,
            "source": c.sources[0],
            "intraday": True,
            "style": account,
        }
        price = float(daily[c.ticker]["Close"].iloc[-1])
        try:
            result = await portfolio.buy(OWNER, c.ticker, c.name, amount, price, reason, confidence, account=account)
        except portfolio.TradeError as exc:
            executed.append(orders.Executed(order, None, f"買えませんでした（{exc}）"))
            record["entries"].append({**entry, "action": "pass", "note": f"{stamp} 買えなかった（{exc}）"})
            continue
        except Exception:
            log.exception("大胆 AI の買いでエラー: %s", c.ticker)
            note = "エラーで、買えたかどうかを確かめられませんでした。`/portfolio` で大胆 AI の保有を確かめてください"
            executed.append(orders.Executed(order, None, note))
            continue
        executed.append(orders.Executed(order, result))
        record["entries"].append({**entry, "action": "buy", "note": f"{stamp} {STYLE_LABELS[account]}"})
        buys_left -= 1
        cash = result.cash_after
        if account == "nisa":
            nisa_left = result.nisa_left
    return executed


async def after_close(jev: JevJudge, now: datetime) -> None:
    """大引け後: 長期（NISA）の保有を Jev で見直して翌取引日の始値で売る注文を出し、保有の状態を判断の記録に残す。"""
    today = now.date()
    record = await db.ai_get_decisions(OWNER, today) or _empty_record()
    pending_sells = {o["ticker"] for o in await db.vp_orders("open", OWNER) if o["side"] == "sell"}
    positions = [p for p in await db.vp_positions(OWNER) if p["ticker"] not in pending_sells]
    daily = await market.get_daily(sorted({p["ticker"] for p in positions}), refresh=False) if positions else {}
    for p in positions:
        df = daily.get(p["ticker"])
        if df is None or len(df) < 80:
            continue
        f = ai_trader.features(df)
        rate = float(df["Close"].iloc[-1]) * p["shares"] / p["cost"] - 1 if p["cost"] else 0.0
        held = market.trading_days_between(p["opened_at"].astimezone(JST).date(), today)
        entry = {
            "ticker": p["ticker"],
            "name": p["company_name"],
            "facts": f"{STYLE_LABELS[p['account']]} ・ 含み損益 {rate:+.1%} ・ 保有 {held} 営業日 ・ {ai_trader.facts(f)}",
        }
        if p["account"] != "nisa":
            record["entries"].append({**entry, "action": "hold", "confidence": None, "note": "短期: 明日も場中のルールで判断"})
            continue
        reason, confidence = None, None
        lock_left = portfolio.nisa_lock_left([p], today)
        if rate <= ai_trader.STOP_LOSS:
            reason = f"損切り（{rate:+.1%}）"
        elif lock_left:
            note = f"NISA の最低保有期間中（あと {lock_left} 営業日は損切り以外で売らない）"
            record["entries"].append({**entry, "action": "hold", "confidence": None, "note": note})
            continue
        elif await _use_jev(today):
            holding = {"return_rate": round(rate, 4), "held_trading_days": held, "account": portfolio.ACCOUNT_LABELS[p["account"]]}
            state = {"company": p["company_name"], "holding": holding, **f}
            try:
                confidence = await jev.should_sell(state)
                record["judged"] += 1
            except Exception:
                log.warning("Jev の売り判断（大胆 AI）に失敗: %s", p["ticker"], exc_info=True)
            if confidence is not None and confidence >= LONG_SELL_THRESHOLD:
                reason = "Jev の判断"
        if reason is None:
            record["entries"].append({**entry, "action": "hold", "confidence": confidence, "note": None})
            continue
        await orders.place_sell(OWNER, p["ticker"], None, p["account"], reason, confidence)
        record["entries"].append({**entry, "action": "sell", "confidence": confidence, "note": f"大引け後 {reason}（翌取引日の始値で売る）"})
    await db.ai_save_decisions(OWNER, today, record)
