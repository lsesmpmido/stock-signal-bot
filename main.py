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
from aiohttp import web
from discord.ext import commands as ext_commands
from discord.ext import tasks
from dotenv import load_dotenv

import access
import ai_trader
import battle
import charts
import commands
import db
import fiscal
import market
import news
import orders
import portfolio
import quiz
import reports
import signals
import views
import watchlist
from jev_client import CATEGORIES, JevJudge
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
WATCH_NEWS_PER_STOCK = 5  # 監視銘柄 1 社あたり、1 回に判定するニュースの数
MAX_WATCH_NEWS = 40  # 監視銘柄のニュースを 1 回に判定する数の上限（Jev の呼び出しを抑える）
PROPOSAL_DEDUP_DAYS = 3
SLOT_GRACE = timedelta(minutes=10)

PROPOSAL_TIMES = {"twice": [time(8, 30), time(16, 0)], "once": [time(8, 30)]}
# 東証のデータは約 20 分遅れるため、場中の時刻を少し後ろにずらして実行する
SESSIONS = [(time(9, 15), time(11, 45)), (time(12, 45), time(15, 45))]
# 大引け後の判定。15:30 の終値（クロージング・オークション）が約 20 分遅れで届いた後にする
CLOSE_TIME = time(15, 55)
HOURLY_TIMES = [time(h) for h in (10, 11, 12, 13, 14, 15)] + [CLOSE_TIME]
# 大引け後、確定した日足を DB に保存する時刻（データの遅れを見込んで少し遅らせる）
PRICE_SYNC_TIME = time(16, 0)
# 答え合わせなどで後から株価を使うため、直近この日数に提案した銘柄の日足も保存しておく
PRICE_SYNC_PROPOSAL_DAYS = 35
# 定番レポートの時刻。朝は 8:30 の提案ジョブの後、大引けは 16:00 の日足保存の後にする
# 取引時間外に出た注文を始値で約定させる時刻。データの遅れで始値がまだ取れない銘柄は次の時刻に回す
FILL_TIMES = [time(9, 30), time(10, 0), time(11, 0), time(13, 0)]
# 週末の整理タイム（土曜 10:00）
CLEANUP_TIME = time(10, 0)
QUIZ_TIME = time(12, 0)  # 銘柄当てクイズ（取引日。前回の答えを発表してから出題する）
THREAD_TIME = time(9, 0)  # 週末の振り返りスレッド（土曜）
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
        return slots + [datetime.combine(d, CLOSE_TIME, JST)]
    if freq == "1h":
        return [datetime.combine(d, t, JST) for t in HOURLY_TIMES]
    if freq == "close":
        return [datetime.combine(d, CLOSE_TIME, JST)]
    return []


def price_sync_slots(day: datetime) -> list[datetime]:
    return [datetime.combine(day.date(), PRICE_SYNC_TIME, JST)] if market.is_trading_day(day.date()) else []


def fill_slots(day: datetime) -> list[datetime]:
    if not market.is_trading_day(day.date()):
        return []
    return [datetime.combine(day.date(), t, JST) for t in FILL_TIMES]


# 話題銘柄（直近に提案した銘柄）の急騰・急落を調べる時刻。昼休み（後場の株価が届く前）は除く
HOT_TIMES = [time(10, 0), time(11, 0), time(13, 0), time(14, 0), time(15, 0)]
HOT_MOVE = 0.07  # 前日の終値からこれ以上動いたら知らせる
HOT_PROPOSAL_DAYS = 3


def hot_slots(day: datetime) -> list[datetime]:
    return [datetime.combine(day.date(), t, JST) for t in HOT_TIMES] if market.is_trading_day(day.date()) else []


def alert_slots(day: datetime) -> list[datetime]:
    """価格アラートを調べる時刻（取引日の場中、15 分おき）。"""
    return signal_slots("15m", day)


def quiz_slots(day: datetime) -> list[datetime]:
    return [datetime.combine(day.date(), QUIZ_TIME, JST)] if market.is_trading_day(day.date()) else []


