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
import reports
import signals
import views
from jev_client import JevJudge
from market import JST
from relations import RelationGraph
from ticker_master import TickerMaster

log = logging.getLogger("bot")

MAX_PROPOSALS_PER_RUN = 10
RELATED_PER_ORIGIN = 3
MAX_RELATED_CANDIDATES = 30
MAX_RELATED_PROPOSALS_PER_RUN = 5
MAX_JEV_CANDIDATES = 150
JEV_CONCURRENCY = 5
PROPOSAL_DEDUP_DAYS = 3
SLOT_GRACE = timedelta(minutes=10)

PROPOSAL_TIMES = {"twice": [time(8, 30), time(16, 0)], "once": [time(8, 30)]}
# 東証のデータは約 20 分遅れるため、場中の時刻を少し後ろにずらして実行する
SESSIONS = [(time(9, 15), time(11, 45)), (time(12, 45), time(15, 45))]
HOURLY_TIMES = [time(h) for h in (10, 11, 12, 13, 14, 15)] + [time(15, 45)]
CLOSE_TIME = time(15, 45)
# 大引け後、確定した日足を DB に保存する時刻（データの遅れを見込んで少し遅らせる）
PRICE_SYNC_TIME = time(16, 0)
# 答え合わせなどで後から株価を使うため、直近この日数に提案した銘柄の日足も保存しておく
PRICE_SYNC_PROPOSAL_DAYS = 35
# 定番レポートの時刻。朝は 8:30 の提案ジョブの後、大引けは 16:00 の日足保存の後にする
REPORT_TIMES = {"morning": time(8, 45), "close": time(16, 5), "weekly": time(16, 10)}


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


def price_sync_slots(day: datetime) -> list[datetime]:
    return [datetime.combine(day.date(), PRICE_SYNC_TIME, JST)] if market.is_trading_day(day.date()) else []


def report_slots(kind: str, day: datetime) -> list[datetime]:
    d = day.date()
    if kind == "weekly":
        ok = market.is_last_trading_day_of_week(d)
    else:
        ok = market.is_trading_day(d)
    return [datetime.combine(d, REPORT_TIMES[kind], JST)] if ok else []


def due_slot(slots: list[datetime], now: datetime) -> str | None:
    """now の直前（猶予 10 分以内）に予定されていた実行時刻。tick が遅れても取りこぼさないようにする。"""
    due = [s for s in slots if s <= now < s + SLOT_GRACE]
    return due[-1].isoformat() if due else None


