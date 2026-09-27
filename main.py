"""AI銘柄自動抽出＆売買タイミング通知 Bot のエントリポイント。

1 プロセスで次の 3 つを動かす:
  - discord.py の Bot
  - aiohttp の /health（死活監視用）
  - 30 秒ごとの tick で、user_settings に従って提案ジョブ・シグナルジョブを起動するスケジューラ
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, time, timedelta

import discord
import pandas as pd
from aiohttp import web
from discord.ext import commands as ext_commands
from discord.ext import tasks
from dotenv import load_dotenv

import charts
import commands
import db
import market
import news
import signals
import views
from jev_client import JevJudge
from market import JST
from ticker_master import TickerMaster

log = logging.getLogger("bot")

MAX_PROPOSALS_PER_RUN = 10
MAX_JEV_CANDIDATES = 150
JEV_CONCURRENCY = 5
PROPOSAL_DEDUP_DAYS = 3
SLOT_GRACE = timedelta(minutes=10)

PROPOSAL_TIMES = {"twice": [time(8, 30), time(16, 0)], "once": [time(8, 30)]}
# 東証のデータは約 20 分遅れるため、場中の時刻を少し後ろにずらして実行する
SESSIONS = [(time(9, 15), time(11, 45)), (time(12, 45), time(15, 45))]
HOURLY_TIMES = [time(h) for h in (10, 11, 12, 13, 14, 15)] + [time(15, 45)]
CLOSE_TIME = time(15, 45)


def proposal_slots(freq: str, day: datetime) -> list[datetime]:
    return [datetime.combine(day.date(), t, JST) for t in PROPOSAL_TIMES.get(freq, [])]


def signal_slots(freq: str, day: datetime) -> list[datetime]:
    if not market.is_trading_day(day.date()):
        return []
    d = day.date()
    if freq == "15m":
        slots = []
        for start, end in SESSIONS:
            t = datetime.combine(d, start, JST)
            while t <= datetime.combine(d, end, JST):
                slots.append(t)
                t += timedelta(minutes=15)
        return slots
    if freq == "1h":
        return [datetime.combine(d, t, JST) for t in HOURLY_TIMES]
    if freq == "close":
        return [datetime.combine(d, CLOSE_TIME, JST)]
    return []


def due_slot(slots: list[datetime], now: datetime) -> str | None:
    """now の直前（猶予 10 分以内）に予定されていた実行時刻。tick が遅れても取りこぼさないようにする。"""
    due = [s for s in slots if s <= now < s + SLOT_GRACE]
    return due[-1].isoformat() if due else None


class StockBot(ext_commands.Bot):
    def __init__(self) -> None:
        super().__init__(command_prefix=ext_commands.when_mentioned, intents=discord.Intents.default())
        self.master = TickerMaster()
        self.jev: JevJudge | None = None
        self._health_runner: web.AppRunner | None = None
        self._proposal_lock = asyncio.Lock()
        self._signal_lock = asyncio.Lock()
        self._master_lock = asyncio.Lock()
        self._background: set[asyncio.Task] = set()

    # ------------------------------------------------------------ 起動・終了

    async def setup_hook(self) -> None:
        # ホスティング環境によってはポートが開くまで起動完了とみなさないので、ヘルスチェック用サーバーを最初に起動する
        await self._start_health_server()
        await db.init()
        await self.refresh_master()
        self.jev = JevJudge()

        self.add_dynamic_items(views.AddPendingButton, views.SkipPendingButton, views.UnwatchButton)
        commands.setup(self.tree)
        if guild_id := os.getenv("DISCORD_GUILD_ID"):
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        self.tick.start()

    async def on_ready(self) -> None:
        log.info("ログインしました: %s (銘柄マスタ %d 件)", self.user, len(self.master))

    async def close(self) -> None:
        self.tick.cancel()
        if self.jev:
            await self.jev.aclose()
        if self._health_runner:
            await self._health_runner.cleanup()
        await db.close()
        await super().close()

    async def _start_health_server(self) -> None:
        async def health(_: web.Request) -> web.Response:
            return web.Response(text="ok" if self.is_ready() else "starting")

        app = web.Application()
        app.router.add_get("/", health)
        app.router.add_get("/health", health)
        self._health_runner = web.AppRunner(app, access_log=None)
        await self._health_runner.setup()
        port = int(os.getenv("PORT", "8080"))
        await web.TCPSite(self._health_runner, "0.0.0.0", port).start()
        log.info("ヘルスチェック用サーバーを起動しました (port %d)", port)

    # ------------------------------------------------------------ スケジューラ

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    @tasks.loop(seconds=30)
    async def tick(self) -> None:
        now = market.now_jst()
        settings = await db.get_all_settings()

        slot = due_slot(proposal_slots(settings["proposal_freq"], now), now)
        if slot and slot != settings.get("_last_proposal_slot") and not self._proposal_lock.locked():
            await db.set_setting("_last_proposal_slot", slot)
            self._spawn(self._guarded(self._proposal_lock, self.run_proposal_job, settings))

        slot = due_slot(signal_slots(settings["signal_freq"], now), now)
        if slot and slot != settings.get("_last_signal_slot") and not self._signal_lock.locked():
            await db.set_setting("_last_signal_slot", slot)
            self._spawn(self._guarded(self._signal_lock, self.run_signal_job))

        if self.master.is_stale() and not self._master_lock.locked():
            self._spawn(self._guarded(self._master_lock, self.refresh_master))

    @tick.before_loop
    async def _before_tick(self) -> None:
        await self.wait_until_ready()

    @tick.error
    async def _tick_error(self, exc: BaseException) -> None:
        log.exception("スケジューラでエラーが発生しました", exc_info=exc)
        self.tick.restart()

    async def _guarded(self, lock: asyncio.Lock, job, *args) -> None:
        async with lock:
            try:
                await job(*args)
            except Exception:
                log.exception("%s でエラーが発生しました", job.__name__)

    async def _channel(self, env_key: str) -> discord.abc.Messageable:
        channel_id = int(os.environ[env_key])
        return self.get_channel(channel_id) or await self.fetch_channel(channel_id)

    # ------------------------------------------------------------ 銘柄一覧の同期

    async def refresh_master(self) -> None:
        """銘柄一覧を読み込み（古ければ JPX から更新し）、上場廃止になった監視銘柄を自動で解除する。"""
        delisted = await self.master.load()
        if not delisted:
            return
        removed = [s for s in await db.list_monitored() if s["ticker"] in delisted]
        if not removed:
            return
        for stock in removed:
            await db.remove_monitored(stock["ticker"])
        log.info("上場廃止のため監視を解除: %s", [s["ticker"] for s in removed])
        embed = discord.Embed(
            title="📤 上場廃止のため監視を自動解除しました",
            description="\n".join(f"・{s['company_name']} ({s['ticker']})" for s in removed),
            color=discord.Color.light_grey(),
        )
        embed.set_footer(text="JPX の上場銘柄一覧から消えた銘柄です")
        try:
            channel = await self._channel("DISCORD_CHANNEL_SIGNAL")
            await channel.send(embed=embed)
        except Exception:  # 起動時にも呼ばれるので、通知の失敗で Bot を止めない
            log.exception("自動解除の通知に失敗しました")

    # ------------------------------------------------------------ モジュール1・2: 新銘柄提案

    async def run_proposal_job(self, settings: dict[str, str]) -> None:
        items = await news.fetch_news()
        skip = {s["ticker"] for s in await db.list_monitored()} | await db.recently_proposed_tickers(
            PROPOSAL_DEDUP_DAYS
        )
        candidates = []
        for item in items:
            for info in self.master.find_in_text(item.title):
                if info.code not in skip:
                    skip.add(info.code)  # 同じ銘柄は 1 記事だけ判定する
                    candidates.append((item, info))
        candidates = candidates[:MAX_JEV_CANDIDATES]
        log.info("Jev 判定対象: %d 件", len(candidates))

        semaphore = asyncio.Semaphore(JEV_CONCURRENCY)

        async def judge(item, info):
            async with semaphore:
                try:
                    return item, info, await self.jev.judge(item.title, item.summary, info.name)
                except Exception:
                    log.warning("Jev 判定に失敗: %s", item.title, exc_info=True)
                    return None

        results = [r for r in await asyncio.gather(*(judge(i, s) for i, s in candidates)) if r]
        min_positive = float(settings["jev_positive_threshold"])
        min_impact = float(settings["jev_impact_threshold"])
        passed = [r for r in results if r[2].is_positive >= min_positive and r[2].impact >= min_impact]
        passed.sort(key=lambda r: (r[2].impact, r[2].is_positive), reverse=True)
        log.info("Jev 判定通過: %d / %d 件", len(passed), len(results))

        channel = await self._channel("DISCORD_CHANNEL_PROPOSAL")
        for item, info, judgement in passed[:MAX_PROPOSALS_PER_RUN]:
            pending_id = await db.add_pending(
                info.code, info.name, item.title, item.url, judgement.is_positive, judgement.impact
            )
            if pending_id is None:
                continue
            embed = discord.Embed(
                title=f"📰 {info.name} ({info.code})",
                description=f"[{item.title}]({item.url})" + (f"\n— {item.source}" if item.source else ""),
                color=discord.Color.gold(),
            )
            embed.add_field(name="プラス材料の確率", value=f"{judgement.is_positive:.0%}")
            embed.add_field(name="インパクト", value=f"{judgement.impact:.2f} / 2")
            embed.add_field(name="市場・業種", value=f"{info.market}\n{info.sector}")
            await channel.send(embed=embed, view=views.proposal_view(pending_id, info.code))

    # ------------------------------------------------------------ モジュール3: 売買シグナル

    async def run_signal_job(self) -> None:
        stocks = await db.list_monitored()
        if not stocks:
            return
        daily = await market.fetch_daily([s["ticker"] for s in stocks])
        channel = await self._channel("DISCORD_CHANNEL_SIGNAL")
        now = market.now_jst()
        for stock in stocks:
            code = stock["ticker"]
            df = daily.get(code)
            if df is None or len(df) < 80:
                log.warning("%s の日足が不足しているため判定をスキップします", code)
                continue
            try:
                await self._check_stock(channel, stock, df, now)
            except Exception:
                log.exception("%s のシグナル判定でエラーが発生しました", code)

    async def _check_stock(self, channel, stock: dict, df, now: datetime) -> None:
        code, name = stock["ticker"], stock["company_name"]
        ind = signals.compute(df)
        state = signals.state_of(ind)
        saved = stock["signal_state"] or {}
        sent: dict[str, str] = saved.get("sent", {})
        events = signals.apply_cooldown(signals.detect(saved.get("state"), state), sent, now)
        if not events:
            await db.save_signal_state(code, {"state": state, "sent": sent})
            return

        # チャートは 1 枚ずつ生成してメモリの山を低く保つ
        detail = await asyncio.to_thread(charts.detailed_chart, code, name, ind)
        intraday = await market.fetch_intraday(code)
        multi = await asyncio.to_thread(charts.multi_timeframe_chart, code, name, intraday, df)
        files = [discord.File(detail, filename=f"{code}_technical.png")]
        if multi is not None:
            files.append(discord.File(multi, filename=f"{code}_multi.png"))

        await channel.send(embed=self._signal_embed(code, name, ind, events), files=files, view=views.signal_view(code))
        for e in events:
            sent[e.key] = now.isoformat()
        label = " / ".join(e.label for e in events)
        await db.save_signal_state(code, {"state": state, "sent": sent}, last_signal=label)

    @staticmethod
    def _signal_embed(code: str, name: str, ind, events: list[signals.Signal]) -> discord.Embed:
        last, prev = ind.iloc[-1], ind.iloc[-2]
        sides = {e.side for e in events}
        if sides == {"BUY"}:
            title, color = "🟢 買いシグナル", discord.Color.green()
        elif sides == {"SELL"}:
            title, color = "🔴 売りシグナル", discord.Color.red()
        else:
            title, color = "🟡 売買シグナル（買い・売り混在）", discord.Color.orange()
        change = (last["Close"] / prev["Close"] - 1) * 100
        embed = discord.Embed(title=f"{title}: {name} ({code})", color=color)
        embed.description = "\n".join(f"・{'買い' if e.side == 'BUY' else '売り'}: {e.label}" for e in events)
        embed.add_field(name="終値", value=f"{last['Close']:,.1f} 円 ({change:+.2f}%)")
        embed.add_field(name="RSI(14)", value=f"{last['RSI']:.1f}")
        embed.add_field(name="MACD / シグナル", value=f"{last['MACD']:.2f} / {last['MACD_signal']:.2f}")
        ma = " / ".join(f"{last[c]:,.0f}" if pd.notna(last[c]) else "—" for c in ("MA25", "MA75", "MA200"))
        embed.add_field(name="移動平均 25 / 75 / 200", value=ma, inline=False)
        embed.set_footer(text=f"日足ベース・データは約20分遅れ・{ind.index[-1]:%Y/%m/%d}")
        return embed


async def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("discord.http").setLevel(logging.WARNING)
    bot = StockBot()
    async with bot:
        await bot.start(os.environ["DISCORD_TOKEN"])


if __name__ == "__main__":
    asyncio.run(main())