def thread_slots(day: datetime) -> list[datetime]:
    return [datetime.combine(day.date(), THREAD_TIME, JST)] if day.weekday() == 5 else []


def cleanup_slots(day: datetime) -> list[datetime]:
    return [datetime.combine(day.date(), CLEANUP_TIME, JST)] if day.weekday() == 5 else []


def report_slots(kind: str, day: datetime) -> list[datetime]:
    d = day.date()
    if kind == "weekly":
        ok = market.is_last_trading_day_of_week(d)
    else:
        ok = market.is_trading_day(d)
    return [datetime.combine(d, REPORT_TIMES[kind], JST)] if ok else []


MAX_EMBEDS_PER_MESSAGE = 10  # Discord の上限: 1 通に埋め込み 10 個まで、合計 6000 文字まで
MAX_EMBED_CHARS_PER_MESSAGE = 6000


def pack_embeds(items: list[tuple]) -> list[list[tuple]]:
    """(埋め込み, 付随データ) の組を、Discord の 1 通の上限に収まるよう順番どおりに分ける。"""
    batches: list[list[tuple]] = []
    size = 0
    for item in items:
        n = len(item[0])
        if not batches or len(batches[-1]) >= MAX_EMBEDS_PER_MESSAGE or size + n > MAX_EMBED_CHARS_PER_MESSAGE:
            batches.append([])
            size = 0
        batches[-1].append(item)
        size += n
    return batches


def due_slot(slots: list[datetime], now: datetime) -> str | None:
    """now の直前（猶予 10 分以内）に予定されていた実行時刻。tick が遅れても取りこぼさないようにする。"""
    due = [s for s in slots if s <= now < s + SLOT_GRACE]
    return due[-1].isoformat() if due else None


