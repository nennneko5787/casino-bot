"""株価: 売買 + 会社設立 + 折れ線チャート + 株式市場。

一般: /stock buy|sell|create|invest|rescue|retire|reopen|chart|currency|portfolio|list
管理者: /stock-admin add|move|delist|relist|set-price
管理者: /stock-market create|update|delete|list

銘柄は株式市場に所属し、市場ごとに値動きレンジ (mu/sigma/impact) と
HP倒産ルールが違う。mu/sigma/impactは5分ごとに市場レンジ内で自動再抽選
され、個別指定はできない (params廃止)。
倒産はHP制 + 追証制: 下落でHPが減り、警告ライン以下で経営危機に入る。
設立者が追証 (rescue) で回復しなければ破産 (保有株は紙くず・会社消去)。
"""

import asyncio
import logging
import os
import time
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
LIST_PAGE_SIZE = 10


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


async def market_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """市場のオートコンプリート。"""
    try:
        items = await stocks.get_markets()
    except Exception:
        logger.exception("市場候補の取得に失敗")
        return []
    current = current.strip().upper()
    return [
        app_commands.Choice(name=f"{m.display_name} ({m.description[:40]})", value=m.id)
        for m in items
        if current in m.id or current in m.display_name.upper()
    ][:25]


def _hp_bar(hp: int, hp_max: int, width: int = 10) -> str:
    """HPバー (例: ██████░░░░ 62/100)。"""
    if hp_max < 1:
        return ""
    filled = max(0, min(width, round(hp / hp_max * width)))
    return f"{'█' * filled}{'░' * (width - filled)} {hp}/{hp_max}"