class StockBot(ext_commands.Bot):
    def __init__(self) -> None:
        super().__init__(command_prefix=ext_commands.when_mentioned, intents=discord.Intents.default())
        self.master = TickerMaster()
        self.relations = RelationGraph()
        self.jev: JevJudge | None = None
        self._health_runner: web.AppRunner | None = None
        self._proposal_lock = asyncio.Lock()
        self._signal_lock = asyncio.Lock()
        self._master_lock = asyncio.Lock()
        self._relations_lock = asyncio.Lock()
        self._price_sync_lock = asyncio.Lock()
        self._report_lock = asyncio.Lock()
        self._background: set[asyncio.Task] = set()

    # ------------------------------------------------------------ 起動・終了

    async def setup_hook(self) -> None:
        # ホスティング環境によってはポートが開くまで起動完了とみなさないので、ヘルスチェック用サーバーを最初に起動する
        await self._start_health_server()
        await db.init()
        await self.refresh_master()
        self.jev = JevJudge()

        self.add_dynamic_items(
            views.AddPendingButton, views.SkipPendingButton, views.UnwatchButton, views.VirtualBuyButton
        )
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

        slot = due_slot(price_sync_slots(now), now)
        if slot and slot != settings.get("_last_price_sync_slot") and not self._price_sync_lock.locked():
            await db.set_setting("_last_price_sync_slot", slot)
            self._spawn(self._guarded(self._price_sync_lock, self.run_price_sync_job))

        for kind in reports.BUILDERS:
            if settings.get(f"report_{kind}", "on") != "on":
                continue
            slot = due_slot(report_slots(kind, now), now)
            key = f"_last_report_{kind}_slot"
            if slot and slot != settings.get(key):
                await db.set_setting(key, slot)
                self._spawn(self._guarded(self._report_lock, self.run_report, kind))

        # 関係データは起動直後（DB キャッシュからの読み込み）と、7 日ごとの再取得をここで行う
        if self.relations.is_stale() and not self._relations_lock.locked():
            self._spawn(self._guarded(self._relations_lock, self.relations.load))

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

    async def _report_channel(self) -> discord.abc.Messageable:
        """レポートの送り先。DISCORD_CHANNEL_REPORT が未設定なら売買シグナル用チャンネル。"""
        key = "DISCORD_CHANNEL_REPORT" if os.getenv("DISCORD_CHANNEL_REPORT") else "DISCORD_CHANNEL_SIGNAL"
        return await self._channel(key)

    # ------------------------------------------------------------ 定番レポート

    async def run_report(self, kind: str) -> None:
        embed = await reports.BUILDERS[kind](market.now_jst())
        await (await self._report_channel()).send(embed=embed)
        await db.log_notification("report", detail=kind)

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
            for stock in removed:
                await db.log_notification("delist", stock["ticker"], stock["company_name"])
        except Exception:  # 起動時にも呼ばれるので、通知の失敗で Bot を止めない
            log.exception("自動解除の通知に失敗しました")

    # ------------------------------------------------------------ モジュール1・2: 新銘柄提案

    async def run_proposal_job(self, settings: dict[str, str]) -> dict[str, int]:
        """ニュースを判定して提案を投稿し、各段階の件数を返す（/test proposal の結果表示に使う）。"""
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

        min_positive = float(settings["jev_positive_threshold"])
        min_impact = float(settings["jev_impact_threshold"])
        results = await self._judge_all([(item, info, None) for item, info in candidates])
        passed = [r for r in results if r[-1].is_positive >= min_positive and r[-1].impact >= min_impact]
        passed.sort(key=lambda r: (r[-1].impact, r[-1].is_positive), reverse=True)
        log.info("Jev 判定通過: %d / %d 件", len(passed), len(results))

        # 関連銘柄への波及: 通過した銘柄の取引先・提携先などについて、同じニュースで改めて判定する
        passed_count = len(passed)
        passed = passed[:MAX_PROPOSALS_PER_RUN]  # 投稿しない銘柄の関連銘柄だけが届かないよう、先に絞る
        related = []
        for item, origin, _, _ in passed:
            added = 0
            for rel in self.relations.neighbors(origin.code, origin.name):
                target = self.master.get(rel.code)
                if target is None or target.code in skip:
                    continue  # 監視中・提案済みなどで除外したぶんは、次の候補で埋める
                skip.add(target.code)
                relation = f"{target.name}は{origin.name}の{rel.label}"
                related.append((item, target, (origin, relation)))
                added += 1
                if added == RELATED_PER_ORIGIN:
                    break
        related = related[:MAX_RELATED_CANDIDATES]
        related_results = await self._judge_all(related)
        related_passed = [
            r for r in related_results if r[-1].is_positive >= min_positive and r[-1].impact >= min_impact
        ]
        related_passed.sort(key=lambda r: (r[-1].impact, r[-1].is_positive), reverse=True)
        log.info("関連銘柄の Jev 判定通過: %d / %d 件", len(related_passed), len(related_results))

        channel = await self._channel("DISCORD_CHANNEL_PROPOSAL")
        posted = await self._post_proposals(channel, passed)
        related_posted = await self._post_proposals(channel, related_passed[:MAX_RELATED_PROPOSALS_PER_RUN])
        return {
            "news": len(items),
            "candidates": len(candidates),
            "judged": len(results),
            "passed": passed_count,
            "posted": posted,
            "related_candidates": len(related),
            "related_passed": len(related_passed),
            "related_posted": related_posted,
        }

    async def _judge_all(self, targets: list[tuple]) -> list[tuple]:
        """(記事, 銘柄, 関連情報 or None) の組をまとめて Jev で判定し、成功したものに判定結果を付けて返す。"""
        semaphore = asyncio.Semaphore(JEV_CONCURRENCY)

        async def judge(item, info, ctx):
            async with semaphore:
                try:
                    relation = ctx[1] if ctx else None
                    return item, info, ctx, await self.jev.judge(item.title, item.summary, info.name, relation)
                except Exception:
                    log.warning("Jev 判定に失敗: %s / %s", info.name, item.title, exc_info=True)
                    return None

        return [r for r in await asyncio.gather(*(judge(*t) for t in targets)) if r]

    async def _post_proposals(self, channel, results: list[tuple]) -> int:
        posted = 0
        for item, info, ctx, judgement in results:
            kind = "news" if ctx is None else "related"
            pending_id = await db.add_pending(
                info.code, info.name, item.title, item.url, judgement.is_positive, judgement.impact, kind
            )
            if pending_id is None:
                continue
            posted += 1
            source = f"\n— {item.source}" if item.source else ""
            if ctx is None:
                embed = discord.Embed(
                    title=f"📰 {info.name} ({info.code})",
                    description=f"[{item.title}]({item.url}){source}",
                    color=discord.Color.gold(),
                )
            else:
                origin, relation = ctx
                embed = discord.Embed(
                    title=f"🔗 関連銘柄: {info.name} ({info.code})",
                    description=f"{origin.name} ({origin.code}) のニュースの関連銘柄です。\n[{item.title}]({item.url}){source}",
                    color=discord.Color.teal(),
                )
                embed.add_field(name="関係", value=relation, inline=False)
            embed.add_field(name="プラス材料の確率", value=f"{judgement.is_positive:.0%}")
            embed.add_field(name="インパクト", value=f"{judgement.impact:.2f} / 2")
            embed.add_field(name="市場・業種", value=f"{info.market}\n{info.sector}")
            if ctx is not None:
                embed.set_footer(text="関係データ: JP Market Vis（EDINET 等から自動抽出。誤りを含む場合があります）")
            await channel.send(embed=embed, view=views.proposal_view(pending_id, info.code))
            await db.log_notification("proposal", info.code, item.title)
        return posted

    # ------------------------------------------------------------ 株価の保存（大引け後）

    async def run_price_sync_job(self) -> None:
        """監視銘柄・直近の提案銘柄・指数の確定した日足を DB に保存し、古い日足を削除する。"""
        codes = [s["ticker"] for s in await db.list_monitored()]
        codes += sorted(await db.recently_proposed_tickers(PRICE_SYNC_PROPOSAL_DAYS))
        codes += list(market.INDEX_CODES)
        daily = await market.get_daily(codes)
        pruned = await market.prune_old_prices()
        log.info("日足を保存しました (%d / %d 銘柄、古い日足 %d 行を削除)", len(daily), len(set(codes)), pruned)

    # ------------------------------------------------------------ モジュール3: 売買シグナル

    async def run_signal_job(self) -> None:
        stocks = await db.list_monitored()
        if not stocks:
            return
        daily = await market.get_daily([s["ticker"] for s in stocks])
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

        await self._send_signal(channel, code, name, ind, df, events)
        for e in events:
            sent[e.key] = now.isoformat()
        label = " / ".join(e.label for e in events)
        await db.log_notification("signal", code, label)
        await db.save_signal_state(code, {"state": state, "sent": sent}, last_signal=label)

    async def send_test_signal(self, code: str, name: str) -> bool:
        """シグナルの有無に関係なく、現在の状態をチャート付きで送る（/test signal 用）。監視状態は変更しない。"""
        df = (await market.get_daily([code])).get(code)
        if df is None or len(df) < 80:
            return False
        channel = await self._channel("DISCORD_CHANNEL_SIGNAL")
        await self._send_signal(channel, code, name, signals.compute(df), df, [])
        return True

    async def _send_signal(self, channel, code: str, name: str, ind, df, events: list[signals.Signal]) -> None:
        # チャートは 1 枚ずつ生成してメモリの山を低く保つ
        detail = await asyncio.to_thread(charts.detailed_chart, code, name, ind)
        intraday = await market.fetch_intraday(code)
        multi = await asyncio.to_thread(charts.multi_timeframe_chart, code, name, intraday, df)
        files = [discord.File(detail, filename=f"{code}_technical.png")]
        if multi is not None:
            files.append(discord.File(multi, filename=f"{code}_multi.png"))
        await channel.send(embed=self._signal_embed(code, name, ind, events), files=files, view=views.signal_view(code))

    @staticmethod
    def _signal_embed(code: str, name: str, ind, events: list[signals.Signal]) -> discord.Embed:
        last, prev = ind.iloc[-1], ind.iloc[-2]
        sides = {e.side for e in events}
        if not events:
            title, color = "🧪 テスト通知（現在の状態）", discord.Color.light_grey()
        elif sides == {"BUY"}:
            title, color = "🟢 買いシグナル", discord.Color.green()
        elif sides == {"SELL"}:
            title, color = "🔴 売りシグナル", discord.Color.red()
        else:
            title, color = "🟡 売買シグナル（買い・売り混在）", discord.Color.orange()
        change = (last["Close"] / prev["Close"] - 1) * 100
        embed = discord.Embed(title=f"{title}: {name} ({code})", color=color)
        if events:
            embed.description = "\n".join(f"・{'買い' if e.side == 'BUY' else '売り'}: {e.label}" for e in events)
        else:
            embed.description = "シグナルの有無に関係なく送ったテストです。監視状態は変更していません。"
        embed.add_field(name="終値", value=f"{last['Close']:,.1f} 円 ({change:+.2f}%)")
        embed.add_field(name="RSI(14)", value=f"{last['RSI']:.1f}")
        embed.add_field(name="MACD / シグナル", value=f"{last['MACD']:.2f} / {last['MACD_signal']:.2f}")
        ma = " / ".join(f"{last[c]:,.0f}" if pd.notna(last[c]) else "—" for c in ("MA25", "MA75", "MA200"))
        embed.add_field(name="移動平均 25 / 75 / 200", value=ma, inline=False)
        embed.set_footer(text=f"日足ベース・データは約20分遅れ・{ind.index[-1]:%Y/%m/%d}")
        return embed


REQUIRED_ENV = (
    "DISCORD_TOKEN",
    "DISCORD_CHANNEL_PROPOSAL",
    "DISCORD_CHANNEL_SIGNAL",
    "JEV_API_KEY",
    "DATABASE_URL",
)


async def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("discord.http").setLevel(logging.WARNING)
    # 足りない環境変数があると、該当する処理を実行したときに初めて失敗するので、起動時にまとめて確認する
    if missing := [key for key in REQUIRED_ENV if not os.getenv(key, "").strip()]:
        raise SystemExit(f"環境変数が設定されていません: {', '.join(missing)}")
    bot = StockBot()
    async with bot:
        await bot.start(os.environ["DISCORD_TOKEN"])


if __name__ == "__main__":
    asyncio.run(main())