class StockBot(ext_commands.Bot):
    def __init__(self) -> None:
        super().__init__(
            command_prefix=ext_commands.when_mentioned, intents=discord.Intents.default(), tree_cls=access.Tree
        )
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
        self._fill_lock = asyncio.Lock()
        self._alert_lock = asyncio.Lock()
        self._hot_lock = asyncio.Lock()
        self._fiscal_lock = asyncio.Lock()
        self._fiscal_checked: datetime | None = None
        self._background: set[asyncio.Task] = set()

    # ------------------------------------------------------------ 起動・終了

    async def setup_hook(self) -> None:
        # ホスティング環境によってはポートが開くまで起動完了とみなさないので、ヘルスチェック用サーバーを最初に起動する
        await self._start_health_server()
        await db.init()
        await self.refresh_master()
        self.jev = JevJudge()

        self.add_dynamic_items(
            views.AddPendingButton,
            views.SkipPendingButton,
            views.UnwatchButton,
            views.VirtualBuyButton,
            views.WhyButton,
            views.QuizButton,
            views.PruneButton,
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

        slot = due_slot(fill_slots(now), now)
        if slot and slot != settings.get("_last_fill_slot") and not self._fill_lock.locked():
            await db.set_setting("_last_fill_slot", slot)
            self._spawn(self._guarded(self._fill_lock, self.run_fill_job))

        slot = due_slot(alert_slots(now), now)
        if slot and slot != settings.get("_last_alert_slot") and not self._alert_lock.locked():
            await db.set_setting("_last_alert_slot", slot)
            self._spawn(self._guarded(self._alert_lock, self.run_alert_job))

        slot = due_slot(hot_slots(now), now)
        if (
            settings["proposal_freq"] != "off"
            and slot
            and slot != settings.get("_last_hot_slot")
            and not self._hot_lock.locked()
        ):
            await db.set_setting("_last_hot_slot", slot)
            self._spawn(self._guarded(self._hot_lock, self.run_hot_job))

        for kind, slots, job in (("quiz", quiz_slots, self.run_quiz), ("thread", thread_slots, self.run_weekly_thread)):
            slot = due_slot(slots(now), now)
            key = f"_last_{kind}_slot"
            if settings.get(f"report_{kind}", "on") == "on" and slot and slot != settings.get(key):
                await db.set_setting(key, slot)
                self._spawn(self._guarded(self._report_lock, job))

        slot = due_slot(cleanup_slots(now), now)
        if settings.get("report_cleanup", "on") == "on" and slot and slot != settings.get("_last_cleanup_slot"):
            await db.set_setting("_last_cleanup_slot", slot)
            self._spawn(self._guarded(self._report_lock, self.run_cleanup))

        for kind in reports.BUILDERS:
            if settings.get(f"report_{kind}", "on") != "on":
                continue
            slot = due_slot(report_slots(kind, now), now)
            key = f"_last_report_{kind}_slot"
            if slot and slot != settings.get(key):
                await db.set_setting(key, slot)
                self._spawn(self._guarded(self._report_lock, self.run_report, kind))

        # 決算日（権利付き最終日の計算用）は 7 日ごとに EDINET から取り直す。確認は 1 時間に 1 回まで
        if not self._fiscal_lock.locked() and (self._fiscal_checked is None or now - self._fiscal_checked > timedelta(hours=1)):
            self._fiscal_checked = now
            self._spawn(self._guarded(self._fiscal_lock, self.refresh_fiscal_ends))

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
        now = market.now_jst()
        embed = await reports.BUILDERS[kind](now)
        if kind == "morning":
            try:
                if link := await reports.surprising_link(self.master, self.relations, now):
                    embed.add_field(name="🧩 意外なつながり", value=link[:1024], inline=False)
            except Exception:  # おまけの欄なので、失敗してもレポートは送る
                log.exception("意外なつながりの作成に失敗しました")
        view = None
        if kind == "close":
            view = views.movers_view(await reports.big_movers())
        channel = await self._report_channel()
        await channel.send(embed=embed, view=view)
        if kind == "morning":
            try:
                await self._send_stock_of_day(channel, now)
            except Exception:  # おまけのメッセージなので、失敗してもレポートは送れている
                log.exception("今日の 1 銘柄の作成に失敗しました")
        await db.log_notification("report", detail=kind)

    async def _send_stock_of_day(self, channel, now: datetime) -> None:
        """今日の 1 銘柄を、チャートと関係の数を添えて紹介する。"""
        pick = await reports.stock_of_day(self.master, self.relations, now)
        if pick is None:
            return
        code, name, reason = pick
        df = (await market.get_daily([code], refresh=False)).get(code)
        if df is None or len(df) < 80:
            return
        ind = signals.compute(df)
        png = await asyncio.to_thread(charts.detailed_chart, code, name, ind)
        info = self.master.get(code)
        embed = discord.Embed(title=f"🎁 今日の 1 銘柄: {name} ({code})", description=reason[:4000], color=discord.Color.gold())
        if info:
            embed.add_field(name="市場・業種", value=f"{info.market}\n{info.sector}")
        month = df["Close"].iloc[-1] / df["Close"].iloc[-21] - 1 if len(df) > 20 else None
        embed.add_field(name="株価", value=f"{df['Close'].iloc[-1]:,.1f} 円" + (f"（1 か月 {month:+.1%}）" if month is not None else ""))
        embed.add_field(name="関係のある上場企業", value=f"{len(self.relations.neighbors(code, name))} 社（`/related {code}` で表示）")
        views.add_indicator_fields(embed, ind)
        embed.set_image(url=f"attachment://{code}_pick.png")
        embed.set_footer(text="監視も保有もしていない銘柄から選んでいます")
        await channel.send(embed=embed, file=discord.File(png, filename=f"{code}_pick.png"), view=views.decided_view(code))
        await db.log_notification("pick", code)

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

    async def notify_splits(self) -> None:
        """株式分割に合わせて直した保有・注文・アラートを知らせる（直したものがない分割は知らせない）。"""
        for event in await db.unnotified_splits():
            detail = event["detail"]
            lines = [
                f"・{portfolio.OWNER_LABELS.get(p['owner'], p['owner'])}の{portfolio.ACCOUNT_LABELS[p['account']]}: "
                f"{p['before']:,} 株 → {p['after']:,} 株"
                for p in detail["positions"]
            ]
            lines += [f"・未約定の売り注文 #{o['id']}: {o['before']:,} 株 → {o['after']:,} 株" for o in detail["orders"]]
            lines += [f"・価格アラート #{a['id']}: {a['before']:,.1f} 円 → {a['after']:,.1f} 円" for a in detail["alerts"]]
            if lines:
                info = self.master.get(event["ticker"])
                name = info.name if info else event["ticker"]
                ratio = event["ratio"]
                embed = discord.Embed(
                    title=f"✂️ {name} ({event['ticker']}) の株式{'分割' if ratio > 1 else '併合'}を反映しました",
                    description=f"{event['ex_date']:%Y/%m/%d} から 1 株 → {ratio:g} 株\n" + "\n".join(lines),
                    color=discord.Color.light_grey(),
                )
                embed.set_footer(text="取得額の合計は変わりません。株価の変化から判定した分割比です")
                channel = await self._report_channel()
                await channel.send(embed=embed)
                await db.log_notification("split", event["ticker"], f"1:{ratio:g}")
            await db.mark_split_notified(event["ticker"], event["ex_date"])

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
        try:
            watch_judged = await self.judge_watch_news()
        except Exception:  # 感情スコアのための判定なので、失敗しても提案の結果は返す
            log.exception("監視銘柄のニュースの判定に失敗しました")
            watch_judged = 0
        return {
            "watch_judged": watch_judged,
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
                    relation = None if ctx in (None, "watch") else ctx[1]
                    j = await self.jev.judge(item.title, item.summary, info.name, relation)
                except Exception:
                    log.warning("Jev 判定に失敗: %s / %s", info.name, item.title, exc_info=True)
                    return None
            kind = "news" if ctx is None else "watch" if ctx == "watch" else "related"
            try:  # 感情スコアの推移（/sentiment）のため、提案しなかった判定も残す
                await db.save_judgement(info.code, item.url, item.title, kind, j.is_positive, j.impact, j.category)
            except Exception:
                log.warning("判定の保存に失敗: %s / %s", info.name, item.title, exc_info=True)
            return item, info, ctx, j

        return [r for r in await asyncio.gather(*(judge(*t) for t in targets)) if r]

    async def judge_watch_news(self) -> int:
        """監視銘柄の直近のニュースを社名で検索し、まだ判定していない記事を Jev で判定して残す（提案はしない）。"""
        targets = []
        for s in await db.list_monitored():
            info = self.master.get(s["ticker"])
            if info is None:
                continue
            try:
                items = await news.search_company(info.name, days=1, limit=WATCH_NEWS_PER_STOCK)
            except Exception:
                log.warning("監視銘柄のニュースを検索できませんでした: %s", info.name, exc_info=True)
                continue
            done = await db.judged_urls(info.code)
            targets += [(item, info, "watch") for item in items if item.url not in done]
        results = await self._judge_all(targets[:MAX_WATCH_NEWS])
        log.info("監視銘柄のニュースを判定: %d 件", len(results))
        return len(results)

    async def _post_proposals(self, channel, results: list[tuple]) -> int:
        posted = 0
        for item, info, ctx, judgement in results:
            kind = "news" if ctx is None else "related"
            pending_id = await db.add_pending(
                info.code, info.name, item.title, item.url, judgement.is_positive, judgement.impact, kind, judgement.category
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
            embed.add_field(name="材料の種類", value=CATEGORIES[judgement.category][0])
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
        """監視銘柄・直近の提案銘柄・仮想の保有銘柄・AI の候補・指数の確定した日足を DB に保存し、古い日足を削除する。"""
        codes = [s["ticker"] for s in await db.list_monitored()]
        codes += sorted(await db.recently_proposed_tickers(PRICE_SYNC_PROPOSAL_DAYS))
        for owner in portfolio.OWNER_LABELS:
            codes += [p["ticker"] for p in await db.vp_positions(owner)]
        # 自分が最近買った銘柄は、売った後も AI の候補に残る。AI はこの後の判断で保存済みの日足を使うので、ここで最新にしておく
        since = market.now_jst() - timedelta(days=ai_trader.YOUR_BUY_WINDOW * 2)
        codes += [t["ticker"] for t in await db.vp_trades("you", since) if t["side"] == "buy"]
        codes += list(market.INDEX_CODES)
        daily = await market.get_daily(codes)
        pruned = await market.prune_old_prices()
        log.info("日足を保存しました (%d / %d 銘柄、古い日足 %d 行を削除)", len(daily), len(set(codes)), pruned)
        try:
            await self.notify_splits()
        except Exception:
            log.exception("株式分割の通知に失敗しました")
        await self.save_snapshots()
        try:
            await self.run_ai_trader()
        except Exception:
            log.exception("AI トレーダーの判断でエラーが発生しました")

    async def run_reset(self, plan: battle.StartPlan, now: datetime) -> bool:
        """勝負をやり直す。AI の判断（日足保存の後）や注文の約定の途中なら、やり直さずに False を返す。

        途中でやり直すと、やり直す前の保有・現金にもとづく注文や判断が、新しい口座に入ってしまうため。
        やり直しの間は両方のロックを持ち、スケジューラが同じ時刻にこれらのジョブを始めないようにする
        （ロック中はその時刻を実行済みにせず、次の tick で改めて始める）。
        """
        if self._price_sync_lock.locked() or self._fill_lock.locked():
            return False
        async with self._price_sync_lock, self._fill_lock:
            await battle.apply_start(plan, now)
        return True

    async def current_mode(self) -> ai_trader.Mode:
        return await battle.current_mode()

    async def run_ai_trader(self) -> ai_trader.Decisions:
        """大引け後に AI が売買を判断し、翌取引日の始値で約定する注文を出す（判断の内容は翌取引日の朝に知らせる）。"""
        return await ai_trader.decide(self.jev, await self.current_mode(), market.now_jst())

    async def save_snapshots(self) -> None:
        """両チームの今日の総資産を記録する（AI との勝負の月ごとの勝敗に使う）。"""
        today = market.now_jst().date()
        for owner in portfolio.OWNER_LABELS:
            s = await portfolio.summary(owner, refresh=False)  # 直前の日足の保存で、確定した株価を保存済み
            await db.vp_save_snapshot(owner, today, s.total_value, s.deposits)

    # ------------------------------------------------------------ 価格アラート・週末の整理タイム

    async def run_alert_job(self) -> None:
        alerts = await db.list_alerts()
        if not alerts:
            return
        daily = await market.get_daily(sorted({a["ticker"] for a in alerts}))
        hits = watchlist.check_alerts(alerts, daily)
        if not hits:
            return
        channel = await self._channel("DISCORD_CHANNEL_SIGNAL")
        for hit in hits:
            a = hit.alert
            if not await db.close_alert(a["id"], triggered=True):
                continue  # 同時に取り消されていた
            word = "超えました" if a["direction"] == "above" else "割りました"
            embed = discord.Embed(
                title=f"⏰ {a['company_name']} ({a['ticker']}) が {a['target']:,.1f} 円を{word}",
                description=f"アラートを作ってからの{'高値' if a['direction'] == 'above' else '安値'} {hit.price:,.1f} 円（約 20 分遅れ）",
                color=discord.Color.orange(),
            )
            mention = f"<@{a['created_by']}>" if a["created_by"] else None
            await channel.send(content=mention, embed=embed, view=views.decided_view(a["ticker"]))
            await db.log_notification("alert", a["ticker"], f"{a['target']:.1f}")

    async def refresh_fiscal_ends(self) -> None:
        if not db.is_stale(await db.fiscal_ends_fetched_at()):
            return
        ends = await fiscal.download()
        if len(ends) < 1000:  # 形式の変更などで取れなかったときは、古いキャッシュを残す
            raise RuntimeError(f"決算日の件数が少なすぎます（{len(ends)} 件）")
        await db.replace_fiscal_ends(ends)
        log.info("決算日（EDINET コードリスト）を更新しました (%d 社)", len(ends))

    async def run_hot_job(self) -> None:
        """直近に提案した銘柄（監視していないもの）のうち、今日大きく動いた銘柄を 1 日 1 回だけ知らせる。"""
        now = market.now_jst()
        watched = {s["ticker"] for s in await db.list_monitored()}
        proposals: dict[str, dict] = {}
        for p in await db.list_pending_since(now - timedelta(days=HOT_PROPOSAL_DAYS)):
            if p["ticker"] not in watched:
                proposals[p["ticker"]] = p  # 同じ銘柄は新しい提案を使う
        if not proposals:
            return
        sent = {n["ticker"] for n in await db.notifications_since(reports._day_start(now), "hot")}
        daily = await market.get_daily(sorted(proposals))
        channel = None
        for code, p in proposals.items():
            df = daily.get(code)
            if code in sent or df is None or len(df) < 2 or df.index[-1].date() != now.date():
                continue
            change = float(df["Close"].iloc[-1] / df["Close"].iloc[-2] - 1)
            if abs(change) < HOT_MOVE:
                continue
            channel = channel or await self._channel("DISCORD_CHANNEL_PROPOSAL")
            up = change > 0
            embed = discord.Embed(
                title=f"{'⚡ 話題銘柄が急騰' if up else '🧊 話題銘柄が急落'}: {p['company_name']} ({code}) {change:+.1%}",
                description=(
                    f"今 {df['Close'].iloc[-1]:,.1f} 円（約 20 分遅れ）・前日の終値 {df['Close'].iloc[-2]:,.1f} 円\n"
                    f"{p['created_at'].astimezone(JST):%m/%d} の提案: {p['news_title']}"
                )[:4000],
                color=discord.Color.red() if up else discord.Color.dark_blue(),
            )
            if p.get("category") in CATEGORIES:
                embed.add_field(name="材料の種類", value=CATEGORIES[p["category"]][0])
            embed.set_footer(text=f"直近 {HOT_PROPOSAL_DAYS} 日に提案した、監視していない銘柄だけを見ています（1 銘柄 1 日 1 回まで）")
            view = views.decided_view(code)
            view.add_item(views.WhyButton(code))
            await channel.send(embed=embed, view=view)
            await db.log_notification("hot", code, f"{change:+.3f}")

    async def run_quiz(self) -> None:
        """前回までのクイズの答えを発表してから、今日のクイズを出題する。"""
        now = market.now_jst()
        channel = await self._report_channel()
        for q in await db.unrevealed_quizzes(now.date()):
            await channel.send(embed=views.quiz_answer_embed(q, await db.quiz_answers(q["id"])))
            await db.mark_quiz_revealed(q["id"])
        question = await quiz.make_question(self.master, now)
        if question is None:
            log.info("クイズに出せる銘柄がありません（監視銘柄・保有銘柄・直近の提案銘柄がない）")
            return
        quiz_id = await db.add_quiz(now.date(), question.code, question.name, question.choices, question.change)
        if quiz_id is None:
            return  # 今日はもう出題した
        png = await asyncio.to_thread(charts.quiz_chart, question.chart)
        embed = discord.Embed(
            title=f"🧩 銘柄当てクイズ（{now:%m/%d}）",
            description=(
                f"この約 6 か月の値動きは、どの銘柄でしょう？（{question.change:+.0%}）\n"
                f"ヒント: 業種は **{question.sector}**\n答えは次の取引日の 12:00 に発表します（1 人 1 回）"
            ),
            color=discord.Color.purple(),
        )
        embed.set_image(url="attachment://quiz.png")
        await channel.send(
            embed=embed, file=discord.File(png, filename="quiz.png"), view=views.quiz_view(quiz_id, question.choices)
        )
        await db.log_notification("quiz", question.code)

    async def run_weekly_thread(self) -> None:
        """土曜に「今週の振り返り」のメッセージを送り、そこから自分のメモを書き込めるスレッドを作る。"""
        now = market.now_jst()
        week_start = reports._week_start(now)
        channel = await self._report_channel()
        embed = await reports.weekly_look_back(now)
        message = await channel.send(embed=embed)
        name = f"📝 今週の振り返り（{week_start:%m/%d}〜{now - timedelta(days=1):%m/%d}）"
        try:
            thread = await message.create_thread(name=name, auto_archive_duration=10080)
        except discord.Forbidden:
            await channel.send(
                "⚠️ スレッドを作る権限がないため、振り返りスレッドを作れませんでした。"
                "Bot に「公開スレッドの作成」「スレッドでメッセージを送信」の権限を付けてください（README の手順 1-3）。"
            )
            return
        await thread.send(
            "今週の売買や、監視していて気づいたことを自由に書き込んでください。\n"
            "例: なぜその銘柄を買った（見送った）か、うまくいったこと・次に気をつけたいこと"
        )
        await db.log_notification("thread", detail=week_start.date().isoformat())

    async def run_cleanup(self) -> None:
        candidates = await watchlist.cleanup_candidates(market.now_jst())
        if not candidates:
            return
        embed, view = views.cleanup_message(candidates, await db.count_monitored(), await views.watch_limit())
        await (await self._report_channel()).send(embed=embed, view=view)
        await db.log_notification("cleanup", detail=",".join(c.ticker for c in candidates))

    # ------------------------------------------------------------ 入金

    async def deposit(self, amount: float) -> None:
        """自分と AI に同じ額を入金し、レポート用チャンネルに知らせる（/deposit から呼ぶ）。"""
        await db.vp_deposit(amount)
        total = float((await db.get_all_settings())[db.DEPOSIT_KEYS["you"]])
        embed = discord.Embed(
            title="💴 入金しました",
            description=(
                f"あなたと AI の仮想口座に **{amount:,.0f} 円** ずつ入金しました。\n入金額の合計は {total:,.0f} 円です。"
            ),
            color=discord.Color.gold(),
        )
        embed.set_footer(text="成績は入金額の合計に対する損益で計算します（月ごとの勝敗では、その月の入金分を除きます）")
        await (await self._report_channel()).send(embed=embed)
        await db.log_notification("deposit", detail=f"{amount:.0f}")

    # ------------------------------------------------------------ 注文の約定（取引時間外の注文を翌取引日の始値で）

    async def run_fill_job(self) -> None:
        """注文を約定させ、AI の前日の判断を知らせ、AI の取引時間中の損切りをする。"""
        now = market.now_jst()
        executed = await orders.fill_open_orders(now)
        mine = [e for e in executed if e.order["owner"] == "you"]
        ai = [e for e in executed if e.order["owner"] == "ai"]
        # 約定はもう済んでいるので、ここから先の失敗で約定の通知を落とさないよう、通知ごとに失敗を受け止める
        # 前日の判断は、寄り付き（始値での約定）の後に知らせる。判断の直後に知らせると、AI の注文を見て同じ値段で買えてしまう
        try:
            decisions = await db.ai_unreported_decisions(now.date())
        except Exception:
            log.exception("AI の判断の読み込みに失敗しました")
            decisions = []
        try:
            stops = await ai_trader.intraday_stop_loss(now)
        except Exception:
            log.exception("AI の取引時間中の損切りでエラーが発生しました")
            stops = []
        if not (mine or ai or decisions or stops):
            return
        channel = await self._report_channel()
        if mine:
            try:
                await channel.send(embed=views.fills_embed(mine, "you"))
                await db.log_notification("fill", detail=f"{len(mine)} 件")
            except Exception:  # 自分の約定の通知に失敗しても、AI の売買・判断・損切りの通知は送る
                log.exception("約定の通知に失敗しました")
        try:
            mode = await self.current_mode()
        except Exception:  # 性格は見出しに出すだけなので、分からなければ通常として知らせる
            log.exception("AI の性格の取得に失敗しました")
            mode = ai_trader.MODES["normal"]
        # (埋め込み, 送れたら通知済みにする判断) の組。約定した売買と判断は、Discord の上限に収まる範囲で 1 通にまとめる
        items = [(views.ai_fills_embed(ai, mode, now), None)] if ai else []
        for d in decisions:
            try:
                items.append((views.ai_decisions_embed(d), d))
            except Exception:
                # 表示できない記録で毎回失敗し続けないよう、通知済みにして飛ばす（記録は ai_decisions に残る）
                log.exception("AI の判断（%s）を表示できませんでした", d["decided_on"])
                await db.ai_mark_reported(d["decided_on"])
        for batch in pack_embeds(items):
            try:
                await channel.send(embeds=[embed for embed, _ in batch])
            except Exception:  # 送れなかった判断は通知済みにせず、次の約定処理で送り直す
                log.exception("AI の売買・判断の通知に失敗しました")
                continue
            for embed, d in batch:
                if d is None:
                    for e in ai:
                        await db.log_notification("ai_trade", e.order["ticker"], e.order["side"])
                else:
                    await db.ai_mark_reported(d["decided_on"])
                    await db.log_notification("ai_decisions", detail=d["decided_on"].isoformat())
        if stops:
            title = f"🤖 AIが取引時間中に損切りしました（{now:%m/%d %H:%M}）"
            await channel.send(embed=views.ai_fills_embed(stops, mode, now, title=title))
            for e in stops:
                await db.log_notification("ai_trade", e.order["ticker"], "stop_loss")

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

        await self._send_signal(channel, code, name, ind, df, events, stock)
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

    async def _send_signal(
        self, channel, code: str, name: str, ind, df, events: list[signals.Signal], stock: dict | None = None
    ) -> None:
        # チャートは 1 枚ずつ生成してメモリの山を低く保つ
        detail = await asyncio.to_thread(charts.detailed_chart, code, name, ind)
        intraday = await market.fetch_intraday(code)
        multi = await asyncio.to_thread(charts.multi_timeframe_chart, code, name, intraday, df)
        files = [discord.File(detail, filename=f"{code}_technical.png")]
        if multi is not None:
            files.append(discord.File(multi, filename=f"{code}_multi.png"))
        embed = self._signal_embed(code, name, ind, events)
        content = None
        if stock and stock.get("memo"):
            embed.add_field(name="📝 メモ", value=stock["memo"], inline=False)
        if stock and stock.get("starred"):
            # お気に入りの銘柄は目立たせ、⭐ を付けた人にメンションする
            embed.title = f"⭐ {embed.title}"
            embed.color = discord.Color.gold()
            content = f"<@{stock['star_user_id']}>" if stock.get("star_user_id") else None
        await channel.send(content=content, embed=embed, files=files, view=views.signal_view(code))

    @staticmethod
    def _signal_embed(code: str, name: str, ind, events: list[signals.Signal]) -> discord.Embed:
        sides = {e.side for e in events}
        if not events:
            title, color = "🧪 テスト通知（現在の状態）", discord.Color.light_grey()
        elif sides == {"BUY"}:
            title, color = "🟢 買いシグナル", discord.Color.green()
        elif sides == {"SELL"}:
            title, color = "🔴 売りシグナル", discord.Color.red()
        else:
            title, color = "🟡 売買シグナル（買い・売り混在）", discord.Color.orange()
        embed = discord.Embed(title=f"{title}: {name} ({code})", color=color)
        if events:
            embed.description = "\n".join(f"・{'買い' if e.side == 'BUY' else '売り'}: {e.label}" for e in events)
        else:
            embed.description = "シグナルの有無に関係なく送ったテストです。監視状態は変更していません。"
        views.add_indicator_fields(embed, ind)
        if events and (past := views.past_signals_text(ind, events)):
            embed.add_field(name="📚 過去の似た局面（この銘柄で同じシグナルが出た後）", value=past, inline=False)
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