async def build_stock_list_embed(
    market_id: str, page: int
) -> tuple[discord.Embed, int, str]:
    """市場別の一覧Embedを作る。戻り値は (embed, 総ページ数, 市場ID)。"""
    markets = await stocks.get_markets()
    if not markets:
        return (
            discord.Embed(title="株価一覧📈", description="市場がありません"),
            1,
            market_id,
        )
    market = next((m for m in markets if m.id == market_id), markets[0])
    items = [
        s for s in await stocks.get_stocks(active_only=False)
        if s.market_id == market.id
    ]
    total_pages = max((len(items) + LIST_PAGE_SIZE - 1) // LIST_PAGE_SIZE, 1)
    page = max(0, min(page, total_pages - 1))
    lines = []
    for s in items[page * LIST_PAGE_SIZE:(page + 1) * LIST_PAGE_SIZE]:
        history = await stocks.get_history(s.ticker, 2)
        if len(history) >= 2:
            diff = history[-1][1] - history[-2][1]
            mark = f"({diff:+})"
        else:
            mark = ""
        status = "" if s.is_active else " [取扱停止]"
        owner = f" [U:<@{s.owner_id}>]" if s.owner_id is not None else ""
        crisis, _ = stocks.crisis_info(s, market)
        crisis_mark = " ⚠️経営危機" if crisis else ""
        lines.append(
            f"`{s.ticker}`: {buildAmountText(s.price)} {mark}{status}{owner}\n"
            f"{_hp_bar(s.hp, market.hp_max)}{crisis_mark}"
        )
    desc = "\n".join(lines) if lines else "銘柄がありません"
    embed = discord.Embed(
        title=f"{market.display_name} 一覧📈 (p.{page + 1}/{total_pages})",
        description=desc,
        color=discord.Color.blue(),
    )
    embed.set_footer(text=f"市場: {market.display_name}／全{len(markets)}市場")
    return embed, total_pages, market.id


class MarketSelect(discord.ui.Select):
    """一覧View用の市場セレクト。選択で市場を切り替える。"""

    def __init__(self, markets: list[stocks.Market], current: str):
        super().__init__(
            placeholder="市場を選択",
            options=[
                discord.SelectOption(
                    label=m.display_name,
                    value=m.id,
                    description=(m.description[:100] or None),
                    default=(m.id == current),
                )
                for m in markets[:25]
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        if isinstance(view, StockListView):
            await view.on_market_selected(interaction, self.values[0])


class StockListView(discord.ui.View):
    """市場別一覧のページネーションView (市場セレクト + 前へ/次へ)。"""

    def __init__(self, market_id: str, markets: list[stocks.Market]):
        super().__init__(timeout=180)
        self.market_id = market_id
        self.page = 0
        self.total_pages = 1
        self.message: discord.Message | None = None
        self.market_select = MarketSelect(markets, market_id)
        self.add_item(self.market_select)

    async def on_market_selected(
        self, interaction: discord.Interaction, market_id: str
    ):
        self.market_id = market_id
        self.page = 0
        for o in self.market_select.options:
            o.default = o.value == self.market_id
        await self.refresh(interaction)

    @discord.ui.button(label="◀ 前へ", style=discord.ButtonStyle.secondary)
    async def prev_page(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        self.page = max(self.page - 1, 0)
        await self.refresh(interaction)

    @discord.ui.button(label="次へ ▶", style=discord.ButtonStyle.secondary)
    async def next_page(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        self.page = min(self.page + 1, max(self.total_pages - 1, 0))
        await self.refresh(interaction)

    async def refresh(self, interaction: discord.Interaction):
        embed, total, mid = await build_stock_list_embed(self.market_id, self.page)
        self.market_id = mid
        self.total_pages = total
        self.page = max(0, min(self.page, total - 1))
        self.prev_page.disabled = self.page <= 0
        self.next_page.disabled = self.page >= total - 1
        await interaction.response.edit_message(embed=embed, view=self)

    async def on_timeout(self):
        for item in self.children:
            if isinstance(item, (discord.ui.Button, discord.ui.Select)):
                item.disabled = True
        if self.message is not None:
            with suppress(Exception):
                await self.message.edit(view=self)


class StockCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._last_tick_at: float | None = None
        self._tick_count = 0

    async def cog_load(self):
        self.tick_loop.start()
        # 起動時再チェック: 停止中のtick分を評価し直す。
        # tick_loop初回も即時実行されるため、_run_tick内のガードで二重進行を防ぐ。
        asyncio.get_running_loop().create_task(self._startup_review())

    async def cog_unload(self):
        self.tick_loop.cancel()

    async def _startup_review(self):
        await self.bot.wait_until_ready()
        logger.info("株価の起動時再チェックを実行します")
        await self._run_tick()

    async def _log_channel(
        self, guild: discord.Guild | None
    ) -> discord.abc.Messageable | None:
        """経営危機・破産関連通知先。envのlog_channel優先、なければ従来の通知先。"""
        if guild is not None:
            raw = os.environ.get("log_channel", "").strip()
            if raw.isdigit():
                ch = guild.get_channel(int(raw))
                if ch is None:
                    with suppress(discord.DiscordException):
                        ch = await guild.fetch_channel(int(raw))
                if isinstance(ch, discord.abc.Messageable):
                    return ch
        return self._notify_channel(guild)

    async def _broadcast(self, msg: str):
        for guild in self.bot.guilds:
            with suppress(Exception):
                ch = await self._log_channel(guild)
                if ch is not None:
                    await ch.send(msg)

    async def _run_tick(self):
        # 起動時再チェックとtick_loop初回が重なった場合は片方だけ進める
        now = time.monotonic()
        if self._last_tick_at is not None and now - self._last_tick_at < 45:
            return
        self._last_tick_at = now
        self._tick_count += 1
        try:
            _, bankrupted, warned, escaped = await stocks.tick_once()
            # 5分ごとに mu/sigma/impact を市場レンジ内で再抽選する
            if self._tick_count % stocks.RANDOMIZE_EVERY_TICKS == 0:
                with suppress(Exception):
                    await stocks.randomize_params()
        except Exception:
            logger.exception("株価の定期更新に失敗")
            return
        for info in warned:
            owner = f"<@{info['owner_id']}>" if info["owner_id"] else "運営"
            await self._broadcast(
                f"⚠️ `{info['ticker']}` が経営危機です"
                f"（市場{info['market_id']}・HP {info['hp']}）\n"
                f"設立者: {owner}／{info['deadline_hours']:g}時間以内に"
                "`/stock rescue` で追証しなければ破産します"
            )
        for info in escaped:
            owner = f"<@{info['owner_id']}>" if info["owner_id"] else "運営"
            await self._broadcast(
                f"✅ `{info['ticker']}` が経営危機から脱出しました"
                f"（市場{info['market_id']}・HP {info['hp']}）\n"
                f"設立者: {owner}"
            )
        for info in bankrupted:
            owner = f"<@{info['owner_id']}>" if info["owner_id"] else "運営"
            await self._broadcast(
                f"💸 `{info['ticker']}` が破産しました"
                f"（市場{info['market_id']}・HP枯渇）\n"
                f"設立者: {owner}／保有株は紙くずになりました"
            )

    @tasks.loop(minutes=TICK_INTERVAL_MINUTES)
    async def tick_loop(self):
        await self._run_tick()

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
            "buy / sell / create / invest / rescue / retire / reopen / chart / "
            "currency / portfolio / list",
            ephemeral=True,
        )

    @stock.command(name="list", brief="市場ごとの銘柄一覧を表示します")
    @app_commands.rename(market="市場")
    @app_commands.describe(market="市場 (省略時は最初の市場)")
    @app_commands.autocomplete(market=market_autocomplete)
    @commands.guild_only()
    async def stockListCommand(
        self, ctx: commands.Context, market: str | None = None
    ):
        markets = await stocks.get_markets()
        if not markets:
            await ctx.reply("市場がありません", ephemeral=True)
            return
        market_id = market or markets[0].id
        try:
            market_id = stocks.normalize_market_id(market_id)
        except ValueError as e:
            await ctx.reply(str(e), ephemeral=True)
            return
        embed, total, mid = await build_stock_list_embed(market_id, 0)
        view = StockListView(mid, markets)
        view.total_pages = total
        view.prev_page.disabled = True
        view.next_page.disabled = total <= 1
        sent = await ctx.reply(embed=embed, view=view)
        if isinstance(sent, discord.Message):
            view.message = sent

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
        market = await stocks.get_market(stock.market_id)
        market_name = market.display_name if market else stock.market_id
        hp_max = market.hp_max if market else stocks.HP_MAX
        if market:
            in_crisis, elapsed = stocks.crisis_info(stock, market)
            if in_crisis:
                rest = stocks.rescue_deadline_hours(stock, market)
                hp_note = (
                    f"\n⚠️経営危機: HP {_hp_bar(stock.hp, hp_max)}"
                    f" ({elapsed:.1f}h経過・残り約{rest:.1f}h)"
                    "\n追証は `/stock rescue` で"
                )
            else:
                hp_note = f"\nHP: {_hp_bar(stock.hp, hp_max)}"
        else:
            hp_note = ""
        market_note = f"\n市場: {market_name}{hp_note}"
        embed = discord.Embed(
            title=f"{name} チャート📈",
            description=(
                f"現在値: {buildAmountText(stock.price)}\n"
                f"mu={stock.mu} sigma={stock.sigma} impact={stock.impact:.4f}/株"
                + ("" if stock.is_active else "\n※取扱停止中")
                + market_note
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
        markets = await stocks.get_markets()
        market_names = {m.id: m.display_name for m in markets}
        market_hp_max = {m.id: m.hp_max for m in markets}
        all_stocks = {s.ticker: s for s in await stocks.get_stocks(active_only=False)}
        lines = []
        total_profit = 0
        for item in items:
            total_profit += item["profit"]
            status = "" if item["is_active"] else " [停止]"
            st = all_stocks.get(item["ticker"])
            if st is not None:
                market_tag = f"[{market_names.get(st.market_id, st.market_id)}]"
                hp_max = market_hp_max.get(st.market_id, stocks.HP_MAX)
                hp_text = f" HP{_hp_bar(st.hp, hp_max)}"
            else:
                market_tag, hp_text = "", ""
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
                f"`{item['ticker']}`{market_tag}{status}: {item['qty']}株 "
                f"(平均{buildAmountText(item['avg_cost'])} → "
                f"現在{buildAmountText(item['price'])}) "
                f"評価{buildAmountText(item['market'])} {profit_text}{hp_text}"
            )
        await ctx.reply(
            embed=discord.Embed(
                title=f"{ctx.author.display_name} のポートフォリオ💼",
                description="\n".join(lines)
                + f"\n```patch\n{buildGetAmountText(total_profit, md=True)}\n```",
                color=discord.Color.purple(),
            )
        )

    @stock.command(name="create", brief="会社を設立します (レベルで枠増加)")
    @app_commands.rename(ticker="銘柄", invest="投資額", market="市場")
    @app_commands.describe(
        ticker="英数字1〜15文字 (例: MYCO)",
        invest="会社への投資額 (1以上)。開始株価になり、額が多いほど好条件に",
        market="上場する株式市場 (省略時はMEOWDAQ)",
    )
    @app_commands.autocomplete(market=market_autocomplete)
    @commands.guild_only()
    async def stockCreateCommand(
        self,
        ctx: commands.Context,
        ticker: str,
        invest: int,
        market: str = "MEOWDAQ",
    ):
        try:
            stock = await stocks.create_company(
                ctx.author.id, ticker, invest, market
            )
        except ValueError as e:
            await ctx.reply(str(e), ephemeral=True)
            return
        except LookupError:
            raise AmountNotEnough()
        rank = stocks.rank_for_invest(invest)
        founder_shares = stocks.founder_shares_for(invest, rank)
        from services import levels as level_service
        from services.database import DBService

        user_level = 1
        with suppress(Exception):  # レベル取得失敗時はLv.1扱いで継続
            user_level = int((await level_service.get_info(ctx.author.id))["level"])
        limit = stocks.max_companies_for_level(user_level)
        cursor = await DBService.pool.execute(
            "SELECT COUNT(*) AS n FROM stocks WHERE owner_id = ?",
            (ctx.author.id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        owned = int(row["n"]) if row else 0
        rest = limit - owned
        nxt = stocks.next_slot_level(user_level)
        if rest > 0:
            slot_note = f" (Lv.{user_level}: あと{rest}社建てられます"
            slot_note += f"・Lv.{nxt}で次の枠解放)" if nxt else "・上限到達)"
        else:
            slot_note = ""
        await ctx.reply(
            f"🏢 `{stock.ticker}` を設立しました！{slot_note}\n"
            f"市場: {stock.market_id}／開始株価: {buildAmountText(stock.price)}\n"
            f"設立費用: {buildAmountText(rank.fee + invest)}"
            f" (手数料{buildAmountText(rank.fee)}"
            f"＋投資{buildAmountText(invest)})\n"
            f"ランク{rank.name}: 創業者株 {founder_shares}株を付与"
            " (売却のみ可・買増不可)\n"
            "※投資額が多いほど好条件 (S: 5万〜 / A: 2万〜 / B: 5千〜 / C: 〜5千未満)\n"
            f"※ランク{rank.name}の特典: 手数料{buildAmountText(rank.fee)}・"
            f"配当率{rank.dividend_rate:.2%}\n"
            "※他人が自社株を売買するたび、代金の1%がロイヤリティで入ります\n"
            "※値動きは市場のレンジから自動抽選されます (5分ごとに見直し)\n"
            "※自分の会社の株の買増はできません"
        )

    @stock.command(name="invest", brief="自分の会社に追加投資します (増資)")
    @app_commands.rename(ticker="銘柄", invest="投資額")
    @app_commands.describe(
        ticker="自分の会社の銘柄",
        invest="追加投資額 (1以上)。現在の株価で創業者株を発行します",
    )
    @app_commands.autocomplete(ticker=ticker_autocomplete)
    @commands.guild_only()
    async def stockInvestCommand(
        self,
        ctx: commands.Context,
        ticker: str,
        invest: int,
    ):
        try:
            stock, new_shares = await stocks.add_investment(
                ctx.author.id, ticker, invest
            )
        except ValueError as e:
            await ctx.reply(str(e), ephemeral=True)
            return
        except LookupError:
            raise AmountNotEnough()
        with suppress(Exception):
            await missions.record_event(ctx.author.id, "trade")
        await ctx.reply(
            f"💰 `{stock.ticker}` に追加投資しました！\n"
            f"投資額: {buildAmountText(invest)}\n"
            f"発行株数: {new_shares}株 @ {buildAmountText(stock.price)}\n"
            "※株価は変わりません (時価増資のため)"
        )

    @stock.command(name="rescue", brief="経営危機の自社に追証します (HP回復)")
    @app_commands.rename(ticker="銘柄", amount="追証額")
    @app_commands.describe(
        ticker="自分の会社の銘柄",
        amount="投じる金額 (1以上)。HPが回復します",
    )
    @app_commands.autocomplete(ticker=ticker_autocomplete)
    @commands.guild_only()
    async def stockRescueCommand(
        self, ctx: commands.Context, ticker: str, amount: int
    ):
        try:
            gain, new_hp, escaped = await stocks.rescue(
                ctx.author.id, ticker, amount
            )
        except ValueError as e:
            await ctx.reply(str(e), ephemeral=True)
            return
        except LookupError:
            raise AmountNotEnough()
        if escaped:
            note = "✅ 経営危機から脱出しました！"
        else:
            stock = await stocks.get_stock(ticker.strip().upper())
            if stock is not None:
                market = await stocks.get_market(stock.market_id)
                rest = (
                    stocks.rescue_deadline_hours(stock, market)
                    if market else 0.0
                )
                note = f"引き続き経営危機です (期限まで残り約{rest:.1f}h)"
            else:
                note = ""
        await ctx.reply(
            f"🛟 `{ticker.strip().upper()}` に追証しました！\n"
            f"投じた金額: {buildAmountText(amount)}\n"
            f"HP +{gain} → {new_hp}\n{note}"
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
            "add / move / delist / relist / set-price",
            ephemeral=True,
        )

    @stockAdmin.command(name="add", brief="※管理者専用 銘柄を追加します")
    @admin_only()
    @commands.guild_only()
    @app_commands.rename(
        ticker="銘柄",
        price="開始価格",
        market="市場",
    )
    @app_commands.describe(
        ticker="英数字1〜15文字 (例: SONY)",
        price="開始価格 (1以上)",
        market="上場する株式市場 (省略時はMEOWDAQ)",
    )
    @app_commands.autocomplete(market=market_autocomplete)
    async def stockAddCommand(
        self,
        ctx: commands.Context,
        ticker: str,
        price: int,
        market: str = "MEOWDAQ",
    ):
        try:
            stock = await stocks.add_ticker(ticker, price, market)
        except ValueError as e:
            await ctx.reply(str(e), ephemeral=True)
            return
        await ctx.reply(
            f"`{stock.ticker}` を上場しました: "
            f"{buildAmountText(stock.price)} (市場{stock.market_id}・HP{stock.hp})"
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

    @stockAdmin.command(name="move", brief="※管理者専用 銘柄の所属市場を変更します")
    @admin_only()
    @commands.guild_only()
    @app_commands.rename(ticker="銘柄", market="市場")
    @app_commands.describe(market="移動先の株式市場")
    @app_commands.autocomplete(ticker=ticker_autocomplete, market=market_autocomplete)
    async def stockMoveCommand(
        self, ctx: commands.Context, ticker: str, market: str
    ):
        try:
            stock = await stocks.move_market(ticker, market)
        except ValueError as e:
            await ctx.reply(str(e), ephemeral=True)
            return
        await ctx.reply(
            f"`{stock.ticker}` を市場{stock.market_id}に移動しました "
            f"(HP{stock.hp})"
        )


    # ---------- /stock-market グループ ----------

    @commands.hybrid_group(name="stock-market", brief="※管理者専用 株式市場を管理します")
    @admin_only()
    @commands.guild_only()
    async def stockMarket(self, ctx: commands.Context):
        await ctx.reply(
            "サブコマンドを指定してください: create / update / delete / list",
            ephemeral=True,
        )

    @stockMarket.command(name="list", brief="株式市場の一覧を表示します")
    @commands.guild_only()
    async def stockMarketListCommand(self, ctx: commands.Context):
        markets = await stocks.get_markets()
        if not markets:
            await ctx.reply("市場がありません", ephemeral=True)
            return
        items = await stocks.get_stocks(active_only=False)
        counts: dict[str, int] = {}
        for s in items:
            counts[s.market_id] = counts.get(s.market_id, 0) + 1
        lines = [
            f"`{m.id}` ({m.display_name}): {m.description}\n"
            f"銘柄数{counts.get(m.id, 0)}・"
            f"mu[{m.mu_min:g}〜{m.mu_max:g}] "
            f"σ[{m.sigma_min:g}〜{m.sigma_max:g}] "
            f"impact[{m.impact_min:g}〜{m.impact_max:g}]\n"
            f"HP上限{m.hp_max}・警告{m.warning_hp}・"
            f"追証期限{m.rescue_hours:g}h・HP0猶予{m.zero_grace_hours:g}h"
            + (
                f"・平均回帰(基準{m.mean_ref_price:g}, k={m.mean_k:g})"
                if m.mean_ref_price is not None and m.mean_k is not None
                else ""
            )
            for m in markets
        ]
        await ctx.reply(
            embed=discord.Embed(
                title="株式市場一覧🏛️",
                description="\n\n".join(lines),
                color=discord.Color.teal(),
            )
        )

    @stockMarket.command(name="create", brief="※管理者専用 株式市場を作ります")
    @admin_only()
    @commands.guild_only()
    @app_commands.rename(market_id="しじょう", name="表示名")
    @app_commands.describe(
        market_id="英数字1〜15文字 (例: MEOWDAQ)",
        name="表示名 (省略時はIDと同じ)",
        description="説明文",
        mu_min="mu下限 (既定-0.001)",
        mu_max="mu上限 (既定0.002)",
        sigma_min="sigma下限 (既定0.02)",
        sigma_max="sigma上限 (既定0.05)",
        impact_min="impact下限 (既定0.0002)",
        impact_max="impact上限 (既定0.0006)",
        warning_hp="経営危機に入るHP (既定30)",
        rescue_hours="追証期限h (既定24)",
    )
    async def stockMarketCreateCommand(
        self,
        ctx: commands.Context,
        market_id: str,
        name: str = "",
        description: str = "",
        mu_min: float = -0.001,
        mu_max: float = 0.002,
        sigma_min: float = 0.02,
        sigma_max: float = 0.05,
        impact_min: float = 0.0002,
        impact_max: float = 0.0006,
        warning_hp: int = 30,
        rescue_hours: float = 24.0,
    ):
        try:
            market = await stocks.create_market(
                stocks.Market(
                    id=market_id,
                    display_name=name.strip() or market_id.strip().upper(),
                    description=description,
                    mu_min=mu_min, mu_max=mu_max,
                    sigma_min=sigma_min, sigma_max=sigma_max,
                    impact_min=impact_min, impact_max=impact_max,
                    warning_hp=warning_hp, rescue_hours=rescue_hours,
                )
            )
        except ValueError as e:
            await ctx.reply(str(e), ephemeral=True)
            return
        await ctx.reply(
            f"市場 `{market.id}` ({market.display_name}) を作りました"
        )

    @stockMarket.command(name="update", brief="※管理者専用 株式市場を変更します")
    @admin_only()
    @commands.guild_only()
    @app_commands.rename(market_id="しじょう")
    @app_commands.describe(
        market_id="変更する市場",
        name="表示名",
        description="説明文",
        mu_min="mu下限", mu_max="mu上限",
        sigma_min="sigma下限", sigma_max="sigma上限",
        impact_min="impact下限", impact_max="impact上限",
        jitter="銘柄ゆらぎ幅",
        hp_max="HP上限", warning_hp="危機ラインHP",
        dmg_per_pct="下落1%あたりのHP減",
        recover_per_pct="上昇1%あたりのHP回復",
        rescue_hp_per_100="100通貨あたりの回復HP",
        rescue_hours="追証期限h",
        zero_grace_hours="HP0の猶予h",
    )
    @app_commands.autocomplete(market_id=market_autocomplete)
    async def stockMarketUpdateCommand(
        self,
        ctx: commands.Context,
        market_id: str,
        name: str | None = None,
        description: str | None = None,
        mu_min: float | None = None,
        mu_max: float | None = None,
        sigma_min: float | None = None,
        sigma_max: float | None = None,
        impact_min: float | None = None,
        impact_max: float | None = None,
        jitter: float | None = None,
        hp_max: int | None = None,
        warning_hp: int | None = None,
        dmg_per_pct: float | None = None,
        recover_per_pct: float | None = None,
        rescue_hp_per_100: float | None = None,
        rescue_hours: float | None = None,
        zero_grace_hours: float | None = None,
    ):
        fields: dict[str, float | int | str] = {
            k: v for k, v in {
                "mu_min": mu_min, "mu_max": mu_max,
                "sigma_min": sigma_min, "sigma_max": sigma_max,
                "impact_min": impact_min, "impact_max": impact_max,
                "jitter": jitter, "hp_max": hp_max, "warning_hp": warning_hp,
                "dmg_per_pct": dmg_per_pct, "recover_per_pct": recover_per_pct,
                "rescue_hp_per_100": rescue_hp_per_100,
                "rescue_hours": rescue_hours,
                "zero_grace_hours": zero_grace_hours,
            }.items() if v is not None
        }
        if name is not None:
            fields["display_name"] = name
        if description is not None:
            fields["description"] = description
        if not fields:
            await ctx.reply("変更する項目を指定してください", ephemeral=True)
            return
        try:
            market = await stocks.update_market(market_id, **fields)
        except ValueError as e:
            await ctx.reply(str(e), ephemeral=True)
            return
        await ctx.reply(f"市場 `{market.id}` を更新しました")

    @stockMarket.command(name="delete", brief="※管理者専用 株式市場を削除します")
    @admin_only()
    @commands.guild_only()
    @app_commands.rename(market_id="しじょう")
    @app_commands.autocomplete(market_id=market_autocomplete)
    async def stockMarketDeleteCommand(self, ctx: commands.Context, market_id: str):
        try:
            mid = stocks.normalize_market_id(market_id)
            await stocks.delete_market(mid)
        except ValueError as e:
            await ctx.reply(str(e), ephemeral=True)
            return
        await ctx.reply(f"市場 `{mid}` を削除しました")


async def setup(bot: commands.Bot):
    await bot.add_cog(StockCog(bot))
