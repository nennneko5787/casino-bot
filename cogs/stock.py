"""株価: 売買 + 会社設立 + 折れ線チャート + 管理者の銘柄追加。

一般: /stock buy|sell|create|retire|reopen|chart|currency|portfolio|list
管理者: /stock-admin add|delist|relist|params|set-price

会社は1人1社まで (設立手数料1000+投資金、開始株価=投資金、
mu/sigma/impactは投資額ランクで自動決定、
創業者株+売買ロイヤリティ+値上がり配当あり)。
自社株は買増不可・売却のみ可。
価格1が24時間続いた会社は破産 (保有株は紙くず・会社消去)。
"""

import asyncio
import logging
from contextlib import suppress

import discord
from discord import app_commands
from discord.ext import commands, tasks

from objects.exceptions import AmountNotEnough
from services import missions, stocks
from services.admin import admin_only
from services.loan import repay_note
from services.message import amountName, buildAmountText, buildGetAmountText
from services.stock_chart import (
    render_currency_chart,
    render_multi_stock_chart,
    render_stock_chart,
)

logger = logging.getLogger(__name__)

CHART_NAME = "chart.png"
TICK_INTERVAL_MINUTES = 1.0


async def ticker_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """銘柄のオートコンプリート。存在チェック自体は各コマンド側で従来通り行う。"""
    try:
        items = await stocks.get_stocks(active_only=False)
    except Exception:
        logger.exception("銘柄候補の取得に失敗")
        return []
    current = current.strip().upper()
    choices = [
        app_commands.Choice(
            name=f"{s.ticker} ({buildAmountText(s.price)})"
            + ("" if s.is_active else " [取扱停止]"),
            value=s.ticker,
        )
        for s in items
        if current in s.ticker
    ]
    return choices[:25]


class StockCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self.tick_loop.start()

    async def cog_unload(self):
        self.tick_loop.cancel()

    @tasks.loop(minutes=TICK_INTERVAL_MINUTES)
    async def tick_loop(self):
        try:
            _, bankrupted = await stocks.tick_once()
        except Exception:
            logger.exception("株価の定期更新に失敗")
            return
        for info in bankrupted:
            owner = f"<@{info['owner_id']}>" if info["owner_id"] else "運営"
            msg = (
                f"💸 `{info['ticker']}` が破産しました"
                f"（価格1が{stocks.BANKRUPT_FLOOR_HOURS}時間継続）\n"
                f"設立者: {owner}／保有株は紙くずになりました"
            )
            for guild in self.bot.guilds:
                with suppress(Exception):
                    ch = self._notify_channel(guild)
                    if ch is not None:
                        await ch.send(msg)

    @tick_loop.before_loop
    async def _before_tick(self):
        await self.bot.wait_until_ready()

    def _notify_channel(
        self, guild: discord.Guild | None
    ) -> discord.TextChannel | None:
        if guild is None:
            return None
        ch = guild.system_channel
        if ch is not None and ch.permissions_for(guild.me).send_messages:
            return ch
        for c in guild.text_channels:
            if c.permissions_for(guild.me).send_messages:
                return c
        return None

    # ---------- /stock グループ ----------

    @commands.hybrid_group(name="stock", brief="株取引・会社設立をします")
    @commands.guild_only()
    async def stock(self, ctx: commands.Context):
        await ctx.reply(
            "サブコマンドを指定してください: "
            "buy / sell / create / retire / reopen / chart / currency / "
            "portfolio / list",
            ephemeral=True,
        )

    @stock.command(name="list", brief="上場中の銘柄一覧を表示します")
    @commands.guild_only()
    async def stockListCommand(self, ctx: commands.Context):
        items = await stocks.get_stocks(active_only=False)
        lines = []
        for s in items:
            history = await stocks.get_history(s.ticker, 2)
            if len(history) >= 2:
                diff = history[-1][1] - history[-2][1]
                mark = f"({diff:+})"
            else:
                mark = ""
            status = "" if s.is_active else " [取扱停止]"
            owner = f" [U:<@{s.owner_id}>]" if s.owner_id is not None else ""
            lines.append(
                f"`{s.ticker}`: {buildAmountText(s.price)} {mark}{status}{owner}"
            )
        desc = "\n".join(lines) if lines else "銘柄がありません"
        await ctx.reply(
            embed=discord.Embed(
                title="株価一覧📈", description=desc, color=discord.Color.blue()
            )
        )

    @stock.command(name="buy", brief="株を買います")
    @app_commands.rename(ticker="銘柄", qty="数量")
    @app_commands.describe(ticker="例: NEKO", qty="買う株数")
    @app_commands.autocomplete(ticker=ticker_autocomplete)
    @commands.guild_only()
    async def stockBuyCommand(self, ctx: commands.Context, ticker: str, qty: int = 1):
        if qty < 1:
            await ctx.reply("数量は1以上にしてください", ephemeral=True)
            return
        try:
            price, cost, rate, royalty = await stocks.buy(ctx.author.id, ticker, qty)
        except ValueError as e:
            await ctx.reply(str(e), ephemeral=True)
            return
        except LookupError:
            raise AmountNotEnough()
        # ミッション: 株取引を記録 (失敗してもゲームは続行)
        with suppress(Exception):
            await missions.record_event(ctx.author.id, "trade")
            await missions.record_event(ctx.author.id, "trade_buy")
        royalty_note = (
            f"\n創業者ロイヤリティ {buildAmountText(royalty)} が設立者に還元されました"
            if royalty > 0
            else ""
        )
        await ctx.reply(
            embed=discord.Embed(
                title="株を購入📈",
                description=(
                    f"<@{ctx.author.id}>\n"
                    f"`{ticker.strip().upper()}` を {qty}株 @"
                    f"{buildAmountText(price)}\n"
                    f"```patch\n{buildGetAmountText(-cost, md=True)}\n```"
                    f"\n需給影響 {rate:+.2%} で価格が動きました" + royalty_note
                ),
                color=discord.Color.green(),
            )
        )

    @stock.command(name="sell", brief="株を売ります")
    @app_commands.rename(ticker="銘柄", qty="数量")
    @app_commands.describe(ticker="例: NEKO", qty="売る株数")
    @app_commands.autocomplete(ticker=ticker_autocomplete)
    @commands.guild_only()
    async def stockSellCommand(self, ctx: commands.Context, ticker: str, qty: int = 1):
        if qty < 1:
            await ctx.reply("数量は1以上にしてください", ephemeral=True)
            return
        try:
            price, proceeds, repaid, rate, royalty = await stocks.sell(
                ctx.author.id, ticker, qty
            )
        except ValueError as e:
            await ctx.reply(str(e), ephemeral=True)
            return
        # ミッション: 株取引を記録 (失敗してもゲームは続行)
        with suppress(Exception):
            await missions.record_event(ctx.author.id, "trade")
        royalty_note = (
            f"\n創業者ロイヤリティ {buildAmountText(royalty)} が設立者に還元されました"
            if royalty > 0
            else ""
        )
        await ctx.reply(
            embed=discord.Embed(
                title="株を売却💰",
                description=(
                    f"<@{ctx.author.id}>\n"
                    f"`{ticker.strip().upper()}` を {qty}株 @"
                    f"{buildAmountText(price)}\n"
                    f"```patch\n{buildGetAmountText(proceeds, md=True)}\n```"
                    + repay_note(repaid)
                    + f"\n需給影響 {rate:+.2%} で価格が動きました"
                    + royalty_note
                ),
                color=discord.Color.gold(),
            )
        )

    @stock.command(name="chart", brief="株価の折れ線チャートを表示します")
    @app_commands.rename(ticker="銘柄", count="件数", ticker2="銘柄2", all_stocks="全銘柄")
    @app_commands.describe(
        ticker="例: NEKO",
        count="直近何件を描くか (最大200)",
        ticker2="指定すると2銘柄の変化率を比較表示します",
        all_stocks="オンにすると全銘柄の変化率を比較表示します",
    )
    @app_commands.autocomplete(ticker=ticker_autocomplete, ticker2=ticker_autocomplete)
    @commands.guild_only()
    async def stockChartCommand(
        self,
        ctx: commands.Context,
        ticker: str,
        count: int = 100,
        ticker2: str | None = None,
        all_stocks: bool = False,
    ):
        if all_stocks:
            items = await stocks.get_stocks(active_only=False)
            if not items:
                await ctx.reply("銘柄がありません", ephemeral=True)
                return
            histories: dict[str, list[tuple[str, int]]] = {}
            for s in items:
                histories[s.ticker] = await stocks.get_history(s.ticker, count)
            buf = await asyncio.to_thread(
                render_multi_stock_chart, histories, amountName
            )
            file = discord.File(buf, filename=CHART_NAME)
            embed = discord.Embed(
                title="全銘柄比較チャート📈",
                description=(
                    f"{len(histories)}銘柄・直近{count}件\n"
                    "各銘柄は区間先頭を0%とした変化率で表示"
                ),
                color=discord.Color.blue(),
            )
            embed.set_image(url=f"attachment://{CHART_NAME}")
            await ctx.reply(embed=embed, file=file)
            return
        try:
            name = stocks.normalize_ticker(ticker)
        except ValueError as e:
            await ctx.reply(str(e), ephemeral=True)
            return
        stock = await stocks.get_stock(name)
        if not stock:
            await ctx.reply(f"{name} は存在しません", ephemeral=True)
            return
        if ticker2:
            try:
                name2 = stocks.normalize_ticker(ticker2)
            except ValueError as e:
                await ctx.reply(str(e), ephemeral=True)
                return
            if name2 == name:
                await ctx.reply("同じ銘柄同士は比較できません", ephemeral=True)
                return
            stock2 = await stocks.get_stock(name2)
            if not stock2:
                await ctx.reply(f"{name2} は存在しません", ephemeral=True)
                return
            histories = {
                name: await stocks.get_history(name, count),
                name2: await stocks.get_history(name2, count),
            }
            buf = await asyncio.to_thread(
                render_multi_stock_chart, histories, amountName
            )
            file = discord.File(buf, filename=CHART_NAME)
            embed = discord.Embed(
                title=f"{name} vs {name2}📈",
                description=(
                    f"`{name}`: 現在{buildAmountText(stock.price)}"
                    + ("" if stock.is_active else " [取扱停止]")
                    + f"\n`{name2}`: 現在{buildAmountText(stock2.price)}"
                    + ("" if stock2.is_active else " [取扱停止]")
                    + "\n各銘柄は区間先頭を0%とした変化率で表示"
                ),
                color=discord.Color.blue(),
            )
            embed.set_image(url=f"attachment://{CHART_NAME}")
            await ctx.reply(embed=embed, file=file)
            return
        history = await stocks.get_history(name, count)
        buf = await asyncio.to_thread(render_stock_chart, history, name, amountName)
        file = discord.File(buf, filename=CHART_NAME)
        embed = discord.Embed(
            title=f"{name} チャート📈",
            description=(
                f"現在値: {buildAmountText(stock.price)}\n"
                f"mu={stock.mu} sigma={stock.sigma} impact={stock.impact:.4f}/株"
                + ("" if stock.is_active else "\n※取扱停止中")
            ),
            color=discord.Color.blue(),
        )
        embed.set_image(url=f"attachment://{CHART_NAME}")
        await ctx.reply(embed=embed, file=file)

    @stock.command(name="currency", brief="通貨の市場価値チャートを表示します")
    @app_commands.rename(count="件数")
    @app_commands.describe(count="直近何件を描くか (最大200)")
    @commands.guild_only()
    async def stockCurrencyCommand(self, ctx: commands.Context, count: int = 100):
        points = await stocks.get_currency_index(count)
        buf = await asyncio.to_thread(render_currency_chart, points)
        file = discord.File(buf, filename=CHART_NAME)
        if points:
            desc = (
                f"現在値: `{points[-1][1]:.1f}` (起点=100)\n"
                "上がる=通貨高／下がる=通貨安\n"
                "(株安・通貨供給減で上昇、株高・通貨増発で下落)"
            )
        else:
            desc = "データがありません"
        embed = discord.Embed(
            title="通貨価値指数💱",
            description=desc,
            color=discord.Color.gold(),
        )
        embed.set_image(url=f"attachment://{CHART_NAME}")
        await ctx.reply(embed=embed, file=file)

    @stock.command(name="portfolio", brief="保有株と評価額を表示します")
    @commands.guild_only()
    async def stockPortfolioCommand(self, ctx: commands.Context):
        items = await stocks.get_portfolio(ctx.author.id)
        if not items:
            await ctx.reply("保有株はありません", ephemeral=True)
            return
        lines = []
        total_profit = 0
        for item in items:
            total_profit += item["profit"]
            status = "" if item["is_active"] else " [停止]"
            if item["profit"] > 0:
                profit_text = f"損益+{buildAmountText(item['profit'])}"
            elif item["profit"] < 0:
                profit_text = f"損益-{buildAmountText(-item['profit'])}"
            else:
                profit_text = "損益±0"
            if item["avg_cost"] > 0:
                rate = (item["price"] - item["avg_cost"]) / item["avg_cost"] * 100
                profit_text += f" ({rate:+.1f}%)"
            lines.append(
                f"`{item['ticker']}`{status}: {item['qty']}株 "
                f"(平均{buildAmountText(item['avg_cost'])} → "
                f"現在{buildAmountText(item['price'])}) "
                f"評価{buildAmountText(item['market'])} {profit_text}"
            )
        await ctx.reply(
            embed=discord.Embed(
                title=f"{ctx.author.display_name} のポートフォリオ💼",
                description="\n".join(lines)
                + f"\n```patch\n{buildGetAmountText(total_profit, md=True)}\n```",
                color=discord.Color.purple(),
            )
        )

    @stock.command(name="create", brief="会社を設立します (1人1社)")
    @app_commands.rename(ticker="銘柄", invest="投資額")
    @app_commands.describe(
        ticker="英数字1〜15文字 (例: MYCO)",
        invest="会社への投資額 (1以上)。開始株価になり、額が多いほど好条件に",
    )
    @commands.guild_only()
    async def stockCreateCommand(
        self,
        ctx: commands.Context,
        ticker: str,
        invest: int,
    ):
        try:
            stock = await stocks.create_company(ctx.author.id, ticker, invest)
        except ValueError as e:
            await ctx.reply(str(e), ephemeral=True)
            return
        except LookupError:
            raise AmountNotEnough()
        rank = stocks.rank_for_invest(invest)
        founder_shares = stocks.founder_shares_for(invest)
        await ctx.reply(
            f"🏢 `{stock.ticker}` を設立しました！\n"
            f"設立費用: {buildAmountText(stocks.FOUNDING_FEE + invest)}"
            f" (手数料{buildAmountText(stocks.FOUNDING_FEE)}"
            f"＋投資{buildAmountText(invest)})\n"
            f"開始株価: {buildAmountText(stock.price)}"
            f" (ランク{rank.name}: mu={stock.mu} sigma={stock.sigma}"
            f" impact={stock.impact})\n"
            f"創業者株 {founder_shares}株を付与 (売却のみ可・買増不可)\n"
            "※投資額が多いほど好条件 (S: 5万〜 / A: 2万〜 / B: 5千〜 / C: 〜5千未満)\n"
            "※他人が自社株を売買するたび、代金の1%がロイヤリティで入ります\n"
            "※株価が上がると、上昇分×保有株数×0.1%が配当で入ります (株を持ち続けるほどお得)\n"
            "※自分の会社の株の買増はできません"
        )

    @stock.command(name="retire", brief="自分の会社を取扱停止します")
    @app_commands.rename(ticker="銘柄")
    @app_commands.autocomplete(ticker=ticker_autocomplete)
    @commands.guild_only()
    async def stockRetireCommand(self, ctx: commands.Context, ticker: str):
        try:
            stock = await stocks.set_active_own(ctx.author.id, ticker, False)
        except ValueError as e:
            await ctx.reply(str(e), ephemeral=True)
            return
        await ctx.reply(f"`{stock.ticker}` を取扱停止しました")

    @stock.command(name="reopen", brief="自分の会社の取扱を再開します")
    @app_commands.rename(ticker="銘柄")
    @app_commands.autocomplete(ticker=ticker_autocomplete)
    @commands.guild_only()
    async def stockReopenCommand(self, ctx: commands.Context, ticker: str):
        try:
            stock = await stocks.set_active_own(ctx.author.id, ticker, True)
        except ValueError as e:
            await ctx.reply(str(e), ephemeral=True)
            return
        await ctx.reply(f"`{stock.ticker}` の取扱を再開しました")

    # ---------- /stock-admin グループ ----------

    @commands.hybrid_group(name="stock-admin", brief="※管理者専用 銘柄を管理します")
    @admin_only()
    @commands.guild_only()
    async def stockAdmin(self, ctx: commands.Context):
        await ctx.reply(
            "サブコマンドを指定してください: "
            "add / delist / relist / params / set-price",
            ephemeral=True,
        )

    @stockAdmin.command(name="add", brief="※管理者専用 銘柄を追加します")
    @admin_only()
    @commands.guild_only()
    @app_commands.rename(
        ticker="銘柄",
        price="開始価格",
        mu="mu",
        sigma="sigma",
        preset="プリセット",
        impact="impact",
        mu_preset="muプリセット",
        impact_preset="impactプリセット",
    )
    @app_commands.describe(
        ticker="英数字1〜15文字 (例: SONY)",
        price="開始価格 (1以上)",
        mu="平均成長率 -1.0〜1.0 (省略時0)",
        sigma="値動きの荒さ。数値指定か下のプリセットのどちらか",
        preset="sigmaのプリセット。おまかせランダム可",
        impact="需給感応度 0〜0.01/株 (省略時0.0005)",
        mu_preset="muのプリセット。おまかせランダム可",
        impact_preset="impactのプリセット。おまかせランダム可",
    )
    @app_commands.choices(
        preset=[
            app_commands.Choice(name="おとなしい (σ=0.02)", value="calm"),
            app_commands.Choice(name="ふつう (σ=0.05)", value="normal"),
            app_commands.Choice(name="荒い (σ=0.10)", value="wild"),
            app_commands.Choice(name="おまかせランダム", value="random"),
        ],
        mu_preset=[
            app_commands.Choice(name="下降トレンド (μ=-0.001)", value="down"),
            app_commands.Choice(name="横ばい (μ=0)", value="flat"),
            app_commands.Choice(name="上昇トレンド (μ=+0.001)", value="up"),
            app_commands.Choice(name="おまかせランダム", value="random"),
        ],
        impact_preset=[
            app_commands.Choice(name="鈍感・動きにくい (0.0002)", value="dull"),
            app_commands.Choice(name="ふつう (0.0005)", value="normal"),
            app_commands.Choice(name="敏感・動きやすい (0.001)", value="sensitive"),
            app_commands.Choice(name="おまかせランダム", value="random"),
        ],
    )
    async def stockAddCommand(
        self,
        ctx: commands.Context,
        ticker: str,
        price: int,
        mu: float | None = None,
        sigma: float | None = None,
        preset: app_commands.Choice[str] | None = None,
        impact: float | None = None,
        mu_preset: app_commands.Choice[str] | None = None,
        impact_preset: app_commands.Choice[str] | None = None,
    ):
        try:
            mu_val = stocks.resolve_mu(mu, mu_preset.value if mu_preset else None)
            sigma_val = await stocks.resolve_sigma(
                sigma, preset.value if preset else None
            )
            impact_val = stocks.resolve_impact(
                impact, impact_preset.value if impact_preset else None
            )
            stock = await stocks.add_ticker(
                ticker, price, mu_val, sigma_val, impact_val
            )
        except ValueError as e:
            await ctx.reply(str(e), ephemeral=True)
            return
        await ctx.reply(
            f"`{stock.ticker}` を上場しました: "
            f"{buildAmountText(stock.price)} "
            f"(mu={stock.mu} sigma={stock.sigma} impact={stock.impact})"
        )

    @stockAdmin.command(name="delist", brief="※管理者専用 銘柄を取扱停止します")
    @admin_only()
    @commands.guild_only()
    @app_commands.rename(ticker="銘柄")
    @app_commands.autocomplete(ticker=ticker_autocomplete)
    async def stockDelistCommand(self, ctx: commands.Context, ticker: str):
        try:
            stock = await stocks.set_active(ticker, False)
        except ValueError as e:
            await ctx.reply(str(e), ephemeral=True)
            return
        await ctx.reply(f"`{stock.ticker}` を取扱停止しました（売却は可能）")

    @stockAdmin.command(name="relist", brief="※管理者専用 取扱停止を解除します")
    @admin_only()
    @commands.guild_only()
    @app_commands.rename(ticker="銘柄")
    @app_commands.autocomplete(ticker=ticker_autocomplete)
    async def stockRelistCommand(self, ctx: commands.Context, ticker: str):
        try:
            stock = await stocks.set_active(ticker, True)
        except ValueError as e:
            await ctx.reply(str(e), ephemeral=True)
            return
        await ctx.reply(f"`{stock.ticker}` の取扱を再開しました")

    @stockAdmin.command(name="set-price", brief="※管理者専用 価格を直接設定します")
    @admin_only()
    @commands.guild_only()
    @app_commands.rename(ticker="銘柄", price="価格")
    @app_commands.describe(ticker="例: GMO", price="設定する価格 (1以上)")
    @app_commands.autocomplete(ticker=ticker_autocomplete)
    async def stockSetPriceCommand(
        self, ctx: commands.Context, ticker: str, price: int
    ):
        try:
            stock = await stocks.set_price(ticker, price)
        except ValueError as e:
            await ctx.reply(str(e), ephemeral=True)
            return
        await ctx.reply(
            f"`{stock.ticker}` の価格を {buildAmountText(stock.price)} に設定しました"
        )

    @stockAdmin.command(name="params", brief="※管理者専用 mu/sigma/impactを変更します")
    @admin_only()
    @commands.guild_only()
    @app_commands.rename(
        ticker="銘柄",
        mu="mu",
        sigma="sigma",
        impact="impact",
        mu_preset="muプリセット",
        impact_preset="impactプリセット",
    )
    @app_commands.describe(
        mu="省略可",
        sigma="省略可",
        impact="省略可",
        preset="sigmaのプリセット。おまかせランダム可",
        mu_preset="muのプリセット。おまかせランダム可",
        impact_preset="impactのプリセット。おまかせランダム可",
    )
    @app_commands.choices(
        preset=[
            app_commands.Choice(name="おとなしい (σ=0.02)", value="calm"),
            app_commands.Choice(name="ふつう (σ=0.05)", value="normal"),
            app_commands.Choice(name="荒い (σ=0.10)", value="wild"),
            app_commands.Choice(name="おまかせランダム", value="random"),
        ],
        mu_preset=[
            app_commands.Choice(name="下降トレンド (μ=-0.001)", value="down"),
            app_commands.Choice(name="横ばい (μ=0)", value="flat"),
            app_commands.Choice(name="上昇トレンド (μ=+0.001)", value="up"),
            app_commands.Choice(name="おまかせランダム", value="random"),
        ],
        impact_preset=[
            app_commands.Choice(name="鈍感・動きにくい (0.0002)", value="dull"),
            app_commands.Choice(name="ふつう (0.0005)", value="normal"),
            app_commands.Choice(name="敏感・動きやすい (0.001)", value="sensitive"),
            app_commands.Choice(name="おまかせランダム", value="random"),
        ],
    )
    @app_commands.autocomplete(ticker=ticker_autocomplete)
    async def stockParamsCommand(
        self,
        ctx: commands.Context,
        ticker: str,
        mu: float | None = None,
        sigma: float | None = None,
        impact: float | None = None,
        preset: app_commands.Choice[str] | None = None,
        mu_preset: app_commands.Choice[str] | None = None,
        impact_preset: app_commands.Choice[str] | None = None,
    ):
        if (
            mu is None
            and sigma is None
            and impact is None
            and preset is None
            and mu_preset is None
            and impact_preset is None
        ):
            await ctx.reply(
                "mu・sigma・impact・各プリセットのいずれかを指定してください",
                ephemeral=True,
            )
            return
        try:
            resolved_mu = None
            if mu is not None or mu_preset is not None:
                resolved_mu = stocks.resolve_mu(
                    mu, mu_preset.value if mu_preset else None
                )
            resolved_sigma = None
            if preset is not None or sigma is not None:
                resolved_sigma = await stocks.resolve_sigma(
                    sigma, preset.value if preset else None
                )
            resolved_impact = None
            if impact is not None or impact_preset is not None:
                resolved_impact = stocks.resolve_impact(
                    impact, impact_preset.value if impact_preset else None
                )
            stock = await stocks.update_params(
                ticker, resolved_mu, resolved_sigma, resolved_impact
            )
        except ValueError as e:
            await ctx.reply(str(e), ephemeral=True)
            return
        await ctx.reply(
            f"`{stock.ticker}` を更新: "
            f"mu={stock.mu} sigma={stock.sigma} impact={stock.impact}"
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(StockCog(bot))
