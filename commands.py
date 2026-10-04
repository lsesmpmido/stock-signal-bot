"""スラッシュコマンド: /settings, /watch add|remove|list|memo|star|tag, /alert add|list|remove, /buy, /sell, /portfolio, /orders, /battle, /deposit, /reset, /chart, /ranking, /compare, /related, /map, /sentiment, /review, /test proposal|signal|report"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import discord
import pandas as pd
from discord import app_commands

import battle
import charts
import db
import market
import orders
import portfolio
import reports
import signals
import review
import views
import watchlist
from jev_client import CATEGORIES
from ticker_master import CODE_PATTERN, normalize_code, sector_etf

TEST_SIGNAL_TIMEOUT = 300  # 秒


@app_commands.command(name="settings", description="通知頻度（新銘柄提案・売買シグナル）を変更します")
@app_commands.default_permissions(manage_guild=True)
async def settings_command(interaction: discord.Interaction) -> None:
    settings = await db.get_all_settings()
    await interaction.response.send_message(
        views.settings_text(settings), view=views.SettingsView(settings), ephemeral=True
    )


async def _monitored_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    current = current.strip()
    return [
        app_commands.Choice(name=f"{s['ticker']} {s['company_name']}"[:100], value=s["ticker"])
        for s in await db.list_monitored()
        if current in s["ticker"] or current in s["company_name"]
    ][:25]


class WatchGroup(
    app_commands.Group,
    name="watch",
    description="監視銘柄の追加・解除・一覧",
    default_permissions=discord.Permissions(manage_guild=True),
):
    @app_commands.command(name="add", description="証券コードを指定して監視対象に追加します")
    @app_commands.describe(code="証券コード（例: 7203, 130A）")
    async def add(self, interaction: discord.Interaction, code: str) -> None:
        code = normalize_code(code)
        if info := interaction.client.master.get(code):
            name, detail = info.name, f"{info.market}・{info.sector}"
        else:
            # JPX の一覧は月 1 回程度しか更新されないため、新規上場銘柄は株価データで上場を確認する
            if not CODE_PATTERN.fullmatch(code):
                await interaction.response.send_message(f"❌ `{code}` は証券コードの形式ではありません。", ephemeral=True)
                return
            await interaction.response.defer(thinking=True)  # yfinance の確認は 3 秒を超えることがある
            try:
                name = await market.lookup_listed_name(code)
            except Exception:
                await interaction.followup.send("⚠️ 株価データを確認できませんでした。時間をおいて再度お試しください。")
                return
            if name is None:
                await interaction.followup.send(f"❌ 証券コード `{code}` の上場銘柄は見つかりませんでした。")
                return
            detail = "JPX の銘柄一覧に未掲載のため、株価データで上場を確認しました（新規上場銘柄など）"

        send = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
        if code in {s["ticker"] for s in await db.list_monitored()}:
            await send(f"ℹ️ {name} ({code}) はすでに監視中です。")
            return
        if await views.watch_is_full():
            view = await views.ReplaceWatchView.create(code, name, "manual")
            if interaction.response.is_done():
                # 公開で「考え中」にした後の最初の送信は、その表示を置き換えるので「自分だけに表示」にできない。
                # 先に公開の短いお知らせで置き換え、入れ替えのメニューは次の送信で実行した人だけに見せる
                await send(f"ℹ️ 監視枠がいっぱいのため、{name} ({code}) と入れ替える銘柄を選んでください。")
            await send(await views.replace_prompt(code, name), view=view, ephemeral=True)
            return
        await db.add_monitored(code, name, "manual")
        embed = discord.Embed(
            title=f"👀 {name} ({code}) を監視対象に追加しました",
            description=f"{detail}\n次回のシグナル判定から監視を始めます。",
            color=discord.Color.green(),
        )
        await send(embed=embed, view=views.link_view(code))

    @app_commands.command(name="remove", description="監視対象から外します")
    @app_commands.describe(code="証券コード")
    @app_commands.autocomplete(code=_monitored_autocomplete)
    async def remove(self, interaction: discord.Interaction, code: str) -> None:
        code = normalize_code(code)
        removed = await db.remove_monitored(code)
        if removed is None:
            await interaction.response.send_message(f"ℹ️ `{code}` は監視対象ではありません。", ephemeral=True)
            return
        await interaction.response.send_message(f"🗑️ {removed['company_name']} ({code}) を監視対象から外しました。")

    @app_commands.command(name="list", description="監視中の銘柄を一覧表示します")
    @app_commands.describe(tag="タグで絞り込む")
    @app_commands.choices(tag=[app_commands.Choice(name=v, value=k) for k, v in watchlist.TAG_LABELS.items()])
    async def list_(self, interaction: discord.Interaction, tag: app_commands.Choice[str] | None = None) -> None:
        stocks = views.sort_watch(await db.list_monitored())
        if tag:
            stocks = [s for s in stocks if s["tag"] == tag.value]
        await interaction.response.send_message(
            embed=views.watch_list_embed(stocks, await views.watch_limit()),
            view=views.WatchListView(stocks),
            ephemeral=True,
        )

    @app_commands.command(name="memo", description="監視銘柄に「なぜ監視したか」のメモを残します（空にすると削除）")
    @app_commands.describe(code="証券コード", memo="メモ（200 字まで）")
    @app_commands.autocomplete(code=_monitored_autocomplete)
    async def memo(
        self, interaction: discord.Interaction, code: str, memo: app_commands.Range[str, 0, 200] = ""
    ) -> None:
        code = normalize_code(code)
        if not await db.update_monitored(code, memo=memo.strip() or None):
            await interaction.response.send_message(f"ℹ️ `{code}` は監視対象ではありません。", ephemeral=True)
            return
        text = f"📝 {code} のメモを保存しました: {memo.strip()}" if memo.strip() else f"📝 {code} のメモを削除しました。"
        await interaction.response.send_message(text, ephemeral=True)

    @app_commands.command(name="star", description="お気に入り（⭐）を付け外しします。⭐ の銘柄のシグナルではメンションします")
    @app_commands.describe(code="証券コード")
    @app_commands.autocomplete(code=_monitored_autocomplete)
    async def star(self, interaction: discord.Interaction, code: str) -> None:
        code = normalize_code(code)
        stock = next((s for s in await db.list_monitored() if s["ticker"] == code), None)
        if stock is None:
            await interaction.response.send_message(f"ℹ️ `{code}` は監視対象ではありません。", ephemeral=True)
            return
        starred = not stock["starred"]
        await db.update_monitored(code, starred=starred, star_user_id=interaction.user.id if starred else None)
        text = (
            f"⭐ {stock['company_name']} ({code}) をお気に入りにしました。シグナルが出たらメンションでお知らせします。"
            if starred
            else f"☆ {stock['company_name']} ({code}) のお気に入りを外しました。"
        )
        await interaction.response.send_message(text, ephemeral=True)

    @app_commands.command(name="tag", description="監視銘柄にタグ（長期・短期・様子見）を付けます")
    @app_commands.describe(code="証券コード", tag="タグ")
    @app_commands.choices(
        tag=[app_commands.Choice(name=v, value=k) for k, v in watchlist.TAG_LABELS.items()]
        + [app_commands.Choice(name="なし", value="none")]
    )
    @app_commands.autocomplete(code=_monitored_autocomplete)
    async def tag(self, interaction: discord.Interaction, code: str, tag: app_commands.Choice[str]) -> None:
        code = normalize_code(code)
        if not await db.update_monitored(code, tag=None if tag.value == "none" else tag.value):
            await interaction.response.send_message(f"ℹ️ `{code}` は監視対象ではありません。", ephemeral=True)
            return
        await interaction.response.send_message(f"🏷️ {code} のタグを「{tag.name}」にしました。", ephemeral=True)


async def _alert_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[int]]:
    return [
        app_commands.Choice(
            name=f"#{a['id']} {a['company_name']} {a['target']:,.0f}円{'超え' if a['direction'] == 'above' else '割れ'}"[:100],
            value=a["id"],
        )
        for a in await db.list_alerts()
        if current in str(a["id"]) or current in a["company_name"] or current in a["ticker"]
    ][:25]


class AlertGroup(
    app_commands.Group,
    name="alert",
    description="価格アラート（指定した価格を超えた・割ったら通知）",
    default_permissions=discord.Permissions(manage_guild=True),
):
    @app_commands.command(name="add", description="指定した価格を超えた（今より上なら）・割った（今より下なら）ときに通知します")
    @app_commands.describe(code="証券コード", price="価格（円）")
    async def add(self, interaction: discord.Interaction, code: str, price: app_commands.Range[float, 0.1]) -> None:
        async def body():
            c, name = await _resolve(interaction, code)
            df = await _daily(c, name, min_bars=1)
            now = float(df["Close"].iloc[-1])
            if price == now:
                raise CommandError(f"今の株価（{now:,.1f} 円）と同じ価格は指定できません。")
            direction = "above" if price > now else "below"
            # 今日の日足があれば、その時点の高値・安値を残す（今日すでについた値でアラートが鳴らないようにする）
            today = df.index[-1].date() == market.now_jst().date()
            base_high = float(df["High"].iloc[-1]) if today else None
            base_low = float(df["Low"].iloc[-1]) if today else None
            alert_id = await db.add_alert(c, name, price, direction, interaction.user.id, base_high, base_low)
            word = "超えたら" if direction == "above" else "割ったら"
            text = f"⏰ #{alert_id} {name} ({c}) が {price:,.1f} 円を{word}お知らせします（今 {now:,.1f} 円）。"
            return {"content": text}

        await _run(interaction, True, body)

    @app_commands.command(name="list", description="設定中の価格アラートを表示します")
    async def list_(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(embed=views.alerts_embed(await db.list_alerts()), ephemeral=True)

    @app_commands.command(name="remove", description="価格アラートを取り消します")
    @app_commands.describe(alert_id="アラート")
    @app_commands.autocomplete(alert_id=_alert_autocomplete)
    async def remove(self, interaction: discord.Interaction, alert_id: int) -> None:
        ok = await db.close_alert(alert_id, triggered=False)
        text = f"🗑️ アラート #{alert_id} を取り消しました。" if ok else f"アラート #{alert_id} は見つからないか、終了済みです。"
        await interaction.response.send_message(text, ephemeral=True)


class TestGroup(
    app_commands.Group,
    name="test",
    description="動作確認用: 定期実行を待たずにすぐ動かします",
    default_permissions=discord.Permissions(manage_guild=True),
):
    @app_commands.command(name="proposal", description="ニュース収集〜提案の投稿を今すぐ実行します")
    async def proposal(self, interaction: discord.Interaction) -> None:
        bot = interaction.client
        if bot._proposal_lock.locked():
            await interaction.response.send_message("⏳ 提案ジョブはすでに実行中です。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)  # ニュース収集と Jev 判定に数十秒かかる
        async with bot._proposal_lock:
            try:
                stats = await bot.run_proposal_job(await db.get_all_settings())
            except Exception as exc:
                await interaction.followup.send(f"⚠️ 提案ジョブでエラーが発生しました: `{exc}`", ephemeral=True)
                raise
        text = (
            f"✅ 提案ジョブを実行しました\n"
            f"ニュース {stats['news']} 件 → 銘柄が見つかった候補 {stats['candidates']} 件 → "
            f"Jev 判定 {stats['judged']} 件 → しきい値通過 {stats['passed']} 件 → 投稿 {stats['posted']} 件\n"
            f"関連銘柄: 候補 {stats['related_candidates']} 件 → しきい値通過 {stats['related_passed']} 件 → "
            f"投稿 {stats['related_posted']} 件\n"
            f"監視銘柄のニュース（感情スコア用）: Jev 判定 {stats['watch_judged']} 件"
        )
        if stats["posted"] + stats["related_posted"] == 0:
            text += "\n（直近 3 日以内に提案済み・監視中の銘柄は除外しています）"
        await interaction.followup.send(text, ephemeral=True)

    @app_commands.command(name="report", description="定番レポートを今すぐ送ります（ON/OFF の設定に関係なく）")
    @app_commands.describe(kind="送るレポート")
    @app_commands.choices(
        kind=[app_commands.Choice(name=label, value=key) for key, label in reports.REPORT_LABELS.items()]
    )
    async def report(self, interaction: discord.Interaction, kind: app_commands.Choice[str]) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await interaction.client.run_report(kind.value)
        except Exception as exc:
            await interaction.followup.send(f"⚠️ レポートの作成でエラーが発生しました: `{exc}`", ephemeral=True)
            raise
        await interaction.followup.send(f"✅ {kind.name}を送りました。", ephemeral=True)

    @app_commands.command(name="signal", description="指定した銘柄のチャート付き通知を、シグナルの有無に関係なく送ります")
    @app_commands.describe(code="証券コード（例: 7203）")
    async def signal(self, interaction: discord.Interaction, code: str) -> None:
        code = normalize_code(code)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            info = interaction.client.master.get(code)
            name = info.name if info else await market.lookup_listed_name(code)
            if name is None:
                await interaction.followup.send(f"❌ 証券コード `{code}` の上場銘柄は見つかりませんでした。", ephemeral=True)
                return
            ok = await asyncio.wait_for(interaction.client.send_test_signal(code, name), TEST_SIGNAL_TIMEOUT)
        except asyncio.TimeoutError:
            await interaction.followup.send(
                "⚠️ テスト通知がタイムアウトしました。株価データの取得（yfinance）が止まっている可能性があります。"
                "実行環境のログを確認してください。",
                ephemeral=True,
            )
            return
        except Exception as exc:
            # 例外のまま終わると「考え中…」の表示が残り続けるので、必ず結果を返す
            await interaction.followup.send(f"⚠️ テスト通知の送信でエラーが発生しました: `{exc}`", ephemeral=True)
            raise
        if not ok:
            await interaction.followup.send(f"⚠️ {name} ({code}) の株価データを取得できませんでした。", ephemeral=True)
            return
        await interaction.followup.send(f"✅ {name} ({code}) のテスト通知を送りました。", ephemeral=True)


# ---------------------------------------------------------------- 仮想売買


async def _held_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    current = current.strip()
    seen = {}
    for p in await db.vp_positions("you"):
        seen.setdefault(p["ticker"], p["company_name"])
    return [
        app_commands.Choice(name=f"{code} {name}"[:100], value=code)
        for code, name in seen.items()
        if current in code or current in name
    ][:25]


@app_commands.command(name="buy", description="仮想で買います（NISA の枠を優先し、超える分は特定口座）")
@app_commands.describe(code="証券コード（例: 7203）", amount="購入金額（万円）。省略すると既定額（20 万円）")
@app_commands.default_permissions(manage_guild=True)
async def buy_command(interaction: discord.Interaction, code: str, amount: float | None = None) -> None:
    code = normalize_code(code)
    await interaction.response.defer(thinking=True)
    try:
        if info := interaction.client.master.get(code):
            name = info.name
        elif not CODE_PATTERN.fullmatch(code):
            raise portfolio.TradeError(f"`{code}` は証券コードの形式ではありません。")
        # JPX の一覧にない（新規上場など）なら株価データで上場を確かめる。見つからないコードの注文は受け付けない
        elif (name := await market.lookup_listed_name(code)) is None:
            raise portfolio.TradeError(f"証券コード `{code}` の上場銘柄は見つかりませんでした。")
        yen = amount * 10_000 if amount is not None else await portfolio.default_amount()
        if yen <= 0:
            raise portfolio.TradeError("購入金額は 0 より大きくしてください。")
        placed = await orders.place_buy("you", code, name, yen)
    except portfolio.TradeError as exc:
        await interaction.followup.send(f"❌ {exc}")
        return
    except Exception as exc:
        await interaction.followup.send(f"⚠️ 仮想購入でエラーが発生しました: `{exc}`")
        raise
    await interaction.followup.send(embed=views.placed_embed(placed, "buy", code, name))


@app_commands.command(name="sell", description="仮想で売ります（株数を省略すると全部。口座を省略すると特定口座から先に売る）")
@app_commands.describe(
    code="証券コード",
    shares="売る株数（省略すると全部）",
    account="売る口座（省略すると両方の口座から。特定口座から先に売り、足りない分を NISA から売る）",
)
@app_commands.choices(
    account=[app_commands.Choice(name=label, value=key) for key, label in portfolio.ACCOUNT_LABELS.items()]
)
@app_commands.autocomplete(code=_held_autocomplete)
@app_commands.default_permissions(manage_guild=True)
async def sell_command(
    interaction: discord.Interaction,
    code: str,
    shares: app_commands.Range[int, 1] | None = None,
    account: app_commands.Choice[str] | None = None,
) -> None:
    await interaction.response.defer(thinking=True)
    code = normalize_code(code)
    positions = await db.vp_positions("you", code)
    if account:
        positions = [p for p in positions if p["account"] == account.value]
    positions.sort(key=lambda p: p["account"] != "tokutei")  # NISA は長く持つ前提として、特定口座から先に売る
    total = sum(p["shares"] for p in positions)
    if not positions:
        where = f"{account.name}で" if account else ""
        await interaction.followup.send(f"❌ その銘柄を{where}保有していません。")
        return
    if shares is not None and shares > total:
        await interaction.followup.send(f"❌ 保有は {total:,} 株です（指定: {shares:,} 株）。")
        return
    name = positions[0]["company_name"]  # 売った後に銘柄名を調べに行かない（そこで失敗すると、売れたのにエラーに見える）
    embeds, remaining = [], total if shares is None else shares
    for p in positions:
        if remaining <= 0:
            break
        n = min(remaining, p["shares"])
        try:
            placed = await orders.place_sell("you", code, n, p["account"])
        except portfolio.TradeError as exc:
            embeds.append(discord.Embed(description=f"❌ {portfolio.ACCOUNT_LABELS[p['account']]}: {exc}"))
            break
        except Exception as exc:
            text = f"⚠️ {portfolio.ACCOUNT_LABELS[p['account']]}の仮想売却でエラーが発生しました: `{exc}`"
            await interaction.followup.send(text, embeds=embeds)
            raise
        embeds.append(views.placed_embed(placed, "sell", code, name))
        remaining -= n
    await interaction.followup.send(embeds=embeds)


@app_commands.command(name="portfolio", description="仮想ポートフォリオの保有・損益・NISA 枠の残りを表示します")
@app_commands.describe(team="表示するチーム（既定: あなた）")
@app_commands.choices(team=[app_commands.Choice(name="あなた", value="you"), app_commands.Choice(name="AI", value="ai")])
@app_commands.default_permissions(manage_guild=True)
async def portfolio_command(interaction: discord.Interaction, team: app_commands.Choice[str] | None = None) -> None:
    await interaction.response.defer(thinking=True)
    try:
        summary = await portfolio.summary(team.value if team else "you")
    except Exception as exc:
        await interaction.followup.send(f"⚠️ ポートフォリオの取得でエラーが発生しました: `{exc}`")
        raise
    await interaction.followup.send(embed=views.portfolio_embed(summary))


@app_commands.command(name="review", description="過去の提案が当たっていたかを答え合わせします")
@app_commands.describe(period="評価する期間（既定: 1週間）")
@app_commands.choices(
    period=[app_commands.Choice(name="1週間（5営業日後）", value=5), app_commands.Choice(name="1か月（20営業日後）", value=20)]
)
@app_commands.default_permissions(manage_guild=True)
async def review_command(interaction: discord.Interaction, period: app_commands.Choice[int] | None = None) -> None:
    await interaction.response.defer(thinking=True)
    try:
        result = await review.build(period.value if period else 5)
    except Exception as exc:
        await interaction.followup.send(f"⚠️ 答え合わせでエラーが発生しました: `{exc}`")
        raise
    await interaction.followup.send(embed=views.review_embed(result))


@app_commands.command(name="orders", description="取引時間外に出した未約定の注文を表示・取り消します")
@app_commands.default_permissions(manage_guild=True)
async def orders_command(interaction: discord.Interaction) -> None:
    open_orders = await db.vp_orders("open", "you")
    await interaction.response.send_message(
        embed=views.orders_embed(open_orders), view=views.OrdersView(open_orders), ephemeral=True
    )


@app_commands.command(name="battle", description="AI との勝負の状況（今月の途中経過・通算成績・AI の性格）を表示します")
@app_commands.default_permissions(manage_guild=True)
async def battle_command(interaction: discord.Interaction) -> None:
    await interaction.response.defer(thinking=True)
    try:
        st = await battle.standing()
        you, ai = await portfolio.summary("you"), await portfolio.summary("ai")
    except Exception as exc:
        await interaction.followup.send(f"⚠️ 勝負の状況の取得でエラーが発生しました: `{exc}`")
        raise
    await interaction.followup.send(embed=views.battle_embed(st, you, ai))


@app_commands.command(name="deposit", description="仮想口座に入金します（あなたと AI に同じ額。いつでも入金できます）")
@app_commands.describe(amount="入金額（万円）")
@app_commands.default_permissions(manage_guild=True)
async def deposit_command(interaction: discord.Interaction, amount: app_commands.Range[float, 0.01, 100000.0]) -> None:
    yen = round(amount * 10_000)
    await interaction.response.defer(ephemeral=True, thinking=True)
    await interaction.client.deposit(yen)
    await interaction.followup.send(f"✅ あなたと AI に **{yen:,} 円** ずつ入金しました。", ephemeral=True)


@app_commands.command(name="reset", description="現金・すでに持っている銘柄を指定して、AI との勝負を同じ条件でやり直します")
@app_commands.default_permissions(manage_guild=True)
async def reset_command(interaction: discord.Interaction) -> None:
    await interaction.response.send_modal(views.ResetModal())


# ---------------------------------------------------------------- 便利コマンド


class CommandError(Exception):
    """利用者に見せるエラー。"""


async def _resolve(interaction: discord.Interaction, code: str) -> tuple[str, str]:
    """証券コードを確かめて (証券コード, 銘柄名) を返す。"""
    code = normalize_code(code)
    if info := interaction.client.master.get(code):
        return code, info.name
    if not CODE_PATTERN.fullmatch(code):
        raise CommandError(f"`{code}` は証券コードの形式ではありません。")
    name = await market.lookup_listed_name(code)
    if name is None:
        raise CommandError(f"証券コード `{code}` の上場銘柄は見つかりませんでした。")
    return code, name


async def _daily(code: str, name: str, years: int = 2, min_bars: int = 80):
    df = (await market.get_daily([code], years=years)).get(code)
    if df is None or len(df) < min_bars:
        raise CommandError(f"{name} ({code}) の株価データを十分に取得できませんでした。")
    return df


async def _run(interaction: discord.Interaction, private: bool, body) -> None:
    """defer → body() → エラーを利用者に返す、の共通処理。body は followup.send に渡す引数の dict を返す。"""
    await interaction.response.defer(ephemeral=private, thinking=True)
    try:
        kwargs = await body()
    except CommandError as exc:
        await interaction.followup.send(f"❌ {exc}", ephemeral=private)
        return
    except Exception as exc:
        await interaction.followup.send(f"⚠️ エラーが発生しました: `{exc}`", ephemeral=private)
        raise
    await interaction.followup.send(ephemeral=private, **kwargs)


PRIVATE_DESC = "自分だけに表示する（既定: チャンネルの全員に表示）"


@app_commands.command(name="chart", description="チャート 2 枚（詳細テクニカル・マルチ時間軸）を表示します")
@app_commands.describe(code="証券コード（例: 7203）", private=PRIVATE_DESC)
@app_commands.default_permissions(manage_guild=True)
async def chart_command(interaction: discord.Interaction, code: str, private: bool = False) -> None:
    async def body():
        c, name = await _resolve(interaction, code)
        df = await _daily(c, name)
        ind = signals.compute(df)
        detail = await asyncio.to_thread(charts.detailed_chart, c, name, ind)
        multi = await asyncio.to_thread(charts.multi_timeframe_chart, c, name, await market.fetch_intraday(c), df)
        files = [discord.File(detail, filename=f"{c}_technical.png")]
        if multi is not None:
            files.append(discord.File(multi, filename=f"{c}_multi.png"))
        return {"embed": views.chart_embed(c, name, ind), "files": files, "view": views.link_view(c)}

    await _run(interaction, private, body)


RANKING_PERIODS = {"day": (1, "今日"), "week": (5, "1週間"), "month": (20, "1か月")}


@app_commands.command(name="ranking", description="監視銘柄と仮想保有銘柄の騰落ランキングを表示します")
@app_commands.describe(period="期間（既定: 今日）", private=PRIVATE_DESC)
@app_commands.choices(period=[app_commands.Choice(name=label, value=key) for key, (_, label) in RANKING_PERIODS.items()])
@app_commands.default_permissions(manage_guild=True)
async def ranking_command(
    interaction: discord.Interaction, period: app_commands.Choice[str] | None = None, private: bool = False
) -> None:
    bars, label = RANKING_PERIODS[period.value if period else "day"]

    async def body():
        watch = await db.list_monitored()
        held = await db.vp_positions("you")
        names = {s["ticker"]: s["company_name"] for s in watch} | {p["ticker"]: p["company_name"] for p in held}
        daily = await market.get_daily(list(names))

        def change(df):
            if df is None or len(df) <= bars:
                return None
            return float(df["Close"].iloc[-1] / df["Close"].iloc[-1 - bars] - 1)

        moves = reports._moves(list(names), names, daily, change)
        title = f"🏆 騰落ランキング（{label}）"
        return {"embed": views.ranking_embed(title, moves, "監視銘柄と、あなたの仮想保有銘柄 ・ 株価は約 20 分遅れ")}

    await _run(interaction, private, body)


COMPARE_PERIODS = {"3m": (63, "3か月"), "6m": (126, "6か月"), "1y": (245, "1年"), "2y": (490, "2年")}


@app_commands.command(name="compare", description="2 銘柄（または銘柄と、その業種の ETF）の値動きを、期間の初日を 100 にそろえて比べます")
@app_commands.describe(
    code1="証券コード 1",
    code2="証券コード 2（省略すると、証券コード 1 の業種の ETF〔TOPIX-17 業種別〕と比べる）",
    period="期間（既定: 6か月）",
    private=PRIVATE_DESC,
)
@app_commands.choices(period=[app_commands.Choice(name=label, value=key) for key, (_, label) in COMPARE_PERIODS.items()])
@app_commands.default_permissions(manage_guild=True)
async def compare_command(
    interaction: discord.Interaction,
    code1: str,
    code2: str | None = None,
    period: app_commands.Choice[str] | None = None,
    private: bool = False,
) -> None:
    bars, label = COMPARE_PERIODS[period.value if period else "6m"]

    async def body():
        c1, n1 = await _resolve(interaction, code1)
        if code2:
            c2, n2 = await _resolve(interaction, code2)
        else:
            # 同業他社の代わりに、業種全体の値動き（業種別 ETF）と比べる
            info = interaction.client.master.get(c1)
            if not (info and (etf := sector_etf(info.sector))):
                raise CommandError(f"{n1} ({c1}) の業種に対応する ETF が見つかりません。比べる銘柄を指定してください。")
            c2, n2 = etf
        if c1 == c2:
            raise CommandError("違う銘柄を 2 つ指定してください。")
        d1, d2 = await _daily(c1, n1, min_bars=2), await _daily(c2, n2, min_bars=2)
        png = await asyncio.to_thread(charts.compare_chart, (c1, n1, d1.tail(bars)), (c2, n2, d2.tail(bars)), label)
        if png is None:
            raise CommandError("2 銘柄に共通する取引日のデータが足りません。")
        # チャートと同じく、2 銘柄に共通する最初の日を起点にする（上場して間もない銘柄と比べるとき、期間が短くなる）
        joined = pd.concat({"a": d1.tail(bars)["Close"], "b": d2.tail(bars)["Close"]}, axis=1).dropna()
        r1, r2 = (float(joined[col].iloc[-1] / joined[col].iloc[0] - 1) for col in ("a", "b"))
        embed = discord.Embed(
            title=f"⚖️ {n1} ({c1}) vs {n2} ({c2})（{label}）",
            description=(
                f"{n1}: **{r1:+.2%}**\n{n2}: **{r2:+.2%}**\n"
                f"起点 {joined.index[0]:%Y/%m/%d} 〜 {joined.index[-1]:%Y/%m/%d}"
            ),
            color=discord.Color.blurple(),
        )
        embed.set_image(url="attachment://compare.png")
        return {"embed": embed, "file": discord.File(png, filename="compare.png")}

    await _run(interaction, private, body)


@app_commands.command(name="related", description="関係データから、その銘柄の関連企業（取引先・親子会社・提携先など）を表示します")
@app_commands.describe(
    code="証券コード（例: 7203）", depth="たどる段階（既定: 1 段階）", private=PRIVATE_DESC
)
@app_commands.choices(
    depth=[
        app_commands.Choice(name="1 段階（関連企業）", value=1),
        app_commands.Choice(name="2 段階（関連企業の関連企業まで）", value=2),
    ]
)
@app_commands.default_permissions(manage_guild=True)
async def related_command(
    interaction: discord.Interaction, code: str, depth: app_commands.Choice[int] | None = None, private: bool = False
) -> None:
    async def body():
        c, name = await _resolve(interaction, code)
        client = interaction.client

        def listed(of: str, of_name: str) -> list:
            # 上場銘柄一覧にない会社（上場廃止など）は、関連銘柄の提案と同じく表示しない
            return [r for r in client.relations.neighbors(of, of_name) if client.master.get(r.code)]

        related = listed(c, name)
        names = {r.code: client.master.get(r.code).name for r in related}
        watched = {s["ticker"] for s in await db.list_monitored()}
        held = {p["ticker"] for p in await db.vp_positions("you")}
        if depth and depth.value == 2:
            seen = {c, *names}
            second = {}
            for r in related[: views.TREE_FIRST]:
                # 2 段階目は、元の銘柄と 1 段階目に出た会社を除き、業績への影響が大きい関係から数社
                rows = [x for x in listed(r.code, names[r.code]) if x.code not in seen][: views.TREE_SECOND]
                seen.update(x.code for x in rows)
                second[r.code] = rows
                names.update({x.code: client.master.get(x.code).name for x in rows})
            embed = views.related_tree_embed(c, name, related, second, names, watched, held)
        else:
            embed = views.related_embed(c, name, related, names, watched, held)
        return {"embed": embed, "view": views.link_view(c)}

    await _run(interaction, private, body)


SENTIMENT_DAYS = 90


@app_commands.command(name="sentiment", description="その銘柄のニュースの感情スコア（Jev のプラス材料の確率）の推移を、株価と並べて表示します")
@app_commands.describe(code="証券コード（例: 7203）", private=PRIVATE_DESC)
@app_commands.default_permissions(manage_guild=True)
async def sentiment_command(interaction: discord.Interaction, code: str, private: bool = False) -> None:
    async def body():
        c, name = await _resolve(interaction, code)
        rows = await db.judgements(c, market.now_jst() - timedelta(days=SENTIMENT_DAYS))
        if not rows:
            raise CommandError(
                f"{name} ({c}) のニュースの判定はまだありません。監視銘柄なら、提案ジョブ（8:30・16:00）のたびにニュースを判定して記録します。"
            )
        judged = pd.DataFrame(rows)
        df = (await market.get_daily([c], refresh=False)).get(c)
        png = await asyncio.to_thread(charts.sentiment_chart, c, name, judged, df)
        recent = judged.tail(5).iloc[::-1]
        embed = discord.Embed(
            title=f"🌡️ {name} ({c}) ニュースの感情スコア（直近 {SENTIMENT_DAYS} 日）",
            description=f"判定した記事 {len(judged)} 件 ・ 平均 {judged['is_positive'].mean():.0%}（50% より上ならプラス寄り）",
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="最近の記事",
            value="\n".join(
                f"・{r.is_positive:.0%} {CATEGORIES.get(r.category, CATEGORIES['other'])[0]} [{r.news_title[:60]}]({r.news_url})"
                for r in recent.itertuples()
            )[:1024],
            inline=False,
        )
        embed.set_image(url="attachment://sentiment.png")
        embed.set_footer(text="Jev の「この銘柄の株価にとってプラス材料か」の確率。提案しなかった記事も含む")
        return {"embed": embed, "file": discord.File(png, filename="sentiment.png")}

    await _run(interaction, private, body)


MAP_NEIGHBORS = 4  # 関係図で、1 銘柄あたりに出す関連企業の数
MAP_PRIMARY = 10  # 関係図に出す監視・保有銘柄の数


@app_commands.command(name="map", description="監視銘柄・保有銘柄と、その関連企業のつながりを図にします")
@app_commands.describe(private=PRIVATE_DESC)
@app_commands.default_permissions(manage_guild=True)
async def map_command(interaction: discord.Interaction, private: bool = False) -> None:
    async def body():
        client = interaction.client
        codes = [s["ticker"] for s in await db.list_monitored()] + [p["ticker"] for p in await db.vp_positions("you")]
        primary = {c: client.master.get(c).name for c in dict.fromkeys(codes) if client.master.get(c)}
        primary = dict(list(primary.items())[:MAP_PRIMARY])
        if not primary:
            raise CommandError("監視銘柄・保有銘柄がありません。`/watch add` や `/buy` で追加すると、関係図を作れます。")
        others, edges = {}, []
        for code, name in primary.items():
            related = [r for r in client.relations.neighbors(code, name) if client.master.get(r.code)]
            for r in related[:MAP_NEIGHBORS]:
                edges.append((code, r.code, r.priority))
                if r.code not in primary:
                    others[r.code] = client.master.get(r.code).name
            # 監視・保有銘柄どうしの関係は、上の件数に関係なく描く
            edges += [(code, r.code, r.priority) for r in related[MAP_NEIGHBORS:] if r.code in primary]
        if not edges:
            raise CommandError("関係データに、監視銘柄・保有銘柄と上場企業との関係は見つかりませんでした。")
        png = await asyncio.to_thread(charts.relation_map, primary, others, edges)
        embed = discord.Embed(
            title="🕸️ 監視・保有銘柄と関連企業の関係図",
            description=f"監視・保有銘柄 {len(primary)} 社と、それぞれ業績への影響が大きい関連企業を {MAP_NEIGHBORS} 社まで描いています。",
            color=discord.Color.teal(),
        )
        embed.set_image(url="attachment://map.png")
        embed.set_footer(text="関係データ: JP Market Vis（EDINET 等から自動抽出。誤りを含む場合があります）")
        return {"embed": embed, "file": discord.File(png, filename="map.png")}

    await _run(interaction, private, body)


def setup(tree: app_commands.CommandTree) -> None:
    for command in (
        settings_command,
        WatchGroup(),
        AlertGroup(),
        TestGroup(),
        buy_command,
        sell_command,
        portfolio_command,
        orders_command,
        battle_command,
        deposit_command,
        reset_command,
        chart_command,
        ranking_command,
        compare_command,
        related_command,
        map_command,
        sentiment_command,
        review_command,
    ):
        # DM には出さない（default_permissions は DM では効かないため）。実行時の確認は access.Tree で行う
        tree.add_command(app_commands.guild_only(command))
