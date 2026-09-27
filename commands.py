"""スラッシュコマンド: /settings, /watch add|remove|list, /test proposal|signal"""

from __future__ import annotations

import asyncio

import discord
from discord import app_commands

import db
import market
import views
from ticker_master import CODE_PATTERN, normalize_code

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
        if not await db.add_monitored(code, name, "manual"):
            await send(f"ℹ️ {name} ({code}) はすでに監視中です。")
            return
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
    async def list_(self, interaction: discord.Interaction) -> None:
        stocks = await db.list_monitored()
        await interaction.response.send_message(
            embed=views.watch_list_embed(stocks), view=views.WatchListView(stocks), ephemeral=True
        )


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
            f"投稿 {stats['related_posted']} 件"
        )
        if stats["posted"] + stats["related_posted"] == 0:
            text += "\n（直近 3 日以内に提案済み・監視中の銘柄は除外しています）"
        await interaction.followup.send(text, ephemeral=True)

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


def setup(tree: app_commands.CommandTree) -> None:
    tree.add_command(settings_command)
    tree.add_command(WatchGroup())
    tree.add_command(TestGroup())
