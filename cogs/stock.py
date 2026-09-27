"""株価: 売買 + 折れ線チャート + 管理者の銘柄追加。

一般: /stock buy|sell|chart|portfolio|list
管理者: /stock-admin add|delist|relist|params
"""

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

from objects.exceptions import AmountNotEnough
from services import stocks
from services.message import buildAmountText, buildGetAmountText
from services.stock_chart import render_stock_chart

logger = logging.getLogger(__name__)

CHART_NAME = "chart.png"
TICK_INTERVAL_MINUTES = 5.0


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
            await stocks.tick_once()
        except Exception:
            logger.exception("株価の定期更新に失敗")

    @tick_loop.before_loop
    async def _before_tick(self):
        await self.bot.wait_until_ready()

    # ---------- /stock グループ ----------

    @commands.hybrid_group(name="stock", brief="株取引をします")
    @commands.guild_only()
    async def stock(self, ctx: commands.Context):
        await ctx.reply(
            "サブコマンドを指定してください: buy / sell / chart / portfolio / list",
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
            lines.append(f"`{s.ticker}`: {buildAmountText(s.price)} {mark}{status}")
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
            price, cost = await stocks.buy(ctx.author.id, ticker, qty)
        except ValueError as e:
            await ctx.reply(str(e), ephemeral=True)
            return
        except LookupError:
            raise AmountNotEnough()
        await ctx.reply(
            embed=discord.Embed(
                title="株を購入📈",
                description=(
                    f"<@{ctx.author.id}>\n"
                    f"`{ticker.strip().upper()}` を {qty}株 @"
                    f"{buildAmountText(price)}\n"
                    f"```patch\n{buildGetAmountText(-cost, md=True)}\n```"
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
            price, proceeds = await stocks.sell(ctx.author.id, ticker, qty)
        except ValueError as e:
            await ctx.reply(str(e), ephemeral=True)
            return
        await ctx.reply(
            embed=discord.Embed(
                title="株を売却💰",
                description=(
                    f"<@{ctx.author.id}>\n"
                    f"`{ticker.strip().upper()}` を {qty}株 @"
                    f"{buildAmountText(price)}\n"
                    f"```patch\n{buildGetAmountText(proceeds, md=True)}\n```"
                ),
                color=discord.Color.gold(),
            )
        )

    @stock.command(name="chart", brief="株価の折れ線チャートを表示します")
    @app_commands.rename(ticker="銘柄", count="件数")
    @app_commands.describe(ticker="例: NEKO", count="直近何件を描くか (最大200)")
    @app_commands.autocomplete(ticker=ticker_autocomplete)
    @commands.guild_only()
    async def stockChartCommand(
        self, ctx: commands.Context, ticker: str, count: int = 100
    ):
        try:
            name = stocks.normalize_ticker(ticker)
        except ValueError as e:
            await ctx.reply(str(e), ephemeral=True)
            return
        stock = await stocks.get_stock(name)
        if not stock:
            await ctx.reply(f"{name} は存在しません", ephemeral=True)
            return
        history = await stocks.get_history(name, count)
        buf = await asyncio.to_thread(render_stock_chart, history, name)
        file = discord.File(buf, filename=CHART_NAME)
        embed = discord.Embed(
            title=f"{name} チャート📈",
            description=(
                f"現在値: {buildAmountText(stock.price)}\n"
                f"mu={stock.mu} sigma={stock.sigma}"
                + ("" if stock.is_active else "\n※取扱停止中")
            ),
            color=discord.Color.blue(),
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
            lines.append(
                f"`{item['ticker']}`{status}: {item['qty']}株 "
                f"(平均{buildAmountText(item['avg_cost'])} → "
                f"現在{buildAmountText(item['price'])}) "
                f"評価{buildAmountText(item['market'])}"
            )
        await ctx.reply(
            embed=discord.Embed(
                title=f"{ctx.author.display_name} のポートフォリオ💼",
                description="\n".join(lines)
                + f"\n```patch\n{buildGetAmountText(total_profit, md=True)}\n```",
                color=discord.Color.purple(),
            )
        )

    # ---------- /stock-admin グループ ----------

    @commands.hybrid_group(name="stock-admin", brief="※管理者専用 銘柄を管理します")
    @commands.has_guild_permissions(administrator=True)
    @commands.guild_only()
    async def stockAdmin(self, ctx: commands.Context):
        await ctx.reply(
            "サブコマンドを指定してください: add / delist / relist / params",
            ephemeral=True,
        )

    @stockAdmin.command(name="add", brief="※管理者専用 銘柄を追加します")
    @commands.has_guild_permissions(administrator=True)
    @commands.guild_only()
    @app_commands.rename(
        ticker="銘柄", price="開始価格", mu="mu", sigma="sigma", preset="プリセット"
    )
    @app_commands.describe(
        ticker="英数字1〜10文字 (例: SONY)",
        price="開始価格 (1以上)",
        mu="平均成長率 -1.0〜1.0 (省略時0)",
        sigma="値動きの荒さ。数値指定か下のプリセットのどちらか",
        preset="指定するとsigmaの代わりに使われます",
    )
    @app_commands.choices(
        preset=[
            app_commands.Choice(name="おとなしい (σ=0.02)", value="calm"),
            app_commands.Choice(name="ふつう (σ=0.05)", value="normal"),
            app_commands.Choice(name="荒い (σ=0.10)", value="wild"),
        ]
    )
    async def stockAddCommand(
        self,
        ctx: commands.Context,
        ticker: str,
        price: int,
        mu: float = 0.0,
        sigma: float | None = None,
        preset: app_commands.Choice[str] | None = None,
    ):
        try:
            resolved = await stocks.resolve_sigma(
                sigma, preset.value if preset else None
            )
            stock = await stocks.add_ticker(ticker, price, mu, resolved)
        except ValueError as e:
            await ctx.reply(str(e), ephemeral=True)
            return
        await ctx.reply(
            f"`{stock.ticker}` を上場しました: "
            f"{buildAmountText(stock.price)} "
            f"(mu={stock.mu} sigma={stock.sigma})"
        )

    @stockAdmin.command(name="delist", brief="※管理者専用 銘柄を取扱停止します")
    @commands.has_guild_permissions(administrator=True)
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
    @commands.has_guild_permissions(administrator=True)
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

    @stockAdmin.command(name="params", brief="※管理者専用 mu/sigmaを変更します")
    @commands.has_guild_permissions(administrator=True)
    @commands.guild_only()
    @app_commands.rename(ticker="銘柄", mu="mu", sigma="sigma")
    @app_commands.describe(mu="省略可", sigma="省略可")
    @app_commands.autocomplete(ticker=ticker_autocomplete)
    async def stockParamsCommand(
        self,
        ctx: commands.Context,
        ticker: str,
        mu: float | None = None,
        sigma: float | None = None,
    ):
        if mu is None and sigma is None:
            await ctx.reply("muかsigmaのどちらかを指定してください", ephemeral=True)
            return
        try:
            stock = await stocks.update_params(ticker, mu, sigma)
        except ValueError as e:
            await ctx.reply(str(e), ephemeral=True)
            return
        await ctx.reply(f"`{stock.ticker}` を更新: mu={stock.mu} sigma={stock.sigma}")


async def setup(bot: commands.Bot):
    await bot.add_cog(StockCog(bot))
