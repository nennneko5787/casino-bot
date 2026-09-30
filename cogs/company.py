"""会社口座: 金庫・招待・引出申請 (設立者承認制)・所得税。

一般: /company balance|deposit|withdraw|requests|invite|accept|decline|invites|members|dissolve|pay-arrears|reopen
金庫はメンバーが預入でき、引出は設立者の承認が必要 (設立者本人は即時)。
金庫残高には毎日0時(JST)に1% (最低100) の所得税がかかり、徴収分は焼却する。
3回連続で滞納すると自動で取扱停止になる (追納後に reopen 可)。
"""

import logging
import os
from contextlib import suppress

import discord
from discord import app_commands
from discord.ext import commands, tasks

from objects.exceptions import AmountNotEnough
from services import company, stocks
from services.loan import repay_note
from services.message import buildAmountText, buildGetAmountText

logger = logging.getLogger(__name__)


async def company_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """ユーザー企業 (会社口座あり) のオートコンプリート。"""
    try:
        items = await stocks.get_stocks(active_only=False)
    except Exception:
        logger.exception("会社候補の取得に失敗")
        return []
    current = current.strip().upper()
    return [
        app_commands.Choice(name=f"{s.ticker} ({buildAmountText(s.price)})", value=s.ticker)
        for s in items
        if s.owner_id is not None and current in s.ticker
    ][:25]


class WithdrawView(discord.ui.View):
    """引出申請の承認/拒否ボタン。設立者のみ押せる。"""

    def __init__(self, request_id: int, owner_id: int):
        super().__init__(timeout=24 * 3600)
        self.request_id = request_id
        self.owner_id = owner_id
        self.message: discord.Message | None = None

    @discord.ui.button(label="承認する", style=discord.ButtonStyle.success)
    async def approve(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "設立者のみ承認できます", ephemeral=True
            )
            return
        await interaction.response.defer()
        try:
            result = await company.decide(
                interaction.user.id, self.request_id, True
            )
        except ValueError as e:
            await self._finish(str(e))
            return
        await self._finish(
            f"✅ 引出を承認しました: {buildAmountText(result['amount'])}"
            + repay_note(result["repaid"])
        )

    @discord.ui.button(label="拒否する", style=discord.ButtonStyle.danger)
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "設立者のみ拒否できます", ephemeral=True
            )
            return
        await interaction.response.defer()
        try:
            await company.decide(interaction.user.id, self.request_id, False)
        except ValueError as e:
            await self._finish(str(e))
            return
        await self._finish("引出申請を拒否しました")

    async def _finish(self, text: str):
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        if self.message is not None:
            with suppress(discord.DiscordException):
                await self.message.edit(content=text, view=self)
        self.stop()

    async def on_timeout(self):
        with suppress(Exception):
            await company.expire_requests()
        await self._finish("引出申請は期限切れになりました")


class DissolveConfirmView(discord.ui.View):
    """解散の確認ボタン。設立者のみ押せる。"""

    def __init__(self, owner_id: int):
        super().__init__(timeout=60)
        self.owner_id = owner_id
        self.confirmed = False
        self.message: discord.Message | None = None

    @discord.ui.button(label="解散する", style=discord.ButtonStyle.danger)
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "設立者のみ解散できます", ephemeral=True
            )
            return
        self.confirmed = True
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="やめる", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        self.stop()

    async def on_timeout(self):
        self.stop()


class CompanyCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        await company.ensure_company_schema()
        self.tax_loop.start()

    async def cog_unload(self):
        self.tax_loop.cancel()

    @tasks.loop(minutes=30.0)
    async def tax_loop(self):
        try:
            result = await company.collect_tax()
        except Exception:
            logger.exception("所得税の徴収に失敗")
            return
        if not result["taxed"] and not result["delisted"]:
            return
        msg = (
            f"🏛️ 所得税を徴収しました: {len(result['taxed'])}社・"
            f"焼却 {buildAmountText(result['burned'])}"
        )
        if result["delisted"]:
            msg += "\n💸 連続滞納で取扱停止: " + ", ".join(
                f"`{t}`" for t in result["delisted"]
            )
        for guild in self.bot.guilds:
            with suppress(Exception):
                raw = os.environ.get("log_channel", "").strip()
                ch = guild.get_channel(int(raw)) if raw.isdigit() else None
                if ch is None and raw.isdigit():
                    with suppress(discord.DiscordException):
                        ch = await guild.fetch_channel(int(raw))
                if isinstance(ch, discord.abc.Messageable):
                    await ch.send(msg)

    @tax_loop.before_loop
    async def _before_tax(self):
        await self.bot.wait_until_ready()

    # ---------- /company グループ ----------

    @commands.hybrid_group(name="company", brief="会社の金庫口座を管理します")
    @commands.guild_only()
    async def company(self, ctx: commands.Context):
        await ctx.reply(
            "サブコマンドを指定してください: "
            "balance / deposit / withdraw / requests / invite / accept / "
            "decline / invites / members / dissolve / pay-arrears / reopen",
            ephemeral=True,
        )

    @company.command(name="balance", brief="金庫残高と会社情報を表示します")
    @app_commands.rename(ticker="銘柄")
    @app_commands.autocomplete(ticker=company_autocomplete)
    @commands.guild_only()
    async def companyBalanceCommand(self, ctx: commands.Context, ticker: str):
        try:
            name = stocks.normalize_ticker(ticker)
            stock = await stocks.get_stock(name)
            if stock is None or stock.owner_id is None:
                await ctx.reply(f"{name} は会社の銘柄ではありません", ephemeral=True)
                return
            account = await company.get_account(name)
            member_list = await company.members(name)
        except ValueError as e:
            await ctx.reply(str(e), ephemeral=True)
            return
        owner_line = f"設立者: <@{stock.owner_id}>"
        member_line = (
            "メンバー: "
            + ", ".join(f"<@{m['user_id']}>" for m in member_list[1:])
            if len(member_list) > 1
            else "メンバー: なし (招待で追加)"
        )
        tax_line = f"所得税: 残高の{int(company.TAX_RATE * 100)}%/日 (最低{company.TAX_MIN})"
        if account["arrears"] > 0:
            tax_line += (
                f"\n⚠️滞納 {buildAmountText(account['arrears'])}"
                f" (連続{account['missed']}回・{company.TAX_MISSED_LIMIT}回で取扱停止)"
                "\n`/company pay-arrears` で追納できます"
            )
        await ctx.reply(
            embed=discord.Embed(
                title=f"🏢 {stock.ticker} の金庫",
                description=(
                    f"株価: {buildAmountText(stock.price)}"
                    + ("" if stock.is_active else " [取扱停止]")
                    + f"\n金庫残高: {buildAmountText(account['balance'])}"
                    + f"\n{owner_line}\n{member_line}\n{tax_line}"
                ),
                color=discord.Color.teal(),
            )
        )

    @company.command(name="deposit", brief="会社の金庫に預け入れます")
    @app_commands.rename(ticker="銘柄", amount="金額")
    @app_commands.describe(amount="預ける金額 (1以上)")
    @app_commands.autocomplete(ticker=company_autocomplete)
    @commands.guild_only()
    async def companyDepositCommand(
        self, ctx: commands.Context, ticker: str, amount: int
    ):
        if amount < 1:
            await ctx.reply("預入額は1以上にしてください", ephemeral=True)
            return
        try:
            name = stocks.normalize_ticker(ticker)
            balance = await company.deposit(ctx.author.id, name, amount)
        except ValueError as e:
            await ctx.reply(str(e), ephemeral=True)
            return
        except LookupError:
            raise AmountNotEnough()
        await ctx.reply(
            f"🏦 `{name}` の金庫に {buildAmountText(amount)} を預けました！\n"
            f"金庫残高: {buildAmountText(balance)}"
        )

    @company.command(name="withdraw", brief="金庫から引出を申請します (設立者承認制)")
    @app_commands.rename(ticker="銘柄", amount="金額")
    @app_commands.describe(amount="引き出す金額 (1以上)")
    @app_commands.autocomplete(ticker=company_autocomplete)
    @commands.guild_only()
    async def companyWithdrawCommand(
        self, ctx: commands.Context, ticker: str, amount: int
    ):
        if amount < 1:
            await ctx.reply("引出額は1以上にしてください", ephemeral=True)
            return
        try:
            name = stocks.normalize_ticker(ticker)
            stock = await stocks.get_stock(name)
            if stock is None or stock.owner_id is None:
                await ctx.reply(f"{name} は会社の銘柄ではありません", ephemeral=True)
                return
            result = await company.request_withdraw(ctx.author.id, name, amount)
        except ValueError as e:
            await ctx.reply(str(e), ephemeral=True)
            return
        if result.get("paid"):
            await ctx.reply(
                f"💸 `{name}` の金庫から {buildAmountText(amount)} を引き出しました！\n"
                f"```patch\n{buildGetAmountText(amount, md=True)}\n```"
                + repay_note(result.get("repaid", 0))
            )
            return
        await ctx.reply("申請を受け付けました。設立者の承認待ちです", ephemeral=True)
        view = WithdrawView(result["request_id"], stock.owner_id)
        msg = await ctx.channel.send(
            f"<@{stock.owner_id}> さん、 `{name}` の金庫からの引出申請です\n"
            f"申請者: <@{ctx.author.id}>／金額: {buildAmountText(amount)}"
            f" (申請ID: {result['request_id']})",
            view=view,
        )
        view.message = msg

    @company.command(name="requests", brief="未決の引出申請を確認します")
    @app_commands.rename(ticker="銘柄")
    @app_commands.autocomplete(ticker=company_autocomplete)
    @commands.guild_only()
    async def companyRequestsCommand(self, ctx: commands.Context, ticker: str):
        try:
            name = stocks.normalize_ticker(ticker)
            reqs = await company.pending_requests(name)
        except ValueError as e:
            await ctx.reply(str(e), ephemeral=True)
            return
        if not reqs:
            await ctx.reply("未決の申請はありません", ephemeral=True)
            return
        lines = [
            f"ID `{r['id']}`: <@{r['requester']}> が {buildAmountText(r['amount'])}"
            for r in reqs
        ]
        await ctx.reply(
            "未決の引出申請:\n" + "\n".join(lines)
            + "\n承認/拒否は `/company decide` か申請メッセージのボタンで",
            ephemeral=True,
        )

    @company.command(name="decide", brief="※設立者用 引出申請を承認/拒否します")
    @app_commands.rename(request_id="申請id", approve="承認するなら真")
    @app_commands.describe(request_id="申請メッセージの申請ID", approve="承認はTrue、拒否はFalse")
    @commands.guild_only()
    async def companyDecideCommand(
        self, ctx: commands.Context, request_id: int, approve: bool
    ):
        try:
            result = await company.decide(ctx.author.id, request_id, approve)
        except ValueError as e:
            await ctx.reply(str(e), ephemeral=True)
            return
        if approve:
            await ctx.reply(
                f"✅ 申請ID `{request_id}` を承認しました: "
                f"{buildAmountText(result['amount'])}"
                + repay_note(result.get("repaid", 0))
            )
        else:
            await ctx.reply(f"申請ID `{request_id}` を拒否しました")

    @company.command(name="invite", brief="※設立者用 メンバーを招待します")
    @app_commands.rename(ticker="銘柄", member="相手")
    @app_commands.autocomplete(ticker=company_autocomplete)
    @commands.guild_only()
    async def companyInviteCommand(
        self, ctx: commands.Context, ticker: str, member: discord.Member
    ):
        if member.bot:
            await ctx.reply("Botは招待できません", ephemeral=True)
            return
        try:
            name = stocks.normalize_ticker(ticker)
            await company.invite(ctx.author.id, name, member.id)
        except ValueError as e:
            await ctx.reply(str(e), ephemeral=True)
            return
        await ctx.reply(
            f"<@{member.id}> さんを `{name}` に招待しました！\n"
            f"`/company accept {name}` で参加できます"
        )

    @company.command(name="accept", brief="招待を応諾してメンバーになります")
    @app_commands.rename(ticker="銘柄")
    @app_commands.autocomplete(ticker=company_autocomplete)
    @commands.guild_only()
    async def companyAcceptCommand(self, ctx: commands.Context, ticker: str):
        try:
            name = stocks.normalize_ticker(ticker)
            await company.accept(ctx.author.id, name)
        except ValueError as e:
            await ctx.reply(str(e), ephemeral=True)
            return
        await ctx.reply(f"`{name}` のメンバーになりました！")

    @company.command(name="decline", brief="招待を拒否します")
    @app_commands.rename(ticker="銘柄")
    @app_commands.autocomplete(ticker=company_autocomplete)
    @commands.guild_only()
    async def companyDeclineCommand(self, ctx: commands.Context, ticker: str):
        try:
            name = stocks.normalize_ticker(ticker)
            await company.decline(ctx.author.id, name)
        except ValueError as e:
            await ctx.reply(str(e), ephemeral=True)
            return
        await ctx.reply(f"`{name}` への招待を拒否しました")

    @company.command(name="invites", brief="自分宛ての招待を確認します")
    @commands.guild_only()
    async def companyInvitesCommand(self, ctx: commands.Context):
        invites = await company.my_invites(ctx.author.id)
        if not invites:
            await ctx.reply("招待はありません", ephemeral=True)
            return
        lines = [
            f"`{inv['ticker']}` (招待者: <@{inv['invited_by']}>)"
            for inv in invites
        ]
        await ctx.reply("招待一覧:\n" + "\n".join(lines), ephemeral=True)

    @company.command(name="members", brief="会社のメンバーを確認します")
    @app_commands.rename(ticker="銘柄")
    @app_commands.autocomplete(ticker=company_autocomplete)
    @commands.guild_only()
    async def companyMembersCommand(self, ctx: commands.Context, ticker: str):
        try:
            name = stocks.normalize_ticker(ticker)
            member_list = await company.members(name)
        except ValueError as e:
            await ctx.reply(str(e), ephemeral=True)
            return
        lines = [
            f"<@{m['user_id']}> ({m['role']})" for m in member_list
        ]
        await ctx.reply(f"`{name}` のメンバー:\n" + "\n".join(lines))

    @company.command(name="dissolve", brief="※設立者用 会社を解散します")
    @app_commands.rename(ticker="銘柄")
    @app_commands.autocomplete(ticker=company_autocomplete)
    @commands.guild_only()
    async def companyDissolveCommand(self, ctx: commands.Context, ticker: str):
        try:
            name = stocks.normalize_ticker(ticker)
            stock = await stocks.get_stock(name)
            if stock is None or stock.owner_id is None:
                await ctx.reply(f"{name} は会社の銘柄ではありません", ephemeral=True)
                return
            if stock.owner_id != ctx.author.id:
                await ctx.reply("設立者のみ解散できます", ephemeral=True)
                return
        except ValueError as e:
            await ctx.reply(str(e), ephemeral=True)
            return
        view = DissolveConfirmView(ctx.author.id)
        sent = await ctx.reply(
            f"本当に `{name}` を解散しますか？\n"
            "金庫残高は株主に保有株数比例で返金され、銘柄は消去されます",
            view=view,
        )
        if isinstance(sent, discord.Message):
            view.message = sent
        await view.wait()
        if not view.confirmed:
            with suppress(Exception):
                if isinstance(sent, discord.Message):
                    await sent.edit(content="解散を中止しました", view=None)
            return
        try:
            result = await company.dissolve(ctx.author.id, name)
        except ValueError as e:
            with suppress(Exception):
                if isinstance(sent, discord.Message):
                    await sent.edit(content=str(e), view=None)
            return
        total = sum(a for _, a in result["distributed"])
        with suppress(Exception):
            if isinstance(sent, discord.Message):
                await sent.edit(
                    content=f"`{name}` を解散しました。返金総額: {buildAmountText(total)}",
                    view=None,
                )

    @company.command(name="pay-arrears", brief="所得税の滞納分を追納します")
    @app_commands.rename(ticker="銘柄", amount="金額")
    @app_commands.autocomplete(ticker=company_autocomplete)
    @commands.guild_only()
    async def companyPayArrearsCommand(
        self, ctx: commands.Context, ticker: str, amount: int
    ):
        if amount < 1:
            await ctx.reply("追納額は1以上にしてください", ephemeral=True)
            return
        try:
            name = stocks.normalize_ticker(ticker)
            result = await company.pay_arrears(ctx.author.id, name, amount)
        except ValueError as e:
            await ctx.reply(str(e), ephemeral=True)
            return
        except LookupError:
            raise AmountNotEnough()
        await ctx.reply(
            f"🧾 `{name}` の滞納を {buildAmountText(result['paid'])} 追納しました！\n"
            f"残滞納: {buildAmountText(result['arrears'])}"
        )

    @company.command(name="reopen", brief="※設立者用 滞納解消後に取扱を再開します")
    @app_commands.rename(ticker="銘柄")
    @app_commands.autocomplete(ticker=company_autocomplete)
    @commands.guild_only()
    async def companyReopenCommand(self, ctx: commands.Context, ticker: str):
        try:
            name = stocks.normalize_ticker(ticker)
            await company.reopen_company(ctx.author.id, name)
        except ValueError as e:
            await ctx.reply(str(e), ephemeral=True)
            return
        await ctx.reply(f"`{name}` の取扱を再開しました")


async def setup(bot: commands.Bot):
    await bot.add_cog(CompanyCog(bot))
